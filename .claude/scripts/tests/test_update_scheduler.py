from __future__ import annotations

import subprocess
from pathlib import Path

import update_scheduler


def completed(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def test_systemd_timer_uses_pacific_wall_time_and_missed_run_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("HOMIE_UPDATE_TIME", "04:00")
    monkeypatch.setenv("HOMIE_UPDATE_TIMEZONE", "America/Los_Angeles")
    monkeypatch.setenv("HOMIE_UPDATE_SYSTEMD_SCOPE", "user")
    service, timer = update_scheduler._systemd_unit_text(tmp_path, update_scheduler._settings())

    assert "OnCalendar=*-*-* 04:00:00 America/Los_Angeles" in timer
    assert "Persistent=true" in timer
    assert "update_worker.py" in service
    assert "--scheduled" in service
    assert "--restart" in service


def test_linux_enable_disable_and_status_reflect_physical_systemd_state(tmp_path, monkeypatch):
    units = tmp_path / "units"
    enabled = False

    monkeypatch.setattr(update_scheduler, "_is_docker", lambda: False)
    monkeypatch.setattr(update_scheduler.platform, "system", lambda: "Linux")
    monkeypatch.setattr(update_scheduler, "shutil_which", lambda _name: "/bin/systemctl")
    monkeypatch.setattr(
        update_scheduler,
        "_systemd_paths",
        lambda settings: (units, settings["unit_name"], ["systemctl", "--user"]),
    )

    def fake_run(argv, check=False):
        nonlocal enabled
        if "enable" in argv:
            enabled = True
        elif "disable" in argv:
            enabled = False
        if "is-enabled" in argv:
            return completed(argv, 0 if enabled else 1, "enabled\n" if enabled else "disabled\n")
        if "show" in argv:
            return completed(argv, 0, "Wed 2026-07-15 04:00:00 PDT\n")
        return completed(argv)

    monkeypatch.setattr(update_scheduler, "_run", fake_run)

    assert update_scheduler.status(tmp_path)["enabled"] is False
    on = update_scheduler.enable(tmp_path)
    assert on["ok"] is True and on["enabled"] is True
    assert Path(on["timer_path"]).read_text().find("Persistent=true") >= 0
    off = update_scheduler.disable(tmp_path)
    assert off["ok"] is True and off["enabled"] is False


def test_docker_is_check_only(tmp_path, monkeypatch):
    monkeypatch.setattr(update_scheduler, "_is_docker", lambda: True)
    result = update_scheduler.status(tmp_path)
    assert result["supported"] is False
    assert result["check_only"] is True


def test_windows_task_uses_daily_local_four_am_trigger(tmp_path, monkeypatch):
    calls = []
    enabled = False
    monkeypatch.setattr(update_scheduler, "_is_docker", lambda: False)
    monkeypatch.setattr(update_scheduler.platform, "system", lambda: "Windows")

    def fake_run(argv, check=False):
        nonlocal enabled
        calls.append(list(argv))
        if argv[0] == "powershell.exe":
            enabled = True
            return completed(argv)
        if "/Query" in argv and enabled:
            return completed(argv, stdout="Next Run Time: 7/15/2026 4:00:00 AM\n")
        return completed(argv, returncode=1, stderr="not found")

    monkeypatch.setattr(update_scheduler, "_run", fake_run)
    result = update_scheduler.enable(tmp_path)

    create = next(argv for argv in calls if argv[0] == "powershell.exe")
    script = create[-1]
    assert "New-ScheduledTaskTrigger -Daily -At '04:00'" in script
    assert "New-ScheduledTaskSettingsSet -StartWhenAvailable" in script
    assert result["enabled"] is True
