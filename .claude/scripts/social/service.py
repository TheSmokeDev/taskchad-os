"""Social post queue service — business logic over the DB layer."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta
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

            db_path = config.get_orchestration_db_path()
        self._db = SocialPostDB(db_path)

    @property
    def db_path(self) -> str:
        return self._db._db_path

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
        editorial_package: dict | None = None,
        resource_week: str | None = None,
        publisher_json: str | None = None,
    ) -> int:
        from social.channels import get_channel
        from social.publishers import assert_publisher_matches_channel, canonical_publisher_json

        selected = get_channel(channel)
        publisher_json = canonical_publisher_json(
            publisher_json if publisher_json is not None else getattr(selected, "publisher", None)
        )
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
            publisher_json=publisher_json,
        )
        assert_publisher_matches_channel(post, selected)
        if post.topic_source == "authority_signal" and editorial_package is None:
            raise StaleSocialApprovalError(
                "New authority drafts require an independently reviewed editorial package"
            )
        if editorial_package is not None:
            self._validate_editorial_payload(editorial_package, post)
        self._validate_resource_week(resource_week)
        return self._db.insert(
            post, editorial_package=editorial_package, resource_week=resource_week
        )

    def get_editorial_package(self, post_id: int, revision: int | None = None) -> dict | None:
        record = self._db.get_editorial_record(post_id, revision)
        if record is None:
            return None
        package = json.loads(record["package_json"])
        if not isinstance(package, dict):
            raise StaleSocialApprovalError("Stored editorial package is not an object")
        return package

    def list_delivered_editorial(self, limit: int = 14) -> list[dict]:
        """Delivered revisions plus labeled posted legacy copy, never pending guesses."""
        return self._db.list_delivered_editorial(limit=limit)

    def mark_editorial_delivered(self, post_id: int, revision: int) -> bool:
        post = self.assert_editorial_integrity(post_id)
        if post.revision != revision:
            return False
        return self._db.mark_editorial_delivered(
            post_id,
            revision,
            content_digest=post.content_digest,
            media_digest=post.media_digest,
            delivered_at=datetime.now(UTC).isoformat(timespec="microseconds"),
        )

    def resource_week_for_post(self, post_id: int) -> str | None:
        return self._db.resource_week_for_post(post_id)

    @staticmethod
    def _validate_resource_week(resource_week: str | None) -> None:
        if resource_week is None:
            return
        if not re.fullmatch(r"\d{4}-W\d{2}", resource_week):
            raise ValueError("resource_week must be a scheduled ISO week (YYYY-Www)")
        year, week = resource_week.split("-W")
        date.fromisocalendar(int(year), int(week), 1)

    @staticmethod
    def _validate_editorial_payload(package: dict, post: SocialPost) -> None:
        """Revalidate the complete source/reviewer contract at the storage boundary."""
        if package.get("schema_version") == "company-editorial/v1":
            from social.company_editorial import validate_company_editorial_package

            validate_company_editorial_package(package, post)
            return
        if package.get("schema_version") != "authority-editorial/v1":
            raise StaleSocialApprovalError("Editorial package version is missing or unsupported")
        if package.get("public_body") != post.body:
            raise StaleSocialApprovalError("Editorial review does not match the public caption")
        validation = package.get("validation")
        if not isinstance(validation, dict) or validation.get("accepted") is not True:
            raise StaleSocialApprovalError("Editorial factual review must pass before queueing")
        if package.get("source_packet_id") != post.source_packet_id:
            raise StaleSocialApprovalError("Editorial source packet does not match the queued post")
        body_digest = hashlib.sha256(post.body.encode("utf-8")).hexdigest()
        if validation.get("public_body_digest") != body_digest:
            raise StaleSocialApprovalError("Editorial review receipt does not bind this caption")
        visual = package.get("visual_brief")
        if not isinstance(visual, dict):
            raise StaleSocialApprovalError("Editorial visual brief is missing")
        visual_digest = hashlib.sha256(
            json.dumps(visual, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if validation.get("visual_brief_digest") != visual_digest:
            raise StaleSocialApprovalError("Editorial visual brief changed after review")
        editorial_fields = (
            "public_body",
            "format",
            "factual_statements",
            "cta",
            "visual_brief",
        )
        try:
            editorial_payload = {field: package[field] for field in editorial_fields}
        except KeyError as exc:
            raise StaleSocialApprovalError("Editorial review payload is incomplete") from exc
        editorial_digest = hashlib.sha256(
            json.dumps(editorial_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if validation.get("editorial_digest") != editorial_digest:
            raise StaleSocialApprovalError("Editorial factual mappings or CTA changed after review")
        from business_signal.models import AuthoritySignalPacket
        from social.authority_editorial import (
            AuthorityEditorialPackage,
            validate_editorial_package,
        )

        try:
            packet = AuthoritySignalPacket.model_validate(package["source_packet"])
            expires = datetime.fromisoformat(package["source_expires_at"])
            if expires.tzinfo is not None and expires <= datetime.now(UTC):
                raise ValueError("Source evidence expired; regenerate the draft")
            if expires.tzinfo is None or expires != packet.expires_at:
                raise ValueError("Source expiry must match the reviewed evidence packet")
            core = {field: package[field] for field in AuthorityEditorialPackage.model_fields}
            reviewed = validate_editorial_package(
                core,
                packet,
                allow_resource_drop=package.get("cta", {}).get("kind") == "resource_drop",
                now=datetime.now(UTC),
            )
            if reviewed.validation.reviewed_at < packet.observed_at:
                raise ValueError("Editorial review predates its evidence packet")
        except (KeyError, TypeError, ValueError) as exc:
            raise StaleSocialApprovalError(
                f"Complete independent editorial review is required: {exc}"
            ) from exc
        if post.content_digest != compute_content_digest(post.title, post.body):
            raise StaleSocialApprovalError(
                "Editorial caption digest changed; regenerate the review"
            )
        if not post.media_path or not Path(post.media_path).is_file():
            raise StaleSocialApprovalError("Editorial image is missing; regenerate before approval")
        if post.media_type != "image":
            raise StaleSocialApprovalError("Editorial posts require a reviewed image")
        if post.media_digest != compute_media_digest(post.media_path):
            raise StaleSocialApprovalError("Editorial image changed; regenerate the image review")
        media_validation = package.get("media_validation")
        if (
            not isinstance(media_validation, dict)
            or media_validation.get("accepted") is not True
            or media_validation.get("media_digest") != post.media_digest
        ):
            raise StaleSocialApprovalError(
                "Editorial image review is missing or no longer matches the rendered image"
            )
        from social.authority_image_factory import _copy_matches_transcript

        visible_copy = {
            key: visual[key]
            for key in ("eyebrow", "headline", "accent", "subhead", "cta")
            if visual.get(key)
        }
        visible_digest = hashlib.sha256(
            json.dumps(visible_copy, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        observed = media_validation.get("observed_text")
        media_fields = {
            "schema_version",
            "validation_method",
            "visual_quality_review",
            "accepted",
            "image_inspected",
            "observed_text",
            "caption_agreement",
            "resource_agreement",
            "objective_defects",
            "media_digest",
            "visible_copy_digest",
            "reasons",
        }
        if (
            set(media_validation) != media_fields
            or media_validation.get("schema_version") != "authority-image-review/v1"
            or media_validation.get("validation_method")
            not in {
                "ocr_text_and_editorial_review",
                "attached_bitmap_review",
            }
            or media_validation.get("visual_quality_review") != "operator_required"
            or any(
                media_validation.get(key) is not True
                for key in (
                    "image_inspected",
                    "caption_agreement",
                    "resource_agreement",
                )
            )
            or media_validation.get("objective_defects") != []
            or media_validation.get("reasons") != []
            or media_validation.get("visible_copy_digest") != visible_digest
            or not isinstance(observed, list)
            or not observed
            or any(not isinstance(text, str) for text in observed)
            or not _copy_matches_transcript(observed, visible_copy)
        ):
            raise StaleSocialApprovalError(
                "Complete image inspection matching the approved visible copy is required"
            )

    def assert_editorial_integrity(self, post: SocialPost | int) -> SocialPost:
        post_id = post if isinstance(post, int) else post.id
        current = self._db.get(post_id)
        if current is None:
            raise ValueError(f"Post {post_id} not found")
        if not isinstance(post, int) and (
            post.revision != current.revision
            or post.content_digest != current.content_digest
            or post.media_digest != current.media_digest
            or post.channel != current.channel
            or post.publisher_json != current.publisher_json
        ):
            raise StaleSocialApprovalError(f"Draft #{post_id} changed; load the current preview")
        record = self._db.get_editorial_record(post_id, current.revision)
        if record is None:
            raise StaleSocialApprovalError(
                f"Draft #{post_id} needs regeneration under methods-first editorial review"
            )
        if (
            record["schema_version"] not in {"authority-editorial/v1", "company-editorial/v1"}
            or record["content_digest"] != current.content_digest
            or record["media_digest"] != current.media_digest
        ):
            raise StaleSocialApprovalError("Stored editorial review binding no longer matches")
        try:
            package = json.loads(record["package_json"])
        except (TypeError, ValueError) as exc:
            raise StaleSocialApprovalError("Stored editorial review is unreadable") from exc
        if not isinstance(package, dict):
            raise StaleSocialApprovalError("Stored editorial review is not an object")
        self._validate_editorial_payload(package, current)
        if (
            current.publisher_json is not None
            and package.get("schema_version") != "company-editorial/v1"
        ):
            raise StaleSocialApprovalError("Company draft requires company-bound editorial review")
        if (
            package.get("cta", {}).get("kind") == "resource_drop"
            and self.resource_week_for_post(post_id) is None
        ):
            raise StaleSocialApprovalError("Resource promise has no reserved weekly allowance")
        if package.get("cta", {}).get("kind") == "resource_drop":
            from social.authority_editorial import resource_registry

            cta = package["cta"]
            resource = resource_registry().get(cta.get("resource_id"))
            if resource is None or any(
                cta.get(field) != resource.get(field) for field in ("title", "digest")
            ):
                raise StaleSocialApprovalError(
                    "Promised resource is missing or changed; regenerate the resource review"
                )
        package_digest = hashlib.sha256(
            json.dumps(package, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
        ).hexdigest()
        if record["package_digest"] != package_digest:
            raise StaleSocialApprovalError(
                "Stored editorial package metadata changed after queueing"
            )
        return current

    def update_editorial_draft(
        self,
        post_id: int,
        *,
        expected_revision: int,
        body: str,
        media_path: str,
        editorial_package: dict,
        media_type: str = "image",
        title: str | None = None,
        resource_week: str | None = None,
        expected_digest: str | None = None,
        publisher_json: str | None = None,
    ) -> SocialPost:
        post = self._require_editable_draft(
            post_id,
            expected_revision=expected_revision,
            expected_digest=expected_digest,
        )
        if post.revision != expected_revision:
            raise StaleSocialApprovalError(f"Draft #{post_id} changed while being revised")
        self._validate_resource_week(resource_week)
        revised = SocialPost(**vars(post))
        revised.title = post.title if title is None else title
        revised.body = body
        revised.media_path = media_path
        revised.media_type = media_type
        revised.content_digest = compute_content_digest(revised.title, body)
        revised.media_digest = compute_media_digest(media_path)
        if publisher_json is not None:
            from social.publishers import canonical_publisher_json

            revised.publisher_json = canonical_publisher_json(publisher_json)
        self._assert_publisher(revised)
        self._validate_editorial_payload(editorial_package, revised)
        ok = self._db.update_editorial_draft(
            post_id,
            expected_revision=expected_revision,
            fields={
                "title": revised.title,
                "body": body,
                "media_path": media_path,
                "media_type": media_type,
                "content_digest": revised.content_digest,
                "media_digest": revised.media_digest,
                "verification_state": "pending",
                "receipt_json": None,
                "publisher_json": revised.publisher_json,
            },
            editorial_package=editorial_package,
            resource_week=resource_week,
            created_at=datetime.now(UTC).isoformat(timespec="microseconds"),
        )
        if not ok:
            raise StaleSocialApprovalError(f"Draft #{post_id} changed while being revised")
        updated = self._db.get(post_id)
        assert updated is not None
        return updated

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
            raise ValueError(f"Cannot schedule post {post_id} with status '{post.status}'")
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

    def set_post_fields(self, post_id: int, **fields: str | int | None) -> SocialPost:
        """Update non-status columns while preserving exact-review provenance.

        Legacy workshop callers still use this generic helper.  Any draft copy
        or media mutation therefore advances the revision and refreshes the
        corresponding digest here instead of silently invalidating the button
        contract.
        """
        post = self._db.get(post_id)
        if post is None:
            raise ValueError(f"Post {post_id} not found")
        review_fields = {"title", "body", "media_path", "media_type", "channel", "publisher_json"}
        if review_fields.intersection(fields):
            if post.status != "draft":
                raise ValueError(
                    f"Post {post_id} is already '{post.status}' and can no longer be edited"
                )
            revised = dict(fields)
            if "publisher_json" in revised:
                from social.publishers import canonical_publisher_json

                revised["publisher_json"] = canonical_publisher_json(revised["publisher_json"])
            candidate = SocialPost(**{**vars(post), **revised})
            self._assert_publisher(candidate)
            revised.update(verification_state="pending", receipt_json=None)
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

    def update_draft_copy(
        self,
        post_id: int,
        *,
        title: str,
        body: str,
        expected_revision: int | None = None,
        expected_digest: str | None = None,
    ) -> SocialPost:
        post = self._require_editable_draft(
            post_id,
            expected_revision=expected_revision,
            expected_digest=expected_digest,
        )
        ok = self._db.update_draft_revision(
            post_id,
            expected_revision=post.revision,
            fields={
                "title": title,
                "body": body,
                "content_digest": compute_content_digest(title, body),
                "verification_state": "pending",
                "receipt_json": None,
            },
        )
        if not ok:
            raise StaleSocialApprovalError(f"Draft #{post_id} changed while copy was being revised")
        updated = self._db.get(post_id)
        assert updated is not None
        return updated

    def update_draft_media(
        self,
        post_id: int,
        *,
        media_path: str | None,
        media_type: str | None,
        expected_revision: int | None = None,
        expected_digest: str | None = None,
    ) -> SocialPost:
        post = self._require_editable_draft(
            post_id,
            expected_revision=expected_revision,
            expected_digest=expected_digest,
        )
        ok = self._db.update_draft_revision(
            post_id,
            expected_revision=post.revision,
            fields={
                "media_path": media_path,
                "media_type": media_type,
                "media_digest": compute_media_digest(media_path),
                "verification_state": "pending",
                "receipt_json": None,
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
        self._assert_publisher(post)
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
        self._assert_publisher(post)
        if post.topic_source == "authority_signal" or post.publisher_json is not None:
            return self.assert_editorial_integrity(post)
        # Blank hashes identify legacy rows created before exact approvals.
        if not post.content_digest and not post.media_digest:
            return post
        if post.content_digest != compute_content_digest(post.title, post.body):
            raise StaleSocialApprovalError(f"Post {post_id} copy changed after approval")
        if post.media_digest != compute_media_digest(post.media_path):
            raise StaleSocialApprovalError(f"Post {post_id} media changed after approval")
        return post

    def supersede_legacy_linkedin_drafts(
        self, reason: str = LEGACY_LINKEDIN_SUPERSEDE_REASON
    ) -> int:
        """Preserve and mark legacy LinkedIn drafts; never called implicitly."""

        return self._db.supersede_legacy_linkedin_drafts(reason)

    @staticmethod
    def _assert_publisher(post: SocialPost) -> None:
        from social.channels import get_channel
        from social.publishers import assert_publisher_matches_channel

        assert_publisher_matches_channel(post, get_channel(post.channel))

    def _require_editable_draft(
        self,
        post_id: int,
        *,
        expected_revision: int | None = None,
        expected_digest: str | None = None,
    ) -> SocialPost:
        post = self._db.get(post_id)
        if post is None:
            raise ValueError(f"Post {post_id} not found")
        if post.status != "draft":
            raise ValueError(
                f"Post {post_id} is already '{post.status}' and can no longer be edited"
            )
        if expected_revision is not None and post.revision != expected_revision:
            raise StaleSocialApprovalError(f"Draft #{post_id} changed while being revised")
        if expected_digest is not None and approval_binding_digest(post) != expected_digest:
            raise StaleSocialApprovalError(f"Draft #{post_id} changed while being revised")
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
        if new_status == "approved" and post.publisher_json is not None and (
            expected_revision is None or expected_digest is None
        ):
            raise StaleSocialApprovalError(
                "Company posts require the current revision-bound Telegram approval card"
            )
        if expected_revision is not None or expected_digest is not None:
            post = self._refresh_draft_provenance(post)
            if expected_revision is None or expected_digest is None:
                raise StaleSocialApprovalError("Exact approval requires both revision and digest")
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
        if new_status == "approved":
            self.assert_integrity(post_id)
        changed = self._db.update_status(
            post_id,
            new_status,
            expected_status=post.status,
            expected_revision=post.revision,
            expected_content_digest=post.content_digest,
            expected_media_digest=post.media_digest,
            expected_channel=post.channel,
            expected_publisher_json=post.publisher_json,
            **fields,
        )
        if not changed:
            raise StaleSocialApprovalError(
                f"Post {post_id} changed before transition; no action was taken"
            )
        updated = self._db.get(post_id)
        assert updated is not None
        return updated
