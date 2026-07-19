"""Lightweight health check HTTP server for monitoring and orchestration."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from update_check import get_current_version

_START_TIME = time.monotonic()
# Read once at import: the running process's version cannot change without a
# restart, and /health must never touch disk on the request path (#132).
_VERSION = get_current_version()


@dataclass
class HealthStatus:
    """Health check response payload.

    Field additions are ADDITIVE ONLY — Mission Control and the bot watchdog
    both parse this payload, so existing keys keep their names and types.
    """

    # "ok" | "degraded" | "error" | "warming".
    # "warming" (added with the liveness work) means the process is up and
    # adapters are not proven dead, but the diagnostics snapshot has not landed
    # yet. Consumers that don't know the value should treat it as "not ok yet",
    # NOT as a failure — the watchdog grants it a grace window.
    status: str
    uptime_seconds: float
    # Probe-backed truth, NOT registration presence. An adapter reports False
    # only when a liveness probe PROVED it dead; unprobed/warming adapters
    # report True so a missing probe can never manufacture a false alarm.
    adapters: dict[str, bool]
    sessions_active: int
    cognition_available: bool
    version: str = _VERSION
    timestamp: str = ""
    # Phase 6 extensions
    runtime_providers: dict[str, str] = field(default_factory=dict)
    memory_doc_count: int = 0
    memory_embedding_status: str = ""
    # Liveness extensions (additive).
    # adapter_liveness: per-adapter probe detail — healthy (True/False/None for
    #   not-yet-probed), consecutive_failures, last_ok_at, last_update_at,
    #   reconnects. This is the forensic surface that was missing when the bot
    #   sat wedged for six weeks.
    # diagnostics_age_seconds: age of the cached diagnostics snapshot; None
    #   until the first refresh lands. Diagnostics is collected on a background
    #   interval, never on this request path.
    adapter_liveness: dict[str, Any] = field(default_factory=dict)
    diagnostics_age_seconds: float | None = None


class HealthServer:
    """Lightweight health check server for monitoring and orchestration."""

    def __init__(self, port: int, status_fn: Callable[[], HealthStatus] | None = None) -> None:
        self.port = port
        self._status_fn = status_fn
        self._runner: Any = None

    async def start(self) -> None:
        """Start the health check HTTP server."""
        from aiohttp import web

        app = web.Application()
        app.router.add_get("/health", self._handle_health)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await site.start()
        print(f"[{datetime.now()}] Health check on port {self.port}")

    async def stop(self) -> None:
        """Stop the health check server."""
        if self._runner:
            await self._runner.cleanup()

    async def _handle_health(self, request: Any) -> Any:
        """Handle GET /health requests."""
        from aiohttp import web

        if self._status_fn:
            status = self._status_fn()
        else:
            status = HealthStatus(
                status="ok",
                uptime_seconds=round(time.monotonic() - _START_TIME, 1),
                adapters={},
                sessions_active=0,
                cognition_available=False,
                timestamp=datetime.now().isoformat(),
            )
        status.timestamp = status.timestamp or datetime.now().isoformat()
        status.uptime_seconds = round(time.monotonic() - _START_TIME, 1)
        return web.json_response(asdict(status))
