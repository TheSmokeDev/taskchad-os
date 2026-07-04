"""Bot lifecycle guard for scheduled/fan-out job creation.

Ported from Hermes v0.18 ``cron/lifecycle_guard.py`` (algorithm verbatim,
command shapes re-anchored to The Homie bot lifecycle, Windows-first).

An agent driving a convoy or a ``/api/scheduled`` job can plant free text that
schedules the bot's own death — ``bash run_chat.sh``, ``taskkill /IM
run_chat``, ``Stop-Process ... thehomie``, ``pkill -f chat/main.py``. When such
a job fires (or a fan-out re-runs it), the bot dies, the supervisor revives it,
and the offending text re-runs — a respawn loop. This module rejects those
specs at CREATION time with an informative ``BotLifecycleBlocked`` error instead
of a silent fire-time failure.

The pattern is intentionally command-shaped and BOT-SPECIFIC: every branch
anchors on a concrete two-token command identifier (the bot launcher
``run_chat.sh``, a launch verb + the repo-qualified ``chat/main.py`` path, or a
kill/service verb + a bot token ``run_chat`` / ``thehomie`` / ``chat/main.py``).
It NEVER matches bare ``python`` / ``main.py`` / ``bot``: a convoy titled "Run
`python app/main.py` and report the error" or a maintenance task that says
"taskkill /IM python.exe" is legitimate work, and a job ``prompt`` is fed to a
future LLM (not a shell), so an over-broad English/generic substring match would
be a high-false-positive bug that prevents nothing. This mirrors upstream's
requirement of BOTH ``hermes`` AND ``gateway`` per branch.

This is a defence-in-depth layer at creation time only; it never mutates state.
"""

from __future__ import annotations

import re
from pathlib import Path


class BotLifecycleBlocked(ValueError):  # noqa: N818 — contract-locked name (PRP rename of GatewayLifecycleBlocked)
    """Raised when a job spec contains a bot-lifecycle command.

    Subclasses ``ValueError`` so the convoy API's global ValueError→HTTP 400
    mapper surfaces it as a create failure. The dashboard ``/api/scheduled``
    seam has NO such mapper and translates it to ``HTTPException(400)`` itself.
    """


# Shell-level command shapes that target the bot lifecycle. Each branch is
# anchored on a concrete, bot-SPECIFIC two-token command identifier so a match
# can only fire on actual shell-command-shaped strings, not on prose, and never
# on a bare ``python`` / ``main.py`` / ``bot`` token.
_BOT_LIFECYCLE_PATTERN = re.compile(
    r"(?i)"
    # A: run the bot's launcher (run_chat.sh is a bot-specific filename).
    r"(?:\brun_chat\.sh\b)"
    # B: (re)launch the chat process — a launch verb AND the repo-qualified
    #    ``chat/main.py`` path. Bare ``main.py`` / ``python app/main.py`` must NOT match.
    r"|(?:(?:python|py|uv\s+run(?:\s+python)?|bash|sh)\b[^\n]*\bchat[\\/]main\.py\b)"
    # C: Windows kill targeting the bot — a bot token, NEVER bare ``python``.
    r"|(?:taskkill\b[^\n]*\b(?:run_chat|thehomie|chat[\\/]main\.py)\b)"
    # D: PowerShell kill / service control targeting the bot — NEVER bare ``python``/``bot``.
    r"|(?:stop-process\b[^\n]*\b(?:run_chat|thehomie|chat[\\/]main\.py)\b)"
    r"|(?:(?:restart-service|nssm\s+(?:restart|stop)|sc\s+stop)\b[^\n]*\b(?:thehomie|homie[-_]?bot)\b)"
    # E: POSIX kill targeting the bot. The LEFT \b is required so ordinary words
    #    ending in "kill" (upskill / reskill / roadkill) do NOT read as the
    #    command `kill` — the bot-specific two-token shape stays intact.
    r"|(?:\b(?:pkill|kill)\b[^\n]*\b(?:run_chat|thehomie|chat[\\/]main\.py)\b)"
)


def contains_bot_lifecycle_command(text: str) -> bool:
    """Return True if *text* contains a bot lifecycle command pattern."""
    if not text:
        return False
    return bool(_BOT_LIFECYCLE_PATTERN.search(text))


def _resolve_script_path(script_path: str) -> Path:
    """Resolve a job ``script`` value the same way a scheduler would.

    A bare/relative script path resolves under ``<STATE_DIR>/scripts/``; an
    absolute path is used as-is. Ported verbatim from upstream (which resolved
    under ``<HERMES_HOME>/scripts``). NOTE: convoys and ``scheduled_tasks`` have
    NO ``script`` field, so this path is DORMANT — kept for a future
    scheduled-job seam that supplies a script. ``config.STATE_DIR`` is resolved
    at CALL time (Rule 1 — it is persona-resolved at import).
    """
    import config

    raw = Path(script_path).expanduser()
    if raw.is_absolute():
        return raw
    return config.STATE_DIR / "scripts" / raw


def _read_script_for_scanning(script_path: str) -> str:
    """Read a script file for lifecycle-pattern scanning.

    Decodes with ``errors="replace"`` so binary or non-UTF-8 content does not
    silently bypass the check — a plain text-mode read raises
    ``UnicodeDecodeError`` on such files, and swallowing that error would let an
    attacker hide the command in binary noise. Returns an empty string only when
    the file cannot be read at all.
    """
    try:
        return _resolve_script_path(script_path).read_bytes().decode(
            "utf-8", errors="replace"
        )
    except OSError:
        return ""


def check_bot_lifecycle(
    prompt: str | None,
    script: str | None = None,
) -> None:
    """Raise ``BotLifecycleBlocked`` if *prompt* or *script* contains a
    bot-lifecycle command pattern.

    ``prompt`` is scanned directly. ``script``, when supplied, is read from disk
    and concatenated for the scan. Both are considered together so a job cannot
    slip through by splitting the command across the prompt and the script.

    Callers should let the exception propagate when they want the create to fail
    with a ``ValueError``-shaped error (the convoy API maps it to HTTP 400; the
    dashboard scheduled seam translates it to ``HTTPException(400)``).
    """
    combined = prompt or ""
    if script:
        script_text = _read_script_for_scanning(script)
        if script_text:
            combined = f"{combined}\n{script_text}"

    if contains_bot_lifecycle_command(combined):
        raise BotLifecycleBlocked(
            "Blocked: job contains a bot lifecycle command (launch/kill/restart "
            "of run_chat.sh / chat/main.py / thehomie). This is blocked to "
            "prevent agent-driven respawn loops under bot-process supervision. "
            "Run bot lifecycle commands from a shell outside the running bot "
            "instead."
        )
