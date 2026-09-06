"""Real receipt semantics with synthetic provider payloads and isolated stores."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from personas.learning import observers
from personas.learning.models import LearningTarget
from personas.learning.service import LearningService

NOW = "2026-09-06T12:00:00+00:00"
LATER = "2026-09-06T13:00:00+00:00"


@pytest.fixture
def learning(tmp_path):
    return LearningService(
        LearningTarget(
            "sales", tmp_path / "memory", tmp_path / "data", tmp_path / "state", tmp_path / "skills"
        )
    )


def mail(identifier, sender, stamp=NOW, *, sent=False, draft=False, thread="t1"):
    return {
        "id": identifier,
        "thread_id": thread,
        "sender": sender,
        "recipients": ["prospect@example.test"] if sent else ["operator@example.test"],
        "occurred_at": stamp,
        "sent": sent,
        "draft": draft,
    }


def observe(messages, **kwargs):
    return observers.observe_mail_response(
        provider="gmail",
        mailbox_id="business",
        outbound_id="m1",
        recipient_email="prospect@example.test",
        messages=messages,
        collected_at=LATER,
        deadline=NOW,
        **kwargs,
    )


def test_operator_followup_is_not_prospect_reply():
    result = observe(
        [
            mail("m1", "operator@example.test", sent=True),
            mail("m2", "operator@example.test", LATER, sent=True),
        ]
    )
    assert result["status"] == "no_reply"
    assert result["intervening_outbound_ids"] == ["m2"]
    assert result["causal_improvement"] is False


def test_inbound_matches_prospect_conversation_and_outbound_time():
    outgoing = mail("m1", "operator@example.test", sent=True)
    result = observe(
        [
            outgoing,
            mail("old", "prospect@example.test", "2026-09-06T11:00:00+00:00"),
            mail("other", "stranger@example.test", LATER),
            mail("thread", "prospect@example.test", LATER, thread="different"),
            mail("reply", "prospect@example.test", LATER),
        ]
    )
    assert result["status"] == "replied"
    assert [m["id"] for m in result["messages"]] == ["reply"]
    assert observe([outgoing], complete=False)["status"] == "pending"
    assert observe([mail("m1", "operator@example.test", draft=True)])["status"] == "not_sent"
    assert observe([])["status"] == "unavailable"


def test_gmail_uses_server_time_and_immutable_ids():
    payload = {
        "id": "t1",
        "messages": [
            {
                "id": "immutable",
                "internalDate": "1788696000000",
                "labelIds": ["SENT"],
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Operator <operator@example.test>"},
                        {"name": "To", "value": "Prospect <prospect@example.test>"},
                        {"name": "Date", "value": "invalid external header"},
                    ]
                },
            }
        ],
    }
    message = observers.gmail_messages(payload)[0]
    assert message["id"] == "immutable"
    assert datetime.fromisoformat(message["occurred_at"]).tzinfo is not None
    assert message["sent"] is True


def test_gmail_access_failure_is_not_no_reply(monkeypatch):
    from integrations import gmail

    def broken():
        raise PermissionError("private token details must not escape")

    monkeypatch.setattr(gmail, "get_gmail_service", broken)
    result = gmail.observe_inbound_response(
        thread_id="t1",
        outbound_id="m1",
        recipient_email="prospect@example.test",
        mailbox_id="business",
        collected_at=LATER,
    )
    assert result["status"] == "unavailable"
    assert result["reason"] == "PermissionError"
    assert "token" not in str(result)


def test_personal_gmail_verifies_account_before_reading_thread(monkeypatch):
    from integrations import personal_gmail

    paths = []

    def get(_session, path, **params):
        paths.append(path)
        return {"emailAddress": "wrong@example.test"}

    monkeypatch.setattr(personal_gmail, "_gmail_get", get)
    result = personal_gmail.observe_inbound_response(
        thread_id="t1",
        outbound_id="m1",
        recipient_email="prospect@example.test",
        mailbox_id="operator@example.test",
        collected_at=LATER,
        session=object(),
    )
    assert result["status"] == "unavailable"
    assert result["reason"] == "mailbox_identity_mismatch"
    assert paths == ["profile"]


def graph_message(identifier, sender, stamp=NOW):
    return {
        "id": identifier,
        "conversationId": "t1",
        "internetMessageId": f"<{identifier}>",
        "from": {"emailAddress": {"address": sender}},
        "isDraft": False,
        "toRecipients": [{"emailAddress": {"address": "prospect@example.test"}}],
        "sentDateTime": stamp,
        "receivedDateTime": stamp,
    }


def test_outlook_paginates_and_requests_immutable_ids():
    from integrations import outlook

    calls = []
    pages = iter(
        [
            {
                "value": [graph_message("m1", "operator@example.test")],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/users/x/messages?$skiptoken=next",
            },
            {"value": [graph_message("reply", "prospect@example.test", LATER)]},
        ]
    )

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: next(pages))

    result = outlook.observe_inbound_response(
        thread_id="t1",
        outbound_id="m1",
        recipient_email="prospect@example.test",
        mailbox_id="operator@example.test",
        collected_at=LATER,
        deadline=NOW,
        mailbox_email="operator@example.test",
        session=SimpleNamespace(get=get),
    )
    assert result["status"] == "replied"
    assert len(calls) == 2
    assert all(c[1]["headers"]["Prefer"] == 'IdType="ImmutableId"' for c in calls)
    assert calls[1][1]["params"] is None


def test_outlook_partial_scan_cannot_claim_no_reply():
    from integrations import outlook

    page = {
        "value": [graph_message("m1", "operator@example.test")],
        "@odata.nextLink": "https://graph.microsoft.com/v1.0/users/x/messages?next=1",
    }
    session = SimpleNamespace(
        get=lambda *a, **k: SimpleNamespace(raise_for_status=lambda: None, json=lambda: page)
    )
    result = outlook.observe_inbound_response(
        thread_id="t1",
        outbound_id="m1",
        recipient_email="prospect@example.test",
        mailbox_id="operator@example.test",
        collected_at=LATER,
        deadline=NOW,
        mailbox_email="operator@example.test",
        session=session,
        max_pages=1,
    )
    assert result["status"] == "pending"


def test_outbound_link_joins_prior_expectation_and_inbound_observation(learning, monkeypatch):
    from integrations import gmail

    exp = learning.capture_experience("offer-1", "sales", "Discuss pricing")
    expected = learning.commit_expectation(
        exp["id"],
        {
            "claim": "Prospect will reply",
            "check_by": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "domain": "sales",
            "resolution_rule": "A prospect response in the linked conversation",
            "situation": {"objection": "price"},
        },
    )
    with pytest.raises(ValueError, match="predates expectation"):
        observers.record_mail_outbound(
            learning,
            exp["id"],
            expected["id"],
            provider="gmail",
            mailbox_id="operator@example.test",
            outbound=mail(
                "historical", "operator@example.test", "2000-01-01T00:00:00+00:00", sent=True
            ),
            recipient_email="prospect@example.test",
        )
    observers.record_mail_outbound(
        learning,
        exp["id"],
        expected["id"],
        provider="gmail",
        mailbox_id="operator@example.test",
        outbound=mail(
            "m1",
            "operator@example.test",
            (datetime.fromisoformat(expected["created_at"]) + timedelta(seconds=1)).isoformat(),
            sent=True,
        ),
        recipient_email="prospect@example.test",
    )
    monkeypatch.setattr(
        gmail,
        "observe_inbound_response",
        lambda **kwargs: {"status": "replied", "messages": [{"id": "reply"}]},
    )
    result = asyncio.run(observers.collect_due_observation(learning, expected))
    assert result["expectation_id"] == expected["id"]
    assert result["metrics"] == {"reply_observed": True}
    assert "observer" not in learning.get_record(expected["id"])["situation"]


def test_study_produces_unevaluated_candidate_not_professional_success(learning):
    from curriculum.learning import complete_study, prepare_study

    video = {
        "video_id": "source1",
        "url": "https://example.test/source",
        "title": "Discovery questions",
    }
    prepared = prepare_study("sales", video, "digest", service=learning)
    result = complete_study(
        prepared,
        video=video,
        transcript_digest="digest",
        dossier_path="synthetic.md",
        study=SimpleNamespace(markdown="A sourced hypothesis", model="test-model"),
        proposals=[
            {
                "title": "Diagnose before discount",
                "body": "When price arrives before value, ask a discovery question.",
            }
        ],
    )
    assert result["status"] == "recorded"
    candidate = learning.store.all("candidate")[0]
    assert candidate["status"] == "proposed"
    assert candidate["changes_behavior"] is True
    assert not learning.store.all("activation")
    evidence = learning.store.all("observation")[0]["evidence"]
    assert evidence["professional_improvement"] is None
    assert evidence["source_claims_verified"] is False


def test_worktick_commits_actor_expectation_before_writing_deliverable(
    learning, monkeypatch, tmp_path
):
    import json

    from cofounder import worktick
    from personas.learning import hooks
    from personas.learning.models import LearningContext

    monkeypatch.setattr(hooks, "_service_for", lambda _: learning)
    monkeypatch.setattr(worktick, "build_draft_prompt", lambda *a: "Resolve the pricing objection")
    monkeypatch.setattr(
        learning,
        "render_context",
        lambda *a, **k: LearningContext(text="Ask a diagnostic question before discounting."),
    )
    expected = {
        "claim": "Prospect provides context",
        "check_by": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        "resolution_rule": "Explicit prospect reply",
        "situation": {"objection": "price"},
    }

    def model(prompt):
        assert "Ask a diagnostic question" in prompt
        return "# A pricing draft\n<<LEARNING_EXPECTATION:" + json.dumps(expected) + ">>"

    def write(*args):
        assert learning.store.all("expectation")[0]["phase"] == "pre_publication"
        assert "LEARNING_EXPECTATION" not in args[3]
        return tmp_path / "deliverable.md"

    monkeypatch.setattr(worktick, "_write_deliverable", write)
    result = worktick._execute_draft(
        "sales",
        "Handle price",
        {},
        "agenda1",
        model,
        datetime.now(UTC),
        learning_origin="assignment1",
    )
    assert result[0] == worktick.EXEC_DONE
    written = [e for e in learning.store.all("execution") if e.get("stage") == "artifact_written"]
    assert written[0]["customer_message_sent"] is False


def test_worktick_code_handoff_delivers_context_and_does_not_claim_completion(
    learning, monkeypatch, tmp_path
):
    from cofounder import repos, worktick
    from personas.learning import hooks
    from personas.learning.models import LearningContext

    monkeypatch.setattr(hooks, "_service_for", lambda _: learning)
    monkeypatch.setattr(repos, "resolve_repo", lambda _: SimpleNamespace(local_path=tmp_path))
    monkeypatch.setattr(
        learning,
        "render_context",
        lambda *a, **k: LearningContext(text="Reproduce the failure before editing."),
    )

    def dispatch(workflow, branch, message, path, slug):
        assert "Reproduce the failure before editing." in message
        assert any(e.get("stage") == "dispatch_started" for e in learning.store.all("execution"))
        return "run-synthetic"

    result = worktick._execute_code(
        "sales",
        "Fix report",
        "sample",
        "agenda2",
        SimpleNamespace(code_workflow="synthetic"),
        dispatch,
        datetime.now(UTC),
        learning_origin="assignment2",
    )
    assert result[0] == worktick.EXEC_DISPATCHED
    receipt = [e for e in learning.store.all("execution") if e.get("stage") == "dispatched"][0]
    assert receipt["run_id"] == "run-synthetic"
    assert receipt["work_completed"] is False
