"""Replay harness — the sharpened axe.

Given a set of queries and a candidate configuration, replays each query
through the real recall pipeline with the candidate values applied, captures
per-query outcomes, and produces a structured ReplayReport.

Guarantees:
- **Determinism**: identical (queries, overrides, vault state) → identical report
  modulo millisecond timing. Reranker (if enabled) is the only non-deterministic
  component; caller can disable via overrides.
- **Isolation**: no writes to `data/state/recall-log.json`, no mutation of
  session DB, no pollution of production Langfuse traces beyond the
  experiment-tagged span. Achieved by `replay_context()`.
- **Reversibility**: config attribute patches are restored on exit, even on
  exception.

Non-goals: proposing changes, deciding verdicts, writing concept pages. Pure
consumer — (queries, config) → metrics.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for _p in (_SCRIPTS_DIR, _CHAT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from evolve.config_override import (
    RECALL_CONFIG_KEYS,
    replay_context,
    snapshot_config,
)
from evolve.models import ReplayQueryResult, ReplayReport, ReplaySummary


def _generate_experiment_id(prefix: str = "exp") -> str:
    """ISO-ish timestamp id usable as a filename fragment."""
    return f"{prefix}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def _resolve_memory_dir(memory_dir: Path | str | None) -> Path:
    """Resolve the memory_dir argument to an absolute Path.

    Defaults to `<repo_root>/vault/memory`, matching config.MEMORY_DIR.
    """
    if memory_dir is not None:
        return Path(memory_dir).resolve()
    try:
        from config import MEMORY_DIR
        return Path(MEMORY_DIR).resolve()
    except Exception:
        return (_SCRIPTS_DIR.parent.parent / "TheHomie" / "Memory").resolve()


async def _replay_one(
    query: str,
    memory_dir: Path,
    *,
    caller: str,
    max_results: int,
) -> ReplayQueryResult:
    """Run one query through the real recall pipeline, capture the outcome."""
    from recall_service import recall, SearchMode

    try:
        response = await recall(
            query=query,
            memory_dir=memory_dir,
            search_mode=SearchMode.AUTO,
            caller=caller,
            max_results=max_results,
        )
    except Exception as exc:
        return ReplayQueryResult(query=query, error=repr(exc))

    log = response.log
    results = response.results or []

    return ReplayQueryResult(
        query=query,
        tier=str(getattr(log, "tier", "")),
        search_mode=str(getattr(log, "search_mode", "")),
        results_count=len(results),
        top_scores=[round(float(r.score), 4) for r in results[:3]],
        result_paths=[str(getattr(r, "path", "")) for r in results],
        latency_ms=round(float(getattr(log, "latency_ms", 0.0)), 2),
        queries_generated=list(getattr(log, "queries_generated", []) or []),
        graph_hops=int(getattr(log, "graph_hops_traversed", 0) or 0),
        graph_neighbors=int(getattr(log, "graph_neighbors_found", 0) or 0),
    )


def _summarize(per_query: list[ReplayQueryResult]) -> ReplaySummary:
    """Aggregate per-query results into summary metrics."""
    s = ReplaySummary(query_count=len(per_query))
    if not per_query:
        return s

    hits = [r for r in per_query if r.results_count > 0]
    s.hit_count = len(hits)
    s.hit_rate = round(s.hit_count / s.query_count, 4)

    top_scores = [r.top_scores[0] for r in hits if r.top_scores]
    s.avg_top_score = round(statistics.mean(top_scores), 4) if top_scores else 0.0

    latencies = [r.latency_ms for r in per_query if r.latency_ms > 0]
    if latencies:
        s.total_latency_ms = round(sum(latencies), 2)
        s.p50_latency_ms = round(statistics.median(latencies), 2)
        # p90 — use nearest-rank since quantiles() needs >=2
        if len(latencies) >= 2:
            sorted_l = sorted(latencies)
            idx = max(0, int(round(0.9 * (len(sorted_l) - 1))))
            s.p90_latency_ms = round(sorted_l[idx], 2)
        else:
            s.p90_latency_ms = s.p50_latency_ms

    for r in per_query:
        s.tier_distribution[r.tier or "unknown"] = (
            s.tier_distribution.get(r.tier or "unknown", 0) + 1
        )

    s.error_count = sum(1 for r in per_query if r.error)
    return s


async def run_replay(
    queries: list[str],
    overrides: dict[str, Any] | None = None,
    memory_dir: Path | str | None = None,
    *,
    experiment_id: str | None = None,
    baseline_experiment_id: str | None = None,
    caller: str = "replay",
    max_results: int = 5,
    isolate: bool = True,
    disable_tracing: bool = True,
) -> ReplayReport:
    """Replay a list of queries under a candidate config.

    Returns a ReplayReport capturing per-query outcomes, aggregate summary, and
    a snapshot of the config values that were actually in effect during the run.

    `disable_tracing` defaults to True so replays don't pollute the live
    Langfuse project with experimental traces. Set False (Phase 2.4) when
    replay-tagged spans are the goal — `run_replay` auto-builds the
    experiment tag from `experiment_id`, `overrides`, and
    `baseline_experiment_id`, populates `langfuse_trace_url` /
    `langfuse_session_url` on the report, and flushes pending spans on exit
    so short-lived CLI processes don't lose the trace.
    """
    overrides = dict(overrides or {})
    experiment_id = experiment_id or _generate_experiment_id("exp")
    resolved_memory_dir = _resolve_memory_dir(memory_dir)

    # Phase 2.4: build the experiment tag once and pass through replay_context.
    # When disable_tracing=False, replay_context wraps the block in a tagged
    # Langfuse root span via replay_tracing.replay_root_span.
    experiment_tag: dict[str, Any] | None = None
    trace_url: str | None = None
    session_url: str | None = None
    if not disable_tracing:
        from evolve.replay_tracing import (
            build_experiment_tag,
            langfuse_session_url,
            langfuse_trace_url,
        )
        experiment_tag = build_experiment_tag(
            experiment_id, overrides, baseline_experiment_id
        )
        trace_url = langfuse_trace_url(experiment_id)
        session_url = langfuse_session_url(experiment_id)

    per_query: list[ReplayQueryResult] = []
    config_snapshot: dict[str, Any] = {}

    try:
        with replay_context(
            overrides,
            isolate=isolate,
            disable_tracing=disable_tracing,
            experiment_tag=experiment_tag,
        ):
            config_snapshot = snapshot_config(RECALL_CONFIG_KEYS)
            for query in queries:
                result = await _replay_one(
                    query,
                    memory_dir=resolved_memory_dir,
                    caller=caller,
                    max_results=max_results,
                )
                per_query.append(result)
    finally:
        # Short-lived process safety: explicitly flush pending Langfuse spans
        # so the trace lands before the CLI returns. The SDK's background
        # flusher can miss spans when the process exits immediately after
        # `run_replay_sync` resolves.
        if not disable_tracing:
            try:
                from runtime.langfuse_setup import flush_langfuse
                flush_langfuse()
            except Exception:
                pass

    report = ReplayReport(
        experiment_id=experiment_id,
        timestamp_utc=datetime.now(UTC).isoformat(),
        overrides=overrides,
        config_snapshot=config_snapshot,
        per_query=per_query,
        summary=_summarize(per_query),
        memory_dir=str(resolved_memory_dir),
        caller=caller,
        langfuse_trace_url=trace_url,
        langfuse_session_url=session_url,
    )
    return report


def write_report(report: ReplayReport, out_dir: Path | str | None = None) -> Path:
    """Persist a report to `.claude/data/evolve/reports/<experiment_id>.json`."""
    out_dir = Path(out_dir) if out_dir else (_SCRIPTS_DIR.parent / "data" / "evolve" / "reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{report.experiment_id}.json"
    path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    return path


def run_replay_sync(
    queries: list[str],
    overrides: dict[str, Any] | None = None,
    **kwargs: Any,
) -> ReplayReport:
    """Synchronous wrapper — convenience for CLI and tests."""
    return asyncio.run(run_replay(queries, overrides, **kwargs))
