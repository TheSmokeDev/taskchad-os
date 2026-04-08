"""Lightweight health check HTTP server for monitoring and orchestration."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

_START_TIME = time.monotonic()


@dataclass
class HealthStatus:
    """Health check response payload."""

    status: str  # "ok" | "degraded" | "error"
    uptime_seconds: float
    adapters: dict[str, bool]
    sessions_active: int
    cognition_available: bool
    version: str = "1.0.0"
    timestamp: str = ""
    # Phase 6 extensions
    runtime_providers: dict[str, str] = field(default_factory=dict)
    memory_doc_count: int = 0
    memory_embedding_status: str = ""


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
