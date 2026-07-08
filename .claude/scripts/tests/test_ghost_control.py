"""Ghost Phone (P4.0) lifecycle — ghost_control status / boot / shutdown."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS.parent / "chat"))

import ghost_control  # type: ignore[import-not-found]  # noqa: E402

_SERIAL = "emulator-5554"
_AVD_ENV = {
    "HOMIE_ADB_BIN": "adb",
    "HOMIE_EMULATOR_BIN": "emulator",
    "HOMIE_GHOST_ADB_SERIAL": _SERIAL,
    "HOMIE_GHOST_AVD": "homie_pixel",
}
_SPARE_ENV = {
    "HOMIE_ADB_BIN": "adb",
    "HOMIE_GHOST_ADB_SERIAL": "192.168.0.174:5555",
    # No HOMIE_GHOST_AVD -> spare physical device backend.
}


def _adb_runner(*, serial: str = _SERIAL, present: bool = True, booted: bool = True, present_after: int = 0):
    """Fake adb runner dispatching on the adb sub-command. ``present_after`` is
    how many `devices` calls return empty before the device appears (models a
    boot / a reconnect)."""

    state = {"devices": 0}
    log: list[list[str]] = []

    def runner(argv, **_kwargs):
        log.append(list(argv))
        args = argv[1:]
        if args[:1] == ["-s"]:
            args = args[2:]
        joined = " ".join(args)
        if joined.startswith("devices"):
            state["devices"] += 1
            here = present and state["devices"] > present_after
            body = (
                f"List of devices attached\n{serial} device\n"
                if here
                else "List of devices attached\n"
            )
            return SimpleNamespace(returncode=0, stdout=body, stderr="")
        if "getprop sys.boot_completed" in joined:
            return SimpleNamespace(returncode=0, stdout="1\n" if booted else "0\n", stderr="")
        if joined.startswith("connect"):
            return SimpleNamespace(returncode=0, stdout="connected to", stderr="")
        if joined.startswith("emu kill"):
            return SimpleNamespace(returncode=0, stdout="OK: killing emulator", stderr="")
        # forward --list (empty) + forward add -> ok
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return runner, log


# ── ghost_status — physical state, never boots ───────────────────────────────


def test_ghost_status_no_serial() -> None:
    st = ghost_control.ghost_status(environ={"HOMIE_ADB_BIN": "adb"})
    assert st["running"] is False
    assert st["booted"] is False
    assert "HOMIE_GHOST_ADB_SERIAL" in st["detail"]


def test_ghost_status_running_and_booted() -> None:
    runner, _ = _adb_runner(present=True, booted=True)
    st = ghost_control.ghost_status(runner=runner, environ=_AVD_ENV)
    assert st["running"] is True
    assert st["booted"] is True
    assert st["serial"] == _SERIAL
    assert st["avd"] == "homie_pixel"


def test_ghost_status_present_but_not_booted() -> None:
    runner, _ = _adb_runner(present=True, booted=False)
    st = ghost_control.ghost_status(runner=runner, environ=_AVD_ENV)
    assert st["running"] is True
    assert st["booted"] is False


def test_ghost_status_never_boots_the_avd(monkeypatch: pytest.MonkeyPatch) -> None:
    # Landmine 2: a status poll must NEVER spawn the emulator (would pin RAM).
    monkeypatch.setattr(
        ghost_control,
        "_spawn_emulator",
        lambda *_a, **_k: pytest.fail("ghost_status must never boot the emulator"),
    )
    monkeypatch.setattr(
        ghost_control.subprocess,
        "Popen",
        lambda *_a, **_k: pytest.fail("ghost_status must never spawn a process"),
    )
    runner, _ = _adb_runner(present=False)
    st = ghost_control.ghost_status(runner=runner, environ=_AVD_ENV)
    assert st["running"] is False


# ── ensure_ghost_running — lazy, self-healing, fail-open ─────────────────────


def test_ensure_ghost_running_no_serial() -> None:
    result = ghost_control.ensure_ghost_running(environ={"HOMIE_ADB_BIN": "adb"})
    assert result["ok"] is False
    assert result["status"] == "no_serial"


def test_ensure_ghost_running_already_up_just_forwards(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ghost_control,
        "_spawn_emulator",
        lambda *_a, **_k: pytest.fail("already-running ghost must not re-boot"),
    )
    runner, log = _adb_runner(present=True, booted=True)
    result = ghost_control.ensure_ghost_running(
        runner=runner, environ=_AVD_ENV, sleep=lambda _s: None
    )
    assert result["ok"] is True
    assert result["status"] == "already_running"
    # The forward was (re)established on the ghost's own serial.
    assert any("forward" in c and _SERIAL in c for c in log)


def test_ensure_ghost_running_boots_avd_when_down() -> None:
    spawns: list[tuple[str, str]] = []
    runner, log = _adb_runner(present=True, booted=True, present_after=2)  # appears on 3rd poll
    result = ghost_control.ensure_ghost_running(
        runner=runner,
        spawner=lambda emu, avd: spawns.append((emu, avd)),
        environ=_AVD_ENV,
        boot_timeout_seconds=60,
        sleep=lambda _s: None,
    )
    assert result["ok"] is True
    assert result["status"] == "booted"
    assert spawns == [("emulator", "homie_pixel")]  # booted exactly once, right AVD


def test_ensure_ghost_running_boot_timeout() -> None:
    runner, _ = _adb_runner(present=False)  # never appears
    spawns: list[tuple[str, str]] = []
    result = ghost_control.ensure_ghost_running(
        runner=runner,
        spawner=lambda emu, avd: spawns.append((emu, avd)),
        environ=_AVD_ENV,
        boot_timeout_seconds=6,
        sleep=lambda _s: None,
    )
    assert result["ok"] is False
    assert result["status"] == "boot_timeout"
    assert spawns  # it tried to boot


def test_ensure_ghost_running_spare_connects_never_boots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ghost_control,
        "_spawn_emulator",
        lambda *_a, **_k: pytest.fail("a spare device must NEVER be booted as an emulator"),
    )
    runner, log = _adb_runner(serial="192.168.0.174:5555", present=True, present_after=1)
    result = ghost_control.ensure_ghost_running(
        runner=runner, environ=_SPARE_ENV, sleep=lambda _s: None
    )
    assert result["ok"] is True
    assert result["status"] == "connected"
    assert any("connect" in c for c in log)


def test_ensure_ghost_running_spare_unreachable() -> None:
    runner, _ = _adb_runner(serial="192.168.0.174:5555", present=False)
    result = ghost_control.ensure_ghost_running(
        runner=runner, environ=_SPARE_ENV, sleep=lambda _s: None
    )
    assert result["ok"] is False
    assert result["status"] == "no_device"


# ── ghost_shutdown ───────────────────────────────────────────────────────────


def test_ghost_shutdown_avd_emu_kill() -> None:
    runner, log = _adb_runner()
    result = ghost_control.ghost_shutdown(runner=runner, environ=_AVD_ENV)
    assert result["ok"] is True
    assert result["status"] == "killed"
    assert any("emu" in c and "kill" in c for c in log)


def test_ghost_shutdown_spare_is_left_running() -> None:
    runner, log = _adb_runner(serial="192.168.0.174:5555")
    result = ghost_control.ghost_shutdown(runner=runner, environ=_SPARE_ENV)
    assert result["ok"] is True
    assert result["status"] == "spare_left_running"
    assert not any("emu" in c and "kill" in c for c in log)  # never powers off a spare
