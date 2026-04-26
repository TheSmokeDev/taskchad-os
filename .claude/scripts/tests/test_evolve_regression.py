"""Phase 2.6 tests — regression corpus loader + evaluator + veto integration.

The regression corpus (regression_queries.json) is the structural floor that
--force cannot override. These tests pin three contracts:

1. The loader fail-loud on schema violations (missing fields, malformed
   min_top_score, empty file). Anything weaker would silently disable
   regression enforcement.
2. evaluate_regression_corpus correctly classifies failures by reason
   (no_results > wrong_top_path > below_min_score).
3. The veto integration treats regression failures as ALWAYS hard — soft
   path is unreachable, --force does not flip them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for _p in (_SCRIPTS_DIR, _CHAT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _make_replay_result(query: str, top_score: float = 0.5,
                         top_path: str = "vault/memory/MEMORY.md",
                         results_count: int | None = None):
    """Build a ReplayQueryResult with the given top hit. results_count
    defaults to 1 when there's a top_score, 0 otherwise."""
    from evolve.models import ReplayQueryResult

    if results_count is None:
        results_count = 1 if top_score > 0 else 0
    top_scores = [top_score] if top_score > 0 else []
    result_paths = [top_path] if top_score > 0 else []
    return ReplayQueryResult(
        query=query,
        results_count=results_count,
        top_scores=top_scores,
        result_paths=result_paths,
    )


def _make_entry(**overrides):
    from evolve.regression import RegressionEntry

    base = {
        "query": "test query",
        "fixed_in": "PR #X — test fix",
        "expected_top_path": "vault/memory/MEMORY.md",
        "min_top_score": 0.45,
    }
    base.update(overrides)
    return RegressionEntry(**base)


# ── TestEvaluateCorpus ────────────────────────────────────────────────────


class TestEvaluateCorpus:
    def test_all_pass_returns_empty_failed_list(self):
        from evolve.regression import evaluate_regression_corpus

        entries = [
            _make_entry(query="A", min_top_score=0.40),
            _make_entry(query="B", min_top_score=0.40),
        ]
        per_query = [
            _make_replay_result("A", top_score=0.55),
            _make_replay_result("B", top_score=0.50),
        ]
        summary = evaluate_regression_corpus(per_query, entries)
        assert summary.total == 2
        assert summary.passed == 2
        assert summary.failed == []

    def test_below_min_score_flagged(self):
        from evolve.regression import evaluate_regression_corpus

        entries = [_make_entry(query="A", min_top_score=0.45)]
        per_query = [_make_replay_result("A", top_score=0.30)]
        summary = evaluate_regression_corpus(per_query, entries)
        assert summary.passed == 0
        assert len(summary.failed) == 1
        assert summary.failed[0].reason == "below_min_score"
        assert summary.failed[0].observed_top_score == 0.30

    def test_no_results_flagged(self):
        from evolve.regression import evaluate_regression_corpus

        entries = [_make_entry(query="A")]
        per_query = [_make_replay_result("A", top_score=0.0, results_count=0)]
        summary = evaluate_regression_corpus(per_query, entries)
        assert summary.failed[0].reason == "no_results"
        assert summary.failed[0].observed_top_score == 0.0

    def test_wrong_top_path_flagged(self):
        from evolve.regression import evaluate_regression_corpus

        entries = [_make_entry(
            query="A",
            expected_top_path="vault/memory/MEMORY.md",
            min_top_score=0.30,  # Below observed score so path takes priority
        )]
        per_query = [_make_replay_result(
            "A", top_score=0.55, top_path="vault/memory/SOUL.md",
        )]
        summary = evaluate_regression_corpus(per_query, entries)
        assert summary.failed[0].reason == "wrong_top_path"
        assert summary.failed[0].observed_top_path == "vault/memory/SOUL.md"

    def test_length_mismatch_raises(self):
        from evolve.regression import evaluate_regression_corpus

        entries = [_make_entry(query="A"), _make_entry(query="B")]
        per_query = [_make_replay_result("A")]  # only 1 vs 2 entries
        with pytest.raises(ValueError, match="length mismatch"):
            evaluate_regression_corpus(per_query, entries)


# ── TestLoaderSchema ─────────────────────────────────────────────────────


class TestLoaderSchema:
    def _write_regression(self, tmp_path: Path, queries) -> Path:
        path = tmp_path / "regression_queries.json"
        path.write_text(
            json.dumps({"version": 1, "queries": queries}),
            encoding="utf-8",
        )
        return path

    def test_missing_required_field_raises(self, tmp_path):
        from evolve.goldens import load_regression_queries

        # Missing min_top_score
        path = self._write_regression(tmp_path, [{
            "query": "x",
            "fixed_in": "PR #X",
            "expected_top_path": "y",
        }])
        with pytest.raises(ValueError, match="min_top_score"):
            load_regression_queries(path=path)

    def test_empty_corpus_rejected(self, tmp_path):
        """An empty regression list would silently disable hard-veto
        enforcement — same posture as veto.py rejecting empty rulesets."""
        from evolve.goldens import load_regression_queries

        path = self._write_regression(tmp_path, [])
        with pytest.raises(ValueError, match="at least one entry"):
            load_regression_queries(path=path)

    def test_min_top_score_out_of_range_raises(self, tmp_path):
        from evolve.goldens import load_regression_queries

        path = self._write_regression(tmp_path, [{
            "query": "x", "fixed_in": "PR #X",
            "expected_top_path": "y", "min_top_score": 1.5,
        }])
        with pytest.raises(ValueError, match="must be in"):
            load_regression_queries(path=path)

    def test_load_regression_entries_coerces(self):
        from evolve.regression import load_regression_entries

        raw = [{
            "query": "q1", "fixed_in": "PR #1",
            "expected_top_path": "p1", "min_top_score": "0.5",  # string
        }]
        entries = load_regression_entries(raw)
        assert len(entries) == 1
        assert entries[0].min_top_score == 0.5  # coerced to float


# ── 2.6.1 hardening — Codex review 2026-04-26 ─────────────────────────────


class TestPathNormalization:
    """Codex finding 1: replay reports record memory-relative paths
    (`MEMORY.md`, `concepts/X.md`) but a human-curated regression corpus
    naturally writes repo-relative (`vault/memory/MEMORY.md`). Without
    normalization, every regression entry classifies as wrong_top_path
    even on healthy candidates, hard-vetoing all adoptions."""

    def test_repo_relative_matches_memory_relative(self):
        """`vault/memory/MEMORY.md` (corpus) vs `MEMORY.md` (replay)
        must compare EQUAL after normalization, NOT trip wrong_top_path."""
        from evolve.regression import evaluate_regression_corpus

        entry = _make_entry(
            query="A",
            expected_top_path="vault/memory/MEMORY.md",
            min_top_score=0.30,
        )
        # Replay produces the memory-relative form
        per_query = [_make_replay_result("A", top_score=0.55, top_path="MEMORY.md")]
        summary = evaluate_regression_corpus(per_query, [entry])
        assert summary.passed == 1
        assert summary.failed == []

    def test_concept_path_normalizes_correctly(self):
        """`concepts/HERMES-AGENT.md` (replay) vs
        `vault/memory/concepts/HERMES-AGENT.md` (corpus) must match."""
        from evolve.regression import evaluate_regression_corpus

        entry = _make_entry(
            query="A",
            expected_top_path="vault/memory/concepts/HERMES-AGENT.md",
            min_top_score=0.30,
        )
        per_query = [_make_replay_result(
            "A", top_score=0.55, top_path="concepts/HERMES-AGENT.md",
        )]
        summary = evaluate_regression_corpus(per_query, [entry])
        assert summary.passed == 1

    def test_truly_different_path_still_drifts(self):
        """Sanity: normalization must not over-collapse. SOUL.md vs MEMORY.md
        is a real wrong_top_path even after normalization."""
        from evolve.regression import evaluate_regression_corpus

        entry = _make_entry(
            query="A",
            expected_top_path="MEMORY.md",
            min_top_score=0.30,
        )
        per_query = [_make_replay_result("A", top_score=0.55, top_path="SOUL.md")]
        summary = evaluate_regression_corpus(per_query, [entry])
        assert summary.failed[0].reason == "wrong_top_path"

    def test_normalize_path_handles_windows_separators(self):
        """Codex finding 1 explicitly mentions abs-path scenarios. Windows
        backslashes must normalize to forward-slashes."""
        from evolve.regression import _normalize_path

        win_abs = "C:\\Users\\YourUser\\thehomie\\TheHomie\\Memory\\MEMORY.md"
        repo_rel = "vault/memory/MEMORY.md"
        memory_rel = "MEMORY.md"
        assert (
            _normalize_path(win_abs)
            == _normalize_path(repo_rel)
            == _normalize_path(memory_rel)
            == "MEMORY.md"
        )


class TestPropagationDoesNotMaskNoResult:
    """Codex finding 1 second-order: path normalization should NOT swallow
    no_results into a wrong_top_path verdict — the ordering in
    _classify_failure (no_results > wrong_top_path > below_min_score)
    must hold."""

    def test_no_results_takes_priority_over_wrong_path(self):
        from evolve.regression import evaluate_regression_corpus

        entry = _make_entry(query="A", expected_top_path="MEMORY.md")
        per_query = [_make_replay_result(
            "A", top_score=0.0, results_count=0,
        )]
        summary = evaluate_regression_corpus(per_query, [entry])
        # Empty results → no_results, not wrong_top_path
        assert summary.failed[0].reason == "no_results"


# ── TestVetoIntegration ─────────────────────────────────────────────────


class TestVetoIntegration:
    def test_clean_summary_does_not_block(self):
        """Phase 2.6: a regression_summary with zero failures must not
        change the verdict from what evaluate_veto would produce alone."""
        from evolve.compare import ReportDelta
        from evolve.regression import RegressionSummary
        from evolve.veto import DEFAULT_VETO_RULESET, evaluate_veto

        delta = ReportDelta(
            baseline_experiment_id="b", candidate_experiment_id="c",
        )  # All-zero delta — would be ADOPT
        clean = RegressionSummary(total=5, passed=5, failed=[])

        verdict_with_clean = evaluate_veto(
            delta, DEFAULT_VETO_RULESET, regression_summary=clean,
        )
        verdict_without = evaluate_veto(delta, DEFAULT_VETO_RULESET)

        assert verdict_with_clean.accepted == verdict_without.accepted
        assert verdict_with_clean.regression_failures == []

    def test_failures_force_hard_veto_even_with_clean_delta(self):
        """A clean delta + ANY regression failure = hard veto. Soft path
        is unreachable when regressions exist."""
        from evolve.compare import ReportDelta
        from evolve.regression import RegressionSummary
        from evolve.veto import DEFAULT_VETO_RULESET, evaluate_veto

        delta = ReportDelta(baseline_experiment_id="b", candidate_experiment_id="c")
        entry = _make_entry(query="bug-#21", min_top_score=0.45)
        from evolve.regression import RegressionFailure

        summary = RegressionSummary(
            total=1, passed=0,
            failed=[RegressionFailure(
                entry=entry, observed_top_score=0.20,
                observed_top_path="vault/memory/MEMORY.md",
                reason="below_min_score",
            )],
        )

        verdict = evaluate_veto(
            delta, DEFAULT_VETO_RULESET, regression_summary=summary,
        )
        assert not verdict.accepted
        # Critical: NOT soft. Regression failures are always hard.
        assert not verdict.soft

    def test_force_does_not_flip_regression_hard_veto(self):
        """compute_exit_code with force=True flips SOFT verdicts to ADOPT.
        Regression failures produce HARD verdicts (verdict.soft=False), so
        force should NOT flip them."""
        from evolve.compare import ReportDelta
        from evolve.regression import (
            RegressionEntry,
            RegressionFailure,
            RegressionSummary,
        )
        from evolve.veto import (
            DEFAULT_VETO_RULESET,
            ExitCode,
            compute_exit_code,
            evaluate_veto,
        )

        delta = ReportDelta(baseline_experiment_id="b", candidate_experiment_id="c")
        summary = RegressionSummary(
            total=1, passed=0,
            failed=[RegressionFailure(
                entry=RegressionEntry(
                    query="x", fixed_in="PR #21",
                    expected_top_path="path", min_top_score=0.5,
                ),
                observed_top_score=0.1,
                observed_top_path="path",
                reason="below_min_score",
            )],
        )
        verdict = evaluate_veto(
            delta, DEFAULT_VETO_RULESET, regression_summary=summary,
        )
        assert compute_exit_code(verdict, force=True) == ExitCode.HARD_VETO

    def test_to_dict_roundtrip(self):
        """VetoVerdict.to_dict must include regression_failures so the
        decision artifact captures the full audit context."""
        from evolve.compare import ReportDelta
        from evolve.regression import (
            RegressionEntry,
            RegressionFailure,
            RegressionSummary,
        )
        from evolve.veto import DEFAULT_VETO_RULESET, evaluate_veto

        delta = ReportDelta(baseline_experiment_id="b", candidate_experiment_id="c")
        summary = RegressionSummary(
            total=1, passed=0,
            failed=[RegressionFailure(
                entry=RegressionEntry(
                    query="x", fixed_in="PR #X",
                    expected_top_path="p", min_top_score=0.5,
                ),
                observed_top_score=0.1,
                observed_top_path="p",
                reason="below_min_score",
            )],
        )
        verdict = evaluate_veto(
            delta, DEFAULT_VETO_RULESET, regression_summary=summary,
        )
        d = verdict.to_dict()
        # Round-trip via JSON
        s = json.dumps(d)
        assert "regression_failures" in s
        assert "PR #X" in s
        assert "below_min_score" in s
