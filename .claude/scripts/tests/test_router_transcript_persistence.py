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
        return {"status", "clear"}

    def get_all_command_names(self):
        return ["status", "clear"]

    async def dispatch(self, command, adapter, incoming, args, collect_only=False):
        if command == "clear":
            return "Session cleared. Next message starts fresh."
        if command == "status":
            return "Session Status"
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
