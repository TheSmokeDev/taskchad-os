"""Ghost Phone P4.1 A2 — the `thehomie ghost` CLI group (status | up | down)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS.parent / "chat"))

import cli  # type: ignore[import-not-found]  # noqa: E402
import ghost_control  # type: ignore[import-not-found]  # noqa: E402
import ghost_device  # type: ignore[import-not-found]  # noqa: E402


def test_ghost_cli_status_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ghost_control,
        "ghost_status",
        lambda **_k: {
            "running": True,
            "booted": True,
            "serial": "emulator-5554",
            "avd": "homie_pixel",
            "detail": "ok",
        },
    )
    r = CliRunner().invoke(cli.main, ["ghost", "status", "--json"])
    assert r.exit_code == 0
    assert '"serial": "emulator-5554"' in r.output
    assert '"running": true' in r.output


def test_ghost_cli_up_boots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMIE_GHOST_ENABLED", "true")
    monkeypatch.delenv("HOMIE_KILLSWITCH_GHOST", raising=False)
    monkeypatch.setattr(
        ghost_control,
        "ensure_ghost_running",
        lambda **_k: {"ok": True, "status": "booted", "detail": "ghost AVD booted"},
    )
    r = CliRunner().invoke(cli.main, ["ghost", "up"])
    assert r.exit_code == 0
    assert "ghost up: booted" in r.output


def test_ghost_cli_up_refused_by_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMIE_GHOST_ENABLED", "true")
    monkeypatch.setenv("HOMIE_KILLSWITCH_GHOST", "disabled")
    monkeypatch.setattr(
        ghost_control,
        "ensure_ghost_running",
        lambda **_k: pytest.fail("kill-switched ghost up must not boot"),
    )
    r = CliRunner().invoke(cli.main, ["ghost", "up"])
    assert r.exit_code == 1
    assert "kill-switch" in r.output.lower()


def test_ghost_cli_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ghost_control,
        "ghost_shutdown",
        lambda **_k: {"ok": True, "status": "killed", "detail": "emu kill sent"},
    )
    r = CliRunner().invoke(cli.main, ["ghost", "down"])
    assert r.exit_code == 0
    assert "ghost down: killed" in r.output


def test_ghost_cli_test_app_launches_expo_go(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMIE_GHOST_ENABLED", "true")
    monkeypatch.delenv("HOMIE_KILLSWITCH_GHOST", raising=False)
    monkeypatch.setattr(
        ghost_control, "ghost_status", lambda **_k: {"running": True, "booted": True}
    )
    seen: list[str] = []
    monkeypatch.setattr(
        ghost_device, "ghost_app_launch", lambda pkg, **_k: seen.append(pkg) or {"package": pkg}
    )
    r = CliRunner().invoke(cli.main, ["ghost", "test-app", "--json"])
    assert r.exit_code == 0
    assert seen == ["host.exp.exponent"]  # Expo Go by default
    assert '"ok": true' in r.output


def test_ghost_cli_test_app_refuses_when_not_booted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMIE_GHOST_ENABLED", "true")
    monkeypatch.delenv("HOMIE_KILLSWITCH_GHOST", raising=False)
    monkeypatch.setattr(
        ghost_control, "ghost_status", lambda **_k: {"running": False, "booted": False}
    )
    monkeypatch.setattr(
        ghost_device,
        "ghost_app_launch",
        lambda *_a, **_k: pytest.fail("must not launch when the ghost is down"),
    )
    r = CliRunner().invoke(cli.main, ["ghost", "test-app"])
    assert r.exit_code == 1
    assert "not booted" in r.output.lower()


def test_ghost_cli_test_app_disabled_when_switch_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMIE_GHOST_ENABLED", raising=False)
    monkeypatch.setattr(
        ghost_control,
        "ghost_status",
        lambda **_k: pytest.fail("disabled ghost must not be probed"),
    )
    r = CliRunner().invoke(cli.main, ["ghost", "test-app"])
    assert r.exit_code == 1
    assert "disabled" in r.output.lower()
