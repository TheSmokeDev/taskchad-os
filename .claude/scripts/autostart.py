"""Bot autostart at logon — the operator-facing toggle.

The external watchdog (``bot_watchdog.py``) only RECOVERS a bot that died;
nothing STARTS one after a reboot unless the operator opts in here (the
2026-07-14 outage: a 6:19 AM reboot orphaned the bot all morning).

``status()`` / ``enable()`` / ``disable()`` are the single implementation
behind all three surfaces — the ``/autostart`` chat command, the
``thehomie autostart`` CLI group, and the dashboard Settings toggle. None of
them carry their own schtasks logic.

State is the OS task registry itself (Rule 2): ``status()`` asks Task
Scheduler, never a config flag or DB row — a task deleted by hand in the
Task Scheduler GUI reads as disabled here with no drift. (Existence, not
enabled/disabled sub-state: a task manually Disabled in the GUI still reads
as on; ``enable()`` always overwrites, so correctness never depends on it.)

Windows-only in V1. macOS/Linux report ``supported=False`` cleanly and never
spawn a subprocess.
"""

from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path

import config
from security import kill_switches

_SCRIPTS_DIR = Path(__file__).resolve().parent

# PowerShell scripts are STATIC strings — operator-controlled values (task
# name, .bat path) travel via environment variables, never string
# interpolation (same injection posture as notifications._notify_windows).
#
# ExecutionTimeLimit is 5 MINUTES, deliberately not the 365-day limit the
# legacy setup_bot_scheduler.ps1 uses: run_bot_start.bat exits in seconds
# (run_chat.sh detaches the bot), so a wedged Git Bash gets reaped without
# ever touching the detached bot process.
#
# Trigger delay PT1M: give the desktop/network a minute after logon before
# the launcher fires (matches the hotfix task registered live 2026-07-14).
_ENABLE_SCRIPT = """
$ErrorActionPreference = 'Stop'
$name = $env:HOMIE_AUTOSTART_TASK
$bat = $env:HOMIE_AUTOSTART_BAT
$wd = $env:HOMIE_AUTOSTART_WORKDIR
if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
}
$action = New-ScheduledTaskAction -Execute $bat -WorkingDirectory $wd
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = 'PT1M'
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Start The Homie bot at logon (run_bot_start.bat -> run_chat.sh, all adapters); the watchdog only recovers, this task starts' | Out-Null
"""

_DISABLE_SCRIPT = """
$ErrorActionPreference = 'Stop'
$name = $env:HOMIE_AUTOSTART_TASK
if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    'removed'
} else {
    'absent'
}
"""


def _base_result(settings: config.BotAutostartSettings) -> dict:
    system = platform.system()
    return {
        "supported": system == "Windows",
        "enabled": False,
        "task_name": settings.task_name,
        "platform": system,
        "detail": "",
    }


def _run_powershell(script: str, env: dict, timeout: float) -> tuple[bool, str]:
    """Run a static PowerShell script. Returns ``(ok, detail)``.

    ``capture_output=True`` is SAFE here: the ScheduledTask cmdlets talk to
    the Task Scheduler service over RPC and spawn no detached children, so
    nothing inherits the pipes (unlike the run_chat.sh launcher trap, where
    the detached bot held stdout open forever).
    """
    try:
        proc = subprocess.run(  # noqa: S603
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return False, f"powershell timed out after {timeout:.0f}s"
    except Exception as exc:  # noqa: BLE001 — surfaces as ok=False, never raises
        return False, f"powershell launch failed: {type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        return False, tail or f"powershell exited {proc.returncode}"
    return True, (proc.stdout or "").strip()


def _audit(action: str, outcome: str, caller: str, detail: str, task_name: str) -> None:
    """Best-effort audit row — a failed audit must never block the mutation."""
    try:
        from dashboard_api import _audit_write  # late-bind — tests monkeypatch

        _audit_write(
            operator_id="autostart_runtime",
            action=action,
            target_persona_id=task_name,
            outcome=outcome,
            detail={"caller": caller, "detail": detail},
        )
    except Exception as exc:  # noqa: BLE001 — audit best-effort
        print(f"[autostart] audit write failed: {exc}")


def status() -> dict:
    """Physical autostart state: does the Task Scheduler task exist?

    Exit-code-only check (``schtasks /query /tn`` returns 0 iff the task
    exists) — the localized schtasks output text is never parsed.
    """
    settings = config.get_bot_autostart_settings()
    result = _base_result(settings)
    if not result["supported"]:
        result["detail"] = "autostart is only supported on Windows (V1)"
        return result
    try:
        proc = subprocess.run(  # noqa: S603
            ["schtasks", "/query", "/tn", settings.task_name],
            capture_output=True,
            timeout=settings.timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 — a broken query reads as disabled
        result["detail"] = f"schtasks query failed: {type(exc).__name__}: {exc}"
        return result
    result["enabled"] = proc.returncode == 0
    result["detail"] = (
        "task registered (at logon)" if result["enabled"] else "task not registered"
    )
    return result


def enable(*, caller: str = "") -> dict:
    """Register the at-logon task (idempotent — always Unregister+Register).

    Raises ``kill_switches.KillSwitchDisabled`` when the operator has set
    ``HOMIE_KILLSWITCH_AUTOSTART=disabled``; every other failure returns an
    ``ok=False`` result dict.
    """
    kill_switches.requireEnabled("autostart", caller=caller)
    settings = config.get_bot_autostart_settings()
    result = _base_result(settings)
    result["ok"] = False
    if not result["supported"]:
        result["detail"] = "autostart is only supported on Windows (V1)"
        return result

    bat = _SCRIPTS_DIR / "run_bot_start.bat"
    if not bat.is_file():
        result["detail"] = f"launcher missing: {bat}"
        _audit("autostart_enable", "failed", caller, result["detail"], settings.task_name)
        return result

    env = {
        **os.environ,
        "HOMIE_AUTOSTART_TASK": settings.task_name,
        "HOMIE_AUTOSTART_BAT": str(bat),
        "HOMIE_AUTOSTART_WORKDIR": str(bat.parent),
    }
    ok, detail = _run_powershell(_ENABLE_SCRIPT, env, settings.timeout_seconds)
    result["ok"] = ok
    result["enabled"] = status()["enabled"]  # post-state re-read (Rule 2)
    result["detail"] = (
        "task registered (at logon, runs run_bot_start.bat)" if ok else detail
    )
    _audit(
        "autostart_enable",
        "succeeded" if ok else "failed",
        caller,
        result["detail"],
        settings.task_name,
    )
    return result


def disable(*, caller: str = "") -> dict:
    """Unregister the at-logon task. Idempotent — absent task is ``ok=True``.

    Raises ``kill_switches.KillSwitchDisabled`` when the operator has set
    ``HOMIE_KILLSWITCH_AUTOSTART=disabled``; every other failure returns an
    ``ok=False`` result dict.
    """
    kill_switches.requireEnabled("autostart", caller=caller)
    settings = config.get_bot_autostart_settings()
    result = _base_result(settings)
    result["ok"] = False
    if not result["supported"]:
        result["detail"] = "autostart is only supported on Windows (V1)"
        return result

    env = {**os.environ, "HOMIE_AUTOSTART_TASK": settings.task_name}
    ok, out = _run_powershell(_DISABLE_SCRIPT, env, settings.timeout_seconds)
    result["ok"] = ok
    result["enabled"] = status()["enabled"]  # post-state re-read (Rule 2)
    if ok:
        result["detail"] = (
            "task removed" if "removed" in out else "task was not registered"
        )
    else:
        result["detail"] = out
    _audit(
        "autostart_disable",
        "succeeded" if ok else "failed",
        caller,
        result["detail"],
        settings.task_name,
    )
    return result
