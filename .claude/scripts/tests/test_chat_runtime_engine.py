from __future__ import annotations

import sqlite3
from pathlib import Path

import engine as engine_module
import pytest
import voice as voice_module
from engine import ConversationEngine
from models import Channel, IncomingMessage, Platform, Thread, User
from session import Session, SQLiteSessionStore

from runtime.base import RUNTIME_LANE_CLAUDE_NATIVE, RuntimeResult, RuntimeToolCall


def _make_message(text: str = "Need a summary") -> IncomingMessage:
    return IncomingMessage(
        text=text,
        user=User(platform=Platform.TELEGRAM, platform_id="user-1", display_name="YourUser"),
        channel=Channel(platform=Platform.TELEGRAM, platform_id="chat-1", is_dm=True),
        platform=Platform.TELEGRAM,
        thread=Thread(thread_id="thread-1"),
    )


def _make_project_root(tmp_path: Path) -> Path:
    project_root = tmp_path / "project"
    (project_root / "TheHomie" / "Memory" / "daily").mkdir(parents=True)
    return project_root


@pytest.mark.asyncio
async def test_engine_persists_runtime_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    convo = ConversationEngine(store, project_root)

    async def fake_run(_request):
        return RuntimeResult(
            text="Runtime says hello",
            runtime_lane=RUNTIME_LANE_CLAUDE_NATIVE,
            provider="claude",
            model="claude-sonnet-4-6",
            profile_key="primary-claude",
            session_id="runtime-session-123",
            cost_usd=0.12,
            tool_calls=[
                RuntimeToolCall(
                    id="tc-1",
                    name="Read",
                    arguments={"path": "src/auth.py"},
                    provider_type="tool_use",
                )
            ],
        )

    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)

    outputs = [out async for out in convo.handle_message(_make_message())]
    assert outputs[-1].text == "Runtime says hello"

    persisted = store.get("telegram", "chat-1", "thread-1")
    assert persisted is not None
    assert persisted.runtime_session_id == "runtime-session-123"
    assert persisted.runtime_lane == "claude_native"
    assert persisted.runtime_provider == "claude"
    assert persisted.runtime_model == "claude-sonnet-4-6"
    assert persisted.runtime_profile_key == "primary-claude"
    assert persisted.runtime_tool_calls == [
        {
            "id": "tc-1",
            "name": "Read",
            "arguments": {"path": "src/auth.py"},
            "provider_type": "tool_use",
            "status": None,
        }
    ]
    messages = store.list_messages("telegram:chat-1:thread-1")
    assert messages[1].tool_calls == [
        {
            "id": "tc-1",
            "name": "Read",
            "arguments": {"path": "src/auth.py"},
            "provider_type": "tool_use",
            "status": None,
        }
    ]


@pytest.mark.asyncio
async def test_engine_uses_runtime_session_for_resume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    convo = ConversationEngine(store, project_root)
    now = convo.session_store.get("telegram", "chat-1", "thread-1")
    assert now is None

    session = Session(
        session_id="telegram:chat-1:thread-1",
        agent_session_id="runtime-session-existing",
        platform="telegram",
        channel_id="chat-1",
        thread_id="thread-1",
        user_id="user-1",
        created_at=engine_module.datetime.now(),
        updated_at=engine_module.datetime.now(),
        runtime_provider="claude",
        runtime_profile_key="primary-claude",
    )
    store.create(session)

    captured: dict[str, str | None] = {}

    async def fake_run(request):
        captured["resume"] = request.resume
        return RuntimeResult(
            text="Resumed successfully",
            runtime_lane=RUNTIME_LANE_CLAUDE_NATIVE,
            provider="claude",
            model="claude-sonnet-4-6",
            profile_key="primary-claude",
            session_id="runtime-session-existing",
        )

    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)

    outputs = [out async for out in convo.handle_message(_make_message("Continue"))]
    assert outputs[-1].text == "Resumed successfully"
    assert captured["resume"] == "runtime-session-existing"


@pytest.mark.asyncio
async def test_short_casual_telegram_message_uses_text_reasoning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    convo = ConversationEngine(store, project_root)
    captured: dict[str, object] = {}

    async def fake_run(request):
        captured["capability"] = request.capability
        captured["allowed_tools"] = list(request.allowed_tools)
        return RuntimeResult(
            text="yo",
            runtime_lane="generic_runtime",
            provider="gemini-cli",
            model="gemini-3-flash-preview",
            profile_key="primary-gemini-cli",
        )

    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)

    outputs = [out async for out in convo.handle_message(_make_message("yo"))]

    assert outputs[-1].text == "yo"
    assert captured["capability"] == "text_reasoning"
    assert captured["allowed_tools"] == []


def test_sqlite_session_store_adds_runtime_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "chat.db"
    store = SQLiteSessionStore(db_path)
    session = Session(
        session_id="telegram:chat-1:thread-1",
        agent_session_id="runtime-session-999",
        platform="telegram",
        channel_id="chat-1",
        thread_id="thread-1",
        user_id="user-1",
        created_at=engine_module.datetime.now(),
        updated_at=engine_module.datetime.now(),
        runtime_provider="openai-compatible",
        runtime_model="gpt-4.1-mini",
        runtime_profile_key="fallback-openai",
        runtime_lane="generic_runtime",
    )
    store.create(session)

    persisted = store.get("telegram", "chat-1", "thread-1")
    assert persisted is not None
    assert persisted.runtime_session_id == "runtime-session-999"
    assert persisted.runtime_lane == "generic_runtime"
    assert persisted.runtime_provider == "openai-compatible"
    assert persisted.runtime_model == "gpt-4.1-mini"
    assert persisted.runtime_profile_key == "fallback-openai"

    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(chat_sessions)").fetchall()
        }
    assert {
        "runtime_session_id",
        "runtime_provider",
        "runtime_model",
        "runtime_profile_key",
        "runtime_lane",
        "runtime_tool_calls_json",
    } <= columns


def test_build_voice_provider_set_keeps_stt_tts_separate() -> None:
    providers = voice_module.build_voice_provider_set(
        openai_api_key="sk-test",
        stt_model="whisper-1",
        tts_engine="openai",
        tts_voice_edge="en-US-GuyNeural",
        tts_voice_openai="alloy",
    )
    assert type(providers.stt).__name__ == "OpenAIWhisperProvider"
    assert type(providers.tts).__name__ == "OpenAITtsProvider"

    edge_only = voice_module.build_voice_provider_set(
        openai_api_key="",
        stt_model="whisper-1",
        tts_engine="edge",
        tts_voice_edge="en-US-GuyNeural",
        tts_voice_openai="alloy",
    )
    assert edge_only.stt is None
    assert type(edge_only.tts).__name__ == "EdgeTtsProvider"
