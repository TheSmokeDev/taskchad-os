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
        """Prevent real session-flush files from leaking into signal tests.

        Living Mind Act 3 extended gather_signal with an episodes/ scan that
        default-resolves the module MEMORY_DIR, so the live vault must be
        isolated here too (pytest never reads live files).
        """
        with patch("memory_dream.STATE_DIR", tmp_path), \
             patch("memory_dream.MEMORY_DIR", tmp_path):
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
             patch("memory_dream.AMENDMENT_LEDGER_FILE", tmp_path / "amendment-proposals.jsonl"), \
             patch("memory_dream.DREAM_SIGNAL_THRESHOLD", threshold):
            yield

    return _ctx()


# =============================================================================
# PHASE 3-4 TESTS (mocked run_with_runtime_lanes — lane-first runtime)
# =============================================================================


class TestFullDream:
    @pytest.mark.asyncio
    async def test_full_dream_happy_path(self, tmp_path, mock_memory_dir, mock_daily_logs):
        """All 4 phases run, state has result='consolidated', lane runtime called 2x."""
        mock_rwf = AsyncMock(side_effect=[
            _make_llm_result("Merged signal into MEMORY.md"),
            _make_llm_result("PRUNE_OK"),
        ])

        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("runtime.lane_router.run_with_runtime_lanes", mock_rwf), \
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
             patch("runtime.lane_router.run_with_runtime_lanes", mock_rwf):
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
             patch("runtime.lane_router.run_with_runtime_lanes", mock_rwf):
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
             patch("runtime.lane_router.run_with_runtime_lanes", mock_rwf), \
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


# =============================================================================
# PRD-8 PHASE 2 WS3 — IDENTITY-PAYLOAD SHIM PARITY TESTS
# =============================================================================
#
# Two tests prove that the consolidate (Phase 3) and prune (Phase 4) phases
# preserve identity-section assembly behavior after refactoring inline file
# reads to use ``cognition.identity_payload.build_identity_payload``.
#
# Per PRP §Workstream 3 Task6:
#   - tests/test_memory_dream.py::test_consolidate_prompt_parity_with_shim
#   - tests/test_memory_dream.py::test_prune_prompt_parity_with_shim
#
# Pattern matches the canonical ``mock_memory_dir`` fixture above. Each test
# captures the pre-refactor inline reads as a private helper, runs both paths
# against the same ``tmp_path / "TheHomie" / "Memory"`` fixture, asserts byte
# equality of the assembled identity section.


def _legacy_consolidate_identity_section(
    memory_file: Path, self_file: Path, goals_file: Path, memory_lines: int
) -> str:
    """Pre-refactor consolidate-phase identity-section assembly.

    Mirrors memory_dream.py:311-313 + the prompt body at :339-349 verbatim.
    Order: MEMORY/SELF/GOALS, with the ``memory_lines`` annotation in the
    MEMORY header and the read-only annotation on the GOALS header.
    """
    memory_content = memory_file.read_text(encoding="utf-8") if memory_file.exists() else ""
    self_content = self_file.read_text(encoding="utf-8") if self_file.exists() else ""
    goals_content = goals_file.read_text(encoding="utf-8") if goals_file.exists() else ""

    return f"""## Current MEMORY.md ({memory_lines} lines)

{memory_content}

## Current SELF.md

{self_content}

## Current GOALS.md (read-only — reference only, do NOT edit)

{goals_content}"""


# F2 post-build fix: production helpers ARE the test target.
from memory_dream import (
    _assemble_consolidate_identity_section as _new_consolidate_identity_section,
    _assemble_prune_memory_section as _new_prune_identity_section,
)


def _legacy_prune_identity_section(memory_file: Path) -> str:
    """Pre-refactor prune-phase identity-section assembly.

    Mirrors memory_dream.py:418-431 verbatim. Prune reads MEMORY only.
    Returns (assembled_section, memory_lines) so callers can assert the
    derived line count too — the line count drives the truncation rule
    in production.
    """
    memory_content = memory_file.read_text(encoding="utf-8") if memory_file.exists() else ""
    memory_lines = len(memory_content.splitlines())

    return f"""## Current MEMORY.md ({memory_lines} lines)

{memory_content}"""


def test_consolidate_prompt_parity_with_shim(mock_memory_dir):
    """Consolidate-phase identity section is byte-identical pre/post refactor.

    WS3 acceptance criterion ``memory_dream_refactor_parity_preserved``
    (consolidate half).
    """
    memory_file = mock_memory_dir / "MEMORY.md"
    self_file = mock_memory_dir / "SELF.md"
    goals_file = mock_memory_dir / "GOALS.md"
    # Production derives memory_lines from the OrientResult; we replicate by
    # counting from the file directly so both legacy + shim see the same value.
    memory_lines = (
        len(memory_file.read_text(encoding="utf-8").splitlines())
        if memory_file.exists()
        else 0
    )

    legacy = _legacy_consolidate_identity_section(
        memory_file, self_file, goals_file, memory_lines
    )
    new = _new_consolidate_identity_section(mock_memory_dir, memory_lines)

    assert legacy == new, (
        "Consolidate identity-section parity broken between legacy reads + shim. "
        f"Diff first 200 chars:\n  legacy[:200]={legacy[:200]!r}\n  new[:200]={new[:200]!r}"
    )


def test_consolidate_prompt_parity_with_shim_missing_files(tmp_path):
    """Missing identity files in consolidate phase preserves parity (fail-open)."""
    empty_dir = tmp_path / "TheHomie" / "Memory"
    empty_dir.mkdir(parents=True)

    memory_file = empty_dir / "MEMORY.md"
    self_file = empty_dir / "SELF.md"
    goals_file = empty_dir / "GOALS.md"

    legacy = _legacy_consolidate_identity_section(
        memory_file, self_file, goals_file, memory_lines=0
    )
    new = _new_consolidate_identity_section(empty_dir, memory_lines=0)

    assert legacy == new
    assert "## Current MEMORY.md (0 lines)" in new


def test_prune_prompt_parity_with_shim(mock_memory_dir):
    """Prune-phase identity section is byte-identical pre/post refactor.

    WS3 acceptance criterion ``memory_dream_refactor_parity_preserved``
    (prune half).
    """
    memory_file = mock_memory_dir / "MEMORY.md"

    legacy = _legacy_prune_identity_section(memory_file)
    new = _new_prune_identity_section(mock_memory_dir)

    assert legacy == new, (
        "Prune identity-section parity broken between legacy reads + shim. "
        f"Diff first 200 chars:\n  legacy[:200]={legacy[:200]!r}\n  new[:200]={new[:200]!r}"
    )


def test_prune_prompt_parity_with_shim_missing_memory(tmp_path):
    """Missing MEMORY.md in prune phase preserves parity (fail-open)."""
    empty_dir = tmp_path / "TheHomie" / "Memory"
    empty_dir.mkdir(parents=True)

    memory_file = empty_dir / "MEMORY.md"

    legacy = _legacy_prune_identity_section(memory_file)
    new = _new_prune_identity_section(empty_dir)

    assert legacy == new
    assert "## Current MEMORY.md (0 lines)" in new


# =============================================================================
# LIVING MIND ACT 3 — EPISODE GATHER (PRP test category 8)
# =============================================================================
#
# Existing classes above are UNTOUCHED (TestStateSchema proves the
# dream-state schema carries no episode registry — Rule 2). Fixture ids are
# the synthetic telegram-1111111111-2222222222 family (R2 NM2).


def _write_dream_episode(
    memory_dir: Path,
    name: str,
    *,
    status: str = "open",
    date: str | None = None,
    body: str = (
        "## Key Decisions\n\n"
        "- lesson learned: episodes feed the dream cycle directly\n"
        "- lesson learned: the gather scan stays pure python\n"
        "- key decision: substantive episodes break dream silence\n"
    ),
) -> Path:
    """Drop a fixture episode into {memory_dir}/episodes/."""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    episodes_dir = memory_dir / "episodes"
    episodes_dir.mkdir(parents=True, exist_ok=True)
    path = episodes_dir / name
    path.write_text(
        "---\n"
        "tags: [system, memory, living-mind]\n"
        f"status: {status}\n"
        f"date: {date}\n"
        'session_id: "telegram-1111111111-2222222222"\n'
        "surface: telegram\n"
        'lifecycle: "20260612-100000"\n'
        'summary: "fixture"\n'
        "---\n\n"
        "# Episode: fixture\n\n"
        f"{body}",
        encoding="utf-8",
    )
    return path


class TestEpisodeGather:
    @pytest.fixture(autouse=True)
    def isolate_state_dir(self, tmp_path):
        """Prevent real session-flush leftovers from leaking into counts."""
        state_dir = tmp_path / "gather-state"
        state_dir.mkdir()
        with patch("memory_dream.STATE_DIR", state_dir):
            yield state_dir

    def test_substantive_episode_breaks_dream_silence(
        self, mock_memory_dir, mock_daily_logs_no_signal
    ):
        """The silence-breaker discriminator: same vault minus the episode is
        DREAM_SILENT; with it the weighted score crosses the threshold."""
        from memory_dream import gather_signal

        with patch("memory_dream.DREAM_SIGNAL_THRESHOLD", 4):
            baseline = gather_signal(
                mock_daily_logs_no_signal, days=7, memory_dir=mock_memory_dir
            )
            assert baseline.found is False
            assert baseline.episode_paths == []

            episode = _write_dream_episode(
                mock_memory_dir, "2026-06-12-telegram-aaaa1111-100000.md"
            )
            result = gather_signal(
                mock_daily_logs_no_signal, days=7, memory_dir=mock_memory_dir
            )

        assert result.found is True
        assert result.signal_score >= 4
        assert result.episode_paths == [episode]
        assert result.files_scanned == baseline.files_scanned + 1

    def test_consolidated_episode_excluded(
        self, mock_memory_dir, mock_daily_logs_no_signal
    ):
        from memory_dream import gather_signal

        _write_dream_episode(
            mock_memory_dir,
            "2026-06-12-telegram-aaaa1111-100000.md",
            status="consolidated",
        )
        result = gather_signal(
            mock_daily_logs_no_signal, days=7, memory_dir=mock_memory_dir
        )
        assert result.episode_paths == []

    def test_out_of_window_episode_excluded(
        self, mock_memory_dir, mock_daily_logs_no_signal
    ):
        from datetime import timedelta as _td

        from memory_dream import gather_signal

        stale = (datetime.now() - _td(days=30)).strftime("%Y-%m-%d")
        _write_dream_episode(
            mock_memory_dir,
            f"{stale}-telegram-aaaa1111-100000.md",
            date=stale,
        )
        result = gather_signal(
            mock_daily_logs_no_signal, days=7, memory_dir=mock_memory_dir
        )
        assert result.episode_paths == []

    def test_missing_episodes_dir_is_noop(
        self, mock_memory_dir, mock_daily_logs_no_signal
    ):
        """Migration-free: vaults without episodes/ behave exactly as before."""
        from memory_dream import gather_signal

        assert not (mock_memory_dir / "episodes").exists()
        result = gather_signal(
            mock_daily_logs_no_signal, days=7, memory_dir=mock_memory_dir
        )
        assert result.episode_paths == []
        assert result.found is False

    def test_episode_paths_newest_first(
        self, mock_memory_dir, mock_daily_logs_no_signal
    ):
        from datetime import timedelta as _td

        from memory_dream import gather_signal

        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - _td(days=1)).strftime("%Y-%m-%d")
        older = _write_dream_episode(
            mock_memory_dir, f"{yesterday}-telegram-aaaa1111-090000.md", date=yesterday
        )
        newer = _write_dream_episode(
            mock_memory_dir, f"{today}-telegram-aaaa1111-100000.md", date=today
        )
        result = gather_signal(
            mock_daily_logs_no_signal, days=7, memory_dir=mock_memory_dir
        )
        assert result.episode_paths == [newer, older]

    def test_existing_call_shape_resolves_module_memory_dir(
        self, mock_memory_dir, mock_daily_logs_no_signal
    ):
        """gather_signal(logs, days=d) — the pre-Act-3 call shape — resolves
        memory_dir from the module attribute at call time (Rule 1)."""
        episode = _write_dream_episode(
            mock_memory_dir, "2026-06-12-telegram-aaaa1111-100000.md"
        )
        with patch("memory_dream.MEMORY_DIR", mock_memory_dir):
            from memory_dream import gather_signal

            result = gather_signal(mock_daily_logs_no_signal, days=7)
        assert result.episode_paths == [episode]

    def test_legacy_state_dir_flush_scan_untouched(
        self, mock_memory_dir, mock_daily_logs_no_signal, isolate_state_dir
    ):
        """A raw failed-flush leftover in STATE_DIR is still counted."""
        from memory_dream import gather_signal

        leftover = isolate_state_dir / (
            "session-flush-telegram-1111111111-2222222222-20260612-090000.md"
        )
        leftover.write_text(
            "**User:** lesson learned: raw leftovers still feed the dream\n",
            encoding="utf-8",
        )
        result = gather_signal(
            mock_daily_logs_no_signal, days=7, memory_dir=mock_memory_dir
        )
        assert result.files_scanned >= 2  # daily log + leftover
        assert any("raw leftovers" in s for s in result.saves)
        assert result.episode_paths == []  # leftovers are NOT episodes

    def test_bland_episode_alone_stays_silent(
        self, mock_memory_dir, mock_daily_logs_no_signal
    ):
        """Intentional (post-build review 🟡): an open episode whose body has
        no signal terms is scanned and carried, but does NOT fire the dream
        by itself — it stays open and re-feeds a later run that has real
        signal (dream firing depends on the regex signal inside bodies)."""
        from memory_dream import gather_signal

        episode = _write_dream_episode(
            mock_memory_dir,
            "2026-06-12-telegram-aaaa1111-100000.md",
            body="## Summary\n\nQuiet session. Routine upkeep only.\n",
        )
        with patch("memory_dream.DREAM_SIGNAL_THRESHOLD", 4):
            result = gather_signal(
                mock_daily_logs_no_signal, days=7, memory_dir=mock_memory_dir
            )
        assert result.found is False  # bland episode does not fire the dream
        assert result.episode_paths == [episode]  # but it IS scanned/carried


# =============================================================================
# LIVING MIND ACT 3 — CONSOLIDATE SECTION + DETERMINISTIC FLIP (category 9)
# =============================================================================


class TestEpisodeConsolidateFlip:
    def test_assemble_episodes_section_empty_is_empty_string(self):
        """Empty paths -> "" so the consolidate prompt stays byte-identical
        to pre-Act-3 assembly for the no-episodes case."""
        from memory_dream import _assemble_episodes_section

        assert _assemble_episodes_section([]) == ""

    def test_assemble_episodes_section_renders_digest(self, mock_memory_dir):
        from memory_dream import _assemble_episodes_section

        episode = _write_dream_episode(
            mock_memory_dir, "2026-06-12-telegram-aaaa1111-100000.md"
        )
        section = _assemble_episodes_section([episode])
        assert section.startswith("## Recent Episodes (open)")
        assert episode.stem in section
        assert "lesson learned: episodes feed the dream" in section

    @pytest.mark.asyncio
    async def test_full_dream_flips_episodes_and_reports(
        self, tmp_path, mock_memory_dir, mock_daily_logs
    ):
        from episodes import read_episode_frontmatter
        from memory_dream import _run_dream_inner

        episode = _write_dream_episode(
            mock_memory_dir, "2026-06-12-telegram-aaaa1111-100000.md"
        )
        daily_entries: list[tuple[str, str]] = []
        mock_rwf = AsyncMock(side_effect=[
            _make_llm_result("Merged signal into MEMORY.md"),
            _make_llm_result("PRUNE_OK"),
        ])

        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("runtime.lane_router.run_with_runtime_lanes", mock_rwf), \
             patch("memory_dream._run_entity_compilation"), \
             patch("memory_dream._run_reindex"), \
             patch(
                 "memory_dream.append_to_daily_log",
                 lambda text, section: daily_entries.append((text, section)),
             ):
            result = await _run_dream_inner(test_mode=False, force=True, days=7)

        assert result is not None and result != "DREAM_SILENT"
        # The consolidate prompt carried the digest + mining instruction.
        prompt = mock_rwf.call_args_list[0][0][0].prompt
        assert "## Recent Episodes (open)" in prompt
        assert "Mine the open episodes" in prompt
        # Deterministic flip happened after the successful Phase 3.
        fm = read_episode_frontmatter(episode)
        assert fm["status"] == "consolidated"
        assert "consolidated_at" in fm
        # State + summary report the counts.
        state = json.loads((tmp_path / "dream-state.json").read_text(encoding="utf-8"))
        assert state["result"] == "consolidated"
        summary_text = "\n".join(t for t, _s in daily_entries)
        assert "episodes: 1 reviewed, 1 consolidated" in summary_text

    @pytest.mark.asyncio
    async def test_no_episodes_prompt_carries_no_episode_section(
        self, tmp_path, mock_memory_dir, mock_daily_logs
    ):
        from memory_dream import _run_dream_inner

        mock_rwf = AsyncMock(side_effect=[
            _make_llm_result("CONSOLIDATION_OK"),
            _make_llm_result("PRUNE_OK"),
        ])
        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("runtime.lane_router.run_with_runtime_lanes", mock_rwf), \
             patch("memory_dream._run_entity_compilation"), \
             patch("memory_dream._run_reindex"), \
             patch("memory_dream.append_to_daily_log", lambda *_a, **_k: None):
            await _run_dream_inner(test_mode=False, force=True, days=7)

        prompt = mock_rwf.call_args_list[0][0][0].prompt
        assert "Recent Episodes" not in prompt
        assert "Mine the open episodes" not in prompt

    @pytest.mark.asyncio
    async def test_consolidate_failure_leaves_episodes_open(
        self, tmp_path, mock_memory_dir, mock_daily_logs
    ):
        """Crash-safe retry: a consolidate() raise never reaches the flip."""
        from episodes import read_episode_frontmatter
        from memory_dream import _run_dream_inner

        episode = _write_dream_episode(
            mock_memory_dir, "2026-06-12-telegram-aaaa1111-100000.md"
        )
        mock_rwf = AsyncMock(side_effect=RuntimeError("LLM down"))

        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("runtime.lane_router.run_with_runtime_lanes", mock_rwf), \
             patch("memory_dream.append_to_daily_log", lambda *_a, **_k: None):
            with pytest.raises(RuntimeError, match="LLM down"):
                await _run_dream_inner(test_mode=False, force=True, days=7)

        assert read_episode_frontmatter(episode)["status"] == "open"
        state = json.loads((tmp_path / "dream-state.json").read_text(encoding="utf-8"))
        assert state["result"] == "failed"

    @pytest.mark.asyncio
    async def test_test_mode_scans_and_sections_but_never_flips(
        self, tmp_path, mock_memory_dir, mock_daily_logs, capsys
    ):
        from episodes import read_episode_frontmatter
        from memory_dream import _run_dream_inner

        episode = _write_dream_episode(
            mock_memory_dir, "2026-06-12-telegram-aaaa1111-100000.md"
        )
        mock_rwf = AsyncMock(side_effect=[
            _make_llm_result("CONSOLIDATION_OK"),
            _make_llm_result("PRUNE_OK"),
        ])
        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("runtime.lane_router.run_with_runtime_lanes", mock_rwf), \
             patch("memory_dream.append_to_daily_log", lambda *_a, **_k: None):
            await _run_dream_inner(test_mode=True, force=True, days=7)

        prompt = mock_rwf.call_args_list[0][0][0].prompt
        assert "## Recent Episodes (open)" in prompt  # dry-run visibility
        assert read_episode_frontmatter(episode)["status"] == "open"  # no flip
        assert "DRY RUN - would mark 1 episode(s) consolidated" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_flip_failure_dream_still_reports_success(
        self, tmp_path, mock_memory_dir, mock_daily_logs, capsys
    ):
        """R1 M1: flip raising -> warning logged, episodes_marked = 0,
        MEMORY re-read + Phase 4 + summary all run, result = consolidated."""
        import episodes as episodes_mod
        from episodes import read_episode_frontmatter
        from memory_dream import _run_dream_inner

        episode = _write_dream_episode(
            mock_memory_dir, "2026-06-12-telegram-aaaa1111-100000.md"
        )
        daily_entries: list[tuple[str, str]] = []
        mock_rwf = AsyncMock(side_effect=[
            _make_llm_result("Merged signal"),
            _make_llm_result("PRUNE_OK"),
        ])

        def exploding_flip(*_args, **_kwargs):
            raise RuntimeError("flip exploded")

        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("runtime.lane_router.run_with_runtime_lanes", mock_rwf), \
             patch("memory_dream._run_entity_compilation"), \
             patch("memory_dream._run_reindex"), \
             patch.object(episodes_mod, "mark_episodes_consolidated", exploding_flip), \
             patch(
                 "memory_dream.append_to_daily_log",
                 lambda text, section: daily_entries.append((text, section)),
             ):
            result = await _run_dream_inner(test_mode=False, force=True, days=7)

        assert result is not None  # dream did NOT raise
        out = capsys.readouterr().out
        assert "WARNING: episode flip failed (non-fatal): flip exploded" in out
        state = json.loads((tmp_path / "dream-state.json").read_text(encoding="utf-8"))
        assert state["result"] == "consolidated"  # dream reports success
        assert "prune" in state["phases_completed"]  # Phase 4 still ran
        assert read_episode_frontmatter(episode)["status"] == "open"
        summary_text = "\n".join(t for t, _s in daily_entries)
        assert "episodes: 1 reviewed, 0 consolidated" in summary_text

    @pytest.mark.asyncio
    async def test_flip_io_failure_real_primitive_warns_and_keeps_episode_open(
        self, tmp_path, mock_memory_dir, mock_daily_logs, capsys
    ):
        """F1 discriminator (post-build review): the REAL flip primitive runs
        and its internal lock fails — the failure must surface through
        EpisodeFlipError into the dream warning path (never a silent 0), and
        the episode must stay open (re-feedable on the next run)."""
        import episodes as episodes_mod
        from episodes import read_episode_frontmatter
        from memory_dream import _run_dream_inner

        episode = _write_dream_episode(
            mock_memory_dir, "2026-06-12-telegram-aaaa1111-100000.md"
        )
        daily_entries: list[tuple[str, str]] = []
        mock_rwf = AsyncMock(side_effect=[
            _make_llm_result("Merged signal"),
            _make_llm_result("PRUNE_OK"),
        ])

        def timing_out_lock(_path, timeout=None):
            raise TimeoutError("flip lock timeout (simulated)")

        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("runtime.lane_router.run_with_runtime_lanes", mock_rwf), \
             patch("memory_dream._run_entity_compilation"), \
             patch("memory_dream._run_reindex"), \
             patch.object(episodes_mod, "file_lock", timing_out_lock), \
             patch(
                 "memory_dream.append_to_daily_log",
                 lambda text, section: daily_entries.append((text, section)),
             ):
            result = await _run_dream_inner(test_mode=False, force=True, days=7)

        assert result is not None  # dream did NOT raise
        out = capsys.readouterr().out
        assert "WARNING: episode flip failed (non-fatal):" in out
        assert episode.name in out  # failure receipt names the file
        assert "flip lock timeout (simulated)" in out
        state = json.loads((tmp_path / "dream-state.json").read_text(encoding="utf-8"))
        assert state["result"] == "consolidated"  # dream reports success
        assert "prune" in state["phases_completed"]  # Phase 4 still ran
        assert read_episode_frontmatter(episode)["status"] == "open"
        summary_text = "\n".join(t for t, _s in daily_entries)
        assert "episodes: 1 reviewed, 0 consolidated" in summary_text

    @pytest.mark.asyncio
    async def test_flip_partial_failure_reports_partial_count(
        self, tmp_path, mock_memory_dir, mock_daily_logs, capsys
    ):
        """Collect-then-raise through the wrapper: one episode flips, one
        fails — the warning fires AND the summary reports the partial truth
        (Rule 2: the flipped file physically says consolidated)."""
        import episodes as episodes_mod
        from episodes import read_episode_frontmatter
        from memory_dream import _run_dream_inner

        # bbbb sorts first (newest-first), so the FAILURE happens before the
        # success — proving the flip loop continues past a failed file.
        bad = _write_dream_episode(
            mock_memory_dir, "2026-06-12-telegram-bbbb2222-110000.md"
        )
        good = _write_dream_episode(
            mock_memory_dir, "2026-06-12-telegram-aaaa1111-100000.md"
        )
        daily_entries: list[tuple[str, str]] = []
        mock_rwf = AsyncMock(side_effect=[
            _make_llm_result("Merged signal"),
            _make_llm_result("PRUNE_OK"),
        ])
        real_write = episodes_mod._atomic_write

        def selective_write(path, content):
            if path == bad:
                raise PermissionError("bbbb write blocked (simulated)")
            return real_write(path, content)

        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("runtime.lane_router.run_with_runtime_lanes", mock_rwf), \
             patch("memory_dream._run_entity_compilation"), \
             patch("memory_dream._run_reindex"), \
             patch.object(episodes_mod, "_atomic_write", selective_write), \
             patch(
                 "memory_dream.append_to_daily_log",
                 lambda text, section: daily_entries.append((text, section)),
             ):
            result = await _run_dream_inner(test_mode=False, force=True, days=7)

        assert result is not None  # dream did NOT raise
        out = capsys.readouterr().out
        assert "WARNING: episode flip failed (non-fatal):" in out
        assert bad.name in out
        # Physical truth: the good episode really flipped; the bad stays open.
        assert read_episode_frontmatter(good)["status"] == "consolidated"
        assert read_episode_frontmatter(bad)["status"] == "open"
        state = json.loads((tmp_path / "dream-state.json").read_text(encoding="utf-8"))
        assert state["result"] == "consolidated"
        summary_text = "\n".join(t for t, _s in daily_entries)
        assert "episodes: 2 reviewed, 1 consolidated" in summary_text

    @pytest.mark.asyncio
    async def test_vault_log_bullets_carry_episode_counts(
        self, tmp_path, mock_memory_dir, mock_daily_logs
    ):
        import entity_extractor
        from memory_dream import _run_dream_inner

        _write_dream_episode(
            mock_memory_dir, "2026-06-12-telegram-aaaa1111-100000.md"
        )
        captured: dict = {}

        def capture_vault_log(_vault, _event, _title, bullets=None):
            captured["bullets"] = list(bullets or [])

        mock_rwf = AsyncMock(side_effect=[
            _make_llm_result("Merged signal"),
            _make_llm_result("PRUNE_OK"),
        ])
        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("runtime.lane_router.run_with_runtime_lanes", mock_rwf), \
             patch("memory_dream._run_entity_compilation"), \
             patch("memory_dream._run_reindex"), \
             patch.object(entity_extractor, "append_vault_log", capture_vault_log), \
             patch("memory_dream.append_to_daily_log", lambda *_a, **_k: None):
            await _run_dream_inner(test_mode=False, force=True, days=7)

        assert any(
            "episodes: 1 reviewed, 1 consolidated" in bullet
            for bullet in captured.get("bullets", [])
        )
