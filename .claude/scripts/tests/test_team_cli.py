"""CLI integration tests for the `thehomie team` command group."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

_CHAT_DIR = Path(__file__).resolve().parent.parent.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))

from cli import main  # noqa: E402


@pytest.fixture
def runner(tmp_path):
    db_path = tmp_path / "test_team_cli.db"
    with patch("config.ORCHESTRATION_DB_PATH", db_path):
        yield CliRunner()


def _create_team(runner, name="team-alpha", lead="lead-1", members=None):
    """Helper: create a team directly via the service (bypass CLI for setup)."""
    from config import ORCHESTRATION_DB_PATH
    from orchestration.db import OrchestrationDB
    from orchestration.models import AddTeamMemberInput, CreateTeamSessionInput
    from orchestration.team_service import TeamService

    db = OrchestrationDB(ORCHESTRATION_DB_PATH)
    ts = TeamService(db)
    result = ts.create_team_session(CreateTeamSessionInput(
        team_name=name, lead_agent_id=lead, lead_agent_name=lead,
        backend_type="local",
    ))
    for m in (members or []):
        ts.add_member(result.session.id, AddTeamMemberInput(
            agent_id=m, agent_name=m, role="worker",
        ))
    db.close()
    return result.session.id


# ── list ────────────────────────────────────────────────────────────────────


def test_team_list_empty(runner):
    r = runner.invoke(main, ["team", "list"])
    assert r.exit_code == 0
    assert "No team sessions" in r.output


def test_team_list_table(runner):
    _create_team(runner, name="alpha", lead="lead-a")
    _create_team(runner, name="beta", lead="lead-b")
    r = runner.invoke(main, ["team", "list"])
    assert r.exit_code == 0
    assert "alpha" in r.output
    assert "beta" in r.output
    assert "active" in r.output


def test_team_list_filter_status(runner):
    tid = _create_team(runner, name="gamma")
    # Close it, then filter
    runner.invoke(main, ["team", "close", str(tid)])
    r_active = runner.invoke(main, ["team", "list", "--status", "active"])
    assert r_active.exit_code == 0
    assert "gamma" not in r_active.output
    r_closed = runner.invoke(main, ["team", "list", "--status", "closed"])
    assert r_closed.exit_code == 0
    assert "gamma" in r_closed.output


def test_team_list_json(runner):
    _create_team(runner, name="json-team", lead="lead-j")
    r = runner.invoke(main, ["team", "list", "--json"])
    assert r.exit_code == 0
    data = json.loads(r.output)
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["team_name"] == "json-team"


# ── status ──────────────────────────────────────────────────────────────────


def test_team_status_shows_members_and_backend(runner):
    tid = _create_team(runner, name="status-team", lead="lead-s",
                       members=["w1", "w2"])
    r = runner.invoke(main, ["team", "status", str(tid)])
    assert r.exit_code == 0
    assert "status-team" in r.output
    assert "lead-s" in r.output
    assert "Backend: local" in r.output
    assert "w1" in r.output
    assert "w2" in r.output
    assert "Mailbox backlog" in r.output


def test_team_status_not_found(runner):
    r = runner.invoke(main, ["team", "status", "9999"])
    assert r.exit_code == 1
    assert "not found" in r.output.lower()


def test_team_status_json(runner):
    tid = _create_team(runner, name="sj", lead="l", members=["w1"])
    r = runner.invoke(main, ["team", "status", str(tid), "--json"])
    assert r.exit_code == 0
    data = json.loads(r.output)
    assert data["session"]["team_name"] == "sj"
    assert len(data["members"]) == 2  # lead + w1


# ── members ─────────────────────────────────────────────────────────────────


def test_team_members_list(runner):
    tid = _create_team(runner, name="m-team", lead="boss", members=["x", "y"])
    r = runner.invoke(main, ["team", "members", str(tid)])
    assert r.exit_code == 0
    assert "boss" in r.output
    assert "x" in r.output
    assert "y" in r.output


def test_team_members_not_found(runner):
    r = runner.invoke(main, ["team", "members", "9999"])
    assert r.exit_code == 1


# ── shutdown ────────────────────────────────────────────────────────────────


def test_team_shutdown_graceful(runner):
    tid = _create_team(runner, name="sd", lead="leader",
                       members=["worker-a", "worker-b"])
    r = runner.invoke(main, ["team", "shutdown", str(tid)])
    assert r.exit_code == 0
    assert "shutdown_requested" in r.output
    assert "Sent 2 shutdown_request" in r.output
    # Verify the status transitioned
    r_status = runner.invoke(main, ["team", "status", str(tid)])
    assert "shutdown_requested" in r_status.output
    # Verify mailbox received shutdown_request messages
    r_inbox = runner.invoke(
        main, ["mailbox", "inbox", "worker-a", "--json"])
    inbox = json.loads(r_inbox.output)
    assert any(
        mwd["message"]["msg_type"] == "shutdown_request" for mwd in inbox
    )


def test_team_shutdown_force_confirmed(runner):
    tid = _create_team(runner, name="sf", lead="l", members=["w1"])
    r = runner.invoke(main, ["team", "shutdown", str(tid), "--force"], input="y\n")
    assert r.exit_code == 0
    assert "closed" in r.output.lower()
    r_status = runner.invoke(main, ["team", "status", str(tid)])
    assert "Status: closed" in r_status.output


def test_team_shutdown_force_aborted(runner):
    tid = _create_team(runner, name="sfa", lead="l")
    r = runner.invoke(main, ["team", "shutdown", str(tid), "--force"], input="n\n")
    assert r.exit_code == 0
    assert "Aborted" in r.output
    r_status = runner.invoke(main, ["team", "status", str(tid)])
    assert "Status: active" in r_status.output


def test_team_shutdown_not_found(runner):
    r = runner.invoke(main, ["team", "shutdown", "9999"])
    assert r.exit_code == 1


# ── close ───────────────────────────────────────────────────────────────────


def test_team_close(runner):
    tid = _create_team(runner, name="c")
    r = runner.invoke(main, ["team", "close", str(tid)])
    assert r.exit_code == 0
    assert "closed" in r.output.lower()


def test_team_close_idempotent(runner):
    tid = _create_team(runner, name="ci")
    runner.invoke(main, ["team", "close", str(tid)])
    r = runner.invoke(main, ["team", "close", str(tid)])
    assert r.exit_code == 0  # idempotent


# ── ping ────────────────────────────────────────────────────────────────────


def test_team_ping(runner):
    tid = _create_team(runner, name="p")
    r = runner.invoke(main, ["team", "ping", str(tid)])
    assert r.exit_code == 0
    assert f"Pinged team #{tid}" in r.output


def test_team_ping_specific_agent(runner):
    tid = _create_team(runner, name="pa", members=["worker-1"])
    r = runner.invoke(main, ["team", "ping", str(tid), "--agent", "worker-1"])
    assert r.exit_code == 0
    assert "agent=worker-1" in r.output


def test_team_ping_not_found(runner):
    r = runner.invoke(main, ["team", "ping", "9999"])
    assert r.exit_code == 1
