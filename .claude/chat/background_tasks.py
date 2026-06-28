"""Minimal durable status ledger for chat turns that continue after timeout."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any


BACKGROUND_TASK_STATE_FILE: Path | None = None
_STATUS_PROBE_RE = re.compile(
    r"\b(still cooking|how we looking|how are we looking|any update|"
    r"status|done yet|finish(?:ed)? yet|still running)\b",
    re.I,
)


def _state_path() -> Path:
    if BACKGROUND_TASK_STATE_FILE is not None:
        return BACKGROUND_TASK_STATE_FILE
    override = os.getenv("HOMIE_BACKGROUND_TASK_STATE_FILE")
    if override:
        return Path(override)
    try:
        from config import STATE_DIR

        return Path(STATE_DIR) / "background-engine-tasks.json"
    except Exception:
        return Path(".claude") / "data" / "state" / "background-engine-tasks.json"


def _now() -> float:
    return time.time()


def _load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {"tasks": {}, "latest_by_session": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"tasks": {}, "latest_by_session": {}}
    if not isinstance(state, dict):
        return {"tasks": {}, "latest_by_session": {}}
    state.setdefault("tasks", {})
    state.setdefault("latest_by_session", {})
    return state


def _save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def is_status_probe(text: str) -> bool:
    """Return True for short follow-up pings about a timed-out task."""

    stripped = " ".join((text or "").strip().split())
    if not stripped or len(stripped) > 120:
        return False
    if stripped.startswith("/"):
        return False
    return bool(_STATUS_PROBE_RE.search(stripped))


def start_task(
    *,
    session_key: str,
    platform: str,
    channel_id: str,
    thread_id: str,
    message_id: str | None,
    user_request: str,
) -> str:
    """Persist a running background task and mark it latest for this session."""

    state = _load_state()
    started_at = _now()
    safe_message_id = message_id or str(int(started_at * 1000))
    task_id = f"{session_key}:{safe_message_id}"
    record = {
        "task_id": task_id,
        "session_key": session_key,
        "platform": platform,
        "channel_id": channel_id,
        "thread_id": thread_id,
        "message_id": message_id or "",
        "user_request": (user_request or "").strip()[:2000],
        "status": "running",
        "started_at": started_at,
        "updated_at": started_at,
        "final_message_id": "",
        "final_text": "",
        "error": "",
    }
    state["tasks"][task_id] = record
    state["latest_by_session"][session_key] = task_id
    _save_state(state)
    return task_id


def update_task(
    task_id: str | None,
    *,
    status: str,
    final_message_id: str | None = None,
    final_text: str | None = None,
    error: str | None = None,
) -> None:
    """Update a persisted background task status. Missing task ids fail open."""

    if not task_id:
        return
    state = _load_state()
    record = state.get("tasks", {}).get(task_id)
    if not isinstance(record, dict):
        return
    record["status"] = status
    record["updated_at"] = _now()
    if final_message_id is not None:
        record["final_message_id"] = final_message_id
    if final_text is not None:
        record["final_text"] = final_text[:4000]
    if error is not None:
        record["error"] = error[:2000]
    _save_state(state)


def latest_for_session(session_key: str) -> dict[str, Any] | None:
    """Return the latest task record for a session, if any."""

    state = _load_state()
    task_id = state.get("latest_by_session", {}).get(session_key)
    if not task_id:
        return None
    record = state.get("tasks", {}).get(task_id)
    return record if isinstance(record, dict) else None


def render_status_reply(record: dict[str, Any]) -> str:
    """Render a concise user-facing status response for a follow-up ping."""

    status = str(record.get("status") or "unknown")
    user_request = str(record.get("user_request") or "").strip()
    request_preview = user_request[:180] + ("..." if len(user_request) > 180 else "")
    started_at = float(record.get("started_at") or 0)
    elapsed = max(int(_now() - started_at), 0) if started_at else 0
    suffix = f"\n\nRequest: {request_preview}" if request_preview else ""

    if status == "running":
        return f"Still cooking. Background task has been running for {elapsed}s.{suffix}"
    if status == "completed":
        return "That background task finished and I posted the result in this thread." + suffix
    if status == "failed":
        error = str(record.get("error") or "unknown error")
        return f"That background task failed: {error}.{suffix}"
    if status == "delivery_failed":
        return (
            "That background task finished, but delivery failed before the result "
            "posted back to the thread."
            + suffix
        )
    return f"Last background task status: {status}.{suffix}"
