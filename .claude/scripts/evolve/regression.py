"""Regression corpus enforcement — Phase 2.6.

Pure evaluator. Given a candidate replay's per-query results plus the
regression query set, returns a ``RegressionSummary`` whose ``failed``
list drives a hard veto in ``evaluate_veto`` (replaces the
``NotImplementedError`` stub at ``veto.py:280``).

Failure reasons:
  - ``below_min_score``: candidate's top score for the query dropped
    below the recorded ``min_top_score`` threshold
  - ``wrong_top_path``: candidate returned results but the top path
    differs from the recorded ``expected_top_path``
  - ``no_results``: candidate returned zero results for a query that
    historically had a stable top hit

The replay is run by ``evolve propose`` ahead of time (via the existing
``run_replay_sync``) so this module is pure / synchronous / I/O-free.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from evolve.models import ReplayQueryResult


# ── Dataclasses ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RegressionEntry:
    """One regression-corpus entry — pure metadata.

    Built from ``regression_queries.json``. ``min_top_score`` is the
    floor below which a candidate replay is treated as a hard regression.
    ``expected_top_path`` is the result path the regression query
    historically returned; mismatches trigger ``wrong_top_path``.
    """

    query: str
    fixed_in: str
    expected_top_path: str
    min_top_score: float
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RegressionFailure:
    """A single regression-query failure with the reason it tripped."""

    entry: RegressionEntry
    observed_top_score: float
    observed_top_path: str
    reason: str  # "below_min_score" | "wrong_top_path" | "no_results"

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry": self.entry.to_dict(),
            "observed_top_score": self.observed_top_score,
            "observed_top_path": self.observed_top_path,
            "reason": self.reason,
        }


@dataclass
class RegressionSummary:
    """Aggregated regression-corpus verdict, JSON-serializable for the
    decision artifact (Phase 2.3.1) and audit trail."""

    total: int = 0
    passed: int = 0
    failed: list[RegressionFailure] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": [f.to_dict() for f in self.failed],
        }


# ── Loader ─────────────────────────────────────────────────────────────────


def load_regression_entries(raw_entries: list[dict[str, Any]]) -> list[RegressionEntry]:
    """Build ``RegressionEntry`` objects from validated raw dicts.

    The raw dict shape is what ``goldens.load_regression_queries`` returns —
    schema validation already happened there. This step coerces to
    typed records.
    """
    out: list[RegressionEntry] = []
    for raw in raw_entries:
        out.append(
            RegressionEntry(
                query=raw["query"],
                fixed_in=raw["fixed_in"],
                expected_top_path=raw["expected_top_path"],
                min_top_score=float(raw["min_top_score"]),
                notes=raw.get("notes", ""),
            )
        )
    return out


# ── Pure evaluator ─────────────────────────────────────────────────────────


def _top_score(result: ReplayQueryResult) -> float:
    return result.top_scores[0] if result.top_scores else 0.0


def _top_path(result: ReplayQueryResult) -> str:
    return result.result_paths[0] if result.result_paths else ""


def _classify_failure(
    entry: RegressionEntry, candidate: ReplayQueryResult
) -> RegressionFailure | None:
    """Return a failure record if the candidate fails this regression entry,
    or None when the entry passes.

    Order of checks: no_results > wrong_top_path > below_min_score. A query
    that returned zero results can't have a valid top path, so we report
    the structural failure before the numeric one.
    """
    if candidate.results_count == 0 or not candidate.top_scores:
        return RegressionFailure(
            entry=entry,
            observed_top_score=0.0,
            observed_top_path="",
            reason="no_results",
        )

    observed_score = _top_score(candidate)
    observed_path = _top_path(candidate)

    if observed_path != entry.expected_top_path:
        return RegressionFailure(
            entry=entry,
            observed_top_score=observed_score,
            observed_top_path=observed_path,
            reason="wrong_top_path",
        )

    if observed_score < entry.min_top_score:
        return RegressionFailure(
            entry=entry,
            observed_top_score=observed_score,
            observed_top_path=observed_path,
            reason="below_min_score",
        )

    return None


def evaluate_regression_corpus(
    candidate_per_query: list[ReplayQueryResult],
    regression_set: list[RegressionEntry],
) -> RegressionSummary:
    """Compute pass/fail per regression query.

    The replay must already have been executed against the regression
    corpus (typically by ``evolve propose`` running the regression queries
    through ``run_replay_sync``). Per-query results are paired by index
    against the regression entries — caller is responsible for keeping
    them aligned (same order, same length).

    Raises ``ValueError`` on length mismatch — silent index drift would
    produce wrong verdicts on every entry.
    """
    if len(candidate_per_query) != len(regression_set):
        raise ValueError(
            f"regression replay length mismatch: candidate has "
            f"{len(candidate_per_query)} per-query results, regression "
            f"set has {len(regression_set)} entries. Caller must keep "
            f"the replay aligned with the regression corpus."
        )

    summary = RegressionSummary(total=len(regression_set))
    for entry, candidate in zip(regression_set, candidate_per_query):
        failure = _classify_failure(entry, candidate)
        if failure is None:
            summary.passed += 1
        else:
            summary.failed.append(failure)
    return summary


__all__ = [
    "RegressionEntry",
    "RegressionFailure",
    "RegressionSummary",
    "load_regression_entries",
    "evaluate_regression_corpus",
]
