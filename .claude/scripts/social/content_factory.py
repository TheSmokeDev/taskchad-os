"""Social content factory — generate media + copy, queue drafts.

The reusable engine the Archon ``social-content-factory`` workflow calls, and
the seam the daily cadence can shell. For a channel it produces N drafts:
generate copy (reuses ``draft_generator``), render media (image via
``video_imagegen`` / vertical video via ``video_pipeline``), and create a
draft carrying the media path.

DEFAULT-DENY (the hard invariant): auto-posting requires
``HOMIE_SOCIAL_UNATTENDED=true`` (ships OFF). Without it, ``produce()`` only
QUEUES drafts — the operator approves via the Telegram card / dashboard and the
Homie dispatches. When the flag is on, produce() ALSO approves + dispatches each
draft, still through the gated executor with a per-post audit row. There is no
path that posts to a real brand account unattended unless the operator has
explicitly flipped the flag.

Fail-open: media generation never raises out of produce() — a media failure
degrades that slot to caption-only, never crashes the run.
"""

from __future__ import annotations

import json
import logging
import random
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from social.brand_packs import (
    SUPPORTED_IMAGE_ASPECTS,
    SUPPORTED_VIDEO_ASPECTS,
    BrandPack,
)

logger = logging.getLogger(__name__)

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent

_ASPECT_SIZES = {
    "1:1": (1080, 1080),
    "16:9": (1600, 900),
    "4:5": (1080, 1350),
    "9:16": (1080, 1920),
}


@dataclass(frozen=True, slots=True)
class _EffectiveBrandSettings:
    """Resolved pack intent over legacy channel brand/media defaults."""

    voice_profile: str
    design_file: str
    persona_pack: str
    image_aspect: str
    video_aspect: str


def _validated_aspect(
    aspect: str,
    *,
    supported: frozenset[str],
    default: str,
) -> str:
    candidate = (aspect or default).strip()
    if candidate not in supported:
        raise ValueError("unsupported media aspect")
    return candidate


def _parse_video_result(stdout: str) -> dict | None:
    """Parse the final complete JSON object emitted by ``video_pipeline.py``."""

    for index in range(len(stdout) - 1, -1, -1):
        if stdout[index] != "{":
            continue
        try:
            value = json.loads(stdout[index:].strip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _effective_brand_settings(
    channel: object,
    brand_pack: BrandPack | None,
) -> _EffectiveBrandSettings:
    """Compute immutable render/copy inputs without mutating ``SocialChannel``."""

    if brand_pack is None:
        return _EffectiveBrandSettings(
            voice_profile=str(getattr(channel, "voice_profile", "") or ""),
            design_file=str(getattr(channel, "design_file", "") or ""),
            persona_pack=str(getattr(channel, "persona_pack", "") or ""),
            image_aspect=str(getattr(channel, "image_aspect", "1:1") or "1:1"),
            video_aspect="9:16",
        )
    return _EffectiveBrandSettings(
        voice_profile=(
            str(brand_pack.voice_profile)
            if brand_pack.voice_profile is not None
            else str(getattr(channel, "voice_profile", "") or "")
        ),
        design_file=(
            str(brand_pack.design_file)
            if brand_pack.design_file is not None
            else str(getattr(channel, "design_file", "") or "")
        ),
        persona_pack=(
            str(brand_pack.persona_pack)
            if brand_pack.persona_pack is not None
            else str(getattr(channel, "persona_pack", "") or "")
        ),
        image_aspect=brand_pack.image_aspect,
        video_aspect=brand_pack.video_aspect,
    )


def _normalize_image_aspect(image_path: str | Path, aspect: str) -> str | None:
    """Create a versioned, center-cropped asset at the requested social ratio.

    Codex image generation can return a square bitmap even when the prompt asks
    for a landscape composition. Keep that raw render and write a distinct
    normalized file so the channel contract is enforced without overwriting the
    generated original.
    """

    source = Path(image_path)
    target_size = _ASPECT_SIZES.get((aspect or "1:1").strip())
    if not source.is_file() or target_size is None:
        return None
    try:
        from PIL import Image, ImageOps

        with Image.open(source) as image:
            if image.size == target_size:
                return str(source)
            normalized = ImageOps.fit(
                image.convert("RGB"),
                target_size,
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )
            ratio_slug = (aspect or "1:1").replace(":", "x")
            destination = source.with_name(
                f"{source.stem}-{ratio_slug}{source.suffix.lower() or '.png'}"
            )
            normalized.save(destination)
        return str(destination)
    except Exception as exc:
        logger.warning("image aspect normalization failed: %s", exc)
        return None


def _resolve_design_file(design_file: str) -> str | None:
    """Resolve a channel's design_file (relative to social/ or absolute) to an
    existing path, or None. Never raises."""
    if not design_file:
        return None
    p = Path(design_file)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent / design_file
    return str(p) if p.is_file() else None


def _resolve_persona_refs(persona_pack: str) -> list[str]:
    """Resolve a channel's persona_pack to its curated reference images. Empty
    pack → []; else the curated identity subset under
    .claude/image-personas/<pack>/, filtered to files that exist. Never raises.

    Prefers the freshest real-photo anchors (front + both profiles + neutral)
    for the tightest feature lock, falling back to earlier refs when absent."""
    if not persona_pack:
        return []
    try:
        pack_dir = _SCRIPTS_DIR.parent / "image-personas" / persona_pack
        # Good-hair curated real photos (2026-07-08): luscious dry hair + natural
        # skin at the source, so the render does not inherit damp/greasy hair.
        preferred = ["ref-17.jpeg", "ref-18.jpeg", "ref-19-new.jpeg", "ref-22.jpg", "ref-23.jpeg"]
        curated = [name for name in preferred if (pack_dir / name).is_file()]
        if not curated:
            curated = ["ref-01.png", "ref-02.png", "ref-03.png", "ref-07.png"]
        return [
            str(pack_dir / name)
            for name in curated
            if (pack_dir / name).is_file()
        ]
    except Exception:
        return []


def _render_video(
    topic: str,
    *,
    duration_s: int = 18,
    design_file: str | None = None,
    persona_pack: str = "",
    aspect: str = "9:16",
) -> str | None:
    """Render an aspect-controlled MP4 via the HyperFrames pipeline. A design_file
    (brand palette/fonts) makes the clip on-brand instead of the dark neutral
    default. A persona_pack locks a face onto the hero + payoff beats. Returns
    the absolute mp4 path or None on any failure (never raises)."""
    try:
        resolved_aspect = _validated_aspect(
            aspect,
            supported=SUPPORTED_VIDEO_ASPECTS,
            default="9:16",
        )
        cmd = [
            "uv", "run", "python", "video_pipeline.py", topic,
            "--aspect", resolved_aspect, "--duration-target", str(duration_s),
            "--captions", "on",
        ]
        resolved = _resolve_design_file(design_file or "")
        if resolved:
            cmd += ["--design-file", resolved]
        for ref in _resolve_persona_refs(persona_pack):
            cmd += ["--persona-ref", ref]
        proc = subprocess.run(
            cmd,
            cwd=str(_SCRIPTS_DIR),
            capture_output=True,
            text=True,
            timeout=900,
        )
        data = _parse_video_result(proc.stdout)
        if proc.returncode != 0 or data is None or data.get("ok") is not True:
            logger.warning("video render failed or returned no verified receipt")
            return None
        raw_mp4 = data.get("mp4_path")
        if not isinstance(raw_mp4, str) or not raw_mp4.strip():
            logger.warning("video render receipt omitted mp4_path")
            return None
        candidate = Path(raw_mp4)
        if not candidate.is_absolute():
            candidate = _SCRIPTS_DIR / candidate
        try:
            mp4 = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            logger.warning("video render receipt points to a missing artifact")
            return None
        if not mp4.is_file() or mp4.suffix.lower() != ".mp4":
            logger.warning("video render receipt does not identify an mp4 file")
            return None
        return str(mp4)
    except Exception as exc:  # subprocess/timeout/etc — fail open
        logger.warning("video render failed: %s", exc)
    return None


def _render_image(
    channel_id: str,
    topic: str,
    *,
    design_file: str | None = None,
    persona_pack: str = "",
    aspect: str = "1:1",
) -> str | None:
    """Generate a scene image via the codex CLI (free). Returns absolute path
    or None (never raises). A design_file (brand palette/fonts) identity-tunes
    the scene mood; a persona_pack locks a face onto it. Fail-open to the
    neutral, ref-less scene when neither is set (byte-identical to the prior
    behavior). Mirrors ``_render_video``'s brand/persona plumbing."""
    try:
        import config
        import video_imagegen

        resolved_aspect = _validated_aspect(
            aspect,
            supported=SUPPORTED_IMAGE_ASPECTS,
            default="1:1",
        )
        images_dir = config.DATA_DIR / "social_images"
        images_dir.mkdir(parents=True, exist_ok=True)
        # Microseconds guarantee that regeneration creates a new asset instead
        # of overwriting a same-second original.
        name = f"{channel_id}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
        prompt = (
            f"A clean, modern social-media scene about: {topic}. Editorial and "
            "brand-friendly, single strong focal point, generous negative space."
        )
        design: dict = {}
        resolved = _resolve_design_file(design_file or "")
        if resolved:
            try:
                import video_styles

                design = video_styles.resolve_design(design_file=resolved) or {}
            except Exception as exc:  # design resolution never breaks the render
                logger.warning("design resolve failed: %s", exc)
                design = {}
        refs = _resolve_persona_refs(persona_pack) or None
        rel = video_imagegen.generate_image(
            prompt=prompt, design=design, aspect=resolved_aspect,
            assets_dir=str(images_dir), name=name, refs=refs,
        )
        if not rel:
            return None
        raw_path = images_dir / Path(rel).name
        return _normalize_image_aspect(raw_path, resolved_aspect)
    except Exception as exc:
        logger.warning("image gen failed: %s", exc)
    return None


def _generate_caption(
    channel_id: str,
    topic: str,
    voice_profile: str,
    *,
    persona_id: str | None = None,
) -> str:
    """Copy via the shared draft_generator runtime path (fast background tier)."""
    from social import draft_generator as dg

    try:
        persona_context = dg._load_persona_identity_context(persona_id)
    except dg.PersonaContextUnavailableError as exc:
        from social.audit import append_social_audit_record

        append_social_audit_record(
            channel=channel_id,
            action="draft",
            outcome="skipped",
            error=str(exc),
        )
        raise

    constraints = dg.CHANNEL_CONSTRAINTS.get(
        channel_id, dg.CHANNEL_CONSTRAINTS["facebook"]
    )
    voice_ctx = ""
    voice_path = Path(voice_profile) if voice_profile else None
    if voice_path is not None and voice_path.is_absolute() and voice_path.is_file():
        try:
            voice_ctx = voice_path.read_text(encoding="utf-8")[:1500]
        except (OSError, UnicodeError):
            voice_ctx = ""
    if not voice_ctx:
        voice_ctx = dg._read_voice_context(
            voice_profile,
            allow_global_fallback=persona_id is None,
        )
    prompt = dg._build_draft_prompt(channel_id, topic, voice_ctx, constraints)
    try:
        body = dg._invoke_runtime(prompt, system_prompt=persona_context)
    except Exception as exc:
        if persona_id is not None:
            from social.audit import append_social_audit_record

            error = f"configured persona draft runtime failed ({type(exc).__name__})"
            append_social_audit_record(
                channel=channel_id,
                action="draft",
                outcome="skipped",
                error=error,
            )
            raise
        logger.warning("caption gen failed, using topic: %s", exc)
        body = topic
    if not body:
        if persona_id is not None:
            from social.audit import append_social_audit_record

            error = "configured persona draft runtime returned no content"
            append_social_audit_record(
                channel=channel_id,
                action="draft",
                outcome="skipped",
                error=error,
            )
            raise RuntimeError(error)
        body = topic
    return body[: constraints["max_chars"]]


def _resolve_media_kind(channel_id: str, requested: str, slot_index: int) -> str:
    """Decide the media kind for a slot. `requested` ∈ {auto,image,video,none}."""
    if requested in ("image", "video", "none"):
        return requested
    # auto: youtube is video-only; else first slot video, rest image.
    if channel_id in ("youtube",):
        return "video"
    return "video" if slot_index == 0 else "image"


def produce(
    channel_id: str,
    *,
    count: int = 1,
    media: str = "auto",
    topic: str | None = None,
    topic_source: str = "factory",
    autopilot: bool | None = None,
    db_path: str | None = None,
    brand_pack: BrandPack | None = None,
    source_packet_id: str | None = None,
) -> dict:
    """Generate ``count`` drafts for ``channel_id`` and queue them.

    Returns a summary: ``{channel, mode, queued: [ids], posted: [ids],
    failed: [ids]}``. In queue mode (default) ``posted`` is empty — the
    operator approves + the Homie dispatches. In unattended mode each draft is
    also approved + dispatched through the gated executor.

    ``autopilot`` overrides the global ``HOMIE_SOCIAL_UNATTENDED`` flag for this
    call: ``None`` (default) honors the flag; ``False`` forces queue-only (the
    operator-approval cadence uses this so it never auto-posts regardless of the
    flag); ``True`` forces autopilot.
    """
    import config
    from social.audit import append_social_audit_record
    from social.channels import get_channel
    from social.service import SocialPostService

    if brand_pack is not None:
        brand_pack.validate()
        if "social" not in brand_pack.allowed_consumers:
            return {"error": "brand pack is not authorized for the social consumer"}

    settings = config.get_content_factory_settings()
    do_autopilot = (
        False
        if brand_pack is not None
        else settings.unattended if autopilot is None else bool(autopilot)
    )
    channel = get_channel(channel_id)
    if channel is None:
        return {"error": f"unknown channel: {channel_id}"}
    effective = _effective_brand_settings(channel, brand_pack)

    svc = SocialPostService(db_path=db_path)
    summary: dict = {
        "channel": channel_id,
        "mode": "autopilot" if do_autopilot else "queue",
        "queued": [],
        "posted": [],
        "failed": [],
    }
    if brand_pack is not None:
        summary["brand_pack"] = {
            "pack_id": brand_pack.pack_id,
            "version": brand_pack.version,
            "source_hash": brand_pack.source_hash,
        }
        summary["media_outcomes"] = []

    for i in range(max(1, count)):
        slot_topic = topic or (
            random.choice(channel.topic_pool) if channel.topic_pool else channel_id
        )
        kind = _resolve_media_kind(channel_id, media, i)

        # Copy/identity validation comes before any provider-backed media work.
        # A missing configured persona therefore skips without creating an
        # orphaned render or spending a media-provider call.
        caption = _generate_caption(
            channel_id,
            slot_topic,
            effective.voice_profile,
            persona_id=getattr(channel, "persona_id", None),
        )
        title = caption[:60].replace("\n", " ")

        media_path: str | None = None
        media_type: str | None = None
        try:
            if kind == "video":
                media_path = _render_video(
                    slot_topic,
                    duration_s=settings.video_duration_s,
                    design_file=effective.design_file,
                    persona_pack=effective.persona_pack,
                    aspect=effective.video_aspect,
                )
                media_type = "video" if media_path else None
            elif kind == "image":
                media_path = _render_image(
                    channel_id,
                    slot_topic,
                    design_file=effective.design_file,
                    persona_pack=effective.persona_pack,
                    aspect=effective.image_aspect,
                )
                media_type = "image" if media_path else None
        except Exception as exc:
            # Keep the factory's documented fail-open boundary even when a
            # renderer adapter violates its own never-raise contract.
            logger.warning("%s render failed: %s", kind, exc)
            media_path = None
            media_type = None

        if brand_pack is not None:
            if kind == "none":
                media_status = "not_requested"
                media_reason = None
            elif media_path:
                media_status = "generated"
                media_reason = None
            else:
                media_status = "degraded"
                media_reason = "media_generation_failed"
            summary["media_outcomes"].append(
                {
                    "slot": i,
                    "requested": kind,
                    "status": media_status,
                    "reason": media_reason,
                }
            )

        pid = svc.create_draft(
            channel=channel_id,
            title=title,
            body=caption,
            voice_profile=effective.voice_profile,
            topic_source=topic_source,
            media_path=media_path,
            media_type=media_type,
            source_packet_id=source_packet_id,
        )
        append_social_audit_record(
            channel=channel_id, action="draft", post_id=pid,
            outcome="created", body_preview=caption,
        )
        summary["queued"].append(pid)

        # Autopilot: post directly ONLY when the operator has enabled unattended
        # mode. Default-deny — without the flag, the draft waits for approval.
        if do_autopilot:
            try:
                from social.post_executor import dispatch_post

                svc.approve_post(pid)
                ok = dispatch_post(pid, db_path=db_path)
                (summary["posted"] if ok else summary["failed"]).append(pid)
            except Exception as exc:
                summary["failed"].append(pid)
                logger.warning("autopilot post of %s failed: %s", pid, exc)

    return summary


def produce_brand_content(
    channel_id: str,
    *,
    brand_pack: BrandPack,
    count: int = 1,
    media: str | None = None,
    topic: str | None = None,
    topic_source: str = "brand_content_factory",
    db_path: str | None = None,
    source_packet_id: str | None = None,
) -> dict:
    """Adapt one resolved BrandPack into the existing draft queue owner.

    This boundary is permanently queue-only. It does not accept an autopilot
    argument, and ``produce`` independently forces queue mode whenever a pack
    is present, so the global unattended flag cannot widen its authority.
    """

    return produce(
        channel_id,
        count=count,
        media=media or brand_pack.default_media_policy,
        topic=topic,
        topic_source=topic_source,
        autopilot=False,
        db_path=db_path,
        brand_pack=brand_pack,
        source_packet_id=source_packet_id,
    )


if __name__ == "__main__":
    import argparse
    import sys

    sys.path.insert(0, str(_SCRIPTS_DIR))
    from personas import apply_persona_override

    apply_persona_override()
    import config  # noqa: F401 — loads .env

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    ap = argparse.ArgumentParser(description="Social content factory")
    ap.add_argument("channel")
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--media", default="auto", choices=["auto", "image", "video", "none"])
    ap.add_argument("--topic", default=None)
    args = ap.parse_args()
    result = produce(args.channel, count=args.count, media=args.media, topic=args.topic)
    print(json.dumps(result, indent=2))
