"""Bootstrap confidence intervals for replay deltas — Phase 2.6.

Pure stdlib. ``random.choices`` for resampling, linear-interpolation
percentile for CI bounds. No numpy / scipy / statsmodels — matches the
"Pure stdlib. No provider deps." posture of evolve.compare and evolve.veto
(2.3.1 commit 22be531). Numpy is transitively present via fastembed but
using it for bootstrap would create a false coupling.

Method: non-parametric percentile bootstrap.

- ``bootstrap_hit_rate_ci``: resample per_query with replacement, compute
  hit_rate on each resample, take the (alpha, 1-alpha) percentiles.
- ``bootstrap_delta_ci``: resample INDICES jointly (paired bootstrap) so
  baseline[i] and candidate[i] stay aligned through the resample. Compute
  the chosen metric's delta on each paired resample.

Determinism: passing ``seed`` makes bootstrap output identical across
runs (PRD Phase 2.6 AC: "deterministic when seed is fixed").

Perf: 50-query sample × 1000 iterations runs in ~50ms on a stock laptop —
well under the PRD's <500ms budget.
"""

from __future__ import annotations

import random
import statistics
from typing import Any

from evolve.models import ReplayQueryResult


# ── Helpers ────────────────────────────────────────────────────────────────


def _percentile(sorted_data: list[float], p: float) -> float:
    """Linear-interpolation percentile (``p`` in [0, 1]).

    Returns 0.0 on empty input — matches the convention used by
    ``ReplaySummary.avg_top_score`` and the empty-query guards elsewhere.
    """
    if not sorted_data:
        return 0.0
    n = len(sorted_data)
    if n == 1:
        return sorted_data[0]
    idx = p * (n - 1)
    lower = int(idx)
    upper = min(lower + 1, n - 1)
    frac = idx - lower
    return sorted_data[lower] + frac * (sorted_data[upper] - sorted_data[lower])


def _hit_rate(per_query: list[ReplayQueryResult]) -> float:
    """Fraction of queries with at least one result. 0.0 on empty input."""
    if not per_query:
        return 0.0
    hits = sum(1 for r in per_query if r.results_count > 0)
    return hits / len(per_query)


def _avg_top_score(per_query: list[ReplayQueryResult]) -> float:
    """Mean of top scores across queries that returned at least one result.

    Queries with empty ``top_scores`` are excluded — same semantics as
    ``ReplaySummary.avg_top_score`` in ``replay._summarize``.
    """
    scores = [r.top_scores[0] for r in per_query if r.top_scores]
    if not scores:
        return 0.0
    return statistics.mean(scores)


_METRIC_FNS: dict[str, Any] = {
    "hit_rate": _hit_rate,
    "avg_top_score": _avg_top_score,
}


def _validate_args(confidence: float, iterations: int) -> None:
    if not (0.0 < confidence < 1.0):
        raise ValueError(
            f"confidence must be in (0, 1) exclusive, got {confidence!r}"
        )
    if iterations < 1:
        raise ValueError(
            f"iterations must be >= 1, got {iterations!r}"
        )


# ── Public API ─────────────────────────────────────────────────────────────


def bootstrap_hit_rate_ci(
    per_query: list[ReplayQueryResult],
    *,
    confidence: float = 0.95,
    iterations: int = 1000,
    seed: int | None = None,
) -> tuple[float, float]:
    """Non-parametric percentile bootstrap CI on hit rate.

    Returns ``(lower, upper)`` bounds at the given confidence level. The
    width of the interval shrinks with sample size (n) and concentrates
    around 1.0 / 0.0 when hit rate is degenerate (all hits / all misses).

    On a no-op replay (baseline == candidate) the CI width should be near
    zero — variance comes entirely from resampling identical results.
    """
    _validate_args(confidence, iterations)
    n = len(per_query)
    if n == 0:
        return (0.0, 0.0)

    rng = random.Random(seed) if seed is not None else random.Random()
    bootstrap_rates: list[float] = []
    for _ in range(iterations):
        sample = rng.choices(per_query, k=n)
        bootstrap_rates.append(_hit_rate(sample))

    bootstrap_rates.sort()
    alpha = (1.0 - confidence) / 2.0
    return (
        _percentile(bootstrap_rates, alpha),
        _percentile(bootstrap_rates, 1.0 - alpha),
    )


def bootstrap_delta_ci(
    baseline_per_query: list[ReplayQueryResult],
    candidate_per_query: list[ReplayQueryResult],
    *,
    metric: str = "hit_rate",
    confidence: float = 0.95,
    iterations: int = 1000,
    seed: int | None = None,
) -> tuple[float, float]:
    """Paired-bootstrap CI on the candidate-minus-baseline delta of a metric.

    Index-joint resampling: ``rng.choices(range(n), k=n)`` produces an
    index list, and both baseline[i] and candidate[i] are pulled with that
    same i. This preserves the per-query pairing that makes ``compare_reports``
    meaningful — query identity must align across baseline/candidate.

    On a no-op replay (baseline == candidate per_query elements) the
    delta CI brackets 0.0 with negligible width.

    Currently supports ``metric in {"hit_rate", "avg_top_score"}``.
    """
    _validate_args(confidence, iterations)
    if metric not in _METRIC_FNS:
        raise ValueError(
            f"metric must be one of {tuple(_METRIC_FNS)}, got {metric!r}"
        )
    if len(baseline_per_query) != len(candidate_per_query):
        raise ValueError(
            f"paired bootstrap requires equal-length inputs; "
            f"baseline={len(baseline_per_query)}, "
            f"candidate={len(candidate_per_query)}"
        )

    n = len(baseline_per_query)
    if n == 0:
        return (0.0, 0.0)

    metric_fn = _METRIC_FNS[metric]
    rng = random.Random(seed) if seed is not None else random.Random()

    bootstrap_deltas: list[float] = []
    for _ in range(iterations):
        indices = rng.choices(range(n), k=n)
        b_sample = [baseline_per_query[i] for i in indices]
        c_sample = [candidate_per_query[i] for i in indices]
        bootstrap_deltas.append(metric_fn(c_sample) - metric_fn(b_sample))

    bootstrap_deltas.sort()
    alpha = (1.0 - confidence) / 2.0
    return (
        _percentile(bootstrap_deltas, alpha),
        _percentile(bootstrap_deltas, 1.0 - alpha),
    )


__all__ = [
    "bootstrap_hit_rate_ci",
    "bootstrap_delta_ci",
]
