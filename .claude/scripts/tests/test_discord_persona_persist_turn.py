"""US-003: Discord persona _persist_turn writes persona_id (set-once)."""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

CHAT_DIR = Path(__file__).resolve().parents[2] / "chat"
if str(CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(CHAT_DIR))

from discord_channel_bindings import DiscordChannelBinding  # noqa: E402
from discord_persona_runtime import (  # noqa: E402
    _persist_turn,
    run_discord_persona_channel_turn,
)
from models import Channel, IncomingMessage, Platform, Thread, User  # noqa: E402
from runtime.base import RuntimeResult  # noqa: E402
from session import get_session_store  # noqa: E402


def _incoming(channel_id: str = "chan-1") -> IncomingMessage:
    return IncomingMessage(
        text="test message",
        user=User(Platform.DISCORD, "user-1", "Tester"),
        channel=Channel(Platform.DISCORD, channel_id, is_dm=False),
        platform=Platform.DISCORD,
        thread=Thread(channel_id),
    )


def _fake_result(**overrides):
    defaults = dict(
        text="reply",
        runtime_lane="claude_native",
        provider="claude",
        model="haiku",
        profile_key="test",
        session_id="sid-1",
    )
    defaults.update(overrides)
    return RuntimeResult(**defaults)


def _write_profile(homie_root: Path, persona_id: str) -> Path:
    profile_root = homie_root / "profiles" / persona_id
    memory_dir = profile_root / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (profile_root / "run").mkdir(parents=True, exist_ok=True)
    (profile_root / "skills").mkdir(parents=True, exist_ok=True)
    (profile_root / "config.yaml").write_text(
        f"persona:\n  display_name: {persona_id.title()}\n  role: test\n",
        encoding="utf-8",
    )
    (memory_dir / "SOUL.md").write_text("# Soul\ntest", encoding="utf-8")
    (memory_dir / "MEMORY.md").write_text("# Memory\ntest", encoding="utf-8")
    return profile_root


# ── Create branch: persona_id is written ────────────────────────────


def test_persist_turn_create_sets_persona_id(tmp_path: Path) -> None:
    store = get_session_store(tmp_path / "chat.db")
    _persist_turn(
        session_store=store,
        incoming=_incoming(),
        response_text="reply",
        result=_fake_result(),
        session_key="discord:chan-1:chan-1",
        platform_str="discord",
        channel_id="chan-1",
        thread_id="chan-1",
        persona_id="sales",
    )
    session = store.get("discord", "chan-1", "chan-1")
    assert session is not None
    assert session.persona_id == "sales"


def test_persist_turn_create_without_persona_id_leaves_null(tmp_path: Path) -> None:
    store = get_session_store(tmp_path / "chat.db")
    _persist_turn(
        session_store=store,
        incoming=_incoming(),
        response_text="reply",
        result=_fake_result(),
        session_key="discord:chan-1:chan-1",
        platform_str="discord",
        channel_id="chan-1",
        thread_id="chan-1",
    )
    session = store.get("discord", "chan-1", "chan-1")
    assert session is not None
    assert session.persona_id is None


# ── Update branch: persona_id is NOT overwritten (set-once) ─────────


def test_persist_turn_update_does_not_overwrite_persona_id(tmp_path: Path) -> None:
    store = get_session_store(tmp_path / "chat.db")
    _persist_turn(
        session_store=store,
        incoming=_incoming(),
        response_text="first",
        result=_fake_result(),
        session_key="discord:chan-1:chan-1",
        platform_str="discord",
        channel_id="chan-1",
        thread_id="chan-1",
        persona_id="sales",
    )
    session_before = store.get("discord", "chan-1", "chan-1")
    assert session_before.persona_id == "sales"

    _persist_turn(
        session_store=store,
        incoming=_incoming(),
        response_text="second",
        result=_fake_result(model="sonnet"),
        session_key="discord:chan-1:chan-1",
        platform_str="discord",
        channel_id="chan-1",
        thread_id="chan-1",
        persona_id="marketing",
    )
    session_after = store.get("discord", "chan-1", "chan-1")
    assert session_after.persona_id == "sales", "persona_id must not be overwritten on update"
    assert session_after.message_count == 2
    assert session_after.runtime_model == "sonnet"


# ── Integration: full turn writes persona_id ────────────────────────


@pytest.mark.asyncio
async def test_full_turn_persists_persona_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    homie_root = tmp_path / ".homie"
    monkeypatch.setenv("HOMIE_HOME", str(homie_root))
    matrix_path = tmp_path / "matrix.yaml"
    matrix_path.write_text(
        "env_groups: {}\nskill_groups: {}\nprofiles:\n  sales:\n    env_groups: []\n    skill_groups: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOMIE_PERSONA_CAPABILITY_MATRIX", str(matrix_path))
    _write_profile(homie_root, "sales")
    db_path = tmp_path / "chat.db"
    store = get_session_store(db_path)

    async def fake_run(req):
        return _fake_result(text="sales answer")

    with patch("runtime.lane_router.run_with_runtime_lanes", side_effect=fake_run):
        await run_discord_persona_channel_turn(
            incoming=_incoming("chan-sales"),
            binding=DiscordChannelBinding(
                channel_id="chan-sales",
                name="sales",
                kind="persona",
                persona_id="sales",
                guild_id="guild-1",
            ),
            session_store=store,
            project_root=tmp_path,
        )

    session = store.get("discord", "chan-sales", "chan-sales")
    assert session is not None
    assert session.persona_id == "sales"


# ── Grep gates ──────────────────────────────────────────────────────


def test_no_cognitive_pass_or_inference_tracker_import() -> None:
    src = (CHAT_DIR / "discord_persona_runtime.py").read_text(encoding="utf-8")
    assert "cognitive_pass" not in src
    assert "InferenceTracker" not in src


def test_persona_turn_stays_no_tools_max_turns_1() -> None:
    src = (CHAT_DIR / "discord_persona_runtime.py").read_text(encoding="utf-8")
    assert "max_turns=1" in src
    assert 'allowed_tools=[]' in src
    assert 'disallowed_tools=["*"]' in src
