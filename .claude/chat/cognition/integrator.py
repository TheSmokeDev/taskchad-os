"""MemoryIntegrator — per-perception WM reshape.

OpenSouls runs a MemoryIntegrator on every perception before the mental
process sees the WM. It reshapes memory, manages regions, and can force
process transitions.

This is the glue between "message arrives" and "process runs."
It replaces the scattered region assembly currently in engine.py.
"""

from __future__ import annotations

from cognition.working_memory import Memory, WorkingMemory


async def integrate_perception(
    wm: WorkingMemory,
    message_text: str,
    recall_results: list | None = None,
    process_weights: dict[str, float] | None = None,
    continuity_text: str = "",
    prefetched_context: str = "",
) -> WorkingMemory:
    """Reshape WM for the current perception before the mental process runs.

    Steps:
    1. Add the user message as a Memory
    2. Add recall results to recalled_memory region
    3. Add continuity context if present
    4. Inject prefetched context if present

    Returns the reshaped WM ready for the mental process.
    """
    # 1. Add user message
    wm = wm.with_memory(Memory(
        role="user",
        content=message_text,
        region="recent_conversation",
        source="conversation",
    ))

    # 2. Add recall results
    if recall_results:
        try:
            from cognition.recall import format_recall_results
            recall_text = format_recall_results(recall_results)
            if recall_text:
                wm = wm.with_memory(Memory(
                    role="system",
                    content=recall_text,
                    region="recalled_memory",
                    source="cognition",
                    name="recall",
                ))
        except ImportError:
            pass

    # 3. Add continuity
    if continuity_text:
        wm = wm.with_memory(Memory(
            role="system",
            content=continuity_text,
            region="continuity",
            source="cognition",
            name="continuity",
        ))

    # 4. Add prefetched context
    if prefetched_context:
        # Check if already present (idempotent)
        has_prefetched = any(
            m.region == "prefetched_context" and m.source == "router"
            for m in wm.memories
        )
        if not has_prefetched:
            wm = wm.with_memory(Memory(
                role="system",
                content=prefetched_context,
                region="prefetched_context",
                source="router",
            ))

    # 5. Order regions for prompt assembly
    wm = wm.order_regions()

    return wm
