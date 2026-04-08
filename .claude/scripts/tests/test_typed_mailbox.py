"""Tests for typed mailbox control semantics (Phase 3)."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from orchestration.db import OrchestrationDB
from orchestration.mailbox_service import MailboxService
from orchestration.models import (
    BlockedRequestPayload,
    IdleReadyPayload,
    SendMessageInput,
    ShutdownRequestPayload,
    TaskAssignmentPayload,
    VerifierFeedbackPayload,
    WorkHandoffPayload,
)


# ── MailboxService typed helpers ──────────────────────────────────────────


@pytest.fixture
def svc():
    db = OrchestrationDB(":memory:")
    yield MailboxService(db), db
    db.close()


def test_plain_send_has_null_msg_type(svc):
    """Backwards compat: sending without msg_type stores NULL."""
    mb, _ = svc
    msg = mb.send_message(
        SendMessageInput(from_agent="a", recipients=["b"], body="hi")
    )
    assert msg.msg_type is None
    inbox = mb.get_inbox("b")
    assert len(inbox) == 1
    assert inbox[0].message.msg_type is None


def test_send_message_with_msg_type_persists(svc):
    mb, _ = svc
    msg = mb.send_message(
        SendMessageInput(
            from_agent="a", recipients=["b"], body="hi", msg_type="direct"
        )
    )
    assert msg.msg_type == "direct"


def test_send_task_assignment(svc):
    mb, _ = svc
    payload = TaskAssignmentPayload(
        subtask_id=7, title="Write tests", description="Cover typed helpers",
        depends_on=[1, 2],
    )
    msg = mb.send_task_assignment("leader", "worker-1", payload)
    assert msg.msg_type == "task_assignment"
    decoded = json.loads(msg.body)
    assert decoded["subtask_id"] == 7
    assert decoded["title"] == "Write tests"
    assert decoded["depends_on"] == [1, 2]


def test_send_work_handoff(svc):
    mb, _ = svc
    payload = WorkHandoffPayload(
        subtask_id=3, summary="Done", artifacts=["file.md", "diff.patch"],
    )
    msg = mb.send_work_handoff("worker-1", "verifier", payload)
    assert msg.msg_type == "work_handoff"
    decoded = json.loads(msg.body)
    assert decoded["summary"] == "Done"
    assert decoded["artifacts"] == ["file.md", "diff.patch"]


def test_send_blocked_request(svc):
    mb, _ = svc
    payload = BlockedRequestPayload(
        subtask_id=5, reason="missing token", needs="API credentials",
    )
    msg = mb.send_blocked_request("worker-1", "leader", payload)
    assert msg.msg_type == "blocked_request"
    assert json.loads(msg.body)["needs"] == "API credentials"


def test_send_verifier_feedback(svc):
    mb, _ = svc
    payload = VerifierFeedbackPayload(
        subtask_id=4, verdict="needs_revision",
        findings=["lint fails", "missing test"], score=6.5,
    )
    msg = mb.send_verifier_feedback("verifier", "worker-1", payload)
    assert msg.msg_type == "verifier_feedback"
    decoded = json.loads(msg.body)
    assert decoded["verdict"] == "needs_revision"
    assert decoded["score"] == 6.5


def test_send_shutdown_request_default(svc):
    mb, _ = svc
    msg = mb.send_shutdown_request("leader", "worker-1")
    assert msg.msg_type == "shutdown_request"
    assert "graceful" in json.loads(msg.body)["reason"]


def test_send_shutdown_request_custom_reason(svc):
    mb, _ = svc
    msg = mb.send_shutdown_request(
        "leader", "worker-1",
        ShutdownRequestPayload(reason="quota exhausted"),
    )
    assert json.loads(msg.body)["reason"] == "quota exhausted"


def test_send_shutdown_ack(svc):
    mb, _ = svc
    msg = mb.send_shutdown_ack("worker-1", "leader")
    assert msg.msg_type == "shutdown_ack"


def test_send_idle_ready(svc):
    mb, _ = svc
    msg = mb.send_idle_ready(
        "worker-1", "leader", IdleReadyPayload(subtask_id=9),
    )
    assert msg.msg_type == "idle_ready"
    assert json.loads(msg.body)["subtask_id"] == 9


def test_inbox_filter_by_msg_type(svc):
    mb, _ = svc
    mb.send_message(SendMessageInput(
        from_agent="a", recipients=["w"], body="plain"))
    mb.send_task_assignment(
        "a", "w", TaskAssignmentPayload(subtask_id=1, title="T1"))
    mb.send_task_assignment(
        "a", "w", TaskAssignmentPayload(subtask_id=2, title="T2"))
    mb.send_blocked_request(
        "a", "w", BlockedRequestPayload(subtask_id=1, reason="r", needs="n"))

    assert len(mb.get_inbox("w")) == 4
    assignments = mb.get_inbox("w", msg_type="task_assignment")
    assert len(assignments) == 2
    assert all(m.message.msg_type == "task_assignment" for m in assignments)

    blocked = mb.get_inbox("w", msg_type="blocked_request")
    assert len(blocked) == 1

    none_match = mb.get_inbox("w", msg_type="shutdown_request")
    assert none_match == []


def test_typed_message_claim_ack_lifecycle(svc):
    """Typed messages participate in the normal claim/ack delivery flow."""
    mb, _ = svc
    mb.send_task_assignment(
        "leader", "w", TaskAssignmentPayload(subtask_id=1, title="T"))

    claimed = mb.claim_deliveries("w")
    assert len(claimed) == 1
    assert claimed[0].message.msg_type == "task_assignment"
    delivery = claimed[0].deliveries[0]
    assert delivery.status == "claimed"

    mb.ack_delivery(delivery.id, "w", delivery.claim_token)
    assert mb.get_inbox("w") == []  # ack'd deliveries no longer in inbox


def test_typed_send_to_new_recipient_no_crash(svc):
    """Sending a typed message to an agent with no prior inbox works."""
    mb, _ = svc
    msg = mb.send_idle_ready("worker-new", "leader-new")
    assert msg.msg_type == "idle_ready"
    assert len(mb.get_inbox("leader-new")) == 1


# ── API surface ────────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "test_typed_mailbox_api.db"
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
        yield TestClient(api_mod.app)
        db.close()


def test_api_send_with_msg_type(client):
    r = client.post("/api/mailbox/send", json={
        "from_agent": "leader",
        "recipients": ["worker-1"],
        "body": "{\"subtask_id\":1,\"title\":\"T\"}",
        "msg_type": "task_assignment",
    })
    assert r.status_code == 200
    assert r.json()["msg_type"] == "task_assignment"


def test_api_inbox_returns_msg_type(client):
    client.post("/api/mailbox/send", json={
        "from_agent": "a", "recipients": ["b"], "body": "hi",
    })
    client.post("/api/mailbox/send", json={
        "from_agent": "a", "recipients": ["b"], "body": "typed",
        "msg_type": "direct",
    })
    r = client.get("/api/mailbox/inbox/b")
    assert r.status_code == 200
    msgs = r.json()
    assert len(msgs) == 2
    # First untyped, second typed (nested under "message")
    assert msgs[0]["message"]["msg_type"] is None
    assert msgs[1]["message"]["msg_type"] == "direct"


def test_api_inbox_filter_by_msg_type(client):
    client.post("/api/mailbox/send", json={
        "from_agent": "a", "recipients": ["b"], "body": "plain",
    })
    client.post("/api/mailbox/send", json={
        "from_agent": "a", "recipients": ["b"], "body": "ta",
        "msg_type": "task_assignment",
    })
    client.post("/api/mailbox/send", json={
        "from_agent": "a", "recipients": ["b"], "body": "sd",
        "msg_type": "shutdown_request",
    })
    r = client.get("/api/mailbox/inbox/b", params={"msg_type": "task_assignment"})
    assert r.status_code == 200
    msgs = r.json()
    assert len(msgs) == 1
    assert msgs[0]["message"]["msg_type"] == "task_assignment"
