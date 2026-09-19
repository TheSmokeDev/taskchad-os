from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from business_signal.models import (
    AuthorityClaim,
    AuthoritySignalPacket,
    AuthorityVisualBrief,
    authority_signal_id,
)
from social import authority_content, authority_image_factory, draft_generator
from social.authority_content import create_authority_linkedin_draft
from social.channels import SocialChannel
from social.service import SocialPostService

NOW = datetime.now(UTC)
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
    grounding = SimpleNamespace(
        grounded=True,
        exemplars=(
            SimpleNamespace(
                id=17,
                title="Grounded infographic",
                prompt="Use a clear visual hierarchy and one focal mechanism.",
                styles=("editorial",),
                scenes=("diagram",),
            ),
        ),
        resolved_case_ids=(17,),
        provenance={
            "prompt_engine": "gpt-image-2-style-library",
            "corpus_pin": "a" * 40,
            "corpus_sha256": "b" * 64,
            "license": "MIT",
        },
        full=lambda: {"grounded": True, "resolved_case_ids": [17]},
    )
    monkeypatch.setattr(
        authority_image_factory,
        "_load_style_corpus",
        lambda: SimpleNamespace(
            require_corpus=lambda: object(),
            select=lambda *_args, **_kwargs: grounding,
        ),
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


def _plan(body=None):
    return {
        "public_body": body
        or (
            "Stop rewriting the whole page.\n\nReaders can inspect cited sources.\n\n"
            "I would open one citation, compare the passage with the answer, "
            "and change one section before retesting."
        ),
        "format": "compact_workflow",
        "factual_statements": [
            {"text": "Readers can inspect cited sources.", "claim_indices": [0]}
        ],
        "cta": {"kind": "none", "text": ""},
        "visual_brief": {
            "eyebrow": "PAGE CHECK",
            "headline": "Inspect one citation",
            "accent": "",
            "subhead": "Compare. Change. Retest.",
            "cta": "",
            "concept": "Three editorial panels showing a page comparison and one marked passage",
        },
    }


def _review(prompt, **kwargs):
    from social.authority_editorial import EditorialVisualBrief, editorial_segments

    plan = _plan()
    return json.dumps(
        {
            "accepted": True,
            "visual_agreement": True,
            "resource_agreement": True,
            "useful_method": True,
            "commenter_claims_supported": True,
            "issues": [],
            "segments": [
                {
                    "segment_id": s["segment_id"],
                    "classification": "factual"
                    if s["text"] == "Readers can inspect cited sources."
                    else "recommendation",
                    "factual_claims": (
                        [
                            {
                                "text": s["text"],
                                "claim_indices": [0],
                                "supported": True,
                                "rationale": "The documentation explicitly supports inspection.",
                            }
                        ]
                        if s["text"] == "Readers can inspect cited sources."
                        else []
                    ),
                    "rationale": "An actionable editorial recommendation, not a result claim.",
                }
                for s in editorial_segments(
                    plan["public_body"], EditorialVisualBrief(**plan["visual_brief"])
                )
            ],
        }
    )


def _fake_factory(tmp_path, captured=None):
    from PIL import Image

    path = tmp_path / "reviewed.png"
    Image.new("RGB", (1080, 1350), "black").save(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    def render(packet, copy, **kwargs):
        if captured is not None:
            captured.update(copy=copy, **kwargs)
        visible = {key: value for key, value in copy.items() if value}
        copy_digest = hashlib.sha256(
            json.dumps(visible, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        return SimpleNamespace(
            media_path=str(path),
            reason=None,
            media_validation={
                "schema_version": "authority-image-review/v1",
                "validation_method": "attached_bitmap_review",
                "visual_quality_review": "operator_required",
                "accepted": True, "media_digest": digest,
                "visible_copy_digest": copy_digest, "image_inspected": True,
                "caption_agreement": True, "resource_agreement": True,
                "objective_defects": [], "reasons": [],
                "observed_text": list(visible.values()),
            },
            prompt_pack_path=None,
            manifest_path=None,
            template_id="infographic-engine",
            example_case_ids=(17,),
        )

    return render


def test_original_paraphrase_is_reviewed_before_media_and_queue(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        authority_image_factory, "render_grounded_authority_card", _fake_factory(tmp_path)
    )

    def writer(*a, **k):
        calls.append("writer")
        assert "TEST SOCIALS IDENTITY" in k["system_prompt"]
        return json.dumps(_plan())

    def reviewer(*a, **k):
        calls.append("reviewer")
        assert "independent" in k["system_prompt"]
        return _review(*a, **k)

    result = create_authority_linkedin_draft(
        _packet(),
        now=NOW,
        db_path=tmp_path / "social.db",
        deliver=False,
        model_invoke=writer,
        review_invoke=reviewer,
    )
    assert result.status == "queued", result.reasons
    assert calls == ["writer", "reviewer"]
    service = SocialPostService(tmp_path / "social.db")
    post = service.get_post(result.post_id)
    package = service.get_editorial_package(post.id)
    assert post.body == _plan()["public_body"]
    assert "Source:" not in post.body and SOURCE not in post.body
    assert package["evidence_sources"][0]["source_url"] == SOURCE
    assert package["validation"]["accepted"] is True
    assert post.media_type == "image"
    assert service.list_delivered_editorial() == []


@pytest.mark.parametrize(
    "body,reason",
    [
        (
            "I built this system. Readers can inspect cited sources.",
            "unsupported_operator_experience",
        ),
        (
            "Readers can inspect cited sources. Our revenue grew 17.3 percent.",
            "experimental_statistics_not_methods_first",
        ),
        ("Readers can inspect cited sources. YourBusiness uses this.", "private_or_secret_text"),
        (
            "Readers can inspect cited sources.\nSource: https://example.com/official/ai-search-sources",
            "public_source_narration",
        ),
    ],
)
def test_invalid_copy_never_renders_or_queues(monkeypatch, tmp_path, body, reason):
    monkeypatch.setattr(
        authority_image_factory,
        "render_grounded_authority_card",
        lambda *a, **k: pytest.fail("render happened before valid copy"),
    )
    result = create_authority_linkedin_draft(
        _packet(),
        now=NOW,
        db_path=tmp_path / "social.db",
        deliver=False,
        model_invoke=lambda *a, **k: json.dumps(_plan(body)),
    )
    assert result.status == "skipped"
    assert reason in result.reasons
    assert SocialPostService(tmp_path / "social.db").list_queue() == []


def test_model_or_review_failure_never_substitutes_canned_copy(tmp_path):
    for reviewer in (lambda *a, **k: "not json", lambda *a, **k: '{"accepted":false}'):
        result = create_authority_linkedin_draft(
            _packet(),
            now=NOW,
            db_path=tmp_path / "social.db",
            deliver=False,
            model_invoke=lambda *a, **k: json.dumps(_plan()),
            review_invoke=reviewer,
        )
        assert result.status == "skipped"
    assert SocialPostService(tmp_path / "social.db").list_queue() == []


def test_expired_packet_never_calls_writer(tmp_path):
    result = create_authority_linkedin_draft(
        _packet(expires=NOW),
        now=NOW,
        db_path=tmp_path / "social.db",
        deliver=False,
        model_invoke=lambda *a, **k: pytest.fail("expired evidence reached writer"),
    )
    assert result.status == "skipped"


def test_weak_packet_never_calls_writer(tmp_path):
    packet = _packet()
    weak = packet.model_copy(
        update={"claims": (packet.claims[0].model_copy(update={"confidence": 0.4}),)}
    )
    result = create_authority_linkedin_draft(
        weak,
        now=NOW,
        db_path=tmp_path / "social.db",
        deliver=False,
        model_invoke=lambda *a, **k: pytest.fail("weak evidence reached writer"),
    )
    assert "no_high_confidence_primary_claim" in result.reasons


def test_placeholder_title_is_not_public_evidence(tmp_path):
    packet = _packet()
    weak = packet.model_copy(update={"claims": (
        packet.claims[0].model_copy(update={"source_title": "N/A"}),)})
    result = create_authority_linkedin_draft(
        weak, now=NOW, db_path=tmp_path / "social.db", deliver=False,
        model_invoke=lambda *a, **k: pytest.fail("placeholder source reached writer"),
    )
    assert "source_title_missing" in result.reasons


def test_missing_or_disagreeing_media_blocks_queue(monkeypatch, tmp_path):
    monkeypatch.setattr(
        authority_image_factory,
        "render_grounded_authority_card",
        lambda *a, **k: SimpleNamespace(
            media_path=None,
            media_validation={"accepted": False},
            reason="image_caption_disagreement",
        ),
    )
    result = create_authority_linkedin_draft(
        _packet(),
        now=NOW,
        db_path=tmp_path / "social.db",
        deliver=False,
        model_invoke=lambda *a, **k: json.dumps(_plan()),
        review_invoke=_review,
    )
    assert result.status == "skipped"
    assert SocialPostService(tmp_path / "social.db").list_queue() == []


def test_argument_not_research_hook_drives_image(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(
        authority_image_factory, "render_grounded_authority_card", _fake_factory(tmp_path, captured)
    )
    result = create_authority_linkedin_draft(
        _packet(),
        now=NOW,
        db_path=tmp_path / "social.db",
        deliver=False,
        model_invoke=lambda *a, **k: json.dumps(_plan()),
        review_invoke=_review,
    )
    assert result.status == "queued", result.reasons
    assert captured["copy"]["headline"] == "Inspect one citation"
    assert captured["editorial_brief"]["public_body"] == _plan()["public_body"]
    assert captured["editorial_brief"]["format"] == "compact_workflow"
