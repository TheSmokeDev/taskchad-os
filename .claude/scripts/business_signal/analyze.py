"""Analysis stage — LLM-powered content angle assignment.

Batches all triaged items into a single haiku-tier LLM call to assign
business-relevant content angles. Fails gracefully: items are returned
without angles if the LLM call fails (still usable downstream).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from business_signal.focus import ChannelFocus, default_focus
from business_signal.models import SignalItem

logger = logging.getLogger(__name__)


async def analyze_items(
    items: list[SignalItem],
    focus: ChannelFocus | None = None,
) -> list[SignalItem]:
    """Add ``content_angle`` to each item via a batched LLM call.

    Returns the same items list (mutated in place) regardless of
    success or failure — failure just leaves ``content_angle`` as None.
    """
    if not items:
        return items

    # Kill-switch guard — mirror heartbeat.py:420-441
    try:
        from security import kill_switches as _ks
        _ks.requireEnabled("llm", caller="signal_analyze")
    except ImportError:
        pass
    except Exception as exc:
        if exc.__class__.__name__ == "KillSwitchDisabled":
            logger.warning("signal_analyze skipped: kill-switch disabled")
            return items
        raise

    if focus is None:
        focus = default_focus()

    prompt = _build_analysis_prompt(items, focus)

    try:
        import os
        from config import PROJECT_ROOT, get_background_models
        from runtime.base import RuntimeRequest
        from runtime.capabilities import TEXT_REASONING
        from runtime.lane_router import run_with_runtime_lanes

        # Support weekly quality-tier runs via env var (default: fast/haiku)
        model_tier = os.getenv("SIGNAL_MODEL_TIER", "fast")
        model = get_background_models()[model_tier]

        result = await run_with_runtime_lanes(
            RuntimeRequest(
                prompt=prompt,
                cwd=PROJECT_ROOT,
                task_name="signal_analyze",
                capability=TEXT_REASONING,
                model=model,
                max_turns=1,
                allowed_tools=[],
            )
        )
        _apply_angles(items, result.text)
    except Exception:
        logger.exception("signal_analyze LLM call failed (items returned without angles)")

    return items


def _build_analysis_prompt(items: list[SignalItem], focus: ChannelFocus) -> str:
    """Build the batched analysis prompt."""
    verticals = ", ".join(sorted(focus.high_keywords)[:8])

    item_block = ""
    for i, item in enumerate(items, 1):
        item_block += f"[{i}] {item.title}\n    {item.summary[:200]}\n    Tags: {', '.join(item.tags)}\n\n"

    return (
        f"You are a business intelligence analyst. The operator's focus verticals: {verticals}.\n\n"
        f"For each numbered item below, suggest ONE short content angle (2-8 words) "
        f"explaining how the operator could create content about this signal. "
        f"Think: what angle would resonate on LinkedIn/X for a founder audience?\n\n"
        f"Return a JSON array of objects: [{{\"index\": 1, \"angle\": \"...\"}}]\n"
        f"Return ONLY the JSON array, no other text.\n\n"
        f"Items:\n{item_block}"
    )


def _apply_angles(items: list[SignalItem], llm_text: str) -> None:
    """Parse LLM response and apply angles to items."""
    text = llm_text.strip()
    # Strip markdown fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.startswith("```")]
        text = "\n".join(lines)

    try:
        angles = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Failed to parse analysis response as JSON")
        return

    if not isinstance(angles, list):
        return

    for entry in angles:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("index")
        angle = entry.get("angle", "")
        if isinstance(idx, int) and 1 <= idx <= len(items) and angle:
            items[idx - 1].content_angle = str(angle)
