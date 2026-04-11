"""Immutable WorkingMemory — the constitutive state object.

Port of OpenSouls WorkingMemory semantics to Python:
- Frozen dataclass (Python-native immutability via dataclasses.replace)
- tuple for memories (immutable sequence)
- Every operation returns a NEW WorkingMemory
- transform() delegates to runtime/ processor via runtime_bridge

CRITICAL: Vault persistence is OUTSIDE WorkingMemory. WM is in-memory only.
Snapshot to vault at explicit boundaries (reflection, session end).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class Memory:
    """Single memory entry in WorkingMemory."""

    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    name: str | None = None
    region: str | None = None
    source: str = "conversation"  # conversation | router | tool | vault
    tool_name: str | None = None
    tool_call_id: str | None = None
    metadata: tuple[tuple[str, Any], ...] = ()
    id: str = ""
    timestamp: str = ""

    def with_region(self, region: str) -> Memory:
        """Return a copy with a different region."""
        return replace(self, region=region)

    def with_metadata(self, key: str, value: Any) -> Memory:
        """Return a copy with an additional metadata entry."""
        existing = dict(self.metadata)
        existing[key] = value
        return replace(self, metadata=tuple(existing.items()))


def _make_id() -> str:
    return str(uuid.uuid4())[:8]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class WorkingMemory:
    """Immutable conversation state. Every operation returns a new instance.

    Port of OpenSouls WorkingMemory to Python-native frozen dataclass.
    """

    soul_name: str
    memories: tuple[Memory, ...] = ()
    region_order: tuple[str, ...] = (
        "identity",
        "self_model",
        "user_model",
        "durable_memory",
        "continuity",
        "recalled_memory",
        "procedural_memory",
        "prefetched_context",
        "recent_conversation",
    )

    # --- List operations ---

    def with_memory(self, memory: Memory) -> WorkingMemory:
        """Append a memory, auto-filling id and timestamp if empty."""
        if not memory.id:
            memory = replace(memory, id=_make_id())
        if not memory.timestamp:
            memory = replace(memory, timestamp=_now_iso())
        return replace(self, memories=self.memories + (memory,))

    def with_monologue(self, text: str) -> WorkingMemory:
        """Shorthand: append an assistant memory in 'internal' region."""
        return self.with_memory(Memory(
            role="assistant", content=text, region="internal",
            source="cognition",
        ))

    def slice(self, start: int, end: int | None = None) -> WorkingMemory:
        """Return a WM with a slice of memories."""
        sliced = self.memories[start:end]
        return replace(self, memories=sliced)

    def filter(self, predicate: Callable[[Memory], bool]) -> WorkingMemory:
        """Return a WM with only memories matching predicate."""
        filtered = tuple(m for m in self.memories if predicate(m))
        return replace(self, memories=filtered)

    def concat(self, other: WorkingMemory) -> WorkingMemory:
        """Combine memories from another WM (keeps this WM's metadata)."""
        return replace(self, memories=self.memories + other.memories)

    def map(self, fn: Callable[[Memory], Memory]) -> WorkingMemory:
        """Apply fn to each memory, return new WM."""
        return replace(self, memories=tuple(fn(m) for m in self.memories))

    def prepend(self, memory: Memory) -> WorkingMemory:
        """Insert a memory at the beginning."""
        if not memory.id:
            memory = replace(memory, id=_make_id())
        if not memory.timestamp:
            memory = replace(memory, timestamp=_now_iso())
        return replace(self, memories=(memory,) + self.memories)

    # --- Region operations ---

    def with_region(self, region: str) -> WorkingMemory:
        """Return a WM where ALL memories are tagged with the given region."""
        return self.map(lambda m: replace(m, region=region))

    def without_regions(self, *regions: str) -> WorkingMemory:
        """Return a WM excluding memories in the given regions."""
        excluded = set(regions)
        return self.filter(lambda m: m.region not in excluded)

    def with_only_regions(self, *regions: str) -> WorkingMemory:
        """Return a WM with only memories in the given regions."""
        included = set(regions)
        return self.filter(lambda m: m.region in included)

    def order_regions(self) -> WorkingMemory:
        """Sort memories by region_order. Memories without a region go last."""
        order_map = {r: i for i, r in enumerate(self.region_order)}
        default_order = len(self.region_order)

        def sort_key(m: Memory) -> int:
            return order_map.get(m.region or "", default_order)

        return replace(self, memories=tuple(sorted(self.memories, key=sort_key)))

    # --- Core ---

    async def transform(
        self,
        instruction: str | Memory,
        processor: str = "claude",
        schema: dict | None = None,
        cwd: Any = None,
    ) -> tuple[WorkingMemory, Any]:
        """Send memory to LLM processor, get response, return [new_wm, value].

        Delegates to runtime_bridge.render_runtime_request() +
        runtime/lane_router.run_with_runtime_lanes() +
        runtime_bridge.apply_runtime_result().
        """
        from cognition.runtime_bridge import apply_runtime_result, render_runtime_request

        request = render_runtime_request(
            self, instruction, processor, cwd=cwd, schema=schema,
        )

        from runtime.lane_router import run_with_runtime_lanes

        result = await run_with_runtime_lanes(request)

        return apply_runtime_result(self, result, instruction=instruction)

    # --- Properties ---

    @property
    def length(self) -> int:
        return len(self.memories)

    def to_messages(self) -> list[dict[str, str]]:
        """Convert to LLM-compatible message list."""
        messages: list[dict[str, str]] = []
        for m in self.memories:
            msg: dict[str, str] = {"role": m.role, "content": m.content}
            if m.name:
                msg["name"] = m.name
            messages.append(msg)
        return messages

    def to_system_prompt(self) -> str:
        """Render system-role memories into a single prompt string.

        Groups by region in region_order, skips empty content.
        """
        ordered = self.order_regions()
        parts: list[str] = []
        seen_regions: set[str] = set()

        for m in ordered.memories:
            if m.role != "system":
                continue
            region = m.region or "default"
            if region not in seen_regions:
                seen_regions.add(region)
                header = region.replace("_", " ").title()
                parts.append(f"# {header}")
            if m.content.strip():
                parts.append(m.content.strip())

        return "\n\n".join(parts)

    def __repr__(self) -> str:
        region_counts: dict[str, int] = {}
        for m in self.memories:
            r = m.region or "default"
            region_counts[r] = region_counts.get(r, 0) + 1
        regions_str = ", ".join(f"{k}={v}" for k, v in region_counts.items())
        return f"WorkingMemory(soul={self.soul_name}, memories={self.length}, regions=[{regions_str}])"
