"""Tests for canonical team session state (Phase 2)."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# Ensure scripts dir is on path for config imports
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from orchestration.db import OrchestrationDB
from orchestration.models import (
    AddTeamMemberInput,
    CreateConvoyInput,
    CreateSubtaskInput,
    CreateTeamSessionInput,
)
from orchestration.convoy_service import ConvoyService
from orchestration.team_service import TeamService


# ── TeamService unit tests ────────────────────────────────────────────────


@pytest.fixture
def svc():
    db = OrchestrationDB(":memory:")
    yield TeamService(db), db
    db.close()


def test_create_team_session_minimal(svc):
    ts, _ = svc
    result = ts.create_team_session(
        CreateTeamSessionInput(team_name="alpha", lead_agent_id="leader-1")
    )
    assert result.session.id >= 1
    assert result.session.team_name == "alpha"
    assert result.session.lead_agent_id == "leader-1"
    assert result.session.status == "active"
    assert result.session.backend_type == "local"
    assert result.session.convoy_id is None
    # Leader auto-inserted as first member
    assert len(result.members) == 1
    assert result.members[0].role == "leader"
    assert result.members[0].agent_id == "leader-1"


def test_create_team_session_with_convoy(svc):
    ts, db = svc
    cs = ConvoyService(db)
    convoy = cs.create_convoy(
        CreateConvoyInput(
            title="Work",
            created_by="sb",
            subtasks=[CreateSubtaskInput(title="a"), CreateSubtaskInput(title="b")],
        )
    )
    result = ts.create_team_session(
        CreateTeamSessionInput(
            team_name="linked",
            lead_agent_id="leader-2",
            convoy_id=convoy.convoy.id,
        )
    )
    assert result.session.convoy_id == convoy.convoy.id


def test_create_team_session_validates_required_fields(svc):
    ts, _ = svc
    with pytest.raises(ValueError, match="team_name"):
        ts.create_team_session(CreateTeamSessionInput(team_name="", lead_agent_id="x"))
    with pytest.raises(ValueError, match="lead_agent_id"):
        ts.create_team_session(CreateTeamSessionInput(team_name="t", lead_agent_id=""))


def test_get_team_session_with_members(svc):
    ts, _ = svc
    created = ts.create_team_session(
        CreateTeamSessionInput(team_name="beta", lead_agent_id="L")
    )
    ts.add_member(created.session.id, AddTeamMemberInput(agent_id="w1"))
    ts.add_member(created.session.id, AddTeamMemberInput(agent_id="w2"))
    fetched = ts.get_team_session(created.session.id)
    assert fetched is not None
    assert len(fetched.members) == 3
    roles = {m.agent_id: m.role for m in fetched.members}
    assert roles["L"] == "leader"
    assert roles["w1"] == "worker"
    assert roles["w2"] == "worker"


def test_get_team_session_not_found(svc):
    ts, _ = svc
    assert ts.get_team_session(9999) is None


def test_list_team_sessions_filter_status(svc):
    ts, _ = svc
    t1 = ts.create_team_session(CreateTeamSessionInput(team_name="a", lead_agent_id="L"))
    t2 = ts.create_team_session(CreateTeamSessionInput(team_name="b", lead_agent_id="L"))
    ts.close_team_session(t2.session.id)

    active = ts.list_team_sessions(status="active")
    closed = ts.list_team_sessions(status="closed")
    assert {t.id for t in active} == {t1.session.id}
    assert {t.id for t in closed} == {t2.session.id}

    all_teams = ts.list_team_sessions()
    assert len(all_teams) == 2


def test_add_member_unique_constraint(svc):
    ts, _ = svc
    t = ts.create_team_session(
        CreateTeamSessionInput(team_name="c", lead_agent_id="L")
    )
    ts.add_member(t.session.id, AddTeamMemberInput(agent_id="w"))
    with pytest.raises(ValueError, match="already a member"):
        ts.add_member(t.session.id, AddTeamMemberInput(agent_id="w"))


def test_add_member_to_closed_team_rejected(svc):
    ts, _ = svc
    t = ts.create_team_session(CreateTeamSessionInput(team_name="x", lead_agent_id="L"))
    ts.close_team_session(t.session.id)
    with pytest.raises(ValueError, match="terminal"):
        ts.add_member(t.session.id, AddTeamMemberInput(agent_id="w"))


def test_add_member_unknown_team(svc):
    ts, _ = svc
    with pytest.raises(ValueError, match="not found"):
        ts.add_member(9999, AddTeamMemberInput(agent_id="w"))


def test_update_member_status(svc):
    ts, _ = svc
    t = ts.create_team_session(CreateTeamSessionInput(team_name="u", lead_agent_id="L"))
    ts.add_member(t.session.id, AddTeamMemberInput(agent_id="w"))
    updated = ts.update_member_status(t.session.id, "w", "idle")
    assert updated.status == "idle"
    with pytest.raises(ValueError, match="Invalid member status"):
        ts.update_member_status(t.session.id, "w", "bogus")
    with pytest.raises(ValueError, match="not found"):
        ts.update_member_status(t.session.id, "nonexistent", "idle")


def test_ping_activity_updates_timestamps(svc):
    ts, db = svc
    t = ts.create_team_session(CreateTeamSessionInput(team_name="p", lead_agent_id="L"))
    before = db.conn.execute(
        "SELECT last_activity_at FROM team_sessions WHERE id=?", (t.session.id,)
    ).fetchone()[0]
    import time as _time

    _time.sleep(1.1)
    ts.ping_activity(t.session.id, agent_id="L")
    after = db.conn.execute(
        "SELECT last_activity_at FROM team_sessions WHERE id=?", (t.session.id,)
    ).fetchone()[0]
    member_ts = db.conn.execute(
        "SELECT last_activity_at FROM team_members WHERE team_session_id=? AND agent_id=?",
        (t.session.id, "L"),
    ).fetchone()[0]
    assert after > before
    assert member_ts >= after - 2  # allow clock drift


def test_ping_activity_unknown_team(svc):
    ts, _ = svc
    with pytest.raises(ValueError, match="not found"):
        ts.ping_activity(9999)


def test_request_shutdown_transitions_status(svc):
    ts, _ = svc
    t = ts.create_team_session(CreateTeamSessionInput(team_name="s", lead_agent_id="L"))
    result = ts.request_shutdown(t.session.id)
    assert result.status == "shutdown_requested"
    assert result.shutdown_requested_at is not None
    # Idempotent
    result2 = ts.request_shutdown(t.session.id)
    assert result2.status == "shutdown_requested"


def test_close_team_session(svc):
    ts, _ = svc
    t = ts.create_team_session(CreateTeamSessionInput(team_name="z", lead_agent_id="L"))
    ts.add_member(t.session.id, AddTeamMemberInput(agent_id="w"))
    result = ts.close_team_session(t.session.id)
    assert result.status == "closed"
    assert result.closed_at is not None
    # Members also closed
    fetched = ts.get_team_session(t.session.id)
    assert all(m.status == "closed" for m in fetched.members)


def test_close_team_session_idempotent(svc):
    ts, _ = svc
    t = ts.create_team_session(CreateTeamSessionInput(team_name="i", lead_agent_id="L"))
    first = ts.close_team_session(t.session.id)
    second = ts.close_team_session(t.session.id)
    assert first.status == second.status == "closed"
    assert first.closed_at == second.closed_at


def test_close_team_session_not_found(svc):
    ts, _ = svc
    with pytest.raises(ValueError, match="not found"):
        ts.close_team_session(9999)


# ── API endpoint tests ────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "test_team_api.db"
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


def test_api_create_team(client):
    r = client.post(
        "/api/team",
        json={"team_name": "api-team", "lead_agent_id": "L"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["session"]["team_name"] == "api-team"
    assert data["session"]["status"] == "active"
    assert len(data["members"]) == 1


def test_api_create_team_validation(client):
    r = client.post("/api/team", json={"team_name": "", "lead_agent_id": "x"})
    assert r.status_code == 400


def test_api_list_and_get_team(client):
    c = client.post("/api/team", json={"team_name": "t1", "lead_agent_id": "L"}).json()
    tid = c["session"]["id"]
    client.post("/api/team", json={"team_name": "t2", "lead_agent_id": "L"})

    r = client.get("/api/team")
    assert r.status_code == 200
    assert len(r.json()) == 2

    r = client.get(f"/api/team/{tid}")
    assert r.status_code == 200
    assert r.json()["session"]["team_name"] == "t1"

    r = client.get("/api/team/9999")
    assert r.status_code == 404


def test_api_list_team_filter(client):
    c = client.post("/api/team", json={"team_name": "a", "lead_agent_id": "L"}).json()
    client.delete(f"/api/team/{c['session']['id']}")
    r = client.get("/api/team", params={"status": "closed"})
    assert r.status_code == 200
    assert len(r.json()) == 1
    r = client.get("/api/team", params={"status": "active"})
    assert len(r.json()) == 0


def test_api_add_member(client):
    c = client.post("/api/team", json={"team_name": "m", "lead_agent_id": "L"}).json()
    tid = c["session"]["id"]
    r = client.post(
        f"/api/team/{tid}/members",
        json={"agent_id": "w1", "role": "worker"},
    )
    assert r.status_code == 200
    assert r.json()["agent_id"] == "w1"
    # Duplicate rejected
    r2 = client.post(f"/api/team/{tid}/members", json={"agent_id": "w1"})
    assert r2.status_code == 400
    # Unknown team
    r3 = client.post("/api/team/9999/members", json={"agent_id": "w"})
    assert r3.status_code == 404


def test_api_shutdown_and_delete(client):
    c = client.post("/api/team", json={"team_name": "sd", "lead_agent_id": "L"}).json()
    tid = c["session"]["id"]
    r = client.post(f"/api/team/{tid}/shutdown")
    assert r.status_code == 200
    assert r.json()["status"] == "shutdown_requested"

    r = client.delete(f"/api/team/{tid}")
    assert r.status_code == 200
    assert r.json()["status"] == "closed"


def test_api_ping(client):
    c = client.post("/api/team", json={"team_name": "pg", "lead_agent_id": "L"}).json()
    tid = c["session"]["id"]
    r = client.post(f"/api/team/{tid}/ping", json={"agent_id": "L"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    r = client.post("/api/team/9999/ping", json={})
    assert r.status_code == 404
