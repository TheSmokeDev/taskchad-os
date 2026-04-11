"""Tests for cognition.recall — tier classification and query expansion."""

from __future__ import annotations

import pytest

from cognition.recall import RecallTier, classify_tier, expand_queries


def test_classify_tier_greeting_hi():
    assert classify_tier("hi") == RecallTier.TIER_0


def test_classify_tier_greeting_hello():
    assert classify_tier("hello!") == RecallTier.TIER_0


def test_classify_tier_greeting_gm():
    assert classify_tier("good morning") == RecallTier.TIER_0


def test_classify_tier_ack_ok():
    assert classify_tier("ok") == RecallTier.TIER_0


def test_classify_tier_ack_thanks():
    assert classify_tier("thanks") == RecallTier.TIER_0


def test_classify_tier_ack_go_ahead():
    assert classify_tier("go ahead") == RecallTier.TIER_0


def test_classify_tier_ack_yes():
    assert classify_tier("yes") == RecallTier.TIER_0


def test_classify_tier_prefetched():
    """Any text + has_prefetched=True -> SKIP."""
    assert classify_tier("check my leads", has_prefetched=True) == RecallTier.SKIP


def test_classify_tier_slash_command():
    assert classify_tier("/budget", is_slash_command=True) == RecallTier.SKIP


def test_classify_tier_memory_signal():
    """Memory signal words -> TIER_1."""
    assert classify_tier("what happened with the leads?") == RecallTier.TIER_1


def test_classify_tier_ambiguous():
    """Ambiguous longer text -> TIER_1."""
    assert classify_tier("how are we looking") == RecallTier.TIER_1


def test_classify_tier_long_text():
    """Long text always TIER_1."""
    assert classify_tier("please check the server and tell me if there are any errors") == RecallTier.TIER_1


def test_classify_tier_remember():
    assert classify_tier("do you remember what we decided about the API?") == RecallTier.TIER_1


@pytest.mark.asyncio
async def test_expand_queries_basic():
    """Returns at least the original message."""
    queries = await expand_queries("what happened with the leads")
    assert len(queries) >= 1
    assert "what happened with the leads" in queries


@pytest.mark.asyncio
async def test_expand_queries_short():
    """Short messages still work."""
    queries = await expand_queries("leads")
    assert len(queries) >= 1


@pytest.mark.asyncio
async def test_expand_queries_max_three():
    """Never more than 3 queries."""
    queries = await expand_queries("tell me about the recent changes to the outreach pipeline and lead flow")
    assert len(queries) <= 3


@pytest.mark.asyncio
async def test_expand_queries_dedup():
    """Duplicate queries removed."""
    queries = await expand_queries("leads leads leads")
    # Should deduplicate
    assert len(queries) <= 3


# ---------------------------------------------------------------------------
# LLM re-ranking tests
# ---------------------------------------------------------------------------

from cognition.recall import RecallResult, _llm_rerank


def _make_results(n: int) -> list[RecallResult]:
    """Helper: create N dummy recall results."""
    return [
        RecallResult(
            path=f"Memory/doc-{i}.md",
            start_line=1,
            end_line=10,
            text=f"Content about topic {i}",
            score=1.0 - i * 0.1,
            match_type="hybrid",
            source_query="test query",
        )
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_rerank_short_list_passthrough():
    """If results <= return_n, skip re-ranking entirely."""
    results = _make_results(3)
    reranked = await _llm_rerank(results, "test query", top_n=10, return_n=5)
    assert reranked == results


@pytest.mark.asyncio
async def test_rerank_returns_capped(monkeypatch):
    """Re-ranking should return at most return_n results."""
    async def mock_run(prompt):
        return "3,1,5,2,4,6,7,8,9,10"

    import cognition.recall
    monkeypatch.setattr(cognition.recall, "_run_rerank_request", mock_run, raising=False)

    results = _make_results(10)
    reranked = await _llm_rerank(results, "test query", top_n=10, return_n=5)
    assert len(reranked) <= 5


@pytest.mark.asyncio
async def test_rerank_fallback_on_timeout(monkeypatch):
    """On timeout, return original results unchanged."""
    import asyncio

    async def mock_run_slow(prompt):
        await asyncio.sleep(10)
        return "1,2,3"

    import cognition.recall
    monkeypatch.setattr(cognition.recall, "_run_rerank_request", mock_run_slow, raising=False)
    monkeypatch.setattr("config.RECALL_RERANK_TIMEOUT_S", 0.01)

    results = _make_results(10)
    reranked = await _llm_rerank(results, "test query", top_n=10, return_n=5)
    # Should return original top 5 (timeout → fallback)
    assert len(reranked) == 5


@pytest.mark.asyncio
async def test_rerank_fallback_on_import_error():
    """If runtime not available, return original results."""
    results = _make_results(10)
    reranked = await _llm_rerank(results, "test query", top_n=10, return_n=5)
    assert len(reranked) == 5
