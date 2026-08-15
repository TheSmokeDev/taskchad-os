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


class TestDryRunNeverMutatesTheVault:
    @pytest.mark.asyncio
    async def test_test_mode_skips_working_memory_and_skill_archival(
        self, tmp_path, mock_memory_dir, mock_daily_logs_no_signal
    ):
        """``--test`` must mean NO file edits — this file's own usage doc says
        so, and the persona ``--child-test`` contract promises "no vault
        edits". Phase 2.5 (working-memory archive) and Phase 2.5b (skill-draft
        archive) run before the signal-found branch and are NOT gated by
        test_mode like every other side-effecting phase in this file — a real
        bug that breaks that contract for the main homie AND every persona."""
        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("living_memory.archive_stale_working_items") as mock_archive_wm, \
             patch("cognition.skill_promotion.archive_stale") as mock_archive_skills:
            from memory_dream import _run_dream_inner

            result = await _run_dream_inner(test_mode=True, force=True, days=7)

        assert result == "DREAM_SILENT"
        mock_archive_wm.assert_not_called()
        mock_archive_skills.assert_not_called()

    @pytest.mark.asyncio
    async def test_silent_test_run_writes_no_state_and_no_daily_log(
        self, tmp_path, mock_memory_dir, mock_daily_logs_no_signal
    ):
        """codex R3 MAJOR 2: the DREAM_SILENT path advanced state and appended a
        daily-log line even under ``--test``.

        That is what made ``persona_dream_tick --child-test`` unsafe: the state
        write sets ``last_run``, so the child's own 20-hour recency guard then
        skips that night's REAL dream — a probe silently costing a persona its
        dream cycle."""
        state_file = tmp_path / "dream-state.json"
        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("memory_dream.append_to_daily_log") as daily:
            from memory_dream import _run_dream_inner

            result = await _run_dream_inner(test_mode=True, force=True, days=7)

        assert result == "DREAM_SILENT"
        assert not state_file.exists(), state_file.read_text(encoding="utf-8")
        daily.assert_not_called()

    @pytest.mark.asyncio
    async def test_test_run_with_signal_writes_no_state_and_no_daily_log(
        self, tmp_path, mock_memory_dir, mock_daily_logs
    ):
        """Same contract on the signal-bearing path, which advanced state twice
        (crash-safe pre-LLM write + the final result write) and logged a
        summary."""
        state_file = tmp_path / "dream-state.json"
        mock_rwf = AsyncMock(side_effect=[
            _make_llm_result("Merged signal"),
            _make_llm_result("PRUNE_OK"),
        ])
        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("runtime.lane_router.run_with_runtime_lanes", mock_rwf), \
             patch("memory_dream.append_to_daily_log") as daily:
            from memory_dream import _run_dream_inner

            await _run_dream_inner(test_mode=True, force=True, days=7)

        assert not state_file.exists(), state_file.read_text(encoding="utf-8")
        daily.assert_not_called()

    @pytest.mark.asyncio
    async def test_test_run_takes_no_lock_sidecar(
        self, tmp_path, mock_memory_dir, mock_daily_logs_no_signal
    ):
        """The lock is a physical write too — a .lock file plus its parent
        dirs, landing in whichever profile tree ``-p`` selected."""
        state_file = tmp_path / "locks" / "dream-state.json"
        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("memory_dream.DREAM_STATE_FILE", state_file), \
             patch("memory_dream.append_to_daily_log"):
            from memory_dream import run_dream

            result = await run_dream(test_mode=True, force=True, days=7)

        assert result == "DREAM_SILENT"
        lock_file = state_file.with_suffix(state_file.suffix + ".lock")
        assert not lock_file.exists()
        assert not state_file.parent.exists()

    @pytest.mark.asyncio
    async def test_test_run_proposes_no_belief_and_writes_no_artifact(
        self, tmp_path, mock_memory_dir, mock_daily_logs
    ):
        """Phase 5 reports what it WOULD propose and stops: propose_belief
        writes a decision artifact on every path, dry run included (that
        preview belongs to the ``evolve propose-belief --dry-run`` CLI, which
        is not bound by this file's no-file-edits contract)."""
        import evolve.evolve_loop as el

        candidate = json.dumps({
            "kind": "belief_candidate",
            "target_file": "SELF.md",
            "summary": "a candidate the dry run must not act on",
            "evidence_paths": ["daily/x.md"],
            "proposed_content": "- something the dream noticed.",
            "confidence_score": 0.9,
        })
        mock_rwf = AsyncMock(side_effect=[
            _make_llm_result("Merged signal.\n" + candidate),
            _make_llm_result("PRUNE_OK"),
        ])
        decision_dir = tmp_path / "decisions"
        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("runtime.lane_router.run_with_runtime_lanes", mock_rwf), \
             patch("config.BELIEF_EVOLVE_DECISION_DIR", decision_dir), \
             patch.object(el, "propose_belief", AsyncMock()) as propose, \
             patch("memory_dream.append_to_daily_log"):
            from memory_dream import _run_belief_evolution_phase

            receipt = await _run_belief_evolution_phase(
                "Merged signal.\n" + candidate, test_mode=True
            )

        propose.assert_not_called()
        assert receipt["result"] == "dry_run_preview"
        assert receipt["candidates_seen"] == 1
        assert not decision_dir.exists()


class TestNoLlmProbe:
    """codex R3 MAJOR 2, cost half — ``--test --no-llm``.

    ``--test`` makes a run harmless; it does not make it free. It is a real dry
    run that still calls the LLM twice so an operator can see what the dream
    WOULD write. ``persona_dream_tick --child-test`` fans that across the whole
    roster, so the probe an operator is told to run to prove the plumbing was
    quietly a full night's token bill.
    """

    @pytest.mark.asyncio
    async def test_no_llm_stops_before_phase_3_and_calls_nothing(
        self, tmp_path, mock_memory_dir, mock_daily_logs
    ):
        """Signal IS found — the run would otherwise consolidate — and not one
        LLM call is made."""
        mock_rwf = AsyncMock(side_effect=[
            _make_llm_result("Merged signal"),
            _make_llm_result("PRUNE_OK"),
        ])
        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("runtime.lane_router.run_with_runtime_lanes", mock_rwf), \
             patch("memory_dream.append_to_daily_log") as daily:
            from memory_dream import _run_dream_inner

            result = await _run_dream_inner(
                test_mode=True, force=True, days=7, no_llm=True
            )

        assert result == "DREAM_NO_LLM"
        mock_rwf.assert_not_called()
        assert not (tmp_path / "dream-state.json").exists()
        daily.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_same_run_without_no_llm_does_call_the_llm(
        self, tmp_path, mock_memory_dir, mock_daily_logs
    ):
        """Non-vacuity: the fixture above really does reach Phase 3, so the
        assertion is about the flag and not about a run with nothing to do."""
        mock_rwf = AsyncMock(side_effect=[
            _make_llm_result("Merged signal"),
            _make_llm_result("PRUNE_OK"),
        ])
        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("runtime.lane_router.run_with_runtime_lanes", mock_rwf), \
             patch("memory_dream.append_to_daily_log"):
            from memory_dream import _run_dream_inner

            result = await _run_dream_inner(
                test_mode=True, force=True, days=7, no_llm=False
            )

        assert result != "DREAM_NO_LLM"
        # One call, not two: Phase 4 self-skips under 150 MEMORY.md lines in
        # test mode. The point is that Phase 3 is REACHED — otherwise the
        # no-llm assertion above would hold for a run that had nothing to do.
        assert mock_rwf.call_count >= 1

    @pytest.mark.asyncio
    async def test_no_signal_under_no_llm_is_still_silent(
        self, tmp_path, mock_memory_dir, mock_daily_logs_no_signal
    ):
        """The gate sits AFTER the signal check, so a quiet persona keeps
        reporting the honest DREAM_SILENT rather than being relabelled."""
        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("memory_dream.append_to_daily_log"):
            from memory_dream import _run_dream_inner

            result = await _run_dream_inner(
                test_mode=True, force=True, days=7, no_llm=True
            )

        assert result == "DREAM_SILENT"

    def test_no_llm_without_test_is_rejected(self, monkeypatch):
        """A bare ``--no-llm`` would run the free phases for real, and those
        advance ``last_run`` on the way past — spending the night's recency
        budget on a cycle that never consolidated. It is a probe modifier, not
        a cheap dream."""
        import memory_dream

        monkeypatch.setattr(
            sys, "argv", ["memory_dream.py", "--no-llm", "--force"]
        )
        with pytest.raises(SystemExit) as exc:
            memory_dream.main()
        assert exc.value.code != 0

    def test_no_llm_with_test_is_accepted(self, monkeypatch, tmp_path, mock_memory_dir):
        """The inverse: the supported combination parses and reaches run_dream
        with the flag threaded through."""
        import memory_dream

        monkeypatch.setattr(
            sys, "argv", ["memory_dream.py", "--test", "--no-llm"]
        )
        seen = {}

        async def _fake_run_dream(**kwargs):
            seen.update(kwargs)
            return "DREAM_NO_LLM"

        monkeypatch.setattr(memory_dream, "run_dream", _fake_run_dream)
        memory_dream.main()
        assert seen["no_llm"] is True
        assert seen["test_mode"] is True


class TestNoLogsWritesAnHonestReceipt:
    """codex R4 MAJOR 1 — the child half of no-budget-without-proof.

    A persona with no daily logs used to log DREAM_SKIPPED, return None, exit 0
    and write NOTHING. From the parent that is indistinguishable from a child
    that died before it could write, so the only safe reading is "no proof" —
    and reading it as success spent that persona's 20-hour recency budget on a
    dream nobody could show a receipt for.
    """

    @pytest.mark.asyncio
    async def test_no_logs_writes_a_profile_scoped_receipt(
        self, tmp_path, mock_memory_dir
    ):
        state_file = tmp_path / "dream-state.json"
        for stale in (mock_memory_dir / "daily").glob("*.md"):
            stale.unlink()

        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("memory_dream.append_to_daily_log"):
            from memory_dream import _run_dream_inner

            result = await _run_dream_inner(test_mode=False, force=True, days=7)

        assert result is None, "callers must still see None (reflect/weekly contract)"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert state["result"] == "DREAM_SKIPPED"
        assert state["signal_found"] is False
        assert state["phases_completed"] == ["orient"]
        assert state["last_run"]

    @pytest.mark.asyncio
    async def test_no_logs_receipt_still_obeys_the_no_file_edits_contract(
        self, tmp_path, mock_memory_dir
    ):
        state_file = tmp_path / "dream-state.json"
        for stale in (mock_memory_dir / "daily").glob("*.md"):
            stale.unlink()

        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("memory_dream.append_to_daily_log") as daily:
            from memory_dream import _run_dream_inner

            await _run_dream_inner(test_mode=True, force=True, days=7)

        assert not state_file.exists()
        daily.assert_not_called()


class TestSpawnNonceEcho:
    """codex R4 MAJOR 2 — the change proof the parent verifies.

    A timestamp can only say a file CLAIMS a recent moment; an untouched file
    keeps whatever it already said, and a future-dated one clears any lower
    bound forever. The nonce says who wrote it.
    """

    @pytest.mark.asyncio
    async def test_receipt_carries_the_parents_spawn_id(
        self, tmp_path, mock_memory_dir, mock_daily_logs_no_signal, monkeypatch
    ):
        monkeypatch.setenv("HOMIE_DREAM_SPAWN_ID", "nonce-abc123")
        state_file = tmp_path / "dream-state.json"
        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("memory_dream.append_to_daily_log"):
            from memory_dream import _run_dream_inner

            await _run_dream_inner(test_mode=False, force=True, days=7)

        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert state["spawn_id"] == "nonce-abc123"

    @pytest.mark.asyncio
    async def test_manual_run_writes_no_spawn_id(
        self, tmp_path, mock_memory_dir, mock_daily_logs_no_signal, monkeypatch
    ):
        """An operator running the dream by hand has no parent to answer to."""
        monkeypatch.delenv("HOMIE_DREAM_SPAWN_ID", raising=False)
        state_file = tmp_path / "dream-state.json"
        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("memory_dream.append_to_daily_log"):
            from memory_dream import _run_dream_inner

            await _run_dream_inner(test_mode=False, force=True, days=7)

        assert "spawn_id" not in json.loads(state_file.read_text(encoding="utf-8"))


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

            # memory_dir MUST be passed explicitly (its own None-sentinel
            # param) — without it gather_signal falls back to the module's
            # REAL MEMORY_DIR and scans this actual worktree's open
            # episodes/, whose count is ambient session state, not a fixed
            # quantity a threshold test can assert against.
            result = gather_signal([log], days=1, memory_dir=tmp_path)

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

            result = gather_signal([log], days=1, memory_dir=tmp_path)

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


# =============================================================================
# PHASE 5 TESTS — belief evolution (#170): EVOLVE_SILENT, kill-switch scoping,
# zero-new-LLM-call regression guard, adoption throttle, fail-open into success.
# =============================================================================


_BELIEF_CANDIDATE_BLOCK = (
    '{"kind": "belief_candidate", "target_file": "SELF.md", '
    '"summary": "lane-first", "evidence_paths": ["daily/x.md"], '
    '"proposed_content": "The Homie routes by lane first.", '
    '"confidence_score": 0.8}'
)


def test_render_belief_receipt_line_branches():
    from memory_dream import _render_belief_receipt_line

    assert _render_belief_receipt_line({"result": "not_run"}) == ""
    assert _render_belief_receipt_line({"result": "EVOLVE_SILENT"}) == (
        "Beliefs: EVOLVE_SILENT (no new/retryable candidates)"
    )
    assert _render_belief_receipt_line({"result": "skipped_killswitch"}) == (
        "Beliefs: skipped (kill-switch disabled)"
    )
    assert _render_belief_receipt_line({"result": "skipped_evolve_disabled"}) == (
        "Beliefs: skipped (EVOLVE_ENABLED=false)"
    )
    assert _render_belief_receipt_line({"result": "failed", "error": "boom"}) == (
        "Beliefs: phase failed (non-fatal): boom"
    )
    ran = {
        "result": "ran", "proposed": 3, "adopted": 1, "rejected": 1,
        "error_retryable": 1, "error_terminal": 0,
    }
    assert _render_belief_receipt_line(ran) == (
        "Beliefs: 3 proposed, 1 adopted, 1 rejected, 1 retryable-error, 0 terminal-error"
    )
    # lowercase=True is the vault-log style — must actually flip the label.
    assert _render_belief_receipt_line(ran, lowercase=True).startswith("beliefs: ")
    # skipped_corrupt (#170 error-handling hardening) appends a visible suffix
    # rather than silently vanishing corrupted retry-queue files.
    ran_with_corrupt = dict(ran, skipped_corrupt=2)
    assert _render_belief_receipt_line(ran_with_corrupt) == (
        "Beliefs: 3 proposed, 1 adopted, 1 rejected, 1 retryable-error, "
        "0 terminal-error, 2 corrupt-skipped"
    )
    silent_with_corrupt = {"result": "EVOLVE_SILENT", "skipped_corrupt": 1}
    assert _render_belief_receipt_line(silent_with_corrupt) == (
        "Beliefs: EVOLVE_SILENT (no new/retryable candidates), 1 corrupt-skipped"
    )


class TestBeliefEvolvePhase:
    @pytest.mark.asyncio
    async def test_belief_evolve_silent_no_candidates(
        self, tmp_path, mock_memory_dir, mock_daily_logs
    ):
        """No belief candidates + empty retry queue -> EVOLVE_SILENT, propose_belief
        never called (zero extra LLM calls)."""
        mock_rwf = AsyncMock(side_effect=[
            _make_llm_result("CONSOLIDATION_OK"),  # no belief_candidate blocks
            _make_llm_result("PRUNE_OK"),
        ])
        never = AsyncMock()
        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("runtime.lane_router.run_with_runtime_lanes", mock_rwf), \
             patch("config.BELIEF_EVOLVE_DECISION_DIR", tmp_path / "decisions"), \
             patch("evolve.evolve_loop.propose_belief", never), \
             patch("memory_dream._run_entity_compilation"), \
             patch("memory_dream._run_reindex"):
            from memory_dream import _run_dream_inner

            await _run_dream_inner(test_mode=False, force=True, days=7)

            state = json.loads((tmp_path / "dream-state.json").read_text(encoding="utf-8"))
            assert state["belief_evolve"]["result"] == "EVOLVE_SILENT"
            never.assert_not_called()

    @pytest.mark.asyncio
    async def test_belief_evolve_killswitch_skips_but_dream_still_succeeds(
        self, tmp_path, mock_memory_dir, mock_daily_logs, monkeypatch
    ):
        """HOMIE_KILLSWITCH_BELIEF_AUTONOMY=disabled -> Phase 5 skipped, but the
        overall dream still succeeds (result='consolidated')."""
        monkeypatch.setenv("HOMIE_KILLSWITCH_BELIEF_AUTONOMY", "disabled")
        mock_rwf = AsyncMock(side_effect=[
            _make_llm_result("Merged signal " + _BELIEF_CANDIDATE_BLOCK),
            _make_llm_result("PRUNE_OK"),
        ])
        never = AsyncMock()
        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("runtime.lane_router.run_with_runtime_lanes", mock_rwf), \
             patch("evolve.evolve_loop.propose_belief", never), \
             patch("memory_dream._run_entity_compilation"), \
             patch("memory_dream._run_reindex"):
            from memory_dream import _run_dream_inner

            await _run_dream_inner(test_mode=False, force=True, days=7)

            state = json.loads((tmp_path / "dream-state.json").read_text(encoding="utf-8"))
            assert state["result"] == "consolidated"  # dream itself UNCHANGED
            assert state["belief_evolve"]["result"] == "skipped_killswitch"
            never.assert_not_called()

    @pytest.mark.asyncio
    async def test_belief_evolve_empty_evidence_rejected_end_to_end(
        self, tmp_path, mock_memory_dir, mock_daily_logs
    ):
        """THE CAMPAIGN CRUX, end-to-end through the REAL propose_belief gate
        chain (not mocked): an empty-evidence high-confidence belief is REJECTED
        autonomously and SELF.md is BYTE-UNCHANGED — even with a LYING judge
        that approves it with max scores. The deterministic evidence gate blocks
        the write the judge cannot override (issue #170 / #175 acceptance)."""
        self_md = mock_memory_dir / "SELF.md"
        before = self_md.read_bytes()

        empty_ev_block = (
            '{"kind": "belief_candidate", "target_file": "SELF.md", '
            '"summary": "unearned claim", "evidence_paths": [], '
            '"proposed_content": "The Homie has read the entire vault.", '
            '"confidence_score": 0.99}'
        )
        # Block must be on its OWN line to parse (_iter_json_records requires a
        # line starting with '{').
        mock_rwf = AsyncMock(side_effect=[
            _make_llm_result("Merged signal.\n" + empty_ev_block),
            _make_llm_result("PRUNE_OK"),
        ])
        # A LYING judge: approves with max scores. The REAL evidence gate must
        # still block the write because evidence_paths is empty.
        lying_judge = AsyncMock(return_value={
            "supported": True, "correctness": 1.0, "evidence_fidelity": 1.0,
            "reason": "looks great",
        })
        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("runtime.lane_router.run_with_runtime_lanes", mock_rwf), \
             patch("config.BELIEF_EVOLVE_DECISION_DIR", tmp_path / "decisions"), \
             patch("evolve.judge.judge_belief_candidate", lying_judge), \
             patch("memory_dream._run_entity_compilation"), \
             patch("memory_dream._run_reindex"):
            from memory_dream import _run_dream_inner

            await _run_dream_inner(test_mode=False, force=True, days=7)

            state = json.loads((tmp_path / "dream-state.json").read_text(encoding="utf-8"))
            be = state["belief_evolve"]
            # The candidate WAS proposed (real gate ran) but adopted NOTHING.
            assert be["proposed"] >= 1
            assert be["adopted"] == 0
            # The load-bearing assertion: identity file untouched by autonomy.
            assert self_md.read_bytes() == before

    @pytest.mark.asyncio
    async def test_belief_evolve_regression_call_count_unchanged(
        self, tmp_path, mock_memory_dir, mock_daily_logs
    ):
        """Regression guard for the 'zero new LLM calls' design claim: with Phase 5
        wired in, the happy path still makes EXACTLY 2 run_with_runtime_lanes calls
        (consolidate + prune) — belief evolution rides the SAME consolidation call."""
        mock_rwf = AsyncMock(side_effect=[
            _make_llm_result("Merged signal into MEMORY.md"),
            _make_llm_result("PRUNE_OK"),
        ])
        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("runtime.lane_router.run_with_runtime_lanes", mock_rwf), \
             patch("config.BELIEF_EVOLVE_DECISION_DIR", tmp_path / "decisions"), \
             patch("memory_dream._run_entity_compilation"), \
             patch("memory_dream._run_reindex"):
            from memory_dream import _run_dream_inner

            await _run_dream_inner(test_mode=False, force=True, days=7)

            assert mock_rwf.call_count == 2  # NO third call added by Phase 5

    @pytest.mark.asyncio
    async def test_belief_evolve_throttle_stops_after_max_adoptions(
        self, tmp_path, mock_memory_dir, mock_daily_logs, monkeypatch
    ):
        """3 candidates all 'adopt' with max_adoptions_per_night=2 -> propose_belief
        called only twice, receipt.adopted == 2 (remaining left for tomorrow)."""
        monkeypatch.setenv("BELIEF_MAX_ADOPTIONS_PER_NIGHT", "2")
        monkeypatch.setenv("BELIEF_MAX_CANDIDATES_PER_NIGHT", "5")
        mock_rwf = AsyncMock(side_effect=[
            _make_llm_result("Merged signal"),
            _make_llm_result("PRUNE_OK"),
        ])
        fake_candidates = [
            {"target_file": "SELF.md", "evidence_paths": ["daily/x.md"], "summary": f"c{i}"}
            for i in range(3)
        ]
        propose = AsyncMock(return_value={"outcome": "adopt", "retryable": False})
        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("runtime.lane_router.run_with_runtime_lanes", mock_rwf), \
             patch("evolve.evolve_loop.extract_belief_candidates", lambda _t: fake_candidates), \
             patch("evolve.evolve_loop.load_retryable_belief_candidates", lambda **_k: ([], 0)), \
             patch("evolve.evolve_loop.propose_belief", propose), \
             patch("memory_dream._run_entity_compilation"), \
             patch("memory_dream._run_reindex"):
            from memory_dream import _run_dream_inner

            await _run_dream_inner(test_mode=False, force=True, days=7)

            assert propose.call_count == 2  # throttle stopped after 2 adoptions
            state = json.loads((tmp_path / "dream-state.json").read_text(encoding="utf-8"))
            assert state["belief_evolve"]["adopted"] == 2

    @pytest.mark.asyncio
    async def test_belief_evolve_never_raises_into_dream_failure(
        self, tmp_path, mock_memory_dir, mock_daily_logs
    ):
        """propose_belief raising an unexpected error -> Phase 5's own try/except
        contains it: overall dream result='consolidated' (NOT 'failed'), belief
        receipt result='failed' with an 'error' key."""
        mock_rwf = AsyncMock(side_effect=[
            _make_llm_result("Merged signal"),
            _make_llm_result("PRUNE_OK"),
        ])
        fake_candidates = [
            {"target_file": "SELF.md", "evidence_paths": ["daily/x.md"], "summary": "c0"}
        ]
        boom = AsyncMock(side_effect=RuntimeError("unexpected phase-5 bug"))
        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("runtime.lane_router.run_with_runtime_lanes", mock_rwf), \
             patch("evolve.evolve_loop.extract_belief_candidates", lambda _t: fake_candidates), \
             patch("evolve.evolve_loop.load_retryable_belief_candidates", lambda **_k: ([], 0)), \
             patch("evolve.evolve_loop.propose_belief", boom), \
             patch("memory_dream._run_entity_compilation"), \
             patch("memory_dream._run_reindex"):
            from memory_dream import _run_dream_inner

            await _run_dream_inner(test_mode=False, force=True, days=7)

            state = json.loads((tmp_path / "dream-state.json").read_text(encoding="utf-8"))
            assert state["result"] == "consolidated"  # dream NOT flipped to failed
            assert state["belief_evolve"]["result"] == "failed"
            assert "error" in state["belief_evolve"]

    @pytest.mark.asyncio
    async def test_belief_evolve_retry_candidates_processed_before_fresh(
        self, tmp_path, mock_memory_dir, mock_daily_logs, monkeypatch
    ):
        """Retry-queue candidates must be judged before fresh candidates so a
        throttle cap doesn't starve self-healing in favor of brand-new proposals."""
        monkeypatch.setenv("BELIEF_MAX_ADOPTIONS_PER_NIGHT", "1")
        mock_rwf = AsyncMock(side_effect=[
            _make_llm_result("Merged signal " + _BELIEF_CANDIDATE_BLOCK),
            _make_llm_result("PRUNE_OK"),
        ])
        retry_candidate = {
            "id": "retry-1",
            "target_file": "SELF.md",
            "evidence_paths": ["daily/x.md"],
            "_attempts": 1,
        }
        fresh_candidate = {
            "target_file": "SELF.md",
            "evidence_paths": ["daily/y.md"],
            "summary": "fresh",
        }
        propose = AsyncMock(return_value={"outcome": "adopt", "retryable": False})
        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("runtime.lane_router.run_with_runtime_lanes", mock_rwf), \
             patch("evolve.evolve_loop.load_retryable_belief_candidates",
                   lambda **_k: ([retry_candidate], 0)), \
             patch("evolve.evolve_loop.extract_belief_candidates",
                   lambda _t: [fresh_candidate]), \
             patch("evolve.evolve_loop.propose_belief", propose), \
             patch("memory_dream._run_entity_compilation"), \
             patch("memory_dream._run_reindex"):
            from memory_dream import _run_dream_inner

            await _run_dream_inner(test_mode=False, force=True, days=7)

            # Only 1 call fires (throttle=1) — it must be the retry candidate.
            assert propose.call_count == 1
            called_cand = propose.call_args.args[0]
            assert called_cand.get("id") == "retry-1"
            state = json.loads((tmp_path / "dream-state.json").read_text(encoding="utf-8"))
            assert state["belief_evolve"]["retried"] == 1
            assert state["belief_evolve"]["candidates_seen"] == 2

    @pytest.mark.asyncio
    async def test_belief_evolve_receipt_counts_retryable_vs_terminal_errors(
        self, tmp_path, mock_memory_dir, mock_daily_logs
    ):
        """propose_belief returning outcome='error' must increment the correct
        receipt bucket based on retryable, not just be lumped into 'rejected'."""
        mock_rwf = AsyncMock(side_effect=[
            _make_llm_result("Merged signal"),
            _make_llm_result("PRUNE_OK"),
        ])
        fake_candidates = [
            {"target_file": "SELF.md", "evidence_paths": ["daily/x.md"], "summary": "c0"},
            {"target_file": "SELF.md", "evidence_paths": ["daily/y.md"], "summary": "c1"},
        ]
        propose = AsyncMock(side_effect=[
            {"outcome": "error", "retryable": True},
            {"outcome": "error", "retryable": False},
        ])
        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("runtime.lane_router.run_with_runtime_lanes", mock_rwf), \
             patch("evolve.evolve_loop.extract_belief_candidates", lambda _t: fake_candidates), \
             patch("evolve.evolve_loop.load_retryable_belief_candidates",
                   lambda **_k: ([], 0)), \
             patch("evolve.evolve_loop.propose_belief", propose), \
             patch("memory_dream._run_entity_compilation"), \
             patch("memory_dream._run_reindex"):
            from memory_dream import _run_dream_inner

            await _run_dream_inner(test_mode=False, force=True, days=7)

            state = json.loads((tmp_path / "dream-state.json").read_text(encoding="utf-8"))
            assert state["belief_evolve"]["error_retryable"] == 1
            assert state["belief_evolve"]["error_terminal"] == 1
            assert state["belief_evolve"]["adopted"] == 0
            assert state["belief_evolve"]["rejected"] == 0

    @pytest.mark.asyncio
    async def test_belief_evolve_caps_fresh_but_not_retry_queue(
        self, tmp_path, mock_memory_dir, mock_daily_logs, monkeypatch
    ):
        """max_candidates_per_night truncates FRESH candidates only; the retry
        queue backlog must never be silently dropped."""
        monkeypatch.setenv("BELIEF_MAX_CANDIDATES_PER_NIGHT", "2")
        monkeypatch.setenv("BELIEF_MAX_ADOPTIONS_PER_NIGHT", "100")  # no throttle interference
        mock_rwf = AsyncMock(side_effect=[
            _make_llm_result("Merged signal"),
            _make_llm_result("PRUNE_OK"),
        ])
        retry_candidates = [
            {"id": f"retry-{i}", "target_file": "SELF.md",
             "evidence_paths": ["daily/x.md"], "_attempts": 1}
            for i in range(4)
        ]
        fresh_candidates = [
            {"target_file": "SELF.md", "evidence_paths": ["daily/y.md"], "summary": f"f{i}"}
            for i in range(5)
        ]
        propose = AsyncMock(return_value={"outcome": "reject", "retryable": False})
        with _patch_dream(mock_memory_dir, tmp_path), \
             patch("runtime.lane_router.run_with_runtime_lanes", mock_rwf), \
             patch("evolve.evolve_loop.load_retryable_belief_candidates",
                   lambda **_k: (retry_candidates, 0)), \
             patch("evolve.evolve_loop.extract_belief_candidates",
                   lambda _t: fresh_candidates), \
             patch("evolve.evolve_loop.propose_belief", propose), \
             patch("memory_dream._run_entity_compilation"), \
             patch("memory_dream._run_reindex"):
            from memory_dream import _run_dream_inner

            await _run_dream_inner(test_mode=False, force=True, days=7)

            # 4 retry (uncapped) + 2 fresh (capped from 5) = 6
            assert propose.call_count == 6
