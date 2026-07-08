"""Social post queue service — business logic over the DB layer."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from social.db import SocialPostDB
from social.models import SOCIAL_POST_TRANSITIONS, SocialPost


class SocialPostService:
    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            import config

            db_path = config.ORCHESTRATION_DB_PATH
        self._db = SocialPostDB(db_path)

    def create_draft(
        self,
        *,
        channel: str,
        title: str,
        body: str,
        voice_profile: str = "",
        topic_source: str = "manual",
        scheduled_for: str | None = None,
        media_path: str | None = None,
        media_type: str | None = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        post = SocialPost(
            channel=channel,
            status="draft",
            title=title,
            body=body,
            voice_profile=voice_profile,
            topic_source=topic_source,
            created_at=now,
            scheduled_for=scheduled_for,
            media_path=media_path,
            media_type=media_type,
        )
        return self._db.insert(post)

    def list_queue(self, *, limit: int = 20) -> list[SocialPost]:
        return self._db.list_recent(limit=limit)

    def list_by_status(self, status: str, *, limit: int = 50) -> list[SocialPost]:
        return self._db.list_by_status(status, limit=limit)

    def list_due(self) -> list[SocialPost]:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return self._db.list_due(now)

    def get_post(self, post_id: int) -> SocialPost | None:
        return self._db.get(post_id)

    def schedule_post(self, post_id: int, scheduled_for: str) -> SocialPost:
        """Set the dispatch time for a draft or approved post."""
        post = self._db.get(post_id)
        if post is None:
            raise ValueError(f"Post {post_id} not found")
        if post.status not in ("draft", "approved"):
            raise ValueError(
                f"Cannot schedule post {post_id} with status '{post.status}'"
            )
        self._db.set_scheduled_for(post_id, scheduled_for)
        updated = self._db.get(post_id)
        assert updated is not None
        return updated

    def approve_post(self, post_id: int) -> SocialPost:
        return self._transition(
            post_id,
            "approved",
            approved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    def reject_post(self, post_id: int, reason: str = "") -> SocialPost:
        return self._transition(
            post_id,
            "rejected",
            rejection_reason=reason or "Rejected by operator",
        )

    def mark_posted(
        self, post_id: int, post_url: str = "", external_ref: str | None = None
    ) -> SocialPost:
        return self._transition(
            post_id,
            "posted",
            posted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            post_url=post_url or None,
            external_ref=external_ref,
        )

    def mark_failed(self, post_id: int, error: str = "") -> SocialPost:
        return self._transition(
            post_id,
            "failed",
            error=error or "Unknown error",
        )

    def count_by_status(self, channel: str | None = None) -> dict[str, int]:
        return self._db.count_by_status(channel)

    def set_post_fields(self, post_id: int, **fields: str | None) -> SocialPost:
        """Update non-status columns (e.g. reconcile filling post_url)."""
        self._db.update_fields(post_id, **fields)
        updated = self._db.get(post_id)
        if updated is None:
            raise ValueError(f"Post {post_id} not found")
        return updated

    def _transition(
        self, post_id: int, new_status: str, **fields: str | None
    ) -> SocialPost:
        post = self._db.get(post_id)
        if post is None:
            raise ValueError(f"Post {post_id} not found")
        allowed = SOCIAL_POST_TRANSITIONS.get(post.status, frozenset())
        if new_status not in allowed:
            raise ValueError(
                f"Cannot transition post {post_id} from '{post.status}' to '{new_status}'"
            )
        self._db.update_status(post_id, new_status, **fields)
        updated = self._db.get(post_id)
        assert updated is not None
        return updated
