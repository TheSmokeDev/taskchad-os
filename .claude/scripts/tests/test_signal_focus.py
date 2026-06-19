"""Tests for business_signal.focus — score_relevance, ChannelFocus, edge cases."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from business_signal.focus import ChannelFocus, default_focus  # noqa: E402


class TestScoreRelevance:
    def test_high_keyword_match(self):
        focus = ChannelFocus(high_keywords={"ai agent"}, medium_keywords=set(), skip_keywords=set())
        score, matched = focus.score_relevance("Building an AI agent for insurance")
        assert score > 0.0
        assert "ai agent" in matched

    def test_medium_keyword_match(self):
        focus = ChannelFocus(high_keywords=set(), medium_keywords={"saas"}, skip_keywords=set())
        score, matched = focus.score_relevance("A new SaaS platform launched")
        assert score > 0.0
        assert "saas" in matched

    def test_skip_keyword_returns_zero(self):
        focus = ChannelFocus(
            high_keywords={"ai agent"},
            medium_keywords=set(),
            skip_keywords={"dockerfile"},
        )
        score, matched = focus.score_relevance("AI agent dockerfile tutorial")
        assert score == 0.0
        assert "dockerfile" in matched

    def test_no_match_returns_zero(self):
        focus = ChannelFocus(high_keywords={"ai agent"}, medium_keywords={"saas"}, skip_keywords=set())
        score, matched = focus.score_relevance("The weather is nice today")
        assert score == 0.0
        assert matched == []

    def test_empty_text(self):
        focus = default_focus()
        score, matched = focus.score_relevance("")
        assert score == 0.0

    def test_all_skip_text(self):
        focus = ChannelFocus(
            high_keywords=set(),
            medium_keywords=set(),
            skip_keywords={"docker", "k8s"},
        )
        score, matched = focus.score_relevance("Docker and k8s deployment guide")
        assert score == 0.0

    def test_score_normalized_0_to_1(self):
        focus = default_focus()
        score, _ = focus.score_relevance("ai agent insurance insurtech small business automation crypto defi bitcoin seo geo ai visibility lead generation voice agent ai receptionist")
        assert 0.0 <= score <= 1.0

    def test_high_weight_gt_medium(self):
        focus = ChannelFocus(
            high_keywords={"ai agent"},
            medium_keywords={"startup"},
            skip_keywords=set(),
        )
        score_high, _ = focus.score_relevance("ai agent platform")
        score_med, _ = focus.score_relevance("startup platform")
        assert score_high > score_med

    def test_empty_keyword_sets(self):
        focus = ChannelFocus()
        score, matched = focus.score_relevance("anything")
        assert score == 0.0
        assert matched == []


class TestDefaultFocus:
    def test_has_keywords(self):
        focus = default_focus()
        assert len(focus.high_keywords) > 0
        assert len(focus.medium_keywords) > 0
        assert len(focus.skip_keywords) > 0

    def test_covers_verticals(self):
        focus = default_focus()
        assert "ai agent" in focus.high_keywords or "ai agents" in focus.high_keywords
        assert "insurance" in focus.high_keywords
        assert "crypto" in focus.high_keywords
        assert "seo" in focus.high_keywords
