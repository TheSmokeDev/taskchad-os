"""Tests for Move 5c RAG pipeline — brainstorm query expansion.

Tests the LLM-based query expansion, heuristic fallback, and
the blank-context synthesis pattern from v1.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

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
        result = asyncio.run(
            expand_queries("what happened with the outreach campaigns")
        )
        assert len(result) >= 1
        assert isinstance(result[0], str)

    def test_fallback_on_brainstorm_failure(self):
        """If brainstorm step fails, falls back to heuristic."""
        from cognition.recall import expand_queries

        # Patch brainstorm to raise
        with patch("cognition.steps.brainstorm", side_effect=RuntimeError("LLM unavailable")):
            result = asyncio.run(
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

        results, log = asyncio.run(
            run_recall_pipeline("hi", RecallTier.TIER_0, Path("/nonexistent"))
        )
        assert results == []
        assert log.tier == "tier_0"

    def test_skip_returns_empty(self):
        from cognition.recall import RecallTier, run_recall_pipeline

        results, log = asyncio.run(
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


class TestRecallPipelineOffLoop:
    """Issue #129: the graph build and the neighbor search must not run on the
    bot's event loop, the neighbor search must run exactly once, and the
    final recall result set must be unchanged by the hoist."""

    TIER_1_QUERY = "what do we know about the outreach pipeline status"

    def _vault(self, tmp_path: Path) -> Path:
        memory_dir = tmp_path / "vault" / "memory"
        memory_dir.mkdir(parents=True)
        (tmp_path / "vault" / "data").mkdir()
        (memory_dir / "SOUL.md").write_text("See [[USER]]", encoding="utf-8")
        (memory_dir / "USER.md").write_text("Links to [[SOUL]]", encoding="utf-8")
        return memory_dir

    def _result(self, path: str):
        from cognition.recall import RecallResult

        return RecallResult(
            path=path,
            start_line=1,
            end_line=2,
            text=f"content of {path}",
            score=0.5,
            match_type="keyword",
        )

    def _patch_pipeline(self, monkeypatch, memory_dir: Path, record: list):
        """Stub the two blocking call sites, recording the thread each ran on.

        Both names are patched on cognition.recall (not cognition.graph):
        recall.py binds get_cached_memory_graph into its own module namespace
        at import time, so that is the name the call site actually resolves.
        """
        import cognition.graph as g_mod
        import cognition.recall as r_mod

        import config as cfg_mod

        # Deterministic: skip the LLM re-rank branch entirely.
        monkeypatch.setattr(cfg_mod, "RECALL_RERANK_ENABLED", False)

        def _fake_search(query, limit=5, memory_dir=None):
            record.append(("search", threading.get_ident()))
            return [self._result("SOUL.md"), self._result("USER.md")]

        def _fake_graph(md):
            record.append(("graph", threading.get_ident()))
            return g_mod.build_memory_graph(md)

        monkeypatch.setattr(r_mod, "_search_with_fallback", _fake_search)
        monkeypatch.setattr(r_mod, "get_cached_memory_graph", _fake_graph)

    def test_no_blocking_work_runs_on_the_event_loop_thread(
        self, tmp_path: Path, monkeypatch
    ):
        from cognition.recall import RecallTier, run_recall_pipeline

        memory_dir = self._vault(tmp_path)
        record: list = []
        self._patch_pipeline(monkeypatch, memory_dir, record)

        # asyncio.run drives the loop on THIS thread.
        loop_thread_ident = threading.get_ident()
        asyncio.run(
            run_recall_pipeline(self.TIER_1_QUERY, RecallTier.TIER_1, memory_dir)
        )

        assert record, "pipeline never reached the graph/search steps"
        assert any(kind == "graph" for kind, _ in record), "graph step never ran"
        assert any(kind == "search" for kind, _ in record), "search step never ran"
        offenders = [kind for kind, ident in record if ident == loop_thread_ident]
        assert not offenders, (
            f"{offenders} ran synchronously on the event-loop thread — this is the "
            "wedge issue #129 fixes"
        )

    def test_neighbor_search_runs_exactly_once(self, tmp_path: Path, monkeypatch):
        """Neighbor content search is hoisted out of the per-neighbor loop."""
        from cognition.recall import RecallTier, expand_queries, run_recall_pipeline

        memory_dir = self._vault(tmp_path)
        record: list = []
        self._patch_pipeline(monkeypatch, memory_dir, record)

        asyncio.run(
            run_recall_pipeline(self.TIER_1_QUERY, RecallTier.TIER_1, memory_dir)
        )

        queries = asyncio.run(expand_queries(self.TIER_1_QUERY))
        searches = sum(1 for kind, _ in record if kind == "search")
        # One per expanded query (Step 2) + exactly ONE neighbor search (Step 3),
        # never one per neighbor path.
        assert searches == len(queries) + 1, (
            f"expected {len(queries)} query searches + 1 neighbor search, "
            f"got {searches}"
        )

    def test_recall_results_unchanged_for_a_fixed_vault(
        self, tmp_path: Path, monkeypatch
    ):
        """Parity guard: the hoist must not change what recall returns.

        The old loop appended len(neighbor_paths) identical copies, which
        _merge_and_rank() dedups by path:line-range; one call must produce the
        same final set.
        """
        from cognition.recall import RecallTier, run_recall_pipeline

        memory_dir = self._vault(tmp_path)
        record: list = []
        self._patch_pipeline(monkeypatch, memory_dir, record)

        results, log = asyncio.run(
            run_recall_pipeline(self.TIER_1_QUERY, RecallTier.TIER_1, memory_dir)
        )

        # Dedup held: one entry per distinct path:line-range, no duplicates.
        keys = [f"{r.path}:{r.start_line}-{r.end_line}" for r in results]
        assert len(keys) == len(set(keys)), f"duplicate results leaked: {keys}"
        assert {r.path for r in results} == {"SOUL.md", "USER.md"}
        assert log.graph_neighbors_found >= 1, "fixture must produce a neighbor"

    def test_graph_task_exception_falls_back_to_empty_graph(
        self, tmp_path: Path, monkeypatch
    ):
        """A get_cached_memory_graph() exception inside the executor must not
        crash the pipeline — it should fall back to an empty MemoryGraph. This
        is new behavior: pre-PR, a graph-build failure propagated directly."""
        import cognition.recall as r_mod
        from cognition.recall import RecallTier, run_recall_pipeline

        memory_dir = self._vault(tmp_path)
        record: list = []
        self._patch_pipeline(monkeypatch, memory_dir, record)

        def _raising_graph(md):
            raise RuntimeError("simulated vault-read failure")

        monkeypatch.setattr(r_mod, "get_cached_memory_graph", _raising_graph)

        results, log = asyncio.run(
            run_recall_pipeline(self.TIER_1_QUERY, RecallTier.TIER_1, memory_dir)
        )

        assert log.graph_neighbors_found == 0
        assert {r.path for r in results} == {"SOUL.md", "USER.md"}


# =============================================================================
# #136 — cross-scale merge/rank + per-leg floors
# =============================================================================


def _mk(path: str, score: float, match_type: str, start_line: int = 1, end_line: int = 2):
    from cognition.recall import RecallResult

    return RecallResult(
        path=path,
        start_line=start_line,
        end_line=end_line,
        text=f"content of {path}",
        score=score,
        match_type=match_type,
    )


class TestMergeAndRankCrossScale:
    """_merge_and_rank must guarantee each leg's best hit survives the top-N cap
    WITHOUT rewriting scores — evolve replay/compare/veto consume absolute
    top_scores, so normalization is forbidden (#136 gate, both vendors)."""

    def test_merge_and_rank_promotes_starved_leg_into_cap_window(self):
        from cognition.graph import MemoryGraph
        from cognition.recall import _merge_and_rank

        # Raw sort ranks the keyword hit (0.12) BELOW all five hybrid hits
        # (0.20-0.36) → cut from top-3. That is the #136 bury.
        results = [_mk("exact-id.md", 0.12, "keyword")] + [
            _mk(f"hybrid-{i}.md", 0.2 + i * 0.04, "hybrid") for i in range(5)
        ]
        merged = _merge_and_rank(results, MemoryGraph(), top_n=3)
        top3 = [r.path for r in merged[:3]]
        assert "exact-id.md" in top3, (
            f"exact keyword hit must survive the cap via leg representation; got {top3}"
        )
        # Scores are raw, NOT normalized — the promoted hit keeps its FTS5 score.
        promoted = next(r for r in merged if r.path == "exact-id.md")
        assert promoted.score == pytest.approx(0.12)

    def test_merge_and_rank_no_reverse_bury(self):
        """The #136-gate counter-case: a lone floor-passing-but-weak keyword hit
        (0.021) must NOT outrank strong hybrids — it gets exactly the tail slot
        of the cap window, never the top (normalization put it at rank 1)."""
        from cognition.graph import MemoryGraph
        from cognition.recall import _merge_and_rank

        results = [_mk("weak-kw.md", 0.021, "keyword")] + [
            _mk(f"strong-hy-{i}.md", 0.5 + i * 0.08, "hybrid") for i in range(5)
        ]
        merged = _merge_and_rank(results, MemoryGraph(), top_n=3)
        # Slots 1-2 stay with the strongest hybrids; the keyword takes slot 3.
        assert merged[0].path == "strong-hy-4.md"
        assert merged[1].path == "strong-hy-3.md"
        assert merged[2].path == "weak-kw.md"
        assert merged[2].score == pytest.approx(0.021)  # raw, never 1.0

    def test_merge_and_rank_no_top_n_is_pure_raw_ranking(self):
        """Without top_n (non-capped callers) ranking is raw and untouched."""
        from cognition.graph import MemoryGraph
        from cognition.recall import _merge_and_rank

        results = [
            _mk("a.md", 0.10, "keyword"),
            _mk("b.md", 0.30, "keyword"),
            _mk("c.md", 0.20, "keyword"),
        ]
        merged = _merge_and_rank(results, MemoryGraph())
        assert [r.path for r in merged] == ["b.md", "c.md", "a.md"]
        # Raw score preserved — the absolute-score contract evolve depends on.
        assert merged[0].score == pytest.approx(0.30)

    def test_merge_and_rank_zero_score_leg_is_safe(self):
        """All-zero legs stay untouched and never crash the ranker."""
        from cognition.graph import MemoryGraph
        from cognition.recall import _merge_and_rank

        results = [_mk("z1.md", 0.0, "keyword"), _mk("z2.md", 0.0, "keyword")]
        merged = _merge_and_rank(results, MemoryGraph())  # must not raise
        assert {r.path for r in merged} == {"z1.md", "z2.md"}
        assert all(r.score == 0.0 for r in merged)

    def test_merge_and_rank_dedup_keeps_higher_raw_score_across_legs(self):
        """Same chunk returned by both legs (search_hybrid folds in its own
        keyword search) — dedup must compare RAW scores. Normalization was
        Codex-blocked (#136 gate — see the leg-representation docstring),
        so this locks the current raw-score dedup contract in place."""
        from cognition.graph import MemoryGraph
        from cognition.recall import _merge_and_rank

        results = [
            _mk("shared.md", 0.12, "keyword"),
            _mk("shared.md", 0.35, "hybrid"),
        ]
        merged = _merge_and_rank(results, MemoryGraph())
        shared = [r for r in merged if r.path == "shared.md"]
        assert len(shared) == 1, "dedup must collapse the shared key to one result"
        assert shared[0].match_type == "hybrid"
        assert shared[0].score == pytest.approx(0.35), (
            "the winning raw score must survive dedup untouched, never normalized"
        )


class _FakeSearchResult:
    """Stand-in for memory_search's SearchResult — just the fields
    _search_with_fallback reads off each hit."""

    def __init__(self, path: str, score: float):
        self.path = path
        self.start_line = 1
        self.end_line = 2
        self.text = "x"
        self.score = score
        self.section_title = ""


class TestSearchWithFallbackFloors:
    """_search_with_fallback must wire the two documented floors: RECALL_MIN_SCORE
    into search_hybrid (hybrid/vector scale) and RECALL_KEYWORD_MIN_SCORE on the
    raw FTS5 keyword leg."""

    def test_search_with_fallback_wires_recall_min_score(self, monkeypatch):
        from cognition.recall import _search_with_fallback

        import config as cfg
        import memory_search as ms

        captured: dict = {}

        def fake_keyword(query, limit=5, memory_dir=None):
            return [_FakeSearchResult("kw-hi.md", 0.10), _FakeSearchResult("kw-lo.md", 0.001)]

        def fake_hybrid(query, limit=5, min_score=None, memory_dir=None):
            captured["min_score"] = min_score
            return [_FakeSearchResult("hy.md", 0.5)]

        monkeypatch.setattr(cfg, "RECALL_MIN_SCORE", 0.42)
        monkeypatch.setattr(cfg, "RECALL_KEYWORD_MIN_SCORE", 0.02)
        monkeypatch.setattr(ms, "search_keyword", fake_keyword)
        monkeypatch.setattr(ms, "search_hybrid", fake_hybrid)

        results = _search_with_fallback("q")
        paths = {r.path for r in results}

        # Keyword floor drops the 0.001 hit, keeps the 0.10 hit.
        assert "kw-hi.md" in paths
        assert "kw-lo.md" not in paths
        # RECALL_MIN_SCORE is threaded straight into search_hybrid (was ignored).
        assert captured["min_score"] == 0.42

    def test_search_with_fallback_keyword_floor_boundary_is_inclusive(self, monkeypatch):
        """The floor check is `< RECALL_KEYWORD_MIN_SCORE` (strictly-less), so a
        score exactly AT the floor must be kept, not dropped."""
        from cognition.recall import _search_with_fallback

        import config as cfg
        import memory_search as ms

        def fake_keyword(query, limit=5, memory_dir=None):
            return [_FakeSearchResult("kw-boundary.md", 0.02)]

        def fake_hybrid(query, limit=5, min_score=None, memory_dir=None):
            return []

        monkeypatch.setattr(cfg, "RECALL_KEYWORD_MIN_SCORE", 0.02)
        monkeypatch.setattr(ms, "search_keyword", fake_keyword)
        monkeypatch.setattr(ms, "search_hybrid", fake_hybrid)

        results = _search_with_fallback("q")
        paths = {r.path for r in results}

        assert "kw-boundary.md" in paths, "a score exactly at the floor must be kept (< not <=)"
