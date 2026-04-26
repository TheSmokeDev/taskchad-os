"""ReplayReport diff — Phase 2.1.

Pure stdlib. Given two ReplayReports (baseline + candidate), produce a
structured delta showing which queries got better, worse, or stayed the
same, plus aggregate shifts in hit rate, latency, and tier distribution.

No network, no LLM — this is the trustable scoring layer. Veto logic
(Phase 2.3) sits on top of this and decides whether a candidate config
is worth adopting.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from evolve.models import ReplayQueryResult, ReplayReport

# Absolute score delta below this is treated as "same" — debounces rounding jitter.
SCORE_NOISE_FLOOR = 0.01
# Absolute latency delta in ms below this is treated as "same".
LATENCY_NOISE_FLOOR_MS = 5.0


@dataclass
class QueryDelta:
    """Per-query diff between baseline and candidate."""

    query: str
    # better|worse|same|new_hit|lost_hit|still_missing|new_error|fixed_error|still_errored
    verdict: str
    baseline_tier: str = ""
    candidate_tier: str = ""
    baseline_top_score: float = 0.0
    candidate_top_score: float = 0.0
    score_delta: float = 0.0
    baseline_latency_ms: float = 0.0
    candidate_latency_ms: float = 0.0
    latency_delta_ms: float = 0.0
    baseline_results_count: int = 0
    candidate_results_count: int = 0
    baseline_error: str = ""
    candidate_error: str = ""
    error_count_delta: int = 0  # +1 new error, -1 fixed error, 0 no change

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReportDelta:
    """Aggregate diff between two ReplayReports."""

    baseline_experiment_id: str
    candidate_experiment_id: str
    hit_rate_delta: float = 0.0
    avg_top_score_delta: float = 0.0
    p50_latency_delta_ms: float = 0.0
    p90_latency_delta_ms: float = 0.0
    tier_distribution_delta: dict[str, int] = field(default_factory=dict)
    verdict_counts: dict[str, int] = field(default_factory=dict)
    per_query: list[QueryDelta] = field(default_factory=list)
    baseline_overrides: dict[str, Any] = field(default_factory=dict)
    candidate_overrides: dict[str, Any] = field(default_factory=dict)
    error_count_delta: int = 0  # +1 net new errors, -1 net fixed, 0 no change
    # Phase 2.6: paired-bootstrap CI bounds (lower, upper) on the delta of
    # each metric. Populated only when compare_reports(..., with_ci=True);
    # None for backward-compat with v1 reports + the default fast path.
    hit_rate_ci_95: tuple[float, float] | None = None
    avg_top_score_ci_95: tuple[float, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_experiment_id": self.baseline_experiment_id,
            "candidate_experiment_id": self.candidate_experiment_id,
            "hit_rate_delta": self.hit_rate_delta,
            "avg_top_score_delta": self.avg_top_score_delta,
            "p50_latency_delta_ms": self.p50_latency_delta_ms,
            "p90_latency_delta_ms": self.p90_latency_delta_ms,
            "tier_distribution_delta": self.tier_distribution_delta,
            "verdict_counts": self.verdict_counts,
            "baseline_overrides": self.baseline_overrides,
            "candidate_overrides": self.candidate_overrides,
            "per_query": [q.to_dict() for q in self.per_query],
            "error_count_delta": self.error_count_delta,
            "hit_rate_ci_95": list(self.hit_rate_ci_95) if self.hit_rate_ci_95 else None,
            "avg_top_score_ci_95": (
                list(self.avg_top_score_ci_95)
                if self.avg_top_score_ci_95
                else None
            ),
        }


def _top_score(result: ReplayQueryResult) -> float:
    return result.top_scores[0] if result.top_scores else 0.0


def _classify(baseline: ReplayQueryResult, candidate: ReplayQueryResult) -> str:
    """Turn a per-query pair into a verdict label."""
    b_err = bool(baseline.error)
    c_err = bool(candidate.error)

    # Error verdicts take priority — a zero results_count caused by an error
    # is not the same as a genuine miss and must not be labeled still_missing.
    if b_err and c_err:
        return "still_errored"
    if not b_err and c_err:
        return "new_error"
    if b_err and not c_err:
        return "fixed_error"

    b_hit = baseline.results_count > 0
    c_hit = candidate.results_count > 0

    if not b_hit and not c_hit:
        return "still_missing"
    if not b_hit and c_hit:
        return "new_hit"
    if b_hit and not c_hit:
        return "lost_hit"

    score_delta = _top_score(candidate) - _top_score(baseline)
    if abs(score_delta) < SCORE_NOISE_FLOOR:
        return "same"
    return "better" if score_delta > 0 else "worse"


def _diff_tier_distribution(
    baseline_dist: dict[str, int], candidate_dist: dict[str, int]
) -> dict[str, int]:
    """Signed delta per tier — positive means candidate saw more of that tier."""
    keys = set(baseline_dist) | set(candidate_dist)
    return {k: candidate_dist.get(k, 0) - baseline_dist.get(k, 0) for k in sorted(keys)}


def compare_reports(
    baseline: ReplayReport,
    candidate: ReplayReport,
    *,
    with_ci: bool = False,
    ci_iterations: int = 1000,
    ci_seed: int | None = None,
) -> ReportDelta:
    """Diff two replay reports. Queries must be in the same order.

    When ``with_ci=True`` (Phase 2.6), populates ``hit_rate_ci_95`` and
    ``avg_top_score_ci_95`` on the returned ``ReportDelta`` via a paired
    percentile bootstrap (B=ci_iterations, default 1000; pure stdlib via
    ``evolve.statistics``). The CI brackets the *delta* metric, not the
    absolute metric.

    ``ci_seed`` is forwarded to the bootstrap RNG for deterministic CIs
    in tests; production runs leave it None for natural variance.
    """
    delta = ReportDelta(
        baseline_experiment_id=baseline.experiment_id,
        candidate_experiment_id=candidate.experiment_id,
        hit_rate_delta=round(candidate.summary.hit_rate - baseline.summary.hit_rate, 4),
        avg_top_score_delta=round(
            candidate.summary.avg_top_score - baseline.summary.avg_top_score, 4
        ),
        p50_latency_delta_ms=round(
            candidate.summary.p50_latency_ms - baseline.summary.p50_latency_ms, 2
        ),
        p90_latency_delta_ms=round(
            candidate.summary.p90_latency_ms - baseline.summary.p90_latency_ms, 2
        ),
        tier_distribution_delta=_diff_tier_distribution(
            baseline.summary.tier_distribution, candidate.summary.tier_distribution
        ),
        baseline_overrides=baseline.overrides,
        candidate_overrides=candidate.overrides,
    )

    # Guard: query sets must be identical and in the same order.
    b_queries = [r.query for r in baseline.per_query]
    c_queries = [r.query for r in candidate.per_query]
    if len(b_queries) != len(c_queries):
        raise ValueError(
            f"Query list length mismatch: baseline has {len(b_queries)} queries, "
            f"candidate has {len(c_queries)}. Re-run both reports against the same query set."
        )
    mismatches = [(i, bq, cq) for i, (bq, cq) in enumerate(zip(b_queries, c_queries)) if bq != cq]
    if mismatches:
        first = mismatches[0]
        raise ValueError(
            f"Query identity mismatch at index {first[0]}: "
            f"baseline={first[1]!r}, candidate={first[2]!r}. "
            f"Ensure both reports use the same query set in the same order."
        )

    verdict_counts: dict[str, int] = {}
    error_count_delta = 0

    for b, c in zip(baseline.per_query, candidate.per_query):
        verdict = _classify(b, c)
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
        b_errored = 1 if b.error else 0
        c_errored = 1 if c.error else 0
        q_error_delta = c_errored - b_errored
        error_count_delta += q_error_delta

        delta.per_query.append(
            QueryDelta(
                query=b.query,
                verdict=verdict,
                baseline_tier=b.tier,
                candidate_tier=c.tier,
                baseline_top_score=_top_score(b),
                candidate_top_score=_top_score(c),
                score_delta=round(_top_score(c) - _top_score(b), 4),
                baseline_latency_ms=b.latency_ms,
                candidate_latency_ms=c.latency_ms,
                latency_delta_ms=round(c.latency_ms - b.latency_ms, 2),
                baseline_results_count=b.results_count,
                candidate_results_count=c.results_count,
                baseline_error=b.error,
                candidate_error=c.error,
                error_count_delta=q_error_delta,
            )
        )

    delta.verdict_counts = verdict_counts
    delta.error_count_delta = error_count_delta

    if with_ci:
        # Lazy import keeps `compare.py` cheap when the bootstrap path is
        # unused; statistics.py is pure stdlib so the import itself has
        # zero cost beyond the module load.
        from evolve.statistics import bootstrap_delta_ci

        delta.hit_rate_ci_95 = bootstrap_delta_ci(
            baseline.per_query,
            candidate.per_query,
            metric="hit_rate",
            confidence=0.95,
            iterations=ci_iterations,
            seed=ci_seed,
        )
        delta.avg_top_score_ci_95 = bootstrap_delta_ci(
            baseline.per_query,
            candidate.per_query,
            metric="avg_top_score",
            confidence=0.95,
            iterations=ci_iterations,
            seed=ci_seed,
        )

    return delta


def _format_ci_band(ci: tuple[float, float] | None) -> str:
    """Render a 95% CI band suffix or empty string when CI is absent."""
    if ci is None:
        return ""
    lo, hi = ci
    return f"  [95% CI: {lo:+.4f}, {hi:+.4f}]"


def format_delta_table(delta: ReportDelta) -> str:
    """Human-readable summary for CLI output.

    When ``hit_rate_ci_95`` / ``avg_top_score_ci_95`` are populated (Phase
    2.6 ``compare_reports(with_ci=True)``), each metric line gets a
    ``[95% CI: lo, hi]`` band suffix so reviewers can distinguish real
    movement from sampling noise.
    """
    lines = [
        f"baseline:  {delta.baseline_experiment_id}",
        f"candidate: {delta.candidate_experiment_id}",
        "",
        "Aggregate:",
        f"  hit_rate:      {delta.hit_rate_delta:+.4f}{_format_ci_band(delta.hit_rate_ci_95)}",
        f"  avg_top_score: {delta.avg_top_score_delta:+.4f}{_format_ci_band(delta.avg_top_score_ci_95)}",
        f"  p50_latency:   {delta.p50_latency_delta_ms:+.2f} ms",
        f"  p90_latency:   {delta.p90_latency_delta_ms:+.2f} ms",
    ]
    if delta.error_count_delta != 0:
        lines.append(f"  error_count:   {delta.error_count_delta:+d}")
    lines.append("")
    lines.append("Verdicts:")
    for verdict in (
        "better",
        "worse",
        "same",
        "new_hit",
        "lost_hit",
        "still_missing",
        "new_error",
        "fixed_error",
        "still_errored",
    ):
        count = delta.verdict_counts.get(verdict, 0)
        if count:
            lines.append(f"  {verdict:<14} {count}")

    if delta.tier_distribution_delta:
        lines.append("")
        lines.append("Tier distribution delta:")
        for tier, d in delta.tier_distribution_delta.items():
            if d:
                lines.append(f"  {tier:<14} {d:+d}")

    lines.append("")
    lines.append("Per-query:")
    for q in delta.per_query:
        arrow = {
            "better": "^",
            "worse": "v",
            "same": "=",
            "new_hit": "+",
            "lost_hit": "-",
            "still_missing": ".",
            "new_error": "!",
            "fixed_error": "~",
            "still_errored": "x",
        }.get(q.verdict, "?")
        lines.append(
            f"  {arrow} [{q.verdict:<13}] {q.score_delta:+.4f}  "
            f"{q.latency_delta_ms:+6.1f}ms  {q.query[:60]}"
        )
    return "\n".join(lines)
