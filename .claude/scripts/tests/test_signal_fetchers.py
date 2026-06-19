"""Tests for business_signal.fetchers — RSS + HARO mocks, dedup, error isolation."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from business_signal.fetchers import FetcherRegistry  # noqa: E402
from business_signal.fetchers.rss_fetcher import RSSFetcher, _feed_name, _load_seen_urls, _parse_feed  # noqa: E402
from business_signal.models import SignalItem  # noqa: E402


class TestFeedName:
    def test_simple_url(self):
        assert _feed_name("https://techcrunch.com/feed/") == "techcrunch"

    def test_www_prefix_stripped(self):
        assert _feed_name("https://www.example.com/rss") == "example"

    def test_hn_url(self):
        assert _feed_name("https://hnrss.org/newest?points=50") == "hnrss"


class TestRSSFetcher:
    def test_name(self):
        f = RSSFetcher()
        assert f.name == "rss"

    @pytest.mark.asyncio
    async def test_fetch_with_no_feeds(self, monkeypatch):
        monkeypatch.setenv("SIGNAL_RSS_FEEDS", "")
        monkeypatch.setenv("SIGNAL_ENABLED", "true")
        mock_fp = MagicMock()
        mock_fp.parse.return_value = MagicMock(entries=[])
        with patch.dict("sys.modules", {"feedparser": mock_fp}):
            f = RSSFetcher()
            items = await f.fetch()
            assert isinstance(items, list)

    @pytest.mark.asyncio
    async def test_fetch_returns_signal_items(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SIGNAL_RSS_FEEDS", "https://test.com/feed")
        state_file = tmp_path / "signal-state.json"

        mock_entry = MagicMock()
        mock_entry.link = "https://test.com/article-1"
        mock_entry.title = "Test Article"
        mock_entry.summary = "This is a test article summary"
        mock_entry.published = "2026-06-17"

        mock_fp = MagicMock()
        mock_fp.parse.return_value = MagicMock(entries=[mock_entry])

        from business_signal.fetchers import rss_fetcher

        with (
            patch.dict("sys.modules", {"feedparser": mock_fp}),
            patch.object(rss_fetcher, "SIGNAL_STATE_FILE", state_file),
        ):
            f = RSSFetcher()
            items = await f.fetch()
            assert len(items) == 1
            assert items[0].source.startswith("rss:")
            assert items[0].title == "Test Article"
            assert items[0].url == "https://test.com/article-1"


class TestRSSDedup:
    def test_load_seen_urls_empty(self, tmp_path):
        state_file = tmp_path / "state.json"
        with patch("business_signal.fetchers.rss_fetcher.SIGNAL_STATE_FILE", state_file):
            result = _load_seen_urls()
            assert result == {}

    def test_load_seen_urls_prunes_expired(self, tmp_path):
        import time

        state_file = tmp_path / "state.json"
        old_ts = time.time() - (8 * 24 * 3600)  # 8 days old
        fresh_ts = time.time() - 3600  # 1 hour old
        state_file.write_text(
            json.dumps({"seen_urls": {"http://old": old_ts, "http://fresh": fresh_ts}}),
            encoding="utf-8",
        )
        with patch("business_signal.fetchers.rss_fetcher.SIGNAL_STATE_FILE", state_file):
            result = _load_seen_urls()
            assert "http://old" not in result
            assert "http://fresh" in result

    def test_parse_feed_dedup(self):
        mock_entry = MagicMock()
        mock_entry.link = "http://already-seen"
        mock_entry.title = "Old Article"
        mock_entry.summary = "already seen"

        mock_fp = MagicMock()
        mock_fp.parse.return_value = MagicMock(entries=[mock_entry])

        import time

        seen = {"http://already-seen": time.time()}
        items = _parse_feed(mock_fp, "https://test.com/feed", seen, time.time())
        assert len(items) == 0


class TestFetcherRegistry:
    @pytest.mark.asyncio
    async def test_error_isolation(self):
        registry = FetcherRegistry()

        class FailingFetcher:
            @property
            def name(self) -> str:
                return "failing"

            async def fetch(self) -> list[SignalItem]:
                raise RuntimeError("boom")

        class WorkingFetcher:
            @property
            def name(self) -> str:
                return "working"

            async def fetch(self) -> list[SignalItem]:
                return [SignalItem(source="working", title="ok", url="http://ok", summary="ok")]

        registry.register(FailingFetcher())
        registry.register(WorkingFetcher())

        items, checked, failed = await registry.fetch_all()
        assert checked == 2
        assert failed == 1
        assert len(items) == 1
        assert items[0].source == "working"
