"""External watchdog for The Homie bot — the watcher of last resort.

Why this exists
---------------
On 2026-07-12 the bot was found wedged for ~6 weeks. Nothing noticed, because
nothing was looking:

* ``service.py`` (retired 2026-07, archived) was crash-only — it waited on
  ``proc.wait()``, and a wedged process never returns.
* The Windows scheduled task restarts on process *exit*, which likewise never
  came.
* ``heartbeat.py`` checks calendar/email/Asana — never the bot.
* Nothing anywhere polled ``:{port}/health``.

The in-bot liveness supervisor (``chat/liveness.py``) now catches a dead adapter
and fails fast, but it cannot catch a bot that is hard-hung, OOM-killed, or dead
before its supervisor ever starts. That is this script's job: poll /health from
*outside* the process and restart the bot when it stops answering truthfully.

Together they cover the whole failure surface:

    dead adapter, process healthy  -> in-bot supervisor self-heals, else exits
    process hung / gone / never up -> this watchdog restarts it

Design
------
* **``--once`` is the default.** The scheduled task fires this every few minutes;
  consecutive-failure counting and the restart budget live in a state file, so a
  short-lived process still has memory across runs. No long-lived daemon to
  supervise (a watchdog that can itself wedge is not a watchdog).
* **Restart budget** — a rolling-hour cap (default 5, same policy as
  ``service.py``). Exhausting it notifies the operator and STOPS restarting; a
  restart loop is worse than a down bot because it hides the real cause.
* **Grace window** — after a restart, failures are not counted while the bot
  boots. Diagnostics warm-up used to take seconds; ``status: "warming"`` is
  explicitly tolerated inside the grace window.
* **Fail-safe classification** — only a PROVEN-bad reading (unreachable, or a
  ``degraded`` status / a ``false`` adapter) counts as a failure. Anything the
  watchdog cannot interpret is treated as OK, because a watchdog that restarts
  on its own confusion is a self-inflicted outage.

Usage
-----
    uv run python bot_watchdog.py                # one poll (scheduled task)
    uv run python bot_watchdog.py --dry-run      # classify only, never restart
    uv run python bot_watchdog.py --json         # machine-readable verdict
    uv run python bot_watchdog.py --daemon       # continuous polling (debug)
    uv run python bot_watchdog.py --status       # show state file, no poll
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
CHAT_DIR = SCRIPTS_DIR.parent / "chat"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# Boot-shim: must run BEFORE any framework imports so profile paths resolve.
from personas import apply_persona_override  # noqa: E402

apply_persona_override()

import config  # noqa: E402
from shared import append_to_daily_log, file_lock  # noqa: E402

try:
    from notifications import send_toast_notification  # noqa: E402
except ImportError:  # pragma: no cover - notifications are optional
    send_toast_notification = None  # type: ignore[assignment]


# Verdicts
OK = "ok"
WARMING = "warming"
DEGRADED = "degraded"  # a GATEWAY is dead — restart-worthy
NONCRITICAL = "noncritical"  # something optional is down — report, never restart
UNREACHABLE = "unreachable"
DISABLED = "disabled"
STANDING_DOWN = "standing_down"  # operator desired state is OFF — no guard

# Only these two justify restarting the bot.
_BAD = {DEGRADED, UNREACHABLE}


def _now() -> datetime:
    return datetime.now()


def _log(message: str) -> None:
    print(f"[{_now()}] [watchdog] {message}", flush=True)


def _notify(title: str, message: str) -> None:
    """Operator alert. Fail-open — a broken toast must not break the watchdog."""
    if send_toast_notification is None:
        _log(f"NOTIFY (no notifier): {title} — {message}")
        return
    try:
        send_toast_notification(title, message, caller="bot_watchdog")
    except Exception as exc:  # noqa: BLE001
        _log(f"notification failed: {exc}")


# ---------------------------------------------------------------- state


def _state_path() -> Path:
    """Resolve at call time (Rule 1) so a profile swap moves the state file."""
    return config.BOT_WATCHDOG_STATE_FILE


def load_state() -> dict[str, Any]:
    """Read watchdog state. A corrupt/missing file yields a clean slate."""
    path = _state_path()
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("state file is not an object")
        return data
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        _log(f"state file unreadable ({exc}) — starting from a clean slate")
        return {}


def save_state(state: dict[str, Any]) -> None:
    """Atomic, locked write. Fail-open: a failed write never blocks a restart."""
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path, timeout=5.0):
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            tmp.replace(path)
    except Exception as exc:  # noqa: BLE001
        _log(f"could not persist state: {exc}")


def _recent_restarts(state: dict[str, Any], within_hours: float = 1.0) -> list[str]:
    """Restart timestamps inside the rolling window (physical list, not a counter)."""
    cutoff = _now() - timedelta(hours=within_hours)
    kept: list[str] = []
    for stamp in state.get("restarts", []):
        try:
            if datetime.fromisoformat(stamp) >= cutoff:
                kept.append(stamp)
        except (TypeError, ValueError):
            continue
    return kept


# ---------------------------------------------------------------- probe


def poll_health(url: str, timeout: float) -> tuple[str, dict[str, Any], str]:
    """Poll /health once.

    Returns ``(verdict, payload, detail)``. Never raises — an unreachable or
    unparseable endpoint IS the finding.
    """
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return UNREACHABLE, {}, f"HTTP {resp.status}"
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        return UNREACHABLE, {}, f"{type(exc).__name__}: {exc.reason}"
    except TimeoutError:
        return UNREACHABLE, {}, f"no response within {timeout}s"
    except Exception as exc:  # noqa: BLE001
        return UNREACHABLE, {}, f"{type(exc).__name__}: {exc}"

    if not isinstance(payload, dict):
        return UNREACHABLE, {}, "health payload is not an object"

    return classify(payload)


def _parse_last_update(value: Any) -> datetime | None:
    """Parse an adapter's ``last_update_at``. Unparseable => None (fail-safe).

    Accepts ISO strings (aware values converted to naive local) and numeric
    unix timestamps. Anything else is ignored — never a verdict input.
    """
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value))
        if isinstance(value, str) and value.strip():
            parsed = datetime.fromisoformat(value.strip())
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone().replace(tzinfo=None)
            return parsed
    except (ValueError, OverflowError, OSError, TypeError):
        return None
    return None


def classify(
    payload: dict[str, Any],
    staleness_seconds: float | None = None,
) -> tuple[str, dict[str, Any], str]:
    """Turn a /health payload into a verdict.

    Restart-worthiness is decided per-adapter by CRITICALITY, not by the summary
    ``status``:

    * A dead **gateway** (Telegram, Discord — the surfaces the operator talks
      through) means the bot is deaf. Restart it.
    * A dead **non-gateway** (the Mission Control relay) is reported but never
      restarted over. The relay dials OUT to an external service and redials
      itself; restarting the bot every 5 minutes while MC is down would turn
      someone else's outage into a self-inflicted restart loop.
    * An **event-stale gateway** (probes healthy, but ``last_update_at`` is
      hours old WHILE another adapter is fresh) is degraded — the fresh peer
      proves the bot and the operator are active, so the quiet one is broken,
      not idle. Both-quiet is NOT stale: a quiet bot is not a dead bot.
      (2026-07-15 Discord class: gateway task alive, event stream dead.)

    Fail-SAFE, not fail-fast: only a proven-bad reading is bad. A payload shape
    the watchdog does not understand resolves to OK, because restarting the bot
    on the watchdog's own confusion would be an outage we caused ourselves.

    ``staleness_seconds`` is a Rule 1 None sentinel — resolved from
    ``config.get_bot_watchdog_settings()`` at call time.
    """
    status = str(payload.get("status", "")).lower()
    adapters = payload.get("adapters")
    liveness = payload.get("adapter_liveness")

    if isinstance(adapters, dict) and not adapters:
        return DEGRADED, payload, "no adapters registered"

    if isinstance(liveness, dict) and liveness:
        dead_gateways = sorted(
            name
            for name, info in liveness.items()
            if isinstance(info, dict)
            and info.get("healthy") is False
            and info.get("critical", True)  # unknown criticality => treat as gateway
        )
        if dead_gateways:
            return DEGRADED, payload, f"gateway(s) proven dead: {', '.join(dead_gateways)}"

        # --- event staleness. Only adapters with an EXPLICIT critical=true and
        # a parseable last_update_at participate; missing/garbled timestamps
        # keep the fail-safe posture (no verdict from confusion).
        if staleness_seconds is None:
            staleness_seconds = config.get_bot_watchdog_settings().staleness_seconds
        now = _now()
        ages: dict[str, float] = {}
        for name, info in liveness.items():
            if not isinstance(info, dict):
                continue
            parsed = _parse_last_update(info.get("last_update_at"))
            if parsed is not None:
                ages[name] = (now - parsed).total_seconds()
        # Only a fresh CRITICAL gateway is evidence the operator is active on a
        # gateway surface. The relay bumps last_update_at on machine-adjacent
        # traffic (web.py:_enqueue), so a fresh non-critical peer proves nothing
        # about a quiet Telegram/Discord — a quiet bot is not a dead bot (#135).
        fresh = {
            name
            for name, age in ages.items()
            if age <= staleness_seconds
            and isinstance(liveness.get(name), dict)
            and liveness[name].get("critical") is True
        }
        stale_gateways = sorted(
            name
            for name, age in ages.items()
            if age > staleness_seconds
            and isinstance(liveness.get(name), dict)
            and liveness[name].get("critical") is True
            and (fresh - {name})  # at least one OTHER critical gateway is fresh
        )
        if stale_gateways:
            stale_desc = ", ".join(
                f"{name} {ages[name] / 3600:.1f}h stale" for name in stale_gateways
            )
            active_desc = ", ".join(sorted(fresh - set(stale_gateways))) or "peer"
            return (
                DEGRADED,
                payload,
                f"gateway event-stale: {stale_desc} while {active_desc} active",
            )

        dead_optional = sorted(
            name
            for name, info in liveness.items()
            if isinstance(info, dict)
            and info.get("healthy") is False
            and not info.get("critical", True)
        )
        if dead_optional:
            return (
                NONCRITICAL,
                payload,
                f"down but not restart-worthy: {', '.join(dead_optional)} "
                f"(gateways healthy; it reconnects on its own)",
            )
        if status in ("degraded", "error"):
            # Every gateway probed healthy, so whatever soured `status` is not
            # something a restart fixes.
            return NONCRITICAL, payload, f"health status={status}, but no gateway is dead"

    elif isinstance(adapters, dict):
        # Payload predates adapter_liveness — no criticality info, so treat every
        # adapter as a gateway (defense in depth: never silently ignore a false).
        dead = sorted(name for name, alive in adapters.items() if alive is False)
        if dead:
            return DEGRADED, payload, f"adapter(s) proven dead: {', '.join(dead)}"

    if status in ("degraded", "error"):
        return DEGRADED, payload, f"health status={status}"
    if status == "warming":
        return WARMING, payload, "diagnostics still warming up"
    return OK, payload, f"health status={status or 'ok'}"


# ---------------------------------------------------------------- restart


def _find_bash() -> str | None:
    """Locate a USABLE bash — Git Bash first, never WSL's System32 bash.exe.

    Under Task Scheduler, ``shutil.which("bash")`` resolves to
    ``C:\\Windows\\System32\\bash.exe`` (WSL) when the feature is installed.
    WSL bash treats backslashes in a Windows script path as escapes and dies
    with ``/bin/bash: ~...run_chat.sh: No such file or directory``.
    Captured live 2026-07-14: every watchdog restart of the morning outage
    failed exactly here — invisibly, until the launcher log existed. Prefer
    the known-good Git Bash installs; fall back to PATH only if it is not
    the System32 WSL shim.
    """
    for candidate in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ):
        if Path(candidate).exists():
            return candidate
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    return None


def restart_command() -> tuple[list[str], str] | None:
    """Pick the launcher. Returns ``(argv, label)``, or None when unlaunchable.

    ``run_chat.sh`` is the ONLY launcher. The old ``run_chat.bat`` fallback is
    retired (archived to ``.claude/_archive/lifecycle-2026-07/``): it hardcoded
    ``--telegram``, so a bash-missing machine got "recovered" into a silently
    Telegram-only bot with no Discord and no relay — a watchdog that restores
    the bot into a quietly degraded state is a slower version of the bug it
    exists to fix. No usable bash now means FAIL LOUD, never fall back.

    The launcher already kills the old profile-owned process, writes the pid
    file, and redirects logs, so we never re-implement spawn/kill/pid here.
    """
    bash = _find_bash()
    script = CHAT_DIR / "run_chat.sh"
    if bash and script.exists():
        return [bash, str(script)], "run_chat.sh (all adapters)"
    return None


def wait_for_healthy(url: str, timeout: float, deadline_seconds: float = 90.0) -> tuple[bool, str]:
    """Poll /health until the bot is actually serving again.

    Rule 2: the launcher's exit code says it *spawned* something. Only /health
    proves the bot came back — and only the adapter map proves it came back
    WHOLE. This is what caught the telegram-only .bat restart.
    """
    started = time.monotonic()
    last = "no response"
    while time.monotonic() - started < deadline_seconds:
        verdict, payload, detail = poll_health(url, timeout)
        if verdict in (OK, WARMING):
            adapters = payload.get("adapters") or {}
            names = ",".join(sorted(adapters)) or "none"
            return True, f"back up in {time.monotonic() - started:.0f}s (adapters: {names})"
        last = detail
        time.sleep(3)
    return False, f"did not become healthy within {deadline_seconds:.0f}s (last: {last})"


_LAUNCHER_LOG_MAX_BYTES = 200 * 1024
_LAUNCHER_LOG_KEEP_BYTES = 100 * 1024


def _launcher_log_path() -> Path:
    """Resolve at call time (Rule 1) — follows the profile with the state file."""
    return _state_path().parent / "watchdog-launcher.log"


def _append_launcher_header(header: str) -> Path:
    """Rotate the launcher log (cap ~200KB, keep the tail) + timestamped header.

    Append-only: the old ``"w"`` open truncated the log on every restart,
    destroying the forensics for every restart but the last one. Rotation
    idiom mirrors ``shared.log_hook_execution`` (read, keep tail, rewrite).
    """
    path = _launcher_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > _LAUNCHER_LOG_MAX_BYTES:
            text = path.read_text(encoding="utf-8", errors="replace")
            path.write_text(text[-_LAUNCHER_LOG_KEEP_BYTES:], encoding="utf-8")
        with open(path, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(f"\n===== {_now().isoformat()} | {header} =====\n")
    except Exception as exc:  # noqa: BLE001 — a broken receipt must not block a restart
        _log(f"launcher log unavailable ({exc})")
    return path


def restart_bot() -> tuple[bool, str]:
    """Run the launcher, then PROVE the bot came back. Returns (ok, detail)."""
    picked = restart_command()
    if picked is None:
        # Git Bash missing (or run_chat.sh gone): FAIL LOUD — toast, launcher-log
        # receipt, no restart attempt. The retired run_chat.bat fallback used to
        # "succeed" here into a Telegram-only bot; a loud stop is the fix.
        detail = (
            "Git Bash not found (or run_chat.sh missing) — cannot restart the bot. "
            "Install Git for Windows; there is NO fallback launcher (run_chat.bat retired)."
        )
        _append_launcher_header(f"RESTART ABORTED: {detail}")
        _notify("The Homie Bot restart BLOCKED", detail)
        return False, detail

    cmd, label = picked
    _log(f"restarting bot via {label}")

    # A file handle, never PIPE. The launcher spawns the bot as a detached
    # grandchild that inherits our handles; with capture_output=True the
    # pipe never reaches EOF (the bot holds it open for its whole life) and
    # subprocess.run blocks forever — even the timeout cannot save it,
    # because the post-kill communicate() blocks on the same pipe. A
    # watchdog that hangs inside its own restart is worse than no watchdog.
    # Verified live: this call hung for >4 minutes before the fix.
    #
    # But DEVNULL swallowed the launcher's own errors: on 2026-07-14 every
    # restart died at run_chat.sh's "Service resolver failed" exit and the
    # watchdog had zero receipts — the whole morning was 5-restart budget
    # exhaustion with nothing to read. A file has no EOF-wait problem and
    # keeps the receipt.
    launcher_log = _append_launcher_header(f"restart via {label}")
    try:
        with open(launcher_log, "a", encoding="utf-8", errors="replace") as log_fh:
            subprocess.run(  # noqa: S603
                cmd,
                cwd=str(CHAT_DIR),
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=log_fh,
                timeout=120,
                check=False,
            )
    except subprocess.TimeoutExpired:
        return False, f"{label}: launcher timed out after 120s (see {launcher_log})"
    except Exception as exc:  # noqa: BLE001
        return False, f"{label}: launcher failed: {type(exc).__name__}: {exc}"

    settings = config.get_bot_watchdog_settings()
    ok, detail = wait_for_healthy(settings.health_url, settings.timeout_seconds)
    return ok, f"{label}: {detail}"


# ---------------------------------------------------------------- the check


def _read_desired_state() -> str:
    """Operator desired state. Fail-OPEN to "on" — a broken flag never kills the guard."""
    try:
        import bot_lifecycle_switch  # local import — keeps the guard import-safe

        return str(bot_lifecycle_switch.get_desired().get("desired", "on")).lower()
    except Exception as exc:  # noqa: BLE001 — guard survives a broken switch
        _log(f"desired-state read failed ({exc}) — guarding as if desired=on")
        return "on"


def run_once(*, dry_run: bool = False) -> dict[str, Any]:
    """One poll → classify → (maybe) restart. Returns a machine-readable verdict."""
    settings = config.get_bot_watchdog_settings()
    state = load_state()

    if not settings.enabled:
        return {"verdict": DISABLED, "detail": "BOT_WATCHDOG_ENABLED=false", "restarted": False}

    # --- #117 ONE switch, ONE enforcer: desired=off => stand down. No health
    # poll, no failure counting, no restart. Fail-OPEN to "on" on ANY error —
    # a broken flag must never kill the guard.
    desired = _read_desired_state()
    if desired == "off":
        _log("standing down — operator desired state is OFF (`thehomie on` to resume)")
        state["last_poll_at"] = _now().isoformat()
        state["last_verdict"] = STANDING_DOWN
        state["last_detail"] = "desired=off (bot-desired-state.json)"
        state["consecutive_failures"] = 0
        save_state(state)
        return {
            "verdict": STANDING_DOWN,
            "detail": "desired=off — watchdog standing down",
            "restarted": False,
        }

    verdict, payload, detail = poll_health(settings.health_url, settings.timeout_seconds)

    # --- grace window: a bot that was just restarted is allowed to be boot-slow.
    in_grace = False
    last_restart = state.get("last_restart_at")
    if last_restart:
        try:
            age = (_now() - datetime.fromisoformat(last_restart)).total_seconds()
            in_grace = age < settings.grace_seconds
        except (TypeError, ValueError):
            in_grace = False

    # A bot still "warming" LONG after boot is not warming, it is stuck. Uptime
    # comes from the payload itself (physical, not inferred).
    uptime = float(payload.get("uptime_seconds") or 0.0)
    if verdict == WARMING and uptime > settings.grace_seconds:
        verdict = DEGRADED
        detail = f"stuck warming for {uptime:.0f}s (> {settings.grace_seconds:.0f}s grace)"

    bad = verdict in _BAD
    if bad and in_grace:
        _log(f"{verdict}: {detail} — inside post-restart grace window, not counting")
        bad = False

    failures = int(state.get("consecutive_failures", 0))
    failures = failures + 1 if bad else 0

    state["consecutive_failures"] = failures
    state["last_poll_at"] = _now().isoformat()
    state["last_verdict"] = verdict
    state["last_detail"] = detail

    result: dict[str, Any] = {
        "verdict": verdict,
        "detail": detail,
        "consecutive_failures": failures,
        "threshold": settings.failure_threshold,
        "health_url": settings.health_url,
        "restarted": False,
        "in_grace": in_grace,
    }

    if not bad:
        _log(f"{verdict}: {detail}")
        save_state(state)
        return result

    _log(f"{verdict}: {detail} (failure {failures}/{settings.failure_threshold})")

    if failures < settings.failure_threshold:
        save_state(state)
        return result

    # --- threshold crossed: restart, subject to the rolling-hour budget.
    recent = _recent_restarts(state)
    if len(recent) >= settings.max_restarts_per_hour:
        result["restart_blocked"] = "budget_exhausted"
        _log(
            f"RESTART BUDGET EXHAUSTED ({len(recent)}/{settings.max_restarts_per_hour} "
            f"in the last hour) — NOT restarting. Manual intervention needed."
        )
        _notify(
            "The Homie Bot is DOWN",
            f"Bot is {verdict} ({detail}) but the watchdog has already restarted it "
            f"{len(recent)} times this hour. Not restarting again — something is "
            f"badly broken. Check .claude/data/bot.log.",
        )
        append_to_daily_log(
            f"Watchdog: bot {verdict} ({detail}) — restart budget exhausted, manual fix needed",
            "Bot Lifecycle",
        )
        state["restarts"] = recent
        save_state(state)
        return result

    if dry_run:
        result["restart_blocked"] = "dry_run"
        _log("DRY RUN — would restart the bot now")
        save_state(state)
        return result

    # #135: the flag was read at the top of this tick, but the poll takes time
    # and `thehomie off` must win. Re-read at the last instant before spawning.
    if _read_desired_state() == "off":
        result["restart_blocked"] = "desired_off"
        _log("NOT restarting — desired flipped OFF since this tick began")
        save_state(state)
        return result

    ok, restart_detail = restart_bot()

    undone = False
    undo_note = ""
    if _read_desired_state() == "off":
        # Operator ran `thehomie off` while the restart was in flight. #117:
        # off => NEVER resurrect — sweep regardless of `ok`: restart_bot()
        # spawns a DETACHED process and then verifies, so ok=False can still
        # mean a live bot was spawned (launcher ran, health proof failed).
        try:
            import shared as _shared  # module-attr call (Rule 3) — tests monkeypatch

            killed = _shared.cleanup_all_bot_processes()
            if killed:
                undo_note = f"UNDONE: desired=off during restart; killed {killed}"
            else:
                # Honest: nothing matched the sweep. Either the spawn already
                # exited (fine) or enumeration failed closed (bot may live —
                # `thehomie off` re-runs the same sweep).
                undo_note = (
                    "UNDONE: desired=off during restart; no surviving process "
                    "found by the sweep"
                )
            result["restart_undone"] = "desired_off"
        except Exception as exc:  # noqa: BLE001 — report loudly; nothing retries this
            undo_note = (
                f"UNDO FAILED: {type(exc).__name__}: {exc} — a resurrected bot "
                "may still be running; run `thehomie off` again"
            )
            result["restart_undo_failed"] = f"{type(exc).__name__}: {exc}"
        restart_detail = f"{restart_detail} — {undo_note}"
        undone = True
        ok = False

    result["restarted"] = ok
    result["restart_detail"] = restart_detail

    recent.append(_now().isoformat())
    state["restarts"] = recent
    if ok:
        # #135: grace protects a BOOTING bot. A failed (or undone) restart has
        # nothing to protect — arming grace would suppress the very failure
        # counting that drives the next recovery attempt.
        state["last_restart_at"] = _now().isoformat()
    state["last_restart_detail"] = restart_detail
    # Reset the counter either way: the next poll re-judges from scratch, and a
    # failed launcher must not instantly re-trip the threshold on the next tick.
    state["consecutive_failures"] = 0
    save_state(state)

    if undone:
        _log(f"restart undone ({restart_detail})")
        # The toast tells the truth about what the sweep achieved — a failed
        # undo must never read as "stopped" (#135 gate finding).
        _notify(
            "The Homie Bot restart undone"
            if "UNDO FAILED" not in undo_note
            else "The Homie Bot restart undo FAILED",
            f"`thehomie off` landed during an in-flight watchdog restart. {undo_note}",
        )
        append_to_daily_log(
            f"Watchdog undo — desired=off won mid-flight: {restart_detail}",
            "Bot Lifecycle",
        )
    elif ok:
        _log(f"bot restarted ({restart_detail})")
        _notify(
            "The Homie Bot was restarted",
            f"Watchdog found the bot {verdict} ({detail}) and restarted it. "
            f"Restart {len(recent)}/{settings.max_restarts_per_hour} this hour.",
        )
        append_to_daily_log(
            f"Watchdog restarted the bot — was {verdict}: {detail}", "Bot Lifecycle"
        )
    else:
        _log(f"RESTART FAILED: {restart_detail}")
        _notify(
            "The Homie Bot restart FAILED",
            f"Bot is {verdict} ({detail}) and the watchdog could not restart it: "
            f"{restart_detail}",
        )
        append_to_daily_log(
            f"Watchdog restart FAILED — bot {verdict}: {restart_detail}", "Bot Lifecycle"
        )

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Watchdog for The Homie bot")
    parser.add_argument("--once", action="store_true", help="single poll (default)")
    parser.add_argument("--daemon", action="store_true", help="poll continuously")
    parser.add_argument(
        "--interval", type=int, default=300, help="daemon poll interval (seconds)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="classify only; never restart"
    )
    parser.add_argument("--json", action="store_true", help="emit the verdict as JSON")
    parser.add_argument(
        "--status", action="store_true", help="print the state file and exit"
    )
    args = parser.parse_args()

    if args.status:
        print(json.dumps(load_state(), indent=2))
        return 0

    if args.daemon:
        _log(f"daemon mode — polling every {args.interval}s")
        while True:
            try:
                run_once(dry_run=args.dry_run)
            except Exception as exc:  # noqa: BLE001 — the watchdog never dies
                _log(f"unexpected error: {type(exc).__name__}: {exc}")
            time.sleep(args.interval)

    result = run_once(dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result, indent=2))

    # Exit 0 on a healthy bot OR a successful restart; 1 when the bot is bad and
    # still bad (so Task Scheduler's Last Run Result surfaces the problem).
    # NONCRITICAL exits 0: the BOT is fine (an optional link is down), and
    # flagging a Mission Control outage as a watchdog failure every 5 minutes is
    # alarm fatigue, not signal. It is still recorded in the state file + log.
    # STANDING_DOWN exits 0: the operator turned the bot OFF on purpose.
    if (
        result["verdict"] in (OK, WARMING, DISABLED, NONCRITICAL, STANDING_DOWN)
        or result.get("restarted")
    ):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
