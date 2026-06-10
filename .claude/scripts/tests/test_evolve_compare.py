"""Regression tests for evolve.compare -- query-identity guard and error verdicts.

Tests guard against the three bugs fixed in this PR:
1. Index-based pairing (now raises on reorder/length mismatch)
2. Error field ignored (now produces new_error/fixed_error/still_errored)
3. error_count_delta not surfaced (now present on ReportDelta and QueryDelta)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for _p in (_SCRIPTS_DIR, _CHAT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from evolve.compare import _classify, compare_reports, format_delta_table  # noqa: E402
from evolve.models import ReplayQueryResult, ReplayReport, ReplaySummary  # noqa: E402


def _make_report(queries: list[ReplayQueryResult], exp_id: str = "test") -> ReplayReport:
    return ReplayReport(
        experiment_id=exp_id,
        timestamp_utc="2026-01-01T00:00:00Z",
        overrides={},
        config_snapshot={},
        per_query=queries,
        summary=ReplaySummary(query_count=len(queries)),
    )


def _result(query: str, *, results: int = 1, error: str = "") -> ReplayQueryResult:
    return ReplayQueryResult(
        query=query,
        results_count=results,
        top_scores=[0.9] if results > 0 else [],
        error=error,
    )


# --- Query identity guard ---


def test_reordered_queries_raise():
    """Swapped query order must raise ValueError, not produce a silent verdict table."""
    baseline = _make_report([_result("A"), _result("B"), _result("C")])
    candidate = _make_report([_result("C"), _result("B"), _result("A")])
    with pytest.raises(ValueError, match="mismatch"):
        compare_reports(baseline, candidate)


def test_length_mismatch_raises():
    """Baseline with 3 queries vs candidate with 2 must raise, not pad silently."""
    baseline = _make_report([_result("A"), _result("B"), _result("C")])
    candidate = _make_report([_result("A"), _result("B")])
    with pytest.raises(ValueError, match="length mismatch"):
        compare_reports(baseline, candidate)


def test_identical_queries_do_not_raise():
    """Same query set in same order must not raise."""
    baseline = _make_report([_result("A"), _result("B")])
    candidate = _make_report([_result("A"), _result("B")])
    delta = compare_reports(baseline, candidate)
    assert len(delta.per_query) == 2


# --- Error verdicts ---


def test_classify_new_error():
    """Candidate errors, baseline was fine -> new_error."""
    b = _result("Q", results=3)
    c = _result("Q", results=0, error="timeout")
    assert _classify(b, c) == "new_error"


def test_classify_fixed_error():
    """Baseline errored, candidate is fine -> fixed_error."""
    b = _result("Q", results=0, error="timeout")
    c = _result("Q", results=3)
    assert _classify(b, c) == "fixed_error"


def test_classify_still_errored():
    """Both baseline and candidate error -> still_errored, not still_missing."""
    b = _result("Q", results=0, error="timeout")
    c = _result("Q", results=0, error="index error")
    assert _classify(b, c) == "still_errored"


def test_classify_still_missing_requires_no_error():
    """Zero results with no error is still_missing, not still_errored."""
    b = _result("Q", results=0, error="")
    c = _result("Q", results=0, error="")
    assert _classify(b, c) == "still_missing"


# --- error_count_delta ---


def test_error_count_delta_new_error():
    """One new error in candidate -> error_count_delta == +1."""
    baseline = _make_report([_result("A"), _result("B")])
    candidate = _make_report([_result("A"), _result("B", results=0, error="fail")])
    delta = compare_reports(baseline, candidate)
    assert delta.error_count_delta == 1
    q_b = next(q for q in delta.per_query if q.query == "B")
    assert q_b.error_count_delta == 1
    assert q_b.verdict == "new_error"


def test_error_count_delta_fixed_error():
    """One fixed error -> error_count_delta == -1."""
    baseline = _make_report([_result("A", results=0, error="old fail"), _result("B")])
    candidate = _make_report([_result("A"), _result("B")])
    delta = compare_reports(baseline, candidate)
    assert delta.error_count_delta == -1


def test_error_count_delta_zero_no_errors():
    """No errors in either report -> error_count_delta == 0."""
    baseline = _make_report([_result("A"), _result("B")])
    candidate = _make_report([_result("A"), _result("B")])
    delta = compare_reports(baseline, candidate)
    assert delta.error_count_delta == 0


def test_error_count_delta_in_to_dict():
    """error_count_delta must appear in ReportDelta.to_dict()."""
    baseline = _make_report([_result("A")])
    candidate = _make_report([_result("A", results=0, error="fail")])
    delta = compare_reports(baseline, candidate)
    d = delta.to_dict()
    assert "error_count_delta" in d
    assert d["error_count_delta"] == 1


# --- format_delta_table() error paths ---


def test_format_delta_table_shows_fixed_error_arrow():
    """fixed_error verdict must use ~ arrow in per-query table."""
    baseline = _make_report([_result("Q", results=0, error="timeout")])
    candidate = _make_report([_result("Q", results=3)])
    delta = compare_reports(baseline, candidate)
    assert "~ [fixed_error  ]" in format_delta_table(delta)


def test_format_delta_table_shows_error_count_when_nonzero():
    """error_count line must appear when error_count_delta != 0."""
    baseline = _make_report([_result("Q")])
    candidate = _make_report([_result("Q", results=0, error="fail")])
    delta = compare_reports(baseline, candidate)
    assert "error_count:" in format_delta_table(delta)


def test_format_delta_table_hides_error_count_when_zero():
    """error_count line must be absent when error_count_delta == 0."""
    baseline = _make_report([_result("Q")])
    candidate = _make_report([_result("Q")])
    delta = compare_reports(baseline, candidate)
    assert "error_count:" not in format_delta_table(delta)


# --- baseline_error / candidate_error field assignment ---


def test_query_delta_error_fields_on_correct_side():
    """baseline_error and candidate_error must not be swapped on QueryDelta."""
    baseline = _make_report([_result("Q", results=0, error="baseline-err")])
    candidate = _make_report([_result("Q", results=0, error="candidate-err")])
    delta = compare_reports(baseline, candidate)
    assert delta.per_query[0].baseline_error == "baseline-err"
    assert delta.per_query[0].candidate_error == "candidate-err"


# --- Pre-existing _classify() hit/miss verdicts ---


def test_classify_new_hit():
    """Baseline had no results; candidate returns results -> new_hit."""
    b = _result("Q", results=0)
    c = _result("Q", results=3)
    assert _classify(b, c) == "new_hit"


def test_classify_lost_hit():
    """Baseline returned results; candidate returns nothing -> lost_hit."""
    b = _result("Q", results=3)
    c = _result("Q", results=0)
    assert _classify(b, c) == "lost_hit"


def test_classify_same_within_noise_floor():
    """Score delta below SCORE_NOISE_FLOOR stays same."""
    from evolve.compare import SCORE_NOISE_FLOOR
    from evolve.models import ReplayQueryResult

    b = ReplayQueryResult(query="Q", results_count=1, top_scores=[0.900])
    c = ReplayQueryResult(query="Q", results_count=1, top_scores=[0.900 + SCORE_NOISE_FLOOR / 2])
    assert _classify(b, c) == "same"


def test_classify_better():
    """Score delta above noise floor and positive -> better."""
    from evolve.models import ReplayQueryResult

    b = ReplayQueryResult(query="Q", results_count=1, top_scores=[0.7])
    c = ReplayQueryResult(query="Q", results_count=1, top_scores=[0.9])
    assert _classify(b, c) == "better"


def test_classify_worse():
    """Score delta above noise floor and negative -> worse."""
    from evolve.models import ReplayQueryResult

    b = ReplayQueryResult(query="Q", results_count=1, top_scores=[0.9])
    c = ReplayQueryResult(query="Q", results_count=1, top_scores=[0.7])
    assert _classify(b, c) == "worse"


# ── Phase 2.6 — bootstrap CI bands on compare_reports ──────────────────────


class TestWithCi:
    """Phase 2.6: compare_reports(with_ci=True) populates hit_rate_ci_95 and
    avg_top_score_ci_95 via paired bootstrap; format_delta_table renders
    [95% CI: lo, hi] suffix when present."""

    def test_default_with_ci_false_leaves_fields_none(self):
        baseline = _make_report([_result("A"), _result("B")])
        candidate = _make_report([_result("A"), _result("B")])

        delta = compare_reports(baseline, candidate)
        assert delta.hit_rate_ci_95 is None
        assert delta.avg_top_score_ci_95 is None

        d = delta.to_dict()
        assert d["hit_rate_ci_95"] is None
        assert d["avg_top_score_ci_95"] is None

    def test_with_ci_true_populates_fields(self):
        """No-op replay (baseline == candidate) should produce CIs that
        bracket 0 with negligible width."""
        baseline = _make_report([_result("A"), _result("B"), _result("C")])
        candidate = _make_report([_result("A"), _result("B"), _result("C")])

        delta = compare_reports(baseline, candidate, with_ci=True, ci_seed=42)

        assert delta.hit_rate_ci_95 is not None
        assert delta.avg_top_score_ci_95 is not None
        # No-op deltas → both bounds are 0.0
        assert delta.hit_rate_ci_95 == (0.0, 0.0)
        assert delta.avg_top_score_ci_95 == (0.0, 0.0)

    def test_format_table_renders_ci_bands(self):
        """format_delta_table must include [95% CI: ...] suffix when CIs
        are populated, omit it otherwise."""
        baseline = _make_report([_result("A"), _result("B")])
        candidate = _make_report([_result("A"), _result("B")])

        # Without CI
        plain = format_delta_table(compare_reports(baseline, candidate))
        assert "95% CI" not in plain

        # With CI
        with_ci = format_delta_table(
            compare_reports(baseline, candidate, with_ci=True, ci_seed=42)
        )
        assert "95% CI" in with_ci
        # Both metric lines should have the suffix
        assert with_ci.count("[95% CI:") == 2  # hit_rate + avg_top_score


# --- Issue #20: typed mismatch exception + error detail rendering ---


class TestQueryIdentityMismatchTyped:
    """F1 — mismatch guard raises the typed exception the CLI catches."""

    def test_length_mismatch_raises_typed_exception(self):
        from evolve.compare import QueryIdentityMismatch

        baseline = _make_report([_result("A"), _result("B"), _result("C")])
        candidate = _make_report([_result("A"), _result("B")])
        with pytest.raises(QueryIdentityMismatch, match="length mismatch"):
            compare_reports(baseline, candidate)

    def test_identity_mismatch_raises_typed_exception(self):
        from evolve.compare import QueryIdentityMismatch

        baseline = _make_report([_result("A"), _result("B")])
        candidate = _make_report([_result("B"), _result("A")])
        with pytest.raises(QueryIdentityMismatch, match="identity mismatch"):
            compare_reports(baseline, candidate)

    def test_typed_exception_is_valueerror_subclass(self):
        """Backward compat — existing `except ValueError` callers keep working."""
        from evolve.compare import QueryIdentityMismatch

        assert issubclass(QueryIdentityMismatch, ValueError)


class TestErrorVerdictDetailRows:
    """F2 — error verdicts render bounded error text + result counts."""

    def test_new_error_row_shows_candidate_error_and_counts(self):
        baseline = _make_report([_result("Q", results=3)])
        candidate = _make_report([_result("Q", results=0, error="timeout contacting embedder")])
        table = format_delta_table(compare_reports(baseline, candidate))
        assert "err: timeout contacting embedder" in table
        assert "(results 3 -> 0)" in table

    def test_fixed_error_row_falls_back_to_baseline_error(self):
        baseline = _make_report([_result("Q", results=0, error="db locked")])
        candidate = _make_report([_result("Q", results=2)])
        table = format_delta_table(compare_reports(baseline, candidate))
        assert "err: db locked" in table
        assert "(results 0 -> 2)" in table

    def test_still_errored_row_shows_candidate_error(self):
        baseline = _make_report([_result("Q", results=0, error="old failure")])
        candidate = _make_report([_result("Q", results=0, error="new failure")])
        table = format_delta_table(compare_reports(baseline, candidate))
        assert "err: new failure" in table

    def test_error_text_is_bounded_to_60_chars(self):
        long_error = "x" * 200
        baseline = _make_report([_result("Q", results=1)])
        candidate = _make_report([_result("Q", results=0, error=long_error)])
        table = format_delta_table(compare_reports(baseline, candidate))
        assert "x" * 60 in table
        assert "x" * 61 not in table

    def test_non_error_rows_have_no_detail_line(self):
        """Parity — tables without error verdicts are unchanged (no err: lines)."""
        baseline = _make_report([_result("A"), _result("B", results=0)])
        candidate = _make_report([_result("A"), _result("B", results=2)])
        table = format_delta_table(compare_reports(baseline, candidate))
        assert "err:" not in table


class TestEvolveCompareCLI:
    """F1 — the CLI emits a concise error, not an unhandled traceback."""

    @staticmethod
    def _write_report(path, exp_id, queries):
        import json

        payload = {
            "experiment_id": exp_id,
            "summary": {"query_count": len(queries)},
            "per_query": [
                {"query": q, "results_count": 1, "top_scores": [0.9]} for q in queries
            ],
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _invoke_compare(self, tmp_path, baseline_queries, candidate_queries):
        from click.testing import CliRunner

        import cli as cli_mod

        b = tmp_path / "baseline.json"
        c = tmp_path / "candidate.json"
        self._write_report(b, "base", baseline_queries)
        self._write_report(c, "cand", candidate_queries)
        runner = CliRunner()
        return runner.invoke(cli_mod.evolve_compare, [str(b), str(c)])

    def test_mismatched_reports_exit_1_with_concise_error(self, tmp_path):
        result = self._invoke_compare(tmp_path, ["A", "B", "C"], ["A", "B"])
        assert result.exit_code == 1
        combined = result.output
        try:
            combined += result.stderr
        except (ValueError, AttributeError):
            pass  # click <8.2 with mix_stderr=True folds stderr into output
        assert "Error: Query list length mismatch" in combined
        assert "Traceback" not in combined
        # The failure must surface as a clean exit, not an escaped ValueError.
        assert result.exc_info[0] is SystemExit

    def test_identity_mismatch_exit_1_with_concise_error(self, tmp_path):
        result = self._invoke_compare(tmp_path, ["A", "B"], ["B", "A"])
        assert result.exit_code == 1
        combined = result.output
        try:
            combined += result.stderr
        except (ValueError, AttributeError):
            pass
        assert "Error: Query identity mismatch" in combined
        assert "Traceback" not in combined
        assert result.exc_info[0] is SystemExit

    def test_matching_reports_still_print_delta_table(self, tmp_path):
        result = self._invoke_compare(tmp_path, ["A", "B"], ["A", "B"])
        assert result.exit_code == 0
        assert "baseline:  base" in result.output
        assert "Per-query:" in result.output
