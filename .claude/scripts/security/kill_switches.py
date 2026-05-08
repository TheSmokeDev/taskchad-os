"""Operator-visible kill-switches for LLM + recall + future surfaces.

Backed by env vars HOMIE_KILLSWITCH_<name>=disabled (case-insensitive). Refusal
counters in module state; surfaced via /api/health killSwitches field.

Rule 3 enforcement: every consumer MUST use module-attribute lookup:

    # CORRECT
    from security import kill_switches
    kill_switches.requireEnabled("llm")

    # WRONG — defeats monkeypatch in tests
    from security.kill_switches import requireEnabled
    requireEnabled("llm")

Audit log: every refusal writes a row via dashboard_api._audit_write with
action='killswitch_refusal', target=<switch_name>, outcome='disabled'. The audit
write is best-effort — if it fails, the kill-switch refusal still raises (we
prioritize the security action over the audit row).

Rule 1: refusal counter map is module-level state (in-memory). NO config.X
default-arg bind. No env-var reads at module load time — every call resolves env.

Rule 2: physical-state-first. The disabled state is read from os.environ on
EVERY call to requireEnabled — cached state would defeat operator-toggling.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Final

logger = logging.getLogger(__name__)

# Module-state refusal counters. Lock-protected for thread safety (FastAPI
# handlers run on a threadpool). Reset on process restart — Phase 7a accepts
# this; persistent counters are Phase 7b if operator UX demands.
#
# R1 M6 fix: process-local labeling via _PROCESS_STARTED_AT — /api/health
# exposes both fields so operators can see the counter is process-local
# (refusals before this timestamp may not be reflected in the count).
#
# R1 M7 fix: _AUDIT_WRITE_FAILURES counter tracks audit-write failures so
# operators can detect when the persistent record is silently failing.
_REFUSAL_COUNTERS: dict[str, int] = {}
_AUDIT_WRITE_FAILURES: dict[str, int] = {}  # per-switch audit-write failure count
_PROCESS_STARTED_AT: Final[float] = time.time()
_REFUSAL_LOCK: Final[threading.Lock] = threading.Lock()


class KillSwitchDisabled(Exception):
    """Raised when a kill-switch is in the disabled state.

    Callers catch this and degrade gracefully (chat returns "this feature is
    disabled by operator", reflection skips the run, etc.). NEVER swallow
    silently — the operator deliberately set the switch to disabled.
    """

    def __init__(self, switch_name: str, reason: str = "kill-switch disabled"):
        self.switch_name = switch_name
        self.reason = reason
        super().__init__(f"{reason}: {switch_name}")


def is_disabled(switch_name: str) -> bool:
    """Return True iff HOMIE_KILLSWITCH_<NAME> env var equals 'disabled' (ci).

    Read from os.environ on every call — no caching (Rule 2 — env state is
    derived from the operator's current setting, not a snapshot).
    """
    env_key = f"HOMIE_KILLSWITCH_{switch_name.upper()}"
    raw = os.environ.get(env_key, "").strip().lower()
    return raw == "disabled"


def requireEnabled(switch_name: str, *, caller: str = "") -> None:
    """Raise KillSwitchDisabled if the named switch is disabled.

    Increments per-switch refusal counter and writes an audit_log row before
    raising. The raise is the contract — callers MUST handle the exception
    (or let it propagate to a top-level handler).

    Rule 3: callers MUST import this via `from security import kill_switches`
    then `kill_switches.requireEnabled(...)`. Top-level `from
    security.kill_switches import requireEnabled` defeats monkeypatch in tests
    and is grep-tested as ZERO matches in production code.
    """
    if not is_disabled(switch_name):
        return

    # Refused — increment counter + audit + raise.
    with _REFUSAL_LOCK:
        _REFUSAL_COUNTERS[switch_name] = _REFUSAL_COUNTERS.get(switch_name, 0) + 1

    # Best-effort audit. Don't let an audit-write failure block the security action.
    # R1 M7 fix: count audit failures so operators can detect silent persistence loss.
    try:
        from dashboard_api import _audit_write  # late-bind — tests can monkeypatch
        _audit_write(
            operator_id="kill_switch_runtime",
            action="killswitch_refusal",
            target_persona_id=switch_name,
            outcome="disabled",
            detail={"caller": caller, "switch": switch_name},
            blocked=True,
        )
    except Exception as exc:  # noqa: BLE001 — audit best-effort
        logger.warning("kill-switch audit-write failed: %s", exc)
        with _REFUSAL_LOCK:
            _AUDIT_WRITE_FAILURES[switch_name] = _AUDIT_WRITE_FAILURES.get(switch_name, 0) + 1

    raise KillSwitchDisabled(
        switch_name=switch_name,
        reason=f"kill-switch '{switch_name}' is disabled by operator",
    )


def get_refusal_counters() -> dict[str, int]:
    """Return a snapshot of refusal counters for /api/health.

    Rule 2 enforcement: returns a COPY — caller cannot mutate the internal
    counter state. /api/health handler reads via this function.
    """
    with _REFUSAL_LOCK:
        return dict(_REFUSAL_COUNTERS)


def get_health_snapshot() -> dict:
    """Return the full kill-switch state snapshot for /api/health.

    R1 M6 fix: process-local labeling. Operators see process_started_at so
    they understand counters reset on restart. R1 M7 fix: audit_write_failures
    surfaces silent persistence failures.

    Shape:
        {
            "counters": {<switch>: <count>},
            "audit_write_failures": {<switch>: <count>},
            "process_started_at": <unix_timestamp>,
        }
    """
    with _REFUSAL_LOCK:
        return {
            "counters": dict(_REFUSAL_COUNTERS),
            "audit_write_failures": dict(_AUDIT_WRITE_FAILURES),
            "process_started_at": _PROCESS_STARTED_AT,
        }


__all__ = [
    "KillSwitchDisabled",
    "get_health_snapshot",
    "get_refusal_counters",
    "is_disabled",
    "requireEnabled",
]
