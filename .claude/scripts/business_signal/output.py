"""Output stage — vault digest writer, content draft creator, daily log.

Writes the daily signal digest to the vault, creates content drafts
from high-signal items, and appends a summary to the daily log.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

from config import DRAFTS_ACTIVE_DIR, MEMORY_DIR

from business_signal.config import SIGNAL_DIR, get_signal_settings
from business_signal.draft_generator import generate_draft_copy
from business_signal.models import SignalDigest, SignalItem

logger = logging.getLogger(__name__)


async def write_signal_output(digest: SignalDigest) -> dict[str, object]:
    """Write digest + drafts + daily log entry.

    Returns ``{digest_path, drafts_created, daily_log_appended}``.
    """
    result: dict[str, object] = {
        "digest_path": None,
        "drafts_created": [],
        "daily_log_appended": False,
    }

    if not digest.items:
        return result

    digest_path = _write_digest(digest)
    result["digest_path"] = str(digest_path)

    drafts = await _create_drafts(digest)
    digest.drafts_created = drafts
    result["drafts_created"] = drafts

    _append_daily_log(digest)
    result["daily_log_appended"] = True

    return result


def _write_digest(digest: SignalDigest) -> Path:
    """Write the digest markdown to the vault."""
    path = MEMORY_DIR / "BUSINESS_SIGNAL_DIGEST.md"

    frontmatter = (
        f"---\n"
        f"tags: [signal, business-intel, auto-generated]\n"
        f"date: {digest.date}\n"
        f"sources_checked: {digest.sources_checked}\n"
        f"sources_failed: {digest.sources_failed}\n"
        f"items_triaged: {digest.total_triaged}\n"
        f"total_fetched: {digest.total_fetched}\n"
        f"---\n\n"
    )

    body = f"# Business Signal Digest — {digest.date}\n\n"

    if digest.markdown_body:
        body += digest.markdown_body + "\n\n"

    body += "## Signal Items\n\n"
    for i, item in enumerate(digest.items, 1):
        angle = f"\n- **Angle:** {item.content_angle}" if item.content_angle else ""
        body += (
            f"### {i}. {item.title}\n"
            f"- **Source:** {item.source}\n"
            f"- **Score:** {item.relevance_score:.2f}\n"
            f"- **Tags:** {', '.join(item.tags)}{angle}\n"
            f"- **URL:** {item.url}\n\n"
            f"{item.summary[:500]}\n\n"
        )

    path.write_text(frontmatter + body, encoding="utf-8")
    logger.info("Digest written to %s", path)
    return path


def _slugify(text: str) -> str:
    """Turn text into a filename-safe slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower())
    return slug.strip("-")[:40]


async def _create_drafts(digest: SignalDigest) -> list[str]:
    """Create content drafts for high-signal items with AI-drafted copy."""
    settings = get_signal_settings()
    threshold = settings.draft_threshold
    drafts: list[str] = []

    DRAFTS_ACTIVE_DIR.mkdir(parents=True, exist_ok=True)

    for i, item in enumerate(digest.items, 1):
        if item.relevance_score < threshold:
            continue

        slug = _slugify(item.title)
        filename = f"draft-{digest.date}-signal-{i:02d}-{slug}.md"
        path = DRAFTS_ACTIVE_DIR / filename

        if path.exists():
            continue

        angle = item.content_angle or "Share your take on this signal"

        # Generate AI-drafted social post copy
        draft_post = await generate_draft_copy(item)

        content = (
            f"---\n"
            f"tags: [draft, signal, content]\n"
            f"status: draft\n"
            f"date: {digest.date}\n"
            f"angle: {angle}\n"
            f"source_url: {item.url}\n"
            f"---\n\n"
            f"# {item.title}\n\n"
            f"**Source:** {item.source} | **Score:** {item.relevance_score:.2f}\n\n"
            f"## Signal\n\n"
            f"{item.summary}\n\n"
            f"## Suggested Angle\n\n"
            f"{angle}\n\n"
            f"## AI-Drafted Post\n\n"
            f"{draft_post}\n\n"
            f"## Your Perspective\n\n"
            f"_Add your take, customize, and post when ready._\n"
        )

        path.write_text(content, encoding="utf-8")
        drafts.append(filename)
        logger.info("Draft created: %s", filename)

    return drafts


def _append_daily_log(digest: SignalDigest) -> None:
    """Append a signal summary to today's daily log."""
    try:
        from shared import append_to_daily_log
    except ImportError:
        logger.warning("shared.append_to_daily_log not available")
        return

    top_items = digest.items[:3]
    lines = [f"Scanned {digest.sources_checked} sources, {digest.total_triaged} items triaged."]
    for item in top_items:
        lines.append(f"- [{item.relevance_score:.2f}] {item.title} ({item.source})")
    if digest.drafts_created:
        lines.append(f"{len(digest.drafts_created)} content draft(s) created.")

    summary = "\n".join(lines)
    append_to_daily_log(summary, "Signal Digest")
