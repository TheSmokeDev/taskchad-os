from __future__ import annotations

import re
from datetime import datetime

import pytest

from models import Channel, IncomingMessage, Platform, User
from router import ChatRouter
from session import SQLiteSessionStore


class _RecordingAdapter:
    platform = Platform.CLI

    def __init__(self) -> None:
        self.sent = []

    async def send(self, message):
        self.sent.append(message)
        return None

    async def update(self, message):
        self.sent.append(message)


class _RouterOnlyManager:
    command_regex = re.compile(r"^/(\w+)\b(.*)$")

    def get_router_commands(self):
        return {"status", "clear", "model", "provider", "teamroom"}

    def get_all_command_names(self):
        return ["status", "clear", "model", "provider", "teamroom"]

    async def dispatch(self, command, adapter, incoming, args, collect_only=False):
        if command == "clear":
            return "Session cleared. Next message starts fresh."
        if command == "status":
            return "Session Status"
        if command == "model":
            return "Switched runtime"
        if command == "provider":
            return "Runtime Provider Status"
        if command == "teamroom":
            return "Team Room Workflow"
        return None

    def detect_intents(self, text):
        return []

    def wants_analysis(self, text):
        return False


class _FakeEngine:
    def __init__(self, store):
        self.session_store = store

    async def handle_message(self, message, progress=None):
        if False:
            yield None


@pytest.mark.asyncio
async def test_router_command_persists_transcript_turn(tmp_path):
    store = SQLiteSessionStore(tmp_path / "chat.db")
    router = ChatRouter(_FakeEngine(store), _RouterOnlyManager())
    adapter = _RecordingAdapter()

    incoming = IncomingMessage(
        text="/status",
        user=User(Platform.CLI, "cli-user", "User"),
        channel=Channel(Platform.CLI, "cli-test", is_dm=True),
        platform=Platform.CLI,
        timestamp=datetime.now(),
    )

    await router._handle(adapter, incoming)

    session = store.get("cli", "cli-test", "cli-test")
    assert session is not None
    assert session.message_count == 1
    messages = store.list_messages("cli:cli-test:cli-test")
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[0].content == "/status"
    assert messages[1].content == "Session Status"


@pytest.mark.asyncio
async def test_clear_command_does_not_recreate_transcript_rows(tmp_path):
    store = SQLiteSessionStore(tmp_path / "chat.db")
    router = ChatRouter(_FakeEngine(store), _RouterOnlyManager())
    adapter = _RecordingAdapter()

    incoming = IncomingMessage(
        text="/clear",
        user=User(Platform.CLI, "cli-user", "User"),
        channel=Channel(Platform.CLI, "cli-test", is_dm=True),
        platform=Platform.CLI,
        timestamp=datetime.now(),
    )

    await router._handle(adapter, incoming)

    assert store.get("cli", "cli-test", "cli-test") is None
    assert store.list_messages("cli:cli-test:cli-test") == []


@pytest.mark.asyncio
async def test_model_command_persists_current_codex_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_LANE", "generic_runtime")
    monkeypatch.setenv("SECOND_BRAIN_GENERIC_PROVIDER", "openai-codex")
    monkeypatch.setenv("SECOND_BRAIN_CODEX_MODEL", "gpt-5.5")
    store = SQLiteSessionStore(tmp_path / "chat.db")
    router = ChatRouter(_FakeEngine(store), _RouterOnlyManager())
    adapter = _RecordingAdapter()

    incoming = IncomingMessage(
        text="/model codex",
        user=User(Platform.CLI, "cli-user", "User"),
        channel=Channel(Platform.CLI, "cli-test", is_dm=True),
        platform=Platform.CLI,
        timestamp=datetime.now(),
    )

    await router._handle(adapter, incoming)

    session = store.get("cli", "cli-test", "cli-test")
    assert session is not None
    assert session.runtime_lane == "generic_runtime"
    assert session.runtime_provider == "openai-codex"
    assert session.runtime_model == "gpt-5.5"
    assert session.runtime_session_id == ""


@pytest.mark.asyncio
async def test_provider_command_persists_current_claude_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_LANE", "claude_native")
    monkeypatch.delenv("SECOND_BRAIN_GENERIC_PROVIDER", raising=False)
    monkeypatch.setenv("SECOND_BRAIN_CLAUDE_MODEL", "claude-opus-4-6")
    store = SQLiteSessionStore(tmp_path / "chat.db")
    router = ChatRouter(_FakeEngine(store), _RouterOnlyManager())
    adapter = _RecordingAdapter()

    incoming = IncomingMessage(
        text="/provider",
        user=User(Platform.CLI, "cli-user", "User"),
        channel=Channel(Platform.CLI, "cli-test", is_dm=True),
        platform=Platform.CLI,
        timestamp=datetime.now(),
    )

    await router._handle(adapter, incoming)

    session = store.get("cli", "cli-test", "cli-test")
    assert session is not None
    assert session.runtime_lane == "claude_native"
    assert session.runtime_provider == "claude"
    assert session.runtime_model == "claude-opus-4-6"


@pytest.mark.asyncio
async def test_teamroom_runtime_command_persists_requested_runtime_lane(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_LANE", "claude_native")
    monkeypatch.setenv("SECOND_BRAIN_GENERIC_PROVIDER", "openai-codex")
    monkeypatch.setenv("SECOND_BRAIN_CODEX_MODEL", "gpt-5.5")
    store = SQLiteSessionStore(tmp_path / "chat.db")
    router = ChatRouter(_FakeEngine(store), _RouterOnlyManager())
    adapter = _RecordingAdapter()

    incoming = IncomingMessage(
        text="/teamroom --runtime --lane generic_runtime How should the team prioritize the next release?",
        user=User(Platform.CLI, "cli-user", "User"),
        channel=Channel(Platform.CLI, "cli-test", is_dm=True),
        platform=Platform.CLI,
        timestamp=datetime.now(),
    )

    await router._handle(adapter, incoming)

    session = store.get("cli", "cli-test", "cli-test")
    assert session is not None
    assert session.runtime_lane == "generic_runtime"
    assert session.runtime_provider == "auto"
    assert session.runtime_model == ""
    assert session.runtime_session_id == ""
