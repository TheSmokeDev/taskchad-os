"""Minimal reasoning_step interface for background cognition tasks.

Wraps the runtime layer for cheap, single-turn LLM calls used by
promotion distillation, continuity rollups, and future enrichment.
No tools - TEXT_REASONING only.

Pattern: memory_reflect.py run_with_runtime_lanes() call.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ReasoningStepResult:
    """Result from a single reasoning step."""

    output_text: str
    parsed: dict | list | None
    model: str
    cost_usd: float
    latency_ms: float


def _extract_json(text: str) -> dict | list | None:
    """Extract JSON from LLM response, handling markdown code blocks."""
    text = text.strip()
    # Try raw parse first
    try:
        result = json.loads(text)
        if isinstance(result, (dict, list)):
            return result
    except (json.JSONDecodeError, ValueError):
        pass
    # Try extracting from ```json ... ``` block
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group(1).strip())
            if isinstance(result, (dict, list)):
                return result
        except (json.JSONDecodeError, ValueError):
            pass
    return None


async def reasoning_step(
    context: str,
    instruction: str,
    output_schema: dict | None = None,
    cwd: Path | None = None,
) -> ReasoningStepResult:
    """Run a single LLM reasoning operation. No tools, cheapest model.

    CRITICAL: TEXT_REASONING only - never TOOL_REASONING for distillation.
    Pattern: memory_reflect.py run_with_runtime_lanes() call.
    """
    from runtime.base import RuntimeRequest
    from runtime.capabilities import TEXT_REASONING
    from runtime.lane_router import run_with_runtime_lanes

    start = time.monotonic()

    # Build prompt: if output_schema, wrap instruction with JSON format request
    prompt = instruction
    if output_schema:
        prompt += f"\n\nRespond with ONLY valid JSON matching: {json.dumps(output_schema)}"

    system_prompt = {
        "type": "preset",
        "preset": "claude_code",
        "append": context,
    } if context else None

    result = await run_with_runtime_lanes(RuntimeRequest(
        prompt=prompt,
        cwd=cwd or Path.cwd(),
        task_name="reasoning_step",
        capability=TEXT_REASONING,
        max_turns=1,
        max_budget_usd=0.10,
        allowed_tools=[],
        system_prompt=system_prompt,
    ))

    elapsed_ms = (time.monotonic() - start) * 1000
    parsed = None
    if output_schema:
        parsed = _extract_json(result.text)

    return ReasoningStepResult(
        output_text=result.text.strip(),
        parsed=parsed,
        model=result.model,
        cost_usd=result.cost_usd or 0.0,
        latency_ms=elapsed_ms,
    )


# === Move 3: Typed cognitive operations ===


@dataclass
class CognitiveContext:
    """Accumulated state across cognitive steps within a turn."""

    session_id: str = ""
    turn_number: int = 0
    active_process: str = "default"  # default|planning|monitoring|learning|execution
    step_history: list[dict[str, Any]] = field(default_factory=list)
    internal_thoughts: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)


@dataclass
class CognitiveStepResult:
    """Typed output from a single cognitive step."""

    step_type: str  # reflect|recall|respond|decide|brainstorm|query
    output: Any  # str | list[str] | bool | list[RecallResult]
    latency_ms: float = 0.0
    model: str = ""
    cost_usd: float = 0.0


async def cognitive_reflect(
    ctx: CognitiveContext,
    prompt: str,
    cwd: Path | None = None,
) -> tuple[CognitiveContext, CognitiveStepResult]:
    """Private reasoning. Not shown to user."""
    result = await reasoning_step(
        context=f"Active mode: {ctx.active_process}",
        instruction=f"Think internally about: {prompt}. Do NOT produce user-facing output.",
        cwd=cwd,
    )
    ctx.internal_thoughts.append(result.output_text)
    ctx.step_history.append({"type": "reflect", "output": result.output_text[:200]})
    return ctx, CognitiveStepResult(
        "reflect", result.output_text, result.latency_ms, result.model, result.cost_usd,
    )


async def cognitive_recall(
    ctx: CognitiveContext,
    queries: list[str],
    memory_dir: Path,
    max_results: int = 5,
) -> tuple[CognitiveContext, CognitiveStepResult]:
    """Wrap existing recall pipeline. NO LLM call."""
    from cognition.recall import RecallTier, run_recall_pipeline

    results, log = await run_recall_pipeline(
        queries[0] if queries else "",
        RecallTier.TIER_1,
        memory_dir,
        max_results=max_results,
    )
    ctx.step_history.append({"type": "recall", "results": len(results)})
    return ctx, CognitiveStepResult("recall", results, log.latency_ms)


async def cognitive_respond(
    ctx: CognitiveContext,
    instruction: str,
    cwd: Path | None = None,
) -> tuple[CognitiveContext, CognitiveStepResult]:
    """Generate user-facing response."""
    thought_context = "\n".join(ctx.internal_thoughts[-3:]) if ctx.internal_thoughts else ""
    result = await reasoning_step(context=thought_context, instruction=instruction, cwd=cwd)
    ctx.step_history.append({"type": "respond", "output": result.output_text[:200]})
    return ctx, CognitiveStepResult(
        "respond", result.output_text, result.latency_ms, result.model, result.cost_usd,
    )


async def cognitive_decide(
    ctx: CognitiveContext,
    options: list[str],
    cwd: Path | None = None,
) -> tuple[CognitiveContext, CognitiveStepResult]:
    """Choose between options."""
    options_str = "\n".join(f"- {o}" for o in options)
    result = await reasoning_step(
        context="",
        instruction=f"Choose ONE:\n{options_str}\nRespond with only the chosen option.",
        cwd=cwd,
    )
    choice = result.output_text.strip()
    ctx.decisions.append(choice)
    ctx.step_history.append({"type": "decide", "output": choice})
    return ctx, CognitiveStepResult(
        "decide", choice, result.latency_ms, result.model, result.cost_usd,
    )


async def cognitive_brainstorm(
    ctx: CognitiveContext,
    prompt: str,
    n: int = 3,
    cwd: Path | None = None,
) -> tuple[CognitiveContext, CognitiveStepResult]:
    """Generate N ideas."""
    result = await reasoning_step(
        context="",
        instruction=f"Generate {n} ideas for: {prompt}. JSON array of strings.",
        output_schema={"type": "array"},
        cwd=cwd,
    )
    ideas = result.parsed if isinstance(result.parsed, list) else [result.output_text]
    ctx.step_history.append({"type": "brainstorm", "output": len(ideas)})
    return ctx, CognitiveStepResult(
        "brainstorm", ideas, result.latency_ms, result.model, result.cost_usd,
    )


async def cognitive_query(
    ctx: CognitiveContext,
    question: str,
    cwd: Path | None = None,
) -> tuple[CognitiveContext, CognitiveStepResult]:
    """Yes/no classification."""
    result = await reasoning_step(
        context="",
        instruction=f"Answer YES or NO only: {question}",
        cwd=cwd,
    )
    answer = result.output_text.strip().lower().startswith("yes")
    ctx.step_history.append({"type": "query", "output": answer})
    return ctx, CognitiveStepResult(
        "query", answer, result.latency_ms, result.model, result.cost_usd,
    )


# === Move 5b: WorkingMemory-based CognitiveStep factory ===


def create_cognitive_step(
    command: str | Callable,
    schema: dict | None = None,
    post_process: Callable | None = None,
) -> Callable:
    """Factory for typed cognitive transformations.

    Pattern: OpenSouls createCognitiveStep, Python-native.

    Args:
        command: Instruction string or callable(wm) -> Memory
        schema: JSON schema for structured output
        post_process: Optional (wm, value) -> (Memory, value) transform

    Returns:
        async (wm, arg?) -> (WorkingMemory, value)
    """

    async def step(
        wm: Any,
        arg: Any = None,
    ) -> tuple[Any, Any]:

        # Build instruction
        if callable(command):
            instruction = command(wm)
        else:
            instruction = command
            if arg:
                instruction = f"{command}\n\nContext: {arg}"

        # Transform through WM
        new_wm, value = await wm.transform(
            instruction=instruction,
            schema=schema,
        )

        # Apply post-processing if provided
        if post_process and value is not None:
            processed_memory, value = post_process(new_wm, value)
            if processed_memory:
                new_wm = new_wm.with_memory(processed_memory)

        return new_wm, value

    step.__name__ = f"cognitive_step({command[:30] if isinstance(command, str) else 'fn'})"
    return step


# Built-in steps (5b)
external_dialog = create_cognitive_step(command="Respond to the user conversationally.")
internal_monologue = create_cognitive_step(
    command="Think internally about the current situation. Do NOT produce user-facing output.",
)
brainstorm = create_cognitive_step(
    command="Generate ideas.",
    schema={"type": "array", "items": {"type": "string"}},
)
decide = create_cognitive_step(
    command="Choose one option from the given choices.",
    schema={"type": "object", "properties": {"choice": {"type": "string"}}},
)
mental_query = create_cognitive_step(
    command="Answer YES or NO only.",
    schema={"type": "object", "properties": {"answer": {"type": "boolean"}}},
)
