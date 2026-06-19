"""Tests for business_signal.models — dataclass construction, serialization, defaults."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from business_signal.models import SignalDigest, SignalItem  # noqa: E402


class TestSignalItem:
    def test_construction_required_fields(self):
        item = SignalItem(source="rss:hn", title="Test", url="http://a", summary="s")
        assert item.source == "rss:hn"
        assert item.title == "Test"
        assert item.url == "http://a"
        assert item.summary == "s"

    def test_defaults(self):
        item = SignalItem(source="x", title="t", url="u", summary="s")
        assert item.relevance_score == 0.0
        assert item.tags == []
        assert item.fetched_at == ""
        assert item.content_angle is None

    def test_mutable_defaults_isolated(self):
        a = SignalItem(source="x", title="t", url="u", summary="s")
        b = SignalItem(source="x", title="t", url="u", summary="s")
        a.tags.append("test")
        assert b.tags == []

    def test_all_fields(self):
        item = SignalItem(
            source="haro",
            title="Title",
            url="http://b",
            summary="Summary",
            relevance_score=0.85,
            tags=["ai", "insurance"],
            fetched_at="2026-06-17T00:00:00Z",
            content_angle="AI in insurance",
        )
        assert item.relevance_score == 0.85
        assert item.content_angle == "AI in insurance"


class TestSignalDigest:
    def test_construction_defaults(self):
        d = SignalDigest(date="2026-06-17")
        assert d.items == []
        assert d.drafts_created == []
        assert d.sources_checked == 0
        assert d.sources_failed == 0
        assert d.total_fetched == 0
        assert d.total_triaged == 0
        assert d.markdown_body == ""

    def test_with_items(self):
        item = SignalItem(source="x", title="t", url="u", summary="s")
        d = SignalDigest(date="2026-06-17", items=[item], total_triaged=1)
        assert len(d.items) == 1
        assert d.total_triaged == 1

    def test_markdown_body(self):
        d = SignalDigest(date="2026-06-17", markdown_body="# Summary")
        assert d.markdown_body == "# Summary"
