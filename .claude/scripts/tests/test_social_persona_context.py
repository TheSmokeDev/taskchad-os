"""Persona ownership wiring for scheduled social drafts."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from social import draft_generator
from social.channels import SocialChannel


def _profile_paths(root: Path) -> dict[str, Path]:
    return {
        "memory": root / "memory",
        "state": root / "state",
    }


def test_named_persona_identity_includes_safety_but_not_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = _profile_paths(tmp_path / "socials")
    paths["memory"].mkdir(parents=True)
    (paths["memory"] / "SOUL.md").write_text("# Socials\n\nSOCIALS_SOUL_MARKER", encoding="utf-8")
    (paths["memory"] / "SAFETY.md").write_text(
        "# Safety\n\nSOCIALS_SAFETY_MARKER", encoding="utf-8"
    )
    (paths["memory"] / "HEARTBEAT.md").write_text(
        "# Heartbeat\n\nHEARTBEAT_MUST_NOT_REACH_DRAFTS", encoding="utf-8"
    )

    import personas

    monkeypatch.setattr(personas, "get_persona_paths", lambda _name: paths)

    context = draft_generator._load_persona_identity_context("socials")

    assert context is not None
    assert "SOCIALS_SOUL_MARKER" in context
    assert "SOCIALS_SAFETY_MARKER" in context
    assert "HEARTBEAT_MUST_NOT_REACH_DRAFTS" not in context


def test_missing_configured_persona_fails_closed_with_skip_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import personas

    missing_paths = _profile_paths(tmp_path / "missing")
    monkeypatch.setattr(personas, "get_persona_paths", lambda _name: missing_paths)
    monkeypatch.setattr(
        draft_generator,
        "get_channel",
        lambda _channel_id: SocialChannel(
            channel_id="linkedin",
            persona_id="missing-persona",
            voice_profile="owner-linkedin",
        ),
    )
    monkeypatch.setattr(
        draft_generator,
        "_invoke_runtime",
        lambda *_args, **_kwargs: pytest.fail("runtime must not be called"),
    )
    receipts: list[dict] = []
    monkeypatch.setattr(
        draft_generator,
        "append_social_audit_record",
        lambda **record: receipts.append(record) or "audit-id",
    )

    post_id = draft_generator.generate_draft(
        "linkedin",
        "unsupported topic",
        db_path=tmp_path / "social.db",
    )

    assert post_id is None
    assert receipts == [
        {
            "channel": "linkedin",
            "action": "draft",
            "outcome": "skipped",
            "error": "configured persona 'missing-persona' has no memory directory",
        }
    ]
    assert not (tmp_path / "social.db").exists()


def test_persona_identity_reaches_runtime_system_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    async def fake_runtime(request):
        captured["request"] = request
        return SimpleNamespace(text="draft body")

    from runtime import lane_router

    monkeypatch.setattr(lane_router, "run_with_runtime_lanes", fake_runtime)

    body = draft_generator._invoke_runtime(
        "SOURCE_PACKET_STAYS_IN_TURN_PROMPT",
        system_prompt="SOCIALS_IDENTITY_STAYS_IN_SYSTEM_PROMPT",
    )

    request = captured["request"]
    assert body == "draft body"
    assert request.prompt == "SOURCE_PACKET_STAYS_IN_TURN_PROMPT"
    assert request.system_prompt == "SOCIALS_IDENTITY_STAYS_IN_SYSTEM_PROMPT"
    assert request.allowed_tools == []
    assert request.disallowed_tools == ["*"]
    assert request.setting_sources == []
    assert request.mcp_servers == []
    assert request.model_only is True


def test_channel_without_persona_keeps_legacy_voice_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        draft_generator,
        "get_channel",
        lambda _channel_id: SocialChannel(
            channel_id="facebook",
            persona_id=None,
            voice_profile="YourBrand",
        ),
    )
    seen: dict[str, str | None] = {}

    def fake_runtime(prompt: str, *, system_prompt: str | None = None) -> str:
        seen["prompt"] = prompt
        seen["system_prompt"] = system_prompt
        return "Legacy compatible post"

    monkeypatch.setattr(draft_generator, "_invoke_runtime", fake_runtime)
    monkeypatch.setattr(
        draft_generator,
        "append_social_audit_record",
        lambda **_record: "audit-id",
    )

    post_id = draft_generator.generate_draft(
        "facebook",
        "renewal lesson",
        db_path=tmp_path / "social.db",
    )

    assert post_id is not None
    assert seen["system_prompt"] is None
    assert "renewal lesson" in str(seen["prompt"])
