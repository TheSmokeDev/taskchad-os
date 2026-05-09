"""Chat-side import shim for cabinet text mode.

Phase 5a contract — Phase 5b's Telegram chat process consumes ONLY this
shim, NEVER `cabinet.text_orchestrator` directly. The shim explicitly
re-exports the canonical names so the chat-process import surface is
stable across cabinet refactors.

Same-process invariant: Phase 5b's chat process accesses cabinet via
HTTP only — this shim exists for type-checker / IDE surface, NOT for
in-process invocation. If a future change calls `handle_text_turn` from
chat code, the chat process gets its own `_CHANNELS` dict and SSE
subscribers in the API process never see those events.
"""
from __future__ import annotations

import sys
from pathlib import Path

# .claude/scripts/ is on sys.path via conftest.py for tests; for production
# the bot main.py inserts it. Re-import path-safe.
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from cabinet.text_orchestrator import (  # noqa: E402
    HandleTurnOptions,
    HandleTurnResult,
    RosterAgent,
    cancel_meeting_turns,
    cancel_turn,
    get_active_turn_ids,
    get_roster,
    handle_text_turn,
    is_acknowledgment,
    is_greeting,
    parse_slash_command,
    wait_for_meeting_turns_idle,
    warmup_meeting,
)

__all__ = [
    "HandleTurnOptions",
    "HandleTurnResult",
    "RosterAgent",
    "cancel_meeting_turns",
    "cancel_turn",
    "get_active_turn_ids",
    "get_roster",
    "handle_text_turn",
    "is_acknowledgment",
    "is_greeting",
    "parse_slash_command",
    "wait_for_meeting_turns_idle",
    "warmup_meeting",
]
