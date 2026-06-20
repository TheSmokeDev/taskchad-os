"""Content draft generator — idea to voice-matched draft per channel.

Uses the runtime layer with the ``fast`` background model tier.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from social.audit import append_social_audit_record
from social.channels import SocialChannel, get_channel
from social.service import SocialPostService

logger = logging.getLogger(__name__)

CHANNEL_CONSTRAINTS: dict[str, dict] = {
    "linkedin": {
        "max_chars": 3000,
        "style": "Professional, thought-leadership tone. Include 3-5 relevant hashtags at the end. Use line breaks for readability.",
    },
    "facebook": {
        "max_chars": 500,
        "style": "Conversational, community-oriented. No hashtag overload. Ask a question or call-to-action at the end.",
    },
    "x": {
        "max_chars": 280,
        "style": "Punchy, direct. No hashtags unless organic. Every word earns its spot.",
    },
    "reddit": {
        "max_chars": 2000,
        "style": "Value-first, no self-promotion tone. Read like a genuine community member sharing insight.",
    },
    "instagram": {
        "max_chars": 2200,
        "style": "Visual-first caption. Start with a hook line. Include 5-10 relevant hashtags at the end.",
    },
    "discord": {
        "max_chars": 2000,
        "style": "Casual, community chat tone. Use markdown formatting.",
    },
}


def _build_draft_prompt(
    channel_id: str,
    topic: str,
    voice_context: str,
    constraints: dict,
) -> str:
    return f"""You are a social media content writer. Generate ONE post for {channel_id.upper()}.

## Topic
{topic}

## Voice & Brand
{voice_context if voice_context else "Write in a confident, authentic voice. No corporate jargon. No em-dashes."}

## Platform Rules
- Maximum {constraints['max_chars']} characters
- Style: {constraints['style']}

## Output
Return ONLY the post text. No preamble, no "Here's a draft:", no markdown code blocks. Just the raw post content ready to paste."""


def _read_voice_context(voice_profile: str = "") -> str:
    try:
        import config
        soul_path = config.SOUL_FILE
        if soul_path.is_file():
            text = soul_path.read_text(encoding="utf-8")
            if len(text) > 1500:
                text = text[:1500]
            return text
    except Exception:
        pass
    return ""


def _invoke_runtime(prompt: str) -> str:
    """Run one background-model completion and return the text.

    Bridges the async runtime from this sync function: a plain ``asyncio.run``
    when no event loop is running (the cadence job / CLI), or a worker thread
    when one is (the async ``/social`` chat handler). The LLM kill-switch is
    enforced inside ``run_with_runtime_lanes``.
    """
    import asyncio

    import config
    from runtime.base import RuntimeRequest
    from runtime.capabilities import TEXT_REASONING
    from runtime.lane_router import run_with_runtime_lanes

    model = config.get_background_models().get("fast", "haiku")
    request = RuntimeRequest(
        prompt=prompt,
        cwd=config.PROJECT_ROOT,
        task_name="social_draft_generator",
        capability=TEXT_REASONING,
        model=model,
        max_turns=1,
        allowed_tools=[],
    )

    async def _go() -> str:
        result = await run_with_runtime_lanes(request)
        return (getattr(result, "text", "") or "").strip()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_go())  # no loop: cadence / CLI
    # A loop is already running (async chat handler) — run in a worker thread.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(_go())).result()


def _generate_social_image(channel_id: str, topic: str) -> str | None:
    """Best-effort scene image for image-first channels (Instagram), generated
    through the codex CLI image generator. Returns the saved absolute path, or
    None on any failure (codex absent, image_generation not enabled, quota,
    timeout). Never raises — a caption-only draft is the graceful fallback.
    """
    if channel_id != "instagram":
        return None
    try:
        from datetime import datetime

        import config
        import video_imagegen

        images_dir = config.DATA_DIR / "social_images"
        images_dir.mkdir(parents=True, exist_ok=True)
        name = f"{channel_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        prompt = (
            f"A clean, modern social-media scene about: {topic}. Editorial and "
            "brand-friendly, single strong focal point, generous negative space."
        )
        rel = video_imagegen.generate_image(
            prompt=prompt,
            design={},
            aspect="1:1",
            assets_dir=str(images_dir),
            name=name,
        )
        if not rel:
            return None
        return str(images_dir / Path(rel).name)
    except Exception:
        return None


def generate_draft(
    channel_id: str,
    topic: str,
    *,
    voice_profile: str = "",
    topic_source: str = "manual",
    scheduled_for: str | None = None,
    db_path: str | Path | None = None,
) -> int | None:
    """Generate a voice-matched draft and save to the post queue.

    Returns the post ID on success, None on failure.
    """
    channel = get_channel(channel_id)
    if channel is None:
        logger.error("Unknown channel: %s", channel_id)
        return None

    constraints = CHANNEL_CONSTRAINTS.get(channel_id, CHANNEL_CONSTRAINTS["facebook"])
    voice_ctx = _read_voice_context(voice_profile or channel.voice_profile)
    prompt = _build_draft_prompt(channel_id, topic, voice_ctx, constraints)

    try:
        body = _invoke_runtime(prompt)

        if not body:
            logger.error("Empty draft from runtime for %s", channel_id)
            return None

        if len(body) > constraints["max_chars"]:
            body = body[: constraints["max_chars"]]

    except Exception as exc:
        logger.error("Draft generation failed for %s: %s", channel_id, exc)
        svc = SocialPostService(db_path=db_path)
        pid = svc.create_draft(
            channel=channel_id,
            title=f"[FAILED] {topic[:60]}",
            body=f"Draft generation failed: {exc}",
            voice_profile=voice_profile or channel.voice_profile,
            topic_source=topic_source,
            scheduled_for=scheduled_for,
        )
        svc.mark_failed(svc.approve_post(pid).id, error=str(exc))
        return None

    # Image-first channels (Instagram): attach a best-effort generated scene
    # image. Caption-only is the graceful fallback when image gen is unavailable.
    image_path = _generate_social_image(channel_id, topic)
    if image_path:
        body = f"{body}\n\n[generated image: {image_path}]"

    title = body[:60].replace("\n", " ")
    if len(body) > 60:
        title += "..."

    svc = SocialPostService(db_path=db_path)
    pid = svc.create_draft(
        channel=channel_id,
        title=title,
        body=body,
        voice_profile=voice_profile or channel.voice_profile,
        topic_source=topic_source,
        scheduled_for=scheduled_for,
    )

    append_social_audit_record(
        channel=channel_id,
        action="draft",
        post_id=pid,
        outcome="created",
        body_preview=body,
    )

    return pid
