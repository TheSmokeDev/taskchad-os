"""Draft content generator — AI-drafted social posts from signal items.

Takes a high-signal item and generates real social media copy
(LinkedIn/X posts) or article sections ready for operator review/posting.
"""

from __future__ import annotations

import logging

from business_signal.models import SignalItem

logger = logging.getLogger(__name__)


async def generate_draft_copy(item: SignalItem) -> str:
    """Generate AI-drafted social post or article section for an item.

    Returns a ready-to-use content snippet (LinkedIn/X post style).
    Fails gracefully: returns a minimal fallback if LLM fails.
    """
    # Kill-switch guard — mirror heartbeat.py:420-441
    try:
        from security import kill_switches as _ks
        _ks.requireEnabled("llm", caller="signal_draft_generator")
    except ImportError:
        pass
    except Exception as exc:
        if exc.__class__.__name__ == "KillSwitchDisabled":
            logger.warning("signal_draft_generator skipped: kill-switch disabled")
            return _fallback_draft_prompt(item)
        raise

    try:
        import os
        from config import PROJECT_ROOT, get_background_models
        from runtime.base import RuntimeRequest
        from runtime.capabilities import TEXT_REASONING
        from runtime.lane_router import run_with_runtime_lanes

        prompt = _build_draft_prompt(item)

        # Support weekly quality-tier runs via env var (default: fast/haiku)
        model_tier = os.getenv("SIGNAL_MODEL_TIER", "fast")
        model = get_background_models()[model_tier]

        result = await run_with_runtime_lanes(
            RuntimeRequest(
                prompt=prompt,
                cwd=PROJECT_ROOT,
                task_name="signal_draft_generator",
                capability=TEXT_REASONING,
                model=model,
                max_turns=1,
                allowed_tools=[],
            )
        )

        return result.text.strip()

    except Exception as exc:
        logger.exception("draft_generator LLM call failed: %s", exc)
        # Fallback: operator-friendly prompt
        return _fallback_draft_prompt(item)


def _build_draft_prompt(item: SignalItem) -> str:
    """Build the draft generation prompt."""
    angle = item.content_angle or "a business opportunity"

    return (
        f"You are a content strategist. Generate a SHORT, punchy social media post "
        f"(LinkedIn/X style, 280 characters max, 1-2 sentences) based on this signal:\n\n"
        f"**Title:** {item.title}\n"
        f"**Summary:** {item.summary[:300]}\n"
        f"**Angle:** {angle}\n"
        f"**Source:** {item.source}\n"
        f"**URL:** {item.url}\n\n"
        f"Make it actionable, founder-focused, and ready to post. NO hashtags. "
        f"Lead with the insight, not the source.\n\n"
        f"Return ONLY the post text, no other commentary."
    )


def _fallback_draft_prompt(item: SignalItem) -> str:
    """Fallback draft prompt (operator-friendly) when LLM fails."""
    angle = item.content_angle or "Share your take"

    return (
        f"**{angle}**\n\n"
        f"{item.summary[:200]}\n\n"
        f"Source: {item.url}"
    )
