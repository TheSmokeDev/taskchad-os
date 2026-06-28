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
from runtime.base import RuntimeResult  # noqa: E402
from session import get_session_store  # noqa: E402


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


def test_load_bindings_and_watched_channels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binding_file = tmp_path / "bindings.json"
    binding_file.write_text(
        json.dumps(
            {
                "guild_id": "guild-1",
                "channels": {
                    "1": {"name": "default", "kind": "default"},
                    "2": {"name": "sales", "persona": "sales"},
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
    assert resolve_discord_channel_binding(_incoming("2", guild_id="other")) is None


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

    async def fake_run(req):
        captured.append(req)
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
    with patch("runtime.lane_router.run_with_runtime_lanes", side_effect=fake_run):
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

    assert outgoing.text == "sales answer"
    request = captured[0]
    assert request.env["HOMIE_HOME"] == str(profile_root)
    assert request.metadata["persona_id"] == "sales"
    assert "SALES_SOUL_MARKER" in request.system_prompt
    assert "SALES_MEMORY_MARKER" in request.system_prompt
    assert "SALES_VOICE_PROMPT" in request.system_prompt
    assert "sales-skill" in request.system_prompt
    assert "marketing-skill" not in request.system_prompt
    assert "dedicated Discord channel `#sales`" in request.system_prompt
    session = store.get("discord", "2", "2")
    assert session is not None
    assert session.runtime_profile_key == "test-profile"
    assert [m.role for m in store.list_messages(session.session_id)] == [
        "user",
        "assistant",
    ]
