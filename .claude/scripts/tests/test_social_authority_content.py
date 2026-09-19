from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from business_signal.models import (
    AuthorityClaim,
    AuthoritySignalPacket,
    AuthorityVisualBrief,
    authority_signal_id,
)
from social import authority_content, draft_generator
from social.authority_content import create_authority_linkedin_draft
from social.channels import SocialChannel
from social.service import SocialPostService

NOW = datetime(2026, 9, 3, 14, 0, tzinfo=UTC)
CLAIM = "Official documentation says cited sources can be inspected by readers."
SOURCE = "https://example.com/official/ai-search-sources"


@pytest.fixture(autouse=True)
def _public_template_socials_binding(monkeypatch):
    """Tests own their persona fixture; public channels.yaml stays unbound."""

    channel = SocialChannel(
        channel_id="linkedin",
        display_name="LinkedIn",
        persona_id="socials",
        voice_profile="owner-linkedin",
        image_aspect="4:5",
    )
    monkeypatch.setattr(authority_content, "get_channel", lambda _channel_id: channel)
    monkeypatch.setattr(
        draft_generator,
        "_load_persona_identity_context",
        lambda _persona_id: "TEST SOCIALS IDENTITY",
    )


def _packet(*, expires: datetime | None = None, first_person: bool = False):
    key = hashlib.sha256(b"authority-content-test").hexdigest()
    return AuthoritySignalPacket(
        signal_id=authority_signal_id(key, NOW - timedelta(hours=1)),
        signal_type="platform_change",
        observed_at=NOW - timedelta(hours=1),
        expires_at=expires or NOW + timedelta(days=2),
        dedup_key=key,
        audience="SMB operators learning GEO",
        content_series="GEO Signal",
        claims=(
            AuthorityClaim(
                text=CLAIM,
                source_url=SOURCE,
                source_title="Official AI Search Sources",
                source_date=date(2026, 9, 2),
                source_class="official_documentation",
                primary_source=True,
                confidence=0.95,
            ),
        ),
        prohibited_claims=("guaranteed ranking",),
        privacy_notes=("private client fleet",),
        article_brief="Explain how source inspection changes GEO workflows.",
        social_brief="Teach readers to verify the citation before copying a tactic.",
        cta_brief="Save this verification checklist.",
        repo_brief="No repository promotion for this signal.",
        visual_brief=AuthorityVisualBrief(
            mode="educational_card",
            eyebrow="GEO RECEIPT",
            headline="Inspect the citation",
            accent="before the tactic",
            subhead="A source-backed workflow beats a confident guess.",
            cta="Save this",
        ),
        article_route="/blog",
        evidence_class=("verified_operator_receipt" if first_person else "public_primary"),
        first_person_allowed=first_person,
    )


def _valid_body() -> str:
    return f"{CLAIM}\n\nSave this verification checklist.\n\nSource: {SOURCE}"


def test_structured_editorial_selection_renders_only_bound_claims():
    body = authority_content._render_editorial_selection(
        '{"claim_indices":[0],"include_cta":true}', _packet()
    )
    assert body == _valid_body()
    assert authority_content.validate_authority_copy(
        body, _packet(), allow_resource_drop=False
    ) == ()


@pytest.mark.parametrize("payload", [
    '{"claim_indices":[99],"include_cta":false}',
    '{"claim_indices":[true],"include_cta":false}',
    '{"claim_indices":[0,0],"include_cta":false}',
    '{"claim_indices":[0],"include_cta":false,"story":"invented"}',
])
def test_invalid_editorial_selection_is_rejected(payload):
    with pytest.raises(ValueError):
        authority_content._render_editorial_selection(payload, _packet())


def test_copy_is_validated_before_media_and_queue(tmp_path: Path):
    calls: list[str] = []

    def model(*args, **kwargs):
        calls.append("copy")
        return "I built this system. " + _valid_body()

    def card(*args, **kwargs):
        calls.append("media")
        raise AssertionError("media must not run for unsupported autobiography")

    result = create_authority_linkedin_draft(
        _packet(), now=NOW, db_path=tmp_path / "social.db", deliver=False,
        model_invoke=model, card_renderer=card,
    )
    assert result.status == "skipped"
    assert "unsupported_autobiography" in result.reasons
    assert calls == ["copy"]
    assert SocialPostService(tmp_path / "social.db").list_queue() == []


def test_expired_packet_never_calls_model(tmp_path: Path):
    called = False

    def model(*args, **kwargs):
        nonlocal called
        called = True
        return _valid_body()

    result = create_authority_linkedin_draft(
        _packet(expires=NOW), now=NOW, db_path=tmp_path / "social.db",
        deliver=False, model_invoke=model,
    )
    assert result.status == "skipped"
    assert called is False


def test_educational_card_is_4x5_without_owner_refs_and_packet_persists(tmp_path: Path):
    captured: dict = {}

    def card(scene_prompt, copy, **kwargs):
        captured.update(kwargs)
        out = tmp_path / "card.png"
        out.write_bytes(b"\x89PNG\r\n")
        return str(out)

    packet = _packet()
    result = create_authority_linkedin_draft(
        packet, now=NOW, db_path=tmp_path / "social.db", deliver=False,
        model_invoke=lambda *a, **k: _valid_body(), card_renderer=card,
    )
    assert result.status == "queued"
    assert captured["aspect"] == "4:5"
    assert captured["refs"] is None
    post = SocialPostService(tmp_path / "social.db").get_post(result.post_id)
    assert post is not None
    assert post.source_packet_id == packet.signal_id
    assert post.status == "draft"
    assert post.media_type == "image"


def test_media_failure_degrades_to_text_only_draft(tmp_path: Path):
    result = create_authority_linkedin_draft(
        _packet(), now=NOW, db_path=tmp_path / "social.db", deliver=False,
        model_invoke=lambda *a, **k: _valid_body(),
        card_renderer=lambda *a, **k: None,
    )
    assert result.status == "queued"
    assert result.media_path is None
    post = SocialPostService(tmp_path / "social.db").get_post(result.post_id)
    assert post is not None and post.media_path is None


def test_weekly_resource_drop_cap_blocks_unapproved_cta(tmp_path: Path):
    body = _valid_body() + "\n\nComment STACK and I will send the playbook."
    result = create_authority_linkedin_draft(
        _packet(first_person=True), now=NOW, db_path=tmp_path / "social.db",
        deliver=False, allow_resource_drop=False,
        model_invoke=lambda *a, **k: body,
    )
    assert result.status == "skipped"
    assert "weekly_resource_drop_cap" in result.reasons


def test_secret_and_unsupported_extra_fact_never_queue(tmp_path: Path):
    for suffix, expected in (
        ("\n\nBearer synthetic-secret-value", "private_or_secret_text"),
        ("\n\nAI engines reward longer pages.", "unsupported_statement"),
        ("\n\nWhy does Google reward longer pages?", "unsupported_statement"),
        (
            "\n\nVerify pages because Google rewards longer content.",
            "unsupported_statement",
        ),
    ):
        result = create_authority_linkedin_draft(
            _packet(),
            now=NOW,
            db_path=tmp_path / f"{expected}.db",
            deliver=False,
            model_invoke=lambda *a, suffix=suffix, **k: _valid_body() + suffix,
        )
        assert result.status == "skipped"
        assert expected in result.reasons


def test_verified_receipt_does_not_license_unrelated_autobiography(tmp_path: Path):
    result = create_authority_linkedin_draft(
        _packet(first_person=True),
        now=NOW,
        db_path=tmp_path / "receipt.db",
        deliver=False,
        model_invoke=lambda *a, **k: _valid_body() + "\n\nI built ten client systems.",
    )
    assert result.status == "skipped"
    assert "unsupported_autobiography" in result.reasons


def test_exact_claim_prefix_cannot_smuggle_an_extra_fact(tmp_path: Path):
    body = _valid_body().replace(
        CLAIM,
        CLAIM + " Acme cuts costs by 50%.",
        1,
    )
    result = create_authority_linkedin_draft(
        _packet(),
        now=NOW,
        db_path=tmp_path / "smuggle.db",
        deliver=False,
        model_invoke=lambda *a, **k: body,
    )
    assert result.status == "skipped"
    assert "unsupported_statement" in result.reasons


def test_source_line_cannot_smuggle_an_extra_fact(tmp_path: Path):
    body = _valid_body().replace(
        f"Source: {SOURCE}",
        f"Source: {SOURCE} Acme cuts costs by 50%.",
    )
    result = create_authority_linkedin_draft(
        _packet(),
        now=NOW,
        db_path=tmp_path / "source-smuggle.db",
        deliver=False,
        model_invoke=lambda *a, **k: body,
    )
    assert result.status == "skipped"
    assert "unsupported_statement" in result.reasons
