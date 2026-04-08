"""Structured logging for recall and capture events.

Provides a RecallLog dataclass and log_recall_event() printer that
matches the existing engine.py logging format.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class RecallLog:
    """Structured log of a single recall event."""

    tier: str = ""
    caller: str = ""  # "chat" | "heartbeat" | "reflection" | "weekly"
    search_mode: str = ""  # "auto" | "keyword" | "hybrid"
    queries_generated: list[str] = field(default_factory=list)
    results_returned: int = 0
    top_scores: list[float] = field(default_factory=list)
    graph_hops_traversed: int = 0
    graph_neighbors_found: int = 0
    captures_written: int = 0
    latency_ms: float = 0.0
    expansion_model: str = ""
    expansion_latency_ms: float = 0.0
    reranked: bool = False


def log_recall_event(log: RecallLog) -> None:
    """Print structured recall log matching engine.py format."""
    scores_str = ",".join(f"{s:.2f}" for s in log.top_scores) if log.top_scores else "none"
    queries_str = str(len(log.queries_generated))

    parts = [
        f"tier={log.tier}",
    ]
    if log.caller:
        parts.append(f"caller={log.caller}")
    if log.search_mode:
        parts.append(f"mode={log.search_mode}")
    parts += [
        f"queries={queries_str}",
        f"results={log.results_returned}",
        f"top_scores=[{scores_str}]",
        f"graph_hops={log.graph_hops_traversed}",
        f"graph_neighbors={log.graph_neighbors_found}",
        f"latency={log.latency_ms:.0f}ms",
    ]

    if log.captures_written > 0:
        parts.append(f"captures={log.captures_written}")
    if log.expansion_model:
        parts.append(f"expansion_model={log.expansion_model}")
        parts.append(f"expansion_latency={log.expansion_latency_ms:.0f}ms")

    print(f"[{datetime.now()}] [Recall] {', '.join(parts)}", flush=True)


@dataclass
class PromotionLog:
    """Structured log of a promotion pipeline run."""

    candidates_total: int = 0
    candidates_eligible: int = 0
    promoted: int = 0
    rejected: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    targets: dict[str, int] = field(default_factory=dict)
    distillation_model: str = ""
    distillation_cost_usd: float = 0.0
    latency_ms: float = 0.0


@dataclass
class CompactionEvent:
    """Logged when a session is reset due to context pressure."""

    session_id: str = ""
    turn_count: int = 0
    reason: str = ""
    continuity_preserved: bool = False
    captures_flushed: int = 0
    recovery_path: str = ""
    timestamp: str = ""


def log_promotion_event(log: PromotionLog) -> None:
    """Print structured promotion log."""
    parts = [
        f"total={log.candidates_total}",
        f"eligible={log.candidates_eligible}",
        f"promoted={log.promoted}",
        f"rejected={log.rejected}",
        f"latency={log.latency_ms:.0f}ms",
    ]
    if log.targets:
        parts.append(f"targets={log.targets}")
    if log.distillation_model:
        parts.append(f"model={log.distillation_model}")
    print(f"[{datetime.now()}] [Promotion] {', '.join(parts)}", flush=True)


def log_compaction_event(event: CompactionEvent) -> None:
    """Print structured compaction log."""
    parts = [
        f"session={event.session_id}",
        f"turns={event.turn_count}",
        f"reason={event.reason}",
        f"continuity={'yes' if event.continuity_preserved else 'no'}",
        f"flushed={event.captures_flushed}",
    ]
    print(f"[{datetime.now()}] [Compaction] {', '.join(parts)}", flush=True)


# === Move 3: Process, Skill, Inference observability ===


@dataclass
class ProcessLog:
    """Mental process transition event."""

    previous_process: str = ""
    new_process: str = ""
    transition_reason: str = ""
    message_text_preview: str = ""  # first 60 chars
    session_id: str = ""


@dataclass
class SkillLog:
    """Skill generation or reuse event."""

    action: str = ""  # proposed | generated | reused | patched
    skill_name: str = ""
    category: str = ""
    tool_count: int = 0
    skill_path: str = ""


@dataclass
class InferenceLog:
    """Inference confidence change event."""

    action: str = ""  # added | strengthened | decayed | contradicted
    inference_preview: str = ""  # first 80 chars
    old_confidence: float = 0.0
    new_confidence: float = 0.0
    evidence_count: int = 0


def log_process_event(log: ProcessLog) -> None:
    """Print structured process transition log."""
    parts = [
        f"prev={log.previous_process}",
        f"new={log.new_process}",
        f"reason={log.transition_reason}",
        f'msg="{log.message_text_preview}"',
    ]
    print(f"[{datetime.now()}] [Process] {', '.join(parts)}", flush=True)


def log_skill_event(log: SkillLog) -> None:
    """Print structured skill event log."""
    parts = [
        f"action={log.action}",
        f"skill={log.skill_name}",
        f"category={log.category}",
        f"tools={log.tool_count}",
    ]
    if log.skill_path:
        parts.append(f"path={log.skill_path}")
    print(f"[{datetime.now()}] [Skill] {', '.join(parts)}", flush=True)


def log_inference_event(log: InferenceLog) -> None:
    """Print structured inference event log."""
    parts = [
        f"action={log.action}",
        f'inference="{log.inference_preview}"',
        f"confidence={log.old_confidence:.2f}->{log.new_confidence:.2f}",
        f"evidence={log.evidence_count}",
    ]
    print(f"[{datetime.now()}] [Inference] {', '.join(parts)}", flush=True)


# === Recall Log Persistence (Phase 4: Memory Graph) ===

_DEFAULT_LOG_PATH = Path(__file__).resolve().parents[2] / "data" / "state" / "recall-log.json"
_MAX_EVENTS = 50


class RecallLogStore:
    """Ring buffer of recent recall events, persisted to JSON file."""

    def __init__(self, path: Path = _DEFAULT_LOG_PATH):
        self._path = path
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, log: RecallLog) -> None:
        with self._lock:
            events = self._load()
            events.append({
                "timestamp": datetime.now(UTC).isoformat(),
                "caller": log.caller,
                "tier": log.tier,
                "queries": log.queries_generated,
                "resultsCount": log.results_returned,
                "topScores": log.top_scores,
                "graphHops": log.graph_hops_traversed,
                "graphNeighbors": log.graph_neighbors_found,
                "latencyMs": round(log.latency_ms, 1),
            })
            if len(events) > _MAX_EVENTS:
                events = events[-_MAX_EVENTS:]
            self._path.write_text(json.dumps(events, indent=2), encoding="utf-8")

    def get_recent(self, n: int = 20) -> list[dict]:
        return self._load()[-n:]

    def _load(self) -> list[dict]:
        if not self._path.exists():
            return []
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []


# === Move 5b: WorkingMemory observability ===


@dataclass
class TransformLog:
    """WM.transform() call event."""

    latency_ms: float = 0.0
    wm_size: int = 0  # Memory count
    processor: str = ""
    success: bool = True
    error: str = ""


@dataclass
class ProcessTransitionLog:
    """Mental process transition via WM-based routing."""

    from_process: str = ""
    to_process: str = ""
    trigger: str = ""
    session_id: str = ""


@dataclass
class CognitionMetrics:
    """Per-session aggregate metrics."""

    session_id: str = ""
    total_transforms: int = 0
    total_transitions: int = 0
    avg_transform_latency_ms: float = 0.0
    wm_high_water_mark: int = 0  # Max memory count seen
    recall_hit_rate: float = 0.0  # Fraction of turns with recall hits


def log_transform_event(log: TransformLog) -> None:
    """Print structured WM transform log."""
    parts = [
        f"latency={log.latency_ms:.0f}ms",
        f"wm_size={log.wm_size}",
        f"processor={log.processor}",
        f"success={log.success}",
    ]
    if log.error:
        parts.append(f"error={log.error}")
    print(f"[{datetime.now()}] [Transform] {', '.join(parts)}", flush=True)


def log_process_transition(log: ProcessTransitionLog) -> None:
    """Print structured process transition log."""
    parts = [
        f"from={log.from_process}",
        f"to={log.to_process}",
        f"trigger={log.trigger}",
    ]
    if log.session_id:
        parts.append(f"session={log.session_id}")
    print(f"[{datetime.now()}] [ProcessTransition] {', '.join(parts)}", flush=True)
