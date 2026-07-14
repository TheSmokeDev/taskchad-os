"""Social cadence scheduler — heartbeat-style cron for content generation + posting.

Generates drafts for channels whose cadence is due, then dispatches approved posts.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Boot-shim: must run BEFORE any framework imports (config, etc.).
from personas import apply_persona_override  # noqa: E402

apply_persona_override()

# Load .env (via config's import-time load_dotenv) BEFORE any os.getenv check —
# otherwise SOCIAL_CADENCE_ENABLED is invisible when cadence.py runs as a
# standalone scheduled job and run_cadence_tick() silently no-ops "cadence disabled".
import config  # noqa: E402,F401

logger = logging.getLogger(__name__)


def _resolve_state_path() -> Path:
    import config
    return config.STATE_DIR / "social-cadence-state.json"


def _load_state(state_path: Path | None = None) -> dict:
    path = state_path or _resolve_state_path()
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_state(state: dict, state_path: Path | None = None) -> None:
    path = state_path or _resolve_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _is_cadence_enabled() -> bool:
    return os.getenv("SOCIAL_CADENCE_ENABLED", "false").lower() == "true"


def _is_media_enabled() -> bool:
    """Whether the cadence renders an on-brand image for asset-carrying channels.
    Default ON; set SOCIAL_CADENCE_MEDIA=false to force caption-only cards."""
    return os.getenv("SOCIAL_CADENCE_MEDIA", "true").lower() == "true"


def _hours_since(iso_str: str) -> float:
    try:
        then = datetime.fromisoformat(iso_str)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - then).total_seconds() / 3600
    except (ValueError, TypeError):
        return float("inf")


def run_cadence_tick(
    *,
    state_path: Path | None = None,
    db_path: str | Path | None = None,
    dry_run: bool = False,
) -> dict:
    """Run one cadence tick.

    Returns a summary dict: {drafts_created: int, posts_dispatched: int, channels_skipped: list}.
    """
    if not _is_cadence_enabled():
        return {"drafts_created": 0, "posts_dispatched": 0, "skipped": "cadence disabled"}

    from social.channels import list_active_channels
    from social import content_factory
    from social.post_executor import dispatch_due_posts
    import random

    media_on = _is_media_enabled()
    # Cap the per-image render so one slow generation can't blow the scheduled
    # task window — a timeout degrades to a caption-only card (video_imagegen is
    # fail-open). setdefault so an explicit env override still wins.
    os.environ.setdefault("VIDEO_ART_TIMEOUT_S", "420")

    state = _load_state(state_path)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    summary: dict = {"drafts_created": 0, "posts_dispatched": 0, "channels_skipped": []}

    active = list_active_channels()

    for ch in active:
        last_key = f"last_draft_at:{ch.channel_id}"
        last_draft = state.get(last_key, "")
        hours = _hours_since(last_draft) if last_draft else float("inf")

        if hours < ch.cadence_interval_hours:
            summary["channels_skipped"].append(ch.channel_id)
            continue

        if not ch.topic_pool:
            logger.warning("No topics for %s, skipping", ch.channel_id)
            summary["channels_skipped"].append(ch.channel_id)
            continue

        topic = random.choice(ch.topic_pool)

        if dry_run:
            logger.info("[DRY RUN] Would draft for %s: %s", ch.channel_id, topic)
            summary["drafts_created"] += 1
            continue

        # Render an on-brand image when the channel carries brand assets
        # (design_file or persona_pack); otherwise stay caption-only. Route
        # through content_factory.produce — the same engine the Archon
        # social-content-factory workflow uses — so the daily card inherits the
        # face-locked, brand-designed media path. media="none" is behaviorally
        # equivalent to the prior caption-only draft. This lane routes with
        # autopilot=False so it NEVER auto-posts, regardless of the global
        # HOMIE_SOCIAL_UNATTENDED flag (the operator approves via the card).
        media_kind = "image" if (media_on and (ch.design_file or ch.persona_pack)) else "none"
        # Fail-open at the channel grain: a media miss degrades to caption-only
        # inside produce(); a hard error (e.g. a transient shared-DB lock) is
        # caught here so one channel can never abort the rest of the tick, the
        # dedup save, or the dispatch of already-approved posts.
        try:
            cf_summary = content_factory.produce(
                ch.channel_id,
                count=1,
                media=media_kind,
                topic=topic,
                topic_source="cadence",
                autopilot=False,
                db_path=db_path,
            )
            pid = (cf_summary.get("queued") or [None])[0]
        except Exception as exc:
            logger.warning("Draft generation for %s failed: %s", ch.channel_id, exc)
            summary["channels_skipped"].append(ch.channel_id)
            continue

        if pid:
            summary["drafts_created"] += 1
            state[last_key] = now_iso
            # Persist dedup state per channel so a force-kill at the task time
            # limit can't discard an already-drafted channel's stamp (re-draft).
            _save_state(state, state_path)
            # Deliver the draft to the operator's Telegram with approve/reject
            # buttons. Fail-open: a delivery miss never blocks the cadence or
            # un-counts the draft (it is already persisted in the queue DB).
            try:
                from social.notify import deliver_draft_to_telegram
                from social.service import SocialPostService

                post = SocialPostService(db_path=db_path).get_post(pid)
                if post is not None:
                    deliver_draft_to_telegram(post)
            except Exception as exc:
                logger.warning("Draft %s delivery failed: %s", pid, exc)

    _save_state(state, state_path)

    # Dispatch all approved posts whose scheduled_for has passed
    if not dry_run:
        # Backstop for the spawn-on-approve runner: fail any dispatch claim
        # that went stale (runner died mid-flight) BEFORE claiming new work.
        # Fail-open: a sweep error never blocks the tick's own dispatches.
        try:
            from social.post_executor import sweep_stale_claims

            summary["stale_claims_swept"] = sweep_stale_claims(db_path=db_path).get(
                "swept", 0
            )
        except Exception as exc:
            logger.warning("Stale-claim sweep failed: %s", exc)

        dispatch_result = dispatch_due_posts(db_path=db_path)
        summary["posts_dispatched"] = dispatch_result.get("dispatched", 0)

        # Close the async-publish loop for the Postiz lane (no webhooks —
        # poll outcomes). Fail-open: reconcile never breaks the cadence.
        try:
            if config.get_postiz_settings().configured:
                from social.postiz_reconcile import reconcile_postiz_posts

                summary["postiz_reconcile"] = reconcile_postiz_posts(
                    db_path=db_path
                )
        except Exception as exc:
            logger.warning("Postiz reconcile failed: %s", exc)

    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    import argparse

    parser = argparse.ArgumentParser(description="Social cadence tick")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    result = run_cadence_tick(dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
