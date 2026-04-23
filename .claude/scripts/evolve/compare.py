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
    verdict: str  # "better" | "worse" | "same" | "new_hit" | "lost_hit" | "still_missing"
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
        }


def _top_score(result: ReplayQueryResult) -> float:
    return result.top_scores[0] if result.top_scores else 0.0


def _classify(baseline: ReplayQueryResult, candidate: ReplayQueryResult) -> str:
    """Turn a per-query pair into a verdict label."""
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


def compare_reports(baseline: ReplayReport, candidate: ReplayReport) -> ReportDelta:
    """Diff two replay reports. Queries must be in the same order."""
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

    # Align per-query by index. If lengths differ, pad with empty results so
    # the mismatch surfaces as `lost_hit` / `new_hit` rather than a crash.
    max_len = max(len(baseline.per_query), len(candidate.per_query))
    verdict_counts: dict[str, int] = {}

    for i in range(max_len):
        b = baseline.per_query[i] if i < len(baseline.per_query) else ReplayQueryResult(query="")
        c = candidate.per_query[i] if i < len(candidate.per_query) else ReplayQueryResult(query="")
        verdict = _classify(b, c)
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1

        delta.per_query.append(
            QueryDelta(
                query=c.query or b.query,
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
            )
        )

    delta.verdict_counts = verdict_counts
    return delta


def format_delta_table(delta: ReportDelta) -> str:
    """Human-readable summary for CLI output."""
    lines = [
        f"baseline:  {delta.baseline_experiment_id}",
        f"candidate: {delta.candidate_experiment_id}",
        "",
        "Aggregate:",
        f"  hit_rate:      {delta.hit_rate_delta:+.4f}",
        f"  avg_top_score: {delta.avg_top_score_delta:+.4f}",
        f"  p50_latency:   {delta.p50_latency_delta_ms:+.2f} ms",
        f"  p90_latency:   {delta.p90_latency_delta_ms:+.2f} ms",
        "",
        "Verdicts:",
    ]
    for verdict in ("better", "worse", "same", "new_hit", "lost_hit", "still_missing"):
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
        }.get(q.verdict, "?")
        lines.append(
            f"  {arrow} [{q.verdict:<13}] {q.score_delta:+.4f}  {q.latency_delta_ms:+6.1f}ms  {q.query[:60]}"
        )
    return "\n".join(lines)
