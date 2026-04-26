"""Phase 2.6 tests — bootstrap CI helpers (evolve/statistics.py).

Pure-stdlib bootstrap using random.choices + linear-interpolation percentile.
Tests cover determinism (seed fixes output), edge cases (n=0/1), correctness
on degenerate distributions (all-hit / all-miss), and paired-bootstrap
structure preservation.
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


def _make_results(hits: int, misses: int):
    """Build a per_query list with `hits` queries that hit (top_score=0.5)
    and `misses` queries that returned nothing."""
    from evolve.models import ReplayQueryResult

    return [
        *[
            ReplayQueryResult(
                query=f"hit-{i}",
                results_count=3,
                top_scores=[0.5, 0.4, 0.3],
            )
            for i in range(hits)
        ],
        *[
            ReplayQueryResult(query=f"miss-{i}", results_count=0)
            for i in range(misses)
        ],
    ]


# ── TestBootstrapHitRate ──────────────────────────────────────────────────


class TestBootstrapHitRate:
    def test_deterministic_with_seed(self):
        from evolve.statistics import bootstrap_hit_rate_ci

        per_query = _make_results(hits=30, misses=20)
        ci_a = bootstrap_hit_rate_ci(per_query, iterations=1000, seed=42)
        ci_b = bootstrap_hit_rate_ci(per_query, iterations=1000, seed=42)
        assert ci_a == ci_b

    def test_different_seeds_produce_different_cis(self):
        """Sanity: bootstrap is actually using the seed (else seeded determinism
        would be vacuously true via fixed initial state)."""
        from evolve.statistics import bootstrap_hit_rate_ci

        per_query = _make_results(hits=25, misses=25)
        ci_a = bootstrap_hit_rate_ci(per_query, iterations=1000, seed=1)
        ci_b = bootstrap_hit_rate_ci(per_query, iterations=1000, seed=2)
        assert ci_a != ci_b

    def test_all_hits_ci_is_one(self):
        """100% hit rate has zero variance — every bootstrap sample is 1.0."""
        from evolve.statistics import bootstrap_hit_rate_ci

        per_query = _make_results(hits=20, misses=0)
        lo, hi = bootstrap_hit_rate_ci(per_query, iterations=1000, seed=42)
        assert lo == 1.0
        assert hi == 1.0

    def test_all_misses_ci_is_zero(self):
        """0% hit rate has zero variance — every bootstrap sample is 0.0."""
        from evolve.statistics import bootstrap_hit_rate_ci

        per_query = _make_results(hits=0, misses=20)
        lo, hi = bootstrap_hit_rate_ci(per_query, iterations=1000, seed=42)
        assert lo == 0.0
        assert hi == 0.0

    def test_50_50_brackets_half(self):
        """At p=0.5 with n=50, the 95% CI should bracket 0.5."""
        from evolve.statistics import bootstrap_hit_rate_ci

        per_query = _make_results(hits=25, misses=25)
        lo, hi = bootstrap_hit_rate_ci(per_query, iterations=2000, seed=42)
        assert lo < 0.5 < hi
        # CI width sanity — should be reasonable for n=50, p=0.5
        assert (hi - lo) < 0.4


# ── TestBootstrapDelta ────────────────────────────────────────────────────


class TestBootstrapDelta:
    def test_no_op_replay_brackets_zero(self):
        """When baseline == candidate per_query, every paired delta is 0
        — CI must bracket 0 with negligible width."""
        from evolve.statistics import bootstrap_delta_ci

        per_query = _make_results(hits=30, misses=20)
        lo, hi = bootstrap_delta_ci(per_query, per_query,
                                     metric="hit_rate", iterations=1000, seed=42)
        assert lo == 0.0
        assert hi == 0.0

    def test_paired_structure_preserved(self):
        """Paired bootstrap resamples INDICES jointly. If we pair perfect
        anti-correlation (baseline hits where candidate misses, vice versa),
        the delta CI must reflect the variance of the difference."""
        from evolve.models import ReplayQueryResult
        from evolve.statistics import bootstrap_delta_ci

        # baseline: 50 hits / 0 misses
        baseline = _make_results(hits=50, misses=0)
        # candidate: 0 hits / 50 misses (every paired delta = -1 for hit_rate)
        candidate = _make_results(hits=0, misses=50)

        lo, hi = bootstrap_delta_ci(baseline, candidate,
                                     metric="hit_rate", iterations=1000, seed=42)
        # Every resample preserves the structure: hits in baseline always pair
        # with misses in candidate. Delta is -1.0 every time.
        assert lo == -1.0
        assert hi == -1.0

    def test_deterministic_with_seed(self):
        from evolve.statistics import bootstrap_delta_ci

        b = _make_results(hits=20, misses=10)
        c = _make_results(hits=25, misses=5)
        ci_a = bootstrap_delta_ci(b, c, metric="hit_rate", iterations=1000, seed=42)
        ci_b = bootstrap_delta_ci(b, c, metric="hit_rate", iterations=1000, seed=42)
        assert ci_a == ci_b


# ── TestEdgeCases ────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_n_zero_returns_zero_ci(self):
        from evolve.statistics import bootstrap_hit_rate_ci, bootstrap_delta_ci

        assert bootstrap_hit_rate_ci([], iterations=100) == (0.0, 0.0)
        assert bootstrap_delta_ci([], [], metric="hit_rate", iterations=100) == (0.0, 0.0)

    def test_n_one_returns_degenerate_ci(self):
        """Resampling n=1 with replacement always picks that one element —
        CI is degenerate (lower == upper) but doesn't crash."""
        from evolve.statistics import bootstrap_hit_rate_ci

        per_query = _make_results(hits=1, misses=0)
        lo, hi = bootstrap_hit_rate_ci(per_query, iterations=1000, seed=42)
        assert lo == hi == 1.0

    def test_iterations_zero_raises(self):
        from evolve.statistics import bootstrap_hit_rate_ci

        with pytest.raises(ValueError, match="iterations"):
            bootstrap_hit_rate_ci(_make_results(hits=10, misses=0), iterations=0)

    def test_confidence_outside_zero_one_raises(self):
        from evolve.statistics import bootstrap_hit_rate_ci

        per_query = _make_results(hits=10, misses=0)
        with pytest.raises(ValueError, match="confidence"):
            bootstrap_hit_rate_ci(per_query, confidence=0.0, iterations=100)
        with pytest.raises(ValueError, match="confidence"):
            bootstrap_hit_rate_ci(per_query, confidence=1.0, iterations=100)
        with pytest.raises(ValueError, match="confidence"):
            bootstrap_hit_rate_ci(per_query, confidence=1.5, iterations=100)

    def test_unknown_metric_raises(self):
        from evolve.statistics import bootstrap_delta_ci

        per_query = _make_results(hits=5, misses=5)
        with pytest.raises(ValueError, match="metric"):
            bootstrap_delta_ci(per_query, per_query, metric="latency", iterations=100)

    def test_unequal_lengths_raises(self):
        from evolve.statistics import bootstrap_delta_ci

        b = _make_results(hits=5, misses=5)
        c = _make_results(hits=3, misses=3)
        with pytest.raises(ValueError, match="paired bootstrap"):
            bootstrap_delta_ci(b, c, metric="hit_rate", iterations=100)
