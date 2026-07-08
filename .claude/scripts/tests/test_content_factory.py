"""Tests for the social content factory (queue vs autopilot, default-deny)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from social import content_factory
from social.channels import SocialChannel
from social.content_factory import _resolve_persona_refs, produce
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


# --- persona pack → --persona-ref plumbing -----------------------------------


def test_resolve_persona_refs_empty_pack_returns_empty():
    assert _resolve_persona_refs("") == []


def test_resolve_persona_refs_returns_curated_existing_subset(monkeypatch, tmp_path):
    pack = tmp_path / "image-personas" / "test-pack"
    pack.mkdir(parents=True)
    # curated subset is ref-01/02/03/07; drop ref-07 to prove existence filter
    for name in ("ref-01.png", "ref-02.png", "ref-03.png", "ref-99.png"):
        (pack / name).write_bytes(b"x")
    # _SCRIPTS_DIR.parent is the persona root — point it at tmp_path
    monkeypatch.setattr(content_factory, "_SCRIPTS_DIR", tmp_path / "scripts")
    refs = _resolve_persona_refs("test-pack")
    names = [Path(r).name for r in refs]
    assert names == ["ref-01.png", "ref-02.png", "ref-03.png"]  # ref-07 absent, filtered


def test_resolve_persona_refs_missing_pack_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(content_factory, "_SCRIPTS_DIR", tmp_path / "scripts")
    assert _resolve_persona_refs("does-not-exist") == []


def test_render_video_appends_persona_ref_args(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        raise RuntimeError("stop after cmd capture")  # fail-open past cmd build

    monkeypatch.setattr(content_factory.subprocess, "run", fake_run)
    monkeypatch.setattr(
        content_factory, "_resolve_persona_refs",
        lambda pack: ["/abs/p1.png", "/abs/p2.png"] if pack else [],
    )
    monkeypatch.setattr(content_factory, "_resolve_design_file", lambda d: None)

    content_factory._render_video("a topic", persona_pack="owner-YourBusiness-rep")
    cmd = captured["cmd"]
    positions = [i for i, t in enumerate(cmd) if t == "--persona-ref"]
    assert len(positions) == 2
    assert [cmd[i + 1] for i in positions] == ["/abs/p1.png", "/abs/p2.png"]


def test_render_video_no_pack_has_no_persona_ref_args(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        raise RuntimeError("stop after cmd capture")

    monkeypatch.setattr(content_factory.subprocess, "run", fake_run)
    monkeypatch.setattr(content_factory, "_resolve_design_file", lambda d: None)

    content_factory._render_video("a topic")  # no persona_pack
    assert "--persona-ref" not in captured["cmd"]
