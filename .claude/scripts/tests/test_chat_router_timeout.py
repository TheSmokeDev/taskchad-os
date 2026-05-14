from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

import router as router_module
from models import Channel, IncomingMessage, OutgoingMessage, Platform, User
from router import ChatRouter


class _SlowEngine:
    session_store = None

    async def handle_message(self, incoming: IncomingMessage, progress: dict[str, Any]):
        await asyncio.sleep(60)
        yield OutgoingMessage(text="late", channel=incoming.channel, thread=incoming.thread)


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

    async def send(self, message: OutgoingMessage) -> str:
        self.sent.append(message)
        return "placeholder-1"

    async def update(self, message: OutgoingMessage) -> None:
        self.updates.append(message)


@pytest.mark.asyncio
async def test_engine_timeout_updates_placeholder(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(router_module, "ENGINE_TIMEOUT_SECONDS", 0.01)

    channel = Channel(platform=Platform.CLI, platform_id="test-channel")
    incoming = IncomingMessage(
        text="please do a slow thing",
        user=User(platform=Platform.CLI, platform_id="user-1"),
        channel=channel,
        platform=Platform.CLI,
    )
    adapter = _CaptureAdapter()
    router = ChatRouter(_SlowEngine(), _NoopManager())  # type: ignore[arg-type]

    await router._handle_inner(adapter, incoming)

    assert adapter.sent[0].text == "Thinking..."
    assert adapter.updates
    assert adapter.updates[-1].is_error is True
    assert "That took too long" in adapter.updates[-1].text
    assert "specific question" in adapter.updates[-1].text
