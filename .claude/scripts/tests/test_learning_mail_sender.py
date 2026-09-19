"""Real proposal -> one authorized fake HTTP send -> sent/read/reply linkage."""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from integrations import outlook
from personas import action_proposals
from personas.learning import hooks, observers
from personas.learning import service as learning_service
from runtime import persona_tools, tool_impl_learning, tool_impl_mail_write, tool_registry

MAILBOX = "operator@example.test"
TO = "prospect@example.test"
FIELDS = {"to_email": TO, "subject": "Price question", "body": "What outcome matters most?"}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    from personas import core, experience

    registry, executors = dict(tool_registry._REGISTRY), dict(action_proposals._EXECUTORS)
    tool_registry._REGISTRY.clear()
    action_proposals._EXECUTORS.clear()
    monkeypatch.setattr(
        core,
        "get_persona_paths",
        lambda p: {
            "data": tmp_path / p / "data",
            "memory": tmp_path / p / "memory",
        },
    )
    monkeypatch.setattr(experience, "_reindex_note", lambda *a, **k: None)
    monkeypatch.setattr(outlook, "GRAPH_USER_EMAIL", MAILBOX)
    monkeypatch.setattr(outlook, "_get_access_token", lambda: "fake-only")
    for name in (
        "HOMIE_KILLSWITCH_PERSONA_ACTION_PROPOSALS",
        "HOMIE_KILLSWITCH_PERSONA_TOOLS",
        "PERSONA_LEARNING_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    tool_impl_mail_write.register_tools()
    tool_impl_learning.register_tools()
    service = learning_service.get_learning_service("sales")
    experience = service.capture_experience("prospect-offer", "chat", "Answer price objection")
    request = SimpleNamespace(metadata={"learning": {}}, system_prompt="")
    turn = hooks.SurfaceTurn(
        request,
        "sales",
        "chat",
        "prospect-offer",
        "attempt-1",
        service=service,
        experience=experience,
    )
    state = {
        "posts": [],
        "gets": [],
        "sent": [],
        "empty": False,
        "ambiguous": False,
        "timeout": False,
        "body_drift": False,
    }

    def response(payload, content=b"json"):
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload, content=content)

    def post(url, *, json, **kwargs):
        assert url.endswith("/sendMail"), "no real auth/network allowed"
        state["posts"].append(copy.deepcopy(json))
        msg = json["message"]
        state["sent"] = [
            {
                "id": "immutable-message-1",
                "conversationId": "physical-thread-1",
                "internetMessageId": "<physical-1@example.test>",
                "internetMessageHeaders": msg.get("internetMessageHeaders", []),
                "from": {"emailAddress": {"address": MAILBOX}},
                "toRecipients": msg["toRecipients"],
                "ccRecipients": [],
                "bccRecipients": [],
                "subject": msg["subject"],
                "body": msg["body"],
                "isDraft": False,
                "sentDateTime": datetime.now(UTC).isoformat(),
            }
        ]
        if state["timeout"]:
            raise TimeoutError("provider acceptance unknown")
        return response({}, b"")

    def get(url, **kwargs):
        assert "graph.microsoft.com" in url
        state["gets"].append((url, kwargs))
        if "/sentitems/messages" in url:
            if state["empty"]:
                return response({"value": []})
            rows = copy.deepcopy(state["sent"])
            if state["ambiguous"]:
                rows.append({**rows[0], "id": "another-message"})
            if state["body_drift"]:
                rows[0]["body"]["content"] = "Different operator email"
            return response({"value": rows})
        reply = {
            "id": "physical-reply-1",
            "conversationId": "physical-thread-1",
            "from": {"emailAddress": {"address": TO}},
            "toRecipients": [{"emailAddress": {"address": MAILBOX}}],
            "isDraft": False,
            "receivedDateTime": (
                datetime.fromisoformat(state["sent"][0]["sentDateTime"]) + timedelta(microseconds=1)
            ).isoformat(),
            "bodyPreview": "The outcome is fewer missed appointments.",
        }
        return response({"value": state["sent"] + [reply]})

    monkeypatch.setattr(outlook.requests, "post", post)
    monkeypatch.setattr(outlook.requests, "get", get)
    yield SimpleNamespace(service=service, turn=turn, state=state)
    tool_registry._REGISTRY.clear()
    tool_registry._REGISTRY.update(registry)
    action_proposals._EXECUTORS.clear()
    action_proposals._EXECUTORS.update(executors)


def propose(harness, *, learning=True, fields=None):
    payload = persona_tools.build_persona_tool_payload(
        "sales", {"toolsets": ["mail_write", "cognitive_learning"]}, learning_capture=True
    )
    assert payload is not None
    dispatch = harness.turn.wrap_dispatch(payload[1])
    if learning:
        dispatch(
            "record_expectation",
            {
                "domain": "sales",
                "claim": "The prospect will reply with their desired outcome",
                "check_by": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                "resolution_rule": "A reply from the named prospect in the sent conversation",
                "situation": {"objection": "price"},
            },
        )
    card = dispatch(tool_impl_mail_write.TOOL, fields or FIELDS)
    assert "Exact email content:" in card, card
    assert FIELDS["body"] in card
    return action_proposals.list_pending("sales")[0]


def approve(proposal):
    return action_proposals.decide_action(
        "sales",
        proposal.action_id,
        True,
        user_role="admin",
        source="interactive",
        actor="operator",
        surface="cli",
    )


def test_real_registered_sender_survives_approval_callback_and_links_reply(harness):
    proposal = propose(harness)
    assert not harness.state["posts"]
    assert hooks.current_action_learning_context(persona_id="sales") is None
    # The original turn is gone; a new service instance sees the persisted IDs.
    fresh = learning_service.get_learning_service("sales")
    stored = action_proposals.get_action("sales", proposal.action_id)
    expected_id = stored.arguments["_learning_context"]["expectation_id"]
    assert fresh.get_record(expected_id)["phase"] == "pre_action"
    result = approve(stored)
    assert result.outcome == action_proposals.DECISION_EXECUTED
    assert result.result["status"] == "accepted" and not result.result["delivery_verified"]
    assert len(harness.state["posts"]) == 1
    executed = fresh.store.all("execution")
    link = next(e for e in executed if e.get("stage") == "outbound_observed")
    assert link["expectation_id"] == expected_id
    assert link["observer"]["outbound_id"] == "immutable-message-1"
    assert link["observer"]["thread_id"] == "physical-thread-1"
    receipt = asyncio.run(observers.collect_due_observation(fresh, fresh.get_record(expected_id)))
    assert receipt["evidence"]["status"] == "replied"
    assert receipt["evidence"]["messages"][0]["id"] == "physical-reply-1"
    approve(stored)
    assert len(harness.state["posts"]) == 1, "double approval must not resend"
    assert all('IdType="ImmutableId"' in k["headers"]["Prefer"] for _, k in harness.state["gets"])


@pytest.mark.parametrize("condition", ["empty", "ambiguous", "body_drift"])
def test_unknown_sent_link_retries_reads_only(harness, condition):
    proposal = propose(harness)
    harness.state[condition] = True
    approve(proposal)
    expected = harness.service.get_record(proposal.arguments["_learning_context"]["expectation_id"])
    initial = asyncio.run(observers.collect_due_observation(harness.service, expected))
    assert initial["status"] == "partial"
    assert "held" not in initial and "metrics" not in initial
    assert not any(
        e.get("stage") == "outbound_observed" for e in harness.service.store.all("execution")
    )
    harness.state[condition] = False
    later = asyncio.run(observers.collect_due_observation(harness.service, expected))
    assert later["evidence"]["status"] == "replied"
    assert len(harness.state["posts"]) == 1


def test_timeout_keeps_unknown_intent_and_consumes_send_authority(harness):
    proposal = propose(harness)
    harness.state["timeout"] = True
    result = approve(proposal)
    assert result.result["status"] == "unknown" and not result.result["retry_send"]
    assert len(harness.state["posts"]) == 1
    assert not any(
        e.get("stage") == "outbound_observed" for e in harness.service.store.all("execution")
    )
    approve(proposal)
    assert len(harness.state["posts"]) == 1


def test_mailbox_drift_and_invalid_token_refuse_before_http(harness, monkeypatch):
    proposal = propose(harness)
    rejected = tool_impl_mail_write.execute_email(
        persona_id="sales",
        action_id=proposal.action_id,
        execution_token="forged",
        arguments=proposal.arguments,
    )
    assert rejected["reason"] == "invalid_execution_token"
    monkeypatch.setattr(outlook, "GRAPH_USER_EMAIL", "different@example.test")
    result = approve(proposal)
    assert result.result["reason"] == "mailbox_changed"
    assert not harness.state["posts"]


def test_expired_expectation_does_not_grade_delayed_approval_as_sales_failure(harness, monkeypatch):
    proposal = propose(harness)

    class LaterClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(UTC) + timedelta(days=2)

    monkeypatch.setattr(observers, "datetime", LaterClock)
    assert approve(proposal).result["status"] == "accepted"
    assert len(harness.state["posts"]) == 1
    observed = harness.service.store.all("observation")
    assert len(observed) == 1
    assert observed[0]["status"] == "unresolvable"
    assert observed[0]["evidence"]["reason"] == "expectation_expired_before_send"
    assert "held" not in observed[0]
    assert not harness.state["gets"]


def test_no_context_uses_original_sender_api_without_attribution(harness):
    assert outlook.send_email(TO, "Legacy caller", "Ordinary body") is True
    assert len(harness.state["posts"]) == 1
    assert "internetMessageHeaders" not in harness.state["posts"][0]["message"]
    assert not harness.state["gets"]
    assert not harness.service.store.all("execution")


def test_storage_failure_does_not_turn_authorized_send_into_failure(harness, monkeypatch):
    proposal = propose(harness)
    monkeypatch.setattr(
        observers, "begin_mail_send", lambda *a, **k: (_ for _ in ()).throw(OSError())
    )
    assert approve(proposal).result["status"] == "accepted"
    assert len(harness.state["posts"]) == 1
    assert not harness.state["gets"]


def test_model_cannot_supply_other_persona_learning_ids(harness):
    foreign = {"persona_id": "crypto", "experience_id": "foreign", "expectation_id": "foreign"}
    proposal = propose(harness, fields={**FIELDS, "_learning_context": foreign})
    assert proposal.arguments["_learning_context"]["persona_id"] == "sales"
    assert proposal.arguments["_learning_context"]["expectation_id"] != "foreign"
    approve(proposal)
    assert not learning_service.get_learning_service("crypto").store.all("execution")


def test_outbound_link_rejects_other_mailbox(harness):
    proposal = propose(harness)
    approve(proposal)
    link = next(
        e for e in harness.service.store.all("execution") if e.get("stage") == "outbound_observed"
    )
    with pytest.raises(ValueError, match="mailbox identity"):
        observers.record_mail_outbound(
            harness.service,
            link["experience_id"],
            link["expectation_id"],
            provider="outlook",
            mailbox_id="different@example.test",
            outbound=link["evidence"],
            recipient_email=TO,
        )


def test_new_tool_is_not_implicitly_granted(harness):
    payload = persona_tools.build_persona_tool_payload(
        "sales", {"toolsets": ["cognitive_learning"]}
    )
    assert payload is not None
    assert all(item["function"]["name"] != tool_impl_mail_write.TOOL for item in payload[0])
