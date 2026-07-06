"""Append-only audit log for browser workflow attempts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from browser_control import redact_text_urls, redact_url

try:
    from config import DATA_DIR
except Exception:  # pragma: no cover - import path fallback for direct scripts
    from personas import get_default_paths

    DATA_DIR = get_default_paths()["data"]


BROWSER_AUDIT_LOG = DATA_DIR / "browser_actions.jsonl"
KNOWN_SURFACES = {
    "cli",
    "telegram",
    "discord",
    "slack",
    "mission_control",
    "whatsapp",
    "unknown",
}


def append_browser_audit_record(
    *,
    command: str,
    workflow_id: str | None,
    outcome: str,
    reason: str = "",
    cdp_port: int | None = None,
    cdp_reachable: bool | None = None,
    surface: str = "unknown",
    session_id: str | None = None,
    target_url: str | None = None,
    action: str | None = None,
    subtask_id: int | None = None,
    executor_name: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Append one sanitized browser audit record and return the record.

    `subtask_id` / `executor_name` are additive (default None) so the executor
    boundary can stamp social-write attempts without breaking existing callers.
    """

    log_path = path or BROWSER_AUDIT_LOG
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": _utc_timestamp(),
        "command": redact_text_urls(command),
        "workflow_id": workflow_id,
        "action": action,
        "outcome": outcome,
        "reason": redact_text_urls(reason),
        "cdp_port": cdp_port,
        "cdp_reachable": cdp_reachable,
        "surface": normalize_surface(surface),
        "session_id": session_id,
        "target_url": redact_url(target_url) if target_url else None,
        "subtask_id": subtask_id,
        "executor_name": executor_name,
    }
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")
    return record


def normalize_surface(surface: str | None) -> str:
    value = (surface or "unknown").strip().lower()
    if value == "web":
        value = "mission_control"
    return value if value in KNOWN_SURFACES else "unknown"


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
