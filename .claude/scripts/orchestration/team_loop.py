"""Team loop step over convoy mailbox and subtask state.

This is the first bounded proof path for autonomous team coordination. It
consumes DB-backed mailbox state, writes a convoy-scoped handoff/status message,
and advances a member's bound subtask through the existing convoy service.
"""

from __future__ import annotations

import dataclasses
import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from orchestration.contract import DEFAULT_WORKSPACE_ID
from orchestration.convoy_service import ConvoyService
from orchestration.db import OrchestrationDB
from orchestration.mailbox_service import MailboxService
from orchestration.models import AgentMessage, MessageWithDeliveries, SendMessageInput, Subtask
from orchestration.observability import orchestration_span, update_observation
from orchestration.team_executor import (
    TeamExecutorService,
    TeamExecutorStepResult,
    executor_result_to_dict,
)
from orchestration.team_service import TeamService
from runtime.base import RuntimeResult
from runtime.capabilities import TEXT_REASONING
from runtime.lane_router import run_with_runtime_lanes
from runtime import RuntimeRequest


@dataclass
class TeamLoopStepResult:
    team_id: int
    agent_id: str
    convoy_id: int
    subtask_id: int
    claimed: list[MessageWithDeliveries] = field(default_factory=list)
    reply: AgentMessage | None = None
    runtime: RuntimeResult | None = None
    subtask_before: Subtask | None = None
    subtask_after: Subtask | None = None
    action: str = "noop"
    completed: bool = False
    convoy_completed: bool = False
    newly_ready: list[Subtask] = field(default_factory=list)


@dataclass
class TeamTickResult:
    team_id: int
    selected_action: str
    reason: str
    agent_id: str | None = None
    convoy_id: int | None = None
    subtask_id: int | None = None
    step: TeamLoopStepResult | None = None
    executor: TeamExecutorStepResult | None = None
    waited: bool = False
    error: str | None = None


class TeamLoopService:
    """Runs one framework-owned team-member coordination step."""

    def __init__(self, db: OrchestrationDB):
        self.db = db
        self.team_svc = TeamService(db)
        self.mailbox_svc = MailboxService(db)
        self.convoy_svc = ConvoyService(db)

    def run_member_step(
        self,
        team_id: int,
        agent_id: str,
        *,
        subtask_id: int | None = None,
        reply_body: str | None = None,
        use_runtime: bool = False,
        runtime_lane: str | None = None,
        complete: bool = False,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> TeamLoopStepResult:
        """Claim mail, write a handoff/status reply, and advance one subtask."""
        with orchestration_span(
            "team_loop.run_member_step",
            metadata={
                "team_id": team_id,
                "agent_id": agent_id,
                "subtask_id": subtask_id,
                "complete": complete,
            },
            trace_metadata={"feature_phase": 8, "team_id": team_id},
            expected_exceptions=(ValueError,),
        ):
            team = self.team_svc.get_team_session(team_id, workspace_id=workspace_id)
            if team is None:
                raise ValueError(f"Team {team_id} not found")
            if team.session.convoy_id is None:
                raise ValueError(f"Team {team_id} has no convoy_id set")

            member = next((m for m in team.members if m.agent_id == agent_id), None)
            if member is None:
                raise ValueError(f"Agent '{agent_id}' is not a member of team {team_id}")
            if member.status != "active":
                raise ValueError(f"Agent '{agent_id}' is not active in team {team_id}")

            effective_subtask_id = subtask_id or member.subtask_id
            if effective_subtask_id is None:
                raise ValueError(f"Agent '{agent_id}' has no bound subtask")

            before = self.convoy_svc.get_subtask(effective_subtask_id, workspace_id=workspace_id)
            if before is None:
                raise ValueError(f"Subtask {effective_subtask_id} not found")
            if before.convoy_id != team.session.convoy_id:
                raise ValueError(
                    f"Subtask {effective_subtask_id} belongs to convoy {before.convoy_id}, "
                    f"not team {team_id}'s convoy {team.session.convoy_id}"
                )

            claimed = self.mailbox_svc.claim_deliveries(
                agent_id,
                workspace_id=workspace_id,
                convoy_id=team.session.convoy_id,
                limit=10,
            )
            runtime_result: RuntimeResult | None = None
            if use_runtime:
                runtime_result = self._run_runtime_turn(
                    team_id=team_id,
                    agent_id=agent_id,
                    convoy_id=team.session.convoy_id,
                    subtask=before,
                    claimed=claimed,
                    runtime_lane=runtime_lane,
                )
                reply_body = self._bounded_runtime_text(runtime_result.text)
            reply = self._send_loop_reply(
                team_id=team_id,
                agent_id=agent_id,
                lead_agent_id=team.session.lead_agent_id,
                convoy_id=team.session.convoy_id,
                subtask_id=effective_subtask_id,
                claimed=claimed,
                reply_body=reply_body,
                workspace_id=workspace_id,
            )
            self._ack_claimed(agent_id, claimed, workspace_id=workspace_id)

            action = "mailbox_reply"
            completed_now = False
            convoy_completed = False
            newly_ready: list[Subtask] = []

            current = before
            if current.status == "ready":
                self.team_svc.dispatch_to_executor(
                    team_id,
                    effective_subtask_id,
                    workspace_id=workspace_id,
                )
                current = self._require_subtask(effective_subtask_id, workspace_id)
                action = "dispatched"

            if current.status == "dispatched":
                current = self.convoy_svc.transition_subtask(
                    effective_subtask_id,
                    "running",
                    workspace_id=workspace_id,
                )
                action = "running"

            if complete:
                if current.status in ("dispatched", "stalled"):
                    current = self.convoy_svc.transition_subtask(
                        effective_subtask_id,
                        "running",
                        workspace_id=workspace_id,
                    )
                if current.status != "running":
                    raise ValueError(
                        f"Subtask {effective_subtask_id} must be running before completion "
                        f"(status: {current.status})"
                    )
                newly_ready, convoy_completed = self.convoy_svc.handle_subtask_completion(
                    effective_subtask_id,
                    workspace_id=workspace_id,
                )
                current = self._require_subtask(effective_subtask_id, workspace_id)
                action = "completed"
                completed_now = True

            self.team_svc.ping_activity(team_id, agent_id=agent_id, workspace_id=workspace_id)
            result = TeamLoopStepResult(
                team_id=team_id,
                agent_id=agent_id,
                convoy_id=team.session.convoy_id,
                subtask_id=effective_subtask_id,
                claimed=claimed,
                reply=reply,
                runtime=runtime_result,
                subtask_before=before,
                subtask_after=current,
                action=action,
                completed=completed_now,
                convoy_completed=convoy_completed,
                newly_ready=newly_ready,
            )
            update_observation(
                metadata={
                    "team_id": team_id,
                    "agent_id": agent_id,
                    "convoy_id": team.session.convoy_id,
                    "subtask_id": effective_subtask_id,
                    "claimed_count": len(claimed),
                    "action": action,
                },
                output={"subtask_status": current.status},
            )
            return result

    def _run_runtime_turn(
        self,
        *,
        team_id: int,
        agent_id: str,
        convoy_id: int,
        subtask: Subtask,
        claimed: list[MessageWithDeliveries],
        runtime_lane: str | None,
    ) -> RuntimeResult:
        prompt = self._build_runtime_prompt(
            team_id=team_id,
            agent_id=agent_id,
            convoy_id=convoy_id,
            subtask=subtask,
            claimed=claimed,
        )
        request = RuntimeRequest(
            prompt=prompt,
            cwd=Path.cwd(),
            task_name="team_loop_member_turn",
            capability=TEXT_REASONING,
            max_turns=1,
            allowed_tools=[],
            disallowed_tools=["*"],
            permission_mode="bypassPermissions",
            runtime_lane=runtime_lane,
            metadata={
                "caller": "team_loop",
                "team_id": team_id,
                "agent_id": agent_id,
                "convoy_id": convoy_id,
                "subtask_id": subtask.id,
            },
            system_prompt=(
                "You are a bounded Homie team member turn. Return only the "
                "handoff/status text for the convoy mailbox. Do not call tools, "
                "do not mention implementation details, and keep it under 120 words."
            ),
        )
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(run_with_runtime_lanes(request))
        raise RuntimeError("TeamLoopService runtime mode requires a synchronous caller")

    def _build_runtime_prompt(
        self,
        *,
        team_id: int,
        agent_id: str,
        convoy_id: int,
        subtask: Subtask,
        claimed: list[MessageWithDeliveries],
    ) -> str:
        claimed_lines = "\n".join(
            f"- {entry.message.from_agent}: {entry.message.subject or entry.message.body[:120]}"
            for entry in claimed
        ) or "- none"
        formatted_mail = self._format_claimed_mail(claimed)
        return (
            f"Team: #{team_id}\n"
            f"Agent: {agent_id}\n"
            f"Convoy: #{convoy_id}\n"
            f"Subtask: #{subtask.id} {subtask.title}\n"
            f"Subtask status: {subtask.status}\n\n"
            f"Claimed mailbox summary:\n{claimed_lines}\n\n"
            f"Claimed mailbox context:\n{formatted_mail or 'No claimed mailbox context.'}\n\n"
            "Write the next concise handoff/status reply for the convoy mailbox."
        )

    @staticmethod
    def _format_claimed_mail(claimed: list[MessageWithDeliveries]) -> str:
        if not claimed:
            return ""
        lines: list[str] = ["## Claimed Messages", ""]
        for entry in claimed:
            lines.append(f"**From**: {entry.message.from_agent}")
            if entry.message.subject:
                lines.append(f"**Subject**: {entry.message.subject}")
            lines.append(f"**Type**: {entry.message.message_type}")
            lines.append("")
            lines.append(entry.message.body)
            lines.append("")
            lines.append("---")
            lines.append("")
        return "\n".join(lines).strip()

    @staticmethod
    def _bounded_runtime_text(text: str) -> str:
        cleaned = (text or "").strip()
        if not cleaned:
            return "Loop step completed; no runtime text was returned."
        words = cleaned.split()
        if len(words) > 140:
            cleaned = " ".join(words[:140])
        return cleaned

    def _send_loop_reply(
        self,
        *,
        team_id: int,
        agent_id: str,
        lead_agent_id: str,
        convoy_id: int,
        subtask_id: int,
        claimed: list[MessageWithDeliveries],
        reply_body: str | None,
        workspace_id: int,
    ) -> AgentMessage:
        recipients = [lead_agent_id] if lead_agent_id != agent_id else []
        if not recipients:
            rows = self.db.conn.execute(
                """SELECT agent_id FROM team_members
                   WHERE workspace_id = ? AND team_session_id = ? AND agent_id != ?
                     AND status = 'active'
                   ORDER BY id LIMIT 3""",
                (workspace_id, team_id, agent_id),
            ).fetchall()
            recipients = [r["agent_id"] for r in rows]
        if not recipients:
            recipients = [agent_id]

        claimed_summary = "\n".join(
            f"- {entry.message.from_agent}: {entry.message.subject or entry.message.body[:80]}"
            for entry in claimed
        ) or "- no pending mailbox items"
        body = reply_body or (
            f"Loop step complete for subtask #{subtask_id}.\n\n"
            f"Mailbox consumed:\n{claimed_summary}"
        )
        return self.mailbox_svc.send_message(
            SendMessageInput(
                from_agent=agent_id,
                recipients=recipients,
                body=body,
                convoy_id=convoy_id,
                subject=f"Loop step: subtask {subtask_id}",
                msg_type="work_handoff",
                message_type="handoff",
            ),
            workspace_id=workspace_id,
        )

    def _ack_claimed(
        self,
        agent_id: str,
        claimed: list[MessageWithDeliveries],
        *,
        workspace_id: int,
    ) -> None:
        for entry in claimed:
            for delivery in entry.deliveries:
                if (
                    delivery.recipient_agent == agent_id
                    and delivery.status == "claimed"
                    and delivery.claim_token
                ):
                    self.mailbox_svc.ack_delivery(
                        delivery.id,
                        recipient_agent=agent_id,
                        claim_token=delivery.claim_token,
                        workspace_id=workspace_id,
                    )

    def _require_subtask(self, subtask_id: int, workspace_id: int) -> Subtask:
        subtask = self.convoy_svc.get_subtask(subtask_id, workspace_id=workspace_id)
        if subtask is None:
            raise ValueError(f"Subtask {subtask_id} not found")
        return subtask


class TeamTickService:
    """Select and run at most one bounded team action."""

    def __init__(
        self,
        db: OrchestrationDB,
        *,
        executor_svc: TeamExecutorService | None = None,
    ):
        self.db = db
        self.team_svc = TeamService(db)
        self.mailbox_svc = MailboxService(db)
        self.convoy_svc = ConvoyService(db)
        self.loop_svc = TeamLoopService(db)
        self.executor_svc = executor_svc or TeamExecutorService(db)

    def run_team_tick(
        self,
        team_id: int,
        *,
        agent_id: str | None = None,
        use_runtime: bool = False,
        runtime_lane: str | None = None,
        complete_running: bool = False,
        execute_running: bool = False,
        executor_command: str = "git_status",
        executor_cwd: str | None = None,
        complete_on_executor_success: bool = False,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> TeamTickResult:
        """Choose one claim/respond/advance/complete action, or wait."""
        with orchestration_span(
            "team_tick.run_team_tick",
            metadata={
                "team_id": team_id,
                "agent_id": agent_id,
                "use_runtime": use_runtime,
                "runtime_lane": runtime_lane,
                "complete_running": complete_running,
                "execute_running": execute_running,
                "executor_command": executor_command,
                "complete_on_executor_success": complete_on_executor_success,
            },
            trace_metadata={"feature_phase": 9, "team_id": team_id},
            expected_exceptions=(ValueError,),
        ):
            team = self.team_svc.get_team_session(team_id, workspace_id=workspace_id)
            if team is None:
                raise ValueError(f"Team {team_id} not found")
            if team.session.convoy_id is None:
                raise ValueError(f"Team {team_id} has no convoy_id set")
            if team.session.status not in ("active", "idle"):
                return self._wait(
                    team_id=team_id,
                    convoy_id=team.session.convoy_id,
                    reason=f"team status is {team.session.status}",
                )

            members = [m for m in team.members if m.status == "active"]
            if agent_id is not None:
                selected = [m for m in members if m.agent_id == agent_id]
                if not selected:
                    raise ValueError(f"Agent '{agent_id}' is not active in team {team_id}")
                members = selected + [m for m in members if m.agent_id != agent_id]

            if not members:
                return self._wait(
                    team_id=team_id,
                    convoy_id=team.session.convoy_id,
                    reason="no active team members",
                )

            for member in members:
                inbox = self.mailbox_svc.get_inbox(
                    member.agent_id,
                    workspace_id=workspace_id,
                    convoy_id=team.session.convoy_id,
                )
                if inbox and member.subtask_id is not None:
                    return self._run_selected(
                        selected_action="claim_respond",
                        reason=f"{len(inbox)} pending convoy mailbox item(s)",
                        team_id=team_id,
                        member_agent_id=member.agent_id,
                        subtask_id=member.subtask_id,
                        convoy_id=team.session.convoy_id,
                        use_runtime=use_runtime,
                        runtime_lane=runtime_lane,
                        complete=False,
                        workspace_id=workspace_id,
                    )

            for member in members:
                subtask = self._member_subtask(member.subtask_id, workspace_id)
                if subtask and subtask.convoy_id == team.session.convoy_id and subtask.status in ("ready", "dispatched"):
                    return self._run_selected(
                        selected_action="advance_ready",
                        reason=f"subtask #{subtask.id} is {subtask.status}",
                        team_id=team_id,
                        member_agent_id=member.agent_id,
                        subtask_id=subtask.id,
                        convoy_id=team.session.convoy_id,
                        use_runtime=use_runtime,
                        runtime_lane=runtime_lane,
                        complete=False,
                        workspace_id=workspace_id,
                    )

            for member in members:
                subtask = self._member_subtask(member.subtask_id, workspace_id)
                if subtask and subtask.convoy_id == team.session.convoy_id and subtask.status == "running":
                    if execute_running:
                        return self._run_executor_selected(
                            reason=f"subtask #{subtask.id} is running and executor policy is enabled",
                            team_id=team_id,
                            member_agent_id=member.agent_id,
                            subtask_id=subtask.id,
                            convoy_id=team.session.convoy_id,
                            command_key=executor_command,
                            cwd=executor_cwd,
                            complete_on_success=complete_on_executor_success,
                            workspace_id=workspace_id,
                        )
                    if not complete_running:
                        return self._wait(
                            team_id=team_id,
                            convoy_id=team.session.convoy_id,
                            agent_id=member.agent_id,
                            subtask_id=subtask.id,
                            reason=f"subtask #{subtask.id} is running; completion policy is disabled",
                        )
                    return self._run_selected(
                        selected_action="complete_running",
                        reason=f"subtask #{subtask.id} is running and completion policy is enabled",
                        team_id=team_id,
                        member_agent_id=member.agent_id,
                        subtask_id=subtask.id,
                        convoy_id=team.session.convoy_id,
                        use_runtime=use_runtime,
                        runtime_lane=runtime_lane,
                        complete=True,
                        workspace_id=workspace_id,
                    )

            return self._wait(
                team_id=team_id,
                convoy_id=team.session.convoy_id,
                reason="no pending mail or actionable bound subtasks",
            )

    def _run_selected(
        self,
        *,
        selected_action: str,
        reason: str,
        team_id: int,
        member_agent_id: str,
        subtask_id: int,
        convoy_id: int,
        use_runtime: bool,
        runtime_lane: str | None,
        complete: bool,
        workspace_id: int,
    ) -> TeamTickResult:
        try:
            step = self.loop_svc.run_member_step(
                team_id,
                member_agent_id,
                subtask_id=subtask_id,
                use_runtime=use_runtime,
                runtime_lane=runtime_lane,
                complete=complete,
                workspace_id=workspace_id,
            )
        except Exception as exc:  # noqa: BLE001 - tick results must stay bounded.
            update_observation(
                level="WARNING",
                status_message=str(exc),
                metadata={
                    "team_id": team_id,
                    "agent_id": member_agent_id,
                    "subtask_id": subtask_id,
                    "selected_action": selected_action,
                    "error_type": type(exc).__name__,
                },
            )
            return TeamTickResult(
                team_id=team_id,
                selected_action=selected_action,
                reason=reason,
                agent_id=member_agent_id,
                convoy_id=convoy_id,
                subtask_id=subtask_id,
                error=f"{type(exc).__name__}: {exc}",
            )
        return TeamTickResult(
            team_id=team_id,
            selected_action=selected_action,
            reason=reason,
            agent_id=member_agent_id,
            convoy_id=step.convoy_id,
            subtask_id=subtask_id,
            step=step,
        )

    def _run_executor_selected(
        self,
        *,
        reason: str,
        team_id: int,
        member_agent_id: str,
        subtask_id: int,
        convoy_id: int,
        command_key: str,
        cwd: str | None,
        complete_on_success: bool,
        workspace_id: int,
    ) -> TeamTickResult:
        try:
            executor = self.executor_svc.run_executor_step(
                team_id,
                agent_id=member_agent_id,
                subtask_id=subtask_id,
                command_key=command_key,
                cwd=cwd,
                complete_on_success=complete_on_success,
                workspace_id=workspace_id,
            )
        except Exception as exc:  # noqa: BLE001 - tick results must stay bounded.
            update_observation(
                level="WARNING",
                status_message=str(exc),
                metadata={
                    "team_id": team_id,
                    "agent_id": member_agent_id,
                    "subtask_id": subtask_id,
                    "selected_action": "executor_step",
                    "executor_command": command_key,
                    "error_type": type(exc).__name__,
                },
            )
            return TeamTickResult(
                team_id=team_id,
                selected_action="executor_step",
                reason=reason,
                agent_id=member_agent_id,
                convoy_id=convoy_id,
                subtask_id=subtask_id,
                error=f"{type(exc).__name__}: {exc}",
            )
        return TeamTickResult(
            team_id=team_id,
            selected_action="executor_step",
            reason=reason,
            agent_id=member_agent_id,
            convoy_id=convoy_id,
            subtask_id=subtask_id,
            executor=executor,
        )

    def _wait(
        self,
        *,
        team_id: int,
        convoy_id: int | None,
        reason: str,
        agent_id: str | None = None,
        subtask_id: int | None = None,
    ) -> TeamTickResult:
        update_observation(
            metadata={
                "team_id": team_id,
                "convoy_id": convoy_id,
                "agent_id": agent_id,
                "subtask_id": subtask_id,
                "action": "wait",
                "reason": reason,
            },
            output={"waited": True},
        )
        return TeamTickResult(
            team_id=team_id,
            selected_action="wait",
            reason=reason,
            agent_id=agent_id,
            convoy_id=convoy_id,
            subtask_id=subtask_id,
            waited=True,
        )

    def _member_subtask(self, subtask_id: int | None, workspace_id: int) -> Subtask | None:
        if subtask_id is None:
            return None
        return self.convoy_svc.get_subtask(subtask_id, workspace_id=workspace_id)


def result_to_dict(result: TeamLoopStepResult) -> dict:
    return {
        "team_id": result.team_id,
        "agent_id": result.agent_id,
        "convoy_id": result.convoy_id,
        "subtask_id": result.subtask_id,
        "claimed": [dataclasses.asdict(entry) for entry in result.claimed],
        "claimed_count": len(result.claimed),
        "reply": dataclasses.asdict(result.reply) if result.reply else None,
        "runtime": {
            "runtime_lane": result.runtime.runtime_lane,
            "provider": result.runtime.provider,
            "model": result.runtime.model,
            "session_id": result.runtime.session_id,
            "tool_call_count": result.runtime.tool_call_count,
        }
        if result.runtime
        else None,
        "subtask_before": dataclasses.asdict(result.subtask_before)
        if result.subtask_before
        else None,
        "subtask_after": dataclasses.asdict(result.subtask_after)
        if result.subtask_after
        else None,
        "action": result.action,
        "completed": result.completed,
        "convoy_completed": result.convoy_completed,
        "newly_ready": [dataclasses.asdict(subtask) for subtask in result.newly_ready],
    }


def tick_result_to_dict(result: TeamTickResult) -> dict:
    return {
        "team_id": result.team_id,
        "selected_action": result.selected_action,
        "reason": result.reason,
        "agent_id": result.agent_id,
        "convoy_id": result.convoy_id,
        "subtask_id": result.subtask_id,
        "step": result_to_dict(result.step) if result.step else None,
        "executor": executor_result_to_dict(result.executor) if result.executor else None,
        "waited": result.waited,
        "error": result.error,
    }
