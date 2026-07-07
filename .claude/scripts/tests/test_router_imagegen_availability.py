"""Covers the imagegen dangling-import fix.

``imagegen_workflow.py`` is sanitizer-denylisted (tenant-specific, never
ships publicly), but router.py — which does ship publicly — used to hard
top-level-import it, crashing every public taskchad-os install on startup.
The fix guards the import behind ``router._IMAGEGEN_AVAILABLE`` and degrades
the image commands to a plain reply instead of crashing when it's absent.
"""

from __future__ import annotations

import re
from datetime import datetime

import pytest

import router as router_module
from models import Channel, IncomingMessage, Platform, User
from router import ChatRouter


class _RecordingAdapter:
    platform = Platform.CLI

    def __init__(self) -> None:
        self.sent = []

    async def send(self, message):
        self.sent.append(message)
        return None

    async def update(self, message):
        self.sent.append(message)


class _ImageCommandManager:
    command_regex = re.compile(r"^/(\w+)\b(.*)$")

    def __init__(self):
        self.dispatched = []

    def get_router_commands(self):
        return set()

    def get_all_command_names(self):
        return ["image", "generate-image", "owner-image"]

    async def dispatch(self, command, adapter, incoming, args, collect_only=False):
        self.dispatched.append((command, args, collect_only))
        return None

    def detect_intents(self, text):
        return []

    def wants_analysis(self, text):
        return False


class _FakeEngine:
    def __init__(self, store=None):
        self.session_store = store
        self.messages = []

    async def handle_message(self, message, progress=None):
        self.messages.append(message)
        if False:
            yield None


def _incoming(text: str) -> IncomingMessage:
    return IncomingMessage(
        text=text,
        user=User(Platform.CLI, "cli-user", "User"),
        channel=Channel(Platform.CLI, "cli-test", is_dm=True),
        platform=Platform.CLI,
        timestamp=datetime.now(),
    )


@pytest.mark.asyncio
async def test_image_command_degrades_gracefully_when_imagegen_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    """Simulates a public taskchad-os install: imagegen_workflow.py absent."""
    monkeypatch.setattr(router_module, "_IMAGEGEN_AVAILABLE", False)
    router = ChatRouter(_FakeEngine(), _ImageCommandManager())
    adapter = _RecordingAdapter()
    incoming = _incoming("/image a red bicycle")

    await router._handle(adapter, incoming)

    assert len(adapter.sent) == 1
    assert adapter.sent[0].text == "Image generation isn't included in this build."
    # Never reached the engine — no crash, no dangling piv dispatch.
    assert router.engine.messages == []
    assert incoming.is_piv is False


@pytest.mark.asyncio
async def test_owner_image_command_degrades_gracefully_when_imagegen_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(router_module, "_IMAGEGEN_AVAILABLE", False)
    router = ChatRouter(_FakeEngine(), _ImageCommandManager())
    adapter = _RecordingAdapter()
    incoming = _incoming("/owner-image waving at the camera")

    await router._handle(adapter, incoming)

    assert len(adapter.sent) == 1
    assert adapter.sent[0].text == "Image generation isn't included in this build."
    assert router.engine.messages == []


@pytest.mark.asyncio
async def test_image_command_still_routes_to_engine_when_imagegen_available(
    monkeypatch: pytest.MonkeyPatch,
):
    """Unchanged behavior for private deployments that carry imagegen_workflow.py."""
    monkeypatch.setattr(router_module, "_IMAGEGEN_AVAILABLE", True)
    router = ChatRouter(_FakeEngine(), _ImageCommandManager())
    adapter = _RecordingAdapter()
    incoming = _incoming("/image a red bicycle")

    await router._handle(adapter, incoming)

    assert len(router.engine.messages) == 1
    routed = router.engine.messages[0]
    assert routed.is_piv is True
    assert routed.piv_command == "imagegen"
    assert "red bicycle" in routed.text
