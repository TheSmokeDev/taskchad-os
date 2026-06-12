from __future__ import annotations

import sqlite3
from pathlib import Path

import engine as engine_module
import pytest
import voice as voice_module
from engine import ConversationEngine
from models import Attachment, Channel, IncomingMessage, Platform, Thread, User
from session import Session, SQLiteSessionStore

from runtime.base import (
    RUNTIME_LANE_CLAUDE_NATIVE,
    RUNTIME_LANE_GENERIC,
    RuntimeResult,
    RuntimeToolCall,
)


def _make_message(text: str = "Need a summary") -> IncomingMessage:
    return IncomingMessage(
        text=text,
        user=User(platform=Platform.TELEGRAM, platform_id="user-1", display_name="YourUser"),
        channel=Channel(platform=Platform.TELEGRAM, platform_id="chat-1", is_dm=True),
        platform=Platform.TELEGRAM,
        thread=Thread(thread_id="thread-1"),
    )


def _make_discord_message(
    text: str = "what should I do next?",
    *,
    platform_id: str = "111",
    display_name: str | None = "Alice",
) -> IncomingMessage:
    return IncomingMessage(
        text=text,
        user=User(
            platform=Platform.DISCORD,
            platform_id=platform_id,
            display_name=display_name,
        ),
        channel=Channel(platform=Platform.DISCORD, platform_id="discord-chan", is_dm=False),
        platform=Platform.DISCORD,
        thread=Thread(thread_id="discord-thread"),
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
async def test_engine_preserves_codex_sentinel_runtime_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    convo = ConversationEngine(store, project_root)

    async def fake_run(_request):
        return RuntimeResult(
            text="OK",
            runtime_lane="generic_runtime",
            provider="openai-codex",
            model="chatgpt-plan-default",
            profile_key="primary-openai-codex",
        )

    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)

    outputs = [out async for out in convo.handle_message(_make_message("Reply with exactly OK"))]
    assert outputs[-1].text == "OK"

    persisted = store.get("telegram", "chat-1", "thread-1")
    assert persisted is not None
    assert persisted.runtime_lane == "generic_runtime"
    assert persisted.runtime_provider == "openai-codex"
    assert persisted.runtime_model == "chatgpt-plan-default"
    assert persisted.runtime_profile_key == "primary-openai-codex"


@pytest.mark.asyncio
async def test_engine_persists_operator_display_text_for_rewritten_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    convo = ConversationEngine(store, project_root)

    async def fake_run(_request):
        return RuntimeResult(
            text="Drafted.",
            runtime_lane="generic_runtime",
            provider="openai-codex",
            model="gpt-5.5",
            profile_key="primary-openai-codex",
        )

    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)

    message = _make_message("LinkedIn/Social Homie internal writing rules: draft the post")
    message.raw_event["display_text"] = "/linkedin draft the post"

    outputs = [out async for out in convo.handle_message(message)]
    assert outputs[-1].text == "Drafted."

    messages = store.list_messages("telegram:chat-1:thread-1")
    assert messages[0].content == "/linkedin draft the post"
    assert "internal writing rules" not in messages[0].content


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
async def test_engine_clears_stale_claude_session_after_generic_turn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    convo = ConversationEngine(store, project_root)
    session = Session(
        session_id="telegram:chat-1:thread-1",
        agent_session_id="claude-session-existing",
        platform="telegram",
        channel_id="chat-1",
        thread_id="thread-1",
        user_id="user-1",
        created_at=engine_module.datetime.now(),
        updated_at=engine_module.datetime.now(),
        runtime_lane=RUNTIME_LANE_CLAUDE_NATIVE,
        runtime_provider="claude",
        runtime_profile_key="primary-claude",
    )
    store.create(session)

    async def fake_run(_request):
        return RuntimeResult(
            text="OK",
            runtime_lane=RUNTIME_LANE_GENERIC,
            provider="openai-codex",
            model="gpt-5.5",
            profile_key="primary-openai-codex",
            session_id=None,
        )

    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)

    outputs = [out async for out in convo.handle_message(_make_message("Reply with exactly OK"))]

    assert outputs[-1].text == "OK"
    persisted = store.get("telegram", "chat-1", "thread-1")
    assert persisted is not None
    assert persisted.runtime_session_id == ""
    assert persisted.runtime_lane == RUNTIME_LANE_GENERIC
    assert persisted.runtime_provider == "openai-codex"


@pytest.mark.asyncio
async def test_engine_does_not_reuse_stale_claude_session_after_generic_turn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    convo = ConversationEngine(store, project_root)
    store.create(
        Session(
            session_id="telegram:chat-1:thread-1",
            agent_session_id="claude-session-existing",
            platform="telegram",
            channel_id="chat-1",
            thread_id="thread-1",
            user_id="user-1",
            created_at=engine_module.datetime.now(),
            updated_at=engine_module.datetime.now(),
            runtime_lane=RUNTIME_LANE_CLAUDE_NATIVE,
            runtime_provider="claude",
            runtime_profile_key="primary-claude",
        )
    )
    captured_resumes: list[str | None] = []

    async def fake_run(request):
        captured_resumes.append(request.resume)
        return RuntimeResult(
            text="OK",
            runtime_lane=RUNTIME_LANE_GENERIC,
            provider="openai-codex",
            model="gpt-5.5",
            profile_key="primary-openai-codex",
            session_id=None,
        )

    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)

    first_outputs = [
        out async for out in convo.handle_message(_make_message("Reply with exactly OK"))
    ]
    second_outputs = [
        out async for out in convo.handle_message(_make_message("Reply with exactly OK again"))
    ]

    assert first_outputs[-1].text == "OK"
    assert second_outputs[-1].text == "OK"
    assert captured_resumes == ["claude-session-existing", None]


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


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["go ahead", "do it", "execute", "implement it", "get started"])
async def test_short_execution_phrases_keep_tools_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    text: str,
) -> None:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    convo = ConversationEngine(store, project_root)
    captured: dict[str, object] = {}

    async def fake_run(request):
        captured["capability"] = request.capability
        captured["allowed_tools"] = list(request.allowed_tools)
        return RuntimeResult(
            text="working",
            runtime_lane="generic_runtime",
            provider="gemini-cli",
            model="gemini-3-flash-preview",
            profile_key="primary-gemini-cli",
        )

    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)

    outputs = [out async for out in convo.handle_message(_make_message(text))]

    assert outputs[-1].text == "working"
    assert captured["capability"] == "tool_reasoning"
    assert "Bash" in captured["allowed_tools"]


@pytest.mark.asyncio
async def test_discord_speaker_context_varies_per_active_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    convo = ConversationEngine(store, project_root)
    captured_prompts: list[str] = []

    async def fake_recall(**kwargs):
        return _FakeRecallResponse(tier="tier_1", formatted_text="")

    async def fake_run(request):
        captured_prompts.append(request.system_prompt["append"])
        return RuntimeResult(
            text="ok",
            runtime_lane=RUNTIME_LANE_GENERIC,
            provider="openai-codex",
            model="gpt-5.5",
            profile_key="primary-openai-codex",
            session_id=None,
        )

    monkeypatch.setattr(engine_module, "recall_memory_service", fake_recall)
    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)

    first_outputs = [
        out
        async for out in convo.handle_message(
            _make_discord_message(platform_id="111", display_name="Alice")
        )
    ]
    second_outputs = [
        out
        async for out in convo.handle_message(
            _make_discord_message(platform_id="222", display_name="Bob")
        )
    ]

    assert first_outputs[-1].text == "ok"
    assert second_outputs[-1].text == "ok"
    assert len(captured_prompts) == 2
    assert "# Current Speaker" in captured_prompts[0]
    assert "display_name: Alice" in captured_prompts[0]
    assert "platform_user_id: 111" in captured_prompts[0]
    assert "display_name: Bob" in captured_prompts[1]
    assert "platform_user_id: 222" in captured_prompts[1]
    assert captured_prompts[0] != captured_prompts[1]


@pytest.mark.asyncio
async def test_unknown_discord_speaker_context_warns_without_owner_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    convo = ConversationEngine(store, project_root)
    captured: dict[str, object] = {}

    async def fake_recall(**kwargs):
        return _FakeRecallResponse(tier="tier_1", formatted_text="")

    async def fake_run(request):
        captured["system_prompt"] = request.system_prompt["append"]
        captured["metadata"] = request.metadata
        return RuntimeResult(
            text="ok",
            runtime_lane=RUNTIME_LANE_GENERIC,
            provider="openai-codex",
            model="gpt-5.5",
            profile_key="primary-openai-codex",
            session_id=None,
        )

    monkeypatch.setattr(engine_module, "recall_memory_service", fake_recall)
    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)

    outputs = [
        out
        async for out in convo.handle_message(
            _make_discord_message(platform_id="", display_name=None)
        )
    ]

    assert outputs[-1].text == "ok"
    append_text = str(captured["system_prompt"])
    assert "status: unknown_unverified" in append_text
    assert "warning: Active speaker identity is incomplete" in append_text
    assert "Do not use owner/default identity" in append_text
    assert captured["metadata"] == {
        "speaker_context": {
            "status": "unknown_unverified",
            "platform": "discord",
            "channel_scope": "shared_channel",
            "has_display_name": False,
            "has_platform_user_id": False,
        }
    }


@pytest.mark.asyncio
async def test_document_attachment_context_reaches_runtime_without_local_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    convo = ConversationEngine(store, project_root)
    document_path = tmp_path / "report.txt"
    document_path.write_text("Attachment body for the model.", encoding="utf-8")
    captured: dict[str, object] = {}

    async def fake_recall(**kwargs):
        return _FakeRecallResponse(tier="tier_1", formatted_text="")

    async def fake_run(request):
        captured["prompt"] = request.prompt
        captured["system_prompt"] = request.system_prompt["append"]
        return RuntimeResult(
            text="ok",
            runtime_lane=RUNTIME_LANE_GENERIC,
            provider="openai-codex",
            model="gpt-5.5",
            profile_key="primary-openai-codex",
            session_id=None,
        )

    monkeypatch.setattr(engine_module, "recall_memory_service", fake_recall)
    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)

    message = _make_discord_message("Please summarize the uploaded report")
    message.attachments = [
        Attachment(
            filename="report.txt",
            mimetype="text/plain",
            url=str(document_path),
            size_bytes=document_path.stat().st_size,
        )
    ]

    outputs = [out async for out in convo.handle_message(message)]

    assert outputs[-1].text == "ok"
    # Phase 2 relocation: attachment content rides the turn prompt, NOT the
    # system append (win32 27K cap + region budgets made that path a dead end).
    prompt_text = str(captured["prompt"])
    assert "# Uploaded Document Content" in prompt_text
    assert "Attachment body for the model." in prompt_text
    assert str(document_path) not in prompt_text
    append_text = str(captured["system_prompt"])
    assert "Attachment body for the model." not in append_text
    assert "# Attachment Context" not in append_text
    assert str(document_path) not in append_text


@pytest.mark.asyncio
async def test_large_document_reaches_prompt_fully_and_never_persists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Phase 2 core invariants: an 85K-char document is fully present in the
    captured turn prompt, absent from the system append, and the document
    body NEVER enters chat.db messages (history persists message.text)."""
    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    convo = ConversationEngine(store, project_root)
    document_path = tmp_path / "transcript.txt"
    body = ("lorem ipsum " * 7_082).strip() + "\nTAIL-SENTINEL-END"  # ~85K chars
    document_path.write_text(body, encoding="utf-8")
    captured: dict[str, object] = {}

    async def fake_recall(**kwargs):
        return _FakeRecallResponse(tier="tier_1", formatted_text="")

    async def fake_run(request):
        captured["prompt"] = request.prompt
        captured["system_prompt"] = request.system_prompt["append"]
        return RuntimeResult(
            text="ok",
            runtime_lane=RUNTIME_LANE_GENERIC,
            provider="openai-codex",
            model="gpt-5.5",
            profile_key="primary-openai-codex",
            session_id=None,
        )

    monkeypatch.setattr(engine_module, "recall_memory_service", fake_recall)
    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)

    message = _make_message("Summarize the upload")
    message.attachments = [
        Attachment(
            filename="transcript.txt",
            mimetype="text/plain",
            url=str(document_path),
            size_bytes=document_path.stat().st_size,
        )
    ]

    outputs = [out async for out in convo.handle_message(message)]

    assert outputs[-1].text == "ok"
    prompt_text = str(captured["prompt"])
    assert "TAIL-SENTINEL-END" in prompt_text, "85K document must inline FULLY"
    assert "[TRUNCATED" not in prompt_text
    assert "PARTIAL CONTENT" not in prompt_text
    assert "TAIL-SENTINEL-END" not in str(captured["system_prompt"])

    persisted = store.list_messages("telegram:chat-1:thread-1")
    assert [msg.role for msg in persisted] == ["user", "assistant"]
    assert persisted[0].content == "Summarize the upload"
    for msg in persisted:
        assert "TAIL-SENTINEL-END" not in msg.content, (
            "document body must NEVER enter chat history"
        )
        assert "lorem ipsum lorem ipsum" not in msg.content


@pytest.mark.asyncio
async def test_discord_built_document_attachment_reaches_runtime_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """2f (R1 B1): a Discord-BUILT document attachment — on-disk filename
    `{message_id}_{attachment_id}.txt` DIFFERENT from the display filename,
    mirroring discord.py _download_document_attachments — reaches the captured
    turn prompt and never the system append."""
    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    convo = ConversationEngine(store, project_root)
    docs_dir = tmp_path / "thehomie_discord_documents"
    docs_dir.mkdir()
    document_path = docs_dir / "777_888.txt"  # discord.py:569 naming shape
    document_path.write_text("Discord doc body for the model.", encoding="utf-8")
    captured: dict[str, object] = {}

    async def fake_recall(**kwargs):
        return _FakeRecallResponse(tier="tier_1", formatted_text="")

    async def fake_run(request):
        captured["prompt"] = request.prompt
        captured["system_prompt"] = request.system_prompt["append"]
        return RuntimeResult(
            text="ok",
            runtime_lane=RUNTIME_LANE_GENERIC,
            provider="openai-codex",
            model="gpt-5.5",
            profile_key="primary-openai-codex",
            session_id=None,
        )

    monkeypatch.setattr(engine_module, "recall_memory_service", fake_recall)
    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)

    message = _make_discord_message("Please read the upload")
    message.attachments = [
        Attachment(
            filename="meeting-notes.txt",  # display name != on-disk name
            mimetype="text/plain",
            url=str(document_path),
            size_bytes=document_path.stat().st_size,
        )
    ]

    outputs = [out async for out in convo.handle_message(message)]

    assert outputs[-1].text == "ok"
    prompt_text = str(captured["prompt"])
    assert "Discord doc body for the model." in prompt_text
    assert "meeting-notes.txt" in prompt_text
    assert str(document_path) not in prompt_text
    assert "Discord doc body for the model." not in str(captured["system_prompt"])


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


# =============================================================================
# PRD-8 Phase 2 (WS4) — engine._build_frozen_regions parity with shim
# =============================================================================
# Refactor target: engine._build_frozen_regions consolidates the SOUL / SELF /
# USER / MEMORY / WORKING reads through cognition.identity_payload.build_identity_payload.
# The interleaved user_inferences (between user_model and durable_memory) and
# procedural_memory (after working_memory) regions stay verbatim.
#
# Parity contract (criterion `engine_refactor_parity_preserved`):
#   same region count, same names, same source attributions, per-region
#   byte-equal content, same max_tokens (from config.REGION_BUDGETS,
#   env-overridable — UNCHANGED), same frozen flags.


def _legacy_build_frozen_regions(
    memory_dir: Path,
    project_root: Path,
    budgets: dict[str, int],
) -> list[object]:
    """Pre-refactor reference implementation of `_build_frozen_regions`.

    Mirrors the inline ``read_file_safe()`` reads at engine.py:170/177/184/240/248
    (PRE-WS4) plus the interleaved user_inferences and procedural_memory
    regions. Used by the parity test to prove the post-refactor (shim-backed)
    path produces an identical region list.

    The interleaved blocks (user_inferences via ``InferenceTracker``,
    procedural_memory via ``build_skill_index``) are conditional and depend on
    cognition optional submodules + filesystem state. The parity test seeds
    ``memory_dir`` and ``project_root`` so BOTH the legacy and post-refactor
    paths take the same branches (both add or both skip), keeping parity
    deterministic.
    """
    import json as _json

    from cognition.regions import PromptRegion as _PromptRegion
    from runtime.bootstrap import read_file_safe as _read_file_safe

    regions: list[object] = []

    soul = _read_file_safe(memory_dir / "SOUL.md")
    if soul:
        regions.append(_PromptRegion(
            "identity", soul, budgets["identity"],
            frozen=True, source="SOUL.md",
        ))

    self_model = _read_file_safe(memory_dir / "SELF.md")
    if self_model:
        regions.append(_PromptRegion(
            "self_model", self_model, budgets["self_model"],
            frozen=True, source="SELF.md",
        ))

    user = _read_file_safe(memory_dir / "USER.md")
    if user:
        regions.append(_PromptRegion(
            "user_model", user, budgets["user_model"],
            frozen=True, source="USER.md",
        ))

    # Interleaved user_inferences region (verbatim from engine.py:191-238 PRE-WS4).
    try:
        from cognition.self_model import InferenceTracker as _InferenceTracker
        from config import (
            INFERENCE_PROMPT_CAP as _INFERENCE_PROMPT_CAP,
            INFERENCE_PROMPT_MIN_CONFIDENCE as _INFERENCE_PROMPT_MIN_CONFIDENCE,
            INFERENCE_STATE_FILE as _INFERENCE_STATE_FILE,
        )
    except ImportError:
        pass
    else:
        try:
            tracker = _InferenceTracker(_INFERENCE_STATE_FILE)
            active = tracker.get_active(
                min_confidence=_INFERENCE_PROMPT_MIN_CONFIDENCE,
            )
            if active:
                active.sort(key=lambda r: r.last_updated or "", reverse=True)
                active.sort(key=lambda r: r.confidence, reverse=True)
                active.sort(key=lambda r: 0 if r.status == "confirmed" else 1)

                inference_lines = []
                for inf in active[:_INFERENCE_PROMPT_CAP]:
                    status_tag = (
                        "confirmed" if inf.status == "confirmed"
                        else f"conf={inf.confidence:.2f}"
                    )
                    inference_lines.append(f"- [{status_tag}] {inf.inference}")
                inference_text = (
                    "## Active Beliefs About User\n"
                    + "\n".join(inference_lines)
                )
                regions.append(_PromptRegion(
                    "user_inferences",
                    inference_text,
                    budgets["user_inferences"],
                    frozen=True,
                    source="inference-tracker",
                ))
        except (OSError, _json.JSONDecodeError):
            pass

    memory = _read_file_safe(memory_dir / "MEMORY.md")
    if memory:
        regions.append(_PromptRegion(
            "durable_memory", memory, budgets["durable_memory"],
            frozen=True, source="MEMORY.md",
        ))

    working = _read_file_safe(memory_dir / "WORKING.md")
    if working:
        regions.append(_PromptRegion(
            "working_memory", working, budgets["working_memory"],
            frozen=True, source="WORKING.md",
        ))

    # Interleaved procedural_memory region (verbatim from engine.py:255-267 PRE-WS4).
    try:
        from cognition.skills import build_skill_index as _build_skill_index
        try:
            skill_text = _build_skill_index(
                project_root / ".claude" / "skills",
            )
            if skill_text:
                regions.append(_PromptRegion(
                    "procedural_memory", skill_text, budgets["procedural_memory"],
                    frozen=True, source="skills/",
                ))
        except Exception:
            pass
    except ImportError:
        pass

    return regions


def _seed_identity_files(memory_dir: Path) -> None:
    """Seed the canonical 5 identity files the engine reads.

    Per R2 NM2 the parity test fixtures live under
    ``tmp_path / 'TheHomie' / 'Memory'`` — NEVER touch the real
    ``vault/memory/`` (sanitizer-denied + non-reproducible).
    """
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "SOUL.md").write_text(
        "# SOUL\nFixture: identity, behavioral rules.\n",
        encoding="utf-8",
    )
    (memory_dir / "SELF.md").write_text(
        "# SELF\nFixture: agent self-model patterns.\n",
        encoding="utf-8",
    )
    (memory_dir / "USER.md").write_text(
        "# USER\nFixture: user profile + integrations.\n",
        encoding="utf-8",
    )
    (memory_dir / "MEMORY.md").write_text(
        "# MEMORY\nFixture: durable decisions + lessons.\n",
        encoding="utf-8",
    )
    (memory_dir / "WORKING.md").write_text(
        "# WORKING\nFixture: open threads scratchpad.\n",
        encoding="utf-8",
    )


def test_frozen_regions_parity_with_shim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """WS4 parity gate: post-refactor `_build_frozen_regions` (which reads via
    ``cognition.identity_payload.build_identity_payload``) produces a region
    list that is byte-identical PER REGION to the pre-refactor reference
    implementation captured in ``_legacy_build_frozen_regions`` above.

    Per criterion ``engine_refactor_parity_preserved`` the comparison covers:
    same count, same ordering, same names, same source attributions, same
    content (per-region byte-equal), same max_tokens (from
    ``config.REGION_BUDGETS`` — env-overridable, UNCHANGED), same frozen flags.
    """
    import config as config_module

    # 1. Seed canonical identity fixtures under tmp_path/vault/memory.
    memory_dir = tmp_path / "TheHomie" / "Memory"
    _seed_identity_files(memory_dir)

    # 2. Repoint config.MEMORY_DIR at the fixture so `_build_frozen_regions`'s
    #    `from config import MEMORY_DIR, REGION_BUDGETS` resolves to test data.
    monkeypatch.setattr(config_module, "MEMORY_DIR", memory_dir)

    budgets = config_module.REGION_BUDGETS

    # 3. Use a project_root with no skills/ dir so the procedural_memory
    #    branch returns the same empty result on both paths (parity-preserving).
    project_root = tmp_path / "project"
    (project_root / ".claude" / "skills").mkdir(parents=True)

    # 4. Build legacy reference regions (pre-refactor logic, verbatim).
    legacy_regions = _legacy_build_frozen_regions(
        memory_dir=memory_dir,
        project_root=project_root,
        budgets=budgets,
    )

    # 5. Build post-refactor regions via the actual ConversationEngine
    #    (its __init__ calls the shim-backed `_build_frozen_regions`).
    store = SQLiteSessionStore(tmp_path / "chat.db")
    convo = ConversationEngine(store, project_root)
    new_regions = convo._frozen_regions

    # 6. Per-region parity assertions.
    assert len(new_regions) == len(legacy_regions), (
        f"Region count drift: legacy={len(legacy_regions)} "
        f"new={len(new_regions)}\n"
        f"legacy names={[r.name for r in legacy_regions]}\n"
        f"new names={[r.name for r in new_regions]}"
    )

    for idx, (legacy, new) in enumerate(zip(legacy_regions, new_regions)):
        assert legacy.name == new.name, (
            f"Region {idx} name drift: legacy={legacy.name!r} new={new.name!r}"
        )
        assert legacy.source == new.source, (
            f"Region {idx} ({new.name}) source drift: "
            f"legacy={legacy.source!r} new={new.source!r}"
        )
        assert legacy.content == new.content, (
            f"Region {idx} ({new.name}) content drift\n"
            f"legacy={legacy.content!r}\nnew={new.content!r}"
        )
        assert legacy.max_tokens == new.max_tokens, (
            f"Region {idx} ({new.name}) max_tokens drift: "
            f"legacy={legacy.max_tokens} new={new.max_tokens}"
        )
        assert legacy.frozen == new.frozen, (
            f"Region {idx} ({new.name}) frozen drift: "
            f"legacy={legacy.frozen} new={new.frozen}"
        )

    # 7. Sanity: verify the canonical 5 identity regions are present in the
    #    expected order. user_inferences and procedural_memory are conditional
    #    and not asserted here (the per-region equality above already covers
    #    them — both paths take the same branches against the same fixtures).
    names = [r.name for r in new_regions]
    expected_identity_order = [
        "identity",
        "self_model",
        "user_model",
        "durable_memory",
        "working_memory",
    ]
    identity_present = [n for n in names if n in expected_identity_order]
    assert identity_present == expected_identity_order, (
        f"Identity region ordering drift: got {identity_present}, "
        f"expected {expected_identity_order}"
    )


@pytest.mark.asyncio
async def test_grounding_rule_reaches_runtime_system_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """GROUNDING_RULES must be the literal PREFIX of the runtime system-prompt
    append — prefix position, not mere containment, so the win32 head-keeping
    truncation can never drop it."""
    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    convo = ConversationEngine(store, project_root)
    captured: dict[str, object] = {}

    async def fake_recall(**kwargs):
        return _FakeRecallResponse(tier="tier_1", formatted_text="")

    async def fake_run(request):
        captured["system_prompt"] = request.system_prompt["append"]
        return RuntimeResult(
            text="ok",
            runtime_lane=RUNTIME_LANE_GENERIC,
            provider="openai-codex",
            model="gpt-5.5",
            profile_key="primary-openai-codex",
            session_id=None,
        )

    monkeypatch.setattr(engine_module, "recall_memory_service", fake_recall)
    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)

    outputs = [out async for out in convo.handle_message(_make_message())]

    assert outputs[-1].text == "ok"
    append_text = str(captured["system_prompt"])
    assert append_text.startswith("# Grounding")
    assert append_text.startswith(engine_module.GROUNDING_RULES)


def test_grounding_survives_win32_truncation() -> None:
    """R1 B3 regression: a head-keeping truncation of an oversized append must
    preserve the full GROUNDING_RULES prefix while dropping the tail."""
    oversized = engine_module.GROUNDING_RULES + "x" * 40000

    result = engine_module._truncate_win32_append(oversized)

    assert result.startswith(engine_module.GROUNDING_RULES)
    assert result.endswith("[TRUNCATED]")
    assert len(result) == 27000 + len("\n[TRUNCATED]")
