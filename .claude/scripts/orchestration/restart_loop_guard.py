"""Restart-loop circuit breaker (Hermes v0.18 port).

An agent (or a bad supervisor trigger) can drive ``chat/main.py`` into a
tight respawn cycle: each boot kills a live/stale predecessor
(``cleanup_all_bot_processes`` returns a non-empty ``killed`` list),
restores cognitive state from the vault, and — if the restored state
re-runs the logic that killed the process — dies again seconds later.  A
supervisor (``run_chat.sh`` relaunch, a scheduled restart, or an external
monitor) keeps respawning it, and the boot-time state restore replays the
same fatal path every ~10 seconds until a human intervenes.

This module is the last-resort circuit breaker.  It records a timestamp
each time the bot boots with a respawn signal, keeps a rolling window of
recent boots persisted across processes (each boot is a fresh process, so
in-memory state is useless), and reports the loop as "tripped" once too
many such boots happen inside a short window.  When tripped, the caller
SKIPS boot-time state restore for that boot — the bot still starts and
serves real inbound messages, it just stops replaying the state that keeps
killing it, which breaks the cycle and puts a human back in the loop.

State lives in ``<STATE_DIR>/restart_loop.json`` so it is profile-scoped
and survives process death.  It is intentionally tiny and best-effort:
any read/write failure fails OPEN (no false trip) because a broken breaker
must never wedge a healthy bot.  The write is atomic (tmp + ``os.replace``)
so a crash mid-write never leaves a truncated log — corrupt state would
lose the boot evidence and fail the breaker open exactly when a respawn
loop should trip.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# Defaults chosen so a legitimate operator restart (or two) never trips the
# breaker, but a documented ~10s respawn loop does within a few cycles.
DEFAULT_MAX_RESTARTS = 3
DEFAULT_WINDOW_SECONDS = 60


def _state_path() -> Path:
    # Resolve at CALL time (Rule 1): config.STATE_DIR is persona-resolved at
    # import, which happens after apply_persona_override() in main.py.
    import config

    return config.STATE_DIR / "restart_loop.json"


def _load_boots() -> List[float]:
    try:
        raw = _state_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        boots = data.get("boots", [])
        return [float(t) for t in boots if isinstance(t, (int, float))]
    except (OSError, ValueError, TypeError):
        return []


def _save_boots(boots: List[float]) -> bool:
    """Persist the boot log atomically. Returns True on success, False on any
    write failure (the caller uses this to fail OPEN — the breaker must never
    trip on state it could not durably record)."""
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # R2 NB1 — atomic write (Windows-first): a crash mid-write must never
        # leave a truncated restart_loop.json. Deviation from upstream's direct
        # write_text, matching the dead_targets tmp + os.replace shape.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps({"boots": boots}), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def record_restart_interrupted_boot(
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    *,
    now: Optional[float] = None,
) -> Tuple[List[float], bool]:
    """Record that the bot just booted with a respawn signal.

    Prunes boots older than ``window_seconds`` and appends the current time.
    Returns ``(boots, persisted)``: the pruned+appended list (most recent
    last) and whether the write succeeded.  Best-effort — a persistence
    failure returns ``persisted=False`` (no raise) so the caller can fail
    OPEN rather than trip on a count it could not durably record.
    """
    ts = time.time() if now is None else now
    cutoff = ts - max(1, window_seconds)
    boots = [t for t in _load_boots() if t >= cutoff]
    boots.append(ts)
    persisted = _save_boots(boots)
    return boots, persisted


def is_restart_loop_tripped(
    max_restarts: int = DEFAULT_MAX_RESTARTS,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    *,
    now: Optional[float] = None,
) -> bool:
    """Return True if the bot has restarted ``>= max_restarts`` times with a
    respawn signal inside the last ``window_seconds``.

    Reads the persisted boot log written by
    ``record_restart_interrupted_boot`` and counts boots within the window.
    Fails OPEN (returns False) on any error — a broken breaker must never
    wedge a healthy bot.
    """
    if max_restarts <= 0:
        return False
    ts = time.time() if now is None else now
    cutoff = ts - max(1, window_seconds)
    try:
        recent = [t for t in _load_boots() if t >= cutoff]
    except Exception:  # pragma: no cover — _load_boots already guards
        return False
    return len(recent) >= max_restarts


def clear() -> None:
    """Remove the persisted boot log (used on clean shutdown / by tests)."""
    try:
        _state_path().unlink(missing_ok=True)
    except OSError:
        pass


def check_and_record(
    max_restarts: int = DEFAULT_MAX_RESTARTS,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    *,
    now: Optional[float] = None,
) -> bool:
    """Record this respawn-signal boot and report whether the loop is now
    tripped.

    This is the single entry point the bot calls: it appends the current
    boot, then checks whether the (now-updated) window has reached the
    threshold.  Returns True when boot-time state restore should be SKIPPED
    to break the loop.

    Fails OPEN when the boot could not be durably persisted: a breaker that
    cannot record boots across processes cannot trust its own count, so it
    never trips (a broken breaker must never wedge a healthy bot).
    """
    boots, persisted = record_restart_interrupted_boot(window_seconds, now=now)
    if not persisted:
        logger.warning(
            "Restart-loop breaker could not persist the boot log (%s) — "
            "failing OPEN (not tripping) so a broken breaker never wedges the "
            "bot by skipping state restore.",
            _state_path(),
        )
        return False
    tripped = len(boots) >= max_restarts if max_restarts > 0 else False
    if tripped:
        logger.warning(
            "Restart-loop breaker TRIPPED: %d rapid bot boots within %ds "
            "(threshold %d). Skipping boot-time state restore to break a "
            "suspected respawn loop. The bot still starts and serves real "
            "inbound messages. If this is a false positive, delete %s.",
            len(boots),
            window_seconds,
            max_restarts,
            _state_path(),
        )
    return tripped
