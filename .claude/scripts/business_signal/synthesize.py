"""Synthesis stage — LLM-powered digest markdown generation.

Takes triaged + analyzed items and produces a structured
``SignalDigest`` with a markdown summary suitable for the vault.
"""

from __future__ import annotations

import logging
from datetime import date

from business_signal.models import SignalDigest, SignalItem

logger = logging.getLogger(__name__)


async def synthesize_digest(
    items: list[SignalItem],
    digest_date: str | None = None,
    sources_checked: int = 0,
    sources_failed: int = 0,
    total_fetched: int = 0,
) -> SignalDigest:
    """Produce a ``SignalDigest`` from analyzed items.

    Uses haiku-tier LLM to generate the digest markdown body.
    Fails gracefully: returns a minimal digest on LLM failure.
    """
    if digest_date is None:
        digest_date = date.today().isoformat()

    digest = SignalDigest(
        date=digest_date,
        items=list(items),
        sources_checked=sources_checked,
        sources_failed=sources_failed,
        total_fetched=total_fetched,
        total_triaged=len(items),
    )

    if not items:
        return digest

    # Kill-switch guard
    try:
        from security import kill_switches as _ks
        _ks.requireEnabled("llm", caller="signal_synthesize")
    except ImportError:
        pass
    except Exception as exc:
        if exc.__class__.__name__ == "KillSwitchDisabled":
            logger.warning("signal_synthesize skipped: kill-switch disabled")
            return digest
        raise

    prompt = _build_synthesis_prompt(items, digest_date)

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
                task_name="signal_synthesize",
                capability=TEXT_REASONING,
                model=model,
                max_turns=1,
                allowed_tools=[],
            )
        )
        digest.markdown_body = result.text.strip()
    except Exception:
        logger.exception("signal_synthesize LLM call failed (minimal digest returned)")

    return digest


def _build_synthesis_prompt(items: list[SignalItem], digest_date: str) -> str:
    """Build the synthesis prompt."""
    item_block = ""
    for i, item in enumerate(items, 1):
        angle = f" | Angle: {item.content_angle}" if item.content_angle else ""
        item_block += (
            f"### {i}. {item.title}\n"
            f"- Source: {item.source}\n"
            f"- Score: {item.relevance_score:.2f}\n"
            f"- Tags: {', '.join(item.tags)}{angle}\n"
            f"- Summary: {item.summary[:300]}\n\n"
        )

    return (
        f"You are writing a daily business intelligence digest for {digest_date}.\n\n"
        f"Summarize the following {len(items)} signal items into a concise digest:\n"
        f"1. A 2-3 sentence executive summary of today's key signals\n"
        f"2. Top 3 items ranked by relevance with one-sentence takeaways\n"
        f"3. Content opportunities (which items could become posts/threads)\n\n"
        f"Write in markdown. Be concise and actionable.\n\n"
        f"Items:\n{item_block}"
    )
