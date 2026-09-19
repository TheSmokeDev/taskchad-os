"""Canonical WorkingMemory <-> runtime adapter.

The ONLY legal path for converting WM state to RuntimeRequest and back.
No other module should assemble RuntimeRequest directly from engine state.

If runtime/ later accepts structured messages, this file is the only
place that changes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from cognition.working_memory import Memory, WorkingMemory

# Processor name -> model hint mapping (runtime profiles handle actual selection)
_PROCESSOR_MODEL_HINTS: dict[str, str] = {
    "claude": None,  # Use default profile
    "fast": "claude-haiku-4-5",
    "quality": "claude-sonnet-5",
}


def render_runtime_request(
    wm: WorkingMemory,
    instruction: str | Memory,
    processor: str,
    *,
    cwd: Path | str | None = None,
    schema: dict | None = None,
) -> Any:
    """Convert WorkingMemory + instruction into a RuntimeRequest.

    This is the temporary compatibility bridge between WM-owned state
    and today's prompt-first runtime contract. WorkingMemory stays the
    source of truth.
    """
    from runtime.base import RuntimeRequest
    from runtime.capabilities import TEXT_REASONING

    # Build instruction text
    if isinstance(instruction, Memory):
        prompt = instruction.content
    else:
        prompt = instruction

    if schema:
        prompt += (
            "\n\nRespond with ONLY valid JSON matching: "
            f"{json.dumps(schema)}"
        )

    # Build system prompt from WM's system-role memories
    system_text = wm.to_system_prompt()

    # Include recent conversation history in the prompt
    # (non-system memories that represent the conversation trace)
    conversation_parts: list[str] = []
    for m in wm.memories:
        if m.role in ("user", "assistant", "tool") and m.content.strip():
            prefix = {"user": "User", "assistant": "Assistant", "tool": "Tool"}
            label = prefix.get(m.role, m.role.title())
            if m.tool_name:
                label = f"Tool ({m.tool_name})"
            conversation_parts.append(f"{label}: {m.content.strip()}")

    if conversation_parts:
        conversation_context = "\n\n".join(conversation_parts)
        system_text = (
            f"{system_text}\n\n# Recent Conversation\n{conversation_context}"
            if system_text
            else f"# Recent Conversation\n{conversation_context}"
        )

    system_prompt: dict[str, Any] | str | None = None
    if system_text:
        system_prompt = {
            "type": "preset",
            "preset": "claude_code",
            "append": system_text,
        }

    # Wire processor to model hint for battery selection
    from runtime.selection import resolve_runtime_selection
    import config

    model_hint = (
        _PROCESSOR_MODEL_HINTS.get(processor)
        if resolve_runtime_selection().lane == "claude_native"
        else None
    )

    return RuntimeRequest(
        prompt=prompt,
        cwd=cwd or Path.cwd(),
        task_name="wm_transform",
        capability=TEXT_REASONING,
        model=model_hint,
        max_turns=1,
        allowed_tools=[],
        model_only=True,
        disallowed_tools=["*"],
        mcp_servers=[],
        setting_sources=[],
        max_budget_usd=config.CHAT_MAX_BUDGET_USD,
        system_prompt=system_prompt,
        metadata={"cognitive_generated": True, "learning_role": "foreground_cognition"},
    )


def apply_runtime_result(
    wm: WorkingMemory,
    result: Any,
    *,
    instruction: str | Memory,
) -> tuple[WorkingMemory, Any]:
    """Append assistant output back into WM and return [new_wm, value].

    The instruction is appended as a user message (if string) and the
    result as an assistant message. This preserves the conversation
    trace inside WM.
    """
    # Append instruction as user memory (if it was a string command)
    if isinstance(instruction, str):
        wm = wm.with_memory(Memory(
            role="user",
            content=instruction,
            region="recent_conversation",
            source="cognition",
        ))

    # Append assistant response
    response_text = result.text.strip() if hasattr(result, "text") else str(result)
    receipt = {
        key: getattr(result, key, None)
        for key in ("runtime_lane", "provider", "model", "session_id", "execution_time_ms")
    }
    receipt.update(
        output_hash=hashlib.sha256(response_text.encode("utf-8")).hexdigest(),
        success=bool(receipt.get("provider") and receipt.get("model")),
    )
    retained = [m for m in wm.memories if m.region == "persona_learning"]
    if len(retained) == 1:
        receipt["retained_context_hash"] = dict(retained[0].metadata).get("context_hash")
    wm = wm.with_memory(Memory(
        role="assistant",
        content=response_text,
        region="recent_conversation",
        source="cognition",
        metadata=(("runtime_receipt", receipt),),
    ))

    # Extract structured value if JSON
    value: Any = response_text
    try:
        parsed = json.loads(response_text)
        if isinstance(parsed, (dict, list)):
            value = parsed
    except (json.JSONDecodeError, ValueError):
        # Try extracting from ```json ``` block
        import re
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", response_text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(1).strip())
                if isinstance(parsed, (dict, list)):
                    value = parsed
            except (json.JSONDecodeError, ValueError):
                pass

    return wm, value
