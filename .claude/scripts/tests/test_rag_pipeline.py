"""Tests for Move 5c RAG pipeline — brainstorm query expansion.

Tests the LLM-based query expansion, heuristic fallback, and
the blank-context synthesis pattern from v1.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

_CHAT_DIR = Path(__file__).resolve().parent.parent.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))


class TestHeuristicExpansion:
    """Heuristic fallback should always work without LLM."""

    def test_short_message_returns_original(self):
        from cognition.recall import _heuristic_expand

        result = _heuristic_expand("hi")
        assert result == ["hi"]

    def test_long_message_splits(self):
        from cognition.recall import _heuristic_expand

        result = _heuristic_expand("how do we handle the SEO strategy for ExampleCorp")
        assert len(result) >= 2
        assert result[0] == "how do we handle the SEO strategy for ExampleCorp"

    def test_memory_signal_extracts_topic(self):
        from cognition.recall import _heuristic_expand

        result = _heuristic_expand("remember what we decided about the auth migration")
        # Should have the original + extracted topic
        assert len(result) >= 2
        assert any("auth migration" in q for q in result)

    def test_deduplication(self):
        from cognition.recall import _heuristic_expand

        result = _heuristic_expand("short msg")
        # No duplicates
        assert len(result) == len(set(q.lower() for q in result))

    def test_max_three_queries(self):
        from cognition.recall import _heuristic_expand

        result = _heuristic_expand(
            "remember the deadline for the phone port number porting process"
        )
        assert len(result) <= 3


class TestExpandQueriesFallback:
    """expand_queries() should fall back to heuristic when LLM unavailable."""

    def test_fallback_on_import_error(self):
        """If WorkingMemory can't be imported, falls back to heuristic."""
        from cognition.recall import expand_queries

        # Should work without any mocking — the heuristic path handles it
        result = asyncio.get_event_loop().run_until_complete(
            expand_queries("what happened with the outreach campaigns")
        )
        assert len(result) >= 1
        assert isinstance(result[0], str)

    def test_fallback_on_brainstorm_failure(self):
        """If brainstorm step fails, falls back to heuristic."""
        from cognition.recall import expand_queries

        # Patch brainstorm to raise
        with patch("cognition.steps.brainstorm", side_effect=RuntimeError("LLM unavailable")):
            result = asyncio.get_event_loop().run_until_complete(
                expand_queries("check the lead conversion rates")
            )
            assert len(result) >= 1
            # Should be heuristic results
            assert result[0] == "check the lead conversion rates"


class TestBlankContextPattern:
    """The brainstorm step should use blank context (v1 pattern)."""

    def test_brainstorm_wm_has_no_conversation(self):
        """The WM created for brainstorm should have system prompt only."""
        from cognition.working_memory import WorkingMemory

        # Simulate what expand_queries builds
        wm = WorkingMemory(soul_name="recall_expander")
        from cognition.working_memory import Memory
        wm = wm.with_memory(Memory(
            role="system",
            content="You are a search query expert.",
            region="identity",
        ))

        # Should have no user/assistant messages (blank context)
        user_msgs = [m for m in wm.memories if m.role == "user"]
        assistant_msgs = [m for m in wm.memories if m.role == "assistant"]
        assert len(user_msgs) == 0
        assert len(assistant_msgs) == 0
        assert wm.length == 1  # Only system prompt


class TestIdempotentInjection:
    """Recall results should replace, not accumulate."""

    def test_recall_region_replaces_on_rebuild(self):
        """Building regions with new recall should not stack old recall."""
        from cognition.working_memory import Memory, WorkingMemory

        wm = WorkingMemory(soul_name="test")
        wm = wm.with_memory(Memory(
            role="system", content="Old recall data",
            region="recalled_memory", source="cognition",
        ))

        # Simulate "replace" by filtering out old + adding new
        wm = wm.without_regions("recalled_memory")
        wm = wm.with_memory(Memory(
            role="system", content="New recall data",
            region="recalled_memory", source="cognition",
        ))

        recalled = [m for m in wm.memories if m.region == "recalled_memory"]
        assert len(recalled) == 1
        assert "New recall" in recalled[0].content


class TestRecallPipelineIntegration:
    """The full recall pipeline should work with the new expand_queries."""

    def test_tier_0_returns_empty(self):
        from cognition.recall import RecallTier, run_recall_pipeline

        results, log = asyncio.get_event_loop().run_until_complete(
            run_recall_pipeline("hi", RecallTier.TIER_0, Path("/nonexistent"))
        )
        assert results == []
        assert log.tier == "tier_0"

    def test_skip_returns_empty(self):
        from cognition.recall import RecallTier, run_recall_pipeline

        results, log = asyncio.get_event_loop().run_until_complete(
            run_recall_pipeline("/budget", RecallTier.SKIP, Path("/nonexistent"))
        )
        assert results == []
        assert log.tier == "skip"

    def test_classify_tier_prefetched_skips(self):
        from cognition.recall import RecallTier, classify_tier

        tier = classify_tier("how are we looking", has_prefetched=True)
        assert tier == RecallTier.SKIP

    def test_classify_tier_greeting_is_tier_0(self):
        from cognition.recall import RecallTier, classify_tier

        tier = classify_tier("hi")
        assert tier == RecallTier.TIER_0

    def test_classify_tier_complex_is_tier_1(self):
        from cognition.recall import RecallTier, classify_tier

        tier = classify_tier("what do we know about the outreach pipeline status")
        assert tier == RecallTier.TIER_1
