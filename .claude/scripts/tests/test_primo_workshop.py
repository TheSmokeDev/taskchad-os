"""Durable Primo X workshop operation tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from social import primo_workshop
from social.primo_workshop import PrimoImageRequiredError
from social.service import SocialPostService


def _factory_producer(db_path: Path, *, media_path: str | None):
    def fake_produce(channel, **kwargs):
        assert channel == "x"
        assert kwargs["autopilot"] is False
        svc = SocialPostService(db_path=db_path)
        pid = svc.create_draft(
            channel="x",
            title="Primo",
            body="Crypto agents need verifiable execution.",
            voice_profile="primo-x",
            topic_source=kwargs["topic_source"],
            media_path=media_path,
            media_type="image" if media_path else None,
        )
        return {"queued": [pid], "posted": [], "failed": [], "mode": "queue"}

    return fake_produce


def test_create_primo_image_draft_requires_readable_media(tmp_path, monkeypatch):
    db = tmp_path / "social.db"
    monkeypatch.setattr(
        "social.content_factory.produce",
        _factory_producer(db, media_path=None),
    )

    with pytest.raises(PrimoImageRequiredError) as exc:
        primo_workshop.create_primo_draft(
            topic="agent wallets", mode="cook", media_mode="image", db_path=db
        )

    post = SocialPostService(db_path=db).get_post(exc.value.post_id)
    assert post is not None
    assert post.status == "draft"
    assert post.media_path is None


def test_create_primo_auto_allows_text_fallback(tmp_path, monkeypatch):
    db = tmp_path / "social.db"
    monkeypatch.setattr(
        "social.content_factory.produce",
        _factory_producer(db, media_path=None),
    )

    post = primo_workshop.create_primo_draft(
        topic=None, mode="run", media_mode="auto", db_path=db
    )

    assert post.status == "draft"
    assert post.media_path is None
    assert post.topic_source == "primo-workshop:run:auto"


def test_create_primo_image_draft_keeps_media(tmp_path, monkeypatch):
    db = tmp_path / "social.db"
    image = tmp_path / "primo.png"
    image.write_bytes(b"png")
    monkeypatch.setattr(
        "social.content_factory.produce",
        _factory_producer(db, media_path=str(image)),
    )

    post = primo_workshop.create_primo_draft(
        topic="agent wallets", mode="cook", media_mode="image", db_path=db
    )

    assert post.media_type == "image"
    assert post.media_path == str(image)


def test_revise_copy_preserves_image(tmp_path, monkeypatch):
    db = tmp_path / "social.db"
    image = tmp_path / "original.png"
    image.write_bytes(b"png")
    svc = SocialPostService(db_path=db)
    pid = svc.create_draft(
        channel="x",
        title="Old",
        body="Old body",
        voice_profile="primo-x",
        media_path=str(image),
        media_type="image",
    )
    monkeypatch.setattr(
        "social.draft_generator._invoke_runtime",
        lambda _prompt: "New technically credible Primo post",
    )
    monkeypatch.setattr("social.primo_workshop.append_social_audit_record", lambda **_: "a")

    post = primo_workshop.revise_primo_copy(pid, "sharper", db_path=db)

    assert post.body == "New technically credible Primo post"
    assert post.media_path == str(image)


def test_regenerate_versions_media_and_remove_only_detaches(tmp_path, monkeypatch):
    db = tmp_path / "social.db"
    old = tmp_path / "old.png"
    new = tmp_path / "new-v2.png"
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    svc = SocialPostService(db_path=db)
    pid = svc.create_draft(
        channel="x",
        title="Primo",
        body="Agent infrastructure",
        media_path=str(old),
        media_type="image",
    )
    seen: dict = {}

    def fake_render(channel_id, prompt, **kwargs):
        seen.update(channel_id=channel_id, prompt=prompt, **kwargs)
        return str(new)

    monkeypatch.setattr(
        primo_workshop, "get_channel",
        lambda _cid: SimpleNamespace(
            design_file="brand_designs/primo-x.json",
            image_aspect="16:9",
        ),
    )
    monkeypatch.setattr("social.content_factory._render_image", fake_render)
    monkeypatch.setattr("social.primo_workshop.append_social_audit_record", lambda **_: "a")

    regenerated = primo_workshop.regenerate_primo_image(
        pid, "terminal geometry", db_path=db
    )
    assert regenerated.media_path == str(new)
    assert old.is_file()
    assert seen["aspect"] == "16:9"
    assert seen["persona_pack"] == ""

    removed = primo_workshop.remove_primo_image(pid, db_path=db)
    assert removed.media_path is None
    assert removed.media_type is None
    assert new.is_file()

