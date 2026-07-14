"""Tests for the bot-autostart toggle (scripts/autostart.py).

One test per distinct code path: physical status via schtasks exit code,
idempotent enable/disable through PowerShell, every failure path (nonzero
exit, timeout, missing launcher), the kill-switch gate, non-Windows
unsupported behavior, call-time env resolution, and best-effort audit.
"""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import autostart  # noqa: E402
from security import kill_switches  # noqa: E402


class FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class Harness:
    """Records every subprocess call; routes responses by executable name."""

    def __init__(self):
        self.calls: list[tuple[list[str], dict]] = []
        self.responses: dict[str, object] = {
            "schtasks": FakeProc(0),
            "powershell.exe": FakeProc(0),
        }
        self.audits: list[dict] = []


@pytest.fixture()
def hz(monkeypatch):
    """Isolate autostart: fake subprocess, forced Windows, fake audit sink."""
    h = Harness()

    def fake_run(argv, **kwargs):
        h.calls.append((list(argv), kwargs))
        resp = h.responses[argv[0]]
        if isinstance(resp, BaseException):
            raise resp
        return resp

    monkeypatch.setattr(autostart.subprocess, "run", fake_run)
    monkeypatch.setattr(autostart.platform, "system", lambda: "Windows")

    fake_da = types.ModuleType("dashboard_api")
    fake_da._audit_write = lambda **kw: h.audits.append(kw)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "dashboard_api", fake_da)
    return h


def ps_calls(h: Harness) -> list[tuple[list[str], dict]]:
    return [c for c in h.calls if c[0][0] == "powershell.exe"]


# ------------------------------------------------------------------- status


def test_status_enabled_when_schtasks_exit_zero(hz) -> None:
    result = autostart.status()
    assert result["supported"] is True
    assert result["enabled"] is True
    assert hz.calls[0][0] == ["schtasks", "/query", "/tn", "SecondBrain-BotStart"]


def test_status_disabled_when_schtasks_exit_nonzero(hz) -> None:
    hz.responses["schtasks"] = FakeProc(1)
    result = autostart.status()
    assert result["enabled"] is False
    assert "not registered" in result["detail"]


def test_status_query_failure_reads_as_disabled(hz) -> None:
    hz.responses["schtasks"] = subprocess.TimeoutExpired(cmd="schtasks", timeout=60)
    result = autostart.status()
    assert result["enabled"] is False
    assert "query failed" in result["detail"]


# ------------------------------------------------------------------- enable


def test_enable_registers_and_reports_enabled(hz) -> None:
    result = autostart.enable(caller="test")
    assert result["ok"] is True
    assert result["enabled"] is True  # post-state re-read hit the schtasks fake

    argv, kwargs = ps_calls(hz)[0]
    assert argv[:3] == ["powershell.exe", "-NoProfile", "-NonInteractive"]
    env = kwargs["env"]
    assert env["HOMIE_AUTOSTART_TASK"] == "SecondBrain-BotStart"
    assert env["HOMIE_AUTOSTART_BAT"].endswith("run_bot_start.bat")
    assert env["HOMIE_AUTOSTART_WORKDIR"] == str(SCRIPTS_DIR)
    # Post-state re-read is physical (Rule 2): a schtasks query follows the mutation.
    assert hz.calls[-1][0][0] == "schtasks"


def test_enable_script_is_unregister_then_register(hz) -> None:
    autostart.enable(caller="test")
    script = ps_calls(hz)[0][0][4]
    assert "Unregister-ScheduledTask" in script
    assert "Register-ScheduledTask" in script
    assert script.index("Unregister-ScheduledTask") < script.index(
        "Register-ScheduledTask -TaskName"
    )
    # Values travel via env vars, never interpolated into the script.
    assert "SecondBrain-BotStart" not in script


def test_enable_surfaces_powershell_failure(hz) -> None:
    hz.responses["powershell.exe"] = FakeProc(1, stderr="Access is denied.")
    hz.responses["schtasks"] = FakeProc(1)
    result = autostart.enable(caller="test")
    assert result["ok"] is False
    assert "Access is denied." in result["detail"]
    assert hz.audits[-1]["action"] == "autostart_enable"
    assert hz.audits[-1]["outcome"] == "failed"


def test_enable_timeout_returns_error_dict(hz) -> None:
    hz.responses["powershell.exe"] = subprocess.TimeoutExpired(
        cmd="powershell.exe", timeout=60
    )
    result = autostart.enable(caller="test")
    assert result["ok"] is False
    assert "timed out" in result["detail"]


def test_enable_missing_launcher_fails_fast(hz, monkeypatch) -> None:
    monkeypatch.setattr(autostart, "_SCRIPTS_DIR", Path("C:/nonexistent-dir-xyz"))
    result = autostart.enable(caller="test")
    assert result["ok"] is False
    assert "launcher missing" in result["detail"]
    assert ps_calls(hz) == []


# ------------------------------------------------------------------ disable


def test_disable_removes_task(hz) -> None:
    hz.responses["powershell.exe"] = FakeProc(0, stdout="removed\n")
    hz.responses["schtasks"] = FakeProc(1)
    result = autostart.disable(caller="test")
    assert result["ok"] is True
    assert result["enabled"] is False
    assert result["detail"] == "task removed"
    assert hz.audits[-1]["action"] == "autostart_disable"
    assert hz.audits[-1]["outcome"] == "succeeded"


def test_disable_absent_is_idempotent_ok(hz) -> None:
    hz.responses["powershell.exe"] = FakeProc(0, stdout="absent\n")
    hz.responses["schtasks"] = FakeProc(1)
    result = autostart.disable(caller="test")
    assert result["ok"] is True
    assert result["detail"] == "task was not registered"


# -------------------------------------------------------------- kill switch


def test_kill_switch_blocks_enable_and_disable(hz, monkeypatch) -> None:
    monkeypatch.setenv("HOMIE_KILLSWITCH_AUTOSTART", "disabled")
    with pytest.raises(kill_switches.KillSwitchDisabled):
        autostart.enable(caller="test")
    with pytest.raises(kill_switches.KillSwitchDisabled):
        autostart.disable(caller="test")
    assert hz.calls == []  # blocked BEFORE any subprocess fires


# -------------------------------------------------------------- non-Windows


def test_non_windows_status_unsupported(hz, monkeypatch) -> None:
    monkeypatch.setattr(autostart.platform, "system", lambda: "Darwin")
    result = autostart.status()
    assert result["supported"] is False
    assert result["enabled"] is False
    assert hz.calls == []


def test_non_windows_enable_error_dict(hz, monkeypatch) -> None:
    monkeypatch.setattr(autostart.platform, "system", lambda: "Linux")
    result = autostart.enable(caller="test")
    assert result["ok"] is False
    assert result["supported"] is False
    assert hz.calls == []


# ------------------------------------------------------------------- config


def test_settings_resolver_reads_env_at_call_time(hz, monkeypatch) -> None:
    monkeypatch.setenv("BOT_AUTOSTART_TASK_NAME", "CustomTask")
    monkeypatch.setenv("BOT_AUTOSTART_TIMEOUT_SECONDS", "7")
    result = autostart.status()
    assert result["task_name"] == "CustomTask"
    argv, kwargs = hz.calls[0]
    assert argv == ["schtasks", "/query", "/tn", "CustomTask"]
    assert kwargs["timeout"] == 7.0


# -------------------------------------------------------------------- audit


def test_audit_failure_is_best_effort(hz, monkeypatch) -> None:
    def boom(**kw):
        raise RuntimeError("audit db locked")

    fake_da = types.ModuleType("dashboard_api")
    fake_da._audit_write = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "dashboard_api", fake_da)

    result = autostart.enable(caller="test")
    assert result["ok"] is True  # mutation succeeded despite the audit failure
