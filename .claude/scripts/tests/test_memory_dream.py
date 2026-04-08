"""
Tests for memory_dream.py — Dream Consolidation Cycle.

All tests are pure Python — no LLM calls, no network, no real file system
writes beyond tmp_path fixtures.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch
from zoneinfo import ZoneInfo

import pytest

# Ensure scripts dir is on path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# =============================================================================
# FIXTURES
# =============================================================================


@pytest.fixture
def mock_memory_dir(tmp_path):
    """Create a minimal memory directory structure."""
    memory_dir = tmp_path / "TheHomie" / "Memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "concepts").mkdir()
    (memory_dir / "daily").mkdir()

    # MEMORY.md with some content
    memory_md = memory_dir / "MEMORY.md"
    memory_md.write_text(
        "---\ntags: [system]\n---\n# MEMORY.md\n\n"
        "## Key Decisions\n\n"
        "- **SQLite default** — use SQLite for all local storage\n"
        "- **Provider-agnostic** — run_with_fallback for all LLM calls\n",
        encoding="utf-8",
    )

    # SELF.md
    self_md = memory_dir / "SELF.md"
    self_md.write_text(
        "---\ntags: [system]\n---\n# SELF.md\n\n## Patterns\n\n- Test pattern\n",
        encoding="utf-8",
    )

    # GOALS.md
    goals_md = memory_dir / "GOALS.md"
    goals_md.write_text("# GOALS\n\n## Q2 2026\n\n- Ship dream cycle\n", encoding="utf-8")

    # Some concept pages
    for name in ["HERMES-AGENT", "CONVOY-SYSTEM", "LANGFUSE"]:
        (memory_dir / "concepts" / f"{name}.md").write_text(f"# {name}\n", encoding="utf-8")

    return memory_dir


@pytest.fixture
def mock_daily_logs(mock_memory_dir):
    """Create mock daily logs with various signal patterns."""
    daily_dir = mock_memory_dir / "daily"
    tz = ZoneInfo("America/Chicago")
    today = datetime.now(tz).date()
    logs = []

    # Log with corrections
    log1 = daily_dir / f"{(today - timedelta(days=1)).strftime('%Y-%m-%d')}.md"
    log1.write_text(
        "# Daily Log\n\n"
        "## Sessions\n\n"
        "### Session (14:00)\n\n"
        "Worked on dream cycle. Actually, the approach was wrong — "
        "don't use CC hooks for framework-level jobs.\n\n"
        "The **dream cycle** should use **run_with_fallback** instead.\n",
        encoding="utf-8",
    )
    logs.append(log1)

    # Log with saves
    log2 = daily_dir / f"{(today - timedelta(days=2)).strftime('%Y-%m-%d')}.md"
    log2.write_text(
        "# Daily Log\n\n"
        "## Sessions\n\n"
        "### Session (10:00)\n\n"
        "Key decision: memory consolidation runs as framework job, not hook.\n"
        "Important lesson: always test the **dream cycle** end-to-end.\n"
        "The **run_with_fallback** pattern works great.\n",
        encoding="utf-8",
    )
    logs.append(log2)

    # Log with stalls
    log3 = daily_dir / f"{(today - timedelta(days=3)).strftime('%Y-%m-%d')}.md"
    log3.write_text(
        "# Daily Log\n\n"
        "## Sessions\n\n"
        "### Session (09:00)\n\n"
        "Got stuck on the entity extraction threshold. The **dream cycle** "
        "failed when processing daily logs with high noise. "
        "The **run_with_fallback** call broke on timeout.\n",
        encoding="utf-8",
    )
    logs.append(log3)

    return logs


@pytest.fixture
def mock_daily_logs_no_signal(mock_memory_dir):
    """Create daily logs with NO signal patterns."""
    daily_dir = mock_memory_dir / "daily"
    tz = ZoneInfo("America/Chicago")
    today = datetime.now(tz).date()

    log = daily_dir / f"{(today - timedelta(days=1)).strftime('%Y-%m-%d')}.md"
    log.write_text(
        "# Daily Log\n\n## Sessions\n\n### Session (14:00)\n\n"
        "Reviewed some code. Had lunch. Read documentation.\n",
        encoding="utf-8",
    )
    return [log]


# =============================================================================
# PHASE 1: ORIENT TESTS
# =============================================================================


class TestOrient:
    def test_orient_counts_lines(self, mock_memory_dir):
        """orient() returns correct line count for MEMORY.md."""
        with patch("memory_dream.MEMORY_FILE", mock_memory_dir / "MEMORY.md"), \
             patch("memory_dream.MEMORY_DIR", mock_memory_dir), \
             patch("memory_dream.DAILY_DIR", mock_memory_dir / "daily"), \
             patch("memory_dream.SELF_FILE", mock_memory_dir / "SELF.md"), \
             patch("memory_dream.GOALS_FILE", mock_memory_dir / "GOALS.md"):
            from memory_dream import orient

            result = orient(days=7)

            assert result.memory_lines > 0
            assert result.self_exists is True
            assert result.goals_exists is True
            assert result.concepts_count == 3

    def test_orient_missing_files(self, tmp_path):
        """orient() handles missing files gracefully."""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        (empty_dir / "daily").mkdir()

        with patch("memory_dream.MEMORY_FILE", empty_dir / "MEMORY.md"), \
             patch("memory_dream.MEMORY_DIR", empty_dir), \
             patch("memory_dream.DAILY_DIR", empty_dir / "daily"), \
             patch("memory_dream.SELF_FILE", empty_dir / "SELF.md"), \
             patch("memory_dream.GOALS_FILE", empty_dir / "GOALS.md"):
            from memory_dream import orient

            result = orient(days=7)

            assert result.memory_lines == 0
            assert result.self_exists is False
            assert result.goals_exists is False
            assert result.concepts_count == 0


# =============================================================================
# PHASE 2: GATHER SIGNAL TESTS
# =============================================================================


class TestGatherSignal:
    @pytest.fixture(autouse=True)
    def isolate_state_dir(self, tmp_path):
        """Prevent real session-flush files from leaking into signal tests."""
        with patch("memory_dream.STATE_DIR", tmp_path):
            yield

    def test_gather_signal_corrections(self, mock_daily_logs):
        """Correction patterns detected in log text."""
        from memory_dream import gather_signal

        result = gather_signal(mock_daily_logs, days=3)

        assert result.found is True
        assert len(result.corrections) > 0
        # Should find "actually" and "don't"
        corrections_text = " ".join(result.corrections).lower()
        assert "actually" in corrections_text or "don't" in corrections_text or "wrong" in corrections_text

    def test_gather_signal_saves(self, mock_daily_logs):
        """Save/remember patterns detected."""
        from memory_dream import gather_signal

        result = gather_signal(mock_daily_logs, days=3)

        assert len(result.saves) > 0
        saves_text = " ".join(result.saves).lower()
        assert "key decision" in saves_text or "important" in saves_text

    def test_gather_signal_stalls(self, mock_daily_logs):
        """Stall patterns detected."""
        from memory_dream import gather_signal

        result = gather_signal(mock_daily_logs, days=3)

        assert len(result.stalls) > 0
        stalls_text = " ".join(result.stalls).lower()
        assert "stuck" in stalls_text or "failed" in stalls_text or "broke" in stalls_text

    def test_gather_signal_repeated_entities(self, mock_daily_logs):
        """Entity appearing 3x across 3 files triggers signal."""
        from memory_dream import gather_signal

        result = gather_signal(mock_daily_logs, days=3)

        # "dream cycle" and "run_with_fallback" appear in all 3 logs
        assert len(result.repeated_entities) > 0
        entity_names = [e.lower() for e in result.repeated_entities]
        assert "dream cycle" in entity_names or "run_with_fallback" in entity_names

    def test_gather_signal_silent(self, mock_daily_logs_no_signal):
        """No patterns → found=False."""
        from memory_dream import gather_signal

        result = gather_signal(mock_daily_logs_no_signal, days=1)

        assert result.found is False
        assert result.digest == ""
        assert len(result.corrections) == 0
        assert len(result.saves) == 0
        assert len(result.stalls) == 0

    def test_gather_signal_digest_under_limit(self, mock_daily_logs):
        """Digest stays under MAX_SIGNAL_CHARS."""
        from memory_dream import MAX_SIGNAL_CHARS, gather_signal

        result = gather_signal(mock_daily_logs, days=3)

        if result.found:
            assert len(result.digest) <= MAX_SIGNAL_CHARS


# =============================================================================
# RECENCY GUARD TESTS
# =============================================================================


class TestRecencyGuard:
    @pytest.mark.asyncio
    async def test_recency_guard_skips(self, tmp_path):
        """last_run < 12h ago → skip."""
        from memory_dream import DREAM_SILENT

        state_file = tmp_path / "dream-state.json"
        recent_time = datetime.now(ZoneInfo("America/Chicago")) - timedelta(hours=2)
        state_file.write_text(
            json.dumps({"last_run": recent_time.isoformat()}),
            encoding="utf-8",
        )

        with patch("memory_dream.DREAM_STATE_FILE", state_file), \
             patch("memory_dream.DREAM_MIN_INTERVAL_HOURS", 12), \
             patch("memory_dream.file_lock") as mock_lock:
            mock_lock.return_value.__enter__ = MagicMock()
            mock_lock.return_value.__exit__ = MagicMock(return_value=False)

            from memory_dream import _run_dream_inner

            result = await _run_dream_inner(test_mode=False, force=False, days=7)

            assert result is None

    @pytest.mark.asyncio
    async def test_recency_guard_force(self, tmp_path, mock_memory_dir, mock_daily_logs_no_signal):
        """--force bypasses recency guard."""
        state_file = tmp_path / "dream-state.json"
        recent_time = datetime.now(ZoneInfo("America/Chicago")) - timedelta(hours=1)
        state_file.write_text(
            json.dumps({"last_run": recent_time.isoformat()}),
            encoding="utf-8",
        )

        with patch("memory_dream.DREAM_STATE_FILE", state_file), \
             patch("memory_dream.DREAM_MIN_INTERVAL_HOURS", 12), \
             patch("memory_dream.MEMORY_FILE", mock_memory_dir / "MEMORY.md"), \
             patch("memory_dream.MEMORY_DIR", mock_memory_dir), \
             patch("memory_dream.DAILY_DIR", mock_memory_dir / "daily"), \
             patch("memory_dream.SELF_FILE", mock_memory_dir / "SELF.md"), \
             patch("memory_dream.GOALS_FILE", mock_memory_dir / "GOALS.md"), \
             patch("memory_dream.STATE_DIR", tmp_path):
            from memory_dream import DREAM_SILENT, _run_dream_inner

            # force=True should bypass guard. With no-signal logs it returns DREAM_SILENT
            result = await _run_dream_inner(test_mode=False, force=True, days=7)

            assert result == DREAM_SILENT  # Got past guard, hit silent


# =============================================================================
# DREAM SILENT SKIPS LLM TEST
# =============================================================================


class TestDreamSilent:
    @pytest.mark.asyncio
    async def test_dream_silent_skips_llm(self, tmp_path, mock_memory_dir, mock_daily_logs_no_signal):
        """When signal=False, run_dream returns DREAM_SILENT without calling run_with_fallback."""
        state_file = tmp_path / "dream-state.json"

        with patch("memory_dream.DREAM_STATE_FILE", state_file), \
             patch("memory_dream.MEMORY_FILE", mock_memory_dir / "MEMORY.md"), \
             patch("memory_dream.MEMORY_DIR", mock_memory_dir), \
             patch("memory_dream.DAILY_DIR", mock_memory_dir / "daily"), \
             patch("memory_dream.SELF_FILE", mock_memory_dir / "SELF.md"), \
             patch("memory_dream.GOALS_FILE", mock_memory_dir / "GOALS.md"), \
             patch("memory_dream.STATE_DIR", tmp_path), \
             patch("memory_dream.file_lock") as mock_lock:
            mock_lock.return_value.__enter__ = MagicMock()
            mock_lock.return_value.__exit__ = MagicMock(return_value=False)

            from memory_dream import DREAM_SILENT, run_dream

            result = await run_dream(test_mode=False, force=True, days=7)

            assert result == DREAM_SILENT


# =============================================================================
# STATE SCHEMA TEST
# =============================================================================


class TestStateSchema:
    @pytest.mark.asyncio
    async def test_state_schema(self, tmp_path, mock_memory_dir, mock_daily_logs_no_signal):
        """Saved state has all required keys after a silent run."""
        state_file = tmp_path / "dream-state.json"

        with patch("memory_dream.DREAM_STATE_FILE", state_file), \
             patch("memory_dream.MEMORY_FILE", mock_memory_dir / "MEMORY.md"), \
             patch("memory_dream.MEMORY_DIR", mock_memory_dir), \
             patch("memory_dream.DAILY_DIR", mock_memory_dir / "daily"), \
             patch("memory_dream.SELF_FILE", mock_memory_dir / "SELF.md"), \
             patch("memory_dream.GOALS_FILE", mock_memory_dir / "GOALS.md"), \
             patch("memory_dream.STATE_DIR", tmp_path):
            from memory_dream import _run_dream_inner

            await _run_dream_inner(test_mode=False, force=True, days=7)

            # Read saved state
            state = json.loads(state_file.read_text(encoding="utf-8"))

            assert "last_run" in state
            assert "days_scanned" in state
            assert "signal_found" in state
            assert "result" in state
            assert "phases_completed" in state
            assert "signal_counts" in state

            # Validate signal_counts structure
            counts = state["signal_counts"]
            assert "corrections" in counts
            assert "saves" in counts
            assert "stalls" in counts
            assert "repeated_entities" in counts


# =============================================================================
# HELPERS FOR LLM-PHASE TESTS
# =============================================================================


def _make_llm_result(text="CONSOLIDATION_OK"):
    """Create a mock LLM result object."""
    result = MagicMock()
    result.text = text
    result.provider = "mock"
    result.model = "mock-model"
    result.cost_usd = 0.001
    return result


def _patch_dream(mock_memory_dir, tmp_path, threshold=1):
    """Context manager patching all memory_dream module-level constants."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        with patch("memory_dream.DREAM_STATE_FILE", tmp_path / "dream-state.json"), \
             patch("memory_dream.MEMORY_FILE", mock_memory_dir / "MEMORY.md"), \
             patch("memory_dream.MEMORY_DIR", mock_memory_dir), \
             patch("memory_dream.DAILY_DIR", mock_memory_dir / "daily"), \
             patch("memory_dream.SELF_FILE", mock_memory_dir / "SELF.md"), \
             patch("memory_dream.GOALS_FILE", mock_memory_dir / "GOALS.md"), \
             patch("memory_dream.STATE_DIR", tmp_path), \
             patch("memory_dream.DREAM_SIGNAL_THRESHOLD", threshold):
            yield

    return _ctx()


# =============================================================================
# PHASE 3-4 TESTS (mocked run_with_fallback)
# =============================================================================


class TestFullDream:
    @pytest.mark.asyncio
    async def test_full_dream_happy_path(self, tmp_path, mock_memory_dir, mock_daily_logs):
        """All 4 phases run, state has result='consolidated', run_with_fallback called 2x."""
        mock_rwf = AsyncMock(side_effect=[
            _make_llm_result("Merged signal into MEMORY.md"),
            _make_llm_result("PRUNE_OK"),
        ])

        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("runtime.registry.run_with_fallback", mock_rwf), \
             patch("memory_dream._run_entity_compilation"), \
             patch("memory_dream._run_reindex"):
            from memory_dream import _run_dream_inner

            result = await _run_dream_inner(test_mode=False, force=True, days=7)

            assert result is not None
            assert result != "DREAM_SILENT"

            # Verify state
            state = json.loads((tmp_path / "dream-state.json").read_text(encoding="utf-8"))
            assert state["result"] == "consolidated"
            assert "consolidate" in state["phases_completed"]
            assert "prune" in state["phases_completed"]
            assert mock_rwf.call_count == 2

    @pytest.mark.asyncio
    async def test_consolidation_failure_allows_retry(self, tmp_path, mock_memory_dir, mock_daily_logs):
        """Phase 3 raises, state has result='failed', recency guard allows retry."""
        mock_rwf = AsyncMock(side_effect=RuntimeError("LLM quota exceeded"))

        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("runtime.registry.run_with_fallback", mock_rwf):
            from memory_dream import _run_dream_inner

            with pytest.raises(RuntimeError, match="LLM quota exceeded"):
                await _run_dream_inner(test_mode=False, force=True, days=7)

            # State should say "failed"
            state = json.loads((tmp_path / "dream-state.json").read_text(encoding="utf-8"))
            assert state["result"] == "failed"
            assert "error" in state

            # Recency guard should allow retry (result == "failed")
            mock_rwf.side_effect = RuntimeError("Still down")
            with pytest.raises(RuntimeError):
                # force=False — should still run because last result was "failed"
                await _run_dream_inner(test_mode=False, force=False, days=7)

    @pytest.mark.asyncio
    async def test_phase4_failure_after_phase3(self, tmp_path, mock_memory_dir, mock_daily_logs):
        """Phase 3 succeeds, Phase 4 raises, phases_completed reflects partial."""
        mock_rwf = AsyncMock(side_effect=[
            _make_llm_result("Merged signal"),  # Phase 3 succeeds
            RuntimeError("prune failed"),         # Phase 4 fails
        ])

        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("runtime.registry.run_with_fallback", mock_rwf):
            from memory_dream import _run_dream_inner

            with pytest.raises(RuntimeError, match="prune failed"):
                await _run_dream_inner(test_mode=False, force=True, days=7)

            state = json.loads((tmp_path / "dream-state.json").read_text(encoding="utf-8"))
            assert state["result"] == "failed"
            assert "consolidate" in state["phases_completed"]
            assert "prune" not in state["phases_completed"]


class TestPostWeeklyFlag:
    @pytest.mark.asyncio
    async def test_weekly_post_step_flag(self, tmp_path, mock_memory_dir, mock_daily_logs):
        """post_weekly=True adds warning string to consolidation prompt."""
        mock_rwf = AsyncMock(side_effect=[
            _make_llm_result("CONSOLIDATION_OK"),
            _make_llm_result("PRUNE_OK"),
        ])

        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("runtime.registry.run_with_fallback", mock_rwf), \
             patch("memory_dream._run_entity_compilation"), \
             patch("memory_dream._run_reindex"):
            from memory_dream import _run_dream_inner

            await _run_dream_inner(test_mode=False, force=True, days=7, post_weekly=True)

            # First call is consolidate — check prompt contains weekly warning
            first_call = mock_rwf.call_args_list[0]
            request_obj = first_call[0][0]  # RuntimeRequest positional arg
            assert "Weekly synthesis JUST ran" in request_obj.prompt


class TestSignalThreshold:
    def test_single_stall_below_threshold(self, tmp_path):
        """A single 'error' mention (1 point) does NOT trigger found=True."""
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        tz = ZoneInfo("America/Chicago")
        today = datetime.now(tz).date()
        log = daily_dir / f"{(today - timedelta(days=1)).strftime('%Y-%m-%d')}.md"
        log.write_text("# Log\n\nFixed an error in the router.\n", encoding="utf-8")

        with patch("memory_dream.STATE_DIR", tmp_path), \
             patch("memory_dream.DREAM_SIGNAL_THRESHOLD", 4):
            from memory_dream import gather_signal

            result = gather_signal([log], days=1)

            # 1 stall * 1pt = 1 < 4 threshold
            assert result.found is False
            assert result.signal_score < 4

    def test_multiple_signals_above_threshold(self, tmp_path):
        """Multiple distinct signals cross the threshold."""
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        tz = ZoneInfo("America/Chicago")
        today = datetime.now(tz).date()

        # Create a log with enough SEPARATED signals to cross threshold
        # Need signals far enough apart to produce distinct snippet matches
        padding = "x" * 200  # Enough to separate context windows
        log = daily_dir / f"{(today - timedelta(days=1)).strftime('%Y-%m-%d')}.md"
        log.write_text(
            f"# Log\n\n"
            f"The approach was wrong, we need to rethink this.\n"
            f"{padding}\n"
            f"Actually, the hooks should be framework-level.\n"
            f"{padding}\n"
            f"Got stuck on provider abstraction for hours.\n"
            f"{padding}\n"
            f"Key decision: use run_with_fallback for everything.\n",
            encoding="utf-8",
        )

        with patch("memory_dream.STATE_DIR", tmp_path), \
             patch("memory_dream.DREAM_SIGNAL_THRESHOLD", 4):
            from memory_dream import gather_signal

            result = gather_signal([log], days=1)

            # "wrong" = 1 correction (2pts)
            # "actually" = 1 correction (2pts)
            # "stuck" = 1 stall (1pt)
            # "key decision" = 1 save (2pts)
            # Total should be well above 4
            assert result.found is True
            assert result.signal_score >= 4
