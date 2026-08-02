"""Redacted, idempotent projection of Homie work state into Buzz chat."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from buzz_config import buzz_state_path, get_buzz_settings
from buzz_state import BuzzStateStore

RECEIPT_TYPES = frozenset(
    {"work.started", "work.approval_required", "work.completed", "work.failed"}
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|private[_-]?key|password|secret)\b\s*[:=]\s*\S+"
)
_SECRET_TOKEN = re.compile(r"(?i)\b(nsec1[0-9a-z]+|sk-[a-z0-9_-]{16,})\b")


def redact_summary(value: str, *, max_chars: int = 240) -> str:
    text = " ".join(str(value).split())
    text = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    text = _SECRET_TOKEN.sub("[redacted]", text)
    return text[:max_chars]


def safe_dashboard_path(value: str) -> str:
    path = str(value).strip()
    if not path.startswith("/") or ".." in path or "?" in path or "#" in path:
        raise ValueError("dashboard_path must be a local absolute path without query data")
    if not re.fullmatch(r"/[A-Za-z0-9/_-]*", path):
        raise ValueError("dashboard_path contains unsupported characters")
    return path[:200]


def enqueue_work_receipt(
    receipt_type: str,
    *,
    work_id: str,
    work_type: str,
    summary: str,
    status: str,
    dashboard_path: str,
    idempotency_key: str,
    state_path: Path | None = None,
    profile: str | None = None,
    timestamp: str | None = None,
) -> bool:
    """Queue one bounded receipt; never publishes prompts, tools, or audit data."""
    if receipt_type not in RECEIPT_TYPES:
        raise ValueError(f"unsupported Buzz receipt type: {receipt_type}")
    if not get_buzz_settings().signal_channel and state_path is None:
        return False
    if profile is None:
        from personas import get_active_profile_name

        profile = get_active_profile_name()
    payload = {
        "receipt_type": receipt_type,
        "work_id": redact_summary(work_id, max_chars=128),
        "work_type": redact_summary(work_type, max_chars=48),
        "profile": redact_summary(profile, max_chars=64),
        "summary": redact_summary(summary),
        "status": redact_summary(status, max_chars=32),
        "timestamp": timestamp or datetime.now(UTC).isoformat(),
        "dashboard_path": safe_dashboard_path(dashboard_path),
    }
    store = BuzzStateStore(state_path or buzz_state_path())
    return store.enqueue_receipt(redact_summary(idempotency_key, max_chars=180), payload)


def render_work_receipt(payload: dict[str, str]) -> str:
    return (
        f"{payload['receipt_type']} · {payload['work_type']} {payload['work_id']}\n"
        f"{payload['summary']}\n"
        f"Profile: {payload['profile']} · Status: {payload['status']} · {payload['timestamp']}\n"
        f"Open in Homie Dashboard: {payload['dashboard_path']}"
    )
