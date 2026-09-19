"""Reusable LinkedIn workshop operations over the social content factory.

The chat layer owns conversation state and buttons. This module owns the
durable draft mutations so Telegram, Discord, CLI, and future GUI surfaces can
share the same queue rows without duplicating generation logic.
"""

from __future__ import annotations

from pathlib import Path

from social.audit import append_social_audit_record
from social.channels import get_channel
from social.models import (
    SocialPost,
    approval_binding_digest,
    compute_content_digest,
    compute_media_digest,
)
from social.publishers import is_linkedin_channel
from social.service import SocialPostService, StaleSocialApprovalError


def _authority_packet_for_post(post: SocialPost):
    if post.topic_source != "authority_signal":
        return None
    if not post.source_packet_id:
        raise RuntimeError("Authority draft has no source packet; regenerate it first")
    from business_signal.config import AUTHORITY_SIGNAL_DIR
    from social.authority_content import load_authority_packet

    packet_path = Path(AUTHORITY_SIGNAL_DIR) / f"{post.source_packet_id}.json"
    return load_authority_packet(packet_path)


def _editable_linkedin_post(
    post_id: int, *, db_path: str | Path | None = None,
    expected_revision: int | None = None, expected_digest: str | None = None,
    allow_unbound_company: bool = False,
) -> tuple[SocialPostService, SocialPost]:
    svc = SocialPostService(db_path=db_path)
    post = svc.get_post(post_id)
    if post is None:
        raise ValueError(f"Post {post_id} not found")
    if not is_linkedin_channel(get_channel(post.channel) or post.channel):
        raise ValueError(f"Post {post_id} is not a LinkedIn draft")
    channel = get_channel(post.channel)
    if (
        channel is not None and channel.publisher is not None
        and post.publisher_json is None and not allow_unbound_company
    ):
        raise ValueError("Company publisher is unbound; review the exact company draft first")
    if post.status != "draft":
        raise ValueError(
            f"Post {post_id} is already '{post.status}' and can no longer be edited"
        )
    if (
        expected_revision is not None and post.revision != expected_revision
        or expected_digest is not None and approval_binding_digest(post) != expected_digest
    ):
        raise StaleSocialApprovalError(f"Draft #{post_id} changed; load the current preview")
    _ensure_original_unchanged(svc, post)
    return svc, post


def _ensure_original_unchanged(svc: SocialPostService, post: SocialPost) -> None:
    current = svc.get_post(post.id)
    if (
        current is None or current.status != "draft"
        or approval_binding_digest(current) != approval_binding_digest(post)
        or post.content_digest
        and post.content_digest != compute_content_digest(post.title, post.body)
        or post.media_digest and post.media_digest != compute_media_digest(post.media_path)
    ):
        raise StaleSocialApprovalError(f"Draft #{post.id} changed while being revised")


def _rebuild_authority_draft(
    svc: SocialPostService,
    post: SocialPost,
    packet,
    *,
    feedback: str,
    visual_direction: str = "",
) -> SocialPost:
    """Replace a reviewed copy/image pair atomically, never only one half."""

    from social.authority_content import build_reviewed_authority_artifacts

    channel = get_channel(post.channel)
    if channel is None or channel.persona_id != "socials":
        raise RuntimeError("Socials persona is not configured for LinkedIn")
    previous = svc.get_editorial_package(post.id, post.revision) or {}
    prior_cta = previous.get("cta") or {}
    resource_week = svc.resource_week_for_post(post.id)
    # An unreviewed legacy body containing "Comment GEO" is not an entitlement.
    # A revision can retain its existing reserved drop, but cannot create one.
    allow_resource = bool(
        resource_week
        and prior_cta.get("kind") == "resource_drop"
        and prior_cta.get("resource_id")
        and prior_cta.get("digest")
    )
    package, media_path, _media_mode = build_reviewed_authority_artifacts(
        packet,
        channel,
        service=svc,
        allow_resource_drop=allow_resource,
        feedback=(
            f"Current public caption (revision context, not new evidence):\n{post.body}"
            f"\n\nOperator feedback:\n{feedback}"
        ),
        format_hint=previous.get("format"),
        visual_direction=visual_direction,
    )
    revised = package["public_body"]
    _ensure_original_unchanged(svc, post)
    return svc.update_editorial_draft(
        post.id,
        expected_revision=post.revision,
        expected_digest=approval_binding_digest(post),
        title=revised[:60].replace("\n", " "),
        body=revised,
        media_path=media_path,
        media_type="image",
        editorial_package=package,
        resource_week=resource_week if allow_resource else None,
    )


def create_linkedin_draft(
    *,
    topic: str | None,
    mode: str,
    db_path: str | Path | None = None,
    channel_id: str = "linkedin",
) -> SocialPost:
    """Create one image-backed, approval-gated LinkedIn draft."""

    from social.content_factory import produce

    channel = get_channel(channel_id)
    if channel_id not in {"linkedin", "li"}:
        if channel is None or not is_linkedin_channel(channel) or not channel.publisher:
            raise ValueError("A company LinkedIn publisher must be configured")
        if not (topic or "").strip():
            raise ValueError("Supply approved company positioning and a specific workflow first")
        from social.company_editorial import generate_company_artifacts
        from social.publishers import canonical_publisher_json

        svc = SocialPostService(db_path=db_path)
        seed = SocialPost(
            channel=channel_id, body=topic.strip(), voice_profile=channel.voice_profile,
        )
        package, media_path = generate_company_artifacts(seed, channel)
        pid = svc.create_draft(
            channel=channel_id, title=package["public_body"][:60].replace("\n", " "),
            body=package["public_body"], voice_profile=channel.voice_profile,
            topic_source="linkedin-workshop:cook", media_path=media_path, media_type="image",
            publisher_json=canonical_publisher_json(channel.publisher), editorial_package=package,
        )
        post = svc.get_post(pid)
        assert post is not None
        return post

    normalized_mode = "run" if mode == "run" else "cook"
    summary = produce(
        channel_id,
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
    expected_revision: int | None = None,
    expected_digest: str | None = None,
) -> SocialPost:
    """Revise one draft, renewing authority copy and image review together."""

    feedback = (feedback or "").strip()
    if not feedback:
        raise ValueError("Revision feedback is required")
    svc, post = _editable_linkedin_post(
        post_id, db_path=db_path, expected_revision=expected_revision,
        expected_digest=expected_digest,
    )

    if post.publisher_json is not None:
        return _rebuild_company_draft(svc, post, feedback=feedback)

    from social import draft_generator as dg

    authority_packet = _authority_packet_for_post(post)
    if authority_packet is not None:
        updated = _rebuild_authority_draft(
            svc, post, authority_packet, feedback=feedback
        )
        append_social_audit_record(
            channel=post.channel,
            action="revise",
            post_id=post_id,
            outcome="updated",
            operator="operator-workshop",
            body_preview=updated.body,
        )
        return updated

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
    _ensure_original_unchanged(svc, post)
    updated = svc.update_draft_copy(
        post_id, body=revised, title=title, expected_revision=post.revision,
        expected_digest=approval_binding_digest(post),
    )
    append_social_audit_record(
        channel=post.channel,
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
    expected_revision: int | None = None,
    expected_digest: str | None = None,
) -> SocialPost:
    """Render and attach a fresh image to an editable LinkedIn draft."""

    direction = (direction or "").strip()
    if not direction:
        raise ValueError("Image direction is required")
    svc, post = _editable_linkedin_post(
        post_id, db_path=db_path, expected_revision=expected_revision,
        expected_digest=expected_digest,
    )
    channel = get_channel(post.channel)
    if channel is None:
        raise RuntimeError("LinkedIn channel is not configured")

    if post.publisher_json is not None:
        return _rebuild_company_draft(svc, post, visual_direction=direction, preserve_caption=True)

    authority_packet = _authority_packet_for_post(post)
    if authority_packet is not None:
        updated = _rebuild_authority_draft(
            svc,
            post,
            authority_packet,
            feedback=(
                "Keep this draft's useful argument and resource promise where "
                "supported. Renew the complete copy/image review for the new visual."
            ),
            visual_direction=direction,
        )
        append_social_audit_record(
            channel=post.channel,
            action="media_regenerate",
            post_id=post_id,
            outcome="updated",
            operator="operator-workshop",
        )
        return updated

    from social.content_factory import _render_image

    if direction.lower() in {"surprise", "surprise me", "fresh", "redo"}:
        direction = "Create a fresh, distinct visual interpretation of this post"
    prompt = f"{direction}. Post context: {post.body[:1200]}"
    media_path = _render_image(
        post.channel,
        prompt,
        design_file=channel.design_file,
        persona_pack=channel.persona_pack,
        aspect=channel.image_aspect,
    )
    if not media_path:
        raise RuntimeError("LinkedIn image generation returned no image")
    _ensure_original_unchanged(svc, post)
    updated = svc.update_draft_media(
        post_id,
        media_path=media_path,
        media_type="image",
        expected_revision=post.revision,
        expected_digest=approval_binding_digest(post),
    )
    append_social_audit_record(
        channel=post.channel,
        action="media_regenerate",
        post_id=post_id,
        outcome="updated",
        operator="operator-workshop",
    )
    return updated


def _rebuild_company_draft(
    svc: SocialPostService, post: SocialPost, *, feedback: str = "",
    visual_direction: str = "", preserve_caption: bool = False,
) -> SocialPost:
    from social.company_editorial import generate_company_artifacts
    from social.publishers import assert_publisher_matches_channel

    channel = get_channel(post.channel)
    assert_publisher_matches_channel(post, channel)
    package, media_path = generate_company_artifacts(
        post, channel, previous=svc.get_editorial_package(post.id, post.revision),
        feedback=feedback, visual_direction=visual_direction, preserve_caption=preserve_caption,
    )
    _ensure_original_unchanged(svc, post)
    updated = svc.update_editorial_draft(
        post.id, expected_revision=post.revision, expected_digest=approval_binding_digest(post),
        body=package["public_body"], title=package["public_body"][:60].replace("\n", " "),
        media_path=media_path, media_type="image", editorial_package=package,
    )
    append_social_audit_record(
        channel=post.channel, action="media_regenerate" if preserve_caption else "revise",
        post_id=post.id, outcome="updated", operator="operator-workshop",
    )
    return updated


def review_company_draft(
    post_id: int, *, visible_copy: dict[str, str], visual_concept: str,
    expected_revision: int, expected_digest: str, publisher_json: str | None = None,
    db_path: str | Path | None = None, review_invoke=None, mapping_invoke=None,
    image_review_invoke=None,
) -> SocialPost:
    """Review an existing company pair without changing one caption byte or pixel."""
    from social import authority_editorial as editorial
    from social.authority_image_factory import _review_render
    from social.company_editorial import (
        company_system_prompt,
        review_company_copy,
    )
    from social.publishers import assert_publisher_matches_channel, canonical_publisher_json

    svc, post = _editable_linkedin_post(
        post_id, db_path=db_path, expected_revision=expected_revision,
        expected_digest=expected_digest,
        allow_unbound_company=True,
    )
    channel = get_channel(post.channel)
    company_system_prompt(channel)
    target = canonical_publisher_json(publisher_json or post.publisher_json or channel.publisher)
    target_post = SocialPost(**{**vars(post), "publisher_json": target})
    assert_publisher_matches_channel(target_post, channel)
    visual = editorial.EditorialVisualBrief(**visible_copy, concept=visual_concept)
    # mapping_invoke remains accepted for callers of the initial helper, but an
    # existing caption has no writer artifact: its independent exhaustive review
    # now supplies the canonical fact mapping, without a redundant model call.
    context = {"kind": "operator_supplied_copy", "text": post.body}
    package = review_company_copy({
        "public_body": post.body, "format": "compact_workflow", "factual_statements": [],
        "cta": {"kind": "none"}, "visual_brief": visual.model_dump(mode="json"),
    }, context=context, review_invoke=review_invoke, bind_existing_copy=True)
    if not post.media_path or not Path(post.media_path).is_file():
        raise ValueError("Existing company image is missing")
    package["media_validation"] = _review_render(
        Path(post.media_path), visible_copy,
        {**package["visual_brief"], "public_body": post.body, "cta": package["cta"]},
        image_review_invoke,
    )
    _ensure_original_unchanged(svc, post)
    return svc.update_editorial_draft(
        post.id, expected_revision=post.revision, expected_digest=approval_binding_digest(post),
        body=post.body, title=post.title, media_path=post.media_path, media_type="image",
        editorial_package=package, publisher_json=target,
    )
