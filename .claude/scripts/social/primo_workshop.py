"""Durable operations for the approval-gated Primo X workshop."""

from __future__ import annotations

from pathlib import Path

from social.audit import append_social_audit_record
from social.channels import get_channel
from social.models import SocialPost
from social.service import SocialPostService

_IMAGE_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}


class PrimoImageRequiredError(RuntimeError):
    """An explicit image request produced a text-only draft."""

    def __init__(self, post_id: int) -> None:
        self.post_id = post_id
        super().__init__("Primo image generation returned no readable image")


def _editable_primo_post(
    post_id: int, *, db_path: str | Path | None = None
) -> tuple[SocialPostService, SocialPost]:
    svc = SocialPostService(db_path=db_path)
    post = svc.get_post(post_id)
    if post is None:
        raise ValueError(f"Post {post_id} not found")
    if post.channel.lower() not in {"x", "twitter"}:
        raise ValueError(f"Post {post_id} is not a Primo X draft")
    if post.status != "draft":
        raise ValueError(
            f"Post {post_id} is already '{post.status}' and can no longer be edited"
        )
    return svc, post


def _has_readable_image(post: SocialPost) -> bool:
    if post.media_type != "image" or not post.media_path:
        return False
    path = Path(post.media_path).expanduser()
    return path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES


def create_primo_draft(
    *,
    topic: str | None,
    mode: str,
    media_mode: str,
    db_path: str | Path | None = None,
) -> SocialPost:
    """Create one queue-backed Primo draft; never approve or dispatch it."""

    from social.content_factory import produce

    normalized_mode = "run" if mode == "run" else "cook"
    normalized_media = (media_mode or "auto").strip().lower()
    if normalized_media not in {"none", "image", "auto"}:
        raise ValueError(f"Unsupported Primo media mode: {media_mode}")

    # content_factory's generic auto mode is video-first. Primo v1 is image or
    # text only, so both Image and Auto-Decide request an image explicitly;
    # Auto-Decide is allowed to use the factory's text-only fallback.
    requested_media = "none" if normalized_media == "none" else "image"
    summary = produce(
        "x",
        count=1,
        media=requested_media,
        topic=(topic or "").strip() or None,
        topic_source=f"primo-workshop:{normalized_mode}:{normalized_media}",
        autopilot=False,
        db_path=str(db_path) if db_path is not None else None,
    )
    queued = summary.get("queued") or []
    if not queued:
        raise RuntimeError(summary.get("error") or "Primo draft generation failed")
    post = SocialPostService(db_path=db_path).get_post(int(queued[0]))
    if post is None:
        raise RuntimeError("Primo draft was queued but could not be reloaded")
    if normalized_media == "image" and not _has_readable_image(post):
        raise PrimoImageRequiredError(post.id)
    return post


def revise_primo_copy(
    post_id: int,
    feedback: str,
    *,
    db_path: str | Path | None = None,
) -> SocialPost:
    """Revise copy in place while preserving the selected image."""

    feedback = (feedback or "").strip()
    if not feedback:
        raise ValueError("Revision feedback is required")
    svc, post = _editable_primo_post(post_id, db_path=db_path)

    from social import draft_generator as dg

    constraints = dg.CHANNEL_CONSTRAINTS["x"]
    voice_context = dg._read_voice_context(post.voice_profile)
    prompt = f"""Revise this Primo X post using the operator's feedback.

## Existing draft
{post.body}

## Operator feedback
{feedback}

## Voice
{voice_context if voice_context else "Crypto-native, technically credible, sharp, and natural."}

## Rules
- Return only the complete revised post.
- Maximum {constraints['max_chars']} characters.
- Preserve true specifics already present.
- Do not invent prices, returns, metrics, customers, quotes, or results.
- No price promises, engagement bait, or em/en dashes.
"""
    revised = (dg._invoke_runtime(prompt) or "").strip()
    if not revised:
        raise RuntimeError("The revision runtime returned an empty draft")
    revised = revised[: constraints["max_chars"]]
    updated = svc.set_post_fields(
        post_id,
        body=revised,
        title=revised[:60].replace("\n", " "),
    )
    append_social_audit_record(
        channel="x",
        action="revise",
        post_id=post_id,
        outcome="updated",
        operator="primo-workshop",
        body_preview=revised,
    )
    return updated


def regenerate_primo_image(
    post_id: int,
    direction: str,
    *,
    db_path: str | Path | None = None,
) -> SocialPost:
    """Generate and attach a fresh, versioned Primo image."""

    direction = (direction or "").strip()
    if not direction:
        raise ValueError("Image direction is required")
    svc, post = _editable_primo_post(post_id, db_path=db_path)
    channel = get_channel("x")
    if channel is None:
        raise RuntimeError("Primo X channel is not configured")

    from social.content_factory import _render_image

    if direction.lower() in {"surprise", "surprise me", "fresh", "redo", "retry"}:
        direction = "Create a fresh, distinct visual interpretation"
    prompt = (
        f"{direction}. Mixed Primo brand system: choose the strongest fit between "
        "clean terminal geometry, a technical crypto/AI explainer, or cinematic "
        f"crypto-agent imagery. No meme-coin spam. Post context: {post.body[:1200]}"
    )
    media_path = _render_image(
        "x",
        prompt,
        design_file=channel.design_file,
        persona_pack="",
        aspect=channel.image_aspect,
    )
    if not media_path or not Path(media_path).is_file():
        raise PrimoImageRequiredError(post_id)
    updated = svc.set_post_fields(
        post_id,
        media_path=media_path,
        media_type="image",
    )
    append_social_audit_record(
        channel="x",
        action="media_regenerate",
        post_id=post_id,
        outcome="updated",
        operator="primo-workshop",
    )
    return updated


def remove_primo_image(
    post_id: int, *, db_path: str | Path | None = None
) -> SocialPost:
    """Detach media from a draft without deleting the versioned asset."""

    svc, _post = _editable_primo_post(post_id, db_path=db_path)
    updated = svc.set_post_fields(post_id, media_path=None, media_type=None)
    append_social_audit_record(
        channel="x",
        action="media_remove",
        post_id=post_id,
        outcome="updated",
        operator="primo-workshop",
    )
    return updated
