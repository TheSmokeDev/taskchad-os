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


@pytest.mark.parametrize(
    "text",
    [
        "pull the lastest update on " + "task" + "chad os",
        "update it",
        "pull the repo abd ypdate yourslef",
    ],
)
def test_discord_update_incident_messages_keep_execution_tools(text: str) -> None:
    assert engine_module._should_use_text_only_fast_path(_make_discord_message(text)) is False


def test_short_discord_smalltalk_still_uses_text_only_fast_path() -> None:
    message = _make_discord_message("how are you?")
    assert engine_module._should_use_text_only_fast_path(message) is True


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
async def test_short_casual_telegram_message_keeps_tools_and_bypass_permissions(
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
        captured["permission_mode"] = request.permission_mode
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
    assert captured["capability"] == "tool_reasoning"
    assert "Bash" in captured["allowed_tools"]
    assert captured["permission_mode"] == "bypassPermissions"


@pytest.mark.asyncio
async def test_short_casual_discord_message_can_use_text_reasoning(
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

    outputs = [out async for out in convo.handle_message(_make_discord_message("yo"))]

    assert outputs[-1].text == "yo"
    assert captured["capability"] == "text_reasoning"
    assert captured["allowed_tools"] == []


@pytest.mark.asyncio
async def test_telegram_prefetched_context_keeps_tools_enabled(
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
        captured["max_turns"] = request.max_turns
        return RuntimeResult(
            text="working",
            runtime_lane="generic_runtime",
            provider="gemini-cli",
            model="gemini-3-flash-preview",
            profile_key="primary-gemini-cli",
        )

    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)

    message = _make_message("open up your browser and go to LinkedIn")
    message.prefetched_context = "## /browserops\nBrowserOps context loaded"
    outputs = [out async for out in convo.handle_message(message)]

    assert outputs[-1].text == "working"
    assert captured["capability"] == "tool_reasoning"
    assert "Bash" in captured["allowed_tools"]
    assert captured["max_turns"] > 1


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
    monkeypatch.setattr(
        convo, "_maybe_session_brief", lambda *args, **kwargs: ("", None)
    )
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
        captured["prompt"] = request.prompt
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
async def test_long_discord_session_injects_true_latest_recent_conversation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression: sessions over 200 rows must inject newest turns, not oldest rows."""
    import config as config_module

    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    convo = ConversationEngine(store, project_root)
    session = Session(
        session_id="discord:discord-chan:discord-thread",
        agent_session_id="",
        platform="discord",
        channel_id="discord-chan",
        thread_id="discord-thread",
        user_id="111",
        created_at=engine_module.datetime.now(),
        updated_at=engine_module.datetime.now(),
        message_count=220,
        runtime_lane=RUNTIME_LANE_GENERIC,
        runtime_provider="openai-codex",
    )
    store.create(session)

    target = "individual clickable YourProduct prospect demo URLs"
    for i in range(220):
        body = f"historical discord turn {i:03d}"
        if i == 219:
            body = f"We need {target} under YourProduct.com today."
        store.add_message("discord:discord-chan:discord-thread", "user", body)

    async def fake_recall(**kwargs):
        return _FakeRecallResponse(tier="tier_1", formatted_text="")

    async def fake_run(request):
        captured["prompt"] = request.prompt
        captured["system_prompt"] = request.system_prompt
        captured["resume"] = request.resume
        return RuntimeResult(
            text="still on it",
            runtime_lane=RUNTIME_LANE_GENERIC,
            provider="openai-codex",
            model="gpt-5.5",
            profile_key="primary-openai-codex",
            session_id=None,
        )

    captured: dict = {}
    monkeypatch.setattr(config_module, "RECENT_CONVERSATION_COUNT", 80)
    monkeypatch.setattr(config_module, "SESSION_TURN_THRESHOLD", 0)
    monkeypatch.setattr(engine_module, "recall_memory_service", fake_recall)
    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)

    outputs = [
        out
        async for out in convo.handle_message(
            _make_discord_message("How we looking still cooking?")
        )
    ]

    assert outputs[-1].text == "still on it"
    assert captured["resume"] is None
    prompt_text = str(captured["prompt"])
    assert "# Recent Conversation Context" in prompt_text
    assert target in prompt_text
    assert "historical discord turn 001" not in prompt_text


@pytest.mark.asyncio
@pytest.mark.parametrize("message_count", [30, 35, 200])
async def test_zero_session_turn_threshold_never_resets_by_turn_count(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    message_count: int,
) -> None:
    """SESSION_TURN_THRESHOLD=0 keeps long local chat sessions resumable."""
    import config as config_module

    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    convo = ConversationEngine(store, project_root)
    session = _seed_resumed_session(store, runtime_session_id="runtime-session-existing")
    session.message_count = message_count
    store.update(session)

    async def fake_recall(**kwargs):
        return _FakeRecallResponse(tier="tier_1", formatted_text="")

    captured: dict = {}
    _install_fake_runtime(monkeypatch, captured)
    monkeypatch.setattr(config_module, "SESSION_TURN_THRESHOLD", 0)
    monkeypatch.setattr(engine_module, "recall_memory_service", fake_recall)

    outputs = [out async for out in convo.handle_message(_make_message("Continue"))]

    assert outputs[-1].text == "resumed response"
    assert captured["resume"] == "runtime-session-existing"


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
            # Living Self Act 1 (B1): the live renderer now source-filters to
            # trustworthy operator-belief sources {reflection, explicit}. The
            # legacy reference must mirror that filter to stay a valid parity
            # baseline (this test proves the identity-payload shim refactor is
            # behavior-preserving, not the inference-source contract).
            active = [r for r in active if r.source in ("reflection", "explicit")]
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


# =============================================================================
# Living Mind Act 4 — Session Opening Brief engine wiring
# (categories 8-10 of the Act 4 validation plan: injection proof, gate skips,
# trace decisions, double-fire guard, marker consumption, fail-open.)
# =============================================================================

import asyncio  # noqa: E402
from datetime import datetime as _dt_cls  # noqa: E402
from datetime import timedelta as _td  # noqa: E402

import cognition.proactive_brief as _pb  # noqa: E402
from cognition.proactive_brief import SessionOpeningBrief  # noqa: E402

from runtime.errors import RuntimeExecutionError  # noqa: E402
from security.kill_switches import KillSwitchDisabled  # noqa: E402

_FIRED_BLOCK = (
    "# Session Opening Brief (deliver first)\n\n"
    "OPEN your reply with a short first-person brief.\n\n"
    "## What changed while away\n- [2026-06-12] [calendar] busy day: 5 events"
)


def _fired_brief() -> SessionOpeningBrief:
    return SessionOpeningBrief(_FIRED_BLOCK, True, 8.5, 1, "")


def _suppressed_brief(reason: str, away: float | None = None) -> SessionOpeningBrief:
    return SessionOpeningBrief("", False, away, 0, reason)


def _patch_brief_seams(
    monkeypatch: pytest.MonkeyPatch,
    *,
    brief: SessionOpeningBrief | None = None,
    builder=None,
    owed: _dt_cls | None = None,
    physical: _dt_cls | None = None,
) -> dict[str, list]:
    """Patch ALL Act 4 module seams so engine tests never touch live
    STATE_DIR / vault files (the PRP test-isolation contract)."""
    calls: dict[str, list] = {"build": [], "clear": []}

    def _default_builder(memory_dir, **kwargs):
        calls["build"].append(kwargs)
        return brief if brief is not None else _suppressed_brief("not_away", 0.1)

    monkeypatch.setattr(
        _pb, "build_session_opening_brief", builder or _default_builder
    )
    monkeypatch.setattr(_pb, "read_brief_owed", lambda **kwargs: owed)
    monkeypatch.setattr(
        _pb, "clear_brief_owed", lambda **kwargs: calls["clear"].append(True)
    )
    monkeypatch.setattr(
        engine_module,
        "resolve_last_operator_activity",
        lambda store, **kwargs: physical,
    )
    return calls


@pytest.mark.asyncio
async def test_session_brief_rides_runtime_prompt_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Category 8: the brief is a prompt SUFFIX — absent from the system
    append (win32 argv guard), absent from persisted chat.db rows (history
    purity), and the outgoing text is the runtime text unmodified."""
    store = SQLiteSessionStore(tmp_path / "chat.db")
    convo = ConversationEngine(store, _make_project_root(tmp_path))
    captured: dict[str, object] = {}

    async def fake_run(request):
        captured["request"] = request
        return RuntimeResult(
            text="Morning rundown.",
            runtime_lane=RUNTIME_LANE_GENERIC,
            provider="openai-codex",
            model="gpt-5.5",
            profile_key="primary-openai-codex",
        )

    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)
    calls = _patch_brief_seams(
        monkeypatch,
        brief=_fired_brief(),
        physical=_dt_cls.now() - _td(hours=10),
    )

    message = _make_message("good morning, how are we looking?")
    outputs = [out async for out in convo.handle_message(message)]

    request = captured["request"]
    assert request.prompt.endswith(_FIRED_BLOCK)
    assert request.prompt.startswith("good morning, how are we looking?")
    assert "# Session Opening Brief" not in request.system_prompt["append"]
    assert outputs[-1].text == "Morning rundown."
    # History purity: the persisted user row is the BARE operator text.
    messages = store.list_messages("telegram:chat-1:thread-1")
    assert messages[0].content == "good morning, how are we looking?"
    assert "# Session Opening Brief" not in messages[0].content
    assert "# Session Opening Brief" not in messages[1].content
    assert len(calls["build"]) == 1
    # fired -> the (absent) marker is still cleared best-effort
    assert calls["clear"] == [True]
    # In-memory guard armed.
    assert convo._session_brief_fired_at is not None


@pytest.mark.asyncio
async def test_session_brief_coexists_with_attachment_brief_last(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    convo = ConversationEngine(store, _make_project_root(tmp_path))
    captured: dict[str, object] = {}

    async def fake_run(request):
        captured["request"] = request
        return RuntimeResult(
            text="ok",
            runtime_lane=RUNTIME_LANE_GENERIC,
            provider="openai-codex",
            model="gpt-5.5",
            profile_key="primary-openai-codex",
        )

    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)
    monkeypatch.setattr(
        engine_module, "build_attachment_context", lambda attachments: "DOC BODY"
    )
    _patch_brief_seams(
        monkeypatch,
        brief=_fired_brief(),
        physical=_dt_cls.now() - _td(hours=10),
    )

    outputs = [out async for out in convo.handle_message(_make_message("summarize"))]
    assert outputs[-1].text == "ok"
    prompt = captured["request"].prompt
    assert prompt.endswith(_FIRED_BLOCK)  # brief LAST
    doc_idx = prompt.index("# Uploaded Document Content")
    brief_idx = prompt.index("# Session Opening Brief")
    assert doc_idx < brief_idx
    assert "DOC BODY" in prompt


@pytest.mark.asyncio
async def test_session_brief_not_away_leaves_prompt_unmodified(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    convo = ConversationEngine(store, _make_project_root(tmp_path))
    captured: dict[str, object] = {}

    async def fake_run(request):
        captured["request"] = request
        return RuntimeResult(
            text="ok",
            runtime_lane=RUNTIME_LANE_GENERIC,
            provider="openai-codex",
            model="gpt-5.5",
            profile_key="primary-openai-codex",
        )

    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)
    _patch_brief_seams(
        monkeypatch,
        brief=_suppressed_brief("not_away", 0.1),
        physical=_dt_cls.now() - _td(minutes=5),
    )

    message = _make_message("quick follow-up question")
    outputs = [out async for out in convo.handle_message(message)]
    assert outputs[-1].text == "ok"
    assert captured["request"].prompt == "quick follow-up question"


def test_session_brief_gate_skips_piv_and_non_interactive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Category 9: PIV turns and non-interactive sources never reach the
    builder; the M5 fail-closed sweep proves raw exact-match (no
    normalization rescue)."""
    store = SQLiteSessionStore(tmp_path / "chat.db")
    convo = ConversationEngine(store, _make_project_root(tmp_path))

    def _boom_builder(memory_dir, **kwargs):
        raise AssertionError("builder must not be called for gated turns")

    def _boom_resolver(store, **kwargs):
        raise AssertionError("resolver must not run for gated turns")

    monkeypatch.setattr(_pb, "build_session_opening_brief", _boom_builder)
    monkeypatch.setattr(
        engine_module, "resolve_last_operator_activity", _boom_resolver
    )

    piv_message = _make_message("run the workflow")
    piv_message.is_piv = True
    trace: dict[str, object] = {}
    assert convo._maybe_session_brief(piv_message, trace_decisions=trace) == ("", None)
    assert trace["session_brief"]["suppressed"] == "is_piv"
    assert trace["session_brief"]["fired"] is False

    # M5 fail-closed sweep: raw exact equality — whitespace, case, and empty
    # variants all fail closed (normalize_source would have rescued them).
    for source in ("cron", "tool", "hook", "cron ", "TOOL", ""):
        message = _make_message("hello")
        message.source = source
        trace = {}
        assert convo._maybe_session_brief(message, trace_decisions=trace) == ("", None)
        assert trace["session_brief"]["suppressed"] == "non_interactive", source


@pytest.mark.asyncio
async def test_imagegen_piv_turn_forces_generic_runtime_and_inlines_skill(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    skill_path = project_root / ".claude" / "skills" / "imagegen" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        "---\nname: imagegen\ndescription: test image skill\n---\n# Imagegen Skill\n",
        encoding="utf-8",
    )
    convo = ConversationEngine(store, project_root)
    captured: dict[str, object] = {}

    async def fake_run(request):
        captured["request"] = request
        return RuntimeResult(
            text="image ok",
            runtime_lane=RUNTIME_LANE_GENERIC,
            provider="openai-codex",
            model="gpt-5.5",
            profile_key="primary-openai-codex",
        )

    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)

    message = _make_message("Run the imagegen skill with a owner packet")
    message.is_piv = True
    message.piv_command = "imagegen"
    outputs = [out async for out in convo.handle_message(message)]

    request = captured["request"]
    assert outputs[-1].text == "image ok"
    assert request.task_name == "image_generation"
    assert request.runtime_lane == RUNTIME_LANE_GENERIC
    assert request.allow_fallback is False
    append = request.system_prompt["append"]
    assert "# Active Skill: imagegen" in append
    assert "# Imagegen Skill" in append
    # Protected-prefix invariant: the skill block must NEVER displace
    # GROUNDING_RULES from the head of the append (the win32 argv cap is a
    # head-keep — anything ahead of the grounding rules pushes safety/identity
    # content toward silent tail truncation).
    assert append.startswith(engine_module.GROUNDING_RULES)
    assert append.index("# Active Skill: imagegen") > 0


@pytest.mark.asyncio
async def test_imagegen_oversized_skill_body_is_capped_and_grounding_stays_prefix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A realistic large SKILL.md (the real imagegen body is ~11.5KB) must not
    blow the win32 head-keep cap: the inlined block is capped and the
    grounding rules stay the literal prefix of the append."""
    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    skill_path = project_root / ".claude" / "skills" / "imagegen" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    oversized_body = "# Imagegen Skill\n" + ("instruction line\n" * 2000)  # ~34KB
    skill_path.write_text(
        "---\nname: imagegen\ndescription: test image skill\n---\n" + oversized_body,
        encoding="utf-8",
    )
    convo = ConversationEngine(store, project_root)
    captured: dict[str, object] = {}

    async def fake_run(request):
        captured["request"] = request
        return RuntimeResult(
            text="image ok",
            runtime_lane=RUNTIME_LANE_GENERIC,
            provider="openai-codex",
            model="gpt-5.5",
            profile_key="primary-openai-codex",
        )

    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)

    message = _make_message("Run the imagegen skill with a owner packet")
    message.is_piv = True
    message.piv_command = "imagegen"
    outputs = [out async for out in convo.handle_message(message)]

    request = captured["request"]
    assert outputs[-1].text == "image ok"
    append = request.system_prompt["append"]
    assert append.startswith(engine_module.GROUNDING_RULES)
    assert "[skill body truncated at cap]" in append
    # The capped block bounds how much skill body can land in the append.
    skill_start = append.index("# Active Skill: imagegen")
    grounding_end = len(engine_module.GROUNDING_RULES)
    capped_len = append.index("[skill body truncated at cap]") - skill_start
    assert capped_len <= engine_module.SKILL_PROMPT_BLOCK_MAX_CHARS
    assert skill_start >= grounding_end


def test_session_brief_negative_decisions_reach_trace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Category 9 (M1): every builder-suppressed reason lands in
    trace_decisions — not only fired cases."""
    store = SQLiteSessionStore(tmp_path / "chat.db")
    convo = ConversationEngine(store, _make_project_root(tmp_path))

    for reason, away in (
        ("disabled", None),
        ("no_history", None),
        ("not_away", 0.25),
        ("no_fresh_items", 12.0),
    ):
        _patch_brief_seams(
            monkeypatch,
            brief=_suppressed_brief(reason, away),
            physical=_dt_cls.now() - _td(hours=1),
        )
        trace: dict[str, object] = {}
        out, token = convo._maybe_session_brief(
            _make_message("hello"), trace_decisions=trace
        )
        assert out == ""
        assert token is None
        decision = trace["session_brief"]
        assert decision["suppressed"] == reason
        assert decision["fired"] is False
        if away is None:
            assert decision["away_hours"] is None
        else:
            assert decision["away_hours"] == pytest.approx(away, abs=0.01)


def test_session_brief_double_fire_guard_folds_fired_at(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Category 9: _session_brief_fired_at folds into the max so a second
    message cannot re-fire even before the first turn persists."""
    store = SQLiteSessionStore(tmp_path / "chat.db")
    convo = ConversationEngine(store, _make_project_root(tmp_path))
    physical = _dt_cls(2026, 6, 11, 22, 0)
    fired_at = _dt_cls(2026, 6, 12, 6, 30)
    convo._session_brief_fired_at = fired_at
    calls = _patch_brief_seams(
        monkeypatch,
        brief=_suppressed_brief("not_away", 0.1),
        physical=physical,
    )

    convo._maybe_session_brief(_make_message("hello"), trace_decisions={})
    assert len(calls["build"]) == 1
    assert calls["build"][0]["last_activity"] == fired_at  # max(physical, fired_at)


def test_session_brief_marker_min_defuses_post_bump_physical(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Category 10: with a marker present, the effective boundary is
    min(marker, physical) — the marker predates the router bump."""
    store = SQLiteSessionStore(tmp_path / "chat.db")
    convo = ConversationEngine(store, _make_project_root(tmp_path))
    marker_boundary = _dt_cls(2026, 6, 11, 22, 0)
    bumped_physical = _dt_cls(2026, 6, 12, 6, 29)  # the /status bump
    calls = _patch_brief_seams(
        monkeypatch,
        brief=_fired_brief(),
        owed=marker_boundary,
        physical=bumped_physical,
    )

    out, token = convo._maybe_session_brief(_make_message("hello"), trace_decisions={})
    assert out == _FIRED_BLOCK
    assert calls["build"][0]["last_activity"] == marker_boundary
    # #138: a FIRED decision defers consumption onto its turn-owned token —
    # the marker is only cleared once the reply reaches the operator.
    assert calls["clear"] == []
    assert convo._session_brief_pending is token
    convo._commit_session_brief(token)
    assert calls["clear"] == [True]
    assert convo._session_brief_pending is None


def test_session_brief_silent_decision_consumes_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Category 10: a silent (no_fresh_items) decision ALSO consumes the
    marker — only-on-fire would defer the debt into an off-window fire."""
    store = SQLiteSessionStore(tmp_path / "chat.db")
    convo = ConversationEngine(store, _make_project_root(tmp_path))
    calls = _patch_brief_seams(
        monkeypatch,
        brief=_suppressed_brief("no_fresh_items", 10.0),
        owed=_dt_cls(2026, 6, 11, 22, 0),
        physical=_dt_cls(2026, 6, 12, 6, 29),
    )

    out, token = convo._maybe_session_brief(_make_message("hello"), trace_decisions={})
    assert out == ""
    assert token is None  # silent decisions carry no pending state
    assert calls["clear"] == [True]


@pytest.mark.asyncio
async def test_session_brief_builder_exception_fail_open_marker_intact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Category 9/10: a builder explosion never breaks the turn (decision
    "error", bare prompt) and leaves the marker INTACT for retry."""
    store = SQLiteSessionStore(tmp_path / "chat.db")
    convo = ConversationEngine(store, _make_project_root(tmp_path))
    captured: dict[str, object] = {}

    async def fake_run(request):
        captured["request"] = request
        return RuntimeResult(
            text="still works",
            runtime_lane=RUNTIME_LANE_GENERIC,
            provider="openai-codex",
            model="gpt-5.5",
            profile_key="primary-openai-codex",
        )

    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)

    def _boom_builder(memory_dir, **kwargs):
        raise RuntimeError("builder exploded")

    cleared: list[bool] = []
    monkeypatch.setattr(_pb, "build_session_opening_brief", _boom_builder)
    monkeypatch.setattr(
        _pb, "read_brief_owed", lambda **kwargs: _dt_cls(2026, 6, 11, 22, 0)
    )
    monkeypatch.setattr(
        _pb, "clear_brief_owed", lambda **kwargs: cleared.append(True)
    )
    monkeypatch.setattr(
        engine_module,
        "resolve_last_operator_activity",
        lambda store, **kwargs: _dt_cls(2026, 6, 12, 6, 0),
    )

    trace: dict[str, object] = {}
    out, token = convo._maybe_session_brief(_make_message("hello"), trace_decisions=trace)
    assert out == ""
    assert token is None
    assert trace["session_brief"]["suppressed"] == "error"
    assert cleared == []  # marker survives for retry

    # The turn itself completes bare through the real path.
    outputs = [out async for out in convo.handle_message(_make_message("hello"))]
    assert outputs[-1].text == "still works"
    assert captured["request"].prompt == "hello"


# =============================================================================
# #138 — commit-on-success: the brief is consumed at DELIVERY, not at decision.
# Every test below fails on the pre-#138 eager-clear code; tests 6-8 also fail
# on the (rejected) PR #155 shared-pending design.
# =============================================================================

_MARKER_BOUNDARY = _dt_cls(2026, 6, 11, 22, 0)
_BUMPED_PHYSICAL = _dt_cls(2026, 6, 12, 6, 29)


def _wake_up_seams(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Fired-brief seams with a marker owed — the wake-up-morning setup."""
    return _patch_brief_seams(
        monkeypatch,
        brief=_fired_brief(),
        owed=_MARKER_BOUNDARY,
        physical=_BUMPED_PHYSICAL,
    )


def _raising_runtime(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException, captured: dict
) -> None:
    async def _boom(request):
        captured["prompt"] = request.prompt
        raise exc

    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", _boom)


def _ok_runtime(
    monkeypatch: pytest.MonkeyPatch, captured: dict, text: str = "ok"
) -> None:
    async def _ok(request):
        captured["prompt"] = request.prompt
        return RuntimeResult(
            text=text,
            runtime_lane=RUNTIME_LANE_GENERIC,
            provider="openai-codex",
            model="gpt-5.5",
            profile_key="primary-openai-codex",
        )

    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", _ok)


def _assert_brief_re_armed(convo: ConversationEngine, calls: dict) -> None:
    """The brief rode a turn that failed → nothing consumed, guard restored."""
    assert calls["clear"] == []                     # marker survives on disk
    assert convo._session_brief_fired_at is None    # rolled back to prev value
    assert convo._session_brief_pending is None     # pending released


@pytest.mark.asyncio
async def test_session_brief_survives_runtime_execution_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """#138: a RuntimeExecutionError (quota/auth — the documented morning
    failure) must NOT eat the brief; the next successful turn re-fires it
    exactly once."""
    store = SQLiteSessionStore(tmp_path / "chat.db")
    convo = ConversationEngine(store, _make_project_root(tmp_path))
    calls = _wake_up_seams(monkeypatch)
    captured: dict[str, object] = {}
    _raising_runtime(monkeypatch, RuntimeExecutionError("quota exhausted"), captured)

    outputs = [o async for o in convo.handle_message(_make_message("good morning"))]

    assert outputs[-1].is_error is True
    assert captured["prompt"].endswith(_FIRED_BLOCK)  # the brief DID ride it
    _assert_brief_re_armed(convo, calls)

    # Retry turn succeeds → the brief rides again and is consumed exactly once.
    _ok_runtime(monkeypatch, captured, text="Morning rundown.")
    outputs = [o async for o in convo.handle_message(_make_message("still there?"))]

    assert outputs[-1].text == "Morning rundown."
    assert captured["prompt"].endswith(_FIRED_BLOCK)
    assert calls["clear"] == [True]
    assert convo._session_brief_pending is None


@pytest.mark.asyncio
async def test_session_brief_survives_generic_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """#138: the generic `except Exception` path rolls back too."""
    store = SQLiteSessionStore(tmp_path / "chat.db")
    convo = ConversationEngine(store, _make_project_root(tmp_path))
    calls = _wake_up_seams(monkeypatch)
    captured: dict[str, object] = {}
    _raising_runtime(monkeypatch, ValueError("provider blew up"), captured)

    outputs = [o async for o in convo.handle_message(_make_message("good morning"))]

    assert outputs[-1].is_error is True
    assert captured["prompt"].endswith(_FIRED_BLOCK)
    _assert_brief_re_armed(convo, calls)


@pytest.mark.asyncio
async def test_session_brief_survives_killswitch_disabled_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """#138: the kill-switch branch returns is_error=False (operator-intended
    state) but is still a non-delivery — the brief must survive it."""
    store = SQLiteSessionStore(tmp_path / "chat.db")
    convo = ConversationEngine(store, _make_project_root(tmp_path))
    calls = _wake_up_seams(monkeypatch)
    captured: dict[str, object] = {}
    _raising_runtime(monkeypatch, KillSwitchDisabled("chat"), captured)

    outputs = [o async for o in convo.handle_message(_make_message("good morning"))]

    assert outputs[-1].is_error is False        # operator-intended, not an error
    assert "[killswitch:chat]" in outputs[-1].text
    _assert_brief_re_armed(convo, calls)


@pytest.mark.asyncio
async def test_session_brief_happy_path_no_double_fire(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """#138: delivery consumes the debt EXACTLY once — an immediate second
    turn neither re-fires nor re-clears (the fold + a real marker clear)."""
    store = SQLiteSessionStore(tmp_path / "chat.db")
    convo = ConversationEngine(store, _make_project_root(tmp_path))
    builds: list[dict] = []

    def _fold_aware_builder(memory_dir, **kwargs):
        builds.append(kwargs)
        last_activity = kwargs.get("last_activity")
        if last_activity is not None and last_activity <= _MARKER_BOUNDARY:
            return _fired_brief()
        return _suppressed_brief("not_away", 0.1)

    calls = _patch_brief_seams(
        monkeypatch,
        builder=_fold_aware_builder,
        owed=_MARKER_BOUNDARY,
        physical=_BUMPED_PHYSICAL,
    )
    # Stateful marker: a clear must actually remove the debt, so the second
    # turn sees the world the first turn left behind.
    marker: dict[str, _dt_cls | None] = {"owed": _MARKER_BOUNDARY}
    monkeypatch.setattr(_pb, "read_brief_owed", lambda **kwargs: marker["owed"])

    def _clear(**kwargs):
        marker["owed"] = None
        calls["clear"].append(True)

    monkeypatch.setattr(_pb, "clear_brief_owed", _clear)
    captured: dict[str, object] = {}
    _ok_runtime(monkeypatch, captured, text="Morning rundown.")

    outputs = [o async for o in convo.handle_message(_make_message("good morning"))]
    assert outputs[-1].text == "Morning rundown."
    assert captured["prompt"].endswith(_FIRED_BLOCK)
    assert calls["clear"] == [True]
    assert convo._session_brief_pending is None

    outputs = [o async for o in convo.handle_message(_make_message("and one more"))]
    assert outputs[-1].text == "Morning rundown."
    # Turn 2 carries the recent-conversation prefix (unrelated engine feature)
    # but NO brief — and consumes nothing further.
    assert "# Session Opening Brief" not in captured["prompt"]
    assert captured["prompt"].endswith("and one more")
    assert calls["clear"] == [True]                  # still exactly once
    assert len(builds) == 2


@pytest.mark.asyncio
async def test_session_brief_rollback_is_noop_without_pending_brief(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """#138: an ordinary error turn that carried NO brief leaves the guard
    untouched — rollback(None) can never clobber engine state."""
    store = SQLiteSessionStore(tmp_path / "chat.db")
    convo = ConversationEngine(store, _make_project_root(tmp_path))
    calls = _patch_brief_seams(
        monkeypatch,
        brief=_suppressed_brief("not_away", 0.1),
        physical=_dt_cls.now() - _td(minutes=5),
    )
    live_fired_at = _dt_cls(2026, 6, 12, 6, 30)
    convo._session_brief_fired_at = live_fired_at
    captured: dict[str, object] = {}
    _raising_runtime(monkeypatch, RuntimeExecutionError("boom"), captured)

    outputs = [o async for o in convo.handle_message(_make_message("hello"))]

    assert outputs[-1].is_error is True
    assert convo._session_brief_fired_at == live_fired_at   # untouched
    assert convo._session_brief_pending is None
    assert calls["clear"] == []

    # Direct seam proof: a None token never touches a live pending.
    sentinel: dict[str, object] = {"prev_fired_at": None}
    convo._session_brief_pending = sentinel
    convo._rollback_session_brief(None)
    convo._commit_session_brief(None)
    assert convo._session_brief_pending is sentinel
    assert calls["clear"] == []


@pytest.mark.asyncio
async def test_session_brief_cancelled_error_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """#138 gate req 2: CancelledError is a BaseException — it bypasses both
    `except Exception` handlers. The dedicated handler must roll back AND
    re-raise (swallowing a cancel would be a worse bug than the one fixed)."""
    store = SQLiteSessionStore(tmp_path / "chat.db")
    convo = ConversationEngine(store, _make_project_root(tmp_path))
    calls = _wake_up_seams(monkeypatch)
    captured: dict[str, object] = {}
    _raising_runtime(monkeypatch, asyncio.CancelledError(), captured)

    with pytest.raises(asyncio.CancelledError):        # PROPAGATED, not eaten
        async for _ in convo.handle_message(_make_message("good morning")):
            pass

    assert captured["prompt"].endswith(_FIRED_BLOCK)
    _assert_brief_re_armed(convo, calls)


@pytest.mark.asyncio
async def test_session_brief_cron_turn_after_cancelled_interactive_does_not_consume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """#138 gate req 1: a cron turn carries no token, so its successful
    delivery must be a structural no-op — it cannot eat the debt a cancelled
    interactive turn left owed."""
    store = SQLiteSessionStore(tmp_path / "chat.db")
    convo = ConversationEngine(store, _make_project_root(tmp_path))
    calls = _wake_up_seams(monkeypatch)
    captured: dict[str, object] = {}
    _raising_runtime(monkeypatch, asyncio.CancelledError(), captured)

    with pytest.raises(asyncio.CancelledError):
        async for _ in convo.handle_message(_make_message("good morning")):
            pass
    _assert_brief_re_armed(convo, calls)

    _ok_runtime(monkeypatch, captured, text="heartbeat ok")
    cron_message = _make_message("scheduled check")
    cron_message.source = "cron"
    outputs = [o async for o in convo.handle_message(cron_message)]

    assert outputs[-1].text == "heartbeat ok"
    assert calls["clear"] == []                     # debt still owed
    assert convo._session_brief_pending is None


@pytest.mark.asyncio
async def test_session_brief_foreign_pending_untouched_by_sibling_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """#138 gate req 1: one engine hosts many concurrent conversations. A
    sibling turn — success OR failure — must not commit, roll back, or
    clobber a pending brief it never carried (identity guard, not equality:
    the tokens are structurally equal dicts)."""
    store = SQLiteSessionStore(tmp_path / "chat.db")
    convo = ConversationEngine(store, _make_project_root(tmp_path))
    calls = _wake_up_seams(monkeypatch)
    # In-flight brief owned by another turn. Structurally identical to what a
    # fresh token looks like — only identity distinguishes them.
    sentinel: dict[str, object] = {"prev_fired_at": None}
    live_fired_at = _dt_cls(2026, 6, 12, 6, 30)
    convo._session_brief_pending = sentinel
    convo._session_brief_fired_at = live_fired_at

    # The sibling's own decision defers before ever reaching the builder.
    trace: dict[str, object] = {}
    assert convo._maybe_session_brief(
        _make_discord_message("what's up?"), trace_decisions=trace
    ) == ("", None)
    assert trace["session_brief"]["suppressed"] == "brief_in_flight"
    assert calls["build"] == []

    captured: dict[str, object] = {}
    _ok_runtime(monkeypatch, captured, text="sibling reply")
    outputs = [o async for o in convo.handle_message(_make_discord_message("hey"))]
    assert outputs[-1].text == "sibling reply"
    assert convo._session_brief_pending is sentinel   # identity preserved
    assert calls["clear"] == []                       # nothing consumed

    _raising_runtime(monkeypatch, RuntimeExecutionError("sibling boom"), captured)
    outputs = [o async for o in convo.handle_message(_make_discord_message("again"))]
    assert outputs[-1].is_error is True
    assert convo._session_brief_pending is sentinel
    assert convo._session_brief_fired_at == live_fired_at  # guard not clobbered
    assert calls["clear"] == []
    assert calls["build"] == []


@pytest.mark.asyncio
async def test_chat_recall_honors_recall_max_results(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The chat hot path must pass config.RECALL_MAX_RESULTS to the recall
    service, not a hardcoded 5 (#136). Patch the knob and assert it propagates."""
    import config

    monkeypatch.setattr(config, "RECALL_MAX_RESULTS", 2)

    store = SQLiteSessionStore(tmp_path / "chat.db")
    project_root = _make_project_root(tmp_path)
    convo = ConversationEngine(store, project_root)
    captured: dict[str, object] = {}

    async def fake_recall(**kwargs):
        captured["max_results"] = kwargs.get("max_results")
        return _FakeRecallResponse(tier="tier_1", formatted_text="")

    async def fake_run(request):
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
            _make_message("give me a real recall query please")
        )
    ]

    assert outputs[-1].text == "ok"
    assert captured["max_results"] == 2, (
        f"engine must honor RECALL_MAX_RESULTS (got {captured.get('max_results')!r}, "
        "was hardcoded 5 before #136)"
    )


def test_session_brief_decision_exception_releases_pending(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """#138 gate (exception atomicity): an exception AFTER the pending slot is
    claimed must self-rollback. Otherwise the caller gets token=None, nobody can
    ever commit/rollback it, and EVERY later brief wedges on brief_in_flight."""
    store = SQLiteSessionStore(tmp_path / "chat.db")
    convo = ConversationEngine(store, _make_project_root(tmp_path))
    boundary = _dt_cls.now() - _td(hours=10)
    _patch_brief_seams(monkeypatch, brief=_fired_brief(), owed=boundary, physical=boundary)

    real_print = print

    def _boom(*a, **k):
        if a and "SessionBrief] fired" in str(a[0]):
            raise RuntimeError("log sink exploded")
        real_print(*a, **k)

    monkeypatch.setattr("builtins.print", _boom)
    out, token = convo._maybe_session_brief(_make_message("gm"), trace_decisions={})
    monkeypatch.undo()

    assert out == ""
    assert token is None
    assert convo._session_brief_pending is None, (
        "decision-path failure must release the pending slot, not wedge it"
    )


# =============================================================================
# #138 — abandonment coverage: handle_message's holder + finally re-arms the
# token on every exception-delivering exit (cancel/close/GC). The contract is
# defer-never-lose: a consumer that breaks while RETAINING the generator holds
# the slot (marker intact) until resume-to-exhaustion (commit), close/
# finalize (rollback), or restart (state discarded) — pinned below.
# =============================================================================


@pytest.mark.asyncio
async def test_session_brief_aclose_after_first_yield_releases_pending(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """#138: the consumer closes the generator after receiving the reply chunk
    but BEFORE resuming it (the inner commit has not run). async-for never
    acloses the inner generator, so without handle_message's holder/finally the
    inner's own seams never run and the slot wedges for the process lifetime.
    The outer finally must free it SYNCHRONOUSLY at aclose."""
    store = SQLiteSessionStore(tmp_path / "chat.db")
    convo = ConversationEngine(store, _make_project_root(tmp_path))
    calls = _wake_up_seams(monkeypatch)
    captured: dict[str, object] = {}
    _ok_runtime(monkeypatch, captured, text="Morning rundown.")

    gen = convo.handle_message(_make_message("good morning"))
    first = await gen.__anext__()  # reply delivered to the consumer...
    assert first.text == "Morning rundown."
    assert convo._session_brief_pending is not None  # ...but not yet committed

    await gen.aclose()  # abandoned right at the delivery yield

    _assert_brief_re_armed(convo, calls)  # slot freed, marker intact, guard restored

    # The next turn re-fires the brief and consumes it exactly once.
    _ok_runtime(monkeypatch, captured, text="here you go")
    outputs = [o async for o in convo.handle_message(_make_message("still there?"))]
    assert outputs[-1].text == "here you go"
    assert captured["prompt"].endswith(_FIRED_BLOCK)
    assert calls["clear"] == [True]
    assert convo._session_brief_pending is None


@pytest.mark.asyncio
async def test_session_brief_break_and_retain_defers_then_close_releases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """#138 contract test: the hole Codex named. A consumer that BREAKS out
    of the iteration while RETAINING the generator delivers no exception —
    no finally runs anywhere, so the slot HOLDS. That is defer-never-lose:
    the marker is intact, briefs are deferred, nothing is consumed. Only
    resume-to-exhaustion (which commits and frees it), close/cancel/GC
    (rollback), or restart (state discarded) releases it.

    Note: the defer half of this test also passes on pre-holder code (a
    held slot defers on any build). The load-bearing half is the
    aclose-release — the same mechanism the money test proves against the
    pre-holder code."""
    store = SQLiteSessionStore(tmp_path / "chat.db")
    convo = ConversationEngine(store, _make_project_root(tmp_path))
    calls = _wake_up_seams(monkeypatch)
    captured: dict[str, object] = {}
    _ok_runtime(monkeypatch, captured, text="Morning rundown.")

    gen = convo.handle_message(_make_message("good morning"))
    first = await gen.__anext__()
    assert first.text == "Morning rundown."
    # BREAK — stop iterating, retain the generator, do NOT aclose.
    assert convo._session_brief_pending is not None

    # Defer semantics: the next decision is suppressed, the marker intact.
    out, token = convo._maybe_session_brief(
        _make_message("anyone home?"), trace_decisions={},
    )
    assert (out, token) == ("", None)
    assert calls["clear"] == []

    # Close releases the slot deterministically.
    await gen.aclose()
    _assert_brief_re_armed(convo, calls)

    # And the next real turn refires and consumes exactly once.
    _ok_runtime(monkeypatch, captured, text="here you go")
    outputs = [o async for o in convo.handle_message(_make_message("still there?"))]
    assert outputs[-1].text == "here you go"
    assert captured["prompt"].endswith(_FIRED_BLOCK)
    assert calls["clear"] == [True]
    assert convo._session_brief_pending is None
