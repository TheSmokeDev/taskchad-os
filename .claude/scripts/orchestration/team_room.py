"""Homie-native team room workflows over convoy, mailbox, and runtime lanes.

The v1 workflow is deliberately concrete: one Growth Boardroom ritual that
creates a real team session, moves work through convoy subtasks, and keeps raw
intermediate turns in orchestration state instead of the chat orchestrator
context.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass

from orchestration.contract import DEFAULT_WORKSPACE_ID
from orchestration.convoy_service import ConvoyService
from orchestration.db import OrchestrationDB
from orchestration.mailbox_service import MailboxService
from orchestration.models import (
    AddTeamMemberInput,
    AgentMessage,
    ConvoyWithSubtasks,
    CreateConvoyInput,
    CreateSubtaskInput,
    CreateTeamSessionInput,
    SendMessageInput,
    Subtask,
    TeamSessionWithMembers,
)
from orchestration.observability import orchestration_span, update_observation
from orchestration.team_loop import TeamLoopService, TeamLoopStepResult, result_to_dict
from orchestration.team_service import TeamService

TEAM_ROOM_WORKFLOW_ID = "growth_boardroom"
TEAM_ROOM_LEAD_AGENT_ID = "teamroom-lead"
TEAM_ROOM_LEAD_AGENT_NAME = "Team Room Lead"


@dataclass(frozen=True)
class TeamRoomRoleSpec:
    key: str
    agent_id: str
    agent_name: str
    subtask_title: str
    proposal_prompt: str
    crosstalk_prompt: str
    revision_prompt: str


@dataclass
class TeamRoomTurn:
    phase: str
    role: TeamRoomRoleSpec
    step: TeamLoopStepResult


@dataclass
class TeamRoomWorkflowResult:
    workflow_id: str
    goal: str
    context: str | None
    lead_frame: str
    convoy: ConvoyWithSubtasks
    team: TeamSessionWithMembers
    proposal_messages: list[AgentMessage]
    proposal_turns: list[TeamRoomTurn]
    crosstalk_messages: list[AgentMessage]
    crosstalk_turns: list[TeamRoomTurn]
    reviewer_message: AgentMessage
    reviewer_turn: TeamRoomTurn
    revision_messages: list[AgentMessage]
    revision_turns: list[TeamRoomTurn]
    synthesis_message: AgentMessage
    final_turn: TeamRoomTurn
    final_brief: str


TEAM_ROOM_DEPARTMENT_ROLES: tuple[TeamRoomRoleSpec, ...] = (
    TeamRoomRoleSpec(
        key="sales",
        agent_id="teamroom-sales",
        agent_name="Sales",
        subtask_title="Sales growth proposal",
        proposal_prompt=(
            "Pitch the sales angle: buyer, wedge offer, first conversation, "
            "qualification, objection handling, and revenue path."
        ),
        crosstalk_prompt=(
            "React to the other departments. Build on one useful idea, challenge "
            "one weak assumption, or explicitly pass if sales has no useful delta."
        ),
        revision_prompt=(
            "Revise the sales plan after cross-talk and adversarial review. Name "
            "the first sales motion, owner, and validation signal."
        ),
    ),
    TeamRoomRoleSpec(
        key="marketing",
        agent_id="teamroom-marketing",
        agent_name="Marketing",
        subtask_title="Marketing growth proposal",
        proposal_prompt=(
            "Pitch the marketing angle: positioning, audience, channel, proof, "
            "campaign hook, and message hierarchy."
        ),
        crosstalk_prompt=(
            "React to the other departments. Build on one useful idea, challenge "
            "one weak assumption, or explicitly pass if marketing has no useful delta."
        ),
        revision_prompt=(
            "Revise the marketing plan after cross-talk and adversarial review. "
            "Name the campaign, proof asset, owner, and validation signal."
        ),
    ),
    TeamRoomRoleSpec(
        key="product_frontend",
        agent_id="teamroom-product",
        agent_name="Product/Frontend",
        subtask_title="Product and frontend growth proposal",
        proposal_prompt=(
            "Pitch the product/frontend angle: user journey, page/app changes, "
            "conversion path, friction removal, and implementation task."
        ),
        crosstalk_prompt=(
            "React to the other departments. Build on one useful idea, challenge "
            "one weak assumption, or explicitly pass if product has no useful delta."
        ),
        revision_prompt=(
            "Revise the product/frontend plan after cross-talk and adversarial "
            "review. Name the first shippable change, owner, and validation signal."
        ),
    ),
    TeamRoomRoleSpec(
        key="ops",
        agent_id="teamroom-ops",
        agent_name="Ops",
        subtask_title="Ops growth proposal",
        proposal_prompt=(
            "Pitch the ops angle: execution order, owners, instrumentation, "
            "handoffs, bottlenecks, and operating cadence."
        ),
        crosstalk_prompt=(
            "React to the other departments. Build on one useful idea, challenge "
            "one weak assumption, or explicitly pass if ops has no useful delta."
        ),
        revision_prompt=(
            "Revise the ops plan after cross-talk and adversarial review. Name "
            "the launch sequence, owner, and validation signal."
        ),
    ),
)

TEAM_ROOM_REVIEWER = TeamRoomRoleSpec(
    key="adversarial_reviewer",
    agent_id="teamroom-reviewer",
    agent_name="Adversarial Reviewer",
    subtask_title="Adversarial boardroom review",
    proposal_prompt=(
        "Challenge the department proposals and cross-talk. Identify weak claims, "
        "missing proof, false consensus, hidden dependencies, and the highest-risk trap."
    ),
    crosstalk_prompt="",
    revision_prompt="",
)

TEAM_ROOM_SYNTHESIZER = TeamRoomRoleSpec(
    key="synthesizer",
    agent_id="teamroom-synthesizer",
    agent_name="Synthesizer",
    subtask_title="Final boardroom synthesis",
    proposal_prompt=(
        "Synthesize the revised department plans into one decision brief with "
        "decision, bets, risks, objections, and next actions."
    ),
    crosstalk_prompt="",
    revision_prompt="",
)


class TeamRoomWorkflowService:
    """Runs bounded team-room rituals through existing orchestration services."""

    def __init__(self, db: OrchestrationDB):
        self.db = db
        self.convoy_svc = ConvoyService(db)
        self.mailbox_svc = MailboxService(db)
        self.team_svc = TeamService(db)
        self.loop_svc = TeamLoopService(db)

    def run_team_room(
        self,
        *,
        goal: str,
        workflow_id: str = TEAM_ROOM_WORKFLOW_ID,
        context: str | None = None,
        use_runtime: bool = False,
        runtime_lane: str | None = None,
        max_rounds: int = 1,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> TeamRoomWorkflowResult:
        """Run the Growth Boardroom workflow and return a bounded final brief."""
        normalized_goal = (goal or "").strip()
        if not normalized_goal:
            raise ValueError("goal is required")
        if workflow_id != TEAM_ROOM_WORKFLOW_ID:
            raise ValueError(f"Unsupported team room workflow: {workflow_id}")
        if max_rounds != 1:
            raise ValueError("growth_boardroom v1 supports max_rounds=1")
        normalized_context = (context or "").strip() or None

        with orchestration_span(
            "team_room.run_team_room",
            metadata={
                "workflow_id": workflow_id,
                "use_runtime": use_runtime,
                "runtime_lane": runtime_lane,
                "max_rounds": max_rounds,
            },
            trace_metadata={"feature_phase": 12, "workflow_id": workflow_id},
            expected_exceptions=(ValueError,),
        ):
            lead_frame = self._build_lead_frame(normalized_goal, normalized_context)
            convoy = self._create_convoy(
                normalized_goal,
                normalized_context,
                workflow_id=workflow_id,
                workspace_id=workspace_id,
            )
            team = self._create_team(
                convoy,
                goal=normalized_goal,
                workflow_id=workflow_id,
                workspace_id=workspace_id,
            )

            proposal_messages = self._seed_proposal_briefs(
                lead_frame=lead_frame,
                convoy_id=convoy.convoy.id,
                workspace_id=workspace_id,
            )
            proposal_turns = self._run_department_phase(
                team.session.id,
                convoy.subtasks[:4],
                phase="proposal",
                reply_builder=lambda role: self._default_proposal_reply(role, normalized_goal),
                use_runtime=use_runtime,
                runtime_lane=runtime_lane,
                workspace_id=workspace_id,
            )

            crosstalk_messages = self._send_crosstalk_briefs(
                lead_frame=lead_frame,
                convoy_id=convoy.convoy.id,
                proposal_turns=proposal_turns,
                workspace_id=workspace_id,
            )
            crosstalk_turns = self._run_department_phase(
                team.session.id,
                convoy.subtasks[4:8],
                phase="crosstalk",
                reply_builder=lambda role: self._default_crosstalk_reply(role, normalized_goal),
                use_runtime=use_runtime,
                runtime_lane=runtime_lane,
                workspace_id=workspace_id,
            )

            reviewer_message = self._send_reviewer_brief(
                lead_frame=lead_frame,
                convoy_id=convoy.convoy.id,
                proposal_turns=proposal_turns,
                crosstalk_turns=crosstalk_turns,
                workspace_id=workspace_id,
            )
            reviewer_step = self.loop_svc.run_member_step(
                team.session.id,
                TEAM_ROOM_REVIEWER.agent_id,
                subtask_id=convoy.subtasks[8].id,
                reply_body=self._default_reviewer_reply(normalized_goal),
                use_runtime=use_runtime,
                runtime_lane=runtime_lane,
                complete=True,
                workspace_id=workspace_id,
            )
            reviewer_turn = TeamRoomTurn(
                phase="adversarial_review",
                role=TEAM_ROOM_REVIEWER,
                step=reviewer_step,
            )

            revision_messages = self._send_revision_briefs(
                lead_frame=lead_frame,
                convoy_id=convoy.convoy.id,
                proposal_turns=proposal_turns,
                crosstalk_turns=crosstalk_turns,
                reviewer_turn=reviewer_turn,
                workspace_id=workspace_id,
            )
            revision_turns = self._run_department_phase(
                team.session.id,
                convoy.subtasks[9:13],
                phase="revision",
                reply_builder=lambda role: self._default_revision_reply(role, normalized_goal),
                use_runtime=use_runtime,
                runtime_lane=runtime_lane,
                workspace_id=workspace_id,
            )

            synthesis_message = self._send_synthesis_brief(
                lead_frame=lead_frame,
                convoy_id=convoy.convoy.id,
                proposal_turns=proposal_turns,
                crosstalk_turns=crosstalk_turns,
                reviewer_turn=reviewer_turn,
                revision_turns=revision_turns,
                workspace_id=workspace_id,
            )
            final_step = self.loop_svc.run_member_step(
                team.session.id,
                TEAM_ROOM_SYNTHESIZER.agent_id,
                subtask_id=convoy.subtasks[13].id,
                reply_body=self._default_final_brief(normalized_goal),
                use_runtime=use_runtime,
                runtime_lane=runtime_lane,
                complete=True,
                workspace_id=workspace_id,
            )
            final_turn = TeamRoomTurn(
                phase="synthesis",
                role=TEAM_ROOM_SYNTHESIZER,
                step=final_step,
            )

            refreshed_convoy = self.convoy_svc.get_convoy(
                convoy.convoy.id,
                workspace_id=workspace_id,
            )
            refreshed_team = self.team_svc.get_team_session(
                team.session.id,
                workspace_id=workspace_id,
            )
            if refreshed_convoy is None or refreshed_team is None:
                raise RuntimeError("Team room workflow state disappeared after run")

            final_brief = final_step.reply.body if final_step.reply else ""
            update_observation(
                metadata={
                    "workflow_id": workflow_id,
                    "convoy_id": refreshed_convoy.convoy.id,
                    "team_id": refreshed_team.session.id,
                    "convoy_status": refreshed_convoy.convoy.status,
                    "proposal_count": len(proposal_turns),
                    "crosstalk_count": len(crosstalk_turns),
                    "revision_count": len(revision_turns),
                },
                output={"final_brief_chars": len(final_brief)},
            )
            return TeamRoomWorkflowResult(
                workflow_id=workflow_id,
                goal=normalized_goal,
                context=normalized_context,
                lead_frame=lead_frame,
                convoy=refreshed_convoy,
                team=refreshed_team,
                proposal_messages=proposal_messages,
                proposal_turns=proposal_turns,
                crosstalk_messages=crosstalk_messages,
                crosstalk_turns=crosstalk_turns,
                reviewer_message=reviewer_message,
                reviewer_turn=reviewer_turn,
                revision_messages=revision_messages,
                revision_turns=revision_turns,
                synthesis_message=synthesis_message,
                final_turn=final_turn,
                final_brief=final_brief,
            )

    def _create_convoy(
        self,
        goal: str,
        context: str | None,
        *,
        workflow_id: str,
        workspace_id: int,
    ) -> ConvoyWithSubtasks:
        metadata_base = {
            "workflow": "team_room",
            "workflow_id": workflow_id,
            "goal_excerpt": _clip_text(goal, max_chars=180),
            "has_context": bool(context),
        }
        proposal_subtasks = [
            CreateSubtaskInput(
                title=role.subtask_title,
                description=self._subtask_description(
                    goal,
                    context,
                    role.proposal_prompt,
                    phase="proposal",
                ),
                assigned_agent_id=role.agent_id,
                assigned_agent_name=role.agent_name,
                metadata=json.dumps({**metadata_base, "phase": "proposal", "role": role.key}, sort_keys=True),
            )
            for role in TEAM_ROOM_DEPARTMENT_ROLES
        ]
        crosstalk_subtasks = [
            CreateSubtaskInput(
                title=f"{role.agent_name} cross-talk response",
                description=self._subtask_description(
                    goal,
                    context,
                    role.crosstalk_prompt,
                    phase="crosstalk",
                ),
                assigned_agent_id=role.agent_id,
                assigned_agent_name=role.agent_name,
                depends_on_subtask_indexes=[0, 1, 2, 3],
                metadata=json.dumps({**metadata_base, "phase": "crosstalk", "role": role.key}, sort_keys=True),
            )
            for role in TEAM_ROOM_DEPARTMENT_ROLES
        ]
        reviewer_subtask = CreateSubtaskInput(
            title=TEAM_ROOM_REVIEWER.subtask_title,
            description=self._subtask_description(
                goal,
                context,
                TEAM_ROOM_REVIEWER.proposal_prompt,
                phase="adversarial_review",
            ),
            assigned_agent_id=TEAM_ROOM_REVIEWER.agent_id,
            assigned_agent_name=TEAM_ROOM_REVIEWER.agent_name,
            depends_on_subtask_indexes=[4, 5, 6, 7],
            metadata=json.dumps(
                {**metadata_base, "phase": "adversarial_review", "role": TEAM_ROOM_REVIEWER.key},
                sort_keys=True,
            ),
        )
        revision_subtasks = [
            CreateSubtaskInput(
                title=f"{role.agent_name} revised plan",
                description=self._subtask_description(
                    goal,
                    context,
                    role.revision_prompt,
                    phase="revision",
                ),
                assigned_agent_id=role.agent_id,
                assigned_agent_name=role.agent_name,
                depends_on_subtask_indexes=[8],
                metadata=json.dumps({**metadata_base, "phase": "revision", "role": role.key}, sort_keys=True),
            )
            for role in TEAM_ROOM_DEPARTMENT_ROLES
        ]
        synthesis_subtask = CreateSubtaskInput(
            title=TEAM_ROOM_SYNTHESIZER.subtask_title,
            description=self._subtask_description(
                goal,
                context,
                TEAM_ROOM_SYNTHESIZER.proposal_prompt,
                phase="synthesis",
            ),
            assigned_agent_id=TEAM_ROOM_SYNTHESIZER.agent_id,
            assigned_agent_name=TEAM_ROOM_SYNTHESIZER.agent_name,
            depends_on_subtask_indexes=[9, 10, 11, 12],
            metadata=json.dumps(
                {**metadata_base, "phase": "synthesis", "role": TEAM_ROOM_SYNTHESIZER.key},
                sort_keys=True,
            ),
        )
        return self.convoy_svc.create_convoy(
            CreateConvoyInput(
                title=f"Team Room: {_clip_text(goal, max_chars=80)}",
                description=(
                    "Homie-native Growth Boardroom workflow: department proposals, "
                    "cross-talk, adversarial review, revisions, and synthesis."
                ),
                created_by=TEAM_ROOM_LEAD_AGENT_ID,
                repo_path=None,
                decomposition_mode="manual",
                subtasks=[
                    *proposal_subtasks,
                    *crosstalk_subtasks,
                    reviewer_subtask,
                    *revision_subtasks,
                    synthesis_subtask,
                ],
            ),
            workspace_id=workspace_id,
        )

    def _create_team(
        self,
        convoy: ConvoyWithSubtasks,
        *,
        goal: str,
        workflow_id: str,
        workspace_id: int,
    ) -> TeamSessionWithMembers:
        team = self.team_svc.create_team_session(
            CreateTeamSessionInput(
                team_name="Growth Boardroom",
                lead_agent_id=TEAM_ROOM_LEAD_AGENT_ID,
                lead_agent_name=TEAM_ROOM_LEAD_AGENT_NAME,
                convoy_id=convoy.convoy.id,
                backend_type="local",
                metadata=json.dumps(
                    {
                        "workflow": "team_room",
                        "workflow_id": workflow_id,
                        "goal_excerpt": _clip_text(goal, max_chars=180),
                    },
                    sort_keys=True,
                ),
            ),
            workspace_id=workspace_id,
        )
        all_roles = [
            *TEAM_ROOM_DEPARTMENT_ROLES,
            TEAM_ROOM_REVIEWER,
            TEAM_ROOM_SYNTHESIZER,
        ]
        for role in all_roles:
            subtask = self._first_subtask_for_agent(convoy.subtasks, role.agent_id)
            self.team_svc.add_member(
                team.session.id,
                AddTeamMemberInput(
                    agent_id=role.agent_id,
                    agent_name=role.agent_name,
                    role="worker",
                    subtask_id=subtask.id,
                ),
                workspace_id=workspace_id,
            )
        refreshed = self.team_svc.get_team_session(team.session.id, workspace_id=workspace_id)
        if refreshed is None:
            raise RuntimeError(f"Failed to read back team session {team.session.id}")
        return refreshed

    def _seed_proposal_briefs(
        self,
        *,
        lead_frame: str,
        convoy_id: int,
        workspace_id: int,
    ) -> list[AgentMessage]:
        messages: list[AgentMessage] = []
        for role in TEAM_ROOM_DEPARTMENT_ROLES:
            messages.append(
                self.mailbox_svc.send_message(
                    SendMessageInput(
                        from_agent=TEAM_ROOM_LEAD_AGENT_ID,
                        recipients=[role.agent_id],
                        convoy_id=convoy_id,
                        subject=f"Growth boardroom proposal brief: {role.agent_name}",
                        body=(
                            f"{lead_frame}\n\n"
                            f"{role.proposal_prompt}\n\n"
                            "Return one bounded written turn from your department. "
                            "Name what you would do, why, owner, and validation signal."
                        ),
                        message_type="message",
                        msg_type="task_assignment",
                    ),
                    workspace_id=workspace_id,
                )
            )
        return messages

    def _send_crosstalk_briefs(
        self,
        *,
        lead_frame: str,
        convoy_id: int,
        proposal_turns: list[TeamRoomTurn],
        workspace_id: int,
    ) -> list[AgentMessage]:
        peer_context = self._format_turns(proposal_turns)
        messages: list[AgentMessage] = []
        for role in TEAM_ROOM_DEPARTMENT_ROLES:
            messages.append(
                self.mailbox_svc.send_message(
                    SendMessageInput(
                        from_agent=TEAM_ROOM_LEAD_AGENT_ID,
                        recipients=[role.agent_id],
                        convoy_id=convoy_id,
                        subject=f"Cross-talk round: {role.agent_name}",
                        body=(
                            f"{lead_frame}\n\n"
                            f"{role.crosstalk_prompt}\n\n"
                            "Department proposals:\n"
                            f"{peer_context}"
                        ),
                        message_type="message",
                        msg_type="work_handoff",
                    ),
                    workspace_id=workspace_id,
                )
            )
        return messages

    def _send_reviewer_brief(
        self,
        *,
        lead_frame: str,
        convoy_id: int,
        proposal_turns: list[TeamRoomTurn],
        crosstalk_turns: list[TeamRoomTurn],
        workspace_id: int,
    ) -> AgentMessage:
        return self.mailbox_svc.send_message(
            SendMessageInput(
                from_agent=TEAM_ROOM_LEAD_AGENT_ID,
                recipients=[TEAM_ROOM_REVIEWER.agent_id],
                convoy_id=convoy_id,
                subject="Growth boardroom adversarial review brief",
                body=(
                    f"{lead_frame}\n\n"
                    f"{TEAM_ROOM_REVIEWER.proposal_prompt}\n\n"
                    "Department proposals:\n"
                    f"{self._format_turns(proposal_turns)}\n\n"
                    "Cross-talk:\n"
                    f"{self._format_turns(crosstalk_turns)}"
                ),
                message_type="message",
                msg_type="verifier_feedback",
            ),
            workspace_id=workspace_id,
        )

    def _send_revision_briefs(
        self,
        *,
        lead_frame: str,
        convoy_id: int,
        proposal_turns: list[TeamRoomTurn],
        crosstalk_turns: list[TeamRoomTurn],
        reviewer_turn: TeamRoomTurn,
        workspace_id: int,
    ) -> list[AgentMessage]:
        reviewer_body = reviewer_turn.step.reply.body if reviewer_turn.step.reply else ""
        messages: list[AgentMessage] = []
        for role in TEAM_ROOM_DEPARTMENT_ROLES:
            messages.append(
                self.mailbox_svc.send_message(
                    SendMessageInput(
                        from_agent=TEAM_ROOM_REVIEWER.agent_id,
                        recipients=[role.agent_id],
                        convoy_id=convoy_id,
                        subject=f"Revision round: {role.agent_name}",
                        body=(
                            f"{lead_frame}\n\n"
                            f"{role.revision_prompt}\n\n"
                            "Original proposals:\n"
                            f"{self._format_turns(proposal_turns)}\n\n"
                            "Cross-talk:\n"
                            f"{self._format_turns(crosstalk_turns)}\n\n"
                            f"Adversarial review:\n{reviewer_body}"
                        ),
                        message_type="interrupt",
                        msg_type="verifier_feedback",
                    ),
                    workspace_id=workspace_id,
                )
            )
        return messages

    def _send_synthesis_brief(
        self,
        *,
        lead_frame: str,
        convoy_id: int,
        proposal_turns: list[TeamRoomTurn],
        crosstalk_turns: list[TeamRoomTurn],
        reviewer_turn: TeamRoomTurn,
        revision_turns: list[TeamRoomTurn],
        workspace_id: int,
    ) -> AgentMessage:
        reviewer_body = reviewer_turn.step.reply.body if reviewer_turn.step.reply else ""
        return self.mailbox_svc.send_message(
            SendMessageInput(
                from_agent=TEAM_ROOM_LEAD_AGENT_ID,
                recipients=[TEAM_ROOM_SYNTHESIZER.agent_id],
                convoy_id=convoy_id,
                subject="Growth boardroom final synthesis brief",
                body=(
                    f"{lead_frame}\n\n"
                    f"{TEAM_ROOM_SYNTHESIZER.proposal_prompt}\n\n"
                    "Department proposals:\n"
                    f"{self._format_turns(proposal_turns)}\n\n"
                    "Cross-talk:\n"
                    f"{self._format_turns(crosstalk_turns)}\n\n"
                    f"Adversarial review:\n{reviewer_body}\n\n"
                    "Revised department plans:\n"
                    f"{self._format_turns(revision_turns)}"
                ),
                message_type="message",
                msg_type="work_handoff",
            ),
            workspace_id=workspace_id,
        )

    def _run_department_phase(
        self,
        team_id: int,
        subtasks: list[Subtask],
        *,
        phase: str,
        reply_builder,
        use_runtime: bool,
        runtime_lane: str | None,
        workspace_id: int,
    ) -> list[TeamRoomTurn]:
        turns: list[TeamRoomTurn] = []
        for role, subtask in zip(TEAM_ROOM_DEPARTMENT_ROLES, subtasks):
            step = self.loop_svc.run_member_step(
                team_id,
                role.agent_id,
                subtask_id=subtask.id,
                reply_body=reply_builder(role),
                use_runtime=use_runtime,
                runtime_lane=runtime_lane,
                complete=True,
                workspace_id=workspace_id,
            )
            turns.append(TeamRoomTurn(phase=phase, role=role, step=step))
        return turns

    @staticmethod
    def _subtask_description(
        goal: str,
        context: str | None,
        prompt: str,
        *,
        phase: str,
    ) -> str:
        parts = [f"Goal: {goal}", f"Phase: {phase}", prompt]
        if context:
            parts.insert(1, f"Context: {context}")
        return "\n\n".join(parts)

    @staticmethod
    def _build_lead_frame(goal: str, context: str | None) -> str:
        frame = (
            "Lead frame:\n"
            f"Goal: {goal}\n"
            "Run this like a company boardroom. Each department should bring its "
            "own expertise, build or challenge peers when useful, and keep the "
            "conversation pointed at decisions and execution."
        )
        if context:
            frame += f"\nContext: {context}"
        return frame

    @staticmethod
    def _first_subtask_for_agent(subtasks: list[Subtask], agent_id: str) -> Subtask:
        for subtask in subtasks:
            if subtask.assigned_agent_id == agent_id:
                return subtask
        raise ValueError(f"No subtask assigned to {agent_id}")

    @staticmethod
    def _format_turns(turns: list[TeamRoomTurn]) -> str:
        lines: list[str] = []
        for turn in turns:
            body = turn.step.reply.body if turn.step.reply else ""
            lines.append(f"- {turn.role.agent_name} ({turn.phase}): {body}")
        return "\n".join(lines)

    @staticmethod
    def _default_proposal_reply(role: TeamRoomRoleSpec, goal: str) -> str:
        replies = {
            "sales": (
                f"Sales proposal for {goal}: define the highest-intent buyer, sell a "
                "narrow first outcome, and convert interest through a direct audit call. "
                "Owner: sales. Validation: qualified conversations, booked calls, and "
                "objections that repeat enough to sharpen the offer."
            ),
            "marketing": (
                f"Marketing proposal for {goal}: turn the goal into one sharp promise, "
                "one proof asset, and one campaign hook. Owner: marketing. Validation: "
                "message click-through, audit requests, and proof gaps people still ask about."
            ),
            "product_frontend": (
                f"Product/frontend proposal for {goal}: make the first user path obvious, "
                "remove conversion friction, and ship the smallest page/app change that "
                "demonstrates the offer. Owner: product/frontend. Validation: CTA rate and "
                "drop-off points."
            ),
            "ops": (
                f"Ops proposal for {goal}: sequence the work, assign owners, instrument the "
                "funnel, and force a daily readout. Owner: ops. Validation: completed launch "
                "checklist, clean handoffs, and visible metrics."
            ),
        }
        return replies.get(role.key, role.proposal_prompt)

    @staticmethod
    def _default_crosstalk_reply(role: TeamRoomRoleSpec, goal: str) -> str:
        replies = {
            "sales": (
                f"Build: marketing's promise should be tested in live sales calls for {goal}. "
                "Challenge: product should not ship a polished path before we know which "
                "objection kills the deal. Sales can feed objections back daily."
            ),
            "marketing": (
                f"Build: sales gives us the buyer language for {goal}, and product gives us "
                "the proof surface. Challenge: ops needs one campaign owner so the story "
                "doesn't scatter across disconnected experiments."
            ),
            "product_frontend": (
                f"Build: marketing's proof asset can become the first conversion surface for "
                f"{goal}. Challenge: sales and ops need to keep the first CTA narrow so the "
                "page does not promise a broad platform before the workflow is proven."
            ),
            "ops": (
                f"Build: product's small shippable change and sales' objection loop make "
                f"{goal} measurable. Challenge: marketing should not launch multiple channels "
                "until one offer, one CTA, and one follow-up owner are working."
            ),
        }
        return replies.get(role.key, "Pass: no useful delta from this department.")

    @staticmethod
    def _default_reviewer_reply(goal: str) -> str:
        return (
            f"Adversarial review for {goal}: the plan can fail through false consensus. "
            "Sales wants buyer proof, marketing wants a campaign, product wants a cleaner "
            "path, and ops wants cadence, but none of that matters unless one offer and "
            "one validation loop are chosen. Highest-risk trap: launching activity before "
            "the team can name the buyer, CTA, owner, and metric in one sentence."
        )

    @staticmethod
    def _default_revision_reply(role: TeamRoomRoleSpec, goal: str) -> str:
        replies = {
            "sales": (
                f"Sales revision for {goal}: commit to one buyer segment and one audit-style "
                "CTA. Sales owns 20 targeted conversations, objection logging, and a weekly "
                "decision on whether the offer is resonating."
            ),
            "marketing": (
                f"Marketing revision for {goal}: commit to one proof-backed campaign around "
                "the audit CTA. Marketing owns the promise, proof asset, and channel test, "
                "then revises copy from sales objections."
            ),
            "product_frontend": (
                f"Product/frontend revision for {goal}: commit to one scan-friendly path from "
                "promise to proof to audit CTA. Product owns the page/app change, CTA tracking, "
                "and friction fixes found in review."
            ),
            "ops": (
                f"Ops revision for {goal}: commit to a two-week launch cadence with named "
                "owners, daily metric readout, and a stop/go review. Ops owns handoffs, QA, "
                "instrumentation, and keeping the team out of broad unfocused work."
            ),
        }
        return replies.get(role.key, role.revision_prompt)

    @staticmethod
    def _default_final_brief(goal: str) -> str:
        return (
            f"Final Team Room brief for {goal}:\n"
            "Decision: run one narrow growth boardroom bet instead of scattering into broad work.\n"
            "Bets: one buyer segment, one audit-style CTA, one proof-backed campaign, one "
            "small product/frontend conversion path, and one daily ops readout.\n"
            "Risks: false consensus, vague buyer language, overbuilt product work, and "
            "marketing activity without sales feedback.\n"
            "Objections: if the buyer will not book the audit, the offer is weak; if the "
            "page does not explain the audit, the product path is weak; if follow-up is not "
            "tracked, ops is weak.\n"
            "Next actions: Sales books 20 targeted conversations; Marketing ships the proof "
            "asset and campaign copy; Product/Frontend ships the CTA path and tracking; Ops "
            "runs the daily readout and the two-week stop/go review."
        )


def _clip_text(text: str | None, *, max_chars: int = 600) -> str:
    value = (text or "").strip()
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 14].rstrip() + "\n...[truncated]"


def team_room_turn_to_dict(turn: TeamRoomTurn) -> dict:
    return {
        "phase": turn.phase,
        "role": turn.role.key,
        "role_name": turn.role.agent_name,
        "agent_id": turn.role.agent_id,
        "subtask_id": turn.step.subtask_id,
        "action": turn.step.action,
        "status": turn.step.subtask_after.status if turn.step.subtask_after else None,
        "completed": turn.step.completed,
        "reply": dataclasses.asdict(turn.step.reply) if turn.step.reply else None,
        "runtime": team_room_runtime_metadata_for_step(turn.step),
        "step": _team_room_safe_step_dict(turn.step),
    }


def team_room_runtime_metadata_for_step(step: TeamLoopStepResult) -> dict | None:
    if step.runtime is None:
        return None
    return {
        "runtime_lane": step.runtime.runtime_lane,
        "provider": step.runtime.provider,
        "model": step.runtime.model,
        "profile_key": step.runtime.profile_key,
        "cost_usd": step.runtime.cost_usd,
        "execution_time_ms": step.runtime_execution_time_ms,
        "tool_call_count": step.runtime.tool_call_count,
        "error": step.runtime_error,
    }


def _team_room_safe_step_dict(step: TeamLoopStepResult) -> dict:
    data = result_to_dict(step)
    data["claimed"] = []
    runtime = data.get("runtime")
    if isinstance(runtime, dict):
        runtime.pop("session_id", None)
    for key in ("subtask_before", "subtask_after"):
        subtask = data.get(key)
        if isinstance(subtask, dict):
            subtask["description"] = ""
    newly_ready = data.get("newly_ready")
    if isinstance(newly_ready, list):
        for subtask in newly_ready:
            if isinstance(subtask, dict):
                subtask["description"] = ""
    return data


def _team_room_safe_subtask_dict(subtask: Subtask) -> dict:
    data = dataclasses.asdict(subtask)
    data["description"] = ""
    return data


def team_room_runtime_summary(result: TeamRoomWorkflowResult) -> dict:
    turns = [
        *result.proposal_turns,
        *result.crosstalk_turns,
        result.reviewer_turn,
        *result.revision_turns,
        result.final_turn,
    ]
    runtime_turns = [turn for turn in turns if turn.step.runtime is not None]
    metadata = [
        team_room_runtime_metadata_for_step(turn.step)
        for turn in runtime_turns
    ]
    present = [item for item in metadata if item is not None]
    costs = [
        item["cost_usd"]
        for item in present
        if isinstance(item.get("cost_usd"), (int, float))
    ]
    elapsed = [
        item["execution_time_ms"]
        for item in present
        if isinstance(item.get("execution_time_ms"), int)
    ]
    errors = [
        str(item["error"])
        for item in present
        if item.get("error")
    ]
    return {
        "enabled": bool(runtime_turns),
        "turn_count": len(runtime_turns),
        "lanes": sorted(
            {str(item["runtime_lane"]) for item in present if item.get("runtime_lane")}
        ),
        "providers": sorted(
            {str(item["provider"]) for item in present if item.get("provider")}
        ),
        "models": sorted(
            {str(item["model"]) for item in present if item.get("model")}
        ),
        "tool_call_count": sum(
            int(item.get("tool_call_count") or 0) for item in present
        ),
        "cost_usd": round(sum(costs), 6) if costs else (0.0 if runtime_turns else None),
        "execution_time_ms": sum(elapsed) if elapsed else (0 if runtime_turns else None),
        "errors": errors,
    }


def team_room_workflow_result_to_dict(result: TeamRoomWorkflowResult) -> dict:
    convoy = result.convoy.convoy
    phase_results = {
        "proposal": [team_room_turn_to_dict(turn) for turn in result.proposal_turns],
        "crosstalk": [team_room_turn_to_dict(turn) for turn in result.crosstalk_turns],
        "adversarial_review": team_room_turn_to_dict(result.reviewer_turn),
        "revision": [team_room_turn_to_dict(turn) for turn in result.revision_turns],
        "synthesis": team_room_turn_to_dict(result.final_turn),
    }
    return {
        "workflow_id": result.workflow_id,
        "goal": result.goal,
        "context_excerpt": _clip_text(result.context, max_chars=600) if result.context else None,
        "team_id": result.team.session.id,
        "convoy_id": result.convoy.convoy.id,
        "runtime": team_room_runtime_summary(result),
        "progress": {
            "completed": convoy.completed_subtasks,
            "total": convoy.total_subtasks,
            "status": convoy.status,
        },
        "team": {
            "session": dataclasses.asdict(result.team.session),
            "members": [dataclasses.asdict(member) for member in result.team.members],
        },
        "convoy": {
            "convoy": dataclasses.asdict(result.convoy.convoy),
            "subtasks": [
                _team_room_safe_subtask_dict(subtask)
                for subtask in result.convoy.subtasks
            ],
            "edges": [dataclasses.asdict(edge) for edge in result.convoy.edges],
        },
        "lead_frame_excerpt": _clip_text(result.lead_frame, max_chars=600),
        "message_counts": {
            "proposal": len(result.proposal_messages),
            "crosstalk": len(result.crosstalk_messages),
            "revision": len(result.revision_messages),
            "reviewer": 1,
            "synthesis": 1,
        },
        "phase_results": phase_results,
        "final_brief": result.final_brief,
    }
