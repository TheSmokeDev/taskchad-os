from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

import background_tasks
import router as router_module
from adapters.base import ProgressCapabilities
from adapters.cli_adapter import CLIAdapter
from adapters.webhook import WebhookAdapter
from adapters.whatsapp import WhatsAppAdapter
from models import Attachment, Channel, IncomingMessage, OutgoingMessage, Platform, User
from router import ChatRouter, _progress_tool_label, _render_progress_status
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
        self.cancelled = asyncio.Event()

    async def handle_message(self, incoming: IncomingMessage, progress: dict[str, Any]):
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        yield OutgoingMessage(text="late", channel=incoming.channel, thread=incoming.thread)


class _MultiYieldEngine:
    def __init__(self, session_store=None) -> None:
        self.session_store = session_store

    async def handle_message(self, incoming: IncomingMessage, progress: dict[str, Any]):
        yield OutgoingMessage(text="real answer", channel=incoming.channel, thread=incoming.thread)
        yield OutgoingMessage(text="follow-up nudge", channel=incoming.channel, thread=incoming.thread)


class _DelayedAnswerEngine:
    def __init__(self, delay: float = 0.03, session_store=None) -> None:
        self.delay = delay
        self.session_store = session_store

    async def handle_message(
        self,
        incoming: IncomingMessage,
        progress: dict[str, Any],
    ):
        await asyncio.sleep(self.delay)
        yield OutgoingMessage(
            text="real answer",
            channel=incoming.channel,
            thread=incoming.thread,
        )


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
    progress_capabilities = ProgressCapabilities(
        enabled=True,
        typing=True,
        editable=True,
        recover_failed_status=True,
    )

    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []
        self.updates: list[OutgoingMessage] = []
        self.events: list[tuple[str, str]] = []
        self.typing_calls = 0

    async def send(self, message: OutgoingMessage) -> str:
        self.sent.append(message)
        self.events.append(("send", message.text))
        return f"sent-{len(self.sent)}"

    async def update(self, message: OutgoingMessage) -> str:
        self.updates.append(message)
        self.events.append(("update", message.text))
        return message.update_message_id or f"updated-{len(self.updates)}"

    async def send_typing(self, channel: Channel) -> None:
        self.typing_calls += 1


class _TransientPlaceholderAdapter(_CaptureAdapter):
    platform = Platform.DISCORD

    def __init__(self, failures: int = 1) -> None:
        super().__init__()
        self.failures = failures

    async def send(self, message: OutgoingMessage) -> str:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("transient Discord 503")
        return await super().send(message)


class _NullFinalUpdateAdapter(_CaptureAdapter):
    platform = Platform.DISCORD

    async def update(self, message: OutgoingMessage) -> None:
        self.updates.append(message)
        self.events.append(("update", message.text))
        return None


class _NoIdAdapter(_CaptureAdapter):
    """Successful non-editing adapter whose sends intentionally have no ID."""

    progress_capabilities = ProgressCapabilities()

    async def send(self, message: OutgoingMessage) -> None:
        self.sent.append(message)
        self.events.append(("send", message.text))
        return None

    async def update(self, message: OutgoingMessage) -> None:
        raise AssertionError("a no-ID CLI turn must not start the edit ticker")


class _FailingFinalUpdateAdapter(_CaptureAdapter):
    async def update(self, message: OutgoingMessage) -> str:
        self.updates.append(message)
        self.events.append(("update", message.text))
        raise RuntimeError("final delivery failed")


class _FailingFinalUpdateAndSendAdapter(_FailingFinalUpdateAdapter):
    async def send(self, message: OutgoingMessage) -> str:
        if message.text == "real answer":
            raise RuntimeError("fresh final delivery failed")
        return await super().send(message)


class _HangingInitialProgressAdapter(_CaptureAdapter):
    async def send(self, message: OutgoingMessage) -> str:
        if message.text == "Thinking...":
            await asyncio.sleep(60)
        return await super().send(message)


class _HangingFinalUpdateAdapter(_CaptureAdapter):
    async def update(self, message: OutgoingMessage) -> str:
        if message.text == "real answer":
            await asyncio.sleep(60)
        return await super().update(message)


class _FailingTypingAdapter(_CaptureAdapter):
    async def send_typing(self, channel: Channel) -> None:
        self.typing_calls += 1
        raise RuntimeError("typing unavailable")


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
    monkeypatch.setattr(router_module, "PROGRESS_UPDATE_SECONDS", 0.005)

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

    event_count = len(adapter.events)
    typing_count = adapter.typing_calls
    await asyncio.sleep(0.02)
    assert len(adapter.events) == event_count
    assert adapter.typing_calls == typing_count

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
async def test_failed_placeholder_recovers_progress_and_keeps_typing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    monkeypatch.setattr(router_module, "PROGRESS_RECOVERY_RETRY_SECONDS", 0.005)
    monkeypatch.setattr(router_module, "PROGRESS_UPDATE_SECONDS", 0.01)
    store = SQLiteSessionStore(tmp_path / "chat.db")
    channel = Channel(platform=Platform.DISCORD, platform_id="test-channel")
    incoming = IncomingMessage(
        text="take long enough to show progress",
        user=User(platform=Platform.DISCORD, platform_id="user-1"),
        channel=channel,
        platform=Platform.DISCORD,
    )
    adapter = _TransientPlaceholderAdapter()
    router = ChatRouter(  # type: ignore[arg-type]
        _DelayedAnswerEngine(session_store=store),
        _NoopManager(),
    )

    await router._handle_inner(adapter, incoming)

    progress_sends = [
        text for event, text in adapter.events
        if event == "send" and text.startswith("⏳ Homie is reasoning")
    ]
    assert progress_sends, "the ticker must replace a failed Thinking message"
    assert adapter.typing_calls >= 2
    assert adapter.updates[-1].text == "real answer"


@pytest.mark.asyncio
async def test_failed_placeholder_with_fast_answer_still_sends_final(tmp_path):
    store = SQLiteSessionStore(tmp_path / "chat.db")
    channel = Channel(platform=Platform.DISCORD, platform_id="test-channel")
    incoming = IncomingMessage(
        text="answer fast",
        user=User(platform=Platform.DISCORD, platform_id="user-1"),
        channel=channel,
        platform=Platform.DISCORD,
    )
    adapter = _TransientPlaceholderAdapter()
    router = ChatRouter(  # type: ignore[arg-type]
        _DelayedAnswerEngine(delay=0, session_store=store),
        _NoopManager(),
    )

    await router._handle_inner(adapter, incoming)

    assert [message.text for message in adapter.sent] == ["real answer"]
    assert adapter.updates == []


@pytest.mark.asyncio
async def test_null_final_edit_falls_back_to_fresh_final_send(tmp_path):
    store = SQLiteSessionStore(tmp_path / "chat.db")
    channel = Channel(platform=Platform.DISCORD, platform_id="test-channel")
    incoming = IncomingMessage(
        text="give me the answer",
        user=User(platform=Platform.DISCORD, platform_id="user-1"),
        channel=channel,
        platform=Platform.DISCORD,
    )
    adapter = _NullFinalUpdateAdapter()
    router = ChatRouter(  # type: ignore[arg-type]
        _DelayedAnswerEngine(delay=0, session_store=store),
        _NoopManager(),
    )

    await router._handle_inner(adapter, incoming)

    assert [message.text for message in adapter.sent] == ["Thinking...", "real answer"]
    assert adapter.updates[-1].text == "real answer"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capabilities",
    [
        pytest.param(CLIAdapter.progress_capabilities, id="cli"),
        pytest.param(WhatsAppAdapter.progress_capabilities, id="whatsapp"),
        pytest.param(WebhookAdapter.progress_capabilities, id="webhook"),
        pytest.param(object(), id="unknown-adapter"),
    ],
)
async def test_disabled_progress_adapter_emits_only_the_final_answer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    capabilities: object,
):
    monkeypatch.setattr(router_module, "PROGRESS_RECOVERY_RETRY_SECONDS", 0.005)
    monkeypatch.setattr(router_module, "PROGRESS_UPDATE_SECONDS", 0.005)
    store = SQLiteSessionStore(tmp_path / "chat.db")
    channel = Channel(platform=Platform.CLI, platform_id="test-channel")
    incoming = IncomingMessage(
        text="a normal CLI turn",
        user=User(platform=Platform.CLI, platform_id="user-1"),
        channel=channel,
        platform=Platform.CLI,
    )
    adapter = _NoIdAdapter()
    adapter.progress_capabilities = capabilities  # type: ignore[assignment]
    router = ChatRouter(  # type: ignore[arg-type]
        _DelayedAnswerEngine(session_store=store),
        _NoopManager(),
    )

    await router._handle_inner(adapter, incoming)

    assert [message.text for message in adapter.sent] == ["real answer"]
    assert adapter.typing_calls == 0


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
async def test_final_edit_exception_falls_back_to_fresh_send_and_followup(tmp_path):
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
    assert adapter.events == [
        ("send", "Thinking..."),
        ("update", "real answer"),
        ("send", "real answer"),
        ("send", "follow-up nudge"),
    ]


@pytest.mark.asyncio
async def test_final_edit_and_fallback_failure_suppresses_followup(tmp_path):
    store = SQLiteSessionStore(tmp_path / "chat.db")
    channel = Channel(platform=Platform.CLI, platform_id="test-channel")
    incoming = IncomingMessage(
        text="tell me what you know",
        user=User(platform=Platform.CLI, platform_id="user-1"),
        channel=channel,
        platform=Platform.CLI,
    )
    adapter = _FailingFinalUpdateAndSendAdapter()
    router = ChatRouter(_MultiYieldEngine(store), _NoopManager())  # type: ignore[arg-type]

    await router._handle_inner(adapter, incoming)

    assert all(msg.text != "follow-up nudge" for msg in adapter.sent)
    assert adapter.sent[-1].is_error is True
    assert "delivery failed" in adapter.sent[-1].text
    assert adapter.events == [
        ("send", "Thinking..."),
        ("update", "real answer"),
        (
            "send",
            "I generated a response, but delivery failed before it "
            "could be shown. I suppressed follow-up nudges for this turn.",
        ),
    ]


@pytest.mark.asyncio
async def test_initial_progress_io_is_bounded(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr(router_module, "PROGRESS_IO_TIMEOUT_SECONDS", 0.005)
    store = SQLiteSessionStore(tmp_path / "chat.db")
    channel = Channel(platform=Platform.CLI, platform_id="test-channel")
    incoming = IncomingMessage(
        text="answer even if progress hangs",
        user=User(platform=Platform.CLI, platform_id="user-1"),
        channel=channel,
        platform=Platform.CLI,
    )
    adapter = _HangingInitialProgressAdapter()
    router = ChatRouter(  # type: ignore[arg-type]
        _DelayedAnswerEngine(delay=0, session_store=store),
        _NoopManager(),
    )

    await asyncio.wait_for(router._handle_inner(adapter, incoming), timeout=0.2)

    assert [message.text for message in adapter.sent] == ["real answer"]


@pytest.mark.asyncio
async def test_final_progress_edit_is_bounded_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    monkeypatch.setattr(router_module, "PROGRESS_IO_TIMEOUT_SECONDS", 0.005)
    store = SQLiteSessionStore(tmp_path / "chat.db")
    channel = Channel(platform=Platform.CLI, platform_id="test-channel")
    incoming = IncomingMessage(
        text="answer even if the final edit hangs",
        user=User(platform=Platform.CLI, platform_id="user-1"),
        channel=channel,
        platform=Platform.CLI,
    )
    adapter = _HangingFinalUpdateAdapter()
    router = ChatRouter(  # type: ignore[arg-type]
        _DelayedAnswerEngine(delay=0, session_store=store),
        _NoopManager(),
    )

    await asyncio.wait_for(router._handle_inner(adapter, incoming), timeout=0.2)

    assert [message.text for message in adapter.sent] == ["Thinking...", "real answer"]


@pytest.mark.asyncio
async def test_typing_failure_does_not_disable_editable_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    monkeypatch.setattr(router_module, "PROGRESS_UPDATE_SECONDS", 0.005)
    store = SQLiteSessionStore(tmp_path / "chat.db")
    channel = Channel(platform=Platform.CLI, platform_id="test-channel")
    incoming = IncomingMessage(
        text="keep showing truthful status",
        user=User(platform=Platform.CLI, platform_id="user-1"),
        channel=channel,
        platform=Platform.CLI,
    )
    adapter = _FailingTypingAdapter()
    router = ChatRouter(  # type: ignore[arg-type]
        _DelayedAnswerEngine(delay=0.02, session_store=store),
        _NoopManager(),
    )

    await router._handle_inner(adapter, incoming)

    assert adapter.typing_calls >= 2
    assert any(
        message.text.startswith("⏳ Homie is reasoning")
        for message in adapter.updates
    )
    assert adapter.updates[-1].text == "real answer"


@pytest.mark.asyncio
async def test_progress_ticker_is_cancelled_after_final_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    monkeypatch.setattr(router_module, "PROGRESS_UPDATE_SECONDS", 0.005)
    store = SQLiteSessionStore(tmp_path / "chat.db")
    channel = Channel(platform=Platform.CLI, platform_id="test-channel")
    incoming = IncomingMessage(
        text="finish cleanly",
        user=User(platform=Platform.CLI, platform_id="user-1"),
        channel=channel,
        platform=Platform.CLI,
    )
    adapter = _CaptureAdapter()
    router = ChatRouter(  # type: ignore[arg-type]
        _DelayedAnswerEngine(delay=0.015, session_store=store),
        _NoopManager(),
    )

    await router._handle_inner(adapter, incoming)
    event_count = len(adapter.events)
    typing_count = adapter.typing_calls
    await asyncio.sleep(0.02)

    assert len(adapter.events) == event_count
    assert adapter.typing_calls == typing_count


@pytest.mark.asyncio
async def test_external_shutdown_cancels_engine_and_progress_ticker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    monkeypatch.setattr(router_module, "PROGRESS_UPDATE_SECONDS", 0.005)
    store = SQLiteSessionStore(tmp_path / "chat.db")
    channel = Channel(platform=Platform.CLI, platform_id="test-channel")
    incoming = IncomingMessage(
        text="stay busy until shutdown",
        user=User(platform=Platform.CLI, platform_id="user-1"),
        channel=channel,
        platform=Platform.CLI,
    )
    engine = _CompletableEngine(store)
    adapter = _CaptureAdapter()
    router = ChatRouter(engine, _NoopManager())  # type: ignore[arg-type]

    turn = asyncio.create_task(router._handle_inner(adapter, incoming))
    await asyncio.wait_for(engine.started.wait(), timeout=0.2)
    await _wait_for_event_text(adapter, "Homie is reasoning")
    turn.cancel()

    with pytest.raises(asyncio.CancelledError):
        await turn
    await asyncio.wait_for(engine.cancelled.wait(), timeout=0.2)
    event_count = len(adapter.events)
    typing_count = adapter.typing_calls
    await asyncio.sleep(0.02)

    assert len(adapter.events) == event_count
    assert adapter.typing_calls == typing_count


def test_progress_tool_labels_never_echo_unknown_names_or_arguments() -> None:
    secretish = "custom_tool_C_Users_YourUser_client-secret --token abc123"

    assert _progress_tool_label(secretish) == "Using a tool"
    assert _progress_tool_label("read_file") == "Reading files"
    assert _progress_tool_label("mcp__crm__lookup") == "Using an integration"
    rendered = _render_progress_status(
        {
            "started": 0,
            "current_tool": secretish,
            "tool_calls": 1,
        }
    )
    assert "client" not in rendered.lower()
    assert "token" not in rendered.lower()
    assert "abc123" not in rendered
