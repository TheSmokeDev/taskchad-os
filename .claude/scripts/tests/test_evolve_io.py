"""Phase 2.3.1 — IO helper tests.

Covers Codex review findings 2 (stale aggregates fail open) and 5
(soft-veto --force lacks durable audit). Both are closed by ``evolve.io``.
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

from evolve.compare import QueryDelta, ReportDelta  # noqa: E402
from evolve.io import load_report_delta, write_decision_artifact  # noqa: E402
from evolve.veto import (  # noqa: E402
    DEFAULT_VETO_RULESET,
    ExitCode,
    evaluate_veto,
)


# ── Helpers ────────────────────────────────────────────────────────────────


def _write_delta_json(path: Path, **overrides) -> Path:
    """Write a delta JSON with sane defaults; overrides clobber per key."""
    data = {
        "baseline_experiment_id": "base",
        "candidate_experiment_id": "cand",
        "hit_rate_delta": 0.0,
        "avg_top_score_delta": 0.0,
        "p50_latency_delta_ms": 0.0,
        "p90_latency_delta_ms": 0.0,
        "tier_distribution_delta": {},
        "verdict_counts": {},
        "per_query": [],
        "baseline_overrides": {},
        "candidate_overrides": {},
        "error_count_delta": 0,
    }
    data.update(overrides)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ── TestLoadReportDelta — Finding 2 coverage ───────────────────────────────


class TestLoadReportDelta:
    def test_recomputes_verdict_counts_from_per_query(self, tmp_path):
        """Stale verdict_counts must NOT bypass the gate."""
        path = _write_delta_json(
            tmp_path / "delta.json",
            verdict_counts={},  # stale/empty
            per_query=[
                {"query": "q1", "verdict": "new_error", "score_delta": 0.0,
                 "error_count_delta": 1},
                {"query": "q2", "verdict": "new_error", "score_delta": 0.0,
                 "error_count_delta": 1},
                {"query": "q3", "verdict": "lost_hit", "score_delta": -0.05},
            ],
        )
        delta = load_report_delta(path)
        assert delta.verdict_counts == {"new_error": 2, "lost_hit": 1}

    def test_recomputes_error_count_delta_from_per_query(self, tmp_path):
        """Stale error_count_delta=0 must not survive when per_query disagrees."""
        path = _write_delta_json(
            tmp_path / "delta.json",
            error_count_delta=0,  # stale
            per_query=[
                {"query": "q", "verdict": "new_error", "score_delta": 0.0,
                 "error_count_delta": 1},
                {"query": "q2", "verdict": "fixed_error", "score_delta": 0.0,
                 "error_count_delta": -1},
                {"query": "q3", "verdict": "new_error", "score_delta": 0.0,
                 "error_count_delta": 1},
            ],
        )
        delta = load_report_delta(path)
        assert delta.error_count_delta == 1  # +1 -1 +1 = 1

    def test_recomputed_counts_fire_hard_veto(self, tmp_path):
        """End-to-end exploit closure: stale aggregates can't bypass new_errors rule."""
        path = _write_delta_json(
            tmp_path / "delta.json",
            verdict_counts={},  # the cached lie
            per_query=[
                {"query": "q1", "verdict": "new_error", "score_delta": 0.0,
                 "error_count_delta": 1},
            ],
        )
        delta = load_report_delta(path)
        v = evaluate_veto(delta, DEFAULT_VETO_RULESET)
        assert v.accepted is False
        assert any(r.rule.name == "new_errors" for r in v.hard_failures)

    def test_rejects_missing_required_fields(self, tmp_path):
        path = tmp_path / "incomplete.json"
        path.write_text(json.dumps({
            "baseline_experiment_id": "b",
            "candidate_experiment_id": "c",
            # missing hit_rate_delta and avg_top_score_delta
        }))
        with pytest.raises(ValueError, match="missing required field"):
            load_report_delta(path)

    def test_rejects_non_dict_root(self, tmp_path):
        path = tmp_path / "wrong.json"
        path.write_text("[1, 2, 3]")
        with pytest.raises(ValueError, match="must be an object"):
            load_report_delta(path)

    def test_empty_per_query_yields_zero_counts(self, tmp_path):
        path = _write_delta_json(tmp_path / "empty.json")
        delta = load_report_delta(path)
        assert delta.verdict_counts == {}
        assert delta.error_count_delta == 0

    def test_round_trip_well_formed_delta(self, tmp_path):
        original = ReportDelta(
            baseline_experiment_id="b",
            candidate_experiment_id="c",
            hit_rate_delta=0.05,
            avg_top_score_delta=0.02,
            p90_latency_delta_ms=10.0,
            per_query=[
                QueryDelta(query="q", verdict="better", score_delta=0.05),
            ],
            verdict_counts={"better": 1},
        )
        path = tmp_path / "rt.json"
        path.write_text(json.dumps(original.to_dict()))
        rebuilt = load_report_delta(path)
        assert rebuilt.hit_rate_delta == 0.05
        assert rebuilt.verdict_counts == {"better": 1}
        assert len(rebuilt.per_query) == 1


# ── TestWriteDecisionArtifact — Finding 5 coverage ─────────────────────────


class TestWriteDecisionArtifact:
    def _make_inputs(self):
        delta = ReportDelta(
            baseline_experiment_id="base",
            candidate_experiment_id="cand-2026-04-25",
            hit_rate_delta=0.0,
            p90_latency_delta_ms=300.0,
        )
        verdict = evaluate_veto(delta, DEFAULT_VETO_RULESET)
        return delta, verdict

    def test_writes_decision_with_all_required_fields(self, tmp_path):
        delta, verdict = self._make_inputs()
        path = write_decision_artifact(
            tmp_path,
            baseline_experiment_id="base",
            candidate_experiment_id="cand-2026-04-25",
            ruleset_name="default",
            delta=delta,
            verdict=verdict,
            force=False,
            exit_code=int(ExitCode.SOFT_VETO),
            overrides={"RECALL_MIN_SCORE": 0.5},
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["baseline_experiment_id"] == "base"
        assert data["candidate_experiment_id"] == "cand-2026-04-25"
        assert data["ruleset"] == "default"
        assert data["force"] is False
        assert data["effective_exit_code"] == int(ExitCode.SOFT_VETO)
        assert data["overrides"] == {"RECALL_MIN_SCORE": 0.5}
        assert "delta" in data and "verdict" in data
        assert "timestamp_utc" in data

    def test_records_force_override_for_audit(self, tmp_path):
        """The whole point of Finding 5: --force must survive on disk."""
        delta, verdict = self._make_inputs()
        assert verdict.soft is True  # precondition
        path = write_decision_artifact(
            tmp_path,
            baseline_experiment_id="base",
            candidate_experiment_id="cand",
            ruleset_name="default",
            delta=delta,
            verdict=verdict,
            force=True,                 # operator overrode soft veto
            exit_code=int(ExitCode.ADOPT),  # process exits 0
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["force"] is True
        assert data["effective_exit_code"] == 0
        # Verdict object still records the soft failure — truth preserved
        assert len(data["verdict"]["soft_failures"]) >= 1

    def test_filename_uses_candidate_experiment_id(self, tmp_path):
        delta, verdict = self._make_inputs()
        path = write_decision_artifact(
            tmp_path,
            baseline_experiment_id="b",
            candidate_experiment_id="exp-20260425T100000Z",
            ruleset_name="default",
            delta=delta,
            verdict=verdict,
            force=False,
            exit_code=0,
        )
        assert path.name == "decision-exp-20260425T100000Z.json"

    def test_creates_missing_output_directory(self, tmp_path):
        delta, verdict = self._make_inputs()
        target = tmp_path / "deep" / "nested" / "path"
        assert not target.exists()
        path = write_decision_artifact(
            target,
            baseline_experiment_id="b",
            candidate_experiment_id="c",
            ruleset_name="default",
            delta=delta,
            verdict=verdict,
            force=False,
            exit_code=0,
        )
        assert path.parent == target
        assert path.exists()

    def test_overrides_can_be_none(self, tmp_path):
        """When --candidate is the input mode, overrides may be unknown."""
        delta, verdict = self._make_inputs()
        path = write_decision_artifact(
            tmp_path,
            baseline_experiment_id="b",
            candidate_experiment_id="c",
            ruleset_name="default",
            delta=delta,
            verdict=verdict,
            force=False,
            exit_code=0,
            overrides=None,
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["overrides"] is None
