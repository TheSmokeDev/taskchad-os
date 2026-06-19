"""Pluggable signal fetchers — protocol + registry.

Each fetcher is an independent data source that returns ``list[SignalItem]``.
The ``FetcherRegistry`` discovers and runs all registered fetchers with
per-fetcher error isolation (one failing does not block others).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol, runtime_checkable

from business_signal.models import SignalItem

logger = logging.getLogger(__name__)


@runtime_checkable
class BaseFetcher(Protocol):
    """Protocol for signal fetchers — structural typing, not ABC."""

    @property
    def name(self) -> str: ...

    async def fetch(self) -> list[SignalItem]: ...


class FetcherRegistry:
    """Discovers and runs registered fetchers with error isolation."""

    def __init__(self) -> None:
        self._fetchers: list[BaseFetcher] = []

    def register(self, fetcher: BaseFetcher) -> None:
        self._fetchers.append(fetcher)

    @property
    def fetchers(self) -> list[BaseFetcher]:
        return list(self._fetchers)

    async def fetch_all(self) -> tuple[list[SignalItem], int, int]:
        """Run all fetchers, return (items, sources_checked, sources_failed).

        Each fetcher is wrapped in independent try/except — mirrors the
        per-integration pattern in ``heartbeat.py:118-587``.
        """
        all_items: list[SignalItem] = []
        sources_checked = 0
        sources_failed = 0

        for fetcher in self._fetchers:
            sources_checked += 1
            try:
                items = await fetcher.fetch()
                all_items.extend(items)
                logger.info("Fetcher %s returned %d items", fetcher.name, len(items))
            except Exception:
                sources_failed += 1
                logger.exception("Fetcher %s failed (non-fatal)", fetcher.name)

        return all_items, sources_checked, sources_failed


def default_registry() -> FetcherRegistry:
    """Build a registry with all available fetchers.

    Fetchers that fail to import (missing deps, unconfigured) are skipped
    with a warning — never blocks the engine.
    """
    registry = FetcherRegistry()

    try:
        from business_signal.fetchers.rss_fetcher import RSSFetcher

        registry.register(RSSFetcher())
    except Exception:
        logger.warning("RSS fetcher unavailable", exc_info=True)

    try:
        from business_signal.fetchers.haro_fetcher import HAROFetcher

        registry.register(HAROFetcher())
    except Exception:
        logger.warning("HARO fetcher unavailable", exc_info=True)

    return registry


__all__ = [
    "BaseFetcher",
    "FetcherRegistry",
    "default_registry",
]
