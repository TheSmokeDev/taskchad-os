"""Tests for recall_service — unified recall API."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure chat/ and scripts/ are on sys.path
_CHAT_DIR = Path(__file__).resolve().parent.parent.parent / "chat"
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from recall_service import (
    RecallResponse,
    SearchMode,
    _FallbackLog,
    _FallbackResult,
    _keyword_only_recall,
    recall,
    reindex_changed,
    reindex_file,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@dataclass
class FakeSearchResult:
    path: str = "daily/2026-03-20.md"
    start_line: int = 10
    end_line: int = 20
    text: str = "Lead volume dropped to 5 today"
    score: float = 0.8
    section_title: str = "Lead Stats"


FAKE_MEMORY_DIR = Path("/tmp/fake_memory")


# ---------------------------------------------------------------------------
# TestRecallServiceAPI
# ---------------------------------------------------------------------------


class TestRecallServiceAPI:
    @pytest.mark.asyncio
    async def test_recall_returns_recall_response(self):
        """Return type is always RecallResponse."""
        with patch("recall_service.RECALL_ENABLED", True, create=True), \
             patch("config.RECALL_ENABLED", True):
            resp = await recall(
                query="test query",
                memory_dir=FAKE_MEMORY_DIR,
                caller="chat",
                search_mode=SearchMode.KEYWORD,
            )
        assert isinstance(resp, RecallResponse)

    @pytest.mark.asyncio
    async def test_recall_disabled_returns_empty(self):
        """RECALL_ENABLED=false should short-circuit."""
        with patch("config.RECALL_ENABLED", False):
            resp = await recall(
                query="check my leads",
                memory_dir=FAKE_MEMORY_DIR,
                caller="chat",
            )
        assert resp.results == []
        assert resp.formatted_text == ""
        assert resp.log.tier == "disabled"

    @pytest.mark.asyncio
    async def test_recall_empty_query_returns_empty(self):
        """Empty string query should return empty, not crash."""
        with patch("config.RECALL_ENABLED", True):
            resp = await recall(
                query="",
                memory_dir=FAKE_MEMORY_DIR,
                caller="chat",
            )
        assert resp.results == []
        assert resp.log.tier == "empty_query"

    @pytest.mark.asyncio
    async def test_recall_whitespace_query_returns_empty(self):
        """Whitespace-only query should return empty."""
        with patch("config.RECALL_ENABLED", True):
            resp = await recall(
                query="   ",
                memory_dir=FAKE_MEMORY_DIR,
                caller="chat",
            )
        assert resp.results == []
        assert resp.log.tier == "empty_query"

    @pytest.mark.asyncio
    async def test_recall_caller_logged(self):
        """caller field appears in log."""
        with patch("config.RECALL_ENABLED", True), \
             patch("config.RECALL_MIN_SCORE", 0.3), \
             patch("memory_search.search_keyword", return_value=[]):
            resp = await recall(
                query="test query for heartbeat",
                memory_dir=FAKE_MEMORY_DIR,
                caller="heartbeat",
                search_mode=SearchMode.KEYWORD,
            )
        assert resp.log.caller == "heartbeat"

    @pytest.mark.asyncio
    async def test_recall_search_mode_logged(self):
        """search_mode field appears in log."""
        with patch("config.RECALL_ENABLED", True), \
             patch("config.RECALL_MIN_SCORE", 0.3), \
             patch("memory_search.search_keyword", return_value=[]):
            resp = await recall(
                query="test query",
                memory_dir=FAKE_MEMORY_DIR,
                caller="chat",
                search_mode=SearchMode.KEYWORD,
            )
        assert resp.log.search_mode == "keyword"

    @pytest.mark.asyncio
    async def test_recall_with_cognition_tier_skip(self):
        """Prefetched data triggers SKIP tier — no search."""
        with patch("config.RECALL_ENABLED", True):
            resp = await recall(
                query="check my leads",
                memory_dir=FAKE_MEMORY_DIR,
                caller="chat",
                has_prefetched=True,
            )
        assert resp.results == []
        assert resp.log.tier == "skip"

    @pytest.mark.asyncio
    async def test_recall_with_cognition_tier0_greeting(self):
        """Short greeting triggers TIER_0 — no search."""
        with patch("config.RECALL_ENABLED", True):
            resp = await recall(
                query="hi",
                memory_dir=FAKE_MEMORY_DIR,
                caller="chat",
            )
        assert resp.results == []
        assert resp.log.tier == "tier_0"

    @pytest.mark.asyncio
    async def test_recall_with_cognition_pipeline(self):
        """Full pipeline path via cognition when available."""
        from cognition.observability import RecallLog
        from cognition.recall import RecallResult

        fake_results = [
            RecallResult(
                path="MEMORY.md",
                start_line=1,
                end_line=10,
                text="Important decision",
                score=0.9,
                match_type="hybrid",
            )
        ]
        fake_log = RecallLog(tier="tier_1")

        with patch("config.RECALL_ENABLED", True), \
             patch("recall_service.run_recall_pipeline", new_callable=AsyncMock, return_value=(fake_results, fake_log)), \
             patch("recall_service.log_recall_event"):
            resp = await recall(
                query="what decisions did we make about the auth system?",
                memory_dir=FAKE_MEMORY_DIR,
                caller="chat",
            )
        assert len(resp.results) == 1
        assert resp.formatted_text  # non-empty
        assert resp.log.caller == "chat"

    @pytest.mark.asyncio
    async def test_recall_hybrid_mode_skips_tier_classification(self):
        """HYBRID mode forces pipeline without tier classification."""
        from cognition.observability import RecallLog

        fake_log = RecallLog(tier="tier_1")

        with patch("config.RECALL_ENABLED", True), \
             patch("recall_service.run_recall_pipeline", new_callable=AsyncMock, return_value=([], fake_log)), \
             patch("recall_service.log_recall_event"):
            resp = await recall(
                query="hi",  # Would normally be TIER_0
                memory_dir=FAKE_MEMORY_DIR,
                caller="chat",
                search_mode=SearchMode.HYBRID,
            )
        # Even "hi" goes through the pipeline in HYBRID mode
        assert resp.log.search_mode == "hybrid"


# ---------------------------------------------------------------------------
# TestKeywordOnlyFallback
# ---------------------------------------------------------------------------


class TestKeywordOnlyFallback:
    def test_keyword_fallback_returns_results(self):
        """When cognition available, returns RecallResult objects."""
        fake = FakeSearchResult()
        with patch("config.RECALL_MIN_SCORE", 0.3), \
             patch("memory_search.search_keyword", return_value=[fake]):
            resp = _keyword_only_recall("lead stats", "chat", 5)
        assert len(resp.results) == 1
        assert resp.formatted_text  # non-empty

    def test_keyword_fallback_formats_text(self):
        """Formatted text uses injection defense wrapper."""
        fake = FakeSearchResult()
        with patch("config.RECALL_MIN_SCORE", 0.3), \
             patch("memory_search.search_keyword", return_value=[fake]):
            resp = _keyword_only_recall("leads", "chat", 5)
        assert "recalled-memory" in resp.formatted_text
        assert "untrusted" in resp.formatted_text

    def test_keyword_fallback_respects_min_score(self):
        """Low-score results are filtered out."""
        fake = FakeSearchResult(score=0.1)  # Below 0.3 threshold
        with patch("config.RECALL_MIN_SCORE", 0.3), \
             patch("memory_search.search_keyword", return_value=[fake]):
            resp = _keyword_only_recall("leads", "chat", 5)
        assert resp.results == []
        assert resp.formatted_text == ""

    def test_keyword_fallback_handles_error(self):
        """Search failure returns empty, not crash."""
        with patch("config.RECALL_MIN_SCORE", 0.3), \
             patch("memory_search.search_keyword", side_effect=Exception("DB not found")):
            resp = _keyword_only_recall("leads", "chat", 5)
        assert resp.results == []
        assert resp.formatted_text == ""
        assert resp.log.latency_ms >= 0  # May be 0.0 on fast mocked paths

    def test_keyword_fallback_log_has_scores(self):
        """Log includes top_scores and results_returned."""
        fake = FakeSearchResult(score=0.85)
        with patch("config.RECALL_MIN_SCORE", 0.3), \
             patch("memory_search.search_keyword", return_value=[fake]):
            resp = _keyword_only_recall("leads", "chat", 5)
        assert resp.log.results_returned == 1
        assert 0.85 in resp.log.top_scores


# ---------------------------------------------------------------------------
# TestRecallServiceReindex
# ---------------------------------------------------------------------------


class TestRecallServiceReindex:
    def test_reindex_file_calls_index_file(self):
        """reindex_file delegates to memory_index.index_file."""
        mock_db = MagicMock()
        with patch("db.get_memory_db", return_value=mock_db), \
             patch("memory_index.index_file", return_value=5):
            result = reindex_file(Path("/tmp/test.md"), Path("/tmp"), generate_embeddings=False)
        assert result == 5
        mock_db.init_schema.assert_called_once()
        mock_db.close.assert_called_once()

    def test_reindex_changed_calls_sync_index(self):
        """reindex_changed delegates to memory_index.sync_index."""
        fake_stats = {"files_total": 10, "files_indexed": 2, "files_skipped": 8, "files_removed": 0, "chunks_total": 20}
        with patch("memory_index.sync_index", return_value=fake_stats):
            result = reindex_changed(Path("/tmp/memory"), generate_embeddings=False)
        assert result["files_indexed"] == 2
        assert result["files_total"] == 10


# ---------------------------------------------------------------------------
# TestRecallResponseDataclass
# ---------------------------------------------------------------------------


class TestRecallResponseDataclass:
    def test_recall_response_fields(self):
        """All fields present and accessible."""
        resp = RecallResponse(results=[], formatted_text="test", log=_FallbackLog())
        assert resp.results == []
        assert resp.formatted_text == "test"
        assert resp.log is not None

    def test_recall_response_empty_defaults(self):
        """Default log has empty values."""
        log = _FallbackLog()
        assert log.tier == ""
        assert log.caller == ""
        assert log.search_mode == ""
        assert log.results_returned == 0
        assert log.top_scores == []
        assert log.latency_ms == 0.0

    def test_fallback_result_fields(self):
        """_FallbackResult has same fields as RecallResult."""
        r = _FallbackResult(path="test.md", score=0.5, text="hello")
        assert r.path == "test.md"
        assert r.score == 0.5
        assert r.match_type == "keyword"
        assert r.graph_hops == 0
        assert r.source_query == ""


# ---------------------------------------------------------------------------
# TestSearchModeEnum
# ---------------------------------------------------------------------------


class TestSearchModeEnum:
    def test_auto_value(self):
        assert SearchMode.AUTO.value == "auto"

    def test_keyword_value(self):
        assert SearchMode.KEYWORD.value == "keyword"

    def test_hybrid_value(self):
        assert SearchMode.HYBRID.value == "hybrid"

    def test_string_comparison(self):
        """SearchMode is a str enum, can compare to strings."""
        assert SearchMode.AUTO == "auto"
