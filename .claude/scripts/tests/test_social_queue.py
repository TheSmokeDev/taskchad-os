"""Tests for social post queue schema + service layer (US-001)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from social.models import SOCIAL_POST_TRANSITIONS, SocialPost
from social.service import SocialPostService


@pytest.fixture()
def svc(tmp_path: Path) -> SocialPostService:
    db_path = tmp_path / "test.db"
    return SocialPostService(db_path=db_path)


class TestSocialPostTransitions:
    def test_draft_can_transition_to_approved(self):
        assert "approved" in SOCIAL_POST_TRANSITIONS["draft"]

    def test_draft_can_transition_to_rejected(self):
        assert "rejected" in SOCIAL_POST_TRANSITIONS["draft"]

    def test_draft_cannot_transition_to_posted(self):
        assert "posted" not in SOCIAL_POST_TRANSITIONS["draft"]

    def test_approved_can_transition_to_posted(self):
        assert "posted" in SOCIAL_POST_TRANSITIONS["approved"]

    def test_approved_can_transition_to_failed(self):
        assert "failed" in SOCIAL_POST_TRANSITIONS["approved"]

    def test_posted_can_only_demote_to_failed(self):
        # posted -> failed exists for async transports (Postiz reconcile);
        # everything else stays forbidden.
        assert SOCIAL_POST_TRANSITIONS["posted"] == frozenset(["failed"])

    def test_failed_is_terminal(self):
        assert len(SOCIAL_POST_TRANSITIONS["failed"]) == 0

    def test_rejected_is_terminal(self):
        assert len(SOCIAL_POST_TRANSITIONS["rejected"]) == 0


class TestCreateDraft:
    def test_returns_post_id(self, svc: SocialPostService):
        pid = svc.create_draft(channel="linkedin", title="Test", body="Hello world")
        assert pid > 0

    def test_post_has_draft_status(self, svc: SocialPostService):
        pid = svc.create_draft(channel="linkedin", title="Test", body="Hello world")
        post = svc.get_post(pid)
        assert post is not None
        assert post.status == "draft"

    def test_post_stores_channel(self, svc: SocialPostService):
        pid = svc.create_draft(channel="facebook", title="FB Post", body="Content")
        post = svc.get_post(pid)
        assert post is not None
        assert post.channel == "facebook"

    def test_post_stores_voice_profile(self, svc: SocialPostService):
        pid = svc.create_draft(
            channel="linkedin", title="T", body="B", voice_profile="YourProduct"
        )
        post = svc.get_post(pid)
        assert post is not None
        assert post.voice_profile == "YourProduct"

    def test_post_stores_scheduled_for(self, svc: SocialPostService):
        pid = svc.create_draft(
            channel="linkedin",
            title="T",
            body="B",
            scheduled_for="2026-06-18T10:00:00+00:00",
        )
        post = svc.get_post(pid)
        assert post is not None
        assert post.scheduled_for == "2026-06-18T10:00:00+00:00"

    def test_created_at_is_set(self, svc: SocialPostService):
        pid = svc.create_draft(channel="linkedin", title="T", body="B")
        post = svc.get_post(pid)
        assert post is not None
        assert post.created_at != ""


class TestApprovePost:
    def test_transitions_to_approved(self, svc: SocialPostService):
        pid = svc.create_draft(channel="linkedin", title="T", body="B")
        post = svc.approve_post(pid)
        assert post.status == "approved"

    def test_sets_approved_at(self, svc: SocialPostService):
        pid = svc.create_draft(channel="linkedin", title="T", body="B")
        post = svc.approve_post(pid)
        assert post.approved_at is not None

    def test_cannot_approve_rejected_post(self, svc: SocialPostService):
        pid = svc.create_draft(channel="linkedin", title="T", body="B")
        svc.reject_post(pid)
        with pytest.raises(ValueError, match="Cannot transition"):
            svc.approve_post(pid)


class TestRejectPost:
    def test_transitions_to_rejected(self, svc: SocialPostService):
        pid = svc.create_draft(channel="linkedin", title="T", body="B")
        post = svc.reject_post(pid, reason="Not relevant")
        assert post.status == "rejected"

    def test_stores_rejection_reason(self, svc: SocialPostService):
        pid = svc.create_draft(channel="linkedin", title="T", body="B")
        post = svc.reject_post(pid, reason="Off brand")
        assert post.rejection_reason == "Off brand"


class TestMarkPosted:
    def test_transitions_from_approved_to_posted(self, svc: SocialPostService):
        pid = svc.create_draft(channel="linkedin", title="T", body="B")
        svc.approve_post(pid)
        post = svc.mark_posted(pid, post_url="https://linkedin.com/post/123")
        assert post.status == "posted"
        assert post.post_url == "https://linkedin.com/post/123"

    def test_sets_posted_at(self, svc: SocialPostService):
        pid = svc.create_draft(channel="linkedin", title="T", body="B")
        svc.approve_post(pid)
        post = svc.mark_posted(pid)
        assert post.posted_at is not None

    def test_cannot_post_draft(self, svc: SocialPostService):
        pid = svc.create_draft(channel="linkedin", title="T", body="B")
        with pytest.raises(ValueError, match="Cannot transition"):
            svc.mark_posted(pid)


class TestMarkFailed:
    def test_transitions_from_approved_to_failed(self, svc: SocialPostService):
        pid = svc.create_draft(channel="linkedin", title="T", body="B")
        svc.approve_post(pid)
        post = svc.mark_failed(pid, error="CDP not ready")
        assert post.status == "failed"
        assert post.error == "CDP not ready"


class TestListQueue:
    def test_returns_recent_posts(self, svc: SocialPostService):
        svc.create_draft(channel="linkedin", title="A", body="1")
        svc.create_draft(channel="facebook", title="B", body="2")
        posts = svc.list_queue()
        assert len(posts) == 2
        assert posts[0].title == "B"  # newest first

    def test_respects_limit(self, svc: SocialPostService):
        for i in range(5):
            svc.create_draft(channel="linkedin", title=f"P{i}", body=f"B{i}")
        posts = svc.list_queue(limit=3)
        assert len(posts) == 3


class TestListByStatus:
    def test_filters_by_status(self, svc: SocialPostService):
        p1 = svc.create_draft(channel="linkedin", title="A", body="1")
        svc.create_draft(channel="linkedin", title="B", body="2")
        svc.approve_post(p1)
        drafts = svc.list_by_status("draft")
        approved = svc.list_by_status("approved")
        assert len(drafts) == 1
        assert len(approved) == 1


class TestListDue:
    def test_returns_approved_posts_due_now(self, svc: SocialPostService):
        pid = svc.create_draft(
            channel="linkedin",
            title="T",
            body="B",
            scheduled_for="2020-01-01T00:00:00",
        )
        svc.approve_post(pid)
        due = svc.list_due()
        assert len(due) == 1

    def test_excludes_future_scheduled(self, svc: SocialPostService):
        pid = svc.create_draft(
            channel="linkedin",
            title="T",
            body="B",
            scheduled_for="2099-12-31T23:59:59",
        )
        svc.approve_post(pid)
        due = svc.list_due()
        assert len(due) == 0

    def test_excludes_approved_without_schedule(self, svc: SocialPostService):
        pid = svc.create_draft(channel="linkedin", title="T", body="B")
        svc.approve_post(pid)
        due = svc.list_due()
        assert len(due) == 0


class TestCountByStatus:
    def test_counts_all(self, svc: SocialPostService):
        svc.create_draft(channel="linkedin", title="A", body="1")
        svc.create_draft(channel="linkedin", title="B", body="2")
        p3 = svc.create_draft(channel="facebook", title="C", body="3")
        svc.approve_post(p3)
        counts = svc.count_by_status()
        assert counts.get("draft", 0) == 2
        assert counts.get("approved", 0) == 1

    def test_counts_by_channel(self, svc: SocialPostService):
        svc.create_draft(channel="linkedin", title="A", body="1")
        svc.create_draft(channel="facebook", title="B", body="2")
        li_counts = svc.count_by_status(channel="linkedin")
        assert li_counts.get("draft", 0) == 1


class TestSchedulePost:
    def test_schedule_draft(self, svc: SocialPostService):
        pid = svc.create_draft(channel="linkedin", title="T", body="B")
        post = svc.schedule_post(pid, "2026-06-20T10:00:00+00:00")
        assert post.scheduled_for == "2026-06-20T10:00:00+00:00"

    def test_schedule_approved(self, svc: SocialPostService):
        pid = svc.create_draft(channel="linkedin", title="T", body="B")
        svc.approve_post(pid)
        post = svc.schedule_post(pid, "2026-06-20T10:00:00+00:00")
        assert post.scheduled_for == "2026-06-20T10:00:00+00:00"

    def test_schedule_posted_raises(self, svc: SocialPostService):
        pid = svc.create_draft(channel="linkedin", title="T", body="B")
        svc.approve_post(pid)
        svc.mark_posted(pid)
        with pytest.raises(ValueError, match="Cannot schedule"):
            svc.schedule_post(pid, "2026-06-20T10:00:00+00:00")

    def test_schedule_nonexistent_raises(self, svc: SocialPostService):
        with pytest.raises(ValueError, match="not found"):
            svc.schedule_post(9999, "2026-06-20T10:00:00+00:00")

    def test_scheduled_post_appears_in_list_due(self, svc: SocialPostService):
        pid = svc.create_draft(
            channel="linkedin", title="T", body="B",
            scheduled_for="2020-01-01T00:00:00",
        )
        svc.approve_post(pid)
        due = svc.list_due()
        assert len(due) == 1
        assert due[0].id == pid

    def test_schedule_then_approve_appears_due(self, svc: SocialPostService):
        pid = svc.create_draft(channel="linkedin", title="T", body="B")
        svc.schedule_post(pid, "2020-01-01T00:00:00")
        svc.approve_post(pid)
        due = svc.list_due()
        assert len(due) == 1


class TestGetPostNotFound:
    def test_returns_none(self, svc: SocialPostService):
        assert svc.get_post(9999) is None

    def test_approve_nonexistent_raises(self, svc: SocialPostService):
        with pytest.raises(ValueError, match="not found"):
            svc.approve_post(9999)


class TestExternalRef:
    def test_mark_posted_stores_external_ref(self, svc: SocialPostService):
        pid = svc.create_draft(channel="mastodon", title="T", body="B")
        svc.approve_post(pid)
        post = svc.mark_posted(pid, external_ref="postiz:abc123")
        assert post.external_ref == "postiz:abc123"
        assert post.post_url is None

    def test_mark_posted_without_ref_leaves_none(self, svc: SocialPostService):
        pid = svc.create_draft(channel="linkedin", title="T", body="B")
        svc.approve_post(pid)
        post = svc.mark_posted(pid, post_url="https://x/1")
        assert post.external_ref is None

    def test_posted_can_demote_to_failed(self, svc: SocialPostService):
        pid = svc.create_draft(channel="mastodon", title="T", body="B")
        svc.approve_post(pid)
        svc.mark_posted(pid, external_ref="postiz:p1")
        post = svc.mark_failed(pid, error="platform error")
        assert post.status == "failed"
        assert post.error == "platform error"

    def test_posted_cannot_reapprove(self, svc: SocialPostService):
        pid = svc.create_draft(channel="mastodon", title="T", body="B")
        svc.approve_post(pid)
        svc.mark_posted(pid)
        with pytest.raises(ValueError, match="Cannot transition"):
            svc.approve_post(pid)

    def test_set_post_fields_updates_post_url(self, svc: SocialPostService):
        pid = svc.create_draft(channel="mastodon", title="T", body="B")
        svc.approve_post(pid)
        svc.mark_posted(pid, external_ref="postiz:p1")
        post = svc.set_post_fields(pid, post_url="https://mastodon.social/@x/1")
        assert post.post_url == "https://mastodon.social/@x/1"
        assert post.status == "posted"

    def test_update_fields_refuses_status(self, svc: SocialPostService):
        pid = svc.create_draft(channel="mastodon", title="T", body="B")
        with pytest.raises(ValueError, match="cannot change status"):
            svc.set_post_fields(pid, status="posted")


class TestExternalRefMigration:
    """Old-schema databases (no external_ref column) migrate idempotently."""

    _OLD_SCHEMA = """
    CREATE TABLE social_post_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft', 'approved', 'posted', 'failed', 'rejected')),
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        voice_profile TEXT NOT NULL DEFAULT '',
        topic_source TEXT NOT NULL DEFAULT 'manual',
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
        scheduled_for TEXT,
        approved_at TEXT,
        posted_at TEXT,
        post_url TEXT,
        rejection_reason TEXT,
        error TEXT,
        audit_id TEXT
    );
    """

    def _make_old_db(self, tmp_path: Path) -> Path:
        import sqlite3

        db_path = tmp_path / "old.db"
        conn = sqlite3.connect(db_path)
        conn.executescript(self._OLD_SCHEMA)
        conn.execute(
            "INSERT INTO social_post_queue (channel, title, body) VALUES (?, ?, ?)",
            ("linkedin", "Old row", "Body"),
        )
        conn.commit()
        conn.close()
        return db_path

    def test_old_db_gains_external_ref_column(self, tmp_path: Path):
        db_path = self._make_old_db(tmp_path)
        svc = SocialPostService(db_path=db_path)
        post = svc.get_post(1)
        assert post is not None
        assert post.external_ref is None

    def test_migration_is_idempotent(self, tmp_path: Path):
        db_path = self._make_old_db(tmp_path)
        SocialPostService(db_path=db_path)
        svc = SocialPostService(db_path=db_path)  # second _ensure_tables run
        pid = svc.create_draft(channel="mastodon", title="T", body="B")
        svc.approve_post(pid)
        post = svc.mark_posted(pid, external_ref="postiz:xyz")
        assert post.external_ref == "postiz:xyz"

    def test_old_db_gains_media_columns(self, tmp_path: Path):
        """A pre-media-column DB migrates and can store media fields."""
        db_path = self._make_old_db(tmp_path)
        svc = SocialPostService(db_path=db_path)
        pid = svc.create_draft(
            channel="instagram", title="T", body="B",
            media_path="/tmp/reel.mp4", media_type="video",
        )
        post = svc.get_post(pid)
        assert post.media_path == "/tmp/reel.mp4"
        assert post.media_type == "video"


class TestMediaColumn:
    def test_create_draft_persists_media(self, svc: SocialPostService):
        pid = svc.create_draft(
            channel="instagram", title="T", body="B",
            media_path="/tmp/x.mp4", media_type="video",
        )
        post = svc.get_post(pid)
        assert post.media_path == "/tmp/x.mp4"
        assert post.media_type == "video"

    def test_media_defaults_none(self, svc: SocialPostService):
        pid = svc.create_draft(channel="linkedin", title="T", body="B")
        post = svc.get_post(pid)
        assert post.media_path is None
        assert post.media_type is None
