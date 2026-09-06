"""Social post queue service — business logic over the DB layer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from social.db import SocialPostDB
from social.models import (
    SOCIAL_POST_TRANSITIONS,
    SocialPost,
    approval_binding_digest,
    compute_content_digest,
    compute_media_digest,
)

LEGACY_LINKEDIN_SUPERSEDE_REASON = "legacy-unverified-content-2026-09-03"


class StaleSocialApprovalError(ValueError):
    """The displayed revision/digest no longer matches the durable draft."""


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
        source_packet_id: str | None = None,
    ) -> int:
        now = datetime.now(UTC).isoformat(timespec="seconds")
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
            source_packet_id=source_packet_id,
            revision=1,
            content_digest=compute_content_digest(title, body),
            media_digest=compute_media_digest(media_path),
            verification_state="pending",
        )
        return self._db.insert(post)

    def list_queue(self, *, limit: int = 20) -> list[SocialPost]:
        return self._db.list_recent(limit=limit)

    def list_by_status(self, status: str, *, limit: int = 50) -> list[SocialPost]:
        return self._db.list_by_status(status, limit=limit)

    def list_due(self) -> list[SocialPost]:
        now = datetime.now(UTC).isoformat(timespec="seconds")
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

    def approve_post(
        self,
        post_id: int,
        *,
        expected_revision: int | None = None,
        expected_digest: str | None = None,
    ) -> SocialPost:
        return self._transition(
            post_id,
            "approved",
            expected_revision=expected_revision,
            expected_digest=expected_digest,
            approved_at=datetime.now(UTC).isoformat(timespec="seconds"),
            verification_state="pending",
        )

    def reject_post(
        self,
        post_id: int,
        reason: str = "",
        *,
        expected_revision: int | None = None,
        expected_digest: str | None = None,
    ) -> SocialPost:
        return self._transition(
            post_id,
            "rejected",
            expected_revision=expected_revision,
            expected_digest=expected_digest,
            rejection_reason=reason or "Rejected by operator",
        )

    def mark_posted(
        self,
        post_id: int,
        post_url: str = "",
        external_ref: str | None = None,
        *,
        verification_state: str | None = None,
        receipt_json: str | None = None,
    ) -> SocialPost:
        fields: dict[str, str | None] = {
            "posted_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "post_url": post_url or None,
            "external_ref": external_ref,
        }
        if verification_state is not None:
            fields["verification_state"] = verification_state
        if receipt_json is not None:
            fields["receipt_json"] = receipt_json
        return self._transition(
            post_id,
            "posted",
            **fields,
        )

    def mark_failed(
        self,
        post_id: int,
        error: str = "",
        *,
        verification_state: str | None = None,
        receipt_json: str | None = None,
    ) -> SocialPost:
        fields: dict[str, str | None] = {"error": error or "Unknown error"}
        if verification_state is not None:
            fields["verification_state"] = verification_state
        if receipt_json is not None:
            fields["receipt_json"] = receipt_json
        return self._transition(
            post_id,
            "failed",
            **fields,
        )

    def mark_verification_required(
        self,
        post_id: int,
        *,
        receipt_json: str,
        error: str = "LinkedIn submission could not be verified; do not retry",
    ) -> SocialPost:
        return self._transition(
            post_id,
            "verification_required",
            verification_state="verification_required",
            receipt_json=receipt_json,
            error=error,
        )

    def claim_post(self, post_id: int) -> bool:
        """CAS-claim an approved post for dispatch. True = this caller owns it."""
        now = datetime.now(UTC).isoformat(timespec="seconds")
        return self._db.claim_post(post_id, now)

    def clear_claim(self, post_id: int) -> bool:
        return self._db.clear_claim(post_id)

    def list_stale_claims(self, ttl_minutes: int) -> list[SocialPost]:
        cutoff = datetime.now(UTC) - timedelta(minutes=ttl_minutes)
        return self._db.list_stale_claims(cutoff.isoformat(timespec="seconds"))

    def count_by_status(self, channel: str | None = None) -> dict[str, int]:
        return self._db.count_by_status(channel)

    def set_post_fields(
        self, post_id: int, **fields: str | int | None
    ) -> SocialPost:
        """Update non-status columns while preserving exact-review provenance.

        Legacy workshop callers still use this generic helper.  Any draft copy
        or media mutation therefore advances the revision and refreshes the
        corresponding digest here instead of silently invalidating the button
        contract.
        """
        post = self._db.get(post_id)
        if post is None:
            raise ValueError(f"Post {post_id} not found")
        review_fields = {"title", "body", "media_path", "media_type"}
        if review_fields.intersection(fields):
            if post.status != "draft":
                raise ValueError(
                    f"Post {post_id} is already '{post.status}' and can no longer be edited"
                )
            revised = dict(fields)
            if {"title", "body"}.intersection(fields):
                title = str(fields.get("title", post.title) or "")
                body = str(fields.get("body", post.body) or "")
                revised["content_digest"] = compute_content_digest(title, body)
            if {"media_path", "media_type"}.intersection(fields):
                media_path = fields.get("media_path", post.media_path)
                revised["media_digest"] = compute_media_digest(
                    str(media_path) if media_path is not None else None
                )
            if not self._db.update_draft_revision(
                post_id,
                expected_revision=post.revision,
                fields=revised,
            ):
                raise StaleSocialApprovalError(
                    f"Draft #{post_id} changed while it was being revised"
                )
        else:
            self._db.update_fields(post_id, **fields)
        updated = self._db.get(post_id)
        if updated is None:
            raise ValueError(f"Post {post_id} not found")
        return updated

    def update_draft_copy(self, post_id: int, *, title: str, body: str) -> SocialPost:
        post = self._require_editable_draft(post_id)
        ok = self._db.update_draft_revision(
            post_id,
            expected_revision=post.revision,
            fields={
                "title": title,
                "body": body,
                "content_digest": compute_content_digest(title, body),
            },
        )
        if not ok:
            raise StaleSocialApprovalError(
                f"Draft #{post_id} changed while copy was being revised"
            )
        updated = self._db.get(post_id)
        assert updated is not None
        return updated

    def update_draft_media(
        self,
        post_id: int,
        *,
        media_path: str | None,
        media_type: str | None,
    ) -> SocialPost:
        post = self._require_editable_draft(post_id)
        ok = self._db.update_draft_revision(
            post_id,
            expected_revision=post.revision,
            fields={
                "media_path": media_path,
                "media_type": media_type,
                "media_digest": compute_media_digest(media_path),
            },
        )
        if not ok:
            raise StaleSocialApprovalError(
                f"Draft #{post_id} changed while media was being revised"
            )
        updated = self._db.get(post_id)
        assert updated is not None
        return updated

    def validate_binding(
        self, post_id: int, *, revision: int, digest: str
    ) -> tuple[bool, SocialPost]:
        """Re-read durable state and fail stale buttons closed."""

        post = self._db.get(post_id)
        if post is None:
            raise ValueError(f"Post {post_id} not found")
        post = self._refresh_draft_provenance(post)
        matches = (
            post.status == "draft"
            and post.revision == revision
            and approval_binding_digest(post) == digest
        )
        return matches, post

    def assert_integrity(self, post_id: int) -> SocialPost:
        """Ensure the approved copy/media still match their stored hashes."""

        post = self._db.get(post_id)
        if post is None:
            raise ValueError(f"Post {post_id} not found")
        # Blank hashes identify legacy rows created before exact approvals.
        if not post.content_digest and not post.media_digest:
            return post
        if post.content_digest != compute_content_digest(post.title, post.body):
            raise StaleSocialApprovalError(
                f"Post {post_id} copy changed after approval"
            )
        if post.media_digest != compute_media_digest(post.media_path):
            raise StaleSocialApprovalError(
                f"Post {post_id} media changed after approval"
            )
        return post

    def supersede_legacy_linkedin_drafts(
        self, reason: str = LEGACY_LINKEDIN_SUPERSEDE_REASON
    ) -> int:
        """Preserve and mark legacy LinkedIn drafts; never called implicitly."""

        return self._db.supersede_legacy_linkedin_drafts(reason)

    def _require_editable_draft(self, post_id: int) -> SocialPost:
        post = self._db.get(post_id)
        if post is None:
            raise ValueError(f"Post {post_id} not found")
        if post.status != "draft":
            raise ValueError(
                f"Post {post_id} is already '{post.status}' and can no longer be edited"
            )
        return post

    def _refresh_draft_provenance(self, post: SocialPost) -> SocialPost:
        if post.status != "draft":
            return post
        content = compute_content_digest(post.title, post.body)
        media = compute_media_digest(post.media_path)
        if post.content_digest == content and post.media_digest == media:
            return post
        ok = self._db.update_draft_revision(
            post.id,
            expected_revision=post.revision,
            fields={"content_digest": content, "media_digest": media},
        )
        refreshed = self._db.get(post.id)
        if refreshed is None:
            raise ValueError(f"Post {post.id} not found")
        if not ok:
            return refreshed
        return refreshed

    def _transition(
        self,
        post_id: int,
        new_status: str,
        *,
        expected_revision: int | None = None,
        expected_digest: str | None = None,
        **fields: str | int | None,
    ) -> SocialPost:
        post = self._db.get(post_id)
        if post is None:
            raise ValueError(f"Post {post_id} not found")
        if expected_revision is not None or expected_digest is not None:
            post = self._refresh_draft_provenance(post)
            if expected_revision is None or expected_digest is None:
                raise StaleSocialApprovalError(
                    "Exact approval requires both revision and digest"
                )
            if (
                post.revision != expected_revision
                or approval_binding_digest(post) != expected_digest
            ):
                raise StaleSocialApprovalError(
                    f"Draft #{post_id} changed; review revision {post.revision}"
                )
        allowed = SOCIAL_POST_TRANSITIONS.get(post.status, frozenset())
        if new_status not in allowed:
            raise ValueError(
                f"Cannot transition post {post_id} from '{post.status}' to '{new_status}'"
            )
        changed = self._db.update_status(
            post_id,
            new_status,
            expected_status=post.status,
            expected_revision=post.revision,
            expected_content_digest=post.content_digest,
            expected_media_digest=post.media_digest,
            **fields,
        )
        if not changed:
            raise StaleSocialApprovalError(
                f"Post {post_id} changed before transition; no action was taken"
            )
        updated = self._db.get(post_id)
        assert updated is not None
        return updated
