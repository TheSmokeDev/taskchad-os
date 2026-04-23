"""Dataclasses for the evolve replay harness."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ReplayQueryResult:
    """Outcome of replaying a single query under one configuration."""

    query: str
    tier: str = ""
    search_mode: str = ""
    results_count: int = 0
    top_scores: list[float] = field(default_factory=list)
    result_paths: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    queries_generated: list[str] = field(default_factory=list)
    graph_hops: int = 0
    graph_neighbors: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReplaySummary:
    """Aggregate metrics across a replay run (single configuration)."""

    query_count: int = 0
    hit_count: int = 0
    hit_rate: float = 0.0
    avg_top_score: float = 0.0
    p50_latency_ms: float = 0.0
    p90_latency_ms: float = 0.0
    total_latency_ms: float = 0.0
    tier_distribution: dict[str, int] = field(default_factory=dict)
    error_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReplayReport:
    """Structured output of a single replay run. Baseline and candidate each produce one."""

    experiment_id: str
    timestamp_utc: str
    overrides: dict[str, Any]
    config_snapshot: dict[str, Any]
    per_query: list[ReplayQueryResult] = field(default_factory=list)
    summary: ReplaySummary = field(default_factory=ReplaySummary)
    memory_dir: str = ""
    caller: str = "replay"

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "timestamp_utc": self.timestamp_utc,
            "overrides": self.overrides,
            "config_snapshot": self.config_snapshot,
            "memory_dir": self.memory_dir,
            "caller": self.caller,
            "summary": self.summary.to_dict(),
            "per_query": [r.to_dict() for r in self.per_query],
        }
