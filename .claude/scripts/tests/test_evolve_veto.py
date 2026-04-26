"""Phase 2.3 — veto layer tests.

Covers VetoRule validation, evaluate_veto truth function, preset transforms,
JSON loader + env precedence, exit-code policy, and format_verdict_table.
"""

from __future__ import annotations

import json
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for _p in (_SCRIPTS_DIR, _CHAT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from evolve.compare import QueryDelta, ReportDelta  # noqa: E402
from evolve.veto import (  # noqa: E402
    DEFAULT_VETO_RULESET,
    METRIC_RESOLVERS,
    OPERATORS,
    PERMISSIVE_VETO_RULESET,
    PRESETS,
    STRICT_VETO_RULESET,
    VALID_METRICS,
    VALID_OPS,
    VALID_SEVERITIES,
    ExitCode,
    VetoRule,
    VetoRuleResult,
    VetoRuleset,
    VetoVerdict,
    compute_exit_code,
    evaluate_veto,
    format_verdict_table,
    load_ruleset,
    load_ruleset_from_dict,
    load_ruleset_from_path,
)


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_delta(
    *,
    hit_rate_delta: float = 0.0,
    avg_top_score_delta: float = 0.0,
    p50_latency_delta_ms: float = 0.0,
    p90_latency_delta_ms: float = 0.0,
    error_count_delta: int = 0,
    verdict_counts: dict[str, int] | None = None,
    score_deltas: list[float] | None = None,
) -> ReportDelta:
    """Build a synthetic ReportDelta for one-line test setup."""
    per_query: list[QueryDelta] = []
    if score_deltas is not None:
        for i, d in enumerate(score_deltas):
            verdict = "better" if d > 0 else "worse" if d < 0 else "same"
            per_query.append(QueryDelta(query=f"q{i}", verdict=verdict, score_delta=d))
    return ReportDelta(
        baseline_experiment_id="base",
        candidate_experiment_id="cand",
        hit_rate_delta=hit_rate_delta,
        avg_top_score_delta=avg_top_score_delta,
        p50_latency_delta_ms=p50_latency_delta_ms,
        p90_latency_delta_ms=p90_latency_delta_ms,
        error_count_delta=error_count_delta,
        verdict_counts=verdict_counts or {},
        per_query=per_query,
    )


def _rule(**kwargs) -> VetoRule:
    """Build a VetoRule with sensible test defaults."""
    return VetoRule(
        name=kwargs.pop("name", "test"),
        metric=kwargs.pop("metric", "hit_rate_delta"),
        op=kwargs.pop("op", "lt"),
        threshold=kwargs.pop("threshold", -0.05),
        severity=kwargs.pop("severity", "hard"),
        message=kwargs.pop("message", ""),
    )


# ── TestVetoRule ───────────────────────────────────────────────────────────


class TestVetoRule:
    def test_rule_is_frozen(self):
        r = _rule()
        with pytest.raises(FrozenInstanceError):
            r.threshold = -0.10  # type: ignore[misc]

    def test_invalid_metric_raises(self):
        with pytest.raises(ValueError, match="unknown metric"):
            _rule(metric="bogus")

    def test_invalid_op_raises(self):
        with pytest.raises(ValueError, match="unknown op"):
            _rule(op="contains")

    def test_invalid_severity_raises(self):
        with pytest.raises(ValueError, match="unknown severity"):
            _rule(severity="critical")

    def test_to_dict_round_trips(self):
        r = _rule(message="hello {value}")
        d = r.to_dict()
        assert d["metric"] == "hit_rate_delta"
        assert d["message"] == "hello {value}"
        assert d["severity"] == "hard"

    # --- 2.3.1 hardening (Codex Finding 3): non-finite thresholds disable the gate

    def test_nan_threshold_rejected(self):
        with pytest.raises(ValueError, match="finite"):
            _rule(threshold=float("nan"))

    def test_positive_inf_threshold_rejected(self):
        with pytest.raises(ValueError, match="finite"):
            _rule(threshold=float("inf"))

    def test_negative_inf_threshold_rejected(self):
        with pytest.raises(ValueError, match="finite"):
            _rule(threshold=float("-inf"))


# ── TestEvaluateVeto ───────────────────────────────────────────────────────


class TestEvaluateVeto:
    def test_all_pass_neutral_delta(self):
        delta = _make_delta()
        v = evaluate_veto(delta, DEFAULT_VETO_RULESET)
        assert v.accepted is True
        assert v.soft is False
        assert len(v.passed) == 6
        assert len(v.hard_failures) == 0
        assert len(v.soft_failures) == 0

    def test_one_hard_failure_blocks(self):
        delta = _make_delta(hit_rate_delta=-0.10)
        v = evaluate_veto(delta, DEFAULT_VETO_RULESET)
        assert v.accepted is False
        assert v.soft is False
        assert len(v.hard_failures) == 1
        assert v.hard_failures[0].rule.name == "hit_rate_regression"

    def test_only_soft_failure_is_soft_veto(self):
        delta = _make_delta(p90_latency_delta_ms=300.0)
        v = evaluate_veto(delta, DEFAULT_VETO_RULESET)
        assert v.accepted is False
        assert v.soft is True
        assert len(v.soft_failures) == 1
        assert v.soft_failures[0].rule.name == "latency_regression"

    def test_mixed_hard_soft_is_hard_not_soft(self):
        delta = _make_delta(hit_rate_delta=-0.10, p90_latency_delta_ms=300.0)
        v = evaluate_veto(delta, DEFAULT_VETO_RULESET)
        assert v.accepted is False
        assert v.soft is False  # hard wins over soft
        assert len(v.hard_failures) == 1
        assert len(v.soft_failures) == 1

    def test_lost_hits_two_blocks(self):
        delta = _make_delta(verdict_counts={"lost_hit": 2})
        v = evaluate_veto(delta, DEFAULT_VETO_RULESET)
        names = [r.rule.name for r in v.hard_failures]
        assert "lost_hits" in names

    def test_lost_hits_one_passes_default(self):
        delta = _make_delta(verdict_counts={"lost_hit": 1})
        v = evaluate_veto(delta, DEFAULT_VETO_RULESET)
        names = [r.rule.name for r in v.hard_failures]
        assert "lost_hits" not in names

    def test_new_errors_any_blocks(self):
        delta = _make_delta(verdict_counts={"new_error": 1})
        v = evaluate_veto(delta, DEFAULT_VETO_RULESET)
        names = [r.rule.name for r in v.hard_failures]
        assert "new_errors" in names

    def test_worst_query_score_with_empty_per_query_passes(self):
        delta = _make_delta()  # no per_query
        v = evaluate_veto(delta, DEFAULT_VETO_RULESET)
        names = [r.rule.name for r in v.soft_failures]
        assert "worst_query_regression" not in names

    def test_worst_query_score_picks_min(self):
        delta = _make_delta(score_deltas=[0.05, -0.20, 0.10])
        v = evaluate_veto(delta, DEFAULT_VETO_RULESET)
        names = [r.rule.name for r in v.soft_failures]
        assert "worst_query_regression" in names

    def test_evaluate_is_pure(self):
        delta = _make_delta(hit_rate_delta=-0.10)
        v1 = evaluate_veto(delta, DEFAULT_VETO_RULESET)
        v2 = evaluate_veto(delta, DEFAULT_VETO_RULESET)
        assert v1.to_dict() == v2.to_dict()

    def test_regression_summary_with_no_failures_still_accepts(self):
        """Phase 2.6: passing a clean regression_summary (no failures) leaves
        the verdict unchanged. The `Failed list empty -> no impact` invariant
        is what makes the regression check additive rather than blocking by
        default."""
        from evolve.regression import RegressionSummary

        delta = _make_delta()
        clean = RegressionSummary(total=5, passed=5, failed=[])
        v = evaluate_veto(delta, DEFAULT_VETO_RULESET, regression_summary=clean)
        assert v.accepted
        assert v.regression_failures == []

    def test_regression_summary_with_failures_forces_hard_veto(self):
        """Phase 2.6: any RegressionFailure in `failed` becomes a hard veto.
        Even a perfectly clean delta cannot be adopted if a known-fixed bug
        regressed. --force does NOT flip this — verified separately by
        compute_exit_code's HARD_VETO branch."""
        from evolve.regression import (
            RegressionEntry,
            RegressionFailure,
            RegressionSummary,
        )

        delta = _make_delta()  # Clean delta — would be ADOPT without regressions
        entry = RegressionEntry(
            query="evolve replay harness phase 2",
            fixed_in="PR #21 — None-sentinel defaults",
            expected_top_path="vault/memory/MEMORY.md",
            min_top_score=0.45,
        )
        summary = RegressionSummary(
            total=1,
            passed=0,
            failed=[
                RegressionFailure(
                    entry=entry,
                    observed_top_score=0.30,
                    observed_top_path="vault/memory/MEMORY.md",
                    reason="below_min_score",
                )
            ],
        )
        v = evaluate_veto(delta, DEFAULT_VETO_RULESET, regression_summary=summary)

        assert not v.accepted
        # Critical: regression failures disqualify the SOFT path too —
        # `--force` cannot rescue a candidate that re-introduces a known bug.
        assert not v.soft
        assert len(v.regression_failures) == 1
        assert v.regression_failures[0].reason == "below_min_score"

    def test_regression_summary_none_still_accepted(self):
        """The default kwarg path must remain pure — None means "not invoked"."""
        delta = _make_delta()
        v = evaluate_veto(delta, DEFAULT_VETO_RULESET, regression_summary=None)
        assert v.accepted
        assert v.regression_failures == []

    def test_to_dict_serializes(self):
        delta = _make_delta(hit_rate_delta=-0.10)
        v = evaluate_veto(delta, DEFAULT_VETO_RULESET)
        # Round-trip through json — guards against unserializable shapes
        s = json.dumps(v.to_dict())
        assert "hit_rate_regression" in s
        assert "hard_failures" in s

    def test_message_template_keys_render(self):
        delta = _make_delta(hit_rate_delta=-0.10)
        v = evaluate_veto(delta, DEFAULT_VETO_RULESET)
        s = format_verdict_table(v, ruleset_name="default")
        assert "-0.1000" in s  # value rendered via {value:+.4f}


# ── TestOperators ──────────────────────────────────────────────────────────


class TestOperators:
    def test_lt_passes_when_value_above_threshold(self):
        assert OPERATORS["lt"](0.0, -0.05) is True

    def test_lt_fails_when_value_below_threshold(self):
        assert OPERATORS["lt"](-0.10, -0.05) is False

    def test_gt_passes_when_value_below_threshold(self):
        assert OPERATORS["gt"](100.0, 200.0) is True

    def test_gt_fails_when_value_above_threshold(self):
        assert OPERATORS["gt"](300.0, 200.0) is False

    def test_eq_fails_on_match(self):
        assert OPERATORS["eq"](5.0, 5.0) is False

    def test_eq_passes_on_mismatch(self):
        assert OPERATORS["eq"](6.0, 5.0) is True

    def test_abs_gt_triggers_on_either_side(self):
        assert OPERATORS["abs_gt"](-3.0, 1.0) is False
        assert OPERATORS["abs_gt"](3.0, 1.0) is False
        assert OPERATORS["abs_gt"](0.5, 1.0) is True


# ── TestMetricResolvers ────────────────────────────────────────────────────


class TestMetricResolvers:
    def test_all_valid_metrics_have_resolvers(self):
        for m in VALID_METRICS:
            assert m in METRIC_RESOLVERS

    def test_direct_field_resolver(self):
        delta = _make_delta(hit_rate_delta=-0.07)
        assert METRIC_RESOLVERS["hit_rate_delta"](delta) == pytest.approx(-0.07)

    def test_worst_query_score_resolver(self):
        delta = _make_delta(score_deltas=[0.1, -0.3, 0.2])
        assert METRIC_RESOLVERS["worst_query_score_delta"](delta) == pytest.approx(-0.3)

    def test_worst_query_score_empty_returns_zero(self):
        delta = _make_delta()
        assert METRIC_RESOLVERS["worst_query_score_delta"](delta) == 0.0

    def test_verdict_count_resolver(self):
        delta = _make_delta(verdict_counts={"new_error": 3, "lost_hit": 5})
        assert METRIC_RESOLVERS["new_error_count"](delta) == 3.0
        assert METRIC_RESOLVERS["lost_hit_count"](delta) == 5.0


# ── TestPresets ────────────────────────────────────────────────────────────


class TestPresets:
    def test_default_has_six_rules(self):
        assert len(DEFAULT_VETO_RULESET.rules) == 6
        assert DEFAULT_VETO_RULESET.name == "default"

    def test_strict_promotes_all_soft_to_hard(self):
        for r in STRICT_VETO_RULESET.rules:
            assert r.severity == "hard", f"{r.name} should be hard under strict"

    def test_strict_tightens_thresholds_2x(self):
        rules = {r.name: r for r in STRICT_VETO_RULESET.rules}
        assert rules["hit_rate_regression"].threshold == pytest.approx(-0.025)
        assert rules["score_regression"].threshold == pytest.approx(-0.015)
        assert rules["latency_regression"].threshold == pytest.approx(100.0)
        assert rules["worst_query_regression"].threshold == pytest.approx(-0.075)

    def test_strict_lost_hits_becomes_zero(self):
        rules = {r.name: r for r in STRICT_VETO_RULESET.rules}
        assert rules["lost_hits"].threshold == 0.0

    def test_strict_new_errors_unchanged(self):
        rules = {r.name: r for r in STRICT_VETO_RULESET.rules}
        assert rules["new_errors"].threshold == 0.0  # already binary

    def test_permissive_all_soft(self):
        for r in PERMISSIVE_VETO_RULESET.rules:
            assert r.severity == "soft", f"{r.name} should be soft in permissive"

    def test_permissive_thresholds_unchanged_from_default(self):
        default = {r.name: r.threshold for r in DEFAULT_VETO_RULESET.rules}
        permissive = {r.name: r.threshold for r in PERMISSIVE_VETO_RULESET.rules}
        assert default == permissive

    def test_permissive_never_hard_blocks(self):
        delta = _make_delta(hit_rate_delta=-0.50, verdict_counts={"new_error": 5})
        v = evaluate_veto(delta, PERMISSIVE_VETO_RULESET)
        assert v.accepted is False
        assert v.soft is True
        assert len(v.hard_failures) == 0

    def test_strict_blocks_smaller_regression_than_default(self):
        # hit_rate_delta = -0.03 passes default (>-0.05) but fails strict (<-0.025)
        delta = _make_delta(hit_rate_delta=-0.03)
        default_v = evaluate_veto(delta, DEFAULT_VETO_RULESET)
        strict_v = evaluate_veto(delta, STRICT_VETO_RULESET)
        assert default_v.accepted is True
        assert strict_v.accepted is False

    def test_presets_dict_exposes_all(self):
        assert set(PRESETS) == {"default", "strict", "permissive"}

    def test_rulesets_are_frozen(self):
        with pytest.raises(FrozenInstanceError):
            DEFAULT_VETO_RULESET.name = "mutated"  # type: ignore[misc]


# ── TestRulesetLoader ──────────────────────────────────────────────────────


class TestRulesetLoader:
    def test_load_default_when_no_arg(self):
        rs = load_ruleset(env={})
        assert rs.name == "default"

    def test_load_preset_by_name(self):
        rs = load_ruleset("strict", env={})
        assert rs.name == "strict"

    def test_env_var_preset(self):
        rs = load_ruleset(env={"EVOLVE_VETO_RULESET": "permissive"})
        assert rs.name == "permissive"

    def test_invalid_env_preset_raises(self):
        with pytest.raises(ValueError, match="EVOLVE_VETO_RULESET"):
            load_ruleset(env={"EVOLVE_VETO_RULESET": "ultra-strict"})

    def test_arg_overrides_env_preset(self, tmp_path):
        custom = tmp_path / "custom.json"
        custom.write_text(
            json.dumps(
                {
                    "name": "custom",
                    "rules": [
                        {
                            "name": "weird",
                            "metric": "hit_rate_delta",
                            "op": "lt",
                            "threshold": -0.99,
                            "severity": "soft",
                        }
                    ],
                }
            )
        )
        rs = load_ruleset(custom, env={"EVOLVE_VETO_RULESET": "strict"})
        assert rs.name == "custom"
        assert len(rs.rules) == 1

    def test_env_path_overrides_env_preset(self, tmp_path):
        custom = tmp_path / "custom.json"
        # 2.3.1: empty rulesets are now rejected; use a one-rule ruleset
        # to verify env-path precedence without tripping the empty guard.
        custom.write_text(
            json.dumps(
                {
                    "name": "from_path",
                    "rules": [
                        {
                            "name": "marker",
                            "metric": "hit_rate_delta",
                            "op": "lt",
                            "threshold": -0.99,
                            "severity": "soft",
                        }
                    ],
                }
            )
        )
        rs = load_ruleset(
            env={
                "EVOLVE_VETO_RULES_PATH": str(custom),
                "EVOLVE_VETO_RULESET": "strict",
            }
        )
        assert rs.name == "from_path"

    def test_load_invalid_json_reports_line(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{\n  invalid json\n}")
        with pytest.raises(ValueError, match=r"line \d+"):
            load_ruleset_from_path(bad)

    def test_load_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_ruleset_from_path(tmp_path / "nope.json")

    def test_load_missing_required_key_reports_index(self):
        with pytest.raises(ValueError, match=r"rules\[0\] missing required key"):
            load_ruleset_from_dict(
                {
                    "rules": [
                        {
                            "name": "x",
                            "metric": "hit_rate_delta",
                            "op": "lt",
                            "threshold": -0.05,
                            # missing severity
                        }
                    ]
                }
            )

    def test_load_bad_metric_value_reports_index(self):
        with pytest.raises(ValueError, match=r"rules\[0\] invalid"):
            load_ruleset_from_dict(
                {
                    "rules": [
                        {
                            "name": "x",
                            "metric": "bogus",
                            "op": "lt",
                            "threshold": -0.05,
                            "severity": "hard",
                        }
                    ]
                }
            )

    def test_load_non_object_root_raises(self):
        with pytest.raises(ValueError, match="ruleset must be a JSON object"):
            load_ruleset_from_dict("not a dict")  # type: ignore[arg-type]

    def test_load_non_array_rules_raises(self):
        with pytest.raises(ValueError, match="rules must be a JSON array"):
            load_ruleset_from_dict({"rules": "not an array"})  # type: ignore[dict-item]

    def test_to_dict_then_from_dict_round_trips(self):
        original = DEFAULT_VETO_RULESET
        rebuilt = load_ruleset_from_dict(original.to_dict())
        assert rebuilt.name == original.name
        assert len(rebuilt.rules) == len(original.rules)
        for a, b in zip(rebuilt.rules, original.rules):
            assert a == b

    # --- 2.3.1 hardening (Codex Finding 3): empty rulesets disable the gate

    def test_empty_ruleset_rejected(self):
        with pytest.raises(ValueError, match="at least one rule"):
            load_ruleset_from_dict({"rules": []})

    def test_empty_ruleset_via_path_rejected(self, tmp_path):
        bad = tmp_path / "empty.json"
        bad.write_text(json.dumps({"name": "empty", "rules": []}))
        with pytest.raises(ValueError, match="at least one rule"):
            load_ruleset_from_path(bad)

    # --- 2.3.1 hardening (Codex Finding 3): JSON NaN / Infinity rejected

    def test_json_nan_rejected_by_path_loader(self, tmp_path):
        bad = tmp_path / "nan.json"
        bad.write_text(
            '{"name":"x","rules":[{"name":"r","metric":"hit_rate_delta",'
            '"op":"lt","threshold":NaN,"severity":"hard"}]}'
        )
        with pytest.raises(ValueError, match="non-finite"):
            load_ruleset_from_path(bad)

    def test_json_infinity_rejected_by_path_loader(self, tmp_path):
        bad = tmp_path / "inf.json"
        bad.write_text(
            '{"name":"x","rules":[{"name":"r","metric":"p90_latency_delta_ms",'
            '"op":"gt","threshold":Infinity,"severity":"hard"}]}'
        )
        with pytest.raises(ValueError, match="non-finite"):
            load_ruleset_from_path(bad)


# ── TestExitCodePolicy ─────────────────────────────────────────────────────


class TestExitCodePolicy:
    def test_adopt_returns_zero(self):
        v = VetoVerdict(accepted=True, soft=False)
        assert compute_exit_code(v, force=False) == ExitCode.ADOPT
        assert compute_exit_code(v, force=True) == ExitCode.ADOPT

    def test_hard_returns_one_even_with_force(self):
        v = VetoVerdict(accepted=False, soft=False)
        assert compute_exit_code(v, force=False) == ExitCode.HARD_VETO
        assert compute_exit_code(v, force=True) == ExitCode.HARD_VETO

    def test_soft_returns_two_without_force(self):
        v = VetoVerdict(accepted=False, soft=True)
        assert compute_exit_code(v, force=False) == ExitCode.SOFT_VETO

    def test_force_flips_soft_to_zero(self):
        v = VetoVerdict(accepted=False, soft=True)
        assert compute_exit_code(v, force=True) == ExitCode.ADOPT

    def test_force_does_not_mutate_verdict(self):
        delta = _make_delta(p90_latency_delta_ms=300.0)
        v = evaluate_veto(delta, DEFAULT_VETO_RULESET)
        before = v.to_dict()
        compute_exit_code(v, force=True)
        assert v.to_dict() == before  # CLI policy must not mutate the truth record


# ── TestExitCodeEnum ───────────────────────────────────────────────────────


class TestExitCodeEnum:
    def test_values(self):
        assert int(ExitCode.ADOPT) == 0
        assert int(ExitCode.HARD_VETO) == 1
        assert int(ExitCode.SOFT_VETO) == 2
        assert int(ExitCode.ERROR) == 3


# ── TestFormatVerdictTable ─────────────────────────────────────────────────


class TestFormatVerdictTable:
    def test_adopt_label(self):
        v = VetoVerdict(accepted=True, soft=False)
        s = format_verdict_table(v, ruleset_name="default")
        assert "ADOPT" in s
        assert "default" in s

    def test_reject_label_on_hard(self):
        delta = _make_delta(hit_rate_delta=-0.10)
        v = evaluate_veto(delta, DEFAULT_VETO_RULESET)
        s = format_verdict_table(v, ruleset_name="default")
        assert "REJECT" in s
        assert "hit_rate_regression" in s

    def test_review_label_on_soft(self):
        delta = _make_delta(p90_latency_delta_ms=300.0)
        v = evaluate_veto(delta, DEFAULT_VETO_RULESET)
        s = format_verdict_table(v, ruleset_name="default")
        assert "REVIEW" in s
        assert "latency_regression" in s

    def test_severity_icons_present(self):
        delta = _make_delta(hit_rate_delta=-0.10, p90_latency_delta_ms=300.0)
        v = evaluate_veto(delta, DEFAULT_VETO_RULESET)
        s = format_verdict_table(v, ruleset_name="default")
        assert "[hard]" in s
        assert "[soft]" in s


# ── Validation enums sanity ────────────────────────────────────────────────


class TestValidationEnums:
    def test_valid_metrics_aligned_with_resolvers(self):
        assert set(VALID_METRICS) == set(METRIC_RESOLVERS.keys())

    def test_valid_ops_aligned_with_operators(self):
        assert set(VALID_OPS) == set(OPERATORS.keys())

    def test_valid_severities(self):
        assert set(VALID_SEVERITIES) == {"hard", "soft"}
