"""AI Operating Room service over Team Room and Team Tick.

This module is a thin product wrapper. Team Room still owns meeting behavior;
Team Tick still owns continuation/execution selection. The Operating Room
service only composes those results into one public-safe proof packet.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from orchestration.contract import DEFAULT_WORKSPACE_ID
from orchestration.db import OrchestrationDB
from orchestration.observability import orchestration_span, update_observation
from orchestration.team_loop import TeamTickService, tick_result_to_dict
from orchestration.team_room import TeamRoomWorkflowService, team_room_workflow_result_to_dict

OPERATING_ROOM_PRODUCT_SURFACE = "homie_operating_room"

_FORBIDDEN_PROOF_KEYS = {
    "authorization",
    "claim_token",
    "credentials",
    "env",
    "hooks",
    "password",
    "prompt",
    "secret",
    "session_id",
    "system_prompt",
    "token",
}


@dataclass
class OperatingRoomRunResult:
    run_id: str
    created_at: str
    team_room: dict[str, Any]
    tick: dict[str, Any] | None
    proof_packet: dict[str, Any]


class OperatingRoomService:
    """Compose Team Room + optional Team Tick into an operator proof packet."""

    def __init__(self, db: OrchestrationDB):
        self.db = db

    def run_operating_room(
        self,
        *,
        goal: str,
        workflow_id: str = "growth_boardroom",
        context: str | None = None,
        use_runtime: bool = False,
        runtime_lane: str | None = None,
        max_rounds: int | None = None,
        meeting_mode: str | None = None,
        run_tick: bool = True,
        tick_agent_id: str | None = None,
        tick_complete_running: bool = False,
        tick_execute_running: bool = False,
        tick_executor_command: str = "git_status",
        tick_executor_cwd: str | None = None,
        tick_complete_on_executor_success: bool = False,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> OperatingRoomRunResult:
        normalized_goal = (goal or "").strip()
        if not normalized_goal:
            raise ValueError("goal is required")

        run_id = f"opr-{uuid.uuid4().hex[:12]}"
        created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        if created_at.endswith("+00:00"):
            created_at = created_at[:-6] + "Z"

        with orchestration_span(
            "operating_room.run",
            metadata={
                "run_id": run_id,
                "workflow_id": workflow_id,
                "meeting_mode": meeting_mode or "facilitated_boardroom",
                "use_runtime": use_runtime,
                "runtime_lane": runtime_lane,
                "run_tick": run_tick,
            },
            trace_metadata={"feature_phase": 13, "workflow_id": workflow_id},
            expected_exceptions=(ValueError,),
        ):
            team_room_result = TeamRoomWorkflowService(self.db).run_team_room(
                goal=normalized_goal,
                workflow_id=workflow_id,
                context=context,
                use_runtime=use_runtime,
                runtime_lane=runtime_lane,
                max_rounds=max_rounds or 2,
                meeting_mode=meeting_mode or "facilitated_boardroom",
                workspace_id=workspace_id,
            )
            team_room_payload = team_room_workflow_result_to_dict(team_room_result)

            tick_payload: dict[str, Any] | None = None
            if run_tick:
                tick = TeamTickService(self.db).run_team_tick(
                    team_room_payload["team_id"],
                    agent_id=tick_agent_id,
                    use_runtime=False,
                    runtime_lane=None,
                    complete_running=tick_complete_running,
                    execute_running=tick_execute_running,
                    executor_command=tick_executor_command,
                    executor_cwd=tick_executor_cwd,
                    complete_on_executor_success=tick_complete_on_executor_success,
                    workspace_id=workspace_id,
                )
                tick_payload = tick_result_to_dict(tick)

            proof_packet = build_operating_room_proof_packet(
                run_id=run_id,
                created_at=created_at,
                team_room=team_room_payload,
                tick=tick_payload,
            )
            self._persist_proof_summary(
                team_id=team_room_payload["team_id"],
                proof_packet=proof_packet,
                workspace_id=workspace_id,
            )

            update_observation(
                metadata={
                    "run_id": run_id,
                    "team_id": proof_packet["team_id"],
                    "convoy_id": proof_packet["convoy_id"],
                    "tick_action": proof_packet["tick_summary"]["selected_action"]
                    if proof_packet.get("tick_summary")
                    else None,
                    "sanitized": proof_packet["sanitized"],
                },
                output={"final_brief_chars": len(proof_packet.get("final_brief") or "")},
            )
            return OperatingRoomRunResult(
                run_id=run_id,
                created_at=created_at,
                team_room=team_room_payload,
                tick=tick_payload,
                proof_packet=proof_packet,
            )

    def _persist_proof_summary(
        self,
        *,
        team_id: int,
        proof_packet: dict[str, Any],
        workspace_id: int,
    ) -> None:
        row = self.db.conn.execute(
            "SELECT metadata FROM team_sessions WHERE id = ? AND workspace_id = ?",
            (team_id, workspace_id),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Team session {team_id} disappeared before proof persist")
        metadata = _json_metadata(row["metadata"])
        metadata.update(
            {
                "operating_room_run_id": proof_packet["run_id"],
                "operating_room_updated_at": proof_packet["created_at"],
                "operating_room_proof": {
                    "run_id": proof_packet["run_id"],
                    "team_id": proof_packet["team_id"],
                    "convoy_id": proof_packet["convoy_id"],
                    "workflow_id": proof_packet["workflow_id"],
                    "meeting_mode": proof_packet["meeting_mode"],
                    "progress": proof_packet["progress"],
                    "owner_actions": proof_packet["owner_actions"],
                    "tick_summary": proof_packet["tick_summary"],
                    "sanitized": True,
                },
            }
        )
        with self.db.conn:
            self.db.conn.execute(
                """UPDATE team_sessions
                   SET metadata = ?, updated_at = ?
                   WHERE id = ? AND workspace_id = ?""",
                (json.dumps(metadata, sort_keys=True), int(time.time()), team_id, workspace_id),
            )


def operating_room_result_to_dict(result: OperatingRoomRunResult) -> dict[str, Any]:
    return {
        "run_id": result.run_id,
        "created_at": result.created_at,
        "team_room": result.team_room,
        "tick": result.tick,
        "proof_packet": result.proof_packet,
    }


def build_operating_room_proof_packet(
    *,
    run_id: str,
    created_at: str,
    team_room: dict[str, Any],
    tick: dict[str, Any] | None,
) -> dict[str, Any]:
    decision_ledger = _scrub(team_room.get("decision_ledger") or {})
    synthesis = _scrub(team_room.get("synthesis") or {})
    runtime = _scrub(team_room.get("runtime") or {})
    proof = {
        "run_id": run_id,
        "created_at": created_at,
        "product_surface": OPERATING_ROOM_PRODUCT_SURFACE,
        "sanitized": True,
        "goal": team_room.get("goal"),
        "workflow_id": team_room.get("workflow_id"),
        "meeting_mode": team_room.get("meeting_mode"),
        "team_id": team_room.get("team_id"),
        "convoy_id": team_room.get("convoy_id"),
        "progress": _scrub(team_room.get("progress") or {}),
        "runtime": runtime,
        "vote_board": _scrub(team_room.get("vote_board") or []),
        "interrupts": _scrub(team_room.get("interrupts") or []),
        "owner_actions": _scrub(decision_ledger.get("owner_actions") or []),
        "decisions": _scrub(decision_ledger.get("decisions") or []),
        "open_questions": _scrub(decision_ledger.get("open_questions") or []),
        "strongest_objection": decision_ledger.get("strongest_objection"),
        "next_meeting_trigger": decision_ledger.get("next_meeting_trigger"),
        "synthesis": synthesis,
        "tick_summary": _tick_summary(tick),
        "final_brief": team_room.get("final_brief"),
    }
    return _scrub(proof)


def _tick_summary(tick: dict[str, Any] | None) -> dict[str, Any] | None:
    if not tick:
        return None
    step = tick.get("step") or {}
    executor = tick.get("executor") or {}
    step_after = step.get("subtask_after") if isinstance(step, dict) else None
    if not isinstance(step_after, dict):
        step_after = {}
    return _scrub(
        {
            "selected_action": tick.get("selected_action"),
            "reason": tick.get("reason"),
            "agent_id": tick.get("agent_id"),
            "convoy_id": tick.get("convoy_id"),
            "subtask_id": tick.get("subtask_id"),
            "waited": tick.get("waited"),
            "error": tick.get("error"),
            "step_action": step.get("action") if isinstance(step, dict) else None,
            "step_claimed_count": step.get("claimed_count") if isinstance(step, dict) else None,
            "step_status": step_after.get("status"),
            "executor_command": executor.get("command_key")
            if isinstance(executor, dict)
            else None,
            "executor_success": executor.get("success") if isinstance(executor, dict) else None,
            "executor_exit_code": executor.get("exit_code") if isinstance(executor, dict) else None,
            "executor_completed": executor.get("completed") if isinstance(executor, dict) else None,
        }
    )


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if normalized in _FORBIDDEN_PROOF_KEYS:
                continue
            if any(marker in normalized for marker in ("token", "secret", "password")):
                continue
            clean[str(key)] = _scrub(item)
        return clean
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def _json_metadata(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
