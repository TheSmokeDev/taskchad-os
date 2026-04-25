"""Phase 2.3.1 — IO helpers for the veto layer.

Two responsibilities:

1. ``load_report_delta(path)`` — load a saved ``ReportDelta`` JSON with
   fail-fast on missing aggregates AND recompute of derived counts from
   ``per_query``. The recompute defeats stale/hand-edited delta files where
   per-query verdicts disagree with the cached aggregate counts (Codex
   review 2026-04-25 Finding 2). Cached aggregates are NOT trusted as the
   gate input — they are caches that can drift.

2. ``write_decision_artifact(out_dir, ...)`` — persist the veto decision
   alongside the candidate report so the audit trail survives without
   stdout capture (Codex review 2026-04-25 Finding 5). The decision JSON
   records ``delta``, ``verdict``, ``force``, ``effective_exit_code``, and
   the experiment IDs — the full context a future reviewer needs to
   understand why a candidate was adopted, rejected, or sent to review.

Pure stdlib. No provider deps. The only clock read is in
``write_decision_artifact``'s timestamp; ``load_report_delta`` is pure.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evolve.compare import QueryDelta, ReportDelta
from evolve.veto import VetoVerdict


# Direct-aggregate fields that have no per-query reconstruction path. Missing
# any of these means the file is too damaged to trust — fail closed.
_REQUIRED_DELTA_FIELDS: tuple[str, ...] = (
    "baseline_experiment_id",
    "candidate_experiment_id",
    "hit_rate_delta",
    "avg_top_score_delta",
)


def load_report_delta(path: Path | str) -> ReportDelta:
    """Load a ``ReportDelta`` from JSON with safety guarantees.

    Guarantees:
    - Required aggregate fields (experiment IDs, hit_rate_delta,
      avg_top_score_delta) MUST be present — missing fields raise
      ``ValueError``. There is no per-query reconstruction for these.
    - ``verdict_counts`` and ``error_count_delta`` are RECOMPUTED from
      ``per_query`` regardless of stored values. Per-query data is the
      source of truth; stored aggregates are a cache that can drift.
    - ``p50/p90 latency`` and ``tier_distribution_delta`` default to safe
      values when missing — they are soft-veto territory only.

    Raises ``ValueError`` on missing required fields or malformed JSON.
    """
    p = Path(path)
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(
            f"delta JSON must be an object, got {type(raw).__name__}"
        )

    missing = [k for k in _REQUIRED_DELTA_FIELDS if k not in raw]
    if missing:
        raise ValueError(
            f"delta JSON {p} missing required field(s): {missing}; "
            f"refuse to evaluate veto on incomplete delta"
        )

    per_query = [QueryDelta(**q) for q in raw.get("per_query", [])]

    # Recompute the count-style aggregates from per_query so a stale or
    # hand-edited cache cannot bypass the count-based hard rules.
    verdict_counts: dict[str, int] = {}
    error_count_delta = 0
    for q in per_query:
        verdict_counts[q.verdict] = verdict_counts.get(q.verdict, 0) + 1
        error_count_delta += int(q.error_count_delta)

    return ReportDelta(
        baseline_experiment_id=raw["baseline_experiment_id"],
        candidate_experiment_id=raw["candidate_experiment_id"],
        hit_rate_delta=float(raw["hit_rate_delta"]),
        avg_top_score_delta=float(raw["avg_top_score_delta"]),
        p50_latency_delta_ms=float(raw.get("p50_latency_delta_ms", 0.0)),
        p90_latency_delta_ms=float(raw.get("p90_latency_delta_ms", 0.0)),
        tier_distribution_delta=raw.get("tier_distribution_delta", {}),
        verdict_counts=verdict_counts,
        per_query=per_query,
        baseline_overrides=raw.get("baseline_overrides", {}),
        candidate_overrides=raw.get("candidate_overrides", {}),
        error_count_delta=error_count_delta,
    )


def write_decision_artifact(
    out_dir: Path | str,
    *,
    baseline_experiment_id: str,
    candidate_experiment_id: str,
    ruleset_name: str,
    delta: ReportDelta,
    verdict: VetoVerdict,
    force: bool,
    exit_code: int,
    overrides: dict[str, Any] | None = None,
    langfuse_trace_url: str | None = None,
) -> Path:
    """Persist a veto decision JSON alongside the candidate report.

    Filename: ``decision-<candidate_experiment_id>.json``.

    The decision artifact records the full context needed to audit any
    autonomous adoption: which ruleset was applied, what the delta said,
    what the verdict was, whether ``--force`` overrode a soft veto, what
    exit code the process actually returned, which overrides drove the
    candidate, and (Phase 2.4) the Langfuse trace URL when the candidate
    replay opted into tracing. This closes the audit-trail gap when
    ``--force`` flips a soft veto to ADOPT — the override is recorded on
    disk, not just stdout — and lets a reviewer click straight from the
    decision JSON to the per-query span tree that produced it.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"decision-{candidate_experiment_id}.json"
    path.write_text(
        json.dumps(
            {
                "timestamp_utc": datetime.now(UTC).isoformat(),
                "baseline_experiment_id": baseline_experiment_id,
                "candidate_experiment_id": candidate_experiment_id,
                "ruleset": ruleset_name,
                "delta": delta.to_dict(),
                "verdict": verdict.to_dict(),
                "force": force,
                "effective_exit_code": int(exit_code),
                "overrides": overrides,
                "langfuse_trace_url": langfuse_trace_url,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


__all__ = [
    "load_report_delta",
    "write_decision_artifact",
]
