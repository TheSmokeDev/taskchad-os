"""Tests for the external bot watchdog (scripts/bot_watchdog.py).

The watchdog is the layer that did not exist during the 6-week wedge: nothing
polled /health, so nothing ever restarted the bot. These tests pin the two
properties that make it trustworthy — it restarts when the bot is PROVEN bad,
and it refuses to restart on anything less than proof.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import bot_watchdog  # noqa: E402
import config  # noqa: E402
from config import BotWatchdogSettings  # noqa: E402


def settings(**overrides: object) -> BotWatchdogSettings:
    base: dict[str, object] = {
        "enabled": True,
        "health_url": "http://127.0.0.1:8788/health",
        "timeout_seconds": 5.0,
        "failure_threshold": 2,
        "max_restarts_per_hour": 3,
        "grace_seconds": 300.0,
    }
    base.update(overrides)
    return BotWatchdogSettings(**base)  # type: ignore[arg-type]


@pytest.fixture()
def wd(monkeypatch, tmp_path):
    """Isolate the watchdog: temp state file, no real restarts, no real toasts."""
    state_file = tmp_path / "bot-watchdog-state.json"
    monkeypatch.setattr(config, "BOT_WATCHDOG_STATE_FILE", state_file)

    restarts: list[str] = []

    def fake_restart() -> tuple[bool, str]:
        restarts.append("restart")
        return True, "Telegram bot started (Windows PID 1234)"

    notices: list[tuple[str, str]] = []
    monkeypatch.setattr(bot_watchdog, "restart_bot", fake_restart)
    monkeypatch.setattr(bot_watchdog, "_notify", lambda t, m: notices.append((t, m)))
    monkeypatch.setattr(bot_watchdog, "append_to_daily_log", lambda *a, **k: None)

    return type(
        "WD", (), {"state_file": state_file, "restarts": restarts, "notices": notices}
    )()


def set_health(monkeypatch, verdict: str, payload: dict | None = None, detail: str = "") -> None:
    monkeypatch.setattr(
        bot_watchdog, "poll_health", lambda *_a, **_k: (verdict, payload or {}, detail)
    )


def set_settings(monkeypatch, **overrides: object) -> None:
    monkeypatch.setattr(config, "get_bot_watchdog_settings", lambda: settings(**overrides))


# ------------------------------------------------------------------ classify


def test_classify_healthy_payload_is_ok() -> None:
    verdict, _, _ = bot_watchdog.classify(
        {"status": "ok", "adapters": {"telegram": True, "discord": True}}
    )
    assert verdict == bot_watchdog.OK


def test_classify_dead_adapter_is_degraded_even_when_status_says_ok() -> None:
    """THE regression test.

    The pre-fix bot reported ``status: ok`` with ``adapters: {telegram: true}``
    while Telegram polling was dead. The watchdog trusts the per-adapter probe
    result over the summary field — a single dead adapter is degraded, full stop.
    """
    verdict, _, detail = bot_watchdog.classify(
        {"status": "ok", "adapters": {"telegram": False, "discord": True}}
    )
    assert verdict == bot_watchdog.DEGRADED
    assert "telegram" in detail


def test_classify_dead_gateway_is_restart_worthy() -> None:
    verdict, _, detail = bot_watchdog.classify(
        {
            "status": "degraded",
            "adapters": {"telegram": False, "discord": True, "web": True},
            "adapter_liveness": {
                "telegram": {"healthy": False, "critical": True},
                "discord": {"healthy": True, "critical": True},
                "web": {"healthy": True, "critical": False},
            },
        }
    )
    assert verdict == bot_watchdog.DEGRADED
    assert "telegram" in detail


def test_dead_relay_is_reported_but_never_restarted(wd, monkeypatch) -> None:
    """THE restart-loop trap.

    The web adapter dials OUT to the Mission Control relay. If MC is down the
    socket is legitimately disconnected — but the BOT is fine, Telegram and
    Discord still work, and RelayWSClient redials on its own. Restarting the bot
    every 5 minutes because someone else's service is down would turn their
    outage into ours, forever.
    """
    payload = {
        "status": "degraded",
        "adapters": {"telegram": True, "discord": True, "web": False},
        "adapter_liveness": {
            "telegram": {"healthy": True, "critical": True},
            "discord": {"healthy": True, "critical": True},
            "web": {"healthy": False, "critical": False},
        },
    }
    verdict, _, detail = bot_watchdog.classify(payload)
    assert verdict == bot_watchdog.NONCRITICAL
    assert "web" in detail

    # ...and it must survive repeated polls without ever restarting.
    set_settings(monkeypatch, failure_threshold=1)
    monkeypatch.setattr(bot_watchdog, "poll_health", lambda *a, **k: (verdict, payload, detail))

    for _ in range(5):
        result = bot_watchdog.run_once()

    assert result["restarted"] is False
    assert result["consecutive_failures"] == 0
    assert wd.restarts == []


def test_degraded_status_with_healthy_gateways_is_not_restart_worthy() -> None:
    verdict, _, _ = bot_watchdog.classify(
        {
            "status": "degraded",
            "adapters": {"telegram": True},
            "adapter_liveness": {"telegram": {"healthy": True, "critical": True}},
        }
    )
    assert verdict == bot_watchdog.NONCRITICAL


def test_classify_no_adapters_is_degraded() -> None:
    verdict, _, _ = bot_watchdog.classify({"status": "ok", "adapters": {}})
    assert verdict == bot_watchdog.DEGRADED


def test_classify_degraded_status_is_degraded() -> None:
    verdict, _, _ = bot_watchdog.classify({"status": "degraded", "adapters": {"telegram": True}})
    assert verdict == bot_watchdog.DEGRADED


def test_classify_warming_is_not_a_failure() -> None:
    verdict, _, _ = bot_watchdog.classify({"status": "warming", "adapters": {"telegram": True}})
    assert verdict == bot_watchdog.WARMING


def test_classify_unknown_shape_is_fail_safe_ok() -> None:
    """A watchdog that restarts on its own confusion is a self-inflicted outage."""
    verdict, _, _ = bot_watchdog.classify({"weird": "payload"})
    assert verdict == bot_watchdog.OK


def test_poll_health_unreachable_when_nothing_is_listening() -> None:
    # Port 1 is reserved and never listening — a real connection failure.
    verdict, _, detail = bot_watchdog.poll_health("http://127.0.0.1:1/health", timeout=1.0)
    assert verdict == bot_watchdog.UNREACHABLE
    assert detail


# ------------------------------------------------------------------ run_once


def test_healthy_bot_is_left_alone(wd, monkeypatch) -> None:
    set_settings(monkeypatch)
    set_health(monkeypatch, bot_watchdog.OK, {"status": "ok"})

    result = bot_watchdog.run_once()

    assert result["verdict"] == bot_watchdog.OK
    assert result["restarted"] is False
    assert wd.restarts == []


def test_single_failure_below_threshold_does_not_restart(wd, monkeypatch) -> None:
    set_settings(monkeypatch, failure_threshold=2)
    set_health(monkeypatch, bot_watchdog.UNREACHABLE, {}, "connection refused")

    result = bot_watchdog.run_once()

    assert result["consecutive_failures"] == 1
    assert result["restarted"] is False
    assert wd.restarts == []


def test_consecutive_failures_across_runs_trigger_restart(wd, monkeypatch) -> None:
    """Failure counting must survive process exit — each --once run is a new
    process, so the count lives in the state file or it does not exist."""
    set_settings(monkeypatch, failure_threshold=2)
    set_health(monkeypatch, bot_watchdog.UNREACHABLE, {}, "connection refused")

    first = bot_watchdog.run_once()
    assert first["restarted"] is False

    second = bot_watchdog.run_once()  # separate call == separate scheduled run

    assert second["consecutive_failures"] == 2
    assert second["restarted"] is True
    assert wd.restarts == ["restart"]
    assert any("restarted" in t.lower() for t, _ in wd.notices)


def test_state_file_persists_the_counter(wd, monkeypatch) -> None:
    set_settings(monkeypatch, failure_threshold=5)
    set_health(monkeypatch, bot_watchdog.DEGRADED, {"status": "degraded"}, "dead")

    bot_watchdog.run_once()

    saved = json.loads(wd.state_file.read_text(encoding="utf-8"))
    assert saved["consecutive_failures"] == 1
    assert saved["last_verdict"] == bot_watchdog.DEGRADED


def test_recovery_clears_the_counter(wd, monkeypatch) -> None:
    set_settings(monkeypatch, failure_threshold=3)
    set_health(monkeypatch, bot_watchdog.DEGRADED, {"status": "degraded"}, "dead")
    bot_watchdog.run_once()

    set_health(monkeypatch, bot_watchdog.OK, {"status": "ok"})
    result = bot_watchdog.run_once()

    assert result["consecutive_failures"] == 0


def test_dry_run_never_restarts(wd, monkeypatch) -> None:
    set_settings(monkeypatch, failure_threshold=1)
    set_health(monkeypatch, bot_watchdog.DEGRADED, {"status": "degraded"}, "dead")

    result = bot_watchdog.run_once(dry_run=True)

    assert result["restart_blocked"] == "dry_run"
    assert wd.restarts == []


def test_restart_budget_exhausted_stops_restarting_and_alerts(wd, monkeypatch) -> None:
    """A restart loop is worse than a down bot — it hides the real cause."""
    from datetime import datetime

    set_settings(monkeypatch, failure_threshold=1, max_restarts_per_hour=2)
    set_health(monkeypatch, bot_watchdog.DEGRADED, {"status": "degraded"}, "dead")

    now = datetime.now().isoformat()
    wd.state_file.parent.mkdir(parents=True, exist_ok=True)
    wd.state_file.write_text(json.dumps({"restarts": [now, now]}), encoding="utf-8")

    result = bot_watchdog.run_once()

    assert result["restart_blocked"] == "budget_exhausted"
    assert wd.restarts == []
    assert any("DOWN" in t for t, _ in wd.notices)


def test_stale_restarts_fall_out_of_the_rolling_hour(wd, monkeypatch) -> None:
    from datetime import datetime, timedelta

    set_settings(monkeypatch, failure_threshold=1, max_restarts_per_hour=2)
    set_health(monkeypatch, bot_watchdog.DEGRADED, {"status": "degraded"}, "dead")

    old = (datetime.now() - timedelta(hours=3)).isoformat()
    wd.state_file.parent.mkdir(parents=True, exist_ok=True)
    wd.state_file.write_text(json.dumps({"restarts": [old, old]}), encoding="utf-8")

    result = bot_watchdog.run_once()

    assert result["restarted"] is True  # the old restarts no longer count
    assert wd.restarts == ["restart"]


def test_failures_inside_the_grace_window_are_not_counted(wd, monkeypatch) -> None:
    """A just-restarted bot is allowed to be slow to boot."""
    from datetime import datetime

    set_settings(monkeypatch, failure_threshold=1, grace_seconds=300)
    set_health(monkeypatch, bot_watchdog.UNREACHABLE, {}, "still booting")

    wd.state_file.parent.mkdir(parents=True, exist_ok=True)
    wd.state_file.write_text(
        json.dumps({"last_restart_at": datetime.now().isoformat()}), encoding="utf-8"
    )

    result = bot_watchdog.run_once()

    assert result["in_grace"] is True
    assert result["consecutive_failures"] == 0
    assert wd.restarts == []


def test_stuck_warming_past_grace_is_treated_as_wedged(wd, monkeypatch) -> None:
    set_settings(monkeypatch, failure_threshold=1, grace_seconds=300)
    set_health(
        monkeypatch,
        bot_watchdog.WARMING,
        {"status": "warming", "uptime_seconds": 9999},
        "warming",
    )

    result = bot_watchdog.run_once()

    assert result["verdict"] == bot_watchdog.DEGRADED
    assert "stuck warming" in result["detail"]
    assert result["restarted"] is True


def test_briefly_warming_is_tolerated(wd, monkeypatch) -> None:
    set_settings(monkeypatch, failure_threshold=1, grace_seconds=300)
    set_health(
        monkeypatch,
        bot_watchdog.WARMING,
        {"status": "warming", "uptime_seconds": 12},
        "warming",
    )

    result = bot_watchdog.run_once()

    assert result["verdict"] == bot_watchdog.WARMING
    assert result["restarted"] is False


def test_disabled_watchdog_is_a_no_op(wd, monkeypatch) -> None:
    set_settings(monkeypatch, enabled=False)
    set_health(monkeypatch, bot_watchdog.UNREACHABLE, {}, "dead")

    result = bot_watchdog.run_once()

    assert result["verdict"] == bot_watchdog.DISABLED
    assert wd.restarts == []


def test_failed_restart_is_reported_not_swallowed(wd, monkeypatch) -> None:
    set_settings(monkeypatch, failure_threshold=1)
    set_health(monkeypatch, bot_watchdog.DEGRADED, {"status": "degraded"}, "dead")
    monkeypatch.setattr(bot_watchdog, "restart_bot", lambda: (False, "launcher exited 1"))

    result = bot_watchdog.run_once()

    assert result["restarted"] is False
    assert result["restart_detail"] == "launcher exited 1"
    assert any("FAILED" in t for t, _ in wd.notices)


# ------------------------------------------------- restart mechanics
# Both of these were found by actually killing the bot and watching the watchdog
# try to revive it — neither was visible from unit tests alone.


def test_restart_prefers_the_launcher_that_starts_all_adapters() -> None:
    """run_chat.bat hardcodes --telegram.

    Restarting through it resurrects a Telegram-only bot with no Discord and no
    relay — a watchdog that "recovers" the bot into a quietly degraded state is
    just a slower version of the bug it exists to fix. Observed live before the
    fix: the restarted bot reported adapters {"telegram": true} and nothing else.
    """
    argv, label = bot_watchdog.restart_command()

    assert "run_chat.sh" in " ".join(argv)
    assert "all adapters" in label


def test_restart_never_inherits_a_pipe_to_the_bot(monkeypatch) -> None:
    """The launcher spawns the bot as a detached grandchild that inherits our
    handles. With capture_output=True the pipe never reaches EOF (the bot holds
    it open for its whole life) and subprocess.run blocks forever — observed
    live as a >4-minute hang. A watchdog that hangs inside its own restart is
    worse than no watchdog."""
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN202
        captured.update(kwargs)
        return type("P", (), {"returncode": 0})()

    monkeypatch.setattr(bot_watchdog.subprocess, "run", fake_run)
    monkeypatch.setattr(bot_watchdog, "wait_for_healthy", lambda *a, **k: (True, "back up"))

    bot_watchdog.restart_bot()

    assert captured.get("stdout") is bot_watchdog.subprocess.DEVNULL
    assert captured.get("stderr") is bot_watchdog.subprocess.DEVNULL
    assert captured.get("capture_output") is None  # never
    assert captured.get("timeout") == 120


def test_restart_is_verified_by_health_not_by_the_launcher_exit_code(monkeypatch) -> None:
    """A launcher exiting 0 means it SPAWNED something. Only /health proves the
    bot actually came back (Rule 2 — physical state, not a claim)."""
    monkeypatch.setattr(
        bot_watchdog.subprocess, "run", lambda *a, **k: type("P", (), {"returncode": 0})()
    )
    monkeypatch.setattr(
        bot_watchdog,
        "wait_for_healthy",
        lambda *a, **k: (False, "did not become healthy within 90s"),
    )

    ok, detail = bot_watchdog.restart_bot()

    assert ok is False  # launcher succeeded, bot did not — report the truth
    assert "did not become healthy" in detail


def test_corrupt_state_file_does_not_crash_the_watchdog(wd, monkeypatch) -> None:
    set_settings(monkeypatch)
    set_health(monkeypatch, bot_watchdog.OK, {"status": "ok"})
    wd.state_file.parent.mkdir(parents=True, exist_ok=True)
    wd.state_file.write_text("{{{ not json", encoding="utf-8")

    result = bot_watchdog.run_once()

    assert result["verdict"] == bot_watchdog.OK
