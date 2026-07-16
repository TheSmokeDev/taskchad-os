"""Bot desired-state switch — issue #117, ONE switch, ONE enforcer.

Single source of truth for "should the bot be running":
``STATE_DIR/bot-desired-state.json``:

    {"desired": "on"|"off", "changed_by": str, "changed_at": iso}

Missing/corrupt file = "on" — preserves the pre-switch always-guarded
behavior, so a broken flag can never silently stand the watchdog down.

The flag is DESIRED state (operator intent), never a claim of actual state
(Rule 2 — the watchdog and ``is_pid_alive`` read physical state; this file
only says what the operator WANTS). One writer surface family, one reader:

    turn_on()/turn_off()          — CLI ``thehomie on|off`` (+ future /homie)
    dashboard activate/deactivate — best-effort flag write (default profile)
    bot_watchdog.run_once()       — the ONE enforcer; desired=off => stand down

``get_desired()`` is ungated; ``turn_on``/``turn_off`` are gated by the
``bot_lifecycle`` kill-switch (HOMIE_KILLSWITCH_BOT_LIFECYCLE=disabled).

Library module — no boot-shim here (entrypoints run apply_persona_override
before importing this, same contract as ``autostart.py``).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import config

# Rule 3 module-attribute lookup — `import shared` (NOT `from shared import X`)
# so test monkeypatches of shared.read_pid / shared.is_pid_alive /
# shared.cleanup_all_bot_processes propagate.
import shared
from security import kill_switches

_VALID_DESIRED = ("on", "off")


def _flag_path() -> Path:
    # Rule 1 — resolve config.STATE_DIR at call time so a profile swap (or a
    # test monkeypatch) moves the flag with it.
    return config.STATE_DIR / "bot-desired-state.json"


def _audit(action: str, outcome: str, caller: str, detail: str) -> None:
    """Best-effort audit row — a failed audit must never block the mutation."""
    try:
        from dashboard_api import _audit_write  # late-bind — tests monkeypatch

        _audit_write(
            operator_id="bot_lifecycle_switch",
            action=action,
            target_persona_id="bot",
            outcome=outcome,
            detail={"caller": caller, "detail": detail},
        )
    except Exception as exc:  # noqa: BLE001 — audit best-effort
        print(f"[bot_lifecycle_switch] audit write failed: {exc}")


def get_desired() -> dict:
    """Read the desired state. Missing/corrupt/unknown file => "on".

    Fail-open to "on" is the safety posture: the watchdog only stands down
    on an EXPLICIT, well-formed "off" — anything less keeps the guard up.
    """
    defaults = {"desired": "on", "changed_by": "", "changed_at": ""}
    try:
        state = shared.load_state(_flag_path())
    except Exception:  # noqa: BLE001 — a broken flag must never break a reader
        return defaults
    desired = str(state.get("desired", "")).strip().lower()
    if desired not in _VALID_DESIRED:
        return defaults
    return {
        "desired": desired,
        "changed_by": str(state.get("changed_by", "")),
        "changed_at": str(state.get("changed_at", "")),
    }


def set_desired(desired: str, changed_by: str) -> dict:
    """Atomically persist the desired state + audit row. Returns the payload."""
    if desired not in _VALID_DESIRED:
        raise ValueError(f"desired must be one of {_VALID_DESIRED}, got {desired!r}")
    path = _flag_path()
    payload = {
        "desired": desired,
        "changed_by": changed_by,
        "changed_at": datetime.now().isoformat(),
    }
    with shared.file_lock(path, timeout=5.0):
        shared.save_state(payload, path)
    _audit(f"bot_desired_{desired}", "succeeded", changed_by, f"flag={path}")
    return payload


def turn_on(changed_by: str = "") -> dict:
    """Set desired=on; start the bot if no live process exists.

    Raises ``kill_switches.KillSwitchDisabled`` when the operator has set
    ``HOMIE_KILLSWITCH_BOT_LIFECYCLE=disabled``. Every other failure returns
    an ``ok=False`` result dict.

    The spawn path is the SAME one the watchdog uses (``bot_watchdog.
    restart_bot`` — Git Bash + run_chat.sh, launcher output to the rotating
    receipt log, /health-verified) so there is exactly one launcher path in
    the framework.
    """
    kill_switches.requireEnabled("bot_lifecycle", caller=changed_by)
    set_desired("on", changed_by)
    result: dict = {
        "ok": True,
        "desired": "on",
        "started": False,
        "pid": None,
        "detail": "",
    }

    pid = shared.read_pid()  # active-profile pid path (Rule 2 physical check)
    if pid is not None and shared.is_pid_alive(pid):
        result["pid"] = pid
        result["detail"] = f"bot already running (pid {pid})"
        return result

    import bot_watchdog  # lazy — avoids an import cycle with run_once()

    ok, detail = bot_watchdog.restart_bot()
    result["ok"] = ok
    result["started"] = ok
    result["detail"] = detail
    if ok:
        result["pid"] = shared.read_pid()
    return result


def turn_off(changed_by: str = "") -> dict:
    """Set desired=off; stop the running bot (profile-aware).

    Raises ``kill_switches.KillSwitchDisabled`` when the operator has set
    ``HOMIE_KILLSWITCH_BOT_LIFECYCLE=disabled``. Every other failure returns
    an ``ok=False`` result dict — the flag write still lands first, so the
    watchdog stands down even if the kill sweep fails.
    """
    kill_switches.requireEnabled("bot_lifecycle", caller=changed_by)
    set_desired("off", changed_by)
    result: dict = {"ok": True, "desired": "off", "stopped": [], "detail": ""}
    try:
        killed = shared.cleanup_all_bot_processes()
        result["stopped"] = killed
        result["detail"] = (
            f"stopped {len(killed)} process(es): {killed}"
            if killed
            else "no running bot found"
        )
    except Exception as exc:  # noqa: BLE001 — flag already off; report the sweep failure
        result["ok"] = False
        result["detail"] = f"stop failed: {type(exc).__name__}: {exc}"
    return result
