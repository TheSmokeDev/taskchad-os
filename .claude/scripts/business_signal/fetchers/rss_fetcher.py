"""RSS/Atom feed fetcher — parses configured feeds via feedparser.

Deduplicates by URL against the signal state file (TTL-based).
Feed URLs configurable via ``SIGNAL_RSS_FEEDS`` env var (comma-separated)
or ``get_signal_settings().rss_feeds`` (Rule 1 call-time).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from business_signal.config import SIGNAL_STATE_FILE, get_signal_settings
from business_signal.models import SignalItem

logger = logging.getLogger(__name__)

SEEN_URL_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days


class RSSFetcher:
    """Fetch signal items from RSS/Atom feeds."""

    @property
    def name(self) -> str:
        return "rss"

    async def fetch(self) -> list[SignalItem]:
        try:
            import feedparser  # type: ignore[import-untyped]
        except ImportError:
            logger.warning("feedparser not installed — RSS fetcher skipped")
            return []

        settings = get_signal_settings()
        feeds = settings.rss_feeds
        if not feeds:
            logger.info("No RSS feeds configured")
            return []

        seen_urls = _load_seen_urls()
        now_ts = time.time()
        items: list[SignalItem] = []

        for feed_url in feeds:
            try:
                feed_items = _parse_feed(feedparser, feed_url, seen_urls, now_ts)
                items.extend(feed_items)
            except Exception:
                logger.exception("RSS feed failed (non-fatal): %s", feed_url)

        _save_seen_urls(seen_urls)
        return items


def _feed_name(url: str) -> str:
    """Derive a short feed name from the URL for the source field."""
    from urllib.parse import urlparse

    host = urlparse(url).hostname or url
    host = host.removeprefix("www.")
    parts = host.split(".")
    return parts[0] if parts else host


def _parse_feed(
    feedparser: object,
    feed_url: str,
    seen_urls: dict[str, float],
    now_ts: float,
) -> list[SignalItem]:
    """Parse a single RSS/Atom feed, dedup against seen_urls."""
    parse = getattr(feedparser, "parse")
    parsed = parse(feed_url)
    feed_label = _feed_name(feed_url)
    entries = getattr(parsed, "entries", []) or []
    items: list[SignalItem] = []

    for entry in entries:
        link = getattr(entry, "link", "") or ""
        if not link:
            continue
        if link in seen_urls:
            continue

        title = getattr(entry, "title", "") or ""
        summary = getattr(entry, "summary", "") or ""
        if not title:
            continue

        # Truncate summary to keep items compact
        if len(summary) > 500:
            summary = summary[:497] + "..."

        published = getattr(entry, "published", "") or ""
        fetched_at = datetime.now(timezone.utc).isoformat()

        items.append(
            SignalItem(
                source=f"rss:{feed_label}",
                title=title.strip(),
                url=link.strip(),
                summary=summary.strip(),
                fetched_at=fetched_at,
            )
        )
        seen_urls[link] = now_ts

    return items


def _load_seen_urls() -> dict[str, float]:
    """Load seen URLs from state file, pruning expired entries."""
    if not SIGNAL_STATE_FILE.exists():
        return {}
    try:
        data = json.loads(SIGNAL_STATE_FILE.read_text(encoding="utf-8"))
        raw: dict[str, float] = data.get("seen_urls", {})
    except (json.JSONDecodeError, OSError):
        return {}

    now_ts = time.time()
    return {url: ts for url, ts in raw.items() if now_ts - ts < SEEN_URL_TTL_SECONDS}


def _save_seen_urls(seen_urls: dict[str, float]) -> None:
    """Persist seen URLs back to state file (merges with existing state)."""
    import os

    SIGNAL_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, object] = {}
    if SIGNAL_STATE_FILE.exists():
        try:
            existing = json.loads(SIGNAL_STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}

    existing["seen_urls"] = seen_urls
    payload = json.dumps(existing, indent=2, default=str)
    tmp = SIGNAL_STATE_FILE.with_suffix(SIGNAL_STATE_FILE.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, SIGNAL_STATE_FILE)
