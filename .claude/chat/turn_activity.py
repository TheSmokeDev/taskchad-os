"""Turn progress streaming — live tool-activity labels + Stop affordance.

Phase 5 (Hermes messaging-surface polish). This module is the ONE shared
place for the "current activity" progress contract:

- the engine derives a human-readable label from runtime tool events via
  ``describe_tool_event()`` and drops it into the shared ``progress`` dict
  (lane-agnostic: generic lanes never fire tool events, so the elapsed
  ticker simply keeps running without a label);
- the router owns one ``TurnProgressReporter`` per in-flight turn, which
  renders the label into rate-limit-aware placeholder edits (Telegram
  ~1 edit/sec/chat, Discord ~5/5s/channel — throttled via
  ``config.get_turn_progress_settings()``, Rule 1 call-time);
- adapters only render ``OutgoingMessage`` updates like any other edit.

Every seam here is fail-open: a progress-edit failure must NEVER fail the
turn. Config import is late-bound and guarded (sanitized-export pattern,
see commit e52e4cc) with safe defaults when config is unavailable.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from typing import Any

from models import Channel, MessageComponent, OutgoingMessage, Thread

# Custom-id prefix for the Stop button. The payload after the prefix is
# deliberately NON-authoritative: the router scopes the cancel to the
# TAPPER's own conversation key, never to anything carried in the button.
STOP_CUSTOM_ID_PREFIX = "turn_stop:"

_DEFAULT_EDIT_MIN_INTERVAL_S = 1.5

# Tool name (lowercased) -> present-progressive verb. Unknown tools fall
# back to "Using <Name>". Detail extraction below appends a short target
# (file basename, pattern, command...) when one can be parsed safely.
_TOOL_VERBS: dict[str, str] = {
    "read": "Reading",
    "write": "Writing",
    "edit": "Editing",
    "multiedit": "Editing",
    "notebookedit": "Editing",
    "grep": "Searching",
    "glob": "Scanning",
    "bash": "Running",
    "websearch": "Searching the web",
    "webfetch": "Fetching",
    "task": "Delegating",
    "todowrite": "Updating the plan",
    "skill": "Using skill",
}

# Keys probed (in order) inside the repr()-style input preview. Path-like
# keys reduce to a basename so progress edits never echo full local paths
# into chat.
_PATH_KEYS = ("file_path", "notebook_path", "path", "filename")
_QUERY_KEYS = ("pattern", "query", "command", "url", "description", "prompt")

_DETAIL_MAX_CHARS = 48


def _settings() -> Any:
    """Late-bound, guarded config lookup (Rule 3 style + export safety)."""
    try:
        import config as _config

        return _config.get_turn_progress_settings()
    except Exception:
        return None


def _clip(value: str, limit: int = _DETAIL_MAX_CHARS) -> str:
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _extract_detail(preview: str) -> str:
    """Pull a short human target out of the truncated repr() input preview.

    Query-like keys win over path-like keys: Grep sends both ``pattern`` and
    ``path``, and the pattern is the informative half. Read/Write/Edit carry
    only path keys, so they still resolve to a basename.
    """
    for key in _QUERY_KEYS + _PATH_KEYS:
        match = re.search(
            r"['\"]" + re.escape(key) + r"['\"]\s*:\s*['\"]([^'\"]*)", preview
        )
        if not match:
            continue
        value = match.group(1).strip()
        if not value:
            continue
        if key in _PATH_KEYS:
            value = value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        return _clip(value)
    return ""


def describe_tool_event(ev: dict[str, Any]) -> str:
    """Map a runtime tool event to a short human activity label.

    Event shape comes from the runtime layer's ``on_tool_event`` callback:
    ``{"id": ..., "name": ..., "input_preview": repr(input)[:200]}``.
    Never raises; returns "" when nothing sensible can be derived.
    """
    try:
        name = str(ev.get("name") or "").strip()
        if not name:
            return ""
        detail = _extract_detail(str(ev.get("input_preview") or ""))
        verb = _TOOL_VERBS.get(name.lower())
        if verb is None:
            return f"Using {name}" + (f": {detail}" if detail else "")
        return f"{verb} {detail}".strip()
    except Exception:
        return ""


def format_progress_status(
    elapsed_s: float, tool_calls: int, activity: str = ""
) -> str:
    """Render the placeholder progress text.

    MUST keep the leading "Working..." prefix — the Telegram adapter's
    voice-reply branch identifies progress ticks by
    ``text.startswith(("Thinking...", "Working..."))``.
    """
    status = f"Working... ({int(elapsed_s)}s)"
    if tool_calls:
        status += f" | {tool_calls} tool calls"
    if activity:
        status += f"\n🔧 {activity}"
    return status


def build_stop_components() -> list[MessageComponent]:
    """Stop button for the progress placeholder (empty list when disabled).

    The custom_id payload ("turn") is intentionally inert — the router
    resolves the cancel scope from the tapper's own conversation identity,
    so one chat can never stop another chat's turn.
    """
    settings = _settings()
    if settings is not None and not settings.stop_button_enabled:
        return []
    return [
        MessageComponent(
            label="⏹ Stop", custom_id=f"{STOP_CUSTOM_ID_PREFIX}turn", style="danger"
        )
    ]


class TurnProgressReporter:
    """Owns all placeholder edits for one in-flight turn.

    ``install()`` binds the sync ``notify_activity`` hook into the shared
    progress dict (called by the engine's ``_on_tool_event`` from inside the
    event loop) and starts the slow elapsed-time ticker. Edits are throttled
    to ``TURN_PROGRESS_EDIT_MIN_INTERVAL_S``; a burst of tool events edits
    once immediately and schedules ONE trailing flush so the final activity
    state always lands. ``close()`` runs before final delivery so a stale
    progress edit can never overwrite the real answer.
    """

    def __init__(
        self,
        adapter: Any,
        *,
        channel: Channel,
        thread: Thread | None,
        placeholder_id: str,
        progress: dict[str, Any],
        components: list[MessageComponent] | None = None,
        ticker_interval_s: float = 12.0,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._adapter = adapter
        self._channel = channel
        self._thread = thread
        self._placeholder_id = placeholder_id
        self._progress = progress
        self._components = list(components or [])
        self._ticker_interval_s = ticker_interval_s
        self._now = now_fn
        self._last_edit_at: float | None = None
        self._closed = False
        self._edit_tasks: set[asyncio.Task[Any]] = set()
        self._flush_task: asyncio.Task[Any] | None = None
        self._ticker_task: asyncio.Task[Any] | None = None

    # ── lifecycle ──────────────────────────────────────────────────

    def install(self) -> None:
        """Bind the notify hook and start the elapsed ticker (fail-open)."""
        self._progress["notify_activity"] = self.notify_activity
        try:
            self._ticker_task = asyncio.get_running_loop().create_task(
                self._run_ticker()
            )
        except Exception:
            self._ticker_task = None

    def close(self) -> None:
        """Stop all progress edits. Call BEFORE delivering the final answer."""
        self._closed = True
        self._progress.pop("notify_activity", None)
        pending = [self._ticker_task, self._flush_task, *self._edit_tasks]
        for task in pending:
            if task is not None and not task.done():
                task.cancel()

    # ── engine-facing hook (sync, fail-open) ───────────────────────

    def notify_activity(self, activity: str) -> None:  # noqa: ARG002 — label read from progress
        """Tool event landed — edit now if the throttle allows, else arm a
        trailing flush so the latest state still lands."""
        if self._closed:
            return
        try:
            interval = self._min_interval()
            now = self._now()
            if (
                self._last_edit_at is not None
                and (now - self._last_edit_at) < interval
            ):
                if self._flush_task is None or self._flush_task.done():
                    delay = max(0.0, interval - (now - self._last_edit_at))
                    self._flush_task = asyncio.get_running_loop().create_task(
                        self._flush_after(delay)
                    )
                return
            self._schedule_edit()
        except Exception:
            pass

    # ── internals ──────────────────────────────────────────────────

    def _min_interval(self) -> float:
        settings = _settings()
        if settings is None:
            return _DEFAULT_EDIT_MIN_INTERVAL_S
        return float(settings.edit_min_interval_s)

    def _render(self) -> str:
        try:
            started = float(self._progress.get("started") or time.time())
        except (TypeError, ValueError):
            started = time.time()
        elapsed = max(0.0, time.time() - started)
        tool_calls = int(self._progress.get("tool_calls") or 0)
        activity = str(self._progress.get("activity") or "")
        return format_progress_status(elapsed, tool_calls, activity)

    def _schedule_edit(self) -> None:
        if self._closed:
            return
        # Gate BEFORE the edit completes so a burst can't stack edits.
        self._last_edit_at = self._now()
        try:
            task = asyncio.get_running_loop().create_task(self._do_edit())
        except Exception:
            return
        self._edit_tasks.add(task)
        task.add_done_callback(self._edit_tasks.discard)

    async def _do_edit(self) -> None:
        if self._closed:
            return
        try:
            await self._adapter.update(
                OutgoingMessage(
                    text=self._render(),
                    channel=self._channel,
                    thread=self._thread,
                    is_update=True,
                    update_message_id=self._placeholder_id,
                    components=list(self._components),
                )
            )
        except Exception:
            pass  # progress-edit failure must never fail the turn

    async def _flush_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            if not self._closed:
                self._schedule_edit()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _run_ticker(self) -> None:
        """Slow heartbeat — refreshes elapsed time even with no tool events."""
        while True:
            await asyncio.sleep(self._ticker_interval_s)
            if self._closed:
                return
            try:
                interval = self._min_interval()
                now = self._now()
                if (
                    self._last_edit_at is None
                    or (now - self._last_edit_at) >= interval
                ):
                    self._schedule_edit()
            except Exception:
                pass
