"""Append-only audit log for webhook ingress deliveries (hermes-v18 Phase 4).

Every webhook POST that reaches a terminal verdict — accepted, rejected
(signature / missing-secret / rate-limit / body-cap / unknown-route /
disabled / event-filtered / read-error / parse), duplicate, delivered,
delivery_failed — writes ONE JSONL row here. Network ingress is a mutating surface; the audit trail
is what makes it default-deny-with-a-paper-trail like every other mutating
surface in the framework.

Cloned from ``skill_audit.py`` (Rule-1 call-time path via ``config.DATA_DIR``
attribute access inside the resolver, fail-open writes, secret redaction).
Deliberate divergence: there is NO ``Path(__file__)/../data`` fallback — that
pattern is an AST-audit violation (skill_audit.py:78 is a legacy carve-out);
if ``config`` cannot be imported the resolver raises and the outer fail-open
``try`` in :func:`append_webhook_audit_record` swallows it.

Fail-open: an audit-write failure NEVER raises into the handler (the security
decision already happened; the paper trail is best-effort).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

AUDIT_FILE_NAME = "webhook_actions.jsonl"

# Terminal verdicts a handler may record. Kept as a frozenset for tests and
# future consumers; unknown verdicts are still written (observability over
# strictness), this is documentation-as-code.
KNOWN_VERDICTS = frozenset({
    "accepted",
    "rejected_signature",
    "rejected_missing_secret",
    "rejected_rate_limit",
    "rejected_body_cap",
    "rejected_unknown_route",
    "rejected_disabled",
    "rejected_event_filtered",
    "rejected_read_error",
    "rejected_parse",
    "duplicate",
    "delivered",
    "delivery_failed",
})

# Secret-shaped tokens scrubbed out of any free-text field before it is
# stored. A ``reason`` string could otherwise echo a leaked credential (or a
# route's HMAC secret) into the log.
_SECRET_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"whsec_[A-Za-z0-9+/=]{10,}"),
    re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----"),
    re.compile(
        r"(?i)(api[_-]?key|token|secret|password|credential)\s*[=:]\s*"
        r"[\"']?[A-Za-z0-9+/=_\-]{12,}"
    ),
)

_REASON_MAX_LEN = 500


def _resolve_audit_path(path: Path | str | None = None) -> Path:
    """Resolve the audit-log path at CALL TIME (Rule 1).

    Explicit ``path`` wins (tests). Otherwise ``DATA_DIR/webhook_actions.jsonl``
    is resolved by importing ``config`` inside the body and reading
    ``config.DATA_DIR`` by attribute access — so a test's
    ``monkeypatch.setattr(config, "DATA_DIR", ...)`` and ``HOMIE_HOME``
    overrides take effect on the NEXT call with no module reload.
    """
    if path is not None:
        return Path(path)
    import config

    return Path(config.DATA_DIR) / AUDIT_FILE_NAME


def _redact(text: str) -> str:
    """Strip secret-shaped tokens from a free-text field and truncate it."""
    if not text:
        return ""
    scrubbed = text
    for pat in _SECRET_PATTERNS:
        scrubbed = pat.sub("[REDACTED]", scrubbed)
    if len(scrubbed) > _REASON_MAX_LEN:
        scrubbed = scrubbed[: _REASON_MAX_LEN - 3] + "..."
    return scrubbed


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def append_webhook_audit_record(
    route: str,
    delivery_id: str,
    verdict: str,
    *,
    event_type: str = "",
    deliver_target: str = "",
    reason: str = "",
    remote: str = "",
    session_id: str = "",
    path: Path | str | None = None,
) -> dict[str, Any] | None:
    """Append one sanitized webhook audit row and return it (or None on failure).

    Args:
        route: the webhook route name the event targeted.
        delivery_id: provider delivery id (X-GitHub-Delivery / svix-id / ...).
        verdict: terminal verdict — one of :data:`KNOWN_VERDICTS`.
        event_type: provider event type (pull_request / invoice.paid / ...).
        deliver_target: configured delivery target when relevant.
        reason: free-text rejection/error context (redacted + truncated).
        remote: best-effort caller address for rejected requests.
        session_id: agent-lane session chat id when one was created.
        path: explicit log-path override (tests); resolves DATA_DIR when None.

    Fail-open: returns the written record, or ``None`` if the write failed.
    NEVER raises into the caller — the request handler's verdict stands
    regardless (same posture as ``skill_audit`` / ``kill_switches``).
    """
    try:
        log_path = _resolve_audit_path(path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": _utc_timestamp(),
            "route": route,
            "delivery_id": delivery_id,
            "verdict": verdict,
            "event_type": event_type,
            "deliver_target": deliver_target,
            "reason": _redact(reason),
            "remote": remote,
            "session_id": session_id or None,
        }
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")
        return record
    except Exception as exc:  # noqa: BLE001 - audit is best-effort, never fatal
        logger.warning("webhook audit-write failed (%s/%s): %s", route, verdict, exc)
        return None


__all__ = (
    "AUDIT_FILE_NAME",
    "KNOWN_VERDICTS",
    "append_webhook_audit_record",
)
