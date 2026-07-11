"""Reusable LinkedIn workshop operations over the social content factory.

The chat layer owns conversation state and buttons. This module owns the
durable draft mutations so Telegram, Discord, CLI, and future GUI surfaces can
share the same queue rows without duplicating generation logic.
"""

from __future__ import annotations

from pathlib import Path

from social.audit import append_social_audit_record
from social.channels import get_channel
from social.models import SocialPost
from social.service import SocialPostService


def _editable_linkedin_post(
    post_id: int, *, db_path: str | Path | None = None
) -> tuple[SocialPostService, SocialPost]:
    svc = SocialPostService(db_path=db_path)
    post = svc.get_post(post_id)
    if post is None:
        raise ValueError(f"Post {post_id} not found")
    if post.channel.lower() not in {"linkedin", "li"}:
        raise ValueError(f"Post {post_id} is not a LinkedIn draft")
    if post.status != "draft":
        raise ValueError(
            f"Post {post_id} is already '{post.status}' and can no longer be edited"
        )
    return svc, post


def create_linkedin_draft(
    *,
    topic: str | None,
    mode: str,
    db_path: str | Path | None = None,
) -> SocialPost:
    """Create one image-backed, approval-gated LinkedIn draft."""

    from social.content_factory import produce

    normalized_mode = "run" if mode == "run" else "cook"
    summary = produce(
        "linkedin",
        count=1,
        media="image",
        topic=(topic or "").strip() or None,
        topic_source=f"linkedin-workshop:{normalized_mode}",
        autopilot=False,
        db_path=str(db_path) if db_path is not None else None,
    )
    queued = summary.get("queued") or []
    if not queued:
        raise RuntimeError(summary.get("error") or "LinkedIn draft generation failed")
    post = SocialPostService(db_path=db_path).get_post(int(queued[0]))
    if post is None:
        raise RuntimeError("LinkedIn draft was queued but could not be reloaded")
    return post


def revise_linkedin_copy(
    post_id: int,
    feedback: str,
    *,
    db_path: str | Path | None = None,
) -> SocialPost:
    """Revise one draft in place while preserving its approved media."""

    feedback = (feedback or "").strip()
    if not feedback:
        raise ValueError("Revision feedback is required")
    svc, post = _editable_linkedin_post(post_id, db_path=db_path)

    from social import draft_generator as dg

    constraints = dg.CHANNEL_CONSTRAINTS["linkedin"]
    voice_context = dg._read_voice_context(post.voice_profile)
    prompt = f"""Revise this LinkedIn draft using the operator's feedback.

## Existing draft
{post.body}

## Operator feedback
{feedback}

## Voice
{voice_context if voice_context else "Confident, natural, specific, and free of corporate jargon."}

## Rules
- Return only the complete revised post.
- Maximum {constraints['max_chars']} characters.
- Preserve true specifics already present.
- Do not invent metrics, names, quotes, customers, results, or experiences.
- No engagement-bait CTA and no em/en dashes.
"""
    revised = (dg._invoke_runtime(prompt) or "").strip()
    if not revised:
        raise RuntimeError("The revision runtime returned an empty draft")
    revised = revised[: constraints["max_chars"]]
    title = revised[:60].replace("\n", " ")
    updated = svc.set_post_fields(post_id, body=revised, title=title)
    append_social_audit_record(
        channel="linkedin",
        action="revise",
        post_id=post_id,
        outcome="updated",
        operator="operator-workshop",
        body_preview=revised,
    )
    return updated


def regenerate_linkedin_image(
    post_id: int,
    direction: str,
    *,
    db_path: str | Path | None = None,
) -> SocialPost:
    """Render and attach a fresh image to an editable LinkedIn draft."""

    direction = (direction or "").strip()
    if not direction:
        raise ValueError("Image direction is required")
    svc, post = _editable_linkedin_post(post_id, db_path=db_path)
    channel = get_channel("linkedin")
    if channel is None:
        raise RuntimeError("LinkedIn channel is not configured")

    from social.content_factory import _render_image

    if direction.lower() in {"surprise", "surprise me", "fresh", "redo"}:
        direction = "Create a fresh, distinct visual interpretation of this post"
    prompt = f"{direction}. Post context: {post.body[:1200]}"
    media_path = _render_image(
        "linkedin",
        prompt,
        design_file=channel.design_file,
        persona_pack=channel.persona_pack,
    )
    if not media_path:
        raise RuntimeError("LinkedIn image generation returned no image")
    updated = svc.set_post_fields(
        post_id,
        media_path=media_path,
        media_type="image",
    )
    append_social_audit_record(
        channel="linkedin",
        action="media_regenerate",
        post_id=post_id,
        outcome="updated",
        operator="operator-workshop",
    )
    return updated
