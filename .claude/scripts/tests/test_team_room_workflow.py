"""Homie-native Team Room workflow tests."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner
from fastapi.testclient import TestClient

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for path in (str(_SCRIPTS_DIR), str(_CHAT_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

import commands  # noqa: E402
import config  # noqa: E402
import core_handlers  # noqa: E402
from cli import main  # noqa: E402
from extension_manager import ExtensionManager  # noqa: E402
from orchestration.db import OrchestrationDB  # noqa: E402
from orchestration.mailbox_service import MailboxService  # noqa: E402
from orchestration.team_room import (  # noqa: E402
    TeamRoomWorkflowService,
    team_room_workflow_result_to_dict,
)
from runtime.base import RuntimeResult  # noqa: E402


def test_team_room_runs_growth_boardroom_workflow() -> None:
    db = OrchestrationDB(":memory:")
    try:
        result = TeamRoomWorkflowService(db).run_team_room(
            goal="Get TaskChad to one million dollars",
            context="Focus on first practical growth motion.",
        )

        assert result.workflow_id == "growth_boardroom"
        assert result.convoy.convoy.status == "completed"
        assert result.team.session.team_name == "Growth Boardroom"
        assert len(result.team.members) == 7
        assert len(result.proposal_messages) == 4
        assert len(result.proposal_turns) == 4
        assert len(result.crosstalk_messages) == 4
        assert len(result.crosstalk_turns) == 4
        assert len(result.revision_messages) == 4
        assert len(result.revision_turns) == 4
        assert result.reviewer_turn.step.subtask_before.status == "ready"
        assert result.final_turn.step.subtask_before.status == "ready"
        assert result.final_turn.step.convoy_completed is True
        assert result.convoy.convoy.total_subtasks == 14
        assert result.convoy.convoy.completed_subtasks == 14
        assert "Final Team Room brief" in result.final_brief
        assert "Next actions" in result.final_brief

        messages = MailboxService(db).get_convoy_messages(result.convoy.convoy.id)
        subjects = [entry.message.subject for entry in messages]
        bodies = [entry.message.body for entry in messages]
        assert "Growth boardroom adversarial review brief" in subjects
        assert "Growth boardroom final synthesis brief" in subjects
        assert len([s for s in subjects if s and s.startswith("Cross-talk round:")]) == 4
        assert len([s for s in subjects if s and s.startswith("Revision round:")]) == 4
        assert any("Build:" in body or "Challenge:" in body for body in bodies)
    finally:
        db.close()


def test_team_room_v2_runs_facilitated_multi_round_workflow() -> None:
    db = OrchestrationDB(":memory:")
    try:
        result = TeamRoomWorkflowService(db).run_team_room(
            goal="Get TaskChad to one million dollars",
            meeting_mode="facilitated_boardroom",
        )
        payload = team_room_workflow_result_to_dict(result)

        assert result.meeting_mode == "facilitated_boardroom"
        assert result.max_rounds == 2
        assert result.convoy.convoy.status == "completed"
        assert len(result.team.members) == 8
        assert len(result.facilitator_messages) == 3
        assert len(result.facilitator_turns) == 3
        assert len(result.discussion_rounds) == 2
        assert len(result.proposal_turns) == 4
        assert len(result.crosstalk_turns) == 8
        assert len(result.revision_turns) == 4
        assert result.convoy.convoy.total_subtasks == 21
        assert result.convoy.convoy.completed_subtasks == 21

        assert payload["meeting_mode"] == "facilitated_boardroom"
        assert payload["max_rounds"] == 2
        assert payload["progress"]["completed"] == 21
        assert payload["progress"]["total"] == 21
        assert payload["message_counts"]["facilitator"] == 3
        assert payload["turn_summary"] == (
            "3 facilitator, 4 proposals, 8 cross-talk, "
            "1 adversarial critique, 4 revisions, 1 final synthesis"
        )
        assert len(payload["discussion_rounds"]) == 2
        assert payload["discussion_rounds"][1]["facilitator_turn"]["role"] == "facilitator"
        assert payload["decision_ledger"]["decisions"]
        assert payload["decision_ledger"]["owner_actions"][0]["owner"] == "Sales"

        messages = MailboxService(db).get_convoy_messages(result.convoy.convoy.id)
        subjects = [entry.message.subject for entry in messages]
        assert "Facilitator opening brief" in subjects
        assert "Facilitator round 1 brief" in subjects
        assert "Facilitator round 2 brief" in subjects
        assert "Cross-talk round 2: Sales" in subjects
    finally:
        db.close()


def test_team_room_runtime_runs_no_tools_and_sanitizes_metadata(monkeypatch) -> None:
    calls = []

    async def fake_run_with_runtime_lanes(request):
        calls.append(request)
        return RuntimeResult(
            text=f"Runtime team room turn {len(calls)}",
            runtime_lane=request.runtime_lane or "generic_runtime",
            provider="openai-codex",
            model="gpt-test",
            profile_key="primary-openai-codex",
            session_id=f"runtime-session-{len(calls)}",
            cost_usd=0.01,
            tool_call_count=0,
        )

    monkeypatch.setattr(
        "orchestration.team_loop.run_with_runtime_lanes",
        fake_run_with_runtime_lanes,
    )
    db = OrchestrationDB(":memory:")
    try:
        result = TeamRoomWorkflowService(db).run_team_room(
            goal="Get TaskChad to one million dollars",
            use_runtime=True,
            runtime_lane="generic_runtime",
        )
        payload = team_room_workflow_result_to_dict(result)

        assert len(calls) == 14
        assert all(call.task_name == "team_loop_member_turn" for call in calls)
        assert all(call.runtime_lane == "generic_runtime" for call in calls)
        assert all(call.allowed_tools == [] for call in calls)
        assert all(call.disallowed_tools == ["*"] for call in calls)
        assert all(call.max_turns == 1 for call in calls)
        assert result.final_brief == "Runtime team room turn 14"

        assert payload["runtime"]["enabled"] is True
        assert payload["runtime"]["turn_count"] == 14
        assert payload["runtime"]["lanes"] == ["generic_runtime"]
        assert payload["runtime"]["providers"] == ["openai-codex"]
        assert payload["runtime"]["models"] == ["gpt-test"]
        assert payload["runtime"]["tool_call_count"] == 0
        assert payload["runtime"]["cost_usd"] == 0.14
        first_turn = payload["phase_results"]["proposal"][0]
        assert first_turn["runtime"]["provider"] == "openai-codex"
        assert "session_id" not in first_turn["runtime"]
        assert "session_id" not in first_turn["step"]["runtime"]
        assert first_turn["step"]["claimed"] == []
        assert all(
            subtask["description"] == ""
            for subtask in payload["convoy"]["subtasks"]
        )
    finally:
        db.close()


def test_team_room_v2_runtime_keeps_tools_off_and_counts_facilitator(monkeypatch) -> None:
    calls = []

    async def fake_run_with_runtime_lanes(request):
        calls.append(request)
        return RuntimeResult(
            text=f"Runtime v2 team room turn {len(calls)}",
            runtime_lane=request.runtime_lane or "generic_runtime",
            provider="openai-codex",
            model="gpt-test",
            profile_key="primary-openai-codex",
            session_id=f"runtime-session-{len(calls)}",
            cost_usd=0.01,
            tool_call_count=0,
        )

    monkeypatch.setattr(
        "orchestration.team_loop.run_with_runtime_lanes",
        fake_run_with_runtime_lanes,
    )
    db = OrchestrationDB(":memory:")
    try:
        result = TeamRoomWorkflowService(db).run_team_room(
            goal="Get TaskChad to one million dollars",
            max_rounds=2,
            use_runtime=True,
            runtime_lane="generic_runtime",
        )
        payload = team_room_workflow_result_to_dict(result)

        assert result.meeting_mode == "facilitated_boardroom"
        assert len(calls) == 21
        assert all(call.allowed_tools == [] for call in calls)
        assert all(call.disallowed_tools == ["*"] for call in calls)
        assert payload["runtime"]["turn_count"] == 21
        assert payload["runtime"]["tool_call_count"] == 0
        assert payload["phase_results"]["facilitator"][0]["runtime"]["provider"] == "openai-codex"
        assert "session_id" not in payload["phase_results"]["facilitator"][0]["runtime"]
        assert result.final_brief == "Runtime v2 team room turn 21"
    finally:
        db.close()


def test_team_room_api_creates_completed_workflow(tmp_path) -> None:
    db_path = tmp_path / "test_team_room_api.db"
    with patch("config.ORCHESTRATION_DB_PATH", db_path):
        import importlib
        import orchestration.api as api_mod

        importlib.reload(api_mod)
        db, cs, ms, reg, team_svc = api_mod._get_services()
        api_mod._db = db
        api_mod._convoy_svc = cs
        api_mod._mailbox_svc = ms
        api_mod._executor_registry = reg
        api_mod._team_svc = team_svc
        try:
            client = TestClient(api_mod.app)
            response = client.post(
                "/api/team/room/run",
                json={"goal": "Get TaskChad to one million dollars"},
            )

            assert response.status_code == 200
            body = response.json()
            assert body["workflow_id"] == "growth_boardroom"
            assert body["progress"]["completed"] == 14
            assert body["progress"]["total"] == 14
            assert len(body["phase_results"]["proposal"]) == 4
            assert len(body["phase_results"]["crosstalk"]) == 4
            assert len(body["phase_results"]["revision"]) == 4
            assert body["phase_results"]["adversarial_review"]["completed"] is True
            assert body["phase_results"]["synthesis"]["completed"] is True
            assert "Final Team Room brief" in body["final_brief"]
            assert body["convoy"]["convoy"]["status"] == "completed"
        finally:
            db.close()


def test_team_room_api_runs_v2_facilitated_workflow(tmp_path) -> None:
    db_path = tmp_path / "test_team_room_api_v2.db"
    with patch("config.ORCHESTRATION_DB_PATH", db_path):
        import importlib
        import orchestration.api as api_mod

        importlib.reload(api_mod)
        db, cs, ms, reg, team_svc = api_mod._get_services()
        api_mod._db = db
        api_mod._convoy_svc = cs
        api_mod._mailbox_svc = ms
        api_mod._executor_registry = reg
        api_mod._team_svc = team_svc
        try:
            client = TestClient(api_mod.app)
            response = client.post(
                "/api/team/room/run",
                json={
                    "goal": "Get TaskChad to one million dollars",
                    "v2": True,
                },
            )

            assert response.status_code == 200
            body = response.json()
            assert body["meeting_mode"] == "facilitated_boardroom"
            assert body["max_rounds"] == 2
            assert body["progress"]["completed"] == 21
            assert body["progress"]["total"] == 21
            assert len(body["phase_results"]["facilitator"]) == 3
            assert len(body["discussion_rounds"]) == 2
            assert body["decision_ledger"]["strongest_objection"]
        finally:
            db.close()


def test_teamroom_chat_command_is_registered_and_runs(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(config, "ORCHESTRATION_DB_PATH", tmp_path / "teamroom_chat.db")
    command_rows = {name: (desc, typ, role) for name, desc, typ, role in commands.COMMANDS}

    desc, typ, role = command_rows["teamroom"]
    assert typ == "router"
    assert role == "admin"
    assert "Growth Boardroom" in desc
    assert core_handlers.CORE_HANDLERS["teamroom"] is core_handlers.handle_teamroom

    manager = ExtensionManager()
    manager.register_core_commands(commands.COMMANDS, commands.CATEGORIES, core_handlers.CORE_HANDLERS)
    help_text = manager.get_help_text(user_role="admin")
    assert "/teamroom" in help_text

    reply = asyncio.run(
        core_handlers.handle_teamroom(
            adapter=None,
            incoming=None,
            args="How do we get TaskChad to one million dollars?",
        )
    )

    assert "*Team Room Workflow*" in reply
    assert "Workflow: `growth_boardroom`" in reply
    assert "Progress: `14/14` subtasks" in reply
    assert "4 proposals, 4 cross-talk, 1 adversarial critique, 4 revisions, 1 final synthesis" in reply
    assert "Runtime turns: `off`" in reply
    assert "Final Team Room brief" in reply


def test_teamroom_chat_v2_command_runs_facilitated_meeting(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(config, "ORCHESTRATION_DB_PATH", tmp_path / "teamroom_chat_v2.db")

    reply = asyncio.run(
        core_handlers.handle_teamroom(
            adapter=None,
            incoming=None,
            args="--v2 How do we get TaskChad to one million dollars?",
        )
    )

    assert "Mode: `facilitated_boardroom`" in reply
    assert "Rounds: `2`" in reply
    assert "Progress: `21/21` subtasks" in reply
    assert (
        "3 facilitator, 4 proposals, 8 cross-talk, 1 adversarial critique, "
        "4 revisions, 1 final synthesis"
    ) in reply
    assert "Final Team Room brief" in reply


def test_teamroom_chat_runtime_command_uses_lane(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(config, "ORCHESTRATION_DB_PATH", tmp_path / "teamroom_runtime.db")
    calls = []

    async def fake_run_with_runtime_lanes(request):
        calls.append(request)
        return RuntimeResult(
            text=f"Runtime command turn {len(calls)}",
            runtime_lane=request.runtime_lane or "generic_runtime",
            provider="openai-codex",
            model="gpt-test",
            cost_usd=0.0,
            tool_call_count=0,
        )

    monkeypatch.setattr(
        "orchestration.team_loop.run_with_runtime_lanes",
        fake_run_with_runtime_lanes,
    )

    reply = asyncio.run(
        core_handlers.handle_teamroom(
            adapter=None,
            incoming=None,
            args="--runtime --lane generic_runtime How do we get TaskChad to one million dollars?",
        )
    )

    assert len(calls) == 14
    assert "Runtime turns: `on`" in reply
    assert "Runtime lane: `generic_runtime`" in reply
    assert "Runtime metadata: `14` turns" in reply
    assert "providers `openai-codex`" in reply
    assert "models `gpt-test`" in reply
    assert "tools `0`" in reply
    assert "Runtime command turn 14" in reply


def test_team_room_cli_run_json(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(config, "ORCHESTRATION_DB_PATH", tmp_path / "teamroom_cli.db")

    result = CliRunner().invoke(
        main,
        [
            "team",
            "room",
            "run",
            "--goal",
            "Get TaskChad to one million dollars",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["workflow_id"] == "growth_boardroom"
    assert body["progress"]["completed"] == 14
    assert body["progress"]["total"] == 14
    assert "Final Team Room brief" in body["final_brief"]


def test_team_room_cli_v2_run_json(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(config, "ORCHESTRATION_DB_PATH", tmp_path / "teamroom_cli_v2.db")

    result = CliRunner().invoke(
        main,
        [
            "team",
            "room",
            "run",
            "--v2",
            "--goal",
            "Get TaskChad to one million dollars",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["meeting_mode"] == "facilitated_boardroom"
    assert body["max_rounds"] == 2
    assert body["progress"]["completed"] == 21
    assert body["progress"]["total"] == 21
    assert body["turn_summary"].startswith("3 facilitator")
