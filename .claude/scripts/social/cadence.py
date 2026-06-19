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
    from social.draft_generator import generate_draft
    from social.post_executor import dispatch_due_posts
    import random

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

        pid = generate_draft(
            ch.channel_id,
            topic,
            topic_source="cadence",
            db_path=db_path,
        )

        if pid:
            summary["drafts_created"] += 1
            state[last_key] = now_iso

    _save_state(state, state_path)

    # Dispatch all approved posts whose scheduled_for has passed
    if not dry_run:
        dispatch_result = dispatch_due_posts(db_path=db_path)
        summary["posts_dispatched"] = dispatch_result.get("dispatched", 0)

    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    import argparse

    parser = argparse.ArgumentParser(description="Social cadence tick")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    result = run_cadence_tick(dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
