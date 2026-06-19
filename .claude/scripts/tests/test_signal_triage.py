"""Tests for business_signal.triage — threshold filtering, sorting, edge cases."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from business_signal.focus import ChannelFocus, default_focus  # noqa: E402
from business_signal.models import SignalItem  # noqa: E402
from business_signal.triage import triage_items  # noqa: E402


def _make_item(title: str, summary: str = "") -> SignalItem:
    return SignalItem(source="test", title=title, url="http://test", summary=summary)


class TestTriageItems:
    def test_filters_below_threshold(self):
        focus = default_focus()
        items = [
            _make_item("AI agents for insurance automation"),
            _make_item("Random sports news about football"),
        ]
        result = triage_items(items, focus, threshold=0.01)
        assert len(result) >= 1
        assert all(it.relevance_score >= 0.01 for it in result)

    def test_sorted_descending(self):
        focus = default_focus()
        items = [
            _make_item("startup news"),
            _make_item("AI agents insurance insurtech automation voice agent"),
        ]
        result = triage_items(items, focus, threshold=0.0)
        if len(result) >= 2:
            assert result[0].relevance_score >= result[1].relevance_score

    def test_empty_input(self):
        focus = default_focus()
        result = triage_items([], focus)
        assert result == []

    def test_all_below_threshold(self):
        focus = ChannelFocus(
            high_keywords={"xyznotreal"},
            medium_keywords=set(),
            skip_keywords=set(),
        )
        items = [_make_item("nothing matches here")]
        result = triage_items(items, focus, threshold=0.5)
        assert result == []

    def test_sets_relevance_score(self):
        focus = default_focus()
        items = [_make_item("AI agents for insurance")]
        result = triage_items(items, focus, threshold=0.0)
        assert result[0].relevance_score > 0.0

    def test_sets_tags(self):
        focus = default_focus()
        items = [_make_item("AI agents for insurance automation")]
        result = triage_items(items, focus, threshold=0.0)
        assert len(result[0].tags) > 0

    def test_skip_keyword_filtered(self):
        focus = ChannelFocus(
            high_keywords={"ai agent"},
            medium_keywords=set(),
            skip_keywords={"dockerfile"},
        )
        items = [_make_item("AI agent dockerfile tutorial")]
        result = triage_items(items, focus, threshold=0.01)
        assert result == []

    def test_threshold_boundary(self):
        focus = ChannelFocus(
            high_keywords={"test"},
            medium_keywords=set(),
            skip_keywords=set(),
        )
        items = [_make_item("test item")]
        result_low = triage_items(items, focus, threshold=0.0)
        assert len(result_low) == 1
