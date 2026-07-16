"""Tests for the bot desired-state switch (scripts/bot_lifecycle_switch.py).

One test per distinct code path: missing/corrupt flag fail-open to "on",
set/get roundtrip + audit, turn_on skip-spawn vs spawn vs failed-spawn,
turn_off cleanup + cleanup-failure, the bot_lifecycle kill-switch gate, and
best-effort audit.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import bot_lifecycle_switch as bls  # noqa: E402
import bot_watchdog  # noqa: E402
import config  # noqa: E402
import shared  # noqa: E402
from security import kill_switches  # noqa: E402


@pytest.fixture()
def sw(monkeypatch, tmp_path):
    """Isolate the switch: temp STATE_DIR, fake audit sink, no real bot."""
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)

    audits: list[dict] = []
    fake_da = types.ModuleType("dashboard_api")
    fake_da._audit_write = lambda **kw: audits.append(kw)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "dashboard_api", fake_da)

    return type(
        "SW",
        (),
        {"flag": tmp_path / "bot-desired-state.json", "audits": audits},
    )()


# --------------------------------------------------------------- get_desired


def test_missing_flag_defaults_on(sw) -> None:
    """No file = "on" — preserves the pre-switch always-guarded behavior."""
    result = bls.get_desired()
    assert result["desired"] == "on"
    assert result["changed_by"] == ""


def test_corrupt_flag_defaults_on(sw) -> None:
    sw.flag.write_text("{{{ not json", encoding="utf-8")
    assert bls.get_desired()["desired"] == "on"


def test_unknown_desired_value_defaults_on(sw) -> None:
    sw.flag.write_text(json.dumps({"desired": "maybe"}), encoding="utf-8")
    assert bls.get_desired()["desired"] == "on"


# --------------------------------------------------------------- set_desired


def test_set_get_roundtrip(sw) -> None:
    payload = bls.set_desired("off", "test-operator")
    assert sw.flag.exists()

    result = bls.get_desired()
    assert result["desired"] == "off"
    assert result["changed_by"] == "test-operator"
    assert result["changed_at"] == payload["changed_at"]
    assert result["changed_at"]  # iso stamp present

    assert sw.audits[-1]["action"] == "bot_desired_off"
    assert sw.audits[-1]["outcome"] == "succeeded"


def test_set_desired_rejects_invalid_value(sw) -> None:
    with pytest.raises(ValueError):
        bls.set_desired("paused", "test")
    assert not sw.flag.exists()


def test_audit_failure_is_best_effort(sw, monkeypatch) -> None:
    def boom(**kw):
        raise RuntimeError("audit db locked")

    fake_da = types.ModuleType("dashboard_api")
    fake_da._audit_write = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "dashboard_api", fake_da)

    bls.set_desired("on", "test")  # must not raise
    assert bls.get_desired()["desired"] == "on"


# ------------------------------------------------------------------- turn_on


def test_turn_on_skips_spawn_when_pid_alive(sw, monkeypatch) -> None:
    monkeypatch.setattr(shared, "read_pid", lambda *a, **k: 4242)
    monkeypatch.setattr(shared, "is_pid_alive", lambda pid: True)

    def never(*a, **k):
        raise AssertionError("restart_bot must NOT run when the bot is alive")

    monkeypatch.setattr(bot_watchdog, "restart_bot", never)

    result = bls.turn_on("test")

    assert result["ok"] is True
    assert result["started"] is False
    assert result["pid"] == 4242
    assert bls.get_desired()["desired"] == "on"


def test_turn_on_spawns_when_no_live_bot(sw, monkeypatch) -> None:
    monkeypatch.setattr(shared, "read_pid", lambda *a, **k: None)
    calls: list[str] = []

    def fake_restart():
        calls.append("spawn")
        return True, "run_chat.sh (all adapters): back up in 8s"

    monkeypatch.setattr(bot_watchdog, "restart_bot", fake_restart)

    result = bls.turn_on("test")

    assert calls == ["spawn"]
    assert result["ok"] is True
    assert result["started"] is True
    assert "back up" in result["detail"]


def test_turn_on_reports_failed_spawn(sw, monkeypatch) -> None:
    monkeypatch.setattr(shared, "read_pid", lambda *a, **k: None)
    monkeypatch.setattr(
        bot_watchdog, "restart_bot", lambda: (False, "Git Bash not found")
    )

    result = bls.turn_on("test")

    assert result["ok"] is False
    assert result["started"] is False
    assert "Git Bash" in result["detail"]
    # The flag still landed — the watchdog will keep trying once bash exists.
    assert bls.get_desired()["desired"] == "on"


# ------------------------------------------------------------------ turn_off


def test_turn_off_calls_profile_aware_cleanup(sw, monkeypatch) -> None:
    monkeypatch.setattr(shared, "cleanup_all_bot_processes", lambda *a, **k: [111, 222])

    result = bls.turn_off("test")

    assert result["ok"] is True
    assert result["stopped"] == [111, 222]
    assert bls.get_desired()["desired"] == "off"


def test_turn_off_flag_lands_even_when_cleanup_fails(sw, monkeypatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("psutil exploded")

    monkeypatch.setattr(shared, "cleanup_all_bot_processes", boom)

    result = bls.turn_off("test")

    assert result["ok"] is False
    assert "stop failed" in result["detail"]
    # Flag write precedes the sweep — the watchdog stands down regardless.
    assert bls.get_desired()["desired"] == "off"


# --------------------------------------------------------------- kill switch


def test_kill_switch_blocks_turn_on_and_off(sw, monkeypatch) -> None:
    monkeypatch.setenv("HOMIE_KILLSWITCH_BOT_LIFECYCLE", "disabled")
    with pytest.raises(kill_switches.KillSwitchDisabled):
        bls.turn_on("test")
    with pytest.raises(kill_switches.KillSwitchDisabled):
        bls.turn_off("test")
    # Blocked BEFORE the flag write — desired state untouched.
    assert not sw.flag.exists()


def test_kill_switch_does_not_gate_get_desired(sw, monkeypatch) -> None:
    monkeypatch.setenv("HOMIE_KILLSWITCH_BOT_LIFECYCLE", "disabled")
    assert bls.get_desired()["desired"] == "on"  # read stays available
