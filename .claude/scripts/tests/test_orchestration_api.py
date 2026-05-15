"""API integration tests for the orchestration control surface.

Uses FastAPI TestClient (synchronous httpx) with an isolated in-memory DB.
Proves all endpoints delegate to ConvoyService/MailboxService with zero
business logic in the handler layer.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# Ensure scripts dir is on path for config imports
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


@pytest.fixture
def client(tmp_path):
    """Each test gets an isolated orchestration DB via tmp_path."""
    db_path = tmp_path / "test_orch_api.db"
    with patch("config.ORCHESTRATION_DB_PATH", db_path):
        # Must re-import to pick up the patched config
        import importlib

        import orchestration.api as api_mod

        importlib.reload(api_mod)
        # Re-init services with the temp DB
        db, cs, ms, reg, ts = api_mod._get_services()
        api_mod._db = db
        api_mod._convoy_svc = cs
        api_mod._mailbox_svc = ms
        api_mod._executor_registry = reg
        api_mod._team_svc = ts
        yield TestClient(api_mod.app)
        db.close()


# ── Convoy endpoint tests ────────────────────────────────────────────────


def test_create_convoy(client):
    r = client.post(
        "/api/convoy",
        json={
            "title": "Test Convoy",
            "created_by": "sb",
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["convoy"]["title"] == "Test Convoy"
    assert data["convoy"]["status"] == "draft"
    assert data["convoy"]["id"] >= 1


def test_list_convoys(client):
    client.post("/api/convoy", json={"title": "A", "created_by": "sb"})
    client.post("/api/convoy", json={"title": "B", "created_by": "sb"})
    r = client.get("/api/convoy")
    assert r.status_code == 200
    titles = [c["title"] for c in r.json()]
    assert "A" in titles
    assert "B" in titles


def test_list_convoys_filter_status(client):
    client.post("/api/convoy", json={"title": "Draft", "created_by": "sb"})
    r = client.get("/api/convoy", params={"status": "active"})
    assert r.status_code == 200
    assert len(r.json()) == 0

    r = client.get("/api/convoy", params={"status": "draft"})
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_get_convoy(client):
    create = client.post("/api/convoy", json={"title": "Get Me", "created_by": "sb"})
    cid = create.json()["convoy"]["id"]
    r = client.get(f"/api/convoy/{cid}")
    assert r.status_code == 200
    assert r.json()["convoy"]["title"] == "Get Me"


def test_get_convoy_not_found(client):
    r = client.get("/api/convoy/9999")
    assert r.status_code == 404


def test_delete_convoy(client):
    create = client.post("/api/convoy", json={"title": "Delete Me", "created_by": "sb"})
    cid = create.json()["convoy"]["id"]
    r = client.delete(f"/api/convoy/{cid}")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # Verify gone
    r = client.get(f"/api/convoy/{cid}")
    assert r.status_code == 404


def test_update_convoy_status(client):
    create = client.post("/api/convoy", json={"title": "Status", "created_by": "sb"})
    cid = create.json()["convoy"]["id"]
    r = client.post(f"/api/convoy/{cid}/status", json={"status": "cancelled"})
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"


def test_add_subtasks(client):
    create = client.post("/api/convoy", json={"title": "AddSub", "created_by": "sb"})
    cid = create.json()["convoy"]["id"]
    r = client.post(
        f"/api/convoy/{cid}/subtasks",
        json={
            "subtasks": [
                {"title": "Sub A"},
                {"title": "Sub B"},
            ],
        },
    )
    assert r.status_code == 200
    assert len(r.json()) == 2
    assert r.json()[0]["title"] == "Sub A"


def test_add_subtasks_missing_convoy_returns_404(client):
    r = client.post(
        "/api/convoy/999/subtasks",
        json={
            "subtasks": [{"title": "Ghost Subtask"}],
        },
    )
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


def test_get_ready_subtasks(client):
    create = client.post("/api/convoy", json={"title": "Ready", "created_by": "sb"})
    cid = create.json()["convoy"]["id"]
    # Add a subtask (zero deps -> auto-ready)
    client.post(
        f"/api/convoy/{cid}/subtasks",
        json={
            "subtasks": [{"title": "Ready Task"}],
        },
    )
    r = client.get(f"/api/convoy/{cid}/ready")
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["title"] == "Ready Task"
    assert r.json()[0]["status"] == "ready"


def test_get_ready_subtasks_missing_convoy_returns_404(client):
    r = client.get("/api/convoy/999/ready")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


def test_dispatch_subtask(client):
    create = client.post("/api/convoy", json={"title": "Dispatch", "created_by": "sb"})
    cid = create.json()["convoy"]["id"]
    subs = client.post(
        f"/api/convoy/{cid}/subtasks",
        json={
            "subtasks": [{"title": "Dispatchable"}],
        },
    ).json()
    sid = subs[0]["id"]
    r = client.post(f"/api/convoy/{cid}/subtask/{sid}/dispatch", json={})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "accepted"
    assert data["executor_name"] == "local"


def test_dispatch_unknown_executor_returns_400(client):
    create = client.post("/api/convoy", json={"title": "Dispatch", "created_by": "sb"})
    cid = create.json()["convoy"]["id"]
    subs = client.post(
        f"/api/convoy/{cid}/subtasks",
        json={
            "subtasks": [{"title": "Dispatchable"}],
        },
    ).json()
    sid = subs[0]["id"]
    r = client.post(
        f"/api/convoy/{cid}/subtask/{sid}/dispatch",
        json={"executor_name": "typo-executor"},
    )
    assert r.status_code == 400
    assert "unknown executor" in r.json()["detail"].lower()


def test_dispatch_rejects_subtask_from_other_convoy(client):
    c1 = client.post("/api/convoy", json={"title": "One", "created_by": "sb"}).json()["convoy"][
        "id"
    ]
    c2 = client.post("/api/convoy", json={"title": "Two", "created_by": "sb"}).json()["convoy"][
        "id"
    ]
    sid = client.post(
        f"/api/convoy/{c1}/subtasks",
        json={
            "subtasks": [{"title": "Task A"}],
        },
    ).json()[0]["id"]

    r = client.post(f"/api/convoy/{c2}/subtask/{sid}/dispatch", json={})
    assert r.status_code == 404
    assert "not found in convoy" in r.json()["detail"].lower()


def test_complete_subtask(client):
    create = client.post("/api/convoy", json={"title": "Complete", "created_by": "sb"})
    cid = create.json()["convoy"]["id"]
    subs = client.post(
        f"/api/convoy/{cid}/subtasks",
        json={
            "subtasks": [{"title": "Completable"}],
        },
    ).json()
    sid = subs[0]["id"]
    # Dispatch first
    client.post(f"/api/convoy/{cid}/subtask/{sid}/dispatch", json={})
    r = client.post(f"/api/convoy/{cid}/subtask/{sid}/complete")
    assert r.status_code == 200
    data = r.json()
    assert data["convoy_completed"] is True
    assert isinstance(data["newly_ready"], list)


def test_complete_rejects_subtask_from_other_convoy(client):
    c1 = client.post("/api/convoy", json={"title": "One", "created_by": "sb"}).json()["convoy"][
        "id"
    ]
    c2 = client.post("/api/convoy", json={"title": "Two", "created_by": "sb"}).json()["convoy"][
        "id"
    ]
    sid = client.post(
        f"/api/convoy/{c1}/subtasks",
        json={
            "subtasks": [{"title": "Task A"}],
        },
    ).json()[0]["id"]

    r = client.post(f"/api/convoy/{c2}/subtask/{sid}/complete")
    assert r.status_code == 404
    assert "not found in convoy" in r.json()["detail"].lower()


def test_fail_subtask(client):
    create = client.post("/api/convoy", json={"title": "Fail", "created_by": "sb"})
    cid = create.json()["convoy"]["id"]
    subs = client.post(
        f"/api/convoy/{cid}/subtasks",
        json={
            "subtasks": [{"title": "Failable"}],
        },
    ).json()
    sid = subs[0]["id"]
    client.post(f"/api/convoy/{cid}/subtask/{sid}/dispatch", json={})
    r = client.post(
        f"/api/convoy/{cid}/subtask/{sid}/fail",
        json={
            "error_message": "test error",
        },
    )
    assert r.status_code == 200
    assert "convoy_failed" in r.json()


def test_fail_rejects_subtask_from_other_convoy(client):
    c1 = client.post("/api/convoy", json={"title": "One", "created_by": "sb"}).json()["convoy"][
        "id"
    ]
    c2 = client.post("/api/convoy", json={"title": "Two", "created_by": "sb"}).json()["convoy"][
        "id"
    ]
    sid = client.post(
        f"/api/convoy/{c1}/subtasks",
        json={
            "subtasks": [{"title": "Task A"}],
        },
    ).json()[0]["id"]

    r = client.post(f"/api/convoy/{c2}/subtask/{sid}/fail", json={"error_message": "x"})
    assert r.status_code == 404
    assert "not found in convoy" in r.json()["detail"].lower()


# ── Mailbox endpoint tests ───────────────────────────────────────────────


def test_send_message(client):
    r = client.post(
        "/api/mailbox/send",
        json={
            "from_agent": "sb",
            "recipients": ["agent-b"],
            "body": "Hello from API",
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["from_agent"] == "sb"
    assert data["body"] == "Hello from API"
    assert data["id"] >= 1


def test_inbox(client):
    client.post(
        "/api/mailbox/send",
        json={
            "from_agent": "sb",
            "recipients": ["agent-b"],
            "body": "Check inbox",
        },
    )
    r = client.get("/api/mailbox/inbox/agent-b")
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["message"]["body"] == "Check inbox"


def test_inbox_empty(client):
    r = client.get("/api/mailbox/inbox/nobody")
    assert r.status_code == 200
    assert r.json() == []


def test_claim(client):
    client.post(
        "/api/mailbox/send",
        json={
            "from_agent": "sb",
            "recipients": ["agent-b"],
            "body": "Claim me",
        },
    )
    r = client.post("/api/mailbox/claim/agent-b")
    assert r.status_code == 200
    claimed = r.json()
    assert len(claimed) == 1
    assert claimed[0]["message"]["body"] == "Claim me"
    # Delivery should be claimed
    agent_b_del = [d for d in claimed[0]["deliveries"] if d["recipient_agent"] == "agent-b"]
    assert len(agent_b_del) == 1
    assert agent_b_del[0]["status"] == "claimed"
    assert agent_b_del[0]["claim_token"] is not None


def test_ack(client):
    client.post(
        "/api/mailbox/send",
        json={
            "from_agent": "sb",
            "recipients": ["agent-b"],
            "body": "Ack me",
        },
    )
    claimed = client.post("/api/mailbox/claim/agent-b").json()
    delivery = [d for d in claimed[0]["deliveries"] if d["recipient_agent"] == "agent-b"][0]
    r = client.post(
        f"/api/mailbox/ack/{delivery['id']}",
        json={
            "recipient_agent": "agent-b",
            "claim_token": delivery["claim_token"],
        },
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_convoy_messages(client):
    # Create convoy, then send a message linked to it
    create = client.post("/api/convoy", json={"title": "MsgConvoy", "created_by": "sb"})
    cid = create.json()["convoy"]["id"]
    client.post(
        "/api/mailbox/send",
        json={
            "from_agent": "sb",
            "recipients": ["agent-b"],
            "body": "Convoy msg",
            "convoy_id": cid,
        },
    )
    r = client.get(f"/api/mailbox/convoy/{cid}")
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["message"]["body"] == "Convoy msg"


# ── Parity tests ─────────────────────────────────────────────────────────


def test_parity_convoy_lifecycle(client, tmp_path):
    """Create via API, verify via CLI — both see the same DB state."""
    from click.testing import CliRunner

    _CHAT_DIR = Path(__file__).resolve().parent.parent.parent / "chat"
    if str(_CHAT_DIR) not in sys.path:
        sys.path.insert(0, str(_CHAT_DIR))

    from cli import main

    db_path = tmp_path / "test_orch_api.db"

    # Create convoy via API
    r = client.post("/api/convoy", json={"title": "Parity", "created_by": "sb"})
    assert r.status_code == 200
    cid = r.json()["convoy"]["id"]

    # Add subtask via API
    subs = client.post(
        f"/api/convoy/{cid}/subtasks",
        json={
            "subtasks": [{"title": "Parity Sub"}],
        },
    ).json()
    assert len(subs) == 1

    # Read back via CLI (same DB path — already patched by fixture)
    cli_runner = CliRunner()
    cli_r = cli_runner.invoke(main, ["convoy", "show", str(cid)])
    assert cli_r.exit_code == 0
    assert "Parity" in cli_r.output
    assert "Parity Sub" in cli_r.output


def test_parity_mailbox_lifecycle(client, tmp_path):
    """Send via API, claim via CLI — both see the same DB state."""
    from click.testing import CliRunner

    _CHAT_DIR = Path(__file__).resolve().parent.parent.parent / "chat"
    if str(_CHAT_DIR) not in sys.path:
        sys.path.insert(0, str(_CHAT_DIR))

    from cli import main

    db_path = tmp_path / "test_orch_api.db"

    # Send via API
    r = client.post(
        "/api/mailbox/send",
        json={
            "from_agent": "sb",
            "recipients": ["agent-b"],
            "body": "Parity mail",
        },
    )
    assert r.status_code == 200

    # Read inbox via CLI
    cli_runner = CliRunner()
    cli_r = cli_runner.invoke(main, ["mailbox", "inbox", "agent-b"])
    assert cli_r.exit_code == 0
    assert "Parity mail" in cli_r.output


def test_api_thin_no_business_logic():
    """Prove api.py contains no SQL, no status transitions, no cycle detection.

    This is a static analysis test: grep the source for patterns that would
    indicate business logic leaking into the handler layer.
    """
    api_path = Path(__file__).resolve().parent.parent / "orchestration" / "api.py"
    source = api_path.read_text()

    # No raw SQL
    assert "SELECT " not in source, "api.py must not contain SQL SELECT statements"
    assert "INSERT " not in source, "api.py must not contain SQL INSERT statements"
    assert "UPDATE " not in source, "api.py must not contain SQL UPDATE statements"
    assert "DELETE FROM" not in source, "api.py must not contain SQL DELETE statements"

    # No status transition logic
    assert "CONVOY_TRANSITIONS" not in source, "api.py must not reference transition maps"
    assert "_detect_cycle" not in source, "api.py must not contain cycle detection"

    # No direct DB access
    assert "conn.execute" not in source, "api.py must not execute SQL directly"
    assert "sqlite3" not in source, "api.py must not import sqlite3"

    # Only imports from orchestration.* + stdlib + fastapi/pydantic
    import_lines = [
        l.strip() for l in source.splitlines() if l.strip().startswith(("import ", "from "))
    ]
    allowed_prefixes = (
        "from __future__",
        "import dataclasses",
        "import logging",
        "import os",
        "import time",
        "from typing",
        "from fastapi",
        "from pydantic",
        "from orchestration.",
        "import config",
        "import importlib",
        # PRP-7c Phase 3 (WS2 lifecycle-surfaces): API_PORT delegates through
        # personas.services for profile-aware port resolution. This is a
        # path-resolution helper, not business logic — same category as
        # ``import config``.
        "from personas.services",
        # PRD-8 Phase 3 / WS2 — dashboard router mount. The dashboard slice
        # owns its own router in dashboard_api.py; orchestration/api.py
        # only includes it via app.include_router. Slice ownership preserved.
        "from dashboard_api import router",
    )
    for line in import_lines:
        assert any(line.startswith(p) for p in allowed_prefixes), (
            f"api.py has disallowed import: {line}"
        )


def test_non_loopback_requires_explicit_opt_in(monkeypatch, tmp_path):
    import importlib

    db_path = tmp_path / "test_orch_api_non_loopback.db"
    monkeypatch.setenv("ORCHESTRATION_API_HOST", "0.0.0.0")
    monkeypatch.delenv("ORCHESTRATION_API_ALLOW_NON_LOOPBACK", raising=False)

    with patch("config.ORCHESTRATION_DB_PATH", db_path):
        import orchestration.api as api_mod

        with pytest.raises(RuntimeError, match="must stay loopback"):
            importlib.reload(api_mod)

    monkeypatch.setenv("ORCHESTRATION_API_ALLOW_NON_LOOPBACK", "true")


def test_non_loopback_without_token_rejected(monkeypatch, tmp_path):
    """Non-loopback + no token → RuntimeError at startup (Issue 1 fix)."""
    import importlib

    db_path = tmp_path / "test_orch_api_non_loopback_notoken.db"
    monkeypatch.setenv("ORCHESTRATION_API_HOST", "0.0.0.0")
    monkeypatch.setenv("ORCHESTRATION_API_ALLOW_NON_LOOPBACK", "true")
    monkeypatch.delenv("ORCHESTRATION_API_TOKEN", raising=False)

    with patch("config.ORCHESTRATION_DB_PATH", db_path):
        import orchestration.api as api_mod

        with pytest.raises(RuntimeError, match="requires ORCHESTRATION_API_TOKEN"):
            importlib.reload(api_mod)


def test_non_loopback_with_token_starts(monkeypatch, tmp_path):
    """Non-loopback + token set → module loads without error."""
    import importlib

    db_path = tmp_path / "test_orch_api_non_loopback_token.db"
    monkeypatch.setenv("ORCHESTRATION_API_HOST", "0.0.0.0")
    monkeypatch.setenv("ORCHESTRATION_API_ALLOW_NON_LOOPBACK", "true")
    monkeypatch.setenv("ORCHESTRATION_API_TOKEN", "remote-secret")

    with patch("config.ORCHESTRATION_DB_PATH", db_path):
        import orchestration.api as api_mod

        importlib.reload(api_mod)  # must not raise
        assert api_mod.ORCHESTRATION_API_TOKEN == "remote-secret"


# ── Subtask transition tests ────────────────────────────────────────────


def _make_dispatched_subtask(client):
    """Helper: create convoy → add subtask → dispatch → return (convoy_id, subtask_id)."""
    cid = client.post("/api/convoy", json={"title": "Trans", "created_by": "sb"}).json()["convoy"][
        "id"
    ]
    sid = client.post(
        f"/api/convoy/{cid}/subtasks",
        json={
            "subtasks": [{"title": "Transitionable"}],
        },
    ).json()[0]["id"]
    client.post(f"/api/convoy/{cid}/subtask/{sid}/dispatch", json={})
    return cid, sid


def test_transition_dispatched_to_running(client):
    cid, sid = _make_dispatched_subtask(client)
    r = client.post(f"/api/convoy/{cid}/subtask/{sid}/transition", json={"status": "running"})
    assert r.status_code == 200
    assert r.json()["status"] == "running"
    assert r.json()["started_at"] is not None


def test_transition_running_to_stalled(client):
    cid, sid = _make_dispatched_subtask(client)
    client.post(f"/api/convoy/{cid}/subtask/{sid}/transition", json={"status": "running"})
    r = client.post(f"/api/convoy/{cid}/subtask/{sid}/transition", json={"status": "stalled"})
    assert r.status_code == 200
    assert r.json()["status"] == "stalled"
    assert r.json()["stall_detected_at"] is not None


def test_transition_stalled_to_running(client):
    cid, sid = _make_dispatched_subtask(client)
    client.post(f"/api/convoy/{cid}/subtask/{sid}/transition", json={"status": "running"})
    client.post(f"/api/convoy/{cid}/subtask/{sid}/transition", json={"status": "stalled"})
    r = client.post(f"/api/convoy/{cid}/subtask/{sid}/transition", json={"status": "running"})
    assert r.status_code == 200
    assert r.json()["status"] == "running"


def test_transition_rejects_completed_via_transition(client):
    """completed must go through /complete, not /transition — protects dependency release."""
    cid, sid = _make_dispatched_subtask(client)
    client.post(f"/api/convoy/{cid}/subtask/{sid}/transition", json={"status": "running"})
    r = client.post(f"/api/convoy/{cid}/subtask/{sid}/transition", json={"status": "completed"})
    assert r.status_code == 400
    assert "cannot transition" in r.json()["detail"].lower()


def test_transition_rejects_failed_via_transition(client):
    """failed must go through /fail, not /transition — protects dependency release."""
    cid, sid = _make_dispatched_subtask(client)
    client.post(f"/api/convoy/{cid}/subtask/{sid}/transition", json={"status": "running"})
    r = client.post(f"/api/convoy/{cid}/subtask/{sid}/transition", json={"status": "failed"})
    assert r.status_code == 400
    assert "cannot transition" in r.json()["detail"].lower()


def test_transition_to_cancelled(client):
    cid, sid = _make_dispatched_subtask(client)
    r = client.post(f"/api/convoy/{cid}/subtask/{sid}/transition", json={"status": "cancelled"})
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"


def test_transition_invalid_returns_400(client):
    cid, sid = _make_dispatched_subtask(client)
    # dispatched → stalled is not valid (must go through running first)
    r = client.post(f"/api/convoy/{cid}/subtask/{sid}/transition", json={"status": "stalled"})
    assert r.status_code == 400
    assert "cannot transition" in r.json()["detail"].lower()


def test_transition_from_terminal_returns_400(client):
    cid, sid = _make_dispatched_subtask(client)
    client.post(f"/api/convoy/{cid}/subtask/{sid}/transition", json={"status": "cancelled"})
    r = client.post(f"/api/convoy/{cid}/subtask/{sid}/transition", json={"status": "running"})
    assert r.status_code == 400


def test_transition_rejects_subtask_from_other_convoy(client):
    c1 = client.post("/api/convoy", json={"title": "One", "created_by": "sb"}).json()["convoy"][
        "id"
    ]
    c2 = client.post("/api/convoy", json={"title": "Two", "created_by": "sb"}).json()["convoy"][
        "id"
    ]
    sid = client.post(
        f"/api/convoy/{c1}/subtasks",
        json={
            "subtasks": [{"title": "Task A"}],
        },
    ).json()[0]["id"]
    r = client.post(f"/api/convoy/{c2}/subtask/{sid}/transition", json={"status": "cancelled"})
    assert r.status_code == 404


# ── Subtask field update tests ──────────────────────────────────────────


def test_update_subtask_fields(client):
    cid, sid = _make_dispatched_subtask(client)
    r = client.patch(
        f"/api/convoy/{cid}/subtask/{sid}",
        json={
            "assigned_agent_id": "agent-1",
            "assigned_agent_name": "Agent One",
            "worktree_branch": "feat/task-1",
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["assigned_agent_id"] == "agent-1"
    assert data["assigned_agent_name"] == "Agent One"
    assert data["worktree_branch"] == "feat/task-1"


def test_update_subtask_fields_terminal_rejects_non_seal(client):
    """Non-seal fields (assigned_agent_id) rejected on terminal subtasks."""
    cid, sid = _make_dispatched_subtask(client)
    client.post(f"/api/convoy/{cid}/subtask/{sid}/transition", json={"status": "cancelled"})
    r = client.patch(
        f"/api/convoy/{cid}/subtask/{sid}",
        json={
            "assigned_agent_id": "agent-1",
        },
    )
    assert r.status_code == 400
    assert "terminal" in r.json()["detail"].lower()


def test_update_subtask_fields_rejects_other_convoy(client):
    c1 = client.post("/api/convoy", json={"title": "One", "created_by": "sb"}).json()["convoy"][
        "id"
    ]
    c2 = client.post("/api/convoy", json={"title": "Two", "created_by": "sb"}).json()["convoy"][
        "id"
    ]
    sid = client.post(
        f"/api/convoy/{c1}/subtasks",
        json={
            "subtasks": [{"title": "Task A"}],
        },
    ).json()[0]["id"]
    r = client.patch(f"/api/convoy/{c2}/subtask/{sid}", json={"assigned_agent_id": "x"})
    assert r.status_code == 404


def test_update_subtask_empty_body_noop(client):
    cid, sid = _make_dispatched_subtask(client)
    r = client.patch(f"/api/convoy/{cid}/subtask/{sid}", json={})
    assert r.status_code == 200


# ── Progress terminal guard test ────────────────────────────────────────


def test_progress_on_terminal_subtask_returns_400(client):
    cid, sid = _make_dispatched_subtask(client)
    client.post(f"/api/convoy/{cid}/subtask/{sid}/transition", json={"status": "cancelled"})
    r = client.post(
        f"/api/convoy/{cid}/subtask/{sid}/progress",
        json={
            "progress_pct": 0.5,
            "message": "should fail",
        },
    )
    assert r.status_code == 400
    assert "terminal" in r.json()["detail"].lower()


# ── Codex adversarial review regression tests ───────────────────────────


def test_regression_cancelled_only_convoy_is_cancelled(client):
    """Regression: all-cancelled convoy must finalize as cancelled, not completed."""
    cid = client.post("/api/convoy", json={"title": "CancelAll", "created_by": "sb"}).json()[
        "convoy"
    ]["id"]
    subs = client.post(
        f"/api/convoy/{cid}/subtasks",
        json={
            "subtasks": [{"title": "A"}, {"title": "B"}],
        },
    ).json()
    # Dispatch both
    for s in subs:
        client.post(f"/api/convoy/{cid}/subtask/{s['id']}/dispatch", json={})
    # Cancel both via transition
    for s in subs:
        r = client.post(
            f"/api/convoy/{cid}/subtask/{s['id']}/transition", json={"status": "cancelled"}
        )
        assert r.status_code == 200

    convoy = client.get(f"/api/convoy/{cid}").json()
    assert convoy["convoy"]["status"] == "cancelled"


def test_regression_merge_commit_settable_on_completed_subtask(client):
    """Regression: merge_commit is a seal field, settable after terminal."""
    cid, sid = _make_dispatched_subtask(client)
    # Complete via the dedicated handler (not /transition)
    client.post(f"/api/convoy/{cid}/subtask/{sid}/complete")
    r = client.patch(
        f"/api/convoy/{cid}/subtask/{sid}",
        json={"merge_commit": "abc123def"},
    )
    assert r.status_code == 200
    assert r.json()["merge_commit"] == "abc123def"


def test_regression_error_message_settable_on_failed_subtask(client):
    """Regression: error_message is a seal field, settable after terminal."""
    cid, sid = _make_dispatched_subtask(client)
    client.post(f"/api/convoy/{cid}/subtask/{sid}/fail", json={"error_message": "initial"})
    r = client.patch(
        f"/api/convoy/{cid}/subtask/{sid}",
        json={"error_message": "detailed root cause analysis"},
    )
    assert r.status_code == 200
    assert r.json()["error_message"] == "detailed root cause analysis"


def test_regression_non_seal_field_still_rejected_on_terminal(client):
    """Regression: assigned_agent_id is NOT a seal field — rejected on terminal."""
    cid, sid = _make_dispatched_subtask(client)
    client.post(f"/api/convoy/{cid}/subtask/{sid}/complete")
    r = client.patch(
        f"/api/convoy/{cid}/subtask/{sid}",
        json={"assigned_agent_id": "should-fail"},
    )
    assert r.status_code == 400
    assert "terminal" in r.json()["detail"].lower()


# ── Phase 6a: Auth Middleware Tests ──────────────────────────────────────

_TEST_TOKEN = "test-secret-token"


@pytest.fixture
def authed_client(tmp_path, monkeypatch):
    """Client with ORCHESTRATION_API_TOKEN='test-secret-token'. Yields (client, token).

    monkeypatch sets the env var BEFORE the module reload so the middleware
    picks up the token at module-level assignment time.
    """
    db_path = tmp_path / "test_orch_api_authed.db"
    monkeypatch.setenv("ORCHESTRATION_API_TOKEN", _TEST_TOKEN)
    with patch("config.ORCHESTRATION_DB_PATH", db_path):
        import importlib

        import orchestration.api as api_mod

        importlib.reload(api_mod)
        db, cs, ms, reg, ts = api_mod._get_services()
        api_mod._db = db
        api_mod._convoy_svc = cs
        api_mod._mailbox_svc = ms
        api_mod._executor_registry = reg
        api_mod._team_svc = ts
        yield TestClient(api_mod.app), _TEST_TOKEN
        db.close()


def test_auth_no_token_configured_allows_all(client):
    """No ORCHESTRATION_API_TOKEN set → all requests pass without auth header."""
    r = client.get("/api/convoy")
    assert r.status_code == 200


def test_auth_token_required_missing_header(authed_client):
    """Token configured, no Authorization header → 401."""
    c, _ = authed_client
    r = c.get("/api/convoy")
    assert r.status_code == 401
    assert "bearer" in r.json()["detail"].lower()


def test_auth_token_required_wrong_token(authed_client):
    """Token configured, wrong Bearer value → 401."""
    c, _ = authed_client
    r = c.get("/api/convoy", headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401


def test_auth_token_required_correct_token(authed_client):
    """Token configured, correct Bearer → 200."""
    c, token = authed_client
    r = c.get("/api/convoy", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


# ── Phase 6b: Executor Callback Tests ────────────────────────────────────


def _make_ready_subtask(client):
    """Helper: convoy → single no-dep subtask in 'ready' state."""
    cid = client.post("/api/convoy", json={"title": "CB", "created_by": "sb"}).json()["convoy"][
        "id"
    ]
    sid = client.post(
        f"/api/convoy/{cid}/subtasks",
        json={"subtasks": [{"title": "Task"}]},
    ).json()[0]["id"]
    return cid, sid


def test_callback_unknown_event_type(client):
    """Unknown event_type → 400."""
    cid, sid = _make_ready_subtask(client)
    r = client.post(
        "/api/executor/callback",
        json={
            "event_type": "subtask.teleported",
            "convoy_id": cid,
            "subtask_id": sid,
            "idempotency_key": f"key:{sid}",
            "payload": {},
        },
    )
    assert r.status_code == 400
    assert "unknown event_type" in r.json()["detail"].lower()


def test_callback_subtask_completed_no_deps(client):
    """Single subtask with no deps: completed callback → convoy completed."""
    cid, sid = _make_ready_subtask(client)
    client.post(f"/api/convoy/{cid}/subtask/{sid}/dispatch", json={})

    r = client.post(
        "/api/executor/callback",
        json={
            "event_type": "subtask.completed",
            "convoy_id": cid,
            "subtask_id": sid,
            "idempotency_key": f"comp:{sid}",
            "payload": {},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "processed"
    assert body["newly_dispatched"] == []

    convoy = client.get(f"/api/convoy/{cid}").json()
    assert convoy["convoy"]["status"] == "completed"


def test_callback_subtask_completed_triggers_auto_dispatch(client):
    """A → B chain: completing A auto-dispatches B and returns b_id in newly_dispatched."""
    r = client.post(
        "/api/convoy",
        json={
            "title": "Chain",
            "created_by": "sb",
            "subtasks": [
                {"title": "A"},
                {"title": "B", "depends_on_subtask_indexes": [0]},
            ],
        },
    )
    cid = r.json()["convoy"]["id"]
    a_id, b_id = [s["id"] for s in r.json()["subtasks"]]

    # Only A is ready initially
    ready = client.get(f"/api/convoy/{cid}/ready").json()
    assert len(ready) == 1 and ready[0]["id"] == a_id

    client.post(f"/api/convoy/{cid}/subtask/{a_id}/dispatch", json={})

    r = client.post(
        "/api/executor/callback",
        json={
            "event_type": "subtask.completed",
            "convoy_id": cid,
            "subtask_id": a_id,
            "idempotency_key": f"chain:a:{a_id}",
            "payload": {},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "processed"
    assert b_id in body["newly_dispatched"]

    # B is now dispatched
    convoy_data = client.get(f"/api/convoy/{cid}").json()
    b_sub = next(s for s in convoy_data["subtasks"] if s["id"] == b_id)
    assert b_sub["status"] == "dispatched"


def test_callback_duplicate_idempotency_key(client):
    """Duplicate callback with same idempotency_key → second returns already_processed."""
    cid, sid = _make_ready_subtask(client)
    client.post(f"/api/convoy/{cid}/subtask/{sid}/dispatch", json={})

    key = f"idem:{sid}"
    body = {
        "event_type": "subtask.completed",
        "convoy_id": cid,
        "subtask_id": sid,
        "idempotency_key": key,
        "payload": {},
    }

    r1 = client.post("/api/executor/callback", json=body)
    assert r1.status_code == 200
    assert r1.json()["status"] == "processed"

    r2 = client.post("/api/executor/callback", json=body)
    assert r2.status_code == 200
    assert r2.json()["status"] == "already_processed"
    assert r2.json()["newly_dispatched"] == []


def test_callback_subtask_failed(client):
    """subtask.failed callback → subtask marked failed."""
    cid, sid = _make_ready_subtask(client)
    client.post(f"/api/convoy/{cid}/subtask/{sid}/dispatch", json={})

    r = client.post(
        "/api/executor/callback",
        json={
            "event_type": "subtask.failed",
            "convoy_id": cid,
            "subtask_id": sid,
            "idempotency_key": f"fail:{sid}",
            "payload": {"error_message": "runtime exploded"},
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "processed"

    convoy_data = client.get(f"/api/convoy/{cid}").json()
    s = next(st for st in convoy_data["subtasks"] if st["id"] == sid)
    assert s["status"] == "failed"


def test_callback_subtask_started(client):
    """subtask.started callback → subtask transitions to running."""
    cid, sid = _make_ready_subtask(client)
    client.post(f"/api/convoy/{cid}/subtask/{sid}/dispatch", json={})

    r = client.post(
        "/api/executor/callback",
        json={
            "event_type": "subtask.started",
            "convoy_id": cid,
            "subtask_id": sid,
            "idempotency_key": f"start:{sid}",
            "payload": {},
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "processed"

    convoy_data = client.get(f"/api/convoy/{cid}").json()
    s = next(st for st in convoy_data["subtasks"] if st["id"] == sid)
    assert s["status"] == "running"


def test_callback_subtask_stalled(client):
    """subtask.stalled callback (requires running first) → subtask transitions to stalled."""
    cid, sid = _make_ready_subtask(client)
    client.post(f"/api/convoy/{cid}/subtask/{sid}/dispatch", json={})
    # Must be running before it can stall
    client.post(f"/api/convoy/{cid}/subtask/{sid}/transition", json={"status": "running"})

    r = client.post(
        "/api/executor/callback",
        json={
            "event_type": "subtask.stalled",
            "convoy_id": cid,
            "subtask_id": sid,
            "idempotency_key": f"stall:{sid}",
            "payload": {},
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "processed"

    convoy_data = client.get(f"/api/convoy/{cid}").json()
    s = next(st for st in convoy_data["subtasks"] if st["id"] == sid)
    assert s["status"] == "stalled"


def test_callback_completed_with_merge_commit(client):
    """subtask.completed with merge_commit payload → merge_commit stored on subtask."""
    cid, sid = _make_ready_subtask(client)
    client.post(f"/api/convoy/{cid}/subtask/{sid}/dispatch", json={})

    r = client.post(
        "/api/executor/callback",
        json={
            "event_type": "subtask.completed",
            "convoy_id": cid,
            "subtask_id": sid,
            "idempotency_key": f"mc:{sid}",
            "payload": {"merge_commit": "deadbeef1234"},
        },
    )
    assert r.status_code == 200

    convoy_data = client.get(f"/api/convoy/{cid}").json()
    s = next(st for st in convoy_data["subtasks"] if st["id"] == sid)
    assert s["merge_commit"] == "deadbeef1234"


def test_callback_invalid_subtask_id_deletes_receipt_for_retry(client):
    """Processing error deletes the receipt so the same idempotency_key can be retried."""
    cid = client.post(
        "/api/convoy", json={"title": "Retry", "created_by": "sb"}
    ).json()["convoy"]["id"]
    nonexistent_id = 99999

    key = f"retry:{nonexistent_id}"
    payload = {
        "event_type": "subtask.completed",
        "convoy_id": cid,
        "subtask_id": nonexistent_id,
        "idempotency_key": key,
        "payload": {},
    }

    # First call: processing fails (subtask not found) → receipt deleted
    r1 = client.post("/api/executor/callback", json=payload)
    assert r1.status_code == 400

    # Second call with same key: NOT already_processed — receipt was cleaned up,
    # so it tries to process again (and fails again for the same reason)
    r2 = client.post("/api/executor/callback", json=payload)
    assert r2.status_code == 400
    assert r2.json().get("detail") is not None  # real processing error, not idempotency skip


# ── Phase 6c: CAS Dispatch Tests ─────────────────────────────────────────


def test_dispatch_cas_prevents_double_dispatch(client):
    """Dispatching the same subtask twice → second attempt returns 400."""
    cid, sid = _make_ready_subtask(client)

    r1 = client.post(f"/api/convoy/{cid}/subtask/{sid}/dispatch", json={})
    assert r1.status_code == 200
    assert r1.json()["status"] == "accepted"

    # Subtask is now 'dispatched' — no longer 'ready'
    r2 = client.post(f"/api/convoy/{cid}/subtask/{sid}/dispatch", json={})
    assert r2.status_code == 400
    assert "not ready" in r2.json()["detail"].lower()


def test_dispatch_executor_exception_rolls_back_claim(client):
    """executor.dispatch() raising rolls claim back — subtask not stuck in 'dispatched'.

    Tests the service directly to avoid TestClient re-raising unhandled exceptions.
    The service and the HTTP test share the same DB via api_mod._convoy_svc.
    """
    from unittest.mock import MagicMock

    import orchestration.api as api_mod

    cid, sid = _make_ready_subtask(client)

    raising_executor = MagicMock()
    raising_executor.dispatch.side_effect = RuntimeError("executor boom")

    # Call the service directly — TestClient re-raises unhandled server exceptions
    with pytest.raises(RuntimeError, match="executor boom"):
        api_mod._convoy_svc.dispatch_subtask(sid, executor=raising_executor)

    # Critical: subtask must NOT be stuck in 'dispatched' — rollback must have fired
    convoy_data = client.get(f"/api/convoy/{cid}").json()
    s = next(st for st in convoy_data["subtasks"] if st["id"] == sid)
    assert s["status"] == "ready"
    assert s["dispatched_at"] is None


def test_dispatch_cas_terminal_state_not_redispatchable(client):
    """Completed (terminal) subtask cannot be re-dispatched — different error than 'already dispatched'."""
    cid, sid = _make_ready_subtask(client)
    client.post(f"/api/convoy/{cid}/subtask/{sid}/dispatch", json={})
    client.post(f"/api/convoy/{cid}/subtask/{sid}/complete")

    r = client.post(f"/api/convoy/{cid}/subtask/{sid}/dispatch", json={})
    assert r.status_code == 400
    detail = r.json()["detail"].lower()
    assert "not ready" in detail
    assert "completed" in detail


# ── Phase 6d: Parity Proofs ───────────────────────────────────────────────


def test_gui_off_convoy_linear_chain(client):
    """Full A→B→C convoy driven by framework callback API alone. MC not involved.

    Proves the conductor loop (handle_executor_callback + _auto_dispatch_ready)
    can drive a convoy to completion without Mission Control's webhook route.
    """
    r = client.post(
        "/api/convoy",
        json={
            "title": "GUI-Off Proof",
            "created_by": "sb",
            "subtasks": [
                {"title": "A"},
                {"title": "B", "depends_on_subtask_indexes": [0]},
                {"title": "C", "depends_on_subtask_indexes": [1]},
            ],
        },
    )
    cid = r.json()["convoy"]["id"]
    a_id, b_id, c_id = [s["id"] for s in r.json()["subtasks"]]

    # Only A is ready initially (B and C are pending — have unmet deps)
    ready = client.get(f"/api/convoy/{cid}/ready").json()
    assert len(ready) == 1 and ready[0]["id"] == a_id

    # Dispatch A (local executor — no MC)
    client.post(f"/api/convoy/{cid}/subtask/{a_id}/dispatch", json={})

    # Callback: A completed → B becomes ready → auto-dispatched
    r = client.post(
        "/api/executor/callback",
        json={
            "event_type": "subtask.completed",
            "convoy_id": cid,
            "subtask_id": a_id,
            "idempotency_key": f"proof:a:{a_id}",
            "payload": {},
        },
    )
    assert r.json()["status"] == "processed"
    assert b_id in r.json()["newly_dispatched"]

    # Callback: B completed → C becomes ready → auto-dispatched
    r = client.post(
        "/api/executor/callback",
        json={
            "event_type": "subtask.completed",
            "convoy_id": cid,
            "subtask_id": b_id,
            "idempotency_key": f"proof:b:{b_id}",
            "payload": {},
        },
    )
    assert r.json()["status"] == "processed"
    assert c_id in r.json()["newly_dispatched"]

    # Callback: C completed → convoy done, no more subtasks to dispatch
    r = client.post(
        "/api/executor/callback",
        json={
            "event_type": "subtask.completed",
            "convoy_id": cid,
            "subtask_id": c_id,
            "idempotency_key": f"proof:c:{c_id}",
            "payload": {},
        },
    )
    assert r.json()["status"] == "processed"
    assert r.json()["newly_dispatched"] == []

    # Convoy is completed — GUI-off proof ✓
    assert client.get(f"/api/convoy/{cid}").json()["convoy"]["status"] == "completed"


def test_callback_ingress_idempotency_proof(client):
    """Duplicate callbacks with same idempotency_key → exactly-once semantics."""
    cid, sid = _make_ready_subtask(client)
    client.post(f"/api/convoy/{cid}/subtask/{sid}/dispatch", json={})

    key = f"idem-proof:{sid}"
    body = {
        "event_type": "subtask.completed",
        "convoy_id": cid,
        "subtask_id": sid,
        "idempotency_key": key,
        "payload": {},
    }

    r1 = client.post("/api/executor/callback", json=body)
    assert r1.status_code == 200
    assert r1.json()["status"] == "processed"

    # Idempotent: second call with same key — exactly-once ✓
    r2 = client.post("/api/executor/callback", json=body)
    assert r2.status_code == 200
    assert r2.json()["status"] == "already_processed"

    # Convoy still completed — not double-processed
    assert client.get(f"/api/convoy/{cid}").json()["convoy"]["status"] == "completed"


def test_callback_wrong_convoy_id_rejected(client):
    """Callback with subtask_id from a different convoy → 400 (Issue 3a fix)."""
    # Convoy A with subtask A
    cid_a, sid_a = _make_ready_subtask(client)
    client.post(f"/api/convoy/{cid_a}/subtask/{sid_a}/dispatch", json={})

    # Convoy B — separate
    cid_b = client.post(
        "/api/convoy", json={"title": "B", "created_by": "sb"}
    ).json()["convoy"]["id"]

    # Submit callback claiming subtask belongs to convoy B (it actually belongs to A)
    r = client.post(
        "/api/executor/callback",
        json={
            "event_type": "subtask.completed",
            "convoy_id": cid_b,  # wrong convoy
            "subtask_id": sid_a,  # belongs to convoy A
            "idempotency_key": f"integrity:{sid_a}",
            "payload": {},
        },
    )
    assert r.status_code == 400
    assert "does not belong to convoy" in r.json()["detail"].lower()

    # Convoy A subtask must NOT have been completed (state preserved)
    convoy_a = client.get(f"/api/convoy/{cid_a}").json()
    s = next(st for st in convoy_a["subtasks"] if st["id"] == sid_a)
    assert s["status"] == "dispatched"


def test_callback_completed_then_failed_is_noop(client):
    """subtask.completed followed by subtask.failed with a *different* idempotency key
    must NOT flip the subtask back to failed — terminal guard protects convoy state."""
    cid, sid = _make_ready_subtask(client)
    client.post(f"/api/convoy/{cid}/subtask/{sid}/dispatch", json={})

    # First: complete the subtask
    r = client.post(
        "/api/executor/callback",
        json={
            "event_type": "subtask.completed",
            "convoy_id": cid,
            "subtask_id": sid,
            "idempotency_key": f"completed:{sid}",
            "payload": {},
        },
    )
    assert r.json()["status"] == "processed"

    # Second: send a failure callback with a DIFFERENT key (buggy executor / race)
    r = client.post(
        "/api/executor/callback",
        json={
            "event_type": "subtask.failed",
            "convoy_id": cid,
            "subtask_id": sid,
            "idempotency_key": f"failed:{sid}",  # different key — bypasses dedup
            "payload": {"error_message": "late failure"},
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "processed"

    # Subtask must remain completed — not flipped to failed
    convoy = client.get(f"/api/convoy/{cid}").json()
    s = next(st for st in convoy["subtasks"] if st["id"] == sid)
    assert s["status"] == "completed"
    assert convoy["convoy"]["status"] == "completed"


def test_callback_failed_then_completed_is_noop(client):
    """subtask.failed followed by subtask.completed with a different key must NOT
    flip a failed subtask to completed — terminal guard is symmetric."""
    cid, sid = _make_ready_subtask(client)
    client.post(f"/api/convoy/{cid}/subtask/{sid}/dispatch", json={})

    # First: fail the subtask
    r = client.post(
        "/api/executor/callback",
        json={
            "event_type": "subtask.failed",
            "convoy_id": cid,
            "subtask_id": sid,
            "idempotency_key": f"fail-first:{sid}",
            "payload": {"error_message": "boom"},
        },
    )
    assert r.json()["status"] == "processed"

    # Second: send a completion callback with a different key
    r = client.post(
        "/api/executor/callback",
        json={
            "event_type": "subtask.completed",
            "convoy_id": cid,
            "subtask_id": sid,
            "idempotency_key": f"comp-late:{sid}",
            "payload": {},
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "processed"

    # Subtask must remain failed — not flipped to completed
    convoy = client.get(f"/api/convoy/{cid}").json()
    s = next(st for st in convoy["subtasks"] if st["id"] == sid)
    assert s["status"] == "failed"


def test_completion_newly_unblocked_only_excludes_preexisting_ready(client):
    """handle_subtask_completion returns only subtasks unblocked by THIS event,
    not pre-existing ready subtasks. Auto-dispatch must not grab unrelated ready tasks."""
    # Create convoy: A (no deps), B (no deps, left ready), C (depends on A)
    r = client.post(
        "/api/convoy",
        json={
            "title": "SelectiveDispatch",
            "created_by": "sb",
            "subtasks": [
                {"title": "A"},
                {"title": "B"},  # independent, starts ready
                {"title": "C", "depends_on_subtask_indexes": [0]},
            ],
        },
    )
    cid = r.json()["convoy"]["id"]
    a_id, b_id, c_id = [s["id"] for s in r.json()["subtasks"]]

    # Dispatch A only — leave B in ready state intentionally
    client.post(f"/api/convoy/{cid}/subtask/{a_id}/dispatch", json={})

    # Complete A via callback
    r = client.post(
        "/api/executor/callback",
        json={
            "event_type": "subtask.completed",
            "convoy_id": cid,
            "subtask_id": a_id,
            "idempotency_key": f"selective:{a_id}",
            "payload": {},
        },
    )
    assert r.status_code == 200
    body = r.json()

    # Only C should be in newly_dispatched — not B (B was already ready before A completed)
    assert c_id in body["newly_dispatched"], "C should be auto-dispatched (newly unblocked by A)"
    assert b_id not in body["newly_dispatched"], "B was already ready — should not be grabbed"

    # B must still be in ready state (not dispatched by the conductor)
    convoy = client.get(f"/api/convoy/{cid}").json()
    b = next(s for s in convoy["subtasks"] if s["id"] == b_id)
    assert b["status"] == "ready"


# ── PRD-8 Phase 7a (R3 NB1) — /api/audit-log middleware exemption ────────


def test_phase7a_audit_log_path_exempt_from_outer_bearer(tmp_path, monkeypatch):
    """R3 NB1 — outer ORCHESTRATION_API_TOKEN middleware skips /api/audit-log.

    Without the exemption, a token-set deployment would force the SAME
    Authorization: Bearer to satisfy BOTH the orchestration token (outer)
    AND the dashboard admin token (inner) — fail-closed forever unless the
    two tokens happen to be equal. This test proves the middleware passes
    through (NOT 401), and the inner endpoint returns 503 since
    DASHBOARD_ADMIN_TOKEN is unset.
    """
    monkeypatch.setenv("ORCHESTRATION_API_TOKEN", "orch-token-abc")
    monkeypatch.delenv("DASHBOARD_ADMIN_TOKEN", raising=False)
    db_path = tmp_path / "test_orch_audit_exempt.db"
    monkeypatch.setattr("config.ORCHESTRATION_DB_PATH", db_path)
    monkeypatch.setattr(
        "config.DASHBOARD_DB_PATH", tmp_path / "test_audit_dashboard.db"
    )
    import importlib
    import orchestration.api as api_mod
    importlib.reload(api_mod)
    db, cs, ms, reg, ts = api_mod._get_services()
    api_mod._db = db
    api_mod._convoy_svc = cs
    api_mod._mailbox_svc = ms
    api_mod._executor_registry = reg
    api_mod._team_svc = ts

    test_client = TestClient(api_mod.app)
    # No Bearer header. If middleware NOT exempt, this would 401. With the
    # exemption, the request reaches the inner handler and 503s on missing
    # DASHBOARD_ADMIN_TOKEN.
    r = test_client.get("/api/audit-log")
    assert r.status_code == 503, (
        f"Expected 503 (admin-token unset, exemption passed through). "
        f"Got {r.status_code}: {r.text}"
    )
    db.close()
