"""Blog extension handlers — publish drafts and check pipeline status.

These are router-level commands (instant response, no engine needed).
Blog GENERATION is handled by the /blog engine command + blog-pipeline skill.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import httpx

SUPABASE_URL = os.getenv("QM_SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("QM_SUPABASE_SERVICE_KEY", "")
SITE_URL = "https://www.your-business.example.com"


def _supabase_headers() -> dict[str, str]:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _get_draft(draft_id: str) -> dict | None:
    """Fetch a single blog draft by ID."""
    resp = httpx.get(
        f"{SUPABASE_URL}/rest/v1/blog_drafts?id=eq.{draft_id}&select=id,title,slug,status",
        headers=_supabase_headers(),
        timeout=10,
    )
    if resp.status_code != 200:
        return None
    rows = resp.json()
    return rows[0] if rows else None


def _publish_draft(draft_id: str) -> dict | None:
    """Set a draft to published status."""
    now = datetime.now(timezone.utc).isoformat()
    resp = httpx.patch(
        f"{SUPABASE_URL}/rest/v1/blog_drafts?id=eq.{draft_id}",
        headers=_supabase_headers(),
        json={"status": "published", "published_at": now},
        timeout=10,
    )
    if resp.status_code not in (200, 204):
        return None
    rows = resp.json() if resp.text.strip() else []
    return rows[0] if rows else {"id": draft_id, "status": "published"}


async def handle_publish(
    adapter: Any,
    incoming: Any,
    args: str,
    *,
    collect_only: bool = False,
) -> str:
    """Publish a blog draft by ID. Usage: /publish <draft_id>"""
    draft_id = args.strip()
    if not draft_id:
        return "Usage: `/publish <draft_id>` — publish a blog draft to the live site."

    # Verify draft exists
    draft = _get_draft(draft_id)
    if not draft:
        return f"Draft `{draft_id}` not found."

    if draft.get("status") == "published":
        title = draft.get("title", {})
        en_title = title.get("en", title) if isinstance(title, dict) else title
        return f"Already published: **{en_title}**"

    # Publish it
    result = _publish_draft(draft_id)
    if not result:
        return f"Failed to publish draft `{draft_id}`. Check Supabase connection."

    # Build the live URL
    title = draft.get("title", {})
    slug = draft.get("slug", {})
    en_title = title.get("en", str(title)) if isinstance(title, dict) else str(title)
    en_slug = slug.get("en", str(slug)) if isinstance(slug, dict) else str(slug)
    live_url = f"{SITE_URL}/en/learn/{en_slug}"

    return (
        f"Published: **{en_title}**\n"
        f"Live at: {live_url}\n"
        f"Admin: admin.your-business.example.com > Blog"
    )


async def handle_blog_status(
    adapter: Any,
    incoming: Any,
    args: str,
    *,
    collect_only: bool = False,
) -> str:
    """Show blog pipeline status — counts by status, recent drafts."""
    try:
        resp = httpx.get(
            f"{SUPABASE_URL}/rest/v1/blog_drafts?select=id,title,status,seo_score,created_at&order=created_at.desc&limit=50",
            headers=_supabase_headers(),
            timeout=10,
        )
        if resp.status_code != 200:
            return f"Failed to fetch blog drafts: {resp.status_code}"

        drafts = resp.json()
    except Exception as e:
        return f"Error connecting to Supabase: {e}"

    if not drafts:
        return "No blog drafts found. Use `/blog <topic>` to generate one."

    # Count by status
    counts: dict[str, int] = {}
    scores: list[float] = []
    for d in drafts:
        status = d.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
        score = d.get("seo_score")
        if score is not None:
            scores.append(float(score))

    total = len(drafts)
    avg_score = sum(scores) / len(scores) if scores else 0
    status_str = " | ".join(f"{s}: {c}" for s, c in sorted(counts.items()))

    # Recent 3
    recent_lines: list[str] = []
    for d in drafts[:3]:
        title = d.get("title", "Untitled")
        if isinstance(title, dict):
            title = title.get("en", "Untitled")
        score = d.get("seo_score", "—")
        status = d.get("status", "?")
        recent_lines.append(f"  - {title} [{status}] (SEO: {score})")

    recent = "\n".join(recent_lines) if recent_lines else "  (none)"

    return (
        f"**Blog Pipeline** — {total} total, avg SEO: {avg_score:.0f}/100\n"
        f"{status_str}\n\n"
        f"**Recent:**\n{recent}"
    )
