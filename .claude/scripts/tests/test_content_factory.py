"""Tests for the social content factory (queue vs autopilot, default-deny)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from social import content_factory
from social.brand_packs import BrandPackError, load_brand_pack
from social.channels import SocialChannel
from social.content_factory import _resolve_persona_refs, produce, produce_brand_content
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


def test_source_packet_id_reaches_durable_draft(svc, monkeypatch):
    monkeypatch.delenv("HOMIE_SOCIAL_UNATTENDED", raising=False)
    with patch("social.channels.get_channel", return_value=_ch()), patch(
        "social.content_factory._generate_caption", return_value="grounded caption"
    ), patch("social.audit.append_social_audit_record"):
        summary = produce(
            "instagram",
            media="none",
            source_packet_id="authority-signal-20260903-001",
            db_path=svc._db._db_path,
        )

    post = svc.get_post(summary["queued"][0])
    assert post.source_packet_id == "authority-signal-20260903-001"


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


def test_autopilot_false_forces_queue_even_when_flag_on(svc, monkeypatch):
    """autopilot=False overrides HOMIE_SOCIAL_UNATTENDED=true → queue-only.
    This is the contract the operator-approval cadence relies on: it must never
    auto-post even if the operator flipped the global flag for the batch lane."""
    monkeypatch.setenv("HOMIE_SOCIAL_UNATTENDED", "true")
    dispatched = []
    with patch("social.channels.get_channel", return_value=_ch()), \
         patch("social.content_factory._render_image", return_value="/tmp/x.png"), \
         patch("social.content_factory._render_video", return_value="/tmp/x.mp4"), \
         patch("social.content_factory._generate_caption", return_value="cap"), \
         patch("social.audit.append_social_audit_record"), \
         patch("social.post_executor.dispatch_post",
               side_effect=lambda pid, **kw: dispatched.append(pid) or True):
        summary = produce("instagram", count=1, autopilot=False, db_path=svc._db._db_path)

    assert summary["mode"] == "queue"
    assert summary["posted"] == []
    assert dispatched == []
    assert svc.get_post(summary["queued"][0]).status == "draft"


def test_autopilot_true_forces_post_even_when_flag_off(svc, monkeypatch):
    """autopilot=True overrides an absent flag → posts."""
    monkeypatch.delenv("HOMIE_SOCIAL_UNATTENDED", raising=False)
    dispatched = []
    with patch("social.channels.get_channel", return_value=_ch()), \
         patch("social.content_factory._render_image", return_value="/tmp/x.png"), \
         patch("social.content_factory._render_video", return_value="/tmp/x.mp4"), \
         patch("social.content_factory._generate_caption", return_value="cap"), \
         patch("social.audit.append_social_audit_record"), \
         patch("social.post_executor.dispatch_post",
               side_effect=lambda pid, **kw: dispatched.append(pid) or True):
        summary = produce("instagram", count=1, autopilot=True, db_path=svc._db._db_path)

    assert summary["mode"] == "autopilot"
    assert len(summary["posted"]) == 1
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


def test_legacy_summary_shape_is_unchanged(svc, monkeypatch):
    monkeypatch.delenv("HOMIE_SOCIAL_UNATTENDED", raising=False)
    with patch("social.channels.get_channel", return_value=_ch()), \
         patch("social.content_factory._generate_caption", return_value="cap"), \
         patch("social.audit.append_social_audit_record"):
        summary = produce(
            "instagram", media="none", db_path=svc._db._db_path
        )

    assert set(summary) == {"channel", "mode", "queued", "posted", "failed"}


def test_scheduled_factory_passes_persona_identity_as_system_prompt(svc, monkeypatch):
    from social import draft_generator as dg

    captured = {}

    def fake_runtime(prompt, *, system_prompt=None):
        captured["prompt"] = prompt
        captured["system_prompt"] = system_prompt
        return "persona-owned caption"

    monkeypatch.setattr(
        dg,
        "_load_persona_identity_context",
        lambda persona_id: f"IDENTITY:{persona_id}",
    )
    monkeypatch.setattr(dg, "_invoke_runtime", fake_runtime)
    with patch(
        "social.channels.get_channel",
        return_value=_ch(persona_id="socials", voice_profile="owner-linkedin"),
    ), patch("social.audit.append_social_audit_record"):
        summary = produce(
            "instagram",
            media="none",
            topic="SOURCE_PACKET_IN_TURN_PROMPT",
            topic_source="cadence",
            autopilot=False,
            db_path=svc._db._db_path,
        )

    assert captured["system_prompt"] == "IDENTITY:socials"
    assert "SOURCE_PACKET_IN_TURN_PROMPT" in captured["prompt"]
    assert svc.get_post(summary["queued"][0]).body == "persona-owned caption"


def test_scheduled_factory_missing_persona_skips_with_receipt(svc, monkeypatch):
    from social import draft_generator as dg

    def missing(_persona_id):
        raise dg.PersonaContextUnavailableError(
            "configured persona 'missing' has no memory directory"
        )

    monkeypatch.setattr(dg, "_load_persona_identity_context", missing)
    receipts = []
    render_image = patch("social.content_factory._render_image")
    monkeypatch.setattr(
        "social.audit.append_social_audit_record",
        lambda **kwargs: receipts.append(kwargs) or "receipt",
    )
    with patch(
        "social.channels.get_channel",
        return_value=_ch(persona_id="missing"),
    ), render_image as renderer, pytest.raises(dg.PersonaContextUnavailableError):
        produce(
            "instagram",
            media="image",
            topic="must not become a draft",
            topic_source="cadence",
            autopilot=False,
            db_path=svc._db._db_path,
        )

    assert svc.list_queue() == []
    renderer.assert_not_called()
    assert receipts == [
        {
            "channel": "instagram",
            "action": "draft",
            "outcome": "skipped",
            "error": "configured persona 'missing' has no memory directory",
        }
    ]


def test_configured_persona_empty_runtime_never_falls_back_to_topic(svc, monkeypatch):
    from social import draft_generator as dg

    monkeypatch.setattr(
        dg, "_load_persona_identity_context", lambda _persona_id: "IDENTITY"
    )
    monkeypatch.setattr(dg, "_invoke_runtime", lambda *args, **kwargs: "")
    receipts = []
    monkeypatch.setattr(
        "social.audit.append_social_audit_record",
        lambda **kwargs: receipts.append(kwargs) or "receipt",
    )
    with patch(
        "social.channels.get_channel",
        return_value=_ch(persona_id="socials"),
    ), pytest.raises(RuntimeError, match="returned no content"):
        produce(
            "instagram",
            media="none",
            topic="must not become the body",
            topic_source="cadence",
            autopilot=False,
            db_path=svc._db._db_path,
        )

    assert svc.list_queue() == []
    assert receipts[0]["outcome"] == "skipped"


def test_legacy_caption_runtime_failure_keeps_topic_fallback(svc, monkeypatch):
    from social import draft_generator as dg

    def runtime_failure(*args, **kwargs):
        raise RuntimeError("legacy runtime down")

    monkeypatch.setattr(dg, "_load_persona_identity_context", lambda value: None)
    monkeypatch.setattr(dg, "_invoke_runtime", runtime_failure)
    with patch(
        "social.channels.get_channel",
        return_value=_ch(persona_id=None),
    ), patch("social.audit.append_social_audit_record"):
        summary = produce(
            "instagram",
            media="none",
            topic="legacy fallback topic",
            db_path=svc._db._db_path,
        )

    assert svc.get_post(summary["queued"][0]).body == "legacy fallback topic"


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


def test_render_video_passes_requested_aspect_exactly_once(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        raise RuntimeError("stop after cmd capture")

    monkeypatch.setattr(content_factory.subprocess, "run", fake_run)
    monkeypatch.setattr(content_factory, "_resolve_design_file", lambda d: None)

    content_factory._render_video("a topic", aspect="16:9")

    cmd = captured["cmd"]
    assert cmd.count("--aspect") == 1
    assert cmd[cmd.index("--aspect") + 1] == "16:9"


def test_render_video_rejects_unsupported_aspect_before_provider(monkeypatch):
    invoked = False

    def fake_run(cmd, **kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("provider must not be invoked")

    monkeypatch.setattr(content_factory.subprocess, "run", fake_run)

    assert content_factory._render_video("a topic", aspect="3:2") is None
    assert invoked is False


def test_render_video_parses_complete_verified_multiline_receipt(monkeypatch, tmp_path):
    mp4 = tmp_path / "verified.mp4"
    mp4.write_bytes(b"verified-render")
    receipt = {
        "ok": True,
        "mp4_path": str(mp4),
        "output_dir": str(tmp_path),
    }

    monkeypatch.setattr(
        content_factory.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="renderer log line\n" + json.dumps(receipt, indent=2),
        ),
    )
    monkeypatch.setattr(content_factory, "_resolve_design_file", lambda value: None)

    assert content_factory._render_video("a topic") == str(mp4.resolve())


def test_render_video_never_reuses_stale_file_after_failed_receipt(monkeypatch, tmp_path):
    stale = tmp_path / "stale-other-tenant.mp4"
    stale.write_bytes(b"unrelated")
    receipt = {"ok": False, "mp4_path": str(stale), "error": "render failed"}
    monkeypatch.setattr(
        content_factory.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout=json.dumps(receipt, indent=2),
        ),
    )

    assert content_factory._render_video("a topic") is None


def test_real_video_pipeline_cli_enforces_factory_aspect_contract():
    pipeline = Path(content_factory.__file__).resolve().parents[1] / "video_pipeline.py"
    accepted = subprocess.run(
        [sys.executable, str(pipeline), "--list-styles", "--aspect", "16:9"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    rejected = subprocess.run(
        [sys.executable, str(pipeline), "--list-styles", "--aspect", "4:5"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert accepted.returncode == 0
    assert rejected.returncode == 2
    assert "invalid choice" in rejected.stderr


# --- _render_image: brand design + persona refs plumbing ---------------------


def _capture_image_gen(monkeypatch):
    """Patch video_imagegen.generate_image to capture its kwargs; returns dict."""
    import video_imagegen

    captured: dict = {}

    def fake_gen(*, prompt, design, aspect, assets_dir, name, refs=None, **kw):
        captured.update(
            prompt=prompt, design=design, aspect=aspect, name=name, refs=refs
        )
        return "assets/scene.png"

    monkeypatch.setattr(video_imagegen, "generate_image", fake_gen)
    return captured


def test_render_image_passes_design_and_refs_when_channel_has_them(monkeypatch):
    captured = _capture_image_gen(monkeypatch)
    monkeypatch.setattr(
        content_factory, "_resolve_design_file", lambda d: "/abs/brand.json"
    )
    monkeypatch.setattr(
        content_factory, "_resolve_persona_refs",
        lambda pack: ["/abs/ref-01.png", "/abs/ref-02.png"] if pack else [],
    )
    import video_styles

    monkeypatch.setattr(
        video_styles, "resolve_design",
        lambda **kw: {"palette": {"bg": "#FFF"}},
    )

    content_factory._render_image(
        "instagram", "rate tips",
        design_file="social/brand_designs/x.json", persona_pack="owner-rep",
        aspect="16:9",
    )
    assert captured["design"] == {"palette": {"bg": "#FFF"}}
    assert captured["refs"] == ["/abs/ref-01.png", "/abs/ref-02.png"]
    assert captured["aspect"] == "16:9"


def test_render_image_neutral_when_channel_has_neither(monkeypatch):
    captured = _capture_image_gen(monkeypatch)
    # No design file, no persona pack -> byte-identical to the prior behavior.
    monkeypatch.setattr(content_factory, "_resolve_design_file", lambda d: None)
    monkeypatch.setattr(content_factory, "_resolve_persona_refs", lambda pack: [])

    content_factory._render_image("instagram", "rate tips")
    assert captured["design"] == {}
    assert captured["refs"] is None


def test_render_image_rejects_unsupported_aspect_before_provider(monkeypatch):
    import video_imagegen

    invoked = False

    def fake_generate_image(**kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("provider must not be invoked")

    monkeypatch.setattr(video_imagegen, "generate_image", fake_generate_image)

    assert content_factory._render_image("instagram", "topic", aspect="3:2") is None
    assert invoked is False


def test_produce_passes_channel_image_aspect_to_renderer(svc, monkeypatch):
    render = patch("social.content_factory._render_image", return_value="/tmp/pic.png")
    with patch(
        "social.channels.get_channel", return_value=_ch(image_aspect="4:5")
    ), render as render_image, patch(
        "social.content_factory._generate_caption", return_value="cap"
    ), patch("social.audit.append_social_audit_record"):
        produce(
            "instagram", media="image", db_path=svc._db._db_path,
        )

    assert render_image.call_args.kwargs["aspect"] == "4:5"


def test_produce_uses_legacy_vertical_video_default(svc, monkeypatch):
    render = patch("social.content_factory._render_video", return_value="/tmp/reel.mp4")
    with patch("social.channels.get_channel", return_value=_ch()), \
         render as render_video, \
         patch("social.content_factory._generate_caption", return_value="cap"), \
         patch("social.audit.append_social_audit_record"):
        produce(
            "instagram", media="video", db_path=svc._db._db_path,
        )

    assert render_video.call_args.kwargs["aspect"] == "9:16"


def _loaded_brand_pack(
    tmp_path: Path,
    *,
    allowed_consumers: list[str] | None = None,
):
    pack_root = tmp_path / "packs" / "orbit"
    pack_root.mkdir(parents=True)
    (pack_root / "voice.md").write_text("Direct and educational.", encoding="utf-8")
    (pack_root / "design.json").write_text("{}", encoding="utf-8")
    (pack_root / "persona").mkdir()
    manifest = {
        "pack_id": "orbit",
        "schema_version": 1,
        "display_name": "Orbit",
        "version": "1.0.0",
        "voice_profile": "voice.md",
        "design_file": "design.json",
        "persona_pack": "persona",
        "image_aspect": "4:5",
        "video_aspect": "16:9",
        "default_media_policy": "image",
        "provenance": "test-fixture",
        "allowed_consumers": allowed_consumers or ["social"],
    }
    source = pack_root / "brand-pack.json"
    source.write_text(json.dumps(manifest), encoding="utf-8")
    return load_brand_pack(source, approved_roots=[tmp_path / "packs"])


def test_brand_adapter_overlays_pack_and_forces_queue_only(svc, monkeypatch, tmp_path):
    pack = _loaded_brand_pack(tmp_path)
    monkeypatch.setenv("HOMIE_SOCIAL_UNATTENDED", "true")
    dispatched = []
    with patch(
        "social.channels.get_channel",
        return_value=_ch(
            voice_profile="legacy",
            design_file="legacy.json",
            persona_pack="legacy-persona",
            image_aspect="1:1",
        ),
    ), patch(
        "social.content_factory._render_image", return_value="/tmp/card.png"
    ) as render_image, patch(
        "social.content_factory._generate_caption", return_value="cap"
    ) as caption, patch(
        "social.audit.append_social_audit_record"
    ), patch(
        "social.post_executor.dispatch_post",
        side_effect=lambda pid, **kw: dispatched.append(pid) or True,
    ):
        summary = produce_brand_content(
            "instagram", brand_pack=pack, db_path=svc._db._db_path
        )

    assert summary["mode"] == "queue"
    assert summary["posted"] == []
    assert dispatched == []
    assert caption.call_args.args[2] == str(pack.voice_profile)
    assert render_image.call_args.kwargs == {
        "design_file": str(pack.design_file),
        "persona_pack": str(pack.persona_pack),
        "aspect": "4:5",
    }
    post = svc.get_post(summary["queued"][0])
    assert post.status == "draft"
    assert post.voice_profile == str(pack.voice_profile)
    assert summary["brand_pack"] == {
        "pack_id": "orbit",
        "version": "1.0.0",
        "source_hash": pack.source_hash,
    }
    assert summary["media_outcomes"] == [
        {
            "slot": 0,
            "requested": "image",
            "status": "generated",
            "reason": None,
        }
    ]


def test_brand_adapter_reports_truthful_media_degradation(svc, tmp_path):
    pack = _loaded_brand_pack(tmp_path)
    with patch("social.channels.get_channel", return_value=_ch()), \
         patch("social.content_factory._render_image", return_value=None), \
         patch("social.content_factory._generate_caption", return_value="safe caption"), \
         patch("social.audit.append_social_audit_record"):
        summary = produce_brand_content(
            "instagram", brand_pack=pack, db_path=svc._db._db_path
        )

    assert summary["mode"] == "queue"
    assert summary["media_outcomes"] == [
        {
            "slot": 0,
            "requested": "image",
            "status": "degraded",
            "reason": "media_generation_failed",
        }
    ]
    post = svc.get_post(summary["queued"][0])
    assert post.body == "safe caption"
    assert post.media_path is None


def test_brand_adapter_contains_renderer_exception_and_queues_caption(svc, tmp_path):
    pack = _loaded_brand_pack(tmp_path)
    with patch("social.channels.get_channel", return_value=_ch()), patch(
        "social.content_factory._render_image", side_effect=RuntimeError("provider down")
    ), patch(
        "social.content_factory._generate_caption", return_value="safe caption"
    ), patch("social.audit.append_social_audit_record"):
        summary = produce_brand_content(
            "instagram", brand_pack=pack, db_path=svc._db._db_path
        )

    assert summary["media_outcomes"][0]["status"] == "degraded"
    post = svc.get_post(summary["queued"][0])
    assert post.status == "draft"
    assert post.body == "safe caption"
    assert post.media_path is None


def test_brand_adapter_passes_distinct_video_aspect(svc, tmp_path):
    pack = _loaded_brand_pack(tmp_path)
    with patch("social.channels.get_channel", return_value=_ch()), patch(
        "social.content_factory._render_video", return_value="/tmp/video.mp4"
    ) as render_video, patch(
        "social.content_factory._generate_caption", return_value="safe caption"
    ), patch("social.audit.append_social_audit_record"):
        produce_brand_content(
            "instagram",
            brand_pack=pack,
            media="video",
            db_path=svc._db._db_path,
        )

    assert render_video.call_args.kwargs["aspect"] == "16:9"


def test_brand_adapter_rejects_unauthorized_consumer_without_draft(svc, tmp_path):
    pack = _loaded_brand_pack(tmp_path, allowed_consumers=["content"])

    summary = produce_brand_content(
        "instagram", brand_pack=pack, db_path=svc._db._db_path
    )

    assert summary == {
        "error": "brand pack is not authorized for the social consumer"
    }
    assert svc.list_queue() == []


def test_direct_produce_rejects_unauthorized_pack_before_any_work(svc, tmp_path):
    pack = _loaded_brand_pack(tmp_path, allowed_consumers=["content"])
    with patch("social.channels.get_channel") as get_channel, patch(
        "social.content_factory._generate_caption"
    ) as caption, patch("social.content_factory._render_image") as render_image:
        summary = produce(
            "instagram",
            media="image",
            brand_pack=pack,
            autopilot=True,
            db_path=svc._db._db_path,
        )

    assert summary == {
        "error": "brand pack is not authorized for the social consumer"
    }
    get_channel.assert_not_called()
    caption.assert_not_called()
    render_image.assert_not_called()
    assert svc.list_queue() == []


def test_direct_produce_revalidates_pack_containment_before_any_work(svc, tmp_path):
    pack = _loaded_brand_pack(tmp_path)
    outside = tmp_path / "outside-voice.md"
    outside.write_text("must never reach runtime", encoding="utf-8")
    object.__setattr__(pack, "voice_profile", outside.resolve())

    with patch("social.channels.get_channel") as get_channel, patch(
        "social.content_factory._generate_caption"
    ) as caption, pytest.raises(BrandPackError, match="voice_profile.*outside"):
        produce(
            "instagram",
            brand_pack=pack,
            media="none",
            db_path=svc._db._db_path,
        )

    get_channel.assert_not_called()
    caption.assert_not_called()
    assert svc.list_queue() == []


@pytest.mark.parametrize(
    ("aspect", "filename", "size"),
    [
        ("16:9", "primo-16x9.png", (1600, 900)),
        ("4:5", "primo-4x5.png", (1080, 1350)),
    ],
)
def test_normalize_image_aspect_versions_original(
    monkeypatch,
    tmp_path,
    aspect,
    filename,
    size,
):
    from PIL import Image

    source = tmp_path / "primo.png"
    Image.new("RGB", (1080, 1080), "#07070B").save(source)

    normalized = content_factory._normalize_image_aspect(source, aspect)

    assert normalized is not None
    normalized_path = Path(normalized)
    assert normalized_path.name == filename
    assert source.is_file()
    with Image.open(normalized_path) as image:
        assert image.size == size
