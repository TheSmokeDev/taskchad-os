"""Tests for the social content factory (queue vs autopilot, default-deny)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from social.channels import SocialChannel
from social.content_factory import produce
from social.service import SocialPostService


@pytest.fixture()
def svc(tmp_path: Path) -> SocialPostService:
    return SocialPostService(db_path=tmp_path / "factory.db")


def _ch(**kw) -> SocialChannel:
    d = dict(channel_id="instagram", display_name="Instagram",
             execution_method="api", topic_pool=["rate tips"])
    d.update(kw)
    return SocialChannel(**d)


def test_queue_mode_only_queues(svc, monkeypatch):
    """Default (no unattended flag) → drafts queued, nothing posted."""
    monkeypatch.delenv("HOMIE_SOCIAL_UNATTENDED", raising=False)
    with patch("social.channels.get_channel", return_value=_ch()), \
         patch("social.content_factory._render_image", return_value="/tmp/x.png"), \
         patch("social.content_factory._render_video", return_value="/tmp/x.mp4"), \
         patch("social.content_factory._generate_caption", return_value="caption here"), \
         patch("social.audit.append_social_audit_record"):
        summary = produce("instagram", count=2, db_path=svc._db._db_path)

    assert summary["mode"] == "queue"
    assert len(summary["queued"]) == 2
    assert summary["posted"] == []
    # queued drafts really exist and are DRAFT (not posted)
    for pid in summary["queued"]:
        assert svc.get_post(pid).status == "draft"


def test_media_attached_to_draft(svc, monkeypatch):
    monkeypatch.delenv("HOMIE_SOCIAL_UNATTENDED", raising=False)
    with patch("social.channels.get_channel", return_value=_ch()), \
         patch("social.content_factory._render_video", return_value="/tmp/reel.mp4"), \
         patch("social.content_factory._render_image", return_value="/tmp/pic.png"), \
         patch("social.content_factory._generate_caption", return_value="cap"), \
         patch("social.audit.append_social_audit_record"):
        # media=video forces the video slot
        summary = produce("instagram", count=1, media="video", db_path=svc._db._db_path)

    post = svc.get_post(summary["queued"][0])
    assert post.media_type == "video"
    assert post.media_path == "/tmp/reel.mp4"


def test_autopilot_posts_only_when_flag_on(svc, monkeypatch):
    """Unattended=true → produce() approves + dispatches each draft."""
    monkeypatch.setenv("HOMIE_SOCIAL_UNATTENDED", "true")
    dispatched = []
    with patch("social.channels.get_channel", return_value=_ch()), \
         patch("social.content_factory._render_image", return_value="/tmp/x.png"), \
         patch("social.content_factory._render_video", return_value="/tmp/x.mp4"), \
         patch("social.content_factory._generate_caption", return_value="cap"), \
         patch("social.audit.append_social_audit_record"), \
         patch("social.post_executor.dispatch_post",
               side_effect=lambda pid, **kw: dispatched.append(pid) or True):
        summary = produce("instagram", count=2, db_path=svc._db._db_path)

    assert summary["mode"] == "autopilot"
    assert len(summary["posted"]) == 2
    assert dispatched == summary["queued"]


def test_media_failure_degrades_to_caption_only(svc, monkeypatch):
    """A media render failure never crashes the run — slot becomes caption-only."""
    monkeypatch.delenv("HOMIE_SOCIAL_UNATTENDED", raising=False)
    with patch("social.channels.get_channel", return_value=_ch()), \
         patch("social.content_factory._render_image", return_value=None), \
         patch("social.content_factory._render_video", return_value=None), \
         patch("social.content_factory._generate_caption", return_value="cap"), \
         patch("social.audit.append_social_audit_record"):
        summary = produce("instagram", count=1, media="image", db_path=svc._db._db_path)

    post = svc.get_post(summary["queued"][0])
    assert post.media_path is None
    assert post.media_type is None
    assert post.body == "cap"


def test_unknown_channel_returns_error(svc, monkeypatch):
    with patch("social.channels.get_channel", return_value=None):
        summary = produce("nope", db_path=svc._db._db_path)
    assert "error" in summary
