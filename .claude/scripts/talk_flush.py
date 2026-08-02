"""Talk session-end vault debrief — the voice surface's memory flush.

A Talk conversation used to evaporate when the session ended: no daily-log
entry, no episode, no vault debrief. This module closes that gap by reusing
the EXISTING flush pipeline end to end — it renders the browser-relayed
transcript into a context file shaped exactly like the ones the session-end
hook writes, then spawns the same detached ``memory_flush.py`` subprocess.
The LLM distillation, daily-log append, episode write, and reindex all run
in that proven path; nothing here re-implements them.

The session id is prefixed ``talk-`` server-side so ``episodes.derive_flush_meta``
resolves surface ``talk`` — the browser can never pick its own surface.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import threading
import time
from datetime import datetime

import config

logger = logging.getLogger(__name__)

#: Mirrors the session-end hook's MIN_TURNS_TO_FLUSH — admit short sessions;
#: memory_flush.py's FLUSH_OK judgment is the semantic gate.
MIN_FLUSH_TURNS = 2
#: Below this many characters of final transcript text there is nothing an
#: LLM pass could distill — skip without spawning anything.
MIN_FLUSH_CHARS = 200
#: Per-row and total caps. memory_flush truncates to its own 15k tail; the
#: total cap here just keeps the context file bounded before that.
MAX_ROW_CHARS = 2_000
MAX_CONTEXT_CHARS = 15_000
#: Row-count ceiling — keeps a hostile client from posting an unbounded list
#: (the char caps bound the FILE; this bounds the loop).
MAX_ROWS = 400
#: One flush per session id per window — the page fires on stop AND pagehide.
DEDUP_WINDOW_S = 60.0

_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_SESSION_ID_CHARS = 40
#: startedAt is browser-supplied and lands in the context header — strip it
#: to ISO-timestamp characters so newlines/Markdown can't forge header lines.
_SAFE_STARTED_RE = re.compile(r"[^0-9TZz:+.\-]+")
_MAX_STARTED_CHARS = 40
#: Only these roles render; anything else is dropped, never relabeled —
#: a forged role must not turn into an authoritative "Homie" line.
_ROLE_LABELS = {"user": "Operator", "assistant": "Homie"}

_dedup_lock = threading.Lock()
_last_spawn_by_session: dict[str, float] = {}


def _safe_session_component(session_id: str) -> str:
    """Collapse a browser-supplied session id to filename-safe chars.

    Same character class as the session-end hook's ``_safe_filename_component``
    — the id lands in a filename that ``derive_flush_meta`` later parses, so
    path separators and hyphen-count games must die here.
    """
    cleaned = _SAFE_COMPONENT_RE.sub("", str(session_id or ""))
    # Stripping separators can fuse surviving dots into ".." — collapse runs
    # so a traversal-shaped token never appears in the filename at all.
    cleaned = re.sub(r"\.{2,}", ".", cleaned)
    # Leading/trailing dots could produce hidden or traversal-adjacent names.
    cleaned = cleaned.strip("._-")[:_MAX_SESSION_ID_CHARS]
    return cleaned


def render_talk_context(
    transcript: list[dict],
    *,
    session_id: str,
    started_at: str | None = None,
    origin: str = "dashboard /talk",
) -> tuple[str, int]:
    """Render browser transcript rows into flush-context markdown.

    Returns ``(context_markdown, turn_count)``. Only rows with non-empty
    text AND a whitelisted role survive; roles map to the operator/homie
    speaker labels the flush prompt already understands from chat-surface
    context files. The browser-supplied ``started_at`` is stripped to
    timestamp characters before it touches the header.
    """
    started = _SAFE_STARTED_RE.sub("", str(started_at or ""))[:_MAX_STARTED_CHARS]
    header_lines: list[str] = [
        "# Talk Session (voice)",
        f"Session: {session_id}",
        f"Started: {started or 'unknown'}",
        # Additive origin (Discord lifecycle pickup passes "discord voice
        # channel").
        f"Surface: {origin} voice conversation",
        "",
    ]
    turn_lines: list[str] = []
    turns = 0
    for row in transcript[-MAX_ROWS:]:
        role_label = _ROLE_LABELS.get(str(row.get("role") or ""))
        if role_label is None:
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        turn_lines.append(f"**{role_label}:** {text[:MAX_ROW_CHARS]}")
        turn_lines.append("")
        turns += 1
    # The tail-cap applies to the TURN BODY only — a whole-context slice
    # used to drop the header, losing the origin attribution exactly on
    # long (Discord) sessions.
    header = "\n".join(header_lines)
    body = "\n".join(turn_lines)
    budget = MAX_CONTEXT_CHARS - len(header) - 1
    if len(body) > budget:
        body = body[-max(0, budget):]
    return f"{header}\n{body}", turns


def start_session_flush(
    transcript: list[dict],
    *,
    session_id: str = "",
    started_at: str | None = None,
    origin: str = "dashboard /talk",
) -> dict:
    """Gate, write the context file, and spawn the detached flush.

    Never raises to the caller — the voice page fires this blindly on
    session end and a flush failure must never surface as a page error.
    """
    try:
        return _start_session_flush_inner(
            transcript, session_id=session_id, started_at=started_at, origin=origin
        )
    except Exception as exc:  # noqa: BLE001 — receipt beats a 500 on teardown
        logger.warning("talk flush failed to start: %s", exc)
        return {"status": "error", "reason": str(exc)[:200]}


def _start_session_flush_inner(
    transcript: list[dict],
    *,
    session_id: str,
    started_at: str | None,
    origin: str = "dashboard /talk",
) -> dict:
    safe_id = _safe_session_component(session_id)
    if not safe_id:
        safe_id = datetime.now().strftime("%H%M%S%f")
    # Server-owned surface invariant: the ``talk-`` prefix is what makes
    # derive_flush_meta resolve surface "talk". Always prefixed here.
    flush_session_id = f"talk-{safe_id}"

    context, turns = render_talk_context(
        transcript or [],
        session_id=flush_session_id,
        started_at=started_at,
        origin=origin,
    )
    body_chars = sum(
        len(str(row.get("text") or "").strip())
        for row in (transcript or [])
        if str(row.get("role") or "") in _ROLE_LABELS
    )
    if turns < MIN_FLUSH_TURNS:
        return {"status": "skipped", "reason": f"fewer than {MIN_FLUSH_TURNS} turns"}
    if body_chars < MIN_FLUSH_CHARS:
        return {"status": "skipped", "reason": f"under {MIN_FLUSH_CHARS} chars"}

    now = time.monotonic()
    with _dedup_lock:
        last = _last_spawn_by_session.get(flush_session_id)
        if last is not None and (now - last) < DEDUP_WINDOW_S:
            return {"status": "skipped", "reason": "already flushed"}
        _last_spawn_by_session[flush_session_id] = now
        # The map only ever holds recent sessions; drop expired entries so a
        # long-lived API process does not accumulate one key per session.
        for key in [
            k for k, v in _last_spawn_by_session.items() if (now - v) >= DEDUP_WINDOW_S
        ]:
            _last_spawn_by_session.pop(key, None)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    context_filename = f"session-flush-{flush_session_id}-{timestamp}.md"
    context_path = config.STATE_DIR / context_filename
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    context_path.write_text(context, encoding="utf-8")

    try:
        _spawn_flush(context_path)
    except Exception:
        # A failed spawn must not (a) leave the dedup entry blocking a valid
        # retry, or (b) orphan a plaintext transcript in STATE_DIR.
        with _dedup_lock:
            _last_spawn_by_session.pop(flush_session_id, None)
        try:
            context_path.unlink()
        except OSError:
            pass
        raise
    return {"status": "started", "contextFile": context_filename}


def _spawn_flush(context_path) -> None:
    """Detached ``memory_flush.py`` spawn — the session-end hook's pattern.

    The subprocess owns the LLM call, daily-log append, episode write, and
    reindex, and it survives an API restart.
    """
    scripts_dir = config.SCRIPTS_DIR
    cmd = [
        "uv",
        "run",
        "--directory",
        str(scripts_dir),
        "python",
        "memory_flush.py",
        "--context-file",
        str(context_path),
    ]
    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    subprocess.Popen(  # noqa: S603 — fixed argv, path from config
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
    )


__all__ = [
    "MIN_FLUSH_CHARS",
    "MIN_FLUSH_TURNS",
    "render_talk_context",
    "start_session_flush",
]
