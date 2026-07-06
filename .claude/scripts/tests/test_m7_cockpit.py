"""Homie Mobile M7 — chat cockpit seams.

Covers the four additive backend seams:
  (a) RuntimeRequest additive fields (effort, on_tool_event) — None defaults.
  (b) claude_sdk forwards `effort` into SDK options and fires `on_tool_event`
      per streamed ToolUseBlock (fail-open, truncated preview).
  (c) engine applies raw_event cockpit overrides (model_override /
      reasoning_effort) and live tool telemetry (progress + emit hook).
  (d) router stop path — cancel_active_turn() cancels the in-flight engine
      task, the turn recovers with a stop marker, turn_aborted is emitted.
  (e) dashboard endpoints — send-body validation, raw_event landing,
      /stop and /steer contracts (fake chat runtime, no engine boot).
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

import pytest

import engine as engine_module
from engine import ConversationEngine
from models import Channel, IncomingMessage, OutgoingMessage, Platform, Thread, User
from router import ChatRouter
from session import SQLiteSessionStore

from runtime.base import RUNTIME_LANE_CLAUDE_NATIVE, RuntimeRequest, RuntimeResult
from runtime.claude_sdk import ClaudeSdkRuntime
from runtime.profiles import RuntimeProfile


# ── (a) RuntimeRequest additive fields ────────────────────────────────────


def test_runtime_request_m7_fields_default_none() -> None:
    req = RuntimeRequest(prompt="hi", cwd=".", task_name="t")
    assert req.effort is None
    assert req.on_tool_event is None


# ── (b) claude_sdk — effort forwarding + on_tool_event firing ─────────────


@pytest.mark.asyncio
async def test_claude_sdk_forwards_effort_into_options() -> None:
    from unittest.mock import patch

    captured: dict[str, object] = {}

    class _DummyOptions:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    async def _empty_query(prompt, options):  # noqa: ARG001
        if False:
            yield None

    runtime = ClaudeSdkRuntime(
        RuntimeProfile(key="primary-claude", provider="claude", model="claude-haiku-4-5-20251001")
    )
    request = RuntimeRequest(prompt="hi", cwd=".", task_name="t", effort="low")

    with patch("claude_agent_sdk.ClaudeAgentOptions", _DummyOptions), \
         patch("claude_agent_sdk.query", _empty_query):
        await runtime.run(request)

    assert captured.get("effort") == "low"


@pytest.mark.asyncio
async def test_claude_sdk_omits_effort_when_unset() -> None:
    from unittest.mock import patch

    captured: dict[str, object] = {}

    class _DummyOptions:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    async def _empty_query(prompt, options):  # noqa: ARG001
        if False:
            yield None

    runtime = ClaudeSdkRuntime(
        RuntimeProfile(key="primary-claude", provider="claude", model="claude-haiku-4-5-20251001")
    )
    request = RuntimeRequest(prompt="hi", cwd=".", task_name="t")

    with patch("claude_agent_sdk.ClaudeAgentOptions", _DummyOptions), \
         patch("claude_agent_sdk.query", _empty_query):
        await runtime.run(request)

    assert "effort" not in captured


@pytest.mark.asyncio
async def test_claude_sdk_fires_on_tool_event_per_tool_use_block() -> None:
    from unittest.mock import patch

    from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock

    class _DummyOptions:
        def __init__(self, **kwargs):
            pass

    big_input = {"path": "x" * 500}
    message = AssistantMessage(
        content=[
            TextBlock(text="working"),
            ToolUseBlock(id="t1", name="Read", input={"path": "src/a.py"}),
            ToolUseBlock(id="t2", name="Grep", input=big_input),
        ],
        model="claude-haiku-4-5-20251001",
    )

    async def _one_message_query(prompt, options):  # noqa: ARG001
        yield message

    events: list[dict] = []
    runtime = ClaudeSdkRuntime(
        RuntimeProfile(key="primary-claude", provider="claude", model="claude-haiku-4-5-20251001")
    )
    request = RuntimeRequest(
        prompt="hi", cwd=".", task_name="t", on_tool_event=events.append
    )

    with patch("claude_agent_sdk.ClaudeAgentOptions", _DummyOptions), \
         patch("claude_agent_sdk.query", _one_message_query):
        result = await runtime.run(request)

    assert [e["name"] for e in events] == ["Read", "Grep"]
    assert events[0]["id"] == "t1"
    assert "src/a.py" in events[0]["input_preview"]
    # Preview is truncated, never the raw 500-char payload.
    assert len(events[1]["input_preview"]) <= 200
    assert events[1]["input_preview"].endswith("...")
    # The counting path is unchanged by the callback.
    assert result.tool_call_count == 2


@pytest.mark.asyncio
async def test_claude_sdk_on_tool_event_exception_is_swallowed() -> None:
    from unittest.mock import patch

    from claude_agent_sdk import AssistantMessage, ToolUseBlock

    class _DummyOptions:
        def __init__(self, **kwargs):
            pass

    message = AssistantMessage(
        content=[ToolUseBlock(id="t1", name="Read", input={})],
        model="claude-haiku-4-5-20251001",
    )

    async def _one_message_query(prompt, options):  # noqa: ARG001
        yield message

    def _boom(_ev: dict) -> None:
        raise RuntimeError("cockpit sink died")

    runtime = ClaudeSdkRuntime(
        RuntimeProfile(key="primary-claude", provider="claude", model="claude-haiku-4-5-20251001")
    )
    request = RuntimeRequest(prompt="hi", cwd=".", task_name="t", on_tool_event=_boom)

    with patch("claude_agent_sdk.ClaudeAgentOptions", _DummyOptions), \
         patch("claude_agent_sdk.query", _one_message_query):
        result = await runtime.run(request)  # must not raise

    assert result.tool_call_count == 1


# ── (c) engine — raw_event overrides + live tool telemetry ────────────────


def _make_message(
    text: str = "Need a summary", raw_event: dict | None = None
) -> IncomingMessage:
    return IncomingMessage(
        text=text,
        user=User(platform=Platform.WEB, platform_id="user-1", display_name="YourUser"),
        channel=Channel(platform=Platform.WEB, platform_id="dashboard-main", is_dm=True),
        platform=Platform.WEB,
        thread=Thread(thread_id="dashboard-main"),
        raw_event=raw_event or {},
    )


def _make_project_root(tmp_path) -> Any:
    project_root = tmp_path / "project"
    (project_root / "TheHomie" / "Memory" / "daily").mkdir(parents=True)
    return project_root


@pytest.mark.asyncio
async def test_engine_applies_raw_event_model_and_effort_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    convo = ConversationEngine(store, _make_project_root(tmp_path))
    captured: dict[str, Any] = {}

    async def fake_run(request):
        captured["model"] = request.model
        captured["effort"] = request.effort
        return RuntimeResult(
            text="ok", runtime_lane=RUNTIME_LANE_CLAUDE_NATIVE, provider="claude",
            model=request.model, profile_key="primary-claude",
        )

    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)

    message = _make_message(
        raw_event={"model_override": "claude-opus-4-7", "reasoning_effort": "low"}
    )
    outputs = [out async for out in convo.handle_message(message)]
    assert outputs[-1].text == "ok"
    assert captured["model"] == "claude-opus-4-7"
    assert captured["effort"] == "low"


@pytest.mark.asyncio
async def test_engine_defaults_unchanged_without_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("SECOND_BRAIN_CLAUDE_MODEL", "claude-sonnet-4-7")
    store = SQLiteSessionStore(tmp_path / "chat.db")
    convo = ConversationEngine(store, _make_project_root(tmp_path))
    captured: dict[str, Any] = {}

    async def fake_run(request):
        captured["model"] = request.model
        captured["effort"] = request.effort
        captured["on_tool_event"] = request.on_tool_event
        return RuntimeResult(
            text="ok", runtime_lane=RUNTIME_LANE_CLAUDE_NATIVE, provider="claude",
            model=request.model, profile_key="primary-claude",
        )

    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)

    outputs = [out async for out in convo.handle_message(_make_message())]
    assert outputs[-1].text == "ok"
    assert captured["model"] == "claude-sonnet-4-7"
    assert captured["effort"] is None
    # No progress dict → no live-telemetry callback.
    assert captured["on_tool_event"] is None


@pytest.mark.asyncio
async def test_engine_on_tool_event_updates_progress_and_emits(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    convo = ConversationEngine(store, _make_project_root(tmp_path))
    emitted: list[dict] = []
    progress: dict[str, Any] = {"tool_calls": 0, "emit_turn_event": emitted.append}
    live_counts: list[int] = []

    async def fake_run(request):
        assert callable(request.on_tool_event)
        request.on_tool_event({"id": "t1", "name": "Read", "input_preview": "{}"})
        live_counts.append(progress["tool_calls"])
        request.on_tool_event({"id": "t2", "name": "Bash", "input_preview": "{}"})
        live_counts.append(progress["tool_calls"])
        return RuntimeResult(
            text="ok", runtime_lane=RUNTIME_LANE_CLAUDE_NATIVE, provider="claude",
            model="m", profile_key="primary-claude",
        )

    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)

    outputs = [
        out async for out in convo.handle_message(_make_message(), progress=progress)
    ]
    assert outputs[-1].text == "ok"
    # Live increments DURING the run — the 12s ticker sees real counts.
    assert live_counts == [1, 2]
    assert [e["type"] for e in emitted] == ["tool_call", "tool_call"]
    assert emitted[0]["name"] == "Read"
    assert emitted[1]["name"] == "Bash"


# ── (d) router — stop path ────────────────────────────────────────────────


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


class _CockpitAdapter:
    platform = Platform.WEB

    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []
        self.updates: list[OutgoingMessage] = []
        self.turn_events: list[dict] = []

    async def send(self, message: OutgoingMessage) -> str:
        self.sent.append(message)
        return f"sent-{len(self.sent)}"

    async def update(self, message: OutgoingMessage) -> str:
        self.updates.append(message)
        return message.update_message_id or f"updated-{len(self.updates)}"

    def emit_turn_event(self, ev: dict, *, channel: Any = None, thread: Any = None) -> None:
        self.turn_events.append(ev)


def _make_web_incoming(text: str = "long running ask") -> IncomingMessage:
    return IncomingMessage(
        text=text,
        user=User(platform=Platform.WEB, platform_id="dashboard-user"),
        channel=Channel(platform=Platform.WEB, platform_id="dashboard-main", is_dm=True),
        platform=Platform.WEB,
        thread=Thread(thread_id="dashboard-main"),
    )


@pytest.mark.asyncio
async def test_cancel_active_turn_stops_engine_and_recovers(tmp_path) -> None:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    engine = _HangingEngine(store)
    router = ChatRouter(engine, _NoopManager())  # type: ignore[arg-type]
    adapter = _CockpitAdapter()
    incoming = _make_web_incoming()

    turn = asyncio.create_task(router._handle_inner(adapter, incoming))
    await asyncio.wait_for(engine.started.wait(), timeout=5)
    assert router._active_turns  # registered while in flight

    stopped = router.cancel_active_turn("web:dashboard-main:dashboard-main:")
    assert stopped == 1

    await asyncio.wait_for(turn, timeout=5)
    # Placeholder replaced with the stop marker, not an error.
    assert adapter.updates
    assert adapter.updates[-1].text == "⏹️ Stopped."
    assert adapter.updates[-1].is_error is False
    # Cockpit adapters get the distinct abort event.
    assert {"type": "turn_aborted"} in [
        {k: v for k, v in ev.items() if k == "type"} for ev in adapter.turn_events
    ]
    # Registry cleaned.
    assert not router._active_turns
    # No assistant reply persisted for the stopped turn.
    messages = store.list_messages("web:dashboard-main:dashboard-main")
    assert [m.role for m in messages] == ["user"] or messages == []


@pytest.mark.asyncio
async def test_cancel_active_turn_prefix_miss_cancels_nothing(tmp_path) -> None:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    engine = _HangingEngine(store)
    router = ChatRouter(engine, _NoopManager())  # type: ignore[arg-type]
    adapter = _CockpitAdapter()

    turn = asyncio.create_task(router._handle_inner(adapter, _make_web_incoming()))
    await asyncio.wait_for(engine.started.wait(), timeout=5)

    assert router.cancel_active_turn("web:some-other-conversation:") == 0
    assert router._active_turns  # still running

    # Clean up: cancel for real so the test doesn't leak the task.
    router.cancel_active_turn("web:dashboard-main:")
    await asyncio.wait_for(turn, timeout=5)


# ── (e) dashboard endpoints ───────────────────────────────────────────────


@pytest.fixture
def dash_client(tmp_path, monkeypatch):
    """Slim isolated app: tmp DB paths, loopback no-token auth."""
    from fastapi.testclient import TestClient

    import config

    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", tmp_path / "dashboard.db")
    monkeypatch.setattr(config, "CHAT_DB_PATH", tmp_path / "chat.db")
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(config, "ORCHESTRATION_DB_PATH", tmp_path / "orch.db")
    monkeypatch.setenv("ORCHESTRATION_API_TOKEN", "")

    import orchestration.api as oa

    importlib.reload(oa)
    db, cs, ms, reg, ts = oa._get_services()
    oa._db = db
    oa._convoy_svc = cs
    oa._mailbox_svc = ms
    oa._executor_registry = reg
    oa._team_svc = ts
    yield TestClient(oa.app)
    db.close()


class _FakeChatRouter:
    def __init__(self) -> None:
        self.queued: list[Any] = []
        self.serialized: list[Any] = []
        self.cancel_prefixes: list[str] = []
        self.cancel_result = 1
        self.retained: list[Any] = []

    def _queue_incoming(self, adapter: Any, incoming: Any) -> None:
        self.queued.append(incoming)

    async def _handle_serialized(self, adapter: Any, incoming: Any) -> None:
        self.serialized.append(incoming)

    def _retain_task(self, task: Any) -> None:
        self.retained.append(task)

    def cancel_active_turn(self, key_prefix: str) -> int:
        self.cancel_prefixes.append(key_prefix)
        return self.cancel_result


class _FakeChatAdapter:
    def track(self, *, persona_id: str, conversation_id: str) -> None:
        pass


def _patch_chat_runtime(monkeypatch) -> _FakeChatRouter:
    import dashboard_api

    fake_router = _FakeChatRouter()
    runtime = {"router": fake_router, "adapter": _FakeChatAdapter()}
    monkeypatch.setattr(dashboard_api, "_get_dashboard_chat_runtime", lambda: runtime)
    monkeypatch.setattr(dashboard_api, "_DASHBOARD_CHAT_RUNTIME", runtime)
    return fake_router


def test_send_rejects_unknown_model(dash_client) -> None:
    r = dash_client.post(
        "/api/conversation/default/send",
        json={"text": "hi", "model": "gpt-99-turbo"},
    )
    assert r.status_code == 400
    assert "unknown model" in r.json()["detail"]


def test_send_rejects_bad_effort(dash_client) -> None:
    r = dash_client.post(
        "/api/conversation/default/send",
        json={"text": "hi", "reasoning_effort": "ludicrous"},
    )
    assert r.status_code == 400
    assert "reasoning_effort" in r.json()["detail"]


def test_send_rejects_overrides_on_persona_conversations(dash_client) -> None:
    r = dash_client.post(
        "/api/conversation/sales/send",
        json={"text": "hi", "model": "claude-opus-4-7"},
    )
    assert r.status_code == 400
    assert "main-only" in r.json()["detail"]


def test_send_lands_overrides_in_raw_event(dash_client, monkeypatch) -> None:
    fake_router = _patch_chat_runtime(monkeypatch)
    r = dash_client.post(
        "/api/conversation/default/send",
        json={"text": "hi", "model": "claude-opus-4-7", "reasoning_effort": "high"},
    )
    assert r.status_code == 200
    assert r.json()["queued"] is True
    assert len(fake_router.queued) == 1
    raw_event = fake_router.queued[0].raw_event
    assert raw_event["model_override"] == "claude-opus-4-7"
    assert raw_event["reasoning_effort"] == "high"


def test_send_without_overrides_keeps_raw_event_empty_strings(dash_client, monkeypatch) -> None:
    fake_router = _patch_chat_runtime(monkeypatch)
    r = dash_client.post("/api/conversation/default/send", json={"text": "hi"})
    assert r.status_code == 200
    raw_event = fake_router.queued[0].raw_event
    assert raw_event["model_override"] == ""
    assert raw_event["reasoning_effort"] == ""


def test_stop_without_runtime_reports_zero(dash_client, monkeypatch) -> None:
    import dashboard_api

    monkeypatch.setattr(dashboard_api, "_DASHBOARD_CHAT_RUNTIME", None)
    r = dash_client.post("/api/conversation/default/stop", json={})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "stopped": 0}


def test_stop_cancels_via_router_with_scoped_prefix(dash_client, monkeypatch) -> None:
    fake_router = _patch_chat_runtime(monkeypatch)
    r = dash_client.post(
        "/api/conversation/default/stop",
        json={"conversation_id": "dashboard-main"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "stopped": 1}
    assert fake_router.cancel_prefixes == ["web:dashboard-main:dashboard-main:"]


def test_stop_is_main_only(dash_client) -> None:
    r = dash_client.post("/api/conversation/sales/stop", json={})
    assert r.status_code == 400
    assert "main-only" in r.json()["detail"]


def test_steer_is_main_only(dash_client) -> None:
    r = dash_client.post("/api/conversation/sales/steer", json={"text": "go left"})
    assert r.status_code == 400
    assert "main-only" in r.json()["detail"]


def test_steer_queues_serialized_turn_with_preamble(dash_client, monkeypatch) -> None:
    fake_router = _patch_chat_runtime(monkeypatch)
    r = dash_client.post(
        "/api/conversation/default/steer",
        json={"text": "focus on the login bug", "conversation_id": "dashboard-main"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["queued"] is True
    assert body["conversation_id"] == "dashboard-main"
    # One serialized task was retained (steer rides the thread lock).
    assert len(fake_router.retained) == 1
    assert len(fake_router.serialized) == 1
    incoming = fake_router.serialized[0]
    assert incoming.text.startswith("[Steer the in-flight conversation")
    assert "focus on the login bug" in incoming.text
    assert incoming.raw_event["steer"] is True
    assert incoming.raw_event["display_text"] == "focus on the login bug"
