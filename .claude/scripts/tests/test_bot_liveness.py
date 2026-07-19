"""Tests for the in-bot adapter-liveness supervisor (chat/liveness.py).

Regression anchor: the bot ran wedged for ~6 weeks with a live PID, a live
process, and /health reporting ``telegram: true``. Every test below maps to a
distinct code path that the pre-fix system had no way to exercise at all.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

CHAT_DIR = Path(__file__).resolve().parents[2] / "chat"
if str(CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(CHAT_DIR))

from liveness import (  # noqa: E402
    AdapterWedgedError,
    DiagnosticsCache,
    LivenessSupervisor,
    ProbeResult,
    resolve_health_status,
)


class FakeAdapter:
    """Adapter test double with scriptable probe results."""

    def __init__(self, results: list[ProbeResult] | None = None) -> None:
        # Results are consumed in order; the last one repeats forever.
        self.results = results or [ProbeResult(True, "ok")]
        self.probe_calls = 0
        self.reconnect_calls = 0
        self.reconnect_raises = False
        self.hang = False
        self._last_update_at: float | None = None
        self._connected_at: float | None = 1.0  # connected unless a test says otherwise

    async def probe_liveness(self) -> ProbeResult:
        self.probe_calls += 1
        if self.hang:
            await asyncio.sleep(60)  # longer than any test timeout
        idx = min(self.probe_calls - 1, len(self.results) - 1)
        return self.results[idx]

    async def reconnect(self) -> None:
        self.reconnect_calls += 1
        if self.reconnect_raises:
            raise RuntimeError("reconnect boom")


class UnprobeableAdapter:
    """A legacy adapter with no liveness protocol (e.g. the CLI adapter)."""


def make_supervisor(adapter: object, **kwargs: object) -> LivenessSupervisor:
    defaults: dict[str, object] = {
        "failure_threshold": 2,
        "reconnect_attempts": 1,
        "probe_timeout_seconds": 0.2,
        "fail_fast": True,
    }
    defaults.update(kwargs)
    return LivenessSupervisor(adapters={"telegram": adapter}, **defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------- happy path


@pytest.mark.asyncio
async def test_healthy_probe_marks_adapter_alive() -> None:
    sup = make_supervisor(FakeAdapter([ProbeResult(True, "polling as @bot")]))

    await sup.probe_once("telegram")

    assert sup.state["telegram"].healthy is True
    assert sup.adapter_health_map() == {"telegram": True}
    assert sup.any_unhealthy() is False
    assert sup.state["telegram"].last_ok_at is not None


# ------------------------------------------------- the anti-false-positive path


@pytest.mark.asyncio
async def test_failure_below_threshold_does_not_mark_degraded() -> None:
    """A single failed probe is a blip, not a wedge.

    If a transient Telegram 502 flipped /health to degraded, the watchdog would
    restart a perfectly healthy bot. Only K CONSECUTIVE failures count.
    """
    sup = make_supervisor(FakeAdapter([ProbeResult(False, "502")]), failure_threshold=3)

    await sup.probe_once("telegram")

    assert sup.state["telegram"].consecutive_failures == 1
    assert sup.state["telegram"].healthy is None  # never probed OK, but not condemned
    assert sup.any_unhealthy() is False
    assert sup.adapter_health_map() == {"telegram": True}


@pytest.mark.asyncio
async def test_recovery_resets_the_failure_counter() -> None:
    adapter = FakeAdapter([ProbeResult(False, "502"), ProbeResult(True, "ok")])
    sup = make_supervisor(adapter, failure_threshold=3)

    await sup.probe_once("telegram")
    await sup.probe_once("telegram")

    assert sup.state["telegram"].consecutive_failures == 0
    assert sup.state["telegram"].healthy is True


# ------------------------------------------------------------- the wedge paths


@pytest.mark.asyncio
async def test_threshold_crossed_then_reconnect_heals_in_process() -> None:
    """Dead updater -> reconnect -> verify probe passes -> stay up. No restart."""
    adapter = FakeAdapter(
        [
            ProbeResult(False, "updater not running"),
            ProbeResult(False, "updater not running"),
            ProbeResult(True, "polling as @bot"),  # the post-reconnect verify probe
        ]
    )
    sup = make_supervisor(adapter, failure_threshold=2)

    await sup.probe_once("telegram")
    await sup.probe_once("telegram")  # crosses threshold -> escalate

    assert adapter.reconnect_calls == 1
    assert sup.state["telegram"].healthy is True
    assert sup.state["telegram"].reconnects == 1
    assert sup.any_unhealthy() is False


@pytest.mark.asyncio
async def test_reconnect_that_lies_still_fails_fast() -> None:
    """THE load-bearing test.

    ``reconnect()`` returning cleanly proves nothing — that is exactly the class
    of false signal (a call that returns without doing its job) that let the bot
    look alive for six weeks. The supervisor must re-probe and refuse to believe
    a reconnect that did not actually revive polling.
    """
    adapter = FakeAdapter([ProbeResult(False, "updater not running")])  # always dead
    sup = make_supervisor(adapter, failure_threshold=2)

    await sup.probe_once("telegram")
    with pytest.raises(AdapterWedgedError, match="unrecoverable"):
        await sup.probe_once("telegram")

    assert adapter.reconnect_calls == 1  # it tried
    assert sup.state["telegram"].healthy is False  # and did not believe it


@pytest.mark.asyncio
async def test_reconnect_raising_fails_fast() -> None:
    adapter = FakeAdapter([ProbeResult(False, "dead")])
    adapter.reconnect_raises = True
    sup = make_supervisor(adapter, failure_threshold=1)

    with pytest.raises(AdapterWedgedError):
        await sup.probe_once("telegram")


@pytest.mark.asyncio
async def test_fail_fast_disabled_reports_degraded_and_stays_up() -> None:
    adapter = FakeAdapter([ProbeResult(False, "dead")])
    sup = make_supervisor(adapter, failure_threshold=1, fail_fast=False)

    await sup.probe_once("telegram")  # must NOT raise

    assert sup.any_unhealthy() is True
    assert sup.adapter_health_map() == {"telegram": False}


@pytest.mark.asyncio
async def test_hanging_probe_is_bounded_and_counts_as_failure() -> None:
    """The supervisor that watches for hangs must not hang on its own probe."""
    adapter = FakeAdapter()
    adapter.hang = True
    sup = make_supervisor(adapter, failure_threshold=5, probe_timeout_seconds=0.05)

    await asyncio.wait_for(sup.probe_once("telegram"), timeout=2.0)

    assert sup.state["telegram"].consecutive_failures == 1
    assert "timeout" in sup.state["telegram"].detail


@pytest.mark.asyncio
async def test_probe_raising_is_a_failure_not_a_crash() -> None:
    class BoomAdapter(FakeAdapter):
        async def probe_liveness(self) -> ProbeResult:
            raise ConnectionError("network down")

        async def reconnect(self) -> None:
            return None

    sup = make_supervisor(BoomAdapter(), failure_threshold=5)

    await sup.probe_once("telegram")

    assert sup.state["telegram"].consecutive_failures == 1
    assert "ConnectionError" in sup.state["telegram"].detail


# ------------------------------------------------------------ fail-open paths


@pytest.mark.asyncio
async def test_unprobeable_adapter_is_reported_honestly_and_never_condemned() -> None:
    """An adapter with no probe cannot be proven dead, so it must never be
    reported dead — a missing probe must not manufacture a restart."""
    sup = LivenessSupervisor(adapters={"cli": UnprobeableAdapter()})

    await sup.probe_once("cli")

    assert sup.state["cli"].probed is False
    assert sup.adapter_health_map() == {"cli": True}
    assert sup.any_unhealthy() is False
    assert sup.snapshot()["cli"]["probed"] is False


@pytest.mark.asyncio
async def test_last_update_at_is_sampled_but_never_gates_health() -> None:
    """A quiet bot is not a dead bot."""
    adapter = FakeAdapter([ProbeResult(True, "ok")])
    adapter._last_update_at = 1_700_000_000.0
    sup = make_supervisor(adapter)

    await sup.probe_once("telegram")

    assert sup.state["telegram"].last_update_at == 1_700_000_000.0
    assert sup.state["telegram"].healthy is True  # traffic recency is not liveness
    assert sup.snapshot()["telegram"]["last_update_at"].startswith("20")


@pytest.mark.asyncio
async def test_notify_failure_never_breaks_the_supervisor() -> None:
    def boom(_title: str, _msg: str) -> None:
        raise RuntimeError("toast service down")

    adapter = FakeAdapter([ProbeResult(False, "dead")])
    sup = make_supervisor(adapter, failure_threshold=1, fail_fast=False, notify=boom)

    await sup.probe_once("telegram")  # must not raise

    assert sup.any_unhealthy() is True


# ------------------------------------------------------ health status resolution


def test_status_degraded_when_adapter_proven_dead() -> None:
    assert (
        resolve_health_status(
            adapters={"telegram": False},
            any_unhealthy=True,
            has_diagnostics=True,
            uptime_seconds=999,
            warmup_seconds=90,
        )
        == "degraded"
    )


def test_status_degraded_when_no_adapters_at_all() -> None:
    assert (
        resolve_health_status(
            adapters={},
            any_unhealthy=False,
            has_diagnostics=True,
            uptime_seconds=999,
            warmup_seconds=90,
        )
        == "degraded"
    )


def test_status_warming_during_boot_without_diagnostics() -> None:
    assert (
        resolve_health_status(
            adapters={"telegram": True},
            any_unhealthy=False,
            has_diagnostics=False,
            uptime_seconds=5,
            warmup_seconds=90,
        )
        == "warming"
    )


def test_missing_diagnostics_after_warmup_is_ok_not_degraded() -> None:
    """Broken diagnostics must never trigger a restart of a healthy bot."""
    assert (
        resolve_health_status(
            adapters={"telegram": True},
            any_unhealthy=False,
            has_diagnostics=False,
            uptime_seconds=10_000,
            warmup_seconds=90,
        )
        == "ok"
    )


def test_status_ok_when_adapters_alive_and_diagnostics_present() -> None:
    assert (
        resolve_health_status(
            adapters={"telegram": True},
            any_unhealthy=False,
            has_diagnostics=True,
            uptime_seconds=10_000,
            warmup_seconds=90,
        )
        == "ok"
    )


# ------------------------------------------------------------ diagnostics cache


# ------------------------------------------------------------- the boot race
# Found by running the real bot, NOT by unit tests: the supervisor starts
# concurrently with the router, so its first probe fired while Telegram was
# still connecting and logged a false "probe FAILED (1/3)".


@pytest.mark.asyncio
async def test_adapter_still_connecting_is_not_counted_as_dead() -> None:
    adapter = FakeAdapter([ProbeResult(False, "updater not running")])
    adapter._connected_at = None  # connect() has not finished yet
    sup = make_supervisor(adapter, failure_threshold=1, startup_grace_seconds=60)

    await sup.probe_once("telegram")  # must NOT raise, must NOT count

    assert adapter.probe_calls == 0  # not even probed — nothing to probe yet
    assert sup.state["telegram"].consecutive_failures == 0
    assert sup.any_unhealthy() is False


@pytest.mark.asyncio
async def test_adapter_that_never_connects_is_eventually_caught() -> None:
    """The startup grace has a HARD END.

    Otherwise a permanently-failing connect would hide behind "still warming up"
    forever — the exact shape of the bug this module exists to kill.
    """
    adapter = FakeAdapter([ProbeResult(False, "updater not running")])  # stays dead
    adapter._connected_at = None
    sup = make_supervisor(adapter, failure_threshold=1, startup_grace_seconds=0)

    with pytest.raises(AdapterWedgedError):
        await sup.probe_once("telegram")

    assert adapter.reconnect_calls == 1  # it tried to bring the connection up


@pytest.mark.asyncio
async def test_never_connected_adapter_that_reconnect_revives_is_kept() -> None:
    """If the escalation's reconnect actually brings the adapter up, keep it —
    verified by a fresh probe, never by the reconnect's own return."""
    adapter = FakeAdapter([ProbeResult(True, "polling as @bot")])
    adapter._connected_at = None
    sup = make_supervisor(adapter, failure_threshold=1, startup_grace_seconds=0)

    await sup.probe_once("telegram")  # must not raise

    assert adapter.reconnect_calls == 1
    assert sup.state["telegram"].healthy is True


# ------------------------------------------- the real TelegramAdapter probe


class _FakeUpdater:
    def __init__(self, running: bool) -> None:
        self.running = running


class _FakeBot:
    def __init__(self, raises: Exception | None = None) -> None:
        self.raises = raises
        self.username = "homie_bot"

    async def get_me(self):  # noqa: ANN202
        if self.raises:
            raise self.raises
        return self


class _FakeApp:
    def __init__(self, running: bool, bot_raises: Exception | None = None) -> None:
        self.updater = _FakeUpdater(running)
        self.bot = _FakeBot(bot_raises)


class _StubTelegram:
    """Binds the REAL TelegramAdapter.probe_liveness to a stub self.

    Exercises the shipped method (not a reimplementation) without needing a live
    bot token or a real PTB Application.
    """

    def __init__(self, app: _FakeApp, last_poll_error: str | None = None) -> None:
        self._app = app
        self._last_poll_error = last_poll_error

    async def probe_liveness(self):  # noqa: ANN202
        from adapters.telegram import TelegramAdapter

        return await TelegramAdapter.probe_liveness(self)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_telegram_probe_reports_dead_when_updater_stopped() -> None:
    """The exact 6-week failure: PTB's updater stopped, everything else 'fine'."""
    stub = _StubTelegram(_FakeApp(running=False), last_poll_error="Conflict: terminated by other getUpdates")

    result = await stub.probe_liveness()

    assert result.healthy is False
    assert "updater not running" in result.detail
    assert "Conflict" in result.detail  # the cause is carried through, not lost


@pytest.mark.asyncio
async def test_telegram_probe_reports_dead_when_get_me_fails() -> None:
    """updater.running can stay True while every API call 401s — so the probe
    also makes a real round-trip rather than trusting the flag alone."""
    stub = _StubTelegram(_FakeApp(running=True, bot_raises=ConnectionError("unauthorized")))

    result = await stub.probe_liveness()

    assert result.healthy is False
    assert "get_me failed" in result.detail


@pytest.mark.asyncio
async def test_telegram_probe_reports_healthy_when_polling() -> None:
    stub = _StubTelegram(_FakeApp(running=True))

    result = await stub.probe_liveness()

    assert result.healthy is True
    assert "homie_bot" in result.detail


def test_supervisor_rechecks_quickly_while_an_adapter_is_still_connecting() -> None:
    adapter = FakeAdapter()
    adapter._connected_at = None
    sup = make_supervisor(adapter, interval_seconds=60)

    assert sup._next_interval() == 5.0

    adapter._connected_at = 1.0
    sup.state["telegram"].healthy = True
    assert sup._next_interval() == 60.0


# ------------------------------------------------ criticality (restart policy)


class NonCriticalAdapter(FakeAdapter):
    """An optional link (the Mission Control relay) — reported, never fatal."""

    liveness_critical = False


@pytest.mark.asyncio
async def test_dead_noncritical_adapter_is_reported_but_never_fails_fast() -> None:
    """A dead relay must not kill the bot.

    The web adapter dials OUT to Mission Control. If MC is down, restarting the
    bot cannot fix it and the relay redials itself — so this path reports and
    alerts, but must NEVER raise AdapterWedgedError.
    """
    notices: list[tuple[str, str]] = []
    adapter = NonCriticalAdapter([ProbeResult(False, "relay websocket not connected")])
    sup = LivenessSupervisor(
        adapters={"web": adapter},
        failure_threshold=1,
        fail_fast=True,  # ON — and still must not fire for a non-gateway
        notify=lambda t, m: notices.append((t, m)),
    )

    await sup.probe_once("web")  # must NOT raise

    assert sup.state["web"].critical is False
    assert sup.state["web"].healthy is False
    assert sup.any_unhealthy() is True  # honest: something IS down
    assert sup.any_critical_unhealthy() is False  # but not restart-worthy
    assert sup.adapter_health_map() == {"web": False}
    assert any("relay is down" in t for t, _ in notices)


@pytest.mark.asyncio
async def test_noncritical_outage_alerts_once_not_every_probe() -> None:
    """A permanently-down relay must not toast the operator every 60 seconds."""
    notices: list[tuple[str, str]] = []
    adapter = NonCriticalAdapter([ProbeResult(False, "relay websocket not connected")])
    sup = LivenessSupervisor(
        adapters={"web": adapter},
        failure_threshold=1,
        notify=lambda t, m: notices.append((t, m)),
    )

    for _ in range(5):
        await sup.probe_once("web")

    assert len(notices) == 1  # one incident, one alert


@pytest.mark.asyncio
async def test_dead_gateway_still_fails_fast_while_a_relay_is_down() -> None:
    """Criticality is per-adapter: an optional outage must not mask a real one."""
    web = NonCriticalAdapter([ProbeResult(False, "relay down")])
    tg = FakeAdapter([ProbeResult(False, "updater not running")])
    sup = LivenessSupervisor(
        adapters={"web": web, "telegram": tg}, failure_threshold=1, reconnect_attempts=1
    )

    await sup.probe_once("web")  # fine, no raise
    with pytest.raises(AdapterWedgedError):
        await sup.probe_once("telegram")

    assert sup.any_critical_unhealthy() is True


@pytest.mark.asyncio
async def test_recovery_rearms_the_alert_for_the_next_incident() -> None:
    adapter = NonCriticalAdapter(
        [
            ProbeResult(False, "relay down"),  # probe -> crosses threshold
            ProbeResult(False, "relay down"),  # the post-reconnect verify probe
            ProbeResult(True, "relay connected"),  # MC comes back
        ]
    )
    sup = LivenessSupervisor(adapters={"web": adapter}, failure_threshold=1)

    await sup.probe_once("web")
    assert sup.state["web"].healthy is False
    assert sup.state["web"].escalated is True

    await sup.probe_once("web")
    assert sup.state["web"].healthy is True
    assert sup.state["web"].escalated is False  # re-armed for the next incident


# --------------------------------------------------- the real WebAdapter probe


class _FakeRelayClient:
    def __init__(self, connected: bool) -> None:
        self.is_connected = connected
        self.relay_url = "wss://mc.example/relay"


class _StubWeb:
    """Binds the REAL WebAdapter.probe_liveness/liveness_ready to a stub self."""

    def __init__(self, client: object | None) -> None:
        self.ws_client = client

    async def probe_liveness(self):  # noqa: ANN202
        from adapters.web import WebAdapter

        return await WebAdapter.probe_liveness(self)  # type: ignore[arg-type]

    def liveness_ready(self) -> bool:
        from adapters.web import WebAdapter

        return WebAdapter.liveness_ready(self)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_web_probe_catches_a_dropped_relay() -> None:
    """Dashboard chat dies silently when the relay drops — listen() just goes
    quiet, exactly like Telegram's wedge."""
    stub = _StubWeb(_FakeRelayClient(connected=False))

    result = await stub.probe_liveness()

    assert result.healthy is False
    assert "not connected" in result.detail
    assert stub.liveness_ready() is False


@pytest.mark.asyncio
async def test_web_probe_reports_healthy_when_relay_is_up() -> None:
    stub = _StubWeb(_FakeRelayClient(connected=True))

    result = await stub.probe_liveness()

    assert result.healthy is True
    assert "mc.example" in result.detail
    assert stub.liveness_ready() is True


@pytest.mark.asyncio
async def test_adapter_that_came_up_then_died_reports_the_real_cause() -> None:
    """Once an adapter has been up, a later failure must report WHY it died, not
    the misleading 'never completed connect()'."""
    adapter = NonCriticalAdapter([ProbeResult(False, "relay websocket not connected")])
    adapter._connected_at = None  # web-style: no connect stamp of its own
    ready = {"v": True}
    adapter.liveness_ready = lambda: ready["v"]  # type: ignore[assignment]
    sup = LivenessSupervisor(adapters={"web": adapter}, failure_threshold=1)

    await sup.probe_once("web")  # up -> ever_connected
    ready["v"] = False  # relay drops
    await sup.probe_once("web")

    assert sup.state["web"].detail == "relay websocket not connected"


# ---------------------------------------------- the real DiscordAdapter probe


class _FakeDiscordClient:
    def __init__(self, *, closed=False, ready=True, latency=0.042) -> None:
        self._closed = closed
        self._ready = ready
        self.latency = latency

    def is_closed(self) -> bool:
        return self._closed

    def is_ready(self) -> bool:
        return self._ready


class _StubDiscord:
    """Binds the REAL DiscordAdapter.probe_liveness to a stub self."""

    def __init__(self, client: _FakeDiscordClient, task: object | None = None) -> None:
        self._client = client
        if task is not None:
            self._task = task

    async def probe_liveness(self):  # noqa: ANN202
        from adapters.discord import DiscordAdapter

        return await DiscordAdapter.probe_liveness(self)  # type: ignore[arg-type]


class _DoneTask:
    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc

    def done(self) -> bool:
        return True

    def cancelled(self) -> bool:
        return False

    def exception(self) -> Exception | None:
        return self._exc


@pytest.mark.asyncio
async def test_discord_probe_catches_a_dead_gateway_task() -> None:
    """discord.py's start() runs fire-and-forget; if it dies the exception is
    swallowed and listen() waits on an empty queue forever — the Telegram wedge
    with a different logo."""
    stub = _StubDiscord(_FakeDiscordClient(), task=_DoneTask(RuntimeError("gateway closed 4004")))

    result = await stub.probe_liveness()

    assert result.healthy is False
    assert "gateway task died" in result.detail
    assert "4004" in result.detail  # the swallowed cause is finally surfaced


@pytest.mark.asyncio
async def test_discord_probe_catches_closed_and_unready_gateways() -> None:
    closed = await _StubDiscord(_FakeDiscordClient(closed=True)).probe_liveness()
    unready = await _StubDiscord(_FakeDiscordClient(ready=False)).probe_liveness()

    assert closed.healthy is False and "closed" in closed.detail
    assert unready.healthy is False and "not ready" in unready.detail


@pytest.mark.asyncio
async def test_discord_probe_reports_healthy_with_latency() -> None:
    result = await _StubDiscord(_FakeDiscordClient(latency=0.042)).probe_liveness()

    assert result.healthy is True
    assert "42ms" in result.detail


@pytest.mark.asyncio
async def test_discord_reconnect_refuses_honestly() -> None:
    """Discord cannot be revived in place — the supervisor must fail fast to a
    process restart rather than pretend it healed."""
    from adapters.discord import DiscordAdapter

    with pytest.raises(RuntimeError, match="cannot be restarted in-process"):
        await DiscordAdapter.reconnect(object())  # type: ignore[arg-type]


# ------------------------------------------------------------ diagnostics cache


@pytest.mark.asyncio
async def test_diagnostics_cache_is_empty_before_first_refresh() -> None:
    cache = DiagnosticsCache()
    assert cache.snapshot() is None
    assert cache.age_seconds() is None


@pytest.mark.asyncio
async def test_diagnostics_cache_populates_off_the_request_path(monkeypatch) -> None:
    import liveness

    class FakeReport:
        runtime_providers = {"claude": "ok"}
        memory_doc_count = 7272
        memory_embedding_status = "ready"
        cognition_available = True
        sessions_active = 4

    fake_mod = type("M", (), {"collect_diagnostics": staticmethod(lambda: FakeReport())})
    monkeypatch.setitem(sys.modules, "diagnostics", fake_mod)

    cache = liveness.DiagnosticsCache()
    await cache.refresh_once()

    assert cache.snapshot() == {
        "runtime_providers": {"claude": "ok"},
        "memory_doc_count": 7272,
        "memory_embedding_status": "ready",
        "cognition_available": True,
        "sessions_active": 4,
    }
    assert cache.age_seconds() is not None


@pytest.mark.asyncio
async def test_diagnostics_refresh_failure_keeps_the_previous_snapshot(monkeypatch) -> None:
    """Fail-open: a stale-but-real reading beats a blank one, and the climbing
    age is itself the signal that diagnostics is sick."""
    import liveness

    class FakeReport:
        runtime_providers: dict[str, str] = {}
        memory_doc_count = 1
        memory_embedding_status = "ready"
        cognition_available = True
        sessions_active = 1

    calls = {"n": 0}

    def collect() -> FakeReport:
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("db locked")
        return FakeReport()

    fake_mod = type("M", (), {"collect_diagnostics": staticmethod(collect)})
    monkeypatch.setitem(sys.modules, "diagnostics", fake_mod)

    cache = liveness.DiagnosticsCache()
    await cache.refresh_once()
    first = cache.snapshot()
    await cache.refresh_once()  # raises internally, must be swallowed

    assert cache.snapshot() == first
    assert first is not None
