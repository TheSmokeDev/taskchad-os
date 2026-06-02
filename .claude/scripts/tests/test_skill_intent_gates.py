from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_CHAT_DIR = Path(__file__).resolve().parent.parent.parent / "chat"
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_CHAT_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR))

from commands import CATEGORIES, COMMANDS, CORE_INTENTS  # noqa: E402
from core_handlers import CORE_HANDLERS  # noqa: E402
from extension_manager import ExtensionManager  # noqa: E402
from models import Channel, IncomingMessage, OutgoingMessage, Platform, User  # noqa: E402
from router import ChatRouter  # noqa: E402


def _build_manager() -> ExtensionManager:
    manager = ExtensionManager()
    manager.register_core_commands(COMMANDS, CATEGORIES, CORE_HANDLERS)
    manager.register_core_intents(CORE_INTENTS)
    return manager


def _incoming(text: str) -> IncomingMessage:
    return IncomingMessage(
        text=text,
        user=User(Platform.CLI, "cli-user", "Tester"),
        channel=Channel(Platform.CLI, "cli-test", is_dm=True),
        platform=Platform.CLI,
    )


class _RecordingAdapter:
    platform = Platform.CLI

    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []
        self.updates: list[OutgoingMessage] = []

    async def send(self, message: OutgoingMessage) -> str:
        self.sent.append(message)
        return "placeholder-1"

    async def update(self, message: OutgoingMessage) -> None:
        self.updates.append(message)


class _RecordingEngine:
    session_store = None

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.prefetched_contexts: list[str] = []

    async def handle_message(self, incoming: IncomingMessage, progress=None):
        self.messages.append(incoming.text)
        self.prefetched_contexts.append(getattr(incoming, "prefetched_context", ""))
        yield OutgoingMessage(
            text="engine handled",
            channel=incoming.channel,
            thread=incoming.thread,
        )


class _SlashOnlyManager:
    command_regex = re.compile(r"^/(send)\b(.*)$")

    def get_router_commands(self):
        return {"send"}

    def get_all_command_names(self):
        return ["send"]

    async def dispatch(self, command, adapter, incoming, args, collect_only=False):
        return "slash command handled"

    def requires_external_action_confirmation(self, text: str) -> bool:
        raise AssertionError("explicit slash commands must bypass natural language gates")

    def detect_intents(self, text: str):
        return []

    def wants_analysis(self, text: str) -> bool:
        return False


@pytest.mark.asyncio
async def test_discussion_only_skill_mentions_reach_engine_without_intent_dispatch():
    engine = _RecordingEngine()
    router = ChatRouter(engine, _build_manager())
    adapter = _RecordingAdapter()

    await router._handle_inner(
        adapter,
        _incoming("should we use the email skill for inbox cleanup?"),
    )

    assert engine.messages == ["should we use the email skill for inbox cleanup?"]
    assert adapter.sent[0].text == "Thinking..."
    assert adapter.updates[-1].text == "engine handled"


@pytest.mark.asyncio
async def test_potential_external_action_requires_confirmation_before_engine():
    engine = _RecordingEngine()
    router = ChatRouter(engine, _build_manager())
    adapter = _RecordingAdapter()

    await router._handle_inner(
        adapter,
        _incoming("we should send an outreach email to customers today"),
    )

    assert engine.messages == []
    assert len(adapter.sent) == 1
    assert "contact a real person" in adapter.sent[0].text
    assert adapter.updates == []


@pytest.mark.asyncio
async def test_authorized_external_action_with_context_reaches_engine():
    engine = _RecordingEngine()
    router = ChatRouter(engine, _build_manager())
    adapter = _RecordingAdapter()
    text = "send this email to bob@example.com now: Hello Bob"

    await router._handle_inner(adapter, _incoming(text))

    assert engine.messages == [text]
    assert adapter.sent[0].text == "Thinking..."
    assert adapter.updates[-1].text == "engine handled"


@pytest.mark.asyncio
async def test_browserops_natural_language_prefetches_context_and_reaches_engine():
    async def fake_browserops(adapter, incoming, args, *, collect_only=False):
        assert collect_only is True
        return "BrowserOps context loaded"

    manager = _build_manager()
    manager._commands["browserops"].handler = fake_browserops
    engine = _RecordingEngine()
    router = ChatRouter(engine, manager)
    adapter = _RecordingAdapter()
    text = "open up your browser and go to LinkedIn"

    await router._handle_inner(adapter, _incoming(text))

    assert engine.messages == [text]
    assert "BrowserOps context loaded" in engine.prefetched_contexts[0]
    assert adapter.sent[0].text == "Thinking..."
    assert adapter.updates[-1].text == "engine handled"


@pytest.mark.asyncio
async def test_linkedin_operator_language_prefetches_browserops_context():
    async def fake_browserops(adapter, incoming, args, *, collect_only=False):
        assert collect_only is True
        return "BrowserOps LinkedIn operator context loaded"

    manager = _build_manager()
    manager._commands["browserops"].handler = fake_browserops
    engine = _RecordingEngine()
    router = ChatRouter(engine, manager)
    adapter = _RecordingAdapter()
    text = "I want to work on my LinkedIn account and build content"

    await router._handle_inner(adapter, _incoming(text))

    assert engine.messages == [text]
    assert "BrowserOps LinkedIn operator context loaded" in engine.prefetched_contexts[0]
    assert adapter.sent[0].text == "Thinking..."
    assert adapter.updates[-1].text == "engine handled"


@pytest.mark.asyncio
async def test_explicit_slash_commands_bypass_natural_language_gates():
    engine = _RecordingEngine()
    router = ChatRouter(engine, _SlashOnlyManager())  # type: ignore[arg-type]
    adapter = _RecordingAdapter()

    await router._handle_inner(adapter, _incoming("/send draft-01"))

    assert engine.messages == []
    assert adapter.sent[0].text == "slash command handled"
