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

import glob
import json
import logging
import os
import random
import subprocess
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent


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
) -> str | None:
    """Render a 9:16 vertical MP4 via the HyperFrames pipeline. A design_file
    (brand palette/fonts) makes the clip on-brand instead of the dark neutral
    default. A persona_pack locks a face onto the hero + payoff beats. Returns
    the absolute mp4 path or None on any failure (never raises)."""
    try:
        import config

        cmd = [
            "uv", "run", "python", "video_pipeline.py", topic,
            "--aspect", "9:16", "--duration-target", str(duration_s),
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
        # The pipeline emits a JSON result line with mp4_path.
        for line in reversed(proc.stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    mp4 = data.get("mp4_path")
                    if mp4 and os.path.isfile(mp4):
                        return mp4
                except json.JSONDecodeError:
                    continue
        # Fallback: newest render on disk (path may be nested in log noise).
        candidates = glob.glob(
            os.path.join(str(config.DATA_DIR), "video-renders", "*", "*.mp4")
        )
        if candidates:
            return max(candidates, key=os.path.getmtime)
        logger.warning("video render produced no mp4 (rc=%s)", proc.returncode)
    except Exception as exc:  # subprocess/timeout/etc — fail open
        logger.warning("video render failed: %s", exc)
    return None


def _render_image(
    channel_id: str,
    topic: str,
    *,
    design_file: str | None = None,
    persona_pack: str = "",
) -> str | None:
    """Generate a scene image via the codex CLI (free). Returns absolute path
    or None (never raises). A design_file (brand palette/fonts) identity-tunes
    the scene mood; a persona_pack locks a face onto it. Fail-open to the
    neutral, ref-less scene when neither is set (byte-identical to the prior
    behavior). Mirrors ``_render_video``'s brand/persona plumbing."""
    try:
        import config
        import video_imagegen

        images_dir = config.DATA_DIR / "social_images"
        images_dir.mkdir(parents=True, exist_ok=True)
        name = f"{channel_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
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
            prompt=prompt, design=design, aspect="1:1",
            assets_dir=str(images_dir), name=name, refs=refs,
        )
        return str(images_dir / Path(rel).name) if rel else None
    except Exception as exc:
        logger.warning("image gen failed: %s", exc)
    return None


def _generate_caption(channel_id: str, topic: str, voice_profile: str) -> str:
    """Copy via the shared draft_generator runtime path (fast background tier)."""
    from social import draft_generator as dg

    constraints = dg.CHANNEL_CONSTRAINTS.get(
        channel_id, dg.CHANNEL_CONSTRAINTS["facebook"]
    )
    voice_ctx = dg._read_voice_context(voice_profile)
    prompt = dg._build_draft_prompt(channel_id, topic, voice_ctx, constraints)
    try:
        body = dg._invoke_runtime(prompt) or topic
    except Exception as exc:
        logger.warning("caption gen failed, using topic: %s", exc)
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

    settings = config.get_content_factory_settings()
    do_autopilot = settings.unattended if autopilot is None else bool(autopilot)
    channel = get_channel(channel_id)
    if channel is None:
        return {"error": f"unknown channel: {channel_id}"}

    svc = SocialPostService(db_path=db_path)
    summary: dict = {
        "channel": channel_id,
        "mode": "autopilot" if do_autopilot else "queue",
        "queued": [],
        "posted": [],
        "failed": [],
    }

    for i in range(max(1, count)):
        slot_topic = topic or (
            random.choice(channel.topic_pool) if channel.topic_pool else channel_id
        )
        kind = _resolve_media_kind(channel_id, media, i)

        media_path: str | None = None
        media_type: str | None = None
        if kind == "video":
            media_path = _render_video(
                slot_topic,
                duration_s=settings.video_duration_s,
                design_file=channel.design_file,
                persona_pack=channel.persona_pack,
            )
            media_type = "video" if media_path else None
        elif kind == "image":
            media_path = _render_image(
                channel_id,
                slot_topic,
                design_file=channel.design_file,
                persona_pack=channel.persona_pack,
            )
            media_type = "image" if media_path else None

        caption = _generate_caption(channel_id, slot_topic, channel.voice_profile)
        title = caption[:60].replace("\n", " ")

        pid = svc.create_draft(
            channel=channel_id,
            title=title,
            body=caption,
            voice_profile=channel.voice_profile,
            topic_source=topic_source,
            media_path=media_path,
            media_type=media_type,
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
