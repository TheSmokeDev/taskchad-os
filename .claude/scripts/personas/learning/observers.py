"""Pure domain evidence adapters. No clients, credentials, or private imports.

Provider payloads are observations, never instructions. A successful tool receipt
is distinct from a customer's response and from proof of improved performance.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses, parseaddr
from typing import Any


def evidence_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _time(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("observation timestamps require an explicit timezone")
    return stamp.astimezone(UTC)


def unavailable_observation(provider: str, reason: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "status": "unavailable",
        "reason": reason,
        "complete": False,
        "messages": [],
        "evidence_kind": "direct",
    }


def gmail_messages(thread: dict[str, Any]) -> list[dict[str, Any]]:
    """Use Gmail immutable IDs and server internalDate, never a guessed date."""
    result = []
    for item in thread.get("messages", []):
        headers = {
            str(h["name"]).lower(): h["value"] for h in item.get("payload", {}).get("headers", [])
        }
        labels = item.get("labelIds", [])
        if not parseaddr(headers.get("from", ""))[1]:
            raise ValueError("message lacks a sender identity")
        result.append(
            {
                "id": str(item["id"]),
                "thread_id": str(item.get("threadId") or thread["id"]),
                "internet_message_id": headers.get("message-id", ""),
                "sender": parseaddr(headers.get("from", ""))[1].lower(),
                "recipients": [
                    a.lower()
                    for _, a in getaddresses([headers.get("to", ""), headers.get("cc", "")])
                ],
                "occurred_at": datetime.fromtimestamp(
                    int(item["internalDate"]) / 1000, UTC
                ).isoformat(),
                "sent": "SENT" in labels,
                "draft": "DRAFT" in labels,
                "in_reply_to": headers.get("in-reply-to", ""),
                "snippet": str(item.get("snippet") or "")[:2000],
            }
        )
    return result


def outlook_messages(messages: list[dict[str, Any]], mailbox_email: str) -> list[dict[str, Any]]:
    """Caller MUST request Graph Prefer: IdType=\"ImmutableId\" on every page."""
    result = []
    for item in messages:
        sender = str(item.get("from", {}).get("emailAddress", {}).get("address") or "").lower()
        if not sender:
            raise ValueError("message lacks a sender identity")
        sent = sender == mailbox_email.lower()
        timestamp = item.get("sentDateTime" if sent else "receivedDateTime")
        result.append(
            {
                "id": str(item["id"]),
                "thread_id": str(item["conversationId"]),
                "internet_message_id": str(item.get("internetMessageId") or ""),
                "sender": sender,
                "recipients": [
                    str(r["emailAddress"]["address"]).lower()
                    for r in [*item.get("toRecipients", []), *item.get("ccRecipients", [])]
                ],
                "occurred_at": _time(str(timestamp or "")).isoformat(),
                "sent": sent,
                "draft": bool(item.get("isDraft", False)),
                "snippet": str(item.get("bodyPreview") or "")[:2000],
            }
        )
    return result


def observe_mail_response(
    *,
    provider: str,
    mailbox_id: str,
    outbound_id: str,
    recipient_email: str,
    messages: list[dict[str, Any]],
    collected_at: str,
    deadline: str | None = None,
    complete: bool = True,
) -> dict[str, Any]:
    """Observe the named prospect in a verified outbound conversation.

    no_reply means no observed reply through a completed observation window. It
    never means rejection, lack of interest, or that a procedure caused failure.
    Multiple operator messages are retained as potential intervening actions.
    """
    collected = _time(collected_at)
    if not mailbox_id or not outbound_id or not recipient_email:
        raise ValueError("mailbox, outbound message, and prospect identity are required")
    outbound = next((m for m in messages if m["id"] == outbound_id), None)
    if outbound is None:
        return unavailable_observation(provider, "outbound_message_not_observed")
    if outbound.get("draft") or not outbound.get("sent"):
        return {
            "provider": provider,
            "status": "not_sent",
            "complete": complete,
            "messages": [],
            "outbound": outbound,
            "evidence_kind": "direct",
        }
    recipient = recipient_email.lower()
    if recipient not in outbound.get("recipients", []):
        return unavailable_observation(provider, "outbound_recipient_mismatch")
    sent_at = _time(outbound["occurred_at"])
    if collected < sent_at:
        raise ValueError("collection precedes outbound event")
    later = [
        m
        for m in messages
        if m["thread_id"] == outbound["thread_id"]
        and sent_at < _time(m["occurred_at"]) <= collected
        and not m.get("draft")
    ]
    replies = sorted(
        [m for m in later if not m.get("sent") and m["sender"] == recipient],
        key=lambda m: (m["occurred_at"], m["id"]),
    )
    deadline_passed = deadline is not None and collected >= _time(deadline)
    status = "replied" if replies else "no_reply" if complete and deadline_passed else "pending"
    payload = {
        "provider": provider,
        "mailbox_id": mailbox_id,
        "status": status,
        "outbound": outbound,
        "messages": replies,
        "complete": complete,
        "collected_at": collected.isoformat(),
        "deadline": deadline,
        "intervening_outbound_ids": sorted(m["id"] for m in later if m.get("sent")),
        "evidence_kind": "direct",
        "causal_improvement": False,
    }
    payload["observation_id"] = f"{provider}:{mailbox_id}:{outbound_id}:{evidence_hash(payload)}"
    return payload


def execution_observation(
    *, source_id: str, status: str, receipt: dict[str, Any], occurred_at: str
) -> dict[str, Any]:
    _time(occurred_at)
    return {
        "source_id": source_id,
        "status": status,
        "receipt": receipt,
        "occurred_at": occurred_at,
        "evidence_kind": "direct",
        "measure": "execution",
        "domain_outcome": None,
    }


def feedback_observation(
    *,
    source_id: str,
    feedback: str,
    actor_id: str,
    occurred_at: str,
    corrected_observation_id: str | None = None,
) -> dict[str, Any]:
    """Explicit feedback preserves attribution; it does not become objective fact."""
    _time(occurred_at)
    if not source_id or not actor_id or not feedback.strip():
        raise ValueError("feedback requires an identified source, actor, and text")
    return {
        "source_id": source_id,
        "actor_id": actor_id,
        "feedback": feedback,
        "occurred_at": occurred_at,
        "evidence_kind": "direct",
        "measure": "reported_feedback",
        "corrects": corrected_observation_id,
    }


def study_observation(
    *,
    source_id: str,
    source_url: str,
    transcript_digest: str,
    dossier_path: str,
    validation_errors: list[str],
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "source_url": source_url,
        "transcript_digest": transcript_digest,
        "dossier_path": dossier_path,
        "validation_errors": validation_errors,
        "evidence_kind": "direct",
        "measure": "study_artifact",
        "experience_kind": "study",
        "source_claims_verified": False,
        "professional_improvement": None,
    }


async def collect_due_observation(service: Any, expectation: dict[str, Any]) -> dict[str, Any]:
    """Collect one due observation from an explicitly linked domain source.

    Only preconfigured read integrations may be selected. Arbitrary imports,
    external URLs, and model-written callables are not observer capabilities.
    mailbox_id is the exact provider account email, verified by the read adapter.
    """
    import asyncio

    observer = expectation.get("situation", {}).get("observer", {})
    # A provider assigns immutable IDs after sending. The original expectation
    # stays immutable; a verified outbound execution supplies that later linkage.
    executions = service.store.all("execution")
    for execution in executions:
        if (
            execution.get("experience_id") == expectation.get("experience_id")
            and execution.get("expectation_id") == expectation["id"]
            and execution.get("stage") == "outbound_observed"
        ):
            observer = execution["observer"]
    # sendMail returns 202 without IDs. A durable pre-send intent lets the
    # scheduler retry only the read, including after the sender process exits.
    if not observer.get("outbound_id"):
        for execution in executions:
            if (
                execution.get("experience_id") == expectation.get("experience_id")
                and execution.get("expectation_id") == expectation["id"]
                and execution.get("stage") == "mail_send_started"
            ):
                linked = await asyncio.to_thread(resolve_mail_outbound, service, execution)
                if linked.get("observer"):
                    observer = linked["observer"]
                else:
                    return {
                        "status": "partial",
                        "quality": "direct",
                        "evidence": linked,
                        "expectation_id": expectation["id"],
                    }
    provider = observer.get("provider")
    if expectation.get("domain") != "sales" or provider not in {
        "gmail",
        "personal_gmail",
        "outlook",
    }:
        return {
            "status": "unresolvable",
            "quality": "direct",
            "evidence": {"reason": "no_linked_observer", "domain": expectation.get("domain")},
            "expectation_id": expectation["id"],
        }
    if provider == "gmail":
        from integrations.gmail import observe_inbound_response
    elif provider == "personal_gmail":
        from integrations.personal_gmail import observe_inbound_response
    else:
        from integrations.outlook import observe_inbound_response
    collected_at = datetime.now(UTC).isoformat()
    required = ("thread_id", "outbound_id", "recipient_email", "mailbox_id")
    if any(not observer.get(key) for key in required):
        payload = unavailable_observation(str(provider), "incomplete_observer_link")
    else:
        payload = await asyncio.to_thread(
            observe_inbound_response,
            **{key: str(observer[key]) for key in required},
            collected_at=collected_at,
            deadline=expectation.get("check_by"),
        )
    observed = payload["status"]
    status = (
        "resolved"
        if observed in {"replied", "no_reply"}
        else "partial"
        if observed == "unavailable"
        else "open"
    )
    return {
        "status": status,
        "quality": "direct",
        "evidence": payload,
        "occurred_at": collected_at,
        "expectation_id": expectation["id"],
        "metrics": {"reply_observed": observed == "replied"} if status == "resolved" else {},
    }


def record_mail_outbound(
    service: Any,
    experience_id: str,
    expectation_id: str,
    *,
    provider: str,
    mailbox_id: str,
    outbound: dict[str, Any],
    recipient_email: str,
) -> dict[str, Any]:
    """Link a physical sent artifact to a prior expectation without rewriting it.

    Call this with normalized provider evidence after retrieving the sent item;
    a sendMail acceptance boolean is not sufficient to call this operation.
    """
    expectation = service.get_record(expectation_id)
    if not expectation or expectation.get("experience_id") != experience_id:
        raise ValueError("outbound expectation must belong to the experience")
    if provider not in {"gmail", "personal_gmail", "outlook"}:
        raise ValueError("unsupported mail observer")
    if not outbound.get("id") or not outbound.get("thread_id") or not mailbox_id:
        raise ValueError("physical provider message and conversation IDs required")
    if not outbound.get("sent") or outbound.get("draft"):
        raise ValueError("outbound message has not been observed sent")
    if str(outbound.get("sender", "")).casefold() != mailbox_id.casefold():
        raise ValueError("outbound mailbox identity mismatch")
    if recipient_email.lower() not in outbound.get("recipients", []):
        raise ValueError("outbound prospect mismatch")
    sent_at = _time(outbound["occurred_at"])
    if expectation.get("phase") != "retrospective" and sent_at < _time(expectation["created_at"]):
        # Only the verified host marker can account for Graph's seconds-only
        # timestamp. Preserve both instants; never rewrite the provider time.
        host_start = outbound.get("host_send_started_at")
        if (
            not outbound.get("send_id")
            or not host_start
            or _time(host_start) < _time(expectation["created_at"])
            or sent_at != _time(host_start).replace(microsecond=0)
        ):
            raise ValueError("outbound predates expectation; use historical evidence instead")
    observer = {
        "provider": provider,
        "mailbox_id": mailbox_id,
        "thread_id": outbound["thread_id"],
        "outbound_id": outbound["id"],
        "recipient_email": recipient_email.lower(),
    }
    return service.record_execution(
        experience_id,
        {
            "stage": "outbound_observed",
            "expectation_id": expectation_id,
            "observer": observer,
            "evidence": outbound,
        },
        attempt_key=f"outbound:{provider}:{mailbox_id}:{outbound['id']}",
    )


def mail_content_hash(to_email: str, subject: str, body: str, content_type: str) -> str:
    """Exact approved content, allowing only transport newline normalization."""
    return evidence_hash(
        {
            "to": to_email.casefold(),
            "subject": subject,
            "body": body.replace("\r\n", "\n"),
            "content_type": content_type.casefold(),
        }
    )


def begin_mail_send(
    context: dict[str, str],
    *,
    mailbox_id: str,
    to_email: str,
    subject: str,
    body: str,
    content_type: str,
    send_id: str,
) -> tuple[Any, dict[str, Any]]:
    """Persist host-attributed intent before a single authorized provider send.

    This operation grants no sending authority. The actual send owner performs
    its existing capability/approval checks before calling it. No ambient
    profile or message text is used to choose a persona or expectation.
    """
    from personas.learning import service as learning_service

    required = ("persona_id", "experience_id", "expectation_id")
    if any(not isinstance(context.get(k), str) or not context[k] for k in required):
        raise ValueError("mail learning requires explicit host attribution")
    service = learning_service.get_learning_service(context["persona_id"])
    if not service.enabled():
        raise ValueError("persona learning is disabled")
    expected = service.get_record(context["expectation_id"])
    if (
        not expected
        or expected.get("kind") != "expectation"
        or expected.get("experience_id") != context["experience_id"]
        or expected.get("domain") != "sales"
        or expected.get("phase") != "pre_action"
        or not mailbox_id
        or not to_email
    ):
        raise ValueError("mail requires an owned prior sales expectation")
    started = datetime.now(UTC)
    if _time(expected["check_by"]) <= started:
        # A late approval cannot turn an already elapsed response window into
        # a failed sales prediction. The authorized send can still proceed.
        service.record_observation(
            context["experience_id"],
            {
                "expectation_id": expected["id"],
                "status": "unresolvable",
                "quality": "direct",
                "evidence": {"reason": "expectation_expired_before_send"},
            },
            source_key=f"mail:{send_id}:expired",
        )
        raise ValueError("sales expectation expired before authorized send")
    intent = service.record_execution(
        context["experience_id"],
        {
            "stage": "mail_send_started",
            "expectation_id": expected["id"],
            "provider": "outlook",
            "mailbox_id": mailbox_id,
            "recipient_email": to_email.casefold(),
            "send_id": send_id,
            "content_hash": mail_content_hash(to_email, subject, body, content_type),
            "content_type": content_type.casefold(),
            "started_at": started.isoformat(),
            "send_not_after": (started + timedelta(minutes=15)).isoformat(),
            "status": "unknown",
            "host_observed": True,
        },
        attempt_key=f"mail:{send_id}:started",
    )
    return service, intent


def resolve_mail_outbound(service: Any, intent: dict[str, Any]) -> dict[str, Any]:
    """Resolve an immutable send intent with a read; this never sends mail."""
    from integrations import outlook

    result = outlook.find_sent_learning_message(intent)
    if result.get("status") != "sent_observed":
        return result
    return record_mail_outbound(
        service,
        intent["experience_id"],
        intent["expectation_id"],
        provider="outlook",
        mailbox_id=intent["mailbox_id"],
        recipient_email=intent["recipient_email"],
        outbound=result["outbound"],
    )
