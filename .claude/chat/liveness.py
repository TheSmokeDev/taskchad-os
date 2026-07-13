"""Adapter liveness supervision + off-request-path diagnostics caching.

Why this module exists
----------------------
On 2026-07-12 the Telegram bot was found wedged for ~6 weeks: the process was
alive (a PID file existed, ``bot-status.sh`` said RUNNING) but Telegram polling
was dead. Three independent blind spots let that happen, and this module closes
the first two:

1. **The wedge was invisible in-process.** ``TelegramAdapter.listen()`` is
   ``while True: await self._queue.get()``. When PTB's updater dies, the queue
   simply stops filling — ``listen()`` never raises, so ``router._listen()``'s
   retry/backoff logic never fires. Nothing anywhere asked "is polling still
   actually running?"
2. **/health lied and blocked.** It reported ``adapters: {telegram: true}`` from
   *registration presence* (Rule 2 violation — a meta claim, not physical state)
   and ran ``collect_diagnostics()`` synchronously on the aiohttp event loop.

``LivenessSupervisor`` polls each adapter's *physical* state on an interval and
is the single source of truth for the ``adapters`` map in /health.
``DiagnosticsCache`` keeps the expensive diagnostics sweep off the request path
so /health answers instantly even while the bot is warming up.

Design rules
------------
* **Fail-open, never fail-silent.** Every unexpected error inside the loop is
  logged and the loop keeps running. The ONE deliberate hard failure is
  ``AdapterWedgedError`` (see below), which is the whole point of the module.
* **Bounded probes.** A probe is wrapped in ``asyncio.wait_for``. A supervisor
  that watches for hangs must never hang on its own probe.
* **Physical state only (Rule 2).** ``updater.running`` + a live ``get_me()``
  round-trip. Registration presence proves nothing; neither does a PID.
* **No LLM, no DB, no disk in the hot path.** The probe is one HTTP call.
* Traffic recency (``last_update_at``) is recorded for forensics but NEVER
  gates health — a quiet bot is not a dead bot.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


class AdapterWedgedError(RuntimeError):
    """An adapter is confirmed dead and could not be revived in-process.

    Raised by :meth:`LivenessSupervisor.run` only after the failure threshold is
    crossed AND every configured in-process reconnect attempt has failed. The
    bot treats this like a router crash: log it, notify, exit non-zero, and let
    the external supervisor (``bot_watchdog.py``, ``service.py``, or the Task
    Scheduler entry) bring up a clean process.

    Fail-fast is only safe because that external restarter exists — a dead bot
    that gets restarted beats a wedged bot that never does.
    """


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of a single adapter liveness probe."""

    healthy: bool
    detail: str = ""


@runtime_checkable
class LivenessProbeable(Protocol):
    """Adapters opt into supervision by implementing these two coroutines."""

    async def probe_liveness(self) -> ProbeResult: ...
    async def reconnect(self) -> None: ...


@dataclass
class AdapterLiveness:
    """Rolling liveness state for one adapter.

    ``healthy is None`` means "not yet probed" (warm-up) — deliberately distinct
    from ``False`` so a bot that has not completed its first probe round is never
    reported as degraded.

    ``critical`` answers one question: *if this adapter dies, is the bot deaf?*
    Telegram and Discord are gateways the operator talks THROUGH, so their death
    is worth restarting the process over. The web/relay adapter is an outbound
    link to an EXTERNAL service (Mission Control) that auto-reconnects on its
    own — restarting the bot cannot fix a Mission Control outage, and doing it
    every 5 minutes while MC is down would be a self-inflicted restart loop.
    Both are probed and both are reported; only one is restart-worthy.
    """

    name: str
    probed: bool = True
    critical: bool = True
    healthy: bool | None = None
    detail: str = ""
    consecutive_failures: int = 0
    last_ok_at: float | None = None  # wall clock (time.time)
    last_update_at: float | None = None  # newest inbound update, forensics only
    reconnects: int = 0
    ever_connected: bool = False
    escalated: bool = False  # one escalation per incident, reset on recovery

    def as_dict(self) -> dict[str, Any]:
        return {
            "probed": self.probed,
            "critical": self.critical,
            "healthy": self.healthy,
            "detail": self.detail,
            "consecutive_failures": self.consecutive_failures,
            "last_ok_at": (
                datetime.fromtimestamp(self.last_ok_at).isoformat()
                if self.last_ok_at
                else None
            ),
            "last_update_at": (
                datetime.fromtimestamp(self.last_update_at).isoformat()
                if self.last_update_at
                else None
            ),
            "reconnects": self.reconnects,
        }


@dataclass
class LivenessSupervisor:
    """Periodically proves each adapter is physically alive; heals or fails fast.

    The supervisor is also the read model for /health — ``adapter_health_map()``
    replaces the old registration-presence dict, so a wedged adapter now shows up
    as ``false`` instead of cheerfully reporting ``true`` for six weeks.
    """

    adapters: dict[str, Any]
    interval_seconds: int = 60
    probe_timeout_seconds: float = 10.0
    failure_threshold: int = 3
    reconnect_attempts: int = 1
    fail_fast: bool = True
    startup_grace_seconds: float = 60.0
    notify: Callable[[str, str], None] | None = None
    state: dict[str, AdapterLiveness] = field(default_factory=dict)
    _started_at: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        for name, adapter in self.adapters.items():
            self.state[name] = AdapterLiveness(
                name=name,
                # Adapters that cannot be probed are reported honestly as
                # unprobed rather than being silently assumed healthy.
                probed=isinstance(adapter, LivenessProbeable),
                # Default True (fail-safe): an adapter that does not declare
                # itself optional is assumed to be a gateway worth restarting for.
                critical=bool(getattr(adapter, "liveness_critical", True)),
            )

    # ---- read model (consumed by /health and the MC heartbeat) ----

    def adapter_health_map(self) -> dict[str, bool]:
        """Truthful ``{adapter: bool}`` map for the /health ``adapters`` field.

        Unprobed adapters and not-yet-probed (warming) adapters report ``True``:
        we only ever report ``False`` on *proven* death, so a missing probe can
        never manufacture a false alarm that the watchdog would act on.
        """
        return {
            name: (True if st.healthy is None else st.healthy)
            for name, st in self.state.items()
        }

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Detailed per-adapter liveness, exposed as the additive
        ``adapter_liveness`` field in /health."""
        return {name: st.as_dict() for name, st in self.state.items()}

    def any_unhealthy(self) -> bool:
        """True iff ANY adapter is PROVEN dead (warming never counts).

        Drives the honest ``degraded`` reading in /health and the MC heartbeat.
        Restart policy is a SEPARATE question — see ``any_critical_unhealthy``.
        """
        return any(st.healthy is False for st in self.state.values())

    def any_critical_unhealthy(self) -> bool:
        """True iff a GATEWAY the operator talks through is proven dead.

        This is the restart-worthy condition. A dead relay to Mission Control is
        reported (``any_unhealthy``) but never restarted over: the bot is not
        broken, the thing it dials is.
        """
        return any(st.healthy is False and st.critical for st in self.state.values())

    # ---- probe loop ----

    async def probe_once(self, name: str) -> None:
        """Probe one adapter and fold the result into its rolling state.

        Never raises for a probe failure — a failure is data, not an error. The
        only exception that escapes is :class:`AdapterWedgedError` from the
        escalation path.
        """
        st = self.state[name]
        adapter = self.adapters[name]
        # Pulled from the adapter rather than pushed by it: the adapter stamps
        # its own clock on enqueue and the supervisor samples it here, so no
        # hot-path call has to know a supervisor exists.
        st.last_update_at = getattr(adapter, "_last_update_at", None)
        if not st.probed:
            return

        # The supervisor and the router start concurrently, so an adapter may not
        # have finished connecting yet. Through ``updater.running`` alone, "not
        # started" and "died" are the same bit — so gate on the adapter's own
        # connect state (physical, Rule 2) instead of guessing from timing.
        #
        # Once an adapter has EVER been up, we stop using this gate and let the
        # probe speak: it reports the real cause ("updater not running", "relay
        # websocket not connected") instead of a misleading "never connected".
        if self._adapter_connected(adapter):
            st.ever_connected = True
        elif not st.ever_connected:
            # The grace has a HARD END: past it, an adapter that never connected
            # is a real failure, not a slow boot. Otherwise a permanently-failing
            # connect would hide behind "still warming up" forever — the exact
            # shape of the bug this module exists to kill.
            if time.monotonic() - self._started_at < self.startup_grace_seconds:
                return
            st.consecutive_failures += 1
            st.detail = "adapter never completed connect()"
            self._log(
                f"{name} has not connected "
                f"({st.consecutive_failures}/{self.failure_threshold})"
            )
            if st.consecutive_failures >= self.failure_threshold:
                st.healthy = False
                await self._escalate(name, st)
            return

        try:
            result = await asyncio.wait_for(
                adapter.probe_liveness(), timeout=self.probe_timeout_seconds
            )
        except TimeoutError:
            result = ProbeResult(
                False, f"probe exceeded {self.probe_timeout_seconds}s timeout"
            )
        except Exception as exc:  # noqa: BLE001 — a raising probe IS a failure
            result = ProbeResult(False, f"{type(exc).__name__}: {exc}")

        if result.healthy:
            if st.healthy is False:
                self._log(f"{name} recovered: {result.detail or 'probe ok'}")
                self._notify(
                    "The Homie Bot recovered",
                    f"{name} adapter is alive again after "
                    f"{st.consecutive_failures} failed probe(s).",
                )
            st.healthy = True
            st.detail = result.detail or "ok"
            st.consecutive_failures = 0
            st.escalated = False
            st.last_ok_at = time.time()
            return

        st.consecutive_failures += 1
        st.detail = result.detail
        self._log(
            f"{name} probe FAILED ({st.consecutive_failures}/"
            f"{self.failure_threshold}): {result.detail}"
        )
        if st.consecutive_failures < self.failure_threshold:
            # Below threshold we hold the previous verdict: a transient Telegram
            # API blip must not flip /health to degraded and trip the watchdog.
            return

        st.healthy = False
        await self._escalate(name, st)

    def _adapter_connected(self, adapter: Any) -> bool:
        """Has this adapter finished coming up? Physical state only.

        Adapters whose connection is owned elsewhere (the web adapter's relay
        socket lives in RelayWSClient) expose ``liveness_ready()``; the rest
        stamp ``_connected_at`` when their own connect() completes.
        """
        ready = getattr(adapter, "liveness_ready", None)
        if callable(ready):
            try:
                return bool(ready())
            except Exception:  # noqa: BLE001 — an unreadable state is "not up"
                return False
        return getattr(adapter, "_connected_at", "unset") is not None

    async def _escalate(self, name: str, st: AdapterLiveness) -> None:
        """Threshold crossed: try to self-heal in-process, else fail fast.

        Runs ONCE per incident (``st.escalated``), so a non-critical adapter that
        stays down does not re-notify and re-reconnect on every probe round.
        """
        if st.escalated:
            return
        st.escalated = True

        for attempt in range(1, self.reconnect_attempts + 1):
            self._log(
                f"{name} declared DEAD — reconnect attempt "
                f"{attempt}/{self.reconnect_attempts}"
            )
            try:
                await asyncio.wait_for(
                    self.adapters[name].reconnect(),
                    timeout=self.probe_timeout_seconds * 3,
                )
            except Exception as exc:  # noqa: BLE001
                self._log(f"{name} reconnect attempt {attempt} failed: {exc}")
                continue

            st.reconnects += 1
            # Re-probe immediately: a reconnect that "succeeded" but left polling
            # dead is exactly the failure mode we are here to catch. Trust the
            # probe, never the reconnect's own return.
            try:
                verify = await asyncio.wait_for(
                    self.adapters[name].probe_liveness(),
                    timeout=self.probe_timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001
                verify = ProbeResult(False, f"{type(exc).__name__}: {exc}")

            if verify.healthy:
                st.healthy = True
                st.detail = f"self-healed after reconnect: {verify.detail or 'ok'}"
                st.consecutive_failures = 0
                st.escalated = False
                st.last_ok_at = time.time()
                self._log(f"{name} SELF-HEALED via in-process reconnect")
                self._notify(
                    "The Homie Bot self-healed",
                    f"{name} adapter was dead and was revived by an in-process "
                    f"reconnect. No restart needed.",
                )
                return
            self._log(f"{name} still dead after reconnect: {verify.detail}")

        if not st.critical:
            # A non-gateway adapter (the Mission Control relay) is down. The bot
            # can still be talked to, and the relay client reconnects itself when
            # MC comes back. Killing the process here would turn someone else's
            # outage into ours, every 5 minutes, forever.
            self._log(
                f"{name} is down but NOT a gateway — reporting degraded, "
                f"not restarting ({st.detail})"
            )
            self._notify(
                "The Homie Bot: relay is down",
                f"{name} is disconnected ({st.detail}). Telegram/Discord still "
                f"work; the bot will not be restarted. It reconnects on its own "
                f"when the service returns.",
            )
            return

        if not self.fail_fast:
            self._log(
                f"{name} is dead and unrecoverable; fail-fast disabled — "
                f"staying up and reporting degraded"
            )
            return

        self._notify(
            "The Homie Bot is WEDGED — restarting",
            f"{name} adapter stopped responding and could not be revived "
            f"in-process ({st.detail}). Exiting so the watchdog restarts a "
            f"clean process.",
        )
        raise AdapterWedgedError(
            f"{name} adapter is dead and unrecoverable ({st.detail})"
        )

    async def run(self) -> None:
        """Probe every adapter forever. Raises :class:`AdapterWedgedError` on
        an unrecoverable wedge; that is the ONLY way this coroutine returns."""
        self._log(
            f"liveness supervisor started — probing "
            f"{sorted(n for n, s in self.state.items() if s.probed) or 'nothing'} "
            f"every {self.interval_seconds}s "
            f"(threshold={self.failure_threshold}, fail_fast={self.fail_fast})"
        )
        while True:
            for name in list(self.adapters):
                try:
                    await self.probe_once(name)
                except AdapterWedgedError:
                    raise
                except Exception as exc:  # noqa: BLE001 — monitoring is fail-open
                    self._log(f"supervisor error probing {name}: {exc}")
            await asyncio.sleep(self._next_interval())

    def _next_interval(self) -> float:
        """Re-check soon while an adapter is still connecting.

        The supervisor's first pass usually lands before the router finishes
        connecting, so without this the first real liveness proof is a full
        interval (60s) away even though connect completes in seconds.
        """
        still_connecting = any(
            st.probed
            and st.healthy is None
            and getattr(self.adapters[name], "_connected_at", None) is None
            for name, st in self.state.items()
        )
        if still_connecting:
            return min(5.0, float(self.interval_seconds))
        return float(self.interval_seconds)

    # ---- helpers ----

    def _log(self, message: str) -> None:
        print(f"[{datetime.now()}] [liveness] {message}", flush=True)

    def _notify(self, title: str, message: str) -> None:
        if self.notify is None:
            return
        try:
            self.notify(title, message)
        except Exception as exc:  # noqa: BLE001 — a failed toast never breaks the bot
            self._log(f"notification failed: {exc}")


def resolve_health_status(
    *,
    adapters: dict[str, bool],
    any_unhealthy: bool,
    has_diagnostics: bool,
    uptime_seconds: float,
    warmup_seconds: float,
) -> str:
    """Decide the /health ``status`` field. Pure — no I/O, trivially testable.

    The separation of concerns here is the whole lesson of the wedge:

    * **Adapter liveness decides ok/degraded.** That is the only condition worth
      restarting a bot over, and it is now backed by a real probe.
    * **Diagnostics decides warming, and nothing else.** Missing diagnostics must
      NEVER read as degraded: the watchdog restarts on degraded, and restarting
      a perfectly healthy bot because a chunk-count query is slow would turn a
      cosmetic problem into an outage. Past warm-up, a bot with healthy adapters
      and no diagnostics is ``ok`` — with ``diagnostics_age_seconds: null``
      telling the operator exactly what is stale.
    """
    if not adapters or any_unhealthy:
        return "degraded"
    if not has_diagnostics and uptime_seconds < warmup_seconds:
        return "warming"
    return "ok"


@dataclass
class DiagnosticsCache:
    """Keeps ``collect_diagnostics()`` OFF the /health request path.

    The old ``/health`` handler called ``collect_diagnostics()`` inline: cold
    imports, ``SELECT COUNT(*) FROM chunks``, runtime provider resolution, and
    CDP/adb probes — ~3.4s warm and effectively hung during warm-up, on the
    aiohttp event loop. This refreshes a snapshot on an interval in a worker
    thread; the handler only ever reads the cached dict.

    ``snapshot()`` returns ``None`` until the first refresh lands, which is what
    lets /health honestly answer ``status: "warming"`` instead of blocking.
    """

    ttl_seconds: float = 30.0
    _snapshot: dict[str, Any] | None = None
    _refreshed_at: float | None = None

    def snapshot(self) -> dict[str, Any] | None:
        return self._snapshot

    def age_seconds(self) -> float | None:
        if self._refreshed_at is None:
            return None
        return round(time.monotonic() - self._refreshed_at, 1)

    async def refresh_once(self) -> None:
        """Collect diagnostics in a worker thread and swap the snapshot in.

        Fail-open: on error the PREVIOUS snapshot is kept (a stale-but-real
        reading beats a blank one) and its age keeps climbing, which is itself
        the signal that diagnostics is unhealthy.
        """

        def _collect() -> dict[str, Any]:
            from diagnostics import collect_diagnostics

            report = collect_diagnostics()
            return {
                "runtime_providers": report.runtime_providers,
                "memory_doc_count": report.memory_doc_count,
                "memory_embedding_status": report.memory_embedding_status,
                "cognition_available": report.cognition_available,
            }

        try:
            self._snapshot = await asyncio.to_thread(_collect)
            self._refreshed_at = time.monotonic()
        except Exception as exc:  # noqa: BLE001
            print(
                f"[{datetime.now()}] [liveness] diagnostics refresh failed: {exc}",
                flush=True,
            )

    async def run(self) -> None:
        """Refresh forever. Never raises — /health degrades to a stale snapshot."""
        while True:
            await self.refresh_once()
            await asyncio.sleep(self.ttl_seconds)
