"""Durable social-layer operations for the LinkedIn workshop."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from business_signal.models import AuthorityClaim, AuthoritySignalPacket, AuthorityVisualBrief
from social import (
    authority_content,
    authority_editorial,
    authority_image_factory,
    content_factory,
    draft_generator,
    linkedin_workshop,
)
from social.channels import SocialChannel
from social.models import SocialPost
from social.service import SocialPostService


def _authority_packet(root) -> AuthoritySignalPacket:
    observed = datetime.now(UTC) - timedelta(hours=1)
    key = hashlib.sha256(b"linkedin-workshop-authority").hexdigest()
    packet = AuthoritySignalPacket(
        signal_id=f"as_{observed:%Y%m%d}_{key[:16]}",
        signal_type="practical_evidence",
        observed_at=observed,
        expires_at=observed + timedelta(days=7),
        dedup_key=key,
        audience="GEO operators",
        content_series="GEO Tip",
        claims=(
            AuthorityClaim(
                text="A primary source supports this exact GEO workshop claim.",
                source_url="https://example.com/geo-source",
                source_title="Primary GEO Source",
                source_date=observed.date(),
                source_class="primary_source",
                primary_source=True,
                confidence=0.9,
            ),
        ),
        article_brief="Explain the supported GEO workshop claim in detail.",
        social_brief="Teach the supported claim with one useful test.",
        cta_brief="Save the test.",
        repo_brief="No repository promotion.",
        visual_brief=AuthorityVisualBrief(
            eyebrow="GEO Test",
            headline="Ranking Is Not Citation",
            accent="Test the answer",
            subhead="Ask, capture, inspect, and retest.",
            cta="Save the test",
        ),
        article_route="/blog",
        evidence_class="public_primary",
    )
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{packet.signal_id}.json").write_text(
        json.dumps(packet.to_public_dict()), encoding="utf-8"
    )
    return packet


def _reviewed_package(packet, body, image) -> dict:
    """Exercise real contract validation with separate offline model adapters."""
    visual = {
        "eyebrow": "GEO WORKFLOW",
        "headline": "Test one answer",
        "cta": "Save the method",
        "concept": "A buyer question beside a short answer-inspection checklist.",
    }
    draft = {
        "public_body": body,
        "format": "compact_workflow",
        "factual_statements": [],
        "cta": {"kind": "none"},
        "visual_brief": visual,
    }

    def reviewer(prompt, *, system_prompt):
        assert "independent" in system_prompt
        raw = prompt.split("<untrusted_segments>\n", 1)[1].split("\n</untrusted_segments>", 1)[0]
        return json.dumps({
            "accepted": True,
            "segments": [
                {
                    "segment_id": segment["segment_id"],
                    "classification": "recommendation",
                    "factual_claims": [],
                    "rationale": "This is an action recommendation, not an observed result.",
                }
                for segment in json.loads(raw)
            ],
            "visual_agreement": True, "resource_agreement": True,
            "useful_method": True, "commenter_claims_supported": True, "issues": [],
        })

    core = authority_editorial.generate_editorial_package(
        packet, system_prompt="Offline Socials test identity",
        model_invoke=lambda *_args, **_kwargs: json.dumps(draft), review_invoke=reviewer,
    )
    package = core.model_dump(mode="json")
    copy = {
        key: value for key, value in package["visual_brief"].items()
        if key in {"eyebrow", "headline", "accent", "subhead", "cta"} and value
    }
    media_review = authority_image_factory._review_render(
        image, copy, {"public_body": body},
        image_review_invoke=lambda *_args, **_kwargs: {
            "accepted": True, "image_inspected": True,
            "observed_text": list(copy.values()), "caption_agreement": True,
            "resource_agreement": True, "objective_defects": [],
        },
    )
    package.update(
        source_packet=packet.model_dump(mode="json"),
        source_expires_at=packet.expires_at.isoformat(), media_validation=media_review,
    )
    return package


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
    assert updated.revision == 2
    assert updated.content_digest != ""
    assert "Do not invent metrics" in prompts[0]


def test_revision_refuses_non_draft(tmp_path) -> None:
    db = tmp_path / "social.db"
    svc = SocialPostService(db_path=db)
    pid = svc.create_draft(channel="linkedin", title="T", body="B")
    svc.approve_post(pid)

    with pytest.raises(ValueError, match="can no longer be edited"):
        linkedin_workshop.revise_linkedin_copy(pid, "change it", db_path=db)


def test_authority_revision_reloads_packet_and_rebuilds_reviewed_pair(
    monkeypatch, tmp_path
) -> None:
    from business_signal import config as signal_config

    packet_dir = tmp_path / "packets"
    packet = _authority_packet(packet_dir)
    monkeypatch.setattr(signal_config, "AUTHORITY_SIGNAL_DIR", packet_dir)
    monkeypatch.setattr(
        linkedin_workshop,
        "get_channel",
        lambda _channel: SocialChannel(
            channel_id="linkedin",
            persona_id="socials",
            voice_profile="owner-linkedin",
        ),
    )
    captured: dict = {}
    image = tmp_path / "new-authority-card.png"
    image.write_bytes(b"reviewed-image")
    body = "Pick one buyer question. Capture its answer. Inspect which pages it cites."

    def builder(packet_arg, channel, **kwargs):
        captured.update(packet=packet_arg, channel=channel, **kwargs)
        return _reviewed_package(packet_arg, body, image), str(image), "educational_card"

    monkeypatch.setattr(authority_content, "build_reviewed_authority_artifacts", builder)
    monkeypatch.setattr(linkedin_workshop, "append_social_audit_record", lambda **_kw: "a")
    db = tmp_path / "social.db"
    svc = SocialPostService(db_path=db)
    # Seed a pre-migration row directly; new public service calls must not
    # create unreviewed authority drafts anymore.
    pid = svc._db.insert(SocialPost(
        channel="linkedin",
        title="Old",
        body="A primary source supports this exact GEO workshop claim.\n\n"
        "Source: https://example.com/geo-source",
        topic_source="authority_signal",
        source_packet_id=packet.signal_id,
    ))

    revised = linkedin_workshop.revise_linkedin_copy(
        pid, "Make the opening more useful", db_path=db
    )

    assert revised.revision == 2
    assert revised.body == body
    assert revised.media_path == str(image)
    assert "Make the opening more useful" not in revised.body
    assert "Source:" not in revised.body
    assert "Make the opening more useful" in captured["feedback"]
    assert captured["packet"].signal_id == packet.signal_id
    assert captured["channel"].persona_id == "socials"
    assert captured["allow_resource_drop"] is False
    assert svc.get_editorial_package(pid, 2)["public_body"] == body


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
    assert updated.revision == 2
    assert updated.media_digest != ""
    assert seen["design_file"] == "brand.json"
    assert seen["persona_pack"] == "person"


def test_authority_image_revision_uses_reviewed_factory_pair_without_persona(
    monkeypatch, tmp_path
) -> None:
    from business_signal import config as signal_config

    packet_dir = tmp_path / "packets"
    packet = _authority_packet(packet_dir)
    monkeypatch.setattr(signal_config, "AUTHORITY_SIGNAL_DIR", packet_dir)
    monkeypatch.setattr(
        linkedin_workshop,
        "get_channel",
        lambda _channel: SocialChannel(channel_id="linkedin", persona_id="socials"),
    )
    image = tmp_path / "factory.png"
    image.write_bytes(b"png")
    seen: dict = {}

    body = "Choose one buyer question. Record the answer and inspect its cited pages."

    def factory(packet_arg, channel, **kwargs):
        seen.update(packet=packet_arg, channel=channel, **kwargs)
        return _reviewed_package(packet_arg, body, image), str(image), "educational_card"

    monkeypatch.setattr(authority_content, "build_reviewed_authority_artifacts", factory)
    monkeypatch.setattr(linkedin_workshop, "append_social_audit_record", lambda **_kw: "a")
    monkeypatch.setattr(
        content_factory,
        "_render_image",
        lambda *_args, **_kwargs: pytest.fail("generic image path leaked"),
    )
    db = tmp_path / "social.db"
    svc = SocialPostService(db_path=db)
    # This is a persisted legacy row being upgraded through the workshop.
    pid = svc._db.insert(SocialPost(
        channel="linkedin",
        title="Authority",
        body="A primary source supports this exact GEO workshop claim.\n\n"
        "Source: https://example.com/geo-source",
        topic_source="authority_signal",
        source_packet_id=packet.signal_id,
    ))

    revised = linkedin_workshop.regenerate_linkedin_image(
        pid, "make the diagram bolder", db_path=db
    )

    assert revised.revision == 2
    assert revised.media_path == str(image)
    assert revised.body == body
    assert seen["packet"].signal_id == packet.signal_id
    assert seen["visual_direction"] == "make the diagram bolder"
    assert not seen["channel"].persona_pack
