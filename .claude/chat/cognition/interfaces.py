"""Forward-compatible protocol interfaces for cognitive architecture.

Move 5a seams: defines the contracts that current wiring uses and
Move 5b's WorkingMemory-based architecture will implement.

Pattern: typing.Protocol for structural subtyping (no base class needed).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# --- Process Detection Interface ---


@runtime_checkable
class ProcessDetector(Protocol):
    """Detects the current mental process from message text.

    5a: detect_process() satisfies this.
    5b: MentalProcess-based router replaces it.
    """

    def __call__(
        self,
        text: str,
        current: Any = None,
    ) -> tuple[Any, str]:
        """Returns (process, transition_reason)."""
        ...


# --- Query Expansion Interface ---


@runtime_checkable
class QueryExpander(Protocol):
    """Expands a user message into search queries for recall.

    5a: expand_queries() (heuristic) satisfies this.
    5c: CognitiveStep.brainstorm() replaces it.
    """

    async def __call__(
        self,
        message_text: str,
        conversation_summary: str = "",
    ) -> list[str]:
        """Returns 1-3 search queries."""
        ...


# --- Memory Processor Interface ---


@runtime_checkable
class MemoryProcessor(Protocol):
    """Processes a runtime request through the provider chain.

    5a: run_with_fallback() satisfies this.
    5b: WorkingMemory.transform() wraps it via runtime_bridge.
    """

    async def __call__(self, request: Any) -> Any:
        """Returns RuntimeResult."""
        ...


# --- Snapshot Interface (5b seam) ---


@dataclass
class WMSnapshotPoint:
    """Marker for where WorkingMemory snapshots should occur.

    5a: identifies the points. 5b: implements actual snapshots.
    """

    location: str  # "startup" | "post_reflection" | "session_end"
    description: str = ""
