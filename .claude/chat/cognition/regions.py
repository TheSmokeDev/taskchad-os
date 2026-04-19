"""9-region prompt assembly with per-region token budgets.

Regions are assembled in a fixed order with each region capped at a
character budget (~4 chars/token). Truncation is always explicit —
never silently drops content.

Pattern: PRD region spec. OpenSouls memory regions research.
"""

from __future__ import annotations

from dataclasses import dataclass

CHARS_PER_TOKEN = 4  # Same heuristic as memory_index.py

# Default budgets in characters (~4 chars/token)
DEFAULT_REGION_BUDGETS: dict[str, int] = {
    "identity": 16000,          # ~4K tokens — SOUL.md
    "self_model": 8000,         # ~2K tokens — SELF.md
    "user_model": 12000,        # ~3K tokens — USER.md
    "durable_memory": 16000,    # ~4K tokens — MEMORY.md
    "continuity": 8000,         # ~2K tokens — active context (stub Move 1)
    "recalled_memory": 16000,   # ~4K tokens — tiered recall results
    "procedural_memory": 8000,  # ~2K tokens — skills index (stub Move 1)
    "prefetched_context": 24000,  # ~6K tokens — router pre-fetch
    "recent_conversation": 600,  # ~2.4K chars — last 4-6 turns, engine-injected
}


@dataclass
class PromptRegion:
    """A single region in the structured prompt."""

    name: str
    content: str
    max_tokens: int  # Budget in tokens (converted to chars internally)
    frozen: bool = False
    source: str = ""

    @property
    def max_chars(self) -> int:
        return self.max_tokens * CHARS_PER_TOKEN


def truncate_region(region: PromptRegion) -> str:
    """Truncate content to token budget. Add warning if truncated.

    Cuts at last newline before budget limit to avoid mid-line breaks.
    Never silently drops — always appends truncation warning.
    """
    content = region.content
    if not content:
        return ""

    budget = region.max_chars
    if budget <= 0 or len(content) <= budget:
        return content

    # Cut at last newline before budget
    truncated = content[:budget]
    last_newline = truncated.rfind("\n")
    if last_newline > budget // 2:
        truncated = truncated[:last_newline]

    chars_over = len(content) - len(truncated)
    tokens_over = chars_over // CHARS_PER_TOKEN
    truncated += f"\n[TRUNCATED: ~{tokens_over} tokens over budget for {region.name}]"

    return truncated


def apply_process_weights(
    base_budgets: dict[str, int],
    weights: dict[str, float],
    min_weight: float = 0.5,
    max_weight: float = 2.0,
) -> dict[str, int]:
    """Apply mental process weight multipliers to region budgets.

    CRITICAL: Clamp weights to [min_weight, max_weight] to prevent starvation.
    Returns adjusted budgets as a new dict.
    """
    adjusted = {}
    for region, budget in base_budgets.items():
        w = weights.get(region, 1.0)
        w = max(min_weight, min(max_weight, w))
        adjusted[region] = max(0, int(budget * w))
    return adjusted


def build_initial_working_memory(
    soul_name: str,
    vault_files: dict[str, str],
    skill_index: str = "",
    prefetched_context: str = "",
    recent_conversation: list[dict[str, str]] | None = None,
) -> WorkingMemory:
    """Build a WorkingMemory from vault files and context.

    Move 5b: Regions become the initial WM state factory.
    Loads SOUL.md, SELF.md, USER.md, MEMORY.md as frozen Memory objects
    in named regions. Preserves router-supplied context explicitly.
    """
    from cognition.working_memory import Memory, WorkingMemory

    wm = WorkingMemory(soul_name=soul_name)

    region_file_map = {
        "identity": "SOUL.md",
        "self_model": "SELF.md",
        "user_model": "USER.md",
        "durable_memory": "MEMORY.md",
    }

    for region, filename in region_file_map.items():
        content = vault_files.get(filename, "")
        if content:
            wm = wm.with_memory(Memory(
                role="system",
                content=content,
                region=region,
                source="vault",
                name=filename,
            ))

    if skill_index:
        wm = wm.with_memory(Memory(
            role="system",
            content=skill_index,
            region="procedural_memory",
            source="vault",
            name="skill_index",
        ))

    if prefetched_context:
        wm = wm.with_memory(Memory(
            role="system",
            content=prefetched_context,
            region="prefetched_context",
            source="router",
        ))

    if recent_conversation:
        for msg in recent_conversation:
            wm = wm.with_memory(Memory(
                role=msg.get("role", "user"),
                content=msg.get("content", ""),
                region="recent_conversation",
                source="conversation",
            ))

    return wm


def assemble_regions(regions: list[PromptRegion]) -> str:
    """Assemble all regions into a single prompt string.

    Regions are ordered by list position (caller ensures correct order).
    Empty regions are skipped. Each region is wrapped with a header.
    """
    parts: list[str] = []

    for region in regions:
        content = truncate_region(region)
        if not content.strip():
            continue

        # Format region name as a readable header
        header = region.name.replace("_", " ").title()
        parts.append(f"# {header}\n{content}")

    return "\n\n---\n\n".join(parts)
