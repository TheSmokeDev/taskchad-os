"""Phase 5 — rich messaging-surface polish (Hermes port).

Covers the four shipped behaviors:
  (a) live tool-activity streaming — describe_tool_event mapping, the shared
      format contract, the engine seam (progress["activity"] + notify hook),
      and TurnProgressReporter edit throttling / trailing flush / close.
  (e) interrupt parity — Stop button on the progress placeholder wired to
      router.cancel_active_turn (same primitive as the dashboard /stop
      endpoint), the immediate-dispatch /stop path, and core_handlers'
      /stop command (channel-grain scope: one chat can't stop another's).
  (b) Telegram formatting robustness — plain-edit retry before the
      duplicate-message fallback, reply_markup re-attachment on edits.
  (c) Discord code-fence-aware chunking.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import core_handlers
import engine as engine_module
import pytest
from engine import ConversationEngine
from models import (
    Channel,
    IncomingMessage,
    MessageComponent,
    OutgoingMessage,
    Platform,
    Thread,
    User,
)
from router import ChatRouter
from session import SQLiteSessionStore
from turn_activity import (
    STOP_CUSTOM_ID_PREFIX,
    TurnProgressReporter,
    build_stop_components,
    describe_tool_event,
    format_progress_status,
)

from runtime.base import RUNTIME_LANE_CLAUDE_NATIVE, RuntimeResult

# ── (a1) describe_tool_event — activity label contract ─────────────────────


def test_describe_read_uses_file_basename_never_full_path() -> None:
    preview = "{'file_path': '~\\\\vault\\\\budget.md'}"
    label = describe_tool_event({"id": "t1", "name": "Read", "input_preview": preview})
    assert label == "Reading budget.md"
    assert "Users" not in label  # progress edits never echo local paths


def test_describe_grep_uses_pattern() -> None:
    label = describe_tool_event(
        {"name": "Grep", "input_preview": "{'pattern': 'run_with_fallback', 'path': 'x'}"}
    )
    assert label == "Searching run_with_fallback"


def test_describe_bash_uses_command() -> None:
    label = describe_tool_event(
        {"name": "Bash", "input_preview": "{'command': 'git status'}"}
    )
    assert label == "Running git status"


def test_describe_unknown_tool_falls_back_to_using() -> None:
    assert describe_tool_event({"name": "FrobnicateTool", "input_preview": "{}"}) == (
        "Using FrobnicateTool"
    )


def test_describe_clips_long_detail() -> None:
    long_cmd = "x" * 300
    label = describe_tool_event(
        {"name": "Bash", "input_preview": f"{{'command': '{long_cmd}'}}"}
    )
    assert label.startswith("Running ")
    assert len(label) <= len("Running ") + 48
    assert label.endswith("…")


def test_describe_is_fail_open() -> None:
    assert describe_tool_event({}) == ""
    assert describe_tool_event({"name": None}) == ""
    assert describe_tool_event({"name": "Read", "input_preview": None}) == "Reading"


def test_format_progress_status_contract() -> None:
    # MUST keep the "Working..." prefix — the Telegram voice branch keys on it.
    bare = format_progress_status(9, 0)
    assert bare == "Working... (9s)"
    with_tools = format_progress_status(32, 3, "Reading budget.md")
    assert with_tools.startswith("Working... (32s) | 3 tool calls")
    assert "\n🔧 Reading budget.md" in with_tools


# ── (a2 / e1) stop components + config knobs (Rule 1 call-time) ────────────


def test_build_stop_components_default_on() -> None:
    components = build_stop_components()
    assert len(components) == 1
    comp = components[0]
    assert comp.custom_id == f"{STOP_CUSTOM_ID_PREFIX}turn"
    assert len(comp.custom_id.encode("utf-8")) <= 64  # Telegram callback_data cap
    assert comp.style == "danger"


def test_build_stop_components_env_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TURN_PROGRESS_STOP_BUTTON_ENABLED", "false")
    assert build_stop_components() == []


def test_turn_progress_settings_env_overrides_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config import get_turn_progress_settings

    defaults = get_turn_progress_settings()
    assert defaults.activity_enabled is True
    assert defaults.edit_min_interval_s == 1.5
    assert defaults.stop_button_enabled is True

    monkeypatch.setenv("TURN_PROGRESS_ACTIVITY_ENABLED", "false")
    monkeypatch.setenv("TURN_PROGRESS_EDIT_MIN_INTERVAL_S", "3.25")
    monkeypatch.setenv("TURN_PROGRESS_STOP_BUTTON_ENABLED", "false")
    overridden = get_turn_progress_settings()
    assert overridden.activity_enabled is False
    assert overridden.edit_min_interval_s == 3.25
    assert overridden.stop_button_enabled is False

    monkeypatch.setenv("TURN_PROGRESS_EDIT_MIN_INTERVAL_S", "-4")
    assert get_turn_progress_settings().edit_min_interval_s == 0.0
    monkeypatch.setenv("TURN_PROGRESS_EDIT_MIN_INTERVAL_S", "junk")
    assert get_turn_progress_settings().edit_min_interval_s == 1.5


# ── (a3) TurnProgressReporter — throttle, flush, close ─────────────────────


class _RecordingAdapter:
    platform = Platform.CLI

    def __init__(self, fail_update: bool = False) -> None:
        self.sent: list[OutgoingMessage] = []
        self.updates: list[OutgoingMessage] = []
        self.fail_update = fail_update

    async def send(self, message: OutgoingMessage) -> str:
        self.sent.append(message)
        return f"sent-{len(self.sent)}"

    async def update(self, message: OutgoingMessage) -> str:
        if self.fail_update:
            raise RuntimeError("edit boom")
        self.updates.append(message)
        return message.update_message_id or "updated"


def _reporter(adapter: Any, progress: dict[str, Any], **kwargs: Any) -> TurnProgressReporter:
    return TurnProgressReporter(
        adapter,
        channel=Channel(platform=Platform.CLI, platform_id="chan-1"),
        thread=Thread(thread_id="chan-1"),
        placeholder_id="ph-1",
        progress=progress,
        components=build_stop_components(),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_reporter_throttles_edit_burst_and_flushes_trailing_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TURN_PROGRESS_EDIT_MIN_INTERVAL_S", "0.08")
    adapter = _RecordingAdapter()
    progress: dict[str, Any] = {"tool_calls": 0, "started": 0.0}
    reporter = _reporter(adapter, progress)
    reporter.install()
    assert callable(progress["notify_activity"])

    # Burst of three tool events — first edits immediately, the rest fold
    # into ONE trailing flush carrying the LATEST activity.
    progress["tool_calls"] = 1
    progress["activity"] = "Reading a.md"
    progress["notify_activity"]("Reading a.md")
    progress["tool_calls"] = 2
    progress["activity"] = "Reading b.md"
    progress["notify_activity"]("Reading b.md")
    progress["tool_calls"] = 3
    progress["activity"] = "Searching memory"
    progress["notify_activity"]("Searching memory")

    await asyncio.sleep(0.02)
    assert len(adapter.updates) == 1  # burst throttled
    first = adapter.updates[0]
    assert first.is_update and first.update_message_id == "ph-1"
    assert first.text.startswith("Working...")
    assert [c.custom_id for c in first.components] == [f"{STOP_CUSTOM_ID_PREFIX}turn"]

    await asyncio.sleep(0.15)  # trailing flush lands after the interval
    assert len(adapter.updates) == 2
    assert "🔧 Searching memory" in adapter.updates[-1].text
    assert "3 tool calls" in adapter.updates[-1].text
    reporter.close()


@pytest.mark.asyncio
async def test_reporter_close_stops_edits_and_unbinds_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TURN_PROGRESS_EDIT_MIN_INTERVAL_S", "0.01")
    adapter = _RecordingAdapter()
    progress: dict[str, Any] = {"tool_calls": 0, "started": 0.0}
    reporter = _reporter(adapter, progress)
    reporter.install()
    reporter.close()
    assert "notify_activity" not in progress  # engine hook unbound
    reporter.notify_activity("Reading late.md")
    await asyncio.sleep(0.05)
    assert adapter.updates == []  # no edit can land after close


@pytest.mark.asyncio
async def test_reporter_edit_failure_is_fail_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TURN_PROGRESS_EDIT_MIN_INTERVAL_S", "0.01")
    adapter = _RecordingAdapter(fail_update=True)
    progress: dict[str, Any] = {"tool_calls": 1, "started": 0.0}
    reporter = _reporter(adapter, progress)
    reporter.install()
    progress["notify_activity"]("Reading a.md")  # must not raise
    await asyncio.sleep(0.05)
    reporter.close()


# ── (a4) engine seam — progress["activity"] + notify hook ──────────────────


def _make_message(text: str = "Need a summary") -> IncomingMessage:
    return IncomingMessage(
        text=text,
        user=User(platform=Platform.WEB, platform_id="user-1", display_name="YourUser"),
        channel=Channel(platform=Platform.WEB, platform_id="dashboard-main", is_dm=True),
        platform=Platform.WEB,
        thread=Thread(thread_id="dashboard-main"),
    )


def _make_project_root(tmp_path) -> Any:
    project_root = tmp_path / "project"
    (project_root / "TheHomie" / "Memory" / "daily").mkdir(parents=True)
    return project_root


@pytest.mark.asyncio
async def test_engine_tool_event_sets_activity_and_notifies(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    convo = ConversationEngine(store, _make_project_root(tmp_path))
    notified: list[str] = []
    progress: dict[str, Any] = {"tool_calls": 0, "notify_activity": notified.append}

    async def fake_run(request):
        request.on_tool_event(
            {"id": "t1", "name": "Read", "input_preview": "{'file_path': 'vault/budget.md'}"}
        )
        return RuntimeResult(
            text="ok", runtime_lane=RUNTIME_LANE_CLAUDE_NATIVE, provider="claude",
            model="m", profile_key="primary-claude",
        )

    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)

    outputs = [
        out async for out in convo.handle_message(_make_message(), progress=progress)
    ]
    assert outputs[-1].text == "ok"
    assert progress["activity"] == "Reading budget.md"
    assert notified == ["Reading budget.md"]
    assert progress["tool_calls"] == 1  # M7 counter unchanged


@pytest.mark.asyncio
async def test_engine_activity_kill_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("TURN_PROGRESS_ACTIVITY_ENABLED", "false")
    store = SQLiteSessionStore(tmp_path / "chat.db")
    convo = ConversationEngine(store, _make_project_root(tmp_path))
    notified: list[str] = []
    progress: dict[str, Any] = {"tool_calls": 0, "notify_activity": notified.append}

    async def fake_run(request):
        request.on_tool_event(
            {"id": "t1", "name": "Read", "input_preview": "{'file_path': 'a.md'}"}
        )
        return RuntimeResult(
            text="ok", runtime_lane=RUNTIME_LANE_CLAUDE_NATIVE, provider="claude",
            model="m", profile_key="primary-claude",
        )

    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)

    [out async for out in convo.handle_message(_make_message(), progress=progress)]
    assert "activity" not in progress
    assert notified == []
    assert progress["tool_calls"] == 1  # counting is NOT gated by the knob


# ── (e2) router — Stop button + /stop wiring ───────────────────────────────


class _NoopManager:
    def get_router_commands(self) -> dict[str, Any]:
        return {}

    def get_all_command_names(self) -> list[str]:
        return ["noop"]

    def detect_intents(self, text: str) -> list[str]:
        return []

    def wants_analysis(self, text: str) -> bool:
        return False


class _HangingEngine:
    def __init__(self, session_store=None) -> None:
        self.session_store = session_store
        self.started = asyncio.Event()

    async def handle_message(self, incoming: IncomingMessage, progress: dict[str, Any]):
        self.started.set()
        await asyncio.sleep(60)
        yield OutgoingMessage(text="late", channel=incoming.channel, thread=incoming.thread)


class _StopCaptureAdapter:
    platform = Platform.TELEGRAM

    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []
        self.updates: list[OutgoingMessage] = []

    async def send(self, message: OutgoingMessage) -> str:
        self.sent.append(message)
        return f"sent-{len(self.sent)}"

    async def update(self, message: OutgoingMessage) -> str:
        self.updates.append(message)
        return message.update_message_id or "updated"


def _tg_incoming(text: str, thread_id: str = "555") -> IncomingMessage:
    return IncomingMessage(
        text=text,
        user=User(platform=Platform.TELEGRAM, platform_id="42"),
        channel=Channel(platform=Platform.TELEGRAM, platform_id="555", is_dm=True),
        platform=Platform.TELEGRAM,
        thread=Thread(thread_id=thread_id),
    )


def test_turn_stop_button_is_immediate() -> None:
    assert ChatRouter._is_immediate_button(_tg_incoming("__button:turn_stop:turn"))
    # Existing immediates unaffected.
    assert ChatRouter._is_immediate_button(_tg_incoming("__button:turn_queue:x"))
    assert not ChatRouter._is_immediate_button(_tg_incoming("hello"))


@pytest.mark.asyncio
async def test_placeholder_carries_stop_button(tmp_path) -> None:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    engine = _HangingEngine(store)
    router = ChatRouter(engine, _NoopManager())  # type: ignore[arg-type]
    adapter = _StopCaptureAdapter()

    turn = asyncio.create_task(router._handle_inner(adapter, _tg_incoming("do it")))
    await asyncio.wait_for(engine.started.wait(), timeout=5)
    assert adapter.sent[0].text == "Thinking..."
    assert [c.custom_id for c in adapter.sent[0].components] == [
        f"{STOP_CUSTOM_ID_PREFIX}turn"
    ]
    router.cancel_active_turn("telegram:555:")
    await asyncio.wait_for(turn, timeout=5)


@pytest.mark.asyncio
async def test_stop_button_tap_cancels_own_turn_only(tmp_path) -> None:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    engine = _HangingEngine(store)
    router = ChatRouter(engine, _NoopManager())  # type: ignore[arg-type]
    adapter = _StopCaptureAdapter()

    turn = asyncio.create_task(router._handle_inner(adapter, _tg_incoming("long ask")))
    await asyncio.wait_for(engine.started.wait(), timeout=5)
    assert router._active_turns

    # A tap from a DIFFERENT chat cancels nothing (per-chat scope, Rule 4).
    other_tap = IncomingMessage(
        text="__button:turn_stop:turn",
        user=User(platform=Platform.TELEGRAM, platform_id="42"),
        channel=Channel(platform=Platform.TELEGRAM, platform_id="999", is_dm=True),
        platform=Platform.TELEGRAM,
        thread=Thread(thread_id="999"),
    )
    other_adapter = _StopCaptureAdapter()
    await router._handle(other_adapter, other_tap)
    assert router._active_turns  # still running
    assert other_adapter.sent[-1].text == "Nothing is running in this chat."

    # A tap from the SAME chat cancels it; placeholder becomes the stop marker.
    await router._handle(adapter, _tg_incoming("__button:turn_stop:turn"))
    await asyncio.wait_for(turn, timeout=5)
    assert not router._active_turns
    assert adapter.updates[-1].text == "⏹️ Stopped."
    assert adapter.updates[-1].is_error is False


@pytest.mark.asyncio
async def test_typed_stop_dispatches_immediately_not_serialized(tmp_path) -> None:
    """A serialized /stop would deadlock behind the thread lock held by the
    very turn it cancels — the router must dispatch it on the immediate path."""
    store = SQLiteSessionStore(tmp_path / "chat.db")
    router = ChatRouter(_HangingEngine(store), _NoopManager())  # type: ignore[arg-type]
    handled: list[str] = []

    async def _fake_handle(adapter: Any, incoming: Any) -> None:
        handled.append(("immediate", incoming.text))

    async def _fake_serialized(adapter: Any, incoming: Any) -> None:
        handled.append(("serialized", incoming.text))

    router._handle = _fake_handle  # type: ignore[method-assign]
    router._handle_serialized = _fake_serialized  # type: ignore[method-assign]

    router._queue_incoming(_StopCaptureAdapter(), _tg_incoming("/stop"))
    router._queue_incoming(_StopCaptureAdapter(), _tg_incoming("/clear"))
    await asyncio.sleep(0.05)
    assert ("immediate", "/stop") in handled
    assert ("serialized", "/clear") in handled


@pytest.mark.asyncio
async def test_handle_stop_command_uses_router_primitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeRouter:
        def __init__(self) -> None:
            self.prefixes: list[str] = []

        def _stop_scope_prefix(self, incoming: Any) -> str:
            return "telegram:555:"

        def cancel_active_turn(self, prefix: str) -> int:
            self.prefixes.append(prefix)
            return 1

    fake = _FakeRouter()
    monkeypatch.setitem(core_handlers._ctx, "router", fake)
    reply = await core_handlers.handle_stop(None, _tg_incoming("/stop"), "")
    assert fake.prefixes == ["telegram:555:"]
    assert "Stopping 1 in-flight turn" in reply

    fake.cancel_active_turn = lambda prefix: 0  # type: ignore[method-assign]
    reply = await core_handlers.handle_stop(None, _tg_incoming("/stop"), "")
    assert reply == "Nothing is running in this chat."


@pytest.mark.asyncio
async def test_handle_stop_without_router_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(core_handlers._ctx, "router", None)
    reply = await core_handlers.handle_stop(None, _tg_incoming("/stop"), "")
    assert "Stop is unavailable" in reply


def test_stop_registered_in_command_registry() -> None:
    from commands import COMMANDS, TELEGRAM_NATIVE_COMMANDS

    entries = [c for c in COMMANDS if c[0] == "stop"]
    assert len(entries) == 1
    assert entries[0][2] == "router"
    assert "stop" in TELEGRAM_NATIVE_COMMANDS
    assert "stop" in core_handlers.CORE_HANDLERS


# ── (b) Telegram — edit fallback chain + markup re-attachment ──────────────


class _FakeTgBot:
    def __init__(self, fail_markdown_edit: bool = False, fail_plain_edit: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail_markdown_edit = fail_markdown_edit
        self.fail_plain_edit = fail_plain_edit
        self._next_id = 100

    async def edit_message_text(self, **kwargs):
        self.calls.append(("edit_message_text", kwargs))
        if kwargs.get("parse_mode") == "Markdown" and self.fail_markdown_edit:
            raise RuntimeError("Can't parse entities: can't find end of the entity")
        if kwargs.get("parse_mode") is None and self.fail_plain_edit:
            raise RuntimeError("plain edit failed")
        return SimpleNamespace(message_id=kwargs["message_id"])

    async def send_message(self, **kwargs):
        self.calls.append(("send_message", kwargs))
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)


def _tg_adapter(bot: _FakeTgBot):
    import adapters.telegram as telegram_adapter

    adapter = telegram_adapter.TelegramAdapter.__new__(telegram_adapter.TelegramAdapter)
    adapter._app = SimpleNamespace(bot=bot)
    adapter._queue = asyncio.Queue()
    adapter.allowed_user_ids = []
    adapter._sent_messages = {}
    adapter._callback_id_map = {}
    adapter._voice_reply_threads = set()
    adapter._pending_document_groups = {}
    adapter._pending_document_tasks = {}
    adapter._document_group_delay_seconds = 0.01
    return adapter


def _tg_channel() -> Channel:
    return Channel(platform=Platform.TELEGRAM, platform_id="123", is_dm=True)


@pytest.mark.asyncio
async def test_telegram_edit_retries_plain_before_duplicating() -> None:
    bot = _FakeTgBot(fail_markdown_edit=True)
    adapter = _tg_adapter(bot)

    result = await adapter.send(
        OutgoingMessage(
            text="Working... (5s)\n🔧 Reading budget_notes.md",
            channel=_tg_channel(),
            is_update=True,
            update_message_id="77",
        )
    )

    assert result == "77"
    kinds = [name for name, _ in bot.calls]
    assert kinds == ["edit_message_text", "edit_message_text"]  # NO duplicate send
    assert bot.calls[0][1].get("parse_mode") == "Markdown"
    assert "parse_mode" not in bot.calls[1][1]


@pytest.mark.asyncio
async def test_telegram_edit_falls_through_to_send_when_both_edits_fail() -> None:
    bot = _FakeTgBot(fail_markdown_edit=True, fail_plain_edit=True)
    adapter = _tg_adapter(bot)

    result = await adapter.send(
        OutgoingMessage(
            text="hello", channel=_tg_channel(), is_update=True, update_message_id="77"
        )
    )

    kinds = [name for name, _ in bot.calls]
    assert kinds == ["edit_message_text", "edit_message_text", "send_message"]
    assert result is not None


@pytest.mark.asyncio
async def test_telegram_edit_reattaches_stop_keyboard() -> None:
    """editMessageText without reply_markup DROPS the inline keyboard — the
    progress ticks must re-attach the Stop button on every edit."""
    bot = _FakeTgBot()
    adapter = _tg_adapter(bot)

    await adapter.send(
        OutgoingMessage(
            text="Working... (12s)",
            channel=_tg_channel(),
            is_update=True,
            update_message_id="77",
            components=[
                MessageComponent(
                    label="⏹ Stop", custom_id="turn_stop:turn", style="danger"
                )
            ],
        )
    )

    name, kwargs = bot.calls[0]
    assert name == "edit_message_text"
    markup = kwargs.get("reply_markup")
    assert markup is not None
    assert markup.inline_keyboard[0][0].callback_data == "turn_stop:turn"


# ── (c) Discord — fence-aware chunking ─────────────────────────────────────


def _discord_split(text: str, max_length: int = 1900) -> list[str]:
    from adapters.discord import DiscordAdapter

    adapter = DiscordAdapter.__new__(DiscordAdapter)
    return adapter._split_message(text, max_length=max_length)


def test_discord_split_never_cuts_inside_code_fence() -> None:
    # The fence fits within one chunk window (splitter guarantee holds for
    # fences <= max_length, matching the Telegram splitter's contract).
    prologue = "Here is the fix:\n\n" + ("context line\n" * 20)
    code = "```python\n" + ("x = 1  # padding line\n" * 30) + "```\n"
    text = prologue + code + "And that is why it works."
    chunks = _discord_split(text, max_length=800)

    assert len(chunks) >= 2
    assert "".join(chunks) == text  # lossless
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0, "chunk splits inside a code fence"


def test_discord_split_short_text_unchanged() -> None:
    assert _discord_split("short answer") == ["short answer"]


def test_discord_split_plain_text_prefers_paragraph_boundaries() -> None:
    text = ("para one line\n" * 30) + "\n" + ("para two line\n" * 30)
    chunks = _discord_split(text, max_length=300)
    assert "".join(chunks) == text
    assert all(len(c) <= 300 for c in chunks)
