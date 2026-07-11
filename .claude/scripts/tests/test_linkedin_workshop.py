"""Durable social-layer operations for the LinkedIn workshop."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from social import content_factory, draft_generator, linkedin_workshop
from social.channels import SocialChannel
from social.service import SocialPostService


def test_create_linkedin_draft_forces_image_and_queue_mode(monkeypatch, tmp_path) -> None:
    seen: dict = {}

    def fake_produce(channel, **kwargs):
        seen.update(channel=channel, **kwargs)
        svc = SocialPostService(db_path=tmp_path / "social.db")
        pid = svc.create_draft(
            channel="linkedin", title="T", body="B", media_path="x.png", media_type="image"
        )
        return {"queued": [pid]}

    monkeypatch.setattr(content_factory, "produce", fake_produce)

    post = linkedin_workshop.create_linkedin_draft(
        topic="real lesson",
        mode="cook",
        db_path=tmp_path / "social.db",
    )

    assert post.status == "draft"
    assert seen["channel"] == "linkedin"
    assert seen["media"] == "image"
    assert seen["autopilot"] is False
    assert seen["topic"] == "real lesson"


def test_revise_updates_same_draft_without_inventing_prompt(monkeypatch, tmp_path) -> None:
    db = tmp_path / "social.db"
    svc = SocialPostService(db_path=db)
    pid = svc.create_draft(
        channel="linkedin",
        title="Old",
        body="Existing true detail",
        voice_profile="",
    )
    prompts: list[str] = []

    def fake_runtime(prompt: str) -> str:
        prompts.append(prompt)
        return "Revised true detail"

    monkeypatch.setattr(draft_generator, "_invoke_runtime", fake_runtime)
    monkeypatch.setattr(linkedin_workshop, "append_social_audit_record", lambda **kw: "a")

    updated = linkedin_workshop.revise_linkedin_copy(
        pid,
        "Make it tighter",
        db_path=db,
    )

    assert updated.id == pid
    assert updated.body == "Revised true detail"
    assert "Do not invent metrics" in prompts[0]


def test_revision_refuses_non_draft(tmp_path) -> None:
    db = tmp_path / "social.db"
    svc = SocialPostService(db_path=db)
    pid = svc.create_draft(channel="linkedin", title="T", body="B")
    svc.approve_post(pid)

    with pytest.raises(ValueError, match="can no longer be edited"):
        linkedin_workshop.revise_linkedin_copy(pid, "change it", db_path=db)


def test_regenerate_image_updates_same_row(monkeypatch, tmp_path) -> None:
    db = tmp_path / "social.db"
    image = tmp_path / "new.png"
    image.write_bytes(b"png")
    svc = SocialPostService(db_path=db)
    pid = svc.create_draft(channel="linkedin", title="T", body="Post context")
    monkeypatch.setattr(
        linkedin_workshop,
        "get_channel",
        lambda channel: SocialChannel(
            channel_id="linkedin",
            design_file="brand.json",
            persona_pack="person",
        ),
    )
    seen: dict = {}

    def fake_render(channel, prompt, **kwargs):
        seen.update(channel=channel, prompt=prompt, **kwargs)
        return str(image)

    monkeypatch.setattr(content_factory, "_render_image", fake_render)
    monkeypatch.setattr(linkedin_workshop, "append_social_audit_record", lambda **kw: "a")

    updated = linkedin_workshop.regenerate_linkedin_image(
        pid,
        "editorial control room",
        db_path=db,
    )

    assert updated.id == pid
    assert updated.media_path == str(image)
    assert updated.media_type == "image"
    assert seen["design_file"] == "brand.json"
    assert seen["persona_pack"] == "person"
