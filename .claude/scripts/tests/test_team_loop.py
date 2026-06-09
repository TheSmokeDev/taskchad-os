"""Team loop proof tests.

These prove a team member can consume convoy-scoped mailbox state, write a
handoff reply, and advance a bound subtask without dashboard-local worker
logic.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))

from orchestration.convoy_service import ConvoyService
from orchestration.db import OrchestrationDB
from orchestration.mailbox_service import MailboxService
from orchestration.models import (
    AddTeamMemberInput,
    CreateConvoyInput,
    CreateSubtaskInput,
    CreateTeamSessionInput,
    SendMessageInput,
)
from orchestration.team_loop import TeamLoopService, TeamTickService
from orchestration.team_service import TeamService
from runtime.base import RuntimeResult


@pytest.fixture
def services():
    db = OrchestrationDB(":memory:")
    yield db, ConvoyService(db), MailboxService(db), TeamService(db), TeamLoopService(db)
    db.close()


def _seed_team_loop(services):
    db, convoy_svc, mailbox_svc, team_svc, _loop_svc = services
    convoy = convoy_svc.create_convoy(
        CreateConvoyInput(
            title="Campaign proof",
            created_by="dashboard",
            subtasks=[
                CreateSubtaskInput(
                    title="Marketing positioning",
                    assigned_agent_id="marketing-worker",
                    assigned_agent_name="Marketing Worker",
                ),
            ],
        )
    )
    subtask_id = convoy.subtasks[0].id
    team = team_svc.create_team_session(
        CreateTeamSessionInput(
            team_name="Revenue Team",
            lead_agent_id="sales-lead",
            lead_agent_name="Sales Lead",
            convoy_id=convoy.convoy.id,
        )
    )
    team_svc.add_member(
        team.session.id,
        AddTeamMemberInput(
            agent_id="marketing-worker",
            agent_name="Marketing Worker",
            role="worker",
            subtask_id=subtask_id,
        ),
    )
    mailbox_svc.send_message(
        SendMessageInput(
            from_agent="sales-lead",
            recipients=["marketing-worker"],
            body="Need positioning for the outbound campaign.",
            convoy_id=convoy.convoy.id,
            subject="Sales asks marketing",
            msg_type="team_message",
        )
    )
    return db, convoy.convoy.id, team.session.id, subtask_id


def test_team_loop_claims_mailbox_writes_handoff_and_advances_subtask(services):
    db, convoy_id, team_id, subtask_id = _seed_team_loop(services)
    _db, _convoy_svc, mailbox_svc, _team_svc, loop_svc = services

    result = loop_svc.run_member_step(
        team_id,
        "marketing-worker",
        reply_body="Positioning is drafted; sales can start the first outbound pass.",
    )

    assert len(result.claimed) == 1
    assert result.reply is not None
    assert result.reply.from_agent == "marketing-worker"
    assert result.reply.msg_type == "work_handoff"
    assert result.subtask_before.status == "ready"
    assert result.subtask_after.status == "running"
    assert result.action == "running"

    assert mailbox_svc.get_inbox("marketing-worker", convoy_id=convoy_id) == []
    convoy_messages = mailbox_svc.get_convoy_messages(convoy_id)
    bodies = [entry.message.body for entry in convoy_messages]
    assert "Need positioning for the outbound campaign." in bodies
    assert any("Positioning is drafted" in body for body in bodies)

    row = db.conn.execute("SELECT status FROM subtasks WHERE id = ?", (subtask_id,)).fetchone()
    assert row["status"] == "running"


def test_team_loop_can_complete_running_subtask(services):
    _db, _convoy_svc, _mailbox_svc, _team_svc, loop_svc = services
    _db, _convoy_id, team_id, subtask_id = _seed_team_loop(services)

    loop_svc.run_member_step(team_id, "marketing-worker")
    result = loop_svc.run_member_step(
        team_id,
        "marketing-worker",
        complete=True,
        reply_body="Marketing task complete.",
    )

    assert result.completed is True
    assert result.subtask_after.status == "completed"
    assert result.convoy_completed is True


def test_team_loop_runtime_reply_uses_lane_router(monkeypatch, services):
    _db, convoy_id, team_id, _subtask_id = _seed_team_loop(services)
    _db, _convoy_svc, mailbox_svc, _team_svc, loop_svc = services
    calls = []

    async def fake_run_with_runtime_lanes(request):
        calls.append(request)
        return RuntimeResult(
            text="Runtime generated marketing handoff for sales.",
            runtime_lane=request.runtime_lane or "generic_runtime",
            provider="codex",
            model="test-model",
            session_id="runtime-session",
            tool_call_count=0,
        )

    monkeypatch.setattr(
        "orchestration.team_loop.run_with_runtime_lanes",
        fake_run_with_runtime_lanes,
    )

    result = loop_svc.run_member_step(
        team_id,
        "marketing-worker",
        use_runtime=True,
        runtime_lane="generic_runtime",
    )

    assert len(calls) == 1
    assert calls[0].task_name == "team_loop_member_turn"
    assert calls[0].runtime_lane == "generic_runtime"
    assert calls[0].allowed_tools == []
    assert calls[0].disallowed_tools == ["*"]
    assert result.runtime is not None
    assert result.runtime.provider == "codex"
    assert result.reply is not None
    assert result.reply.body == "Runtime generated marketing handoff for sales."
    bodies = [entry.message.body for entry in mailbox_svc.get_convoy_messages(convoy_id)]
    assert "Runtime generated marketing handoff for sales." in bodies


def test_team_tick_claims_pending_mail_and_runs_member_step(services):
    _db, convoy_id, team_id, subtask_id = _seed_team_loop(services)
    _db, _convoy_svc, mailbox_svc, _team_svc, _loop_svc = services
    tick_svc = TeamTickService(_db)

    result = tick_svc.run_team_tick(team_id)

    assert result.selected_action == "claim_respond"
    assert result.agent_id == "marketing-worker"
    assert result.subtask_id == subtask_id
    assert result.step is not None
    assert result.step.claimed
    assert result.step.subtask_after.status == "running"
    assert mailbox_svc.get_inbox("marketing-worker", convoy_id=convoy_id) == []


def test_team_tick_advances_ready_subtask_without_pending_mail(services):
    _db, _convoy_svc, mailbox_svc, _team_svc, _loop_svc = services
    _db, convoy_id, team_id, _subtask_id = _seed_team_loop(services)
    claimed = mailbox_svc.claim_deliveries("marketing-worker", convoy_id=convoy_id)
    for entry in claimed:
        for delivery in entry.deliveries:
            if delivery.recipient_agent == "marketing-worker":
                mailbox_svc.ack_delivery(
                    delivery.id,
                    "marketing-worker",
                    delivery.claim_token,
                )

    result = TeamTickService(_db).run_team_tick(team_id)

    assert result.selected_action == "advance_ready"
    assert result.step is not None
    assert result.step.claimed == []
    assert result.step.subtask_before.status == "ready"
    assert result.step.subtask_after.status == "running"


def test_team_tick_waits_or_completes_running_subtask_by_policy(services):
    _db, _convoy_svc, _mailbox_svc, _team_svc, loop_svc = services
    _db, _convoy_id, team_id, subtask_id = _seed_team_loop(services)
    loop_svc.run_member_step(team_id, "marketing-worker")

    wait_result = TeamTickService(_db).run_team_tick(team_id)
    assert wait_result.selected_action == "wait"
    assert wait_result.waited is True
    assert wait_result.subtask_id == subtask_id
    assert "completion policy is disabled" in wait_result.reason

    complete_result = TeamTickService(_db).run_team_tick(
        team_id,
        complete_running=True,
    )
    assert complete_result.selected_action == "complete_running"
    assert complete_result.step is not None
    assert complete_result.step.completed is True
    assert complete_result.step.subtask_after.status == "completed"


def test_team_tick_waits_when_no_work_is_available(services):
    _db, _convoy_svc, _mailbox_svc, _team_svc, loop_svc = services
    _db, _convoy_id, team_id, _subtask_id = _seed_team_loop(services)
    loop_svc.run_member_step(team_id, "marketing-worker")
    loop_svc.run_member_step(team_id, "marketing-worker", complete=True)

    result = TeamTickService(_db).run_team_tick(team_id, complete_running=True)

    assert result.selected_action == "wait"
    assert result.waited is True
    assert result.step is None
    assert "no pending mail" in result.reason


def test_team_tick_runtime_failure_is_bounded(monkeypatch, services):
    _db, _convoy_id, team_id, subtask_id = _seed_team_loop(services)

    async def fake_run_with_runtime_lanes(_request):
        raise RuntimeError("runtime unavailable")

    monkeypatch.setattr(
        "orchestration.team_loop.run_with_runtime_lanes",
        fake_run_with_runtime_lanes,
    )

    result = TeamTickService(_db).run_team_tick(team_id, use_runtime=True)

    assert result.selected_action == "claim_respond"
    assert result.subtask_id == subtask_id
    assert result.step is None
    assert result.error == "RuntimeError: runtime unavailable"


def test_teamtick_chat_command_is_registered_and_parses_options():
    from commands import COMMANDS
    from core_handlers import CORE_HANDLERS, _format_team_tick_reply, _parse_teamtick_args

    assert any(name == "teamtick" and kind == "router" for name, _desc, kind, _role in COMMANDS)
    assert "teamtick" in CORE_HANDLERS

    parsed = _parse_teamtick_args("9 --agent sales-worker --runtime --lane generic_runtime --complete")
    assert not isinstance(parsed, str)
    team_id, opts = parsed
    assert team_id == 9
    assert opts == {
        "agent_id": "sales-worker",
        "use_runtime": True,
        "runtime_lane": "generic_runtime",
        "complete_running": True,
        "execute_running": False,
        "executor_command": "git_status",
        "executor_cwd": None,
        "complete_on_executor_success": False,
        "allow_live_agent_run": False,
    }

    parsed = _parse_teamtick_args(
        "9 --agent sales-worker --execute-running --command git_status --complete-on-success"
    )
    assert not isinstance(parsed, str)
    team_id, opts = parsed
    assert team_id == 9
    assert opts["agent_id"] == "sales-worker"
    assert opts["execute_running"] is True
    assert opts["executor_command"] == "git_status"
    assert opts["complete_on_executor_success"] is True

    parsed = _parse_teamtick_args("9 --allow-live-agent-run")
    assert not isinstance(parsed, str)
    _team_id, opts = parsed
    assert opts["allow_live_agent_run"] is True

    reply = _format_team_tick_reply(
        SimpleNamespace(
            team_id=9,
            selected_action="claim_respond",
            agent_id="sales_worker",
            subtask_id=12,
            reason="1 pending convoy mailbox item(s)",
            error=None,
            waited=False,
            step=SimpleNamespace(
                action="mailbox_reply",
                claimed=[object()],
                subtask_after=SimpleNamespace(status="running"),
                runtime=None,
            ),
        )
    )
    assert "Action: `claim_respond`" in reply
    assert "Agent: `sales_worker`" in reply
    assert "Step: `mailbox_reply`; claimed 1; status `running`" in reply

    executor_reply = _format_team_tick_reply(
        SimpleNamespace(
            team_id=9,
            selected_action="executor_step",
            agent_id="sales_worker",
            subtask_id=12,
            reason="subtask #12 is running and executor policy is enabled",
            error=None,
            waited=False,
            step=None,
            executor=SimpleNamespace(
                command_key="git_status",
                exit_code=0,
                success=True,
            ),
        )
    )
    assert "Action: `executor_step`" in executor_reply
    assert "Executor: `git_status`; exit `0`; success `True`" in executor_reply


def test_teamtick_chat_command_refuses_without_live_opt_in(monkeypatch):
    import asyncio

    from core_handlers import handle_teamtick

    monkeypatch.delenv("HOMIE_ALLOW_LIVE_AGENT_RUN", raising=False)

    reply = asyncio.run(
        handle_teamtick(adapter=None, incoming=None, args="9 --agent sales-worker")
    )

    assert "Live agent/factory action refused" in reply
    assert "chat /teamtick" in reply


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "test_team_loop_api.db"
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
        yield TestClient(api_mod.app)
        db.close()


def test_team_loop_step_api(client):
    convoy = client.post(
        "/api/convoy",
        json={
            "title": "API team loop",
            "created_by": "dashboard",
            "subtasks": [{"title": "Marketing positioning"}],
        },
    ).json()
    convoy_id = convoy["convoy"]["id"]
    subtask_id = convoy["subtasks"][0]["id"]
    team = client.post(
        "/api/team",
        json={
            "team_name": "Revenue Team",
            "lead_agent_id": "sales-lead",
            "convoy_id": convoy_id,
        },
    ).json()
    team_id = team["session"]["id"]
    client.post(
        f"/api/team/{team_id}/members",
        json={
            "agent_id": "marketing-worker",
            "agent_name": "Marketing Worker",
            "role": "worker",
            "subtask_id": subtask_id,
        },
    )
    client.post(
        "/api/mailbox/send",
        json={
            "from_agent": "sales-lead",
            "recipients": ["marketing-worker"],
            "body": "Need positioning for the outbound campaign.",
            "convoy_id": convoy_id,
        },
    )

    response = client.post(
        f"/api/team/{team_id}/loop-step",
        json={
            "agent_id": "marketing-worker",
            "reply_body": "Positioning is ready for sales.",
            "allow_live_agent_run": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["claimed_count"] == 1
    assert body["action"] == "running"
    assert body["reply"]["from_agent"] == "marketing-worker"
    assert body["subtask_before"]["status"] == "ready"
    assert body["subtask_after"]["status"] == "running"


def test_team_tick_api(client):
    convoy = client.post(
        "/api/convoy",
        json={
            "title": "API team tick",
            "created_by": "dashboard",
            "subtasks": [{"title": "Marketing positioning"}],
        },
    ).json()
    convoy_id = convoy["convoy"]["id"]
    subtask_id = convoy["subtasks"][0]["id"]
    team = client.post(
        "/api/team",
        json={
            "team_name": "Revenue Team",
            "lead_agent_id": "sales-lead",
            "convoy_id": convoy_id,
        },
    ).json()
    team_id = team["session"]["id"]
    client.post(
        f"/api/team/{team_id}/members",
        json={
            "agent_id": "marketing-worker",
            "agent_name": "Marketing Worker",
            "role": "worker",
            "subtask_id": subtask_id,
        },
    )
    client.post(
        "/api/mailbox/send",
        json={
            "from_agent": "sales-lead",
            "recipients": ["marketing-worker"],
            "body": "Need positioning for the outbound campaign.",
            "convoy_id": convoy_id,
        },
    )

    response = client.post(
        f"/api/team/{team_id}/tick",
        json={"complete_running": False, "allow_live_agent_run": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["selected_action"] == "claim_respond"
    assert body["agent_id"] == "marketing-worker"
    assert body["step"]["claimed_count"] == 1
    assert body["step"]["action"] == "running"
