"""Auto-generate short cabinet meeting titles from the first user/assistant exchange.

PORTED FROM Hermes `agent/title_generator.py:1-5, 22-44, 95-125` per PRD-8 §0a.

M2 contract — fires AFTER the first user/assistant exchange (NOT first user
turn alone). Inputs are BOTH messages. 30s timeout (matches Hermes
`TITLE_GENERATION_TIMEOUT_S`). Background async via `asyncio.create_task` so
it never blocks `handle_text_turn`. On failure or timeout, leaves
`cabinet_meetings.title=NULL` — UI displays "Untitled meeting".

B1 lock — dispatched via lane_router; cabinet code never invokes any concrete
provider client.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Final

from dashboard_db import get_connection
from runtime import lane_router
from runtime.base import RuntimeRequest
from runtime.capabilities import TEXT_REASONING

logger = logging.getLogger(__name__)

# PRD-8 Phase 7b WS1 (codex post-build F1) — log-message redaction at every
# cabinet log emit site. Module-attribute import (Rule 3); redact() unconditional.
from security import redact as _redact_mod  # noqa: E402
_redact = _redact_mod.redact


TITLE_MODEL: Final[str] = "claude-haiku-4-5-20251001"
TITLE_GENERATION_TIMEOUT_S: Final[float] = 30.0
TITLE_MAX_LEN: Final[int] = 80

_TITLE_PROMPT: Final[str] = (
    "Generate a short, descriptive title (3-7 words) for a conversation that starts "
    "with the following exchange. The title should capture the main topic or intent. "
    "Return ONLY the title text, nothing else. No quotes, no punctuation at the end, "
    "no prefixes."
)


def _clean_title(raw: str) -> str | None:
    """Mirror Hermes `generate_title` cleanup at title_generator.py:46-53."""
    title = raw.strip()
    # Strip optional "Title:" prefix BEFORE quote-stripping (Hermes order).
    if title.lower().startswith("title:"):
        title = title[len("title:"):].strip()
    title = title.strip('"').strip("'")
    if len(title) > TITLE_MAX_LEN:
        title = title[: TITLE_MAX_LEN - 3] + "..."
    return title or None


async def generate_title(
    user_message: str,
    assistant_response: str,
    *,
    timeout: float | None = None,
) -> str | None:
    """Generate a session title from the first exchange.

    Mirror Hermes `generate_title` semantics — both messages, lane-router
    dispatch, 30s default timeout. Returns the cleaned title or None.

    Rule 1: `timeout=None` sentinel resolved at body-time.
    """
    resolved_timeout = TITLE_GENERATION_TIMEOUT_S if timeout is None else timeout
    user_snippet = (user_message or "")[:500]
    assistant_snippet = (assistant_response or "")[:500]
    if not user_snippet and not assistant_snippet:
        return None

    prompt = (
        f"{_TITLE_PROMPT}\n\n"
        f"User: {user_snippet}\n\nAssistant: {assistant_snippet}"
    )

    request = RuntimeRequest(
        prompt=prompt,
        cwd=Path.cwd(),
        task_name="cabinet_title",
        capability=TEXT_REASONING,
        model=TITLE_MODEL,
        max_turns=1,
        allowed_tools=[],
        disallowed_tools=["*"],
        permission_mode="bypassPermissions",
        allow_fallback=False,
        metadata={"caller": "cabinet_title"},
    )

    try:
        result = await asyncio.wait_for(
            lane_router.run_with_runtime_lanes(request),
            timeout=resolved_timeout,
        )
    except (TimeoutError, Exception) as exc:  # noqa: BLE001
        logger.debug("cabinet title generation failed: %s", _redact(str(exc)))
        return None

    return _clean_title(result.text or "")


async def maybe_set_meeting_title(
    meeting_id: int,
    user_message: str,
    assistant_response: str,
) -> None:
    """Set `cabinet_meetings.title` if not already set. Best-effort, never raises.

    Mirrors Hermes `auto_title_session` — checks existing title first;
    only writes when previously NULL/empty. Background-safe.
    """
    if not meeting_id or not user_message or not assistant_response:
        return

    # Skip if title already set (operator may have set one manually).
    try:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT title FROM cabinet_meetings WHERE id = ?",
                (meeting_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("cabinet title pre-check failed: %s", _redact(str(exc)))
        return

    if row is None:
        return
    existing = row["title"] if "title" in row.keys() else None
    if existing:
        return

    title = await generate_title(user_message, assistant_response)
    if not title:
        return

    try:
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE cabinet_meetings SET title = ? WHERE id = ?",
                (title, meeting_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("cabinet title write failed: %s", _redact(str(exc)))


def schedule_title_generation(
    meeting_id: int,
    user_message: str,
    assistant_response: str,
    *,
    fire: bool = True,
) -> asyncio.Task | None:
    """Fire-and-forget background title generation.

    Mirrors Hermes `maybe_auto_title` — schedules via `asyncio.create_task`
    so the caller (handle_text_turn) never waits on the title. Returns the
    Task for tests; production callers ignore the return.

    `fire=False` returns None without scheduling — for tests that exercise
    the conditional skip without requiring a running loop.
    """
    if not fire:
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — skip silently. Caller is in a sync path.
        logger.debug("schedule_title_generation: no running loop")
        return None
    return loop.create_task(
        maybe_set_meeting_title(meeting_id, user_message, assistant_response)
    )


__all__ = [
    "TITLE_GENERATION_TIMEOUT_S",
    "TITLE_MAX_LEN",
    "TITLE_MODEL",
    "generate_title",
    "maybe_set_meeting_title",
    "schedule_title_generation",
]
