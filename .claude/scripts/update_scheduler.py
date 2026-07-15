"""Native daily scheduler for the safe framework updater.

Linux uses a real systemd timer with an IANA timezone and Persistent=true.
Windows uses Task Scheduler at local wall-clock time.  Docker is check-only:
replacing a container remains the orchestrator's responsibility.
"""

from __future__ import annotations

import json
import os
import platform
import shlex
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

DEFAULT_TASK_NAME = "TheHomie-AutoUpdate"
DEFAULT_UNIT_NAME = "thehomie-auto-update"


def _settings() -> dict[str, str]:
    return {
        "time": os.getenv("HOMIE_UPDATE_TIME", "04:00").strip(),
        "timezone": os.getenv("HOMIE_UPDATE_TIMEZONE", "America/Los_Angeles").strip(),
        "task_name": os.getenv("HOMIE_UPDATE_TASK_NAME", DEFAULT_TASK_NAME).strip(),
        "unit_name": os.getenv("HOMIE_UPDATE_SYSTEMD_UNIT", DEFAULT_UNIT_NAME).strip(),
        "scope": os.getenv("HOMIE_UPDATE_SYSTEMD_SCOPE", "user").strip().lower(),
        "user": os.getenv("HOMIE_UPDATE_USER", "").strip(),
    }


def _is_docker() -> bool:
    if Path("/.dockerenv").exists():
        return True
    try:
        return "docker" in Path("/proc/1/cgroup").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def _run(argv: Sequence[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        text=True,
        capture_output=True,
        check=check,
    )


def _worker_argv(repo_root: Path) -> list[str]:
    worker = repo_root / ".claude" / "scripts" / "update_worker.py"
    request_file = repo_root / ".claude" / "data" / "state" / "framework-update-request.json"
    return [
        sys.executable,
        str(worker),
        "--repo",
        str(repo_root),
        "--scheduled",
        "--restart",
        "--requester-file",
        str(request_file),
    ]


def _systemd_paths(settings: dict[str, str]) -> tuple[Path, str, list[str]]:
    unit = settings["unit_name"]
    if settings["scope"] == "system":
        return Path("/etc/systemd/system"), unit, ["systemctl"]
    return Path.home() / ".config" / "systemd" / "user", unit, ["systemctl", "--user"]


def _systemd_unit_text(repo_root: Path, settings: dict[str, str]) -> tuple[str, str]:
    command = " ".join(shlex.quote(part) for part in _worker_argv(repo_root))
    user_line = (
        f"User={settings['user']}\n" if settings["scope"] == "system" and settings["user"] else ""
    )
    service = (
        "[Unit]\n"
        "Description=The Homie safe stable-release updater\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"{user_line}"
        f"WorkingDirectory={repo_root}\n"
        f"ExecStart={command}\n"
        "Nice=10\n"
    )
    hour, minute = settings["time"].split(":", 1)
    timer = (
        "[Unit]\n"
        "Description=Daily stable YourProduct OS update check\n\n"
        "[Timer]\n"
        f"OnCalendar=*-*-* {int(hour):02d}:{int(minute):02d}:00 {settings['timezone']}\n"
        "Persistent=true\n"
        f"Unit={settings['unit_name']}.service\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service, timer


def _linux_status(repo_root: Path, settings: dict[str, str]) -> dict[str, Any]:
    unit_dir, unit, systemctl = _systemd_paths(settings)
    timer_path = unit_dir / f"{unit}.timer"
    service_path = unit_dir / f"{unit}.service"
    query = _run([*systemctl, "is-enabled", f"{unit}.timer"])
    enabled = query.returncode == 0 and query.stdout.strip() == "enabled"
    next_run = ""
    if enabled:
        show = _run(
            [
                *systemctl,
                "show",
                f"{unit}.timer",
                "--property=NextElapseUSecRealtime",
                "--value",
            ]
        )
        next_run = show.stdout.strip()
    return {
        "supported": True,
        "platform": "linux-systemd",
        "enabled": enabled,
        "time": settings["time"],
        "timezone": settings["timezone"],
        "persistent": True,
        "unit": f"{unit}.timer",
        "scope": settings["scope"],
        "next_run": next_run or None,
        "service_path": str(service_path),
        "timer_path": str(timer_path),
        "repo_root": str(repo_root),
        "detail": (query.stderr or query.stdout).strip(),
    }


def _windows_status(repo_root: Path, settings: dict[str, str]) -> dict[str, Any]:
    result = _run(["schtasks", "/Query", "/TN", settings["task_name"], "/FO", "LIST", "/V"])
    enabled = result.returncode == 0 and "disabled" not in result.stdout.lower()
    next_run = None
    for line in result.stdout.splitlines():
        if line.lower().startswith("next run time:"):
            next_run = line.split(":", 1)[1].strip()
            break
    return {
        "supported": True,
        "platform": "windows-task-scheduler",
        "enabled": enabled,
        "time": settings["time"],
        "timezone": settings["timezone"],
        "persistent": True,
        "task_name": settings["task_name"],
        "next_run": next_run,
        "repo_root": str(repo_root),
        "detail": (result.stderr or result.stdout).strip(),
    }


def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _windows_enable_script(repo_root: Path, settings: dict[str, str]) -> str:
    argv = _worker_argv(repo_root)
    executable = _powershell_literal(argv[0])
    arguments = _powershell_literal(subprocess.list2cmdline(argv[1:]))
    task_name = _powershell_literal(settings["task_name"])
    start_at = _powershell_literal(settings["time"])
    return (
        f"$action = New-ScheduledTaskAction -Execute {executable} -Argument {arguments}; "
        f"$trigger = New-ScheduledTaskTrigger -Daily -At {start_at}; "
        "$taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries; "
        f"Register-ScheduledTask -TaskName {task_name} -Action $action -Trigger $trigger "
        "-Settings $taskSettings -Force | Out-Null"
    )


def status(repo_root: str | Path) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    settings = _settings()
    if _is_docker():
        return {
            "supported": False,
            "platform": "docker",
            "enabled": False,
            "check_only": True,
            "time": settings["time"],
            "timezone": settings["timezone"],
            "persistent": False,
            "detail": (
                "container deployments are check-only; replace the container via its orchestrator"
            ),
        }
    if platform.system() == "Windows":
        return _windows_status(root, settings)
    if shutil_which("systemctl"):
        return _linux_status(root, settings)
    return {
        "supported": False,
        "platform": platform.system().lower(),
        "enabled": False,
        "time": settings["time"],
        "timezone": settings["timezone"],
        "persistent": False,
        "detail": "no supported native scheduler found",
    }


def shutil_which(command: str) -> str | None:
    # Kept as a tiny seam for unit tests without mocking the whole shutil module.
    import shutil

    return shutil.which(command)


def enable(repo_root: str | Path) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    settings = _settings()
    current = status(root)
    if not current["supported"]:
        return current
    if platform.system() == "Windows":
        result = _run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _windows_enable_script(root, settings),
            ]
        )
        if result.returncode != 0:
            return {**current, "ok": False, "detail": (result.stderr or result.stdout).strip()}
        return {**_windows_status(root, settings), "ok": True}

    unit_dir, unit, systemctl = _systemd_paths(settings)
    service, timer = _systemd_unit_text(root, settings)
    try:
        unit_dir.mkdir(parents=True, exist_ok=True)
        (unit_dir / f"{unit}.service").write_text(service, encoding="utf-8")
        (unit_dir / f"{unit}.timer").write_text(timer, encoding="utf-8")
    except OSError as exc:
        return {**current, "ok": False, "detail": f"cannot install systemd units: {exc}"}
    reload_result = _run([*systemctl, "daemon-reload"])
    enable_result = _run([*systemctl, "enable", "--now", f"{unit}.timer"])
    if reload_result.returncode != 0 or enable_result.returncode != 0:
        detail = (enable_result.stderr or reload_result.stderr or enable_result.stdout).strip()
        return {**_linux_status(root, settings), "ok": False, "detail": detail}
    return {**_linux_status(root, settings), "ok": True}


def disable(repo_root: str | Path) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    settings = _settings()
    current = status(root)
    if platform.system() == "Windows":
        result = _run(["schtasks", "/Delete", "/F", "/TN", settings["task_name"]])
        ok = result.returncode == 0 or "cannot find" in (result.stderr + result.stdout).lower()
        return {**_windows_status(root, settings), "ok": ok}
    if not current["supported"]:
        return current
    unit_dir, unit, systemctl = _systemd_paths(settings)
    result = _run([*systemctl, "disable", "--now", f"{unit}.timer"])
    for suffix in ("service", "timer"):
        try:
            (unit_dir / f"{unit}.{suffix}").unlink(missing_ok=True)
        except OSError:
            pass
    _run([*systemctl, "daemon-reload"])
    return {**_linux_status(root, settings), "ok": result.returncode == 0}


def launch_now(repo_root: str | Path, *, requester: dict[str, str] | None = None) -> dict[str, Any]:
    """Launch an update outside the bot service cgroup when systemd owns the bot."""
    root = Path(repo_root).resolve()
    settings = _settings()
    worker_argv = _worker_argv(root)
    if requester:
        worker_argv.extend(["--requester-json", json.dumps(requester, separators=(",", ":"))])

    if platform.system() == "Windows" or not os.getenv("INVOCATION_ID"):
        from shared import spawn_detached

        pid = spawn_detached(
            worker_argv,
            cwd=root,
            log_path=root / ".claude" / "data" / "state" / "framework-update-worker.log",
        )
        return {"ok": True, "worker_id": str(pid), "platform": platform.system().lower()}

    unit_dir, unit, systemctl = _systemd_paths(settings)
    if not (unit_dir / f"{unit}.service").is_file():
        return {
            "ok": False,
            "detail": (
                "bot is managed by systemd but the independent updater unit is not installed; "
                "run /update auto on first"
            ),
        }
    request_file = root / ".claude" / "data" / "state" / "framework-update-request.json"
    request_file.parent.mkdir(parents=True, exist_ok=True)
    request_file.write_text(json.dumps(requester or {}), encoding="utf-8")
    command = [*systemctl, "start", "--no-block", f"{unit}.service"]
    if settings["scope"] == "system" and os.geteuid() != 0:
        command = ["sudo", "-n", *command]
    result = _run(command)
    if result.returncode != 0:
        return {"ok": False, "detail": (result.stderr or result.stdout).strip()}
    return {"ok": True, "worker_id": f"{unit}.service", "platform": "linux-systemd"}
