"""Unit tests for mailbox service.

Each test mirrors the MC donor function it replaces.
Parity oracle: mission-control/src/lib/mailbox.ts
"""

import pytest

from orchestration.db import OrchestrationDB
from orchestration.mailbox_service import MailboxService
from orchestration.models import SendMessageInput


@pytest.fixture
def svc():
    db = OrchestrationDB(":memory:")
    yield MailboxService(db)
    db.close()


# ── Send ───────────────────────────────────────────────────────────────────


def test_send_message(svc):
    # Parity: mailbox.ts:sendMessage()
    msg = svc.send_message(SendMessageInput(
        from_agent="sb",
        recipients=["agent-b", "agent-c"],
        body="Hello everyone",
        subject="Greeting",
        message_type="command",
    ))
    assert msg.id > 0
    assert msg.from_agent == "sb"
    assert msg.body == "Hello everyone"
    assert msg.message_type == "command"


def test_send_creates_per_recipient_deliveries(svc):
    # Parity: mailbox.ts:sendMessage() — one delivery per recipient
    msg = svc.send_message(SendMessageInput(
        from_agent="sb",
        recipients=["agent-b", "agent-c"],
        body="Multi-recipient",
    ))
    inbox_agent_b = svc.get_inbox("agent-b")
    inbox_agent_c = svc.get_inbox("agent-c")
    assert len(inbox_agent_b) == 1
    assert len(inbox_agent_c) == 1


def test_send_no_recipients_raises(svc):
    with pytest.raises(ValueError, match="recipient"):
        svc.send_message(SendMessageInput(from_agent="sb", recipients=[], body="No one"))


def test_send_with_convoy_id(svc):
    # Messages can be associated with a convoy (FK requires real convoy)
    from orchestration.convoy_service import ConvoyService
    from orchestration.models import CreateConvoyInput
    cs = ConvoyService(svc.db)
    c = cs.create_convoy(CreateConvoyInput(title="Test", created_by="sb"))
    msg = svc.send_message(SendMessageInput(
        from_agent="sb", recipients=["agent-b"],
        body="Real convoy", convoy_id=c.convoy.id,
    ))
    assert msg.convoy_id == c.convoy.id
    convoy_msgs = svc.get_convoy_messages(c.convoy.id)
    assert len(convoy_msgs) == 1
    assert convoy_msgs[0].message.body == "Real convoy"


# ── Claim ──────────────────────────────────────────────────────────────────


def test_claim_deliveries(svc):
    # Parity: mailbox.ts:claimDeliveries()
    svc.send_message(SendMessageInput(
        from_agent="sb", recipients=["agent-b"], body="Claim me",
    ))
    claimed = svc.claim_deliveries("agent-b")
    assert len(claimed) == 1
    assert claimed[0].message.body == "Claim me"
    # Delivery should be claimed
    agent_b_delivery = [d for d in claimed[0].deliveries if d.recipient_agent == "agent-b"][0]
    assert agent_b_delivery.status == "claimed"
    assert agent_b_delivery.claim_token is not None
    assert agent_b_delivery.claimed_at is not None


def test_claim_empty_returns_empty(svc):
    # Parity: mailbox.ts:claimDeliveries() — no pending
    claimed = svc.claim_deliveries("nobody")
    assert claimed == []


def test_claim_does_not_double_claim(svc):
    svc.send_message(SendMessageInput(
        from_agent="sb", recipients=["agent-b"], body="Once",
    ))
    first = svc.claim_deliveries("agent-b")
    assert len(first) == 1
    second = svc.claim_deliveries("agent-b")
    assert len(second) == 0  # already claimed


def test_claim_with_limit(svc):
    for i in range(5):
        svc.send_message(SendMessageInput(
            from_agent="sb", recipients=["agent-b"], body=f"Msg {i}",
        ))
    claimed = svc.claim_deliveries("agent-b", limit=3)
    assert len(claimed) == 3
    remaining = svc.claim_deliveries("agent-b", limit=10)
    assert len(remaining) == 2


# ── Ack ────────────────────────────────────────────────────────────────────


def test_ack_delivery(svc):
    # Parity: mailbox.ts:ackDelivery()
    svc.send_message(SendMessageInput(
        from_agent="sb", recipients=["agent-b"], body="Ack me",
    ))
    claimed = svc.claim_deliveries("agent-b")
    agent_b_delivery = [d for d in claimed[0].deliveries if d.recipient_agent == "agent-b"][0]
    svc.ack_delivery(agent_b_delivery.id, "agent-b", agent_b_delivery.claim_token)

    # After ack, inbox should be empty (acked != pending/claimed)
    inbox = svc.get_inbox("agent-b")
    assert len(inbox) == 0


# ── Inbox ──────────────────────────────────────────────────────────────────


def test_get_inbox(svc):
    svc.send_message(SendMessageInput(
        from_agent="sb", recipients=["agent-b"], body="Inbox test",
    ))
    inbox = svc.get_inbox("agent-b")
    assert len(inbox) == 1
    assert inbox[0].message.body == "Inbox test"

    # Other agent sees nothing
    other_inbox = svc.get_inbox("other")
    assert len(other_inbox) == 0


def test_inbox_excludes_acked(svc):
    svc.send_message(SendMessageInput(
        from_agent="sb", recipients=["agent-b"], body="Will ack",
    ))
    claimed = svc.claim_deliveries("agent-b")
    agent_b_delivery = [d for d in claimed[0].deliveries if d.recipient_agent == "agent-b"][0]
    svc.ack_delivery(agent_b_delivery.id, "agent-b", agent_b_delivery.claim_token)
    inbox = svc.get_inbox("agent-b")
    assert len(inbox) == 0


# ── Convoy Messages ───────────────────────────────────────────────────────


def test_get_convoy_messages(svc):
    # Parity: mailbox.ts:getConvoyMessages()
    from orchestration.convoy_service import ConvoyService
    from orchestration.models import CreateConvoyInput
    cs = ConvoyService(svc.db)
    c = cs.create_convoy(CreateConvoyInput(title="Convoy Msgs", created_by="sb"))

    svc.send_message(SendMessageInput(
        from_agent="sb", recipients=["agent-b"],
        body="First", convoy_id=c.convoy.id,
    ))
    svc.send_message(SendMessageInput(
        from_agent="agent-b", recipients=["sb"],
        body="Reply", convoy_id=c.convoy.id,
    ))

    msgs = svc.get_convoy_messages(c.convoy.id)
    assert len(msgs) == 2
    assert msgs[0].message.body == "First"
    assert msgs[1].message.body == "Reply"


# ── Format for Dispatch ───────────────────────────────────────────────────


def test_format_mail_for_dispatch(svc):
    # Parity: mailbox.ts:formatMailForDispatch()
    svc.send_message(SendMessageInput(
        from_agent="sb", recipients=["agent-b"],
        body="Important task", subject="Action Required",
        message_type="command",
    ))
    formatted = svc.format_mail_for_dispatch("agent-b")
    assert formatted is not None
    assert "## Unread Messages" in formatted
    assert "Important task" in formatted
    assert "Action Required" in formatted
    assert "**From**: sb" in formatted
    assert "**Type**: command" in formatted

    # After format, messages should be marked as seen (not in inbox as pending)
    # But get_inbox shows pending + claimed, not seen
    inbox = svc.get_inbox("agent-b")
    assert len(inbox) == 0  # seen is not pending/claimed


def test_ack_without_claim_raises(svc):
    svc.send_message(SendMessageInput(
        from_agent="sb", recipients=["agent-b"], body="No claim yet",
    ))
    with pytest.raises(ValueError, match="must be claimed"):
        svc.ack_delivery(1, "agent-b", "bogus")


def test_ack_requires_correct_recipient_and_token(svc):
    svc.send_message(SendMessageInput(
        from_agent="sb", recipients=["agent-b"], body="Claim me",
    ))
    claimed = svc.claim_deliveries("agent-b")
    agent_b_delivery = [d for d in claimed[0].deliveries if d.recipient_agent == "agent-b"][0]

    with pytest.raises(ValueError, match="not owned"):
        svc.ack_delivery(agent_b_delivery.id, "other", agent_b_delivery.claim_token)

    with pytest.raises(ValueError, match="claim token does not match"):
        svc.ack_delivery(agent_b_delivery.id, "agent-b", "wrong-token")


def test_format_mail_empty_returns_none(svc):
    formatted = svc.format_mail_for_dispatch("nobody")
    assert formatted is None


# ── Message Types ──────────────────────────────────────────────────────────


def test_all_message_types(svc):
    types = [
        "command", "approval_request", "clarification", "exception",
        "handoff", "interrupt", "cancel", "result", "status", "message",
    ]
    for mt in types:
        msg = svc.send_message(SendMessageInput(
            from_agent="sb", recipients=["agent-b"],
            body=f"Type: {mt}", message_type=mt,
        ))
        assert msg.message_type == mt
