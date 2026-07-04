from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

import background_tasks
import router as router_module
from models import Attachment, Channel, IncomingMessage, OutgoingMessage, Platform, User
from router import ChatRouter
from session import SQLiteSessionStore


class _SlowEngine:
    def __init__(self, session_store=None) -> None:
        self.session_store = session_store

    async def handle_message(self, incoming: IncomingMessage, progress: dict[str, Any]):
        await asyncio.sleep(60)
        yield OutgoingMessage(text="late", channel=incoming.channel, thread=incoming.thread)


class _CompletableEngine:
    def __init__(self, session_store=None) -> None:
        self.session_store = session_store
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def handle_message(self, incoming: IncomingMessage, progress: dict[str, Any]):
        self.started.set()
        await self.release.wait()
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


async def _wait_for_event_text(adapter: _CaptureAdapter, text: str) -> None:
    for _ in range(20):
        if any(text in event_text for _, event_text in adapter.events):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"event text not found: {text!r}")


@pytest.fixture(autouse=True)
def _isolate_background_task_state(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(
        background_tasks,
        "BACKGROUND_TASK_STATE_FILE",
        tmp_path / "background-engine-tasks.json",
    )


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
    engine = _CompletableEngine(store)
    router = ChatRouter(engine, _NoopManager())  # type: ignore[arg-type]

    await router._handle_inner(adapter, incoming)

    assert adapter.sent[0].text == "Thinking..."
    assert adapter.updates
    assert adapter.updates[-1].is_error is True
    assert "chat runtime timeout" in adapter.updates[-1].text
    assert "kept that turn running in the background" in adapter.updates[-1].text

    messages = store.list_messages("cli:test-channel:test-channel")
    assert [msg.role for msg in messages] == ["user", "assistant"]
    assert messages[0].content == "please do a slow thing"
    assert "chat runtime timeout" in messages[1].content

    engine.release.set()
    await _wait_for_event_text(adapter, "Background task finished")
    assert adapter.sent[-1].text == "Background task finished:\n\nlate"
    record = background_tasks.latest_for_session("cli:test-channel:test-channel")
    assert record is not None
    assert record["status"] == "completed"


@pytest.mark.asyncio
async def test_status_probe_answers_from_running_background_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    monkeypatch.setattr(router_module, "ENGINE_TIMEOUT_SECONDS", 0.01)

    store = SQLiteSessionStore(tmp_path / "chat.db")
    channel = Channel(platform=Platform.CLI, platform_id="test-channel")
    adapter = _CaptureAdapter()
    engine = _CompletableEngine(store)
    router = ChatRouter(engine, _NoopManager())  # type: ignore[arg-type]

    first = IncomingMessage(
        text="create individual clickable YourProduct prospect demo URLs",
        user=User(platform=Platform.CLI, platform_id="user-1"),
        channel=channel,
        platform=Platform.CLI,
    )
    await router._handle_inner(adapter, first)

    record = background_tasks.latest_for_session("cli:test-channel:test-channel")
    assert record is not None
    assert record["status"] == "running"

    status_ping = IncomingMessage(
        text="How we looking still cooking?",
        user=User(platform=Platform.CLI, platform_id="user-1"),
        channel=channel,
        platform=Platform.CLI,
    )
    await router._handle_inner(adapter, status_ping)

    assert "Still cooking" in adapter.sent[-1].text
    assert "individual clickable YourProduct prospect demo URLs" in adapter.sent[-1].text

    engine.release.set()
    await _wait_for_event_text(adapter, "Background task finished")


@pytest.mark.asyncio
async def test_engine_timeout_with_attachment_names_file_and_states_not_processed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    """Real router timeout path: an attachment turn that times out must name the
    file and state it was NOT processed — in the adapter update AND the
    persisted assistant row (the durable record the next turn reads)."""
    monkeypatch.setattr(router_module, "ENGINE_TIMEOUT_SECONDS", 0.01)

    store = SQLiteSessionStore(tmp_path / "chat.db")
    channel = Channel(platform=Platform.CLI, platform_id="test-channel")
    incoming = IncomingMessage(
        text="[Document received: transcript.txt] Please summarize it.",
        user=User(platform=Platform.CLI, platform_id="user-1"),
        channel=channel,
        platform=Platform.CLI,
        attachments=[Attachment(filename="transcript.txt")],
    )
    adapter = _CaptureAdapter()
    engine = _CompletableEngine(store)
    router = ChatRouter(engine, _NoopManager())  # type: ignore[arg-type]

    await router._handle_inner(adapter, incoming)

    assert adapter.sent[0].text == "Thinking..."
    assert adapter.updates
    final_update = adapter.updates[-1]
    assert final_update.is_error is True
    assert "transcript.txt" in final_update.text
    assert "not confirmed" in final_update.text
    assert "kept the turn running in the background" in final_update.text

    messages = store.list_messages("cli:test-channel:test-channel")
    assert [msg.role for msg in messages] == ["user", "assistant"]
    assert "transcript.txt" in messages[1].content
    assert "not confirmed" in messages[1].content

    engine.release.set()
    await _wait_for_event_text(adapter, "Background task finished")
    assert adapter.sent[-1].text == "Background task finished:\n\nlate"


def test_engine_timeout_uses_attachment_timeout_when_attachments_present(
    monkeypatch: pytest.MonkeyPatch,
):
    """Attachment turns resolve CHAT_ENGINE_ATTACHMENT_TIMEOUT_SECONDS at call
    time; plain turns keep CHAT_ENGINE_TIMEOUT_SECONDS; the module-level
    ENGINE_TIMEOUT_SECONDS override keeps ABSOLUTE precedence over both."""
    import config

    monkeypatch.setattr(router_module, "ENGINE_TIMEOUT_SECONDS", None)
    monkeypatch.setattr(config, "CHAT_ENGINE_ATTACHMENT_TIMEOUT_SECONDS", 555.0)
    monkeypatch.setattr(config, "CHAT_ENGINE_TIMEOUT_SECONDS", 111.0)

    assert router_module._engine_timeout_seconds(True) == 555.0
    assert router_module._engine_timeout_seconds(False) == 111.0
    assert router_module._engine_timeout_seconds() == 111.0

    # Module override wins over BOTH config values.
    monkeypatch.setattr(router_module, "ENGINE_TIMEOUT_SECONDS", 0.5)
    assert router_module._engine_timeout_seconds(True) == 0.5
    assert router_module._engine_timeout_seconds(False) == 0.5


def test_engine_timeout_message_caps_filename_list():
    """Supplementary unit test: filename list caps at 3 names + overflow count,
    and the not-confirmed fact lands within the 400-char recent-conversation clip."""
    attachments = [
        Attachment(filename="a.txt"),
        Attachment(filename="b.txt"),
        Attachment(filename="c.txt"),
        Attachment(filename="d.txt"),
    ]

    message = router_module._engine_timeout_message(180.0, attachments)

    assert "a.txt, b.txt, c.txt" in message
    assert "(+1 more)" in message
    assert "d.txt" not in message
    not_fact_pos = message.find("not confirmed")
    assert 0 <= not_fact_pos < 400


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
async def test_voice_origin_turn_skips_placeholder_and_delivers_final_via_send(tmp_path):
    """Voice turns must NOT get a "Thinking..." placeholder send.

    The placeholder would consume the adapter's one-shot voice-reply flag and
    speak "Thinking..." instead of the real answer (the Discord voice-reply
    integration bug). With voice_origin set, the first and only non-followup
    send() must be the final answer.
    """
    store = SQLiteSessionStore(tmp_path / "chat.db")
    channel = Channel(platform=Platform.CLI, platform_id="test-channel")
    incoming = IncomingMessage(
        text="transcribed voice question",
        user=User(platform=Platform.CLI, platform_id="user-1"),
        channel=channel,
        platform=Platform.CLI,
        voice_origin=True,
    )
    adapter = _CaptureAdapter()
    router = ChatRouter(_MultiYieldEngine(store), _NoopManager())  # type: ignore[arg-type]

    await router._handle_inner(adapter, incoming)

    assert all("Thinking..." not in text for _, text in adapter.events)
    assert adapter.updates == []  # no placeholder -> nothing to update
    assert adapter.events == [
        ("send", "real answer"),
        ("send", "follow-up nudge"),
    ]


def test_merge_incoming_batch_preserves_voice_origin():
    channel = Channel(platform=Platform.CLI, platform_id="test-channel")
    user = User(platform=Platform.CLI, platform_id="user-1")
    text_turn = IncomingMessage(
        text="first text message",
        user=user,
        channel=channel,
        platform=Platform.CLI,
    )
    voice_turn = IncomingMessage(
        text="transcribed voice message",
        user=user,
        channel=channel,
        platform=Platform.CLI,
        voice_origin=True,
    )

    merged = ChatRouter._merge_incoming_batch([text_turn, voice_turn])
    assert merged.voice_origin is True

    merged_text_only = ChatRouter._merge_incoming_batch(
        [
            IncomingMessage(
                text="a", user=user, channel=channel, platform=Platform.CLI
            ),
            IncomingMessage(
                text="b", user=user, channel=channel, platform=Platform.CLI
            ),
        ]
    )
    assert merged_text_only.voice_origin is False


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
