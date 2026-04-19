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


class _FakeRecallLog:
    def __init__(self, tier: str = "tier_1") -> None:
        self.tier = tier


class _FakeRecallResponse:
    def __init__(self, tier: str = "tier_1", formatted_text: str = "") -> None:
        self.results: list = []
        self.formatted_text = formatted_text
        self.log = _FakeRecallLog(tier=tier)


def _seed_resumed_session(
    store: SQLiteSessionStore,
    runtime_session_id: str = "runtime-session-abc",
) -> Session:
    session = Session(
        session_id="telegram:chat-1:thread-1",
        agent_session_id=runtime_session_id,
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
    return session


def _install_fake_runtime(
    monkeypatch: pytest.MonkeyPatch,
    captured: dict,
    text: str = "resumed response",
) -> None:
    async def fake_run(request):
        captured["system_prompt"] = request.system_prompt
        captured["capability"] = request.capability
        captured["resume"] = request.resume
        captured["allowed_tools"] = list(request.allowed_tools)
        return RuntimeResult(
            text=text,
            runtime_lane=RUNTIME_LANE_CLAUDE_NATIVE,
            provider="claude",
            model="claude-sonnet-4-6",
            profile_key="primary-claude",
            session_id=request.resume or "runtime-session-abc",
        )

    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)


@pytest.mark.asyncio
async def test_resumed_session_runs_full_cognition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Path B core invariant: recall_service is called on every resumed turn."""
    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    convo = ConversationEngine(store, project_root)
    _seed_resumed_session(store)

    recall_calls: list[dict] = []

    async def fake_recall(**kwargs):
        recall_calls.append(kwargs)
        return _FakeRecallResponse(tier="tier_1", formatted_text="## Memory\n\nfake recall snippet")

    monkeypatch.setattr(engine_module, "recall_memory_service", fake_recall)

    captured: dict = {}
    _install_fake_runtime(monkeypatch, captured)

    outputs = [
        out
        async for out in convo.handle_message(_make_message("what about AI consciousness?"))
    ]

    assert outputs[-1].text == "resumed response"
    assert captured["resume"] == "runtime-session-abc"
    assert len(recall_calls) == 1, "Recall must run on resumed turns (Path B)"
    assert recall_calls[0]["query"] == "what about AI consciousness?"
    assert recall_calls[0]["caller"] == "chat"


@pytest.mark.asyncio
async def test_resumed_session_injects_continuity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Continuity state must appear in the assembled prompt on resumed turns."""
    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    convo = ConversationEngine(store, project_root)
    _seed_resumed_session(store)

    # Redirect CONTINUITY_DIR to tmp and seed a focus marker
    import config as config_module
    from cognition.continuity import ContinuityState, save_continuity

    continuity_dir = tmp_path / "continuity"
    monkeypatch.setattr(config_module, "CONTINUITY_DIR", continuity_dir)
    save_continuity(
        ContinuityState(
            session_id="telegram:chat-1:thread-1",
            current_focus="AI consciousness deep dive",
        ),
        continuity_dir,
    )

    async def fake_recall(**kwargs):
        return _FakeRecallResponse(tier="tier_1", formatted_text="")

    monkeypatch.setattr(engine_module, "recall_memory_service", fake_recall)

    captured: dict = {}
    _install_fake_runtime(monkeypatch, captured)

    outputs = [out async for out in convo.handle_message(_make_message("yee"))]

    assert outputs[-1].text == "resumed response"
    append_text = captured["system_prompt"]["append"]
    assert "AI consciousness deep dive" in append_text, (
        "Continuity current_focus must appear in the assembled prompt"
    )
    assert "Continuity" in append_text, "Continuity region header must be present"


@pytest.mark.asyncio
async def test_resumed_session_injects_recent_conversation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The last-N prior messages must be injected as a recent_conversation region."""
    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    convo = ConversationEngine(store, project_root)
    _seed_resumed_session(store)

    session_key = "telegram:chat-1:thread-1"
    store.add_message(session_key, "user", "do u dream")
    store.add_message(session_key, "assistant", "Sometimes I dream about vector spaces")
    store.add_message(session_key, "user", "what about AI consciousness?")
    store.add_message(
        session_key,
        "assistant",
        "Consciousness in AI is an unresolved philosophical question",
    )

    async def fake_recall(**kwargs):
        return _FakeRecallResponse(tier="tier_1", formatted_text="")

    monkeypatch.setattr(engine_module, "recall_memory_service", fake_recall)

    captured: dict = {}
    _install_fake_runtime(monkeypatch, captured)

    outputs = [out async for out in convo.handle_message(_make_message("yee"))]

    assert outputs[-1].text == "resumed response"
    append_text = captured["system_prompt"]["append"]
    assert "Recent Conversation" in append_text, "Header for recent_conversation must be present"
    assert "do u dream" in append_text
    assert "Sometimes I dream about vector spaces" in append_text
    assert "what about AI consciousness?" in append_text
    assert "Consciousness in AI is an unresolved philosophical question" in append_text


@pytest.mark.asyncio
async def test_yee_after_substantive_preserves_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Scenario regression: 'yee' after an AI-consciousness exchange must not be a fresh-session greeting.

    The assembled prompt on turn 3 must contain 'consciousness' somewhere — via recall results,
    continuity focus, or recent_conversation transcript. Any of the three is sufficient.
    """
    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    convo = ConversationEngine(store, project_root)
    _seed_resumed_session(store)

    import config as config_module
    from cognition.continuity import ContinuityState, save_continuity

    continuity_dir = tmp_path / "continuity"
    monkeypatch.setattr(config_module, "CONTINUITY_DIR", continuity_dir)
    save_continuity(
        ContinuityState(
            session_id="telegram:chat-1:thread-1",
            current_focus="what about AI consciousness",
        ),
        continuity_dir,
    )

    session_key = "telegram:chat-1:thread-1"
    store.add_message(session_key, "user", "what about AI consciousness?")
    store.add_message(
        session_key,
        "assistant",
        "Consciousness in AI remains philosophically contested",
    )

    async def fake_recall(**kwargs):
        return _FakeRecallResponse(tier="tier_1", formatted_text="")

    monkeypatch.setattr(engine_module, "recall_memory_service", fake_recall)

    captured: dict = {}
    _install_fake_runtime(monkeypatch, captured)

    outputs = [out async for out in convo.handle_message(_make_message("yee"))]

    assert outputs[-1].text == "resumed response"
    append_text = captured["system_prompt"]["append"].lower()
    assert "consciousness" in append_text, (
        "Turn 3 context lost — resumed 'yee' must carry consciousness signal "
        "via continuity, recent_conversation, or recall"
    )


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
