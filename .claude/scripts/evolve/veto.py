"""Phase 2.3 — veto thresholds for autonomous adoption.

Pure stdlib evaluator. Given a ReportDelta and a VetoRuleset, returns a
VetoVerdict with hard/soft failure breakdown. The CLI maps the verdict to
exit codes and applies --force at the policy layer; this module never
decides whether to ship — it only reports the truth.

Design language:
- METRIC_RESOLVERS (dict) — every metric lookup goes through one dispatcher.
  Direct fields read straight from ReportDelta; derived metrics compute from
  per_query / verdict_counts. Adding a metric = one dict entry.
- OPERATORS (dict) — every comparison goes through one dispatcher. Returns
  True when the rule *passes* (i.e. the failure condition is NOT met).
- Presets are transforms over DEFAULT_VETO_RULESET — strict tightens, permissive
  flips severity. One source of truth (default), two policy lenses.
- VetoVerdict.to_dict() is JSON-serialisable next to ReportDelta.to_dict()
  for an audit-trail that survives the run.

Phase 2.6 seam: evaluate_veto accepts an optional regression_summary kwarg.
When 2.6 ships regression_queries.json, propose() will compute a
RegressionSummary and pass it; veto adds a hard rule that any below-threshold
regression query is a blocker. Today the kwarg is documented but unused — no
stub code, just a forward-compatible signature.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field, replace
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable

from evolve.compare import ReportDelta


# ── Exit codes ─────────────────────────────────────────────────────────────


class ExitCode(IntEnum):
    """CLI exit-code contract for `evolve veto` / `evolve propose`."""

    ADOPT = 0
    HARD_VETO = 1
    SOFT_VETO = 2
    ERROR = 3


# ── Validation enums ───────────────────────────────────────────────────────


VALID_METRICS: tuple[str, ...] = (
    "hit_rate_delta",
    "avg_top_score_delta",
    "p50_latency_delta_ms",
    "p90_latency_delta_ms",
    "error_count_delta",
    "worst_query_score_delta",
    "new_error_count",
    "lost_hit_count",
)
VALID_OPS: tuple[str, ...] = ("lt", "gt", "eq", "abs_gt")
VALID_SEVERITIES: tuple[str, ...] = ("hard", "soft")


# ── Dataclasses ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class VetoRule:
    """A single threshold rule against one metric.

    `op` describes the *failure* condition: ``lt -0.05`` means "fails if
    value < -0.05". ``gt 200`` means "fails if value > 200". ``eq`` is exact
    match; ``abs_gt`` triggers when |value| exceeds threshold.
    ``severity="hard"`` blocks adoption; "soft" requires human review.
    """

    name: str
    metric: str
    op: str
    threshold: float
    severity: str
    message: str = ""

    def __post_init__(self) -> None:
        if self.metric not in VALID_METRICS:
            raise ValueError(
                f"unknown metric {self.metric!r}; must be one of {VALID_METRICS}"
            )
        if self.op not in VALID_OPS:
            raise ValueError(
                f"unknown op {self.op!r}; must be one of {VALID_OPS}"
            )
        if self.severity not in VALID_SEVERITIES:
            raise ValueError(
                f"unknown severity {self.severity!r}; must be one of {VALID_SEVERITIES}"
            )
        # Non-finite thresholds turn the gate into a no-op: NaN comparisons
        # always return False, so `not (value < NaN)` is always True (passes).
        # Reject at construction so neither programmatic nor JSON paths can
        # inject a dead rule. Codex review (2026-04-25) Finding 3.
        if not math.isfinite(self.threshold):
            raise ValueError(
                f"threshold must be a finite number, got {self.threshold!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VetoRuleset:
    """A named bundle of rules.

    Frozen + tuple-typed `rules` keeps presets immutable: tests that mutate
    a ruleset must build a new one. No accidental cross-test contamination.
    """

    rules: tuple[VetoRule, ...]
    name: str = "default"

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "rules": [r.to_dict() for r in self.rules]}


@dataclass
class VetoRuleResult:
    """Result of evaluating one rule against one delta."""

    rule: VetoRule
    value: float
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule.to_dict(),
            "value": self.value,
            "passed": self.passed,
        }


@dataclass
class VetoVerdict:
    """Three-state verdict.

    ``accepted=True`` only when every rule passes AND no regression-corpus
    failures occurred (Phase 2.6).
    ``accepted=False, soft=False`` is hard veto (blocker — rule hard-failures
    or regression-corpus failures present).
    ``accepted=False, soft=True`` is soft veto (review required).
    The CLI maps these to exit codes 0/1/2 respectively.

    --force is a CLI policy and does NOT mutate this verdict — the truth
    record stays honest even when an operator overrides the gate.
    Regression-corpus failures are NEVER softenable: --force does not
    flip them; ``compute_exit_code`` only flips soft vetoes.
    """

    accepted: bool
    soft: bool
    hard_failures: list[VetoRuleResult] = field(default_factory=list)
    soft_failures: list[VetoRuleResult] = field(default_factory=list)
    passed: list[VetoRuleResult] = field(default_factory=list)
    # Phase 2.6: regression-corpus failures live alongside rule failures.
    # Keeping them in a separate field (not synthesized into VetoRuleResult)
    # avoids polluting VALID_METRICS with a pseudo-metric and preserves
    # the full RegressionFailure context (entry, observed_top_path, reason)
    # for the audit artifact.
    regression_failures: list[Any] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "soft": self.soft,
            "hard_failures": [r.to_dict() for r in self.hard_failures],
            "soft_failures": [r.to_dict() for r in self.soft_failures],
            "passed": [r.to_dict() for r in self.passed],
            "regression_failures": [
                f.to_dict() if hasattr(f, "to_dict") else f
                for f in self.regression_failures
            ],
        }


# ── Metric resolvers ───────────────────────────────────────────────────────
# All metric reads dispatch through this dict. Direct fields read straight
# from ReportDelta; derived metrics compute from per_query / verdict_counts.
# Adding a metric = one dict entry. No branching in evaluate_veto.


def _direct_field(name: str) -> Callable[[ReportDelta], float]:
    """Build a resolver that reads ``name`` off ReportDelta as float."""

    def resolver(delta: ReportDelta) -> float:
        return float(getattr(delta, name))

    return resolver


def _worst_query_score(delta: ReportDelta) -> float:
    """Min score_delta across per_query, 0.0 when empty."""
    if not delta.per_query:
        return 0.0
    return float(min(q.score_delta for q in delta.per_query))


def _verdict_count(verdict_label: str) -> Callable[[ReportDelta], float]:
    """Build a resolver that counts per_query verdicts of a given label."""

    def resolver(delta: ReportDelta) -> float:
        return float(delta.verdict_counts.get(verdict_label, 0))

    return resolver


METRIC_RESOLVERS: dict[str, Callable[[ReportDelta], float]] = {
    "hit_rate_delta": _direct_field("hit_rate_delta"),
    "avg_top_score_delta": _direct_field("avg_top_score_delta"),
    "p50_latency_delta_ms": _direct_field("p50_latency_delta_ms"),
    "p90_latency_delta_ms": _direct_field("p90_latency_delta_ms"),
    "error_count_delta": _direct_field("error_count_delta"),
    "worst_query_score_delta": _worst_query_score,
    "new_error_count": _verdict_count("new_error"),
    "lost_hit_count": _verdict_count("lost_hit"),
}


# ── Operators ──────────────────────────────────────────────────────────────
# `op` describes the *failure* condition; OPERATORS returns True when the
# rule *passes* (failure condition NOT met). One inversion at one place
# beats negations scattered through evaluate_veto.


OPERATORS: dict[str, Callable[[float, float], bool]] = {
    "lt": lambda value, threshold: not (value < threshold),
    "gt": lambda value, threshold: not (value > threshold),
    "eq": lambda value, threshold: not (value == threshold),
    "abs_gt": lambda value, threshold: not (abs(value) > threshold),
}


def _resolve_metric(delta: ReportDelta, metric: str) -> float:
    resolver = METRIC_RESOLVERS.get(metric)
    if resolver is None:
        raise ValueError(
            f"no resolver for metric {metric!r}; valid: {tuple(METRIC_RESOLVERS)}"
        )
    return resolver(delta)


def _check_rule(delta: ReportDelta, rule: VetoRule) -> VetoRuleResult:
    value = _resolve_metric(delta, rule.metric)
    op = OPERATORS.get(rule.op)
    if op is None:
        raise ValueError(f"unknown op {rule.op!r}; valid: {tuple(OPERATORS)}")
    return VetoRuleResult(rule=rule, value=value, passed=op(value, rule.threshold))


# ── Pure verdict function ──────────────────────────────────────────────────


def compute_exit_code(verdict: "VetoVerdict", force: bool = False) -> ExitCode:
    """Verdict + --force policy → exit code.

    --force flips soft veto to ADOPT (still records the failures in the
    verdict). Hard veto is never overridable; --force is ignored.
    """
    if verdict.accepted:
        return ExitCode.ADOPT
    if verdict.soft and force:
        return ExitCode.ADOPT
    if verdict.soft:
        return ExitCode.SOFT_VETO
    return ExitCode.HARD_VETO


def evaluate_veto(
    delta: ReportDelta,
    ruleset: VetoRuleset,
    *,
    regression_summary: Any | None = None,
) -> VetoVerdict:
    """Evaluate every rule in `ruleset` against `delta`. Pure function.

    No I/O, no clock reads, no randomness. Same input → same verdict.

    Phase 2.6: ``regression_summary`` (an ``evolve.regression.RegressionSummary``)
    carries per-query verdicts from the regression corpus. Any entry in
    ``regression_summary.failed`` becomes a hard failure on the verdict;
    regression failures are NEVER softenable (no ``severity="soft"`` path)
    — the regression corpus is the structural floor that --force cannot
    override. Replaces the Phase 2.3 NotImplementedError stub at this
    line in commit 22be531 (Codex review Finding 4 sealed the seam).
    """
    hard_failures: list[VetoRuleResult] = []
    soft_failures: list[VetoRuleResult] = []
    passed: list[VetoRuleResult] = []

    for rule in ruleset.rules:
        result = _check_rule(delta, rule)
        if result.passed:
            passed.append(result)
        elif rule.severity == "hard":
            hard_failures.append(result)
        else:
            soft_failures.append(result)

    regression_failures: list[Any] = []
    if regression_summary is not None and getattr(regression_summary, "failed", None):
        regression_failures = list(regression_summary.failed)

    # Regression failures are always hard. They poison ``accepted`` AND
    # disqualify the soft path — a candidate that breaks a known-fixed
    # bug cannot be approved through review either.
    accepted = (
        not hard_failures
        and not soft_failures
        and not regression_failures
    )
    soft = (
        bool(soft_failures)
        and not hard_failures
        and not regression_failures
    )
    return VetoVerdict(
        accepted=accepted,
        soft=soft,
        hard_failures=hard_failures,
        soft_failures=soft_failures,
        passed=passed,
        regression_failures=regression_failures,
    )


# ── Default + preset rulesets ──────────────────────────────────────────────


DEFAULT_VETO_RULESET = VetoRuleset(
    name="default",
    rules=(
        VetoRule(
            name="hit_rate_regression",
            metric="hit_rate_delta",
            op="lt",
            threshold=-0.05,
            severity="hard",
            message="hit_rate dropped {value:+.4f} (threshold {threshold:+.4f})",
        ),
        VetoRule(
            name="score_regression",
            metric="avg_top_score_delta",
            op="lt",
            threshold=-0.03,
            severity="hard",
            message="avg_top_score dropped {value:+.4f} (threshold {threshold:+.4f})",
        ),
        VetoRule(
            name="new_errors",
            metric="new_error_count",
            op="gt",
            threshold=0.0,
            severity="hard",
            message="{value:.0f} new error(s) introduced",
        ),
        VetoRule(
            name="lost_hits",
            metric="lost_hit_count",
            op="gt",
            threshold=1.0,
            severity="hard",
            message="{value:.0f} hits lost (threshold {threshold:.0f})",
        ),
        VetoRule(
            name="latency_regression",
            metric="p90_latency_delta_ms",
            op="gt",
            threshold=200.0,
            severity="soft",
            message="p90 latency rose {value:+.1f}ms (threshold +{threshold:.0f}ms)",
        ),
        VetoRule(
            name="worst_query_regression",
            metric="worst_query_score_delta",
            op="lt",
            threshold=-0.15,
            severity="soft",
            message="worst query collapsed {value:+.4f} (threshold {threshold:+.4f})",
        ),
    ),
)


def _build_strict() -> VetoRuleset:
    """Strict: tighten thresholds 2x; promote latency + worst-query to hard.

    `new_errors` is already binary (gt 0); leave unchanged.
    `lost_hits` tightens from gt 1 to gt 0 (one lost hit becomes a blocker).
    All other thresholds halve, soft severities promote to hard.
    """
    transformed: list[VetoRule] = []
    for r in DEFAULT_VETO_RULESET.rules:
        if r.name == "new_errors":
            transformed.append(r)
            continue
        if r.name == "lost_hits":
            transformed.append(replace(r, threshold=0.0))
            continue
        new_threshold = r.threshold / 2
        new_severity = "hard" if r.severity == "soft" else r.severity
        transformed.append(replace(r, threshold=new_threshold, severity=new_severity))
    return VetoRuleset(name="strict", rules=tuple(transformed))


def _build_permissive() -> VetoRuleset:
    """Permissive: severity-flip-only — every rule becomes soft.

    Thresholds unchanged. Information value preserved (every deviation
    surfaces as a soft veto for review). The hard veto path becomes
    unreachable; --force is unnecessary against this preset.
    """
    return VetoRuleset(
        name="permissive",
        rules=tuple(replace(r, severity="soft") for r in DEFAULT_VETO_RULESET.rules),
    )


STRICT_VETO_RULESET = _build_strict()
PERMISSIVE_VETO_RULESET = _build_permissive()


PRESETS: dict[str, VetoRuleset] = {
    "default": DEFAULT_VETO_RULESET,
    "strict": STRICT_VETO_RULESET,
    "permissive": PERMISSIVE_VETO_RULESET,
}


# ── Loader (precedence: arg > EVOLVE_VETO_RULES_PATH > EVOLVE_VETO_RULESET > default) ─


def _validate_ruleset_dict(data: Any) -> None:
    """Hand-rolled schema check — keeps the module stdlib-pure."""
    if not isinstance(data, dict):
        raise ValueError(f"ruleset must be a JSON object, got {type(data).__name__}")
    rules = data.get("rules")
    if not isinstance(rules, list):
        raise ValueError(
            f"ruleset.rules must be a JSON array, got {type(rules).__name__}"
        )
    # An empty ruleset is a fail-open: evaluate_veto returns accepted=True for
    # any delta. Reject so a custom rules file can never disable the gate.
    # Codex review (2026-04-25) Finding 3.
    if len(rules) == 0:
        raise ValueError(
            "ruleset.rules must contain at least one rule; "
            "an empty ruleset would silently disable the gate"
        )
    for i, r in enumerate(rules):
        if not isinstance(r, dict):
            raise ValueError(
                f"rules[{i}] must be a JSON object, got {type(r).__name__}"
            )
        for required in ("name", "metric", "op", "threshold", "severity"):
            if required not in r:
                raise ValueError(f"rules[{i}] missing required key {required!r}")


def _reject_nonfinite_json_constant(constant: str) -> float:
    """parse_constant hook for json.loads — rejects NaN/Infinity tokens.

    Python's json module accepts non-standard JSON `NaN`/`Infinity` by default.
    Combined with `not (value < NaN)` always being True, that lets a custom
    rules file disable the gate. We refuse them at parse time.
    Codex review (2026-04-25) Finding 3.
    """
    raise ValueError(
        f"non-finite JSON value {constant!r} is not allowed in rulesets; "
        f"thresholds must be finite numbers"
    )


def load_ruleset_from_dict(data: dict[str, Any]) -> VetoRuleset:
    """Build a VetoRuleset from a parsed JSON object."""
    _validate_ruleset_dict(data)
    name = data.get("name") or "custom"
    rules: list[VetoRule] = []
    for i, r in enumerate(data["rules"]):
        try:
            rules.append(
                VetoRule(
                    name=r["name"],
                    metric=r["metric"],
                    op=r["op"],
                    threshold=float(r["threshold"]),
                    severity=r["severity"],
                    message=r.get("message", ""),
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"rules[{i}] invalid: {exc}") from exc
    return VetoRuleset(name=name, rules=tuple(rules))


def load_ruleset_from_path(path: Path | str) -> VetoRuleset:
    """Load a VetoRuleset from a JSON file with line-numbered parse errors.

    `parse_constant` rejects NaN / Infinity tokens before they can reach
    VetoRule (defense in depth — VetoRule.__post_init__ also rejects
    non-finite thresholds, so both paths are blocked).
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"ruleset file not found: {p}")
    try:
        data = json.loads(
            p.read_text(encoding="utf-8"),
            parse_constant=_reject_nonfinite_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"invalid JSON in {p} at line {exc.lineno} col {exc.colno}: {exc.msg}"
        ) from exc
    return load_ruleset_from_dict(data)


def load_ruleset(
    path_or_name: Path | str | None = None,
    *,
    env: dict[str, str] | None = None,
) -> VetoRuleset:
    """Resolve a ruleset using PRD precedence:

    1. explicit `path_or_name` arg (preset name if registered AND file does
       not exist on disk; otherwise treated as path)
    2. ``EVOLVE_VETO_RULES_PATH`` env var (path)
    3. ``EVOLVE_VETO_RULESET`` env var (preset name)
    4. ``DEFAULT_VETO_RULESET``

    `env` defaults to ``os.environ`` but is overridable for tests.
    """
    env = env if env is not None else dict(os.environ)

    if path_or_name is not None:
        candidate = str(path_or_name)
        if candidate in PRESETS and not Path(candidate).exists():
            return PRESETS[candidate]
        return load_ruleset_from_path(candidate)

    env_path = env.get("EVOLVE_VETO_RULES_PATH", "").strip()
    if env_path:
        return load_ruleset_from_path(env_path)

    env_preset = env.get("EVOLVE_VETO_RULESET", "").strip()
    if env_preset:
        if env_preset not in PRESETS:
            raise ValueError(
                f"EVOLVE_VETO_RULESET={env_preset!r} unknown; "
                f"valid: {tuple(PRESETS)}"
            )
        return PRESETS[env_preset]

    return DEFAULT_VETO_RULESET


# ── Formatter ──────────────────────────────────────────────────────────────


def _format_rule_line(result: VetoRuleResult) -> str:
    icon = "OK" if result.passed else ("X" if result.rule.severity == "hard" else "!")
    try:
        msg = result.rule.message.format(
            value=result.value,
            threshold=result.rule.threshold,
        )
    except (KeyError, IndexError, ValueError):
        msg = (
            result.rule.message
            or f"{result.rule.metric}={result.value} {result.rule.op} {result.rule.threshold}"
        )
    return f"  {icon} [{result.rule.severity:<4}] {result.rule.name:<25} {msg}"


def format_verdict_table(
    verdict: VetoVerdict, *, ruleset_name: str | None = None
) -> str:
    """Human-readable summary, styled to extend ``format_delta_table``."""
    lines = ["", "Veto:"]
    if ruleset_name:
        lines.append(f"  ruleset:   {ruleset_name}")
    if verdict.accepted:
        lines.append("  decision:  ADOPT")
    elif verdict.soft:
        lines.append("  decision:  REVIEW (soft veto)")
    else:
        lines.append("  decision:  REJECT (hard veto)")

    if verdict.hard_failures:
        lines.append("")
        lines.append("Hard failures:")
        for r in verdict.hard_failures:
            lines.append(_format_rule_line(r))
    if verdict.regression_failures:
        lines.append("")
        lines.append("Regression-corpus failures (always hard, --force cannot override):")
        for f in verdict.regression_failures:
            entry = f.entry
            lines.append(
                f"  X [hard] {entry.fixed_in:<40} "
                f"{f.reason} — top_score={f.observed_top_score:.3f} "
                f"(min {entry.min_top_score:.3f}) for {entry.query!r}"
            )
    if verdict.soft_failures:
        lines.append("")
        lines.append("Soft failures (review):")
        for r in verdict.soft_failures:
            lines.append(_format_rule_line(r))
    if verdict.passed:
        lines.append("")
        lines.append("Passed:")
        for r in verdict.passed:
            lines.append(_format_rule_line(r))
    return "\n".join(lines)


# ── Public exports ─────────────────────────────────────────────────────────


__all__ = [
    "ExitCode",
    "VetoRule",
    "VetoRuleset",
    "VetoVerdict",
    "VetoRuleResult",
    "VALID_METRICS",
    "VALID_OPS",
    "VALID_SEVERITIES",
    "METRIC_RESOLVERS",
    "OPERATORS",
    "DEFAULT_VETO_RULESET",
    "STRICT_VETO_RULESET",
    "PERMISSIVE_VETO_RULESET",
    "PRESETS",
    "evaluate_veto",
    "compute_exit_code",
    "load_ruleset",
    "load_ruleset_from_dict",
    "load_ruleset_from_path",
    "format_verdict_table",
]
