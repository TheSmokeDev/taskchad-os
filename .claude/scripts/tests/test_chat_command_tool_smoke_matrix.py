from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
_REPO_ROOT = _SCRIPTS_DIR.parent.parent
sys.path.insert(0, str(_CHAT_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR))

from commands import COMMANDS, CORE_INTENTS, TELEGRAM_NATIVE_COMMANDS  # noqa: E402
from core_handlers import CORE_HANDLERS  # noqa: E402
from extension_manager import ExtensionManager  # noqa: E402
from models import Channel, IncomingMessage, OutgoingMessage, Platform, User  # noqa: E402
from router import ChatRouter  # noqa: E402


def _build_manager() -> ExtensionManager:
    manager = ExtensionManager()
    manager.register_core_commands(COMMANDS, [], CORE_HANDLERS)
    manager.register_core_intents(CORE_INTENTS)
    return manager


def _incoming(text: str) -> IncomingMessage:
    return IncomingMessage(
        text=text,
        user=User(Platform.CLI, "cli-user", "Tester"),
        channel=Channel(Platform.CLI, "cli-test", is_dm=True),
        platform=Platform.CLI,
    )


def _command_type(name: str) -> str:
    for command_name, _description, command_type, _min_role in COMMANDS:
        if command_name == name:
            return command_type
    raise AssertionError(f"command not registered: {name}")


class _RecordingAdapter:
    platform = Platform.CLI

    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []
        self.updates: list[OutgoingMessage] = []

    async def send(self, message: OutgoingMessage) -> str:
        self.sent.append(message)
        return "placeholder-1"

    async def update(self, message: OutgoingMessage) -> str:
        self.updates.append(message)
        return "updated-1"


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


def test_issue_39_matrix_artifact_covers_required_surfaces() -> None:
    matrix = (
        _REPO_ROOT
        / ".archon"
        / "artifacts"
        / "issues-39-37-38"
        / "issue-39-smoke-matrix.md"
    ).read_text(encoding="utf-8")

    required = [
        "CLI-01",
        "CLI-02",
        "CLI-03",
        "CLI-04",
        "CLI-05",
        "CLI-06",
        "CLI-07",
        "CLI-08",
        "Telegram-01",
        "Telegram-02",
        "Dashboard-01",
        "Dashboard-02",
        "Dashboard-03",
        "Dashboard-04",
        "Dashboard-05",
        "Desktop-01",
        "Desktop-02",
        "Cabinet LiveKit mic readiness is not part of this matrix",
    ]
    for item in required:
        assert item in matrix


def test_issue_39_router_engine_split_is_explicit() -> None:
    expected_types = {
        "provider": "router",
        "browser": "router",
        "browserops": "router",
        "linkedin_profile": "router",
        "teamroom": "router",
        "linkedin": "engine",
    }

    for command, expected_type in expected_types.items():
        assert _command_type(command) == expected_type

    assert "linkedin" in TELEGRAM_NATIVE_COMMANDS
    assert "linkedin_profile" in TELEGRAM_NATIVE_COMMANDS


@pytest.mark.asyncio
async def test_issue_39_natural_language_check_uses_linkedin_status_router_path() -> None:
    async def fake_linkedin_profile(adapter, incoming, args, *, collect_only=False):
        assert collect_only is True
        assert args == ""
        return "LinkedIn Browser Status\nCDP: reachable"

    manager = _build_manager()
    manager._commands["linkedin_profile"].handler = fake_linkedin_profile
    engine = _RecordingEngine()
    router = ChatRouter(engine, manager)
    adapter = _RecordingAdapter()

    await router._handle_inner(adapter, _incoming("check my LinkedIn profile"))

    assert engine.messages == []
    assert adapter.sent[-1].text == "LinkedIn Browser Status\nCDP: reachable"


@pytest.mark.asyncio
async def test_issue_39_linkedin_draft_slash_command_reaches_engine_as_draft_only() -> None:
    engine = _RecordingEngine()
    router = ChatRouter(engine, _build_manager())
    adapter = _RecordingAdapter()
    incoming = _incoming("/linkedin draft a post about AI operators. Do not post.")

    await router._handle_inner(adapter, incoming)

    assert len(engine.messages) == 1
    prompt = engine.messages[0]
    assert "LinkedIn/Social Homie" in prompt
    assert "draft a post about AI operators. Do not post." in prompt
    assert "must not" in prompt
    assert "publish, DM, edit a profile" in prompt
    assert incoming.raw_event["display_text"] == (
        "/linkedin draft a post about AI operators. Do not post."
    )


@pytest.mark.asyncio
async def test_issue_39_browserops_prefetch_still_reaches_engine_for_browser_strategy() -> None:
    async def fake_browserops(adapter, incoming, args, *, collect_only=False):
        assert collect_only is True
        return "BrowserOps context loaded"

    manager = _build_manager()
    manager._commands["browserops"].handler = fake_browserops
    engine = _RecordingEngine()
    router = ChatRouter(engine, manager)
    adapter = _RecordingAdapter()

    await router._handle_inner(adapter, _incoming("open up your browser and go to LinkedIn"))

    assert engine.messages == ["open up your browser and go to LinkedIn"]
    assert engine.prefetched_contexts == ["## /browserops\nBrowserOps context loaded"]
    assert adapter.sent[0].text == "Thinking..."
    assert adapter.updates[-1].text == "engine handled"


@pytest.mark.asyncio
async def test_issue_39_in_flight_buttons_remain_queue_and_steer() -> None:
    manager = _build_manager()
    engine = _RecordingEngine()
    router = ChatRouter(engine, manager)
    adapter = _RecordingAdapter()
    incoming = _incoming("follow-up while thinking")

    await router._offer_turn_followup_choice(adapter, incoming, "cli:cli-test:cli-test")

    assert [component.label for component in adapter.sent[-1].components] == [
        "Queue Next",
        "Steer Current",
    ]
    assert [component.custom_id.split(":", 1)[0] for component in adapter.sent[-1].components] == [
        "turn_queue",
        "turn_steer",
    ]
