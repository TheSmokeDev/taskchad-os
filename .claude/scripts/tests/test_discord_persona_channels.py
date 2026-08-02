"""Discord channel bindings route to real persona profile context."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

CHAT_DIR = Path(__file__).resolve().parents[2] / "chat"
if str(CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(CHAT_DIR))

from discord_channel_bindings import (  # noqa: E402
    DiscordChannelBinding,
    load_discord_channel_bindings,
    resolve_discord_channel_binding,
    watched_channel_ids,
)
from discord_persona_runtime import run_discord_persona_channel_turn  # noqa: E402
from models import Channel, IncomingMessage, Platform, Thread, User  # noqa: E402
from session import get_session_store  # noqa: E402

from personas.discord_bindings import (  # noqa: E402
    DiscordBindingError,
    load_binding_document,
    reconcile_persona_bindings,
)
from runtime.base import RuntimeResult  # noqa: E402
from runtime.errors import RuntimeCallerToolTransportError  # noqa: E402


def _write_profile(homie_root: Path, persona_id: str) -> Path:
    profile_root = homie_root / "profiles" / persona_id
    memory_dir = profile_root / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (profile_root / "run").mkdir(parents=True, exist_ok=True)
    (profile_root / "skills").mkdir(parents=True, exist_ok=True)
    (profile_root / "config.yaml").write_text(
        "\n".join(
            [
                "persona:",
                f"  display_name: {persona_id.title()} Homie",
                f"  role: {persona_id} role marker",
                "cabinet:",
                "  tools: []",
                "  voice_persona_prompt: |",
                f"    {persona_id.upper()}_VOICE_PROMPT",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (memory_dir / "SOUL.md").write_text(
        f"# Soul\n{persona_id.upper()}_SOUL_MARKER", encoding="utf-8"
    )
    (memory_dir / "MEMORY.md").write_text(
        f"# Memory\n{persona_id.upper()}_MEMORY_MARKER", encoding="utf-8"
    )
    return profile_root


def _incoming(channel_id: str, guild_id: str = "guild-1") -> IncomingMessage:
    return IncomingMessage(
        text="what should we do next?",
        user=User(Platform.DISCORD, "user-1", "Operator"),
        channel=Channel(Platform.DISCORD, channel_id, is_dm=False),
        platform=Platform.DISCORD,
        thread=Thread(channel_id),
        raw_event={"guild": guild_id},
    )


def test_load_bindings_and_watched_channels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding_file = tmp_path / "bindings.json"
    binding_file.write_text(
        json.dumps(
            {
                "guild_id": "guild-1",
                "channels": {
                    "1": {"name": "default", "kind": "default"},
                    "2": {"name": "sales", "persona": "sales"},
                    "4": {
                        "name": "staged",
                        "kind": "persona",
                        "persona": "staged",
                        "enabled": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DISCORD_CHANNEL_BINDINGS_FILE", str(binding_file))
    monkeypatch.setenv("DISCORD_WATCHED_CHANNELS", "3")

    bindings = load_discord_channel_bindings()
    assert bindings["2"].persona_id == "sales"
    assert watched_channel_ids() == ["1", "2", "3"]
    assert resolve_discord_channel_binding(_incoming("1")) is None
    assert resolve_discord_channel_binding(_incoming("2")).persona_id == "sales"
    assert resolve_discord_channel_binding(_incoming("4")) is None
    assert resolve_discord_channel_binding(_incoming("2", guild_id="other")) is None


def test_strict_mutation_reader_refuses_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "bindings.json"
    path.write_text("{", encoding="utf-8")

    with pytest.raises(DiscordBindingError, match="invalid Discord"):
        load_binding_document(path, strict=True)
    assert load_discord_channel_bindings(path) == {}


def test_fail_soft_reader_skips_only_the_malformed_row(tmp_path: Path) -> None:
    path = tmp_path / "bindings.json"
    path.write_text(
        json.dumps(
            {
                "channels": {
                    "1": {"persona": "sales"},
                    "2": "malformed",
                }
            }
        ),
        encoding="utf-8",
    )

    assert set(load_discord_channel_bindings(path)) == {"1"}


def test_binding_reconcile_preserves_unknown_and_guild_fields() -> None:
    document = {
        "guild_id": "guild-1",
        "operator_note": "keep",
        "channels": {
            "2": {
                "kind": "persona",
                "persona": "sales",
                "guild_id": "guild-override",
                "custom": {"keep": True},
            }
        },
    }
    updated = reconcile_persona_bindings(
        document,
        persona_id="sales",
        channels=[
            type(
                "ChannelIntent",
                (),
                {"kind": "discord", "channel_id": "2", "name": "sales-room"},
            )()
        ],
    )

    assert updated["guild_id"] == "guild-1"
    assert updated["operator_note"] == "keep"
    assert updated["channels"]["2"]["guild_id"] == "guild-override"
    assert updated["channels"]["2"]["custom"] == {"keep": True}
    assert updated["channels"]["2"]["name"] == "sales-room"
    assert "enabled" not in updated["channels"]["2"]


def test_binding_reconcile_preserves_legacy_ownership_and_removes_legacy_rows() -> None:
    owned = {"channels": {"2": {"persona": "other"}}}
    intent = type(
        "ChannelIntent",
        (),
        {"kind": "discord", "channel_id": "2", "name": "sales-room"},
    )()
    with pytest.raises(DiscordBindingError, match="already bound"):
        reconcile_persona_bindings(
            owned,
            persona_id="sales",
            channels=[intent],
        )

    legacy = {"channels": {"2": {"persona": "sales"}}}
    removed = reconcile_persona_bindings(
        legacy,
        persona_id="sales",
        channels=[],
    )
    assert removed["channels"] == {}


@pytest.mark.asyncio
async def test_bound_channel_turn_uses_target_profile_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    homie_root = tmp_path / ".homie"
    monkeypatch.setenv("HOMIE_HOME", str(homie_root))
    matrix_path = tmp_path / "persona-capability-matrix.yaml"
    matrix_path.write_text(
        "\n".join(
            [
                "env_groups:",
                "  runtime_core: [OPENAI_API_KEY]",
                "skill_groups:",
                "  sales_lane: [sales-skill]",
                "profiles:",
                "  sales:",
                "    env_groups: [runtime_core]",
                "    skill_groups: [sales_lane]",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOMIE_PERSONA_CAPABILITY_MATRIX", str(matrix_path))
    profile_root = _write_profile(homie_root, "sales")
    skills_root = tmp_path / ".claude" / "skills"
    for skill_name in ("sales-skill", "marketing-skill"):
        skill_dir = skills_root / skill_name
        skill_dir.mkdir(parents=True)
        skill_dir.joinpath("SKILL.md").write_text(
            (
                "---\n"
                f"name: {skill_name}\n"
                f"description: {skill_name} description\n"
                "---\n"
            ),
            encoding="utf-8",
        )
    db_path = tmp_path / "chat.db"
    store = get_session_store(db_path)
    captured = []
    observed_progress: list[str] = []
    progress: dict[str, object] = {}

    async def fake_run(req):
        captured.append(req)
        observed_progress.append(str(progress.get("status") or ""))
        return RuntimeResult(
            text="sales answer",
            runtime_lane="claude_native",
            provider="claude",
            model="haiku",
            profile_key="test-profile",
            session_id="runtime-1",
        )

    binding = load_discord_channel_bindings(
        path=tmp_path / "missing.json"
    ).get("nope")
    assert binding is None
    incoming = _incoming("2")
    incoming.prefetched_context = "# Crypto Desk Live Snapshot\nOpen plays: 1"
    with patch("runtime.lane_router.run_with_runtime_lanes", side_effect=fake_run):
        outgoing = await run_discord_persona_channel_turn(
            incoming=incoming,
            binding=DiscordChannelBinding(
                channel_id="2",
                name="sales",
                kind="persona",
                persona_id="sales",
                guild_id="guild-1",
            ),
            session_store=store,
            project_root=tmp_path,
            progress=progress,
        )

    assert outgoing.text == "sales answer"
    request = captured[0]
    assert request.env["HOMIE_HOME"] == str(profile_root)
    assert request.metadata["persona_id"] == "sales"
    assert len(captured) == 1
    assert "The data below was already gathered via direct API calls." in request.prompt
    assert "Do NOT run any commands, tools, or scripts" in request.prompt
    assert "# Crypto Desk Live Snapshot\nOpen plays: 1" in request.prompt
    assert "SALES_SOUL_MARKER" in request.system_prompt
    assert "SALES_MEMORY_MARKER" in request.system_prompt
    assert "SALES_VOICE_PROMPT" in request.system_prompt
    assert "sales-skill" in request.system_prompt
    assert "marketing-skill" not in request.system_prompt
    assert "dedicated Discord channel `#sales`" in request.system_prompt
    assert request.allowed_tools == []
    assert request.disallowed_tools == ["*"]
    assert observed_progress == ["Sales Homie is reasoning"]
    assert progress["status"] == "Sales Homie is reasoning"
    assert progress["tool_calls"] == 0
    assert "current_tool" not in progress
    session = store.get("discord", "2", "2")
    assert session is not None
    assert session.runtime_profile_key == "test-profile"
    assert [m.role for m in store.list_messages(session.session_id)] == [
        "user",
        "assistant",
    ]


@pytest.mark.asyncio
async def test_tool_transport_failure_retries_once_as_declared_text_only_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    homie_root = tmp_path / ".homie"
    monkeypatch.setenv("HOMIE_HOME", str(homie_root))
    _write_profile(homie_root, "sales")
    store = get_session_store(tmp_path / "chat.db")
    captured = []
    dispatched = []
    definition = {
        "type": "function",
        "function": {
            "name": "safe_lookup",
            "description": "Read a harmless scoped value.",
            "parameters": {"type": "object", "properties": {}},
        },
    }

    async def fake_run(request):
        captured.append(request)
        if len(captured) == 1:
            raise RuntimeCallerToolTransportError("no safe caller-tool lane")
        return RuntimeResult(
            text="I can still talk, but I did not run anything.",
            runtime_lane="generic_runtime",
            provider="openai-codex",
            model="gpt-5.6-sol",
            profile_key="primary-openai-codex",
        )

    with (
        patch(
            "runtime.persona_tools.build_persona_tool_payload",
            return_value=(
                [definition],
                lambda name, arguments: dispatched.append((name, arguments)),
            ),
        ),
        patch("runtime.lane_router.run_with_runtime_lanes", side_effect=fake_run),
    ):
        outgoing = await run_discord_persona_channel_turn(
            incoming=_incoming("2"),
            binding=DiscordChannelBinding(
                channel_id="2",
                name="sales",
                kind="persona",
                persona_id="sales",
                guild_id="guild-1",
            ),
            session_store=store,
            project_root=tmp_path,
        )

    assert len(captured) == 2
    assert captured[0].tool_defs == [definition]
    assert captured[1].tool_defs is None
    assert captured[1].tool_dispatch is None
    assert captured[1].tool_scope_version is None
    assert captured[1].metadata["caller_tools_degraded"] is True
    assert "Do not claim" in captured[1].prompt
    assert dispatched == []
    assert "no tool action was performed" in outgoing.text
