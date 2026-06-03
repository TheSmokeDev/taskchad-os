from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

import router as router_module
from models import Channel, IncomingMessage, OutgoingMessage, Platform, User
from router import ChatRouter
from session import SQLiteSessionStore


class _SlowEngine:
    def __init__(self, session_store=None) -> None:
        self.session_store = session_store

    async def handle_message(self, incoming: IncomingMessage, progress: dict[str, Any]):
        await asyncio.sleep(60)
        yield OutgoingMessage(text="late", channel=incoming.channel, thread=incoming.thread)


class _MultiYieldEngine:
    def __init__(self, session_store=None) -> None:
        self.session_store = session_store

    async def handle_message(self, incoming: IncomingMessage, progress: dict[str, Any]):
        yield OutgoingMessage(text="real answer", channel=incoming.channel, thread=incoming.thread)
        yield OutgoingMessage(text="follow-up nudge", channel=incoming.channel, thread=incoming.thread)


class _NoopManager:
    command_regex = re.compile(r"^/(\w+)\b\s*(.*)$")

    def get_router_commands(self) -> dict[str, Any]:
        return {}

    def get_all_command_names(self) -> list[str]:
        return ["noop"]

    def detect_intents(self, text: str) -> list[str]:
        return []

    def wants_analysis(self, text: str) -> bool:
        return False


class _CaptureAdapter:
    platform = Platform.CLI

    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []
        self.updates: list[OutgoingMessage] = []
        self.events: list[tuple[str, str]] = []

    async def send(self, message: OutgoingMessage) -> str:
        self.sent.append(message)
        self.events.append(("send", message.text))
        return f"sent-{len(self.sent)}"

    async def update(self, message: OutgoingMessage) -> str:
        self.updates.append(message)
        self.events.append(("update", message.text))
        return message.update_message_id or f"updated-{len(self.updates)}"


class _FailingFinalUpdateAdapter(_CaptureAdapter):
    async def update(self, message: OutgoingMessage) -> str:
        self.updates.append(message)
        self.events.append(("update", message.text))
        raise RuntimeError("final delivery failed")


@pytest.mark.asyncio
async def test_engine_timeout_updates_placeholder_and_persists_turn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    monkeypatch.setattr(router_module, "ENGINE_TIMEOUT_SECONDS", 0.01)

    store = SQLiteSessionStore(tmp_path / "chat.db")
    channel = Channel(platform=Platform.CLI, platform_id="test-channel")
    incoming = IncomingMessage(
        text="please do a slow thing",
        user=User(platform=Platform.CLI, platform_id="user-1"),
        channel=channel,
        platform=Platform.CLI,
    )
    adapter = _CaptureAdapter()
    router = ChatRouter(_SlowEngine(store), _NoopManager())  # type: ignore[arg-type]

    await router._handle_inner(adapter, incoming)

    assert adapter.sent[0].text == "Thinking..."
    assert adapter.updates
    assert adapter.updates[-1].is_error is True
    assert "chat runtime timeout" in adapter.updates[-1].text
    assert "I did not finish that turn" in adapter.updates[-1].text

    messages = store.list_messages("cli:test-channel:test-channel")
    assert [msg.role for msg in messages] == ["user", "assistant"]
    assert messages[0].content == "please do a slow thing"
    assert "chat runtime timeout" in messages[1].content


@pytest.mark.asyncio
async def test_multi_yield_engine_preserves_first_output_as_placeholder_update(tmp_path):
    store = SQLiteSessionStore(tmp_path / "chat.db")
    channel = Channel(platform=Platform.CLI, platform_id="test-channel")
    incoming = IncomingMessage(
        text="tell me what you know",
        user=User(platform=Platform.CLI, platform_id="user-1"),
        channel=channel,
        platform=Platform.CLI,
    )
    adapter = _CaptureAdapter()
    router = ChatRouter(_MultiYieldEngine(store), _NoopManager())  # type: ignore[arg-type]

    await router._handle_inner(adapter, incoming)

    assert adapter.sent[0].text == "Thinking..."
    assert adapter.updates[-1].text == "real answer"
    assert adapter.sent[1].text == "follow-up nudge"
    assert adapter.events == [
        ("send", "Thinking..."),
        ("update", "real answer"),
        ("send", "follow-up nudge"),
    ]


@pytest.mark.asyncio
async def test_multi_yield_engine_suppresses_followup_when_final_update_fails(tmp_path):
    store = SQLiteSessionStore(tmp_path / "chat.db")
    channel = Channel(platform=Platform.CLI, platform_id="test-channel")
    incoming = IncomingMessage(
        text="tell me what you know",
        user=User(platform=Platform.CLI, platform_id="user-1"),
        channel=channel,
        platform=Platform.CLI,
    )
    adapter = _FailingFinalUpdateAdapter()
    router = ChatRouter(_MultiYieldEngine(store), _NoopManager())  # type: ignore[arg-type]

    await router._handle_inner(adapter, incoming)

    assert adapter.sent[0].text == "Thinking..."
    assert adapter.updates[-1].text == "real answer"
    assert all(msg.text != "follow-up nudge" for msg in adapter.sent)
    assert adapter.sent[1].is_error is True
    assert "delivery failed" in adapter.sent[1].text
    assert adapter.events == [
        ("send", "Thinking..."),
        ("update", "real answer"),
        (
            "send",
            "I generated a response, but delivery failed before it "
            "could be shown. I suppressed follow-up nudges for this turn.",
        ),
    ]
