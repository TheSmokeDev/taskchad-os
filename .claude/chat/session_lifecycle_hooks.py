"""Chat session lifecycle helpers for explicit context boundaries."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from session_keys import build_session_key

LOGGER = logging.getLogger(__name__)

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
TRANSCRIPT_LIMIT = 1000


@dataclass
class LifecycleEvent:
    """One clear-lifecycle step result."""

    step: str
    status: str
    detail: str = ""


@dataclass
class ClearLifecycleResult:
    """Clear lifecycle result surfaced to command handlers and tests."""

    session_id: str
    transcript_path: Path | None = None
    events: list[LifecycleEvent] = field(default_factory=list)

    def add(self, step: str, status: str, detail: str = "") -> None:
        self.events.append(LifecycleEvent(step=step, status=status, detail=detail))

    @property
    def failures(self) -> list[LifecycleEvent]:
        return [event for event in self.events if event.status in {"error", "warn"}]

    def warning_summary(self, *, limit: int = 3) -> str:
        warnings = self.failures[:limit]
        if not warnings:
            return ""
        parts = [
            f"{event.step}: {event.detail or event.status}"
            for event in warnings
        ]
        if len(self.failures) > limit:
            parts.append(f"+{len(self.failures) - limit} more")
        return "; ".join(parts)


@dataclass
class HookInvocation:
    """Subprocess hook invocation result without leaking hook stdout."""

    hook_name: str
    returncode: int
    stderr: str = ""
    stdout_chars: int = 0

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.stderr.strip()

    def detail(self) -> str:
        if self.returncode != 0:
            return f"exit {self.returncode}"
        if self.stderr.strip():
            return _tail(self.stderr.strip())
        if self.stdout_chars:
            return f"stdout {self.stdout_chars} chars"
        return "completed"


def get_state_dir() -> Path:
    """Resolve the active state directory lazily so persona env overrides apply."""

    from config import STATE_DIR  # noqa: WPS433

    return STATE_DIR


def write_clear_transcript(
    *,
    store: Any,
    session: Any,
    platform: str,
    channel_id: str,
    thread_id: str,
    source: str = "clear",
    state_dir: Path | None = None,
) -> Path:
    """Persist current session messages as JSONL before destructive clear."""

    session_id = getattr(session, "session_id", "") or build_session_key(
        platform,
        channel_id,
        thread_id,
    )
    root = (state_dir or get_state_dir()) / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    transcript_path = (
        root / f"clear-transcript-{_safe_filename(session_id)}-{timestamp}.jsonl"
    )

    messages = []
    list_messages = getattr(store, "list_messages", None)
    if callable(list_messages):
        messages = list_messages(session_id, limit=TRANSCRIPT_LIMIT)

    metadata = {
        "type": "session_signal",
        "source": source,
        "event": "clear",
        "session_id": session_id,
        "platform": platform,
        "channel_id": channel_id,
        "thread_id": thread_id,
        "user_id": getattr(session, "user_id", ""),
        "message_count": getattr(session, "message_count", 0),
        "runtime_lane": getattr(session, "runtime_lane", ""),
        "runtime_provider": getattr(session, "runtime_provider", ""),
        "runtime_model": getattr(session, "runtime_model", ""),
        "created_at": _iso(getattr(session, "created_at", None)),
        "updated_at": _iso(getattr(session, "updated_at", None)),
        "written_at": datetime.now().isoformat(),
    }

    with transcript_path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(metadata, ensure_ascii=False) + "\n")
        for message in messages:
            row = {
                "type": "message",
                "message": {
                    "role": getattr(message, "role", ""),
                    "content": getattr(message, "content", ""),
                },
                "created_at": _iso(getattr(message, "created_at", None)),
                "tool_calls": getattr(message, "tool_calls", []),
            }
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    return transcript_path


def run_hook_script(
    hook_name: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: float = 15.0,
) -> HookInvocation:
    """Invoke an existing hook script with JSON stdin."""

    hook_path = HOOKS_DIR / hook_name
    if not hook_path.exists():
        raise FileNotFoundError(f"hook not found: {hook_path}")

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(  # noqa: S603
        [sys.executable, str(hook_path)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        creationflags=creation_flags,
    )
    return HookInvocation(
        hook_name=hook_name,
        returncode=completed.returncode,
        stderr=completed.stderr or "",
        stdout_chars=len(completed.stdout or ""),
    )


def clear_session_with_lifecycle(
    *,
    store: Any,
    session: Any,
    platform: str,
    channel_id: str,
    thread_id: str,
    engine: Any = None,
    source: str = "clear",
) -> ClearLifecycleResult:
    """Run clear lifecycle steps and delete the session after hook attempts."""

    session_id = getattr(session, "session_id", "") or build_session_key(
        platform,
        channel_id,
        thread_id,
    )
    result = ClearLifecycleResult(session_id=session_id)

    try:
        result.transcript_path = write_clear_transcript(
            store=store,
            session=session,
            platform=platform,
            channel_id=channel_id,
            thread_id=thread_id,
            source=source,
        )
        result.add("persist_transcript", "ok", str(result.transcript_path))
    except Exception as exc:  # noqa: BLE001
        _record_failure(result, "persist_transcript", exc)

    payload = {
        "session_id": session_id,
        "source": source,
        "platform": platform,
        "channel_id": channel_id,
        "thread_id": thread_id,
        "transcript_path": str(result.transcript_path or ""),
    }
    _invoke_hook(result, "session-end-flush.py", payload)
    _invoke_hook(result, "session-start-context.py", payload)

    deleted = store.delete(platform, channel_id, thread_id)
    result.add(
        "session_delete",
        "ok" if deleted else "warn",
        "deleted" if deleted else "not found",
    )

    reload_identity = getattr(engine, "reload_soul_context", None)
    if callable(reload_identity):
        try:
            reload_identity()
            result.add("identity_reload", "ok", "reloaded")
        except Exception as exc:  # noqa: BLE001
            _record_failure(result, "identity_reload", exc)
    else:
        result.add("identity_reload", "skip", "engine has no reload_soul_context")

    _log_result(result)
    return result


def _invoke_hook(
    result: ClearLifecycleResult,
    hook_name: str,
    payload: dict[str, Any],
) -> None:
    try:
        invocation = run_hook_script(hook_name, payload)
    except Exception as exc:  # noqa: BLE001
        _record_failure(result, hook_name, exc)
        return

    if invocation.ok:
        result.add(hook_name, "ok", invocation.detail())
    else:
        result.add(hook_name, "warn", invocation.detail())
        LOGGER.warning("clear lifecycle hook warning: %s", invocation.detail())


def _record_failure(
    result: ClearLifecycleResult,
    step: str,
    exc: Exception,
) -> None:
    detail = f"{type(exc).__name__}: {_tail(str(exc))}"
    result.add(step, "error", detail)
    LOGGER.warning("clear lifecycle step failed: %s: %s", step, detail)


def _log_result(result: ClearLifecycleResult) -> None:
    try:
        log_path = get_state_dir() / "clear-lifecycle-events.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "timestamp": datetime.now().isoformat(),
            "session_id": result.session_id,
            "transcript_path": str(result.transcript_path or ""),
            "events": [
                {
                    "step": event.step,
                    "status": event.status,
                    "detail": event.detail,
                }
                for event in result.events
            ],
        }
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("clear lifecycle log write failed: %s", exc)


def _safe_filename(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return safe or "session"


def _iso(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return ""


def _tail(value: str, *, max_chars: int = 300) -> str:
    text = value.strip()
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]
