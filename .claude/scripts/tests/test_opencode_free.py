"""Synthetic-only contract tests: a Free request must never spend paid quota."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from runtime import lane_router, model_switch, routing
from runtime import opencode_free as free
from runtime.base import RuntimeRequest, RuntimeResult
from runtime.openai_compatible import OpenAICompatibleRuntime, _require_model_only_request
from runtime.profiles import build_profile_for_provider
from runtime.selection import apply_runtime_selection_choice, resolve_runtime_selection


@pytest.fixture(autouse=True)
def isolated_selection(monkeypatch):
    from runtime.model_control import MODEL_CONFIGS
    from runtime.selection import (
        GENERIC_PROVIDER_ENV_KEY,
        LEGACY_RUNTIME_PROVIDER_KEY,
        RUNTIME_LANE_ENV_KEY,
    )

    keys = {GENERIC_PROVIDER_ENV_KEY, LEGACY_RUNTIME_PROVIDER_KEY, RUNTIME_LANE_ENV_KEY}
    keys.update(config.model_env_key for config in MODEL_CONFIGS.values())
    keys.update(
        key for key in os.environ if key.startswith("SECOND_BRAIN_")
        and ("PROVIDER" in key or "MODEL" in key or "RUNTIME_LANE" in key)
    )
    # Track even absent keys: the real selection service writes os.environ
    # directly. Deleting only pre-existing keys leaks Free into later suites.
    for key in keys:
        monkeypatch.setenv(key, "")


def test_runtime_and_channel_handlers_belong_to_this_checkout():
    import core_handlers

    import config

    root = Path(__file__).resolve().parents[3]
    for module in (config, core_handlers, free, lane_router):
        assert Path(module.__file__).resolve().is_relative_to(root)


def profile(model=free.FREE_DEFAULT_MODEL):
    return replace(build_profile_for_provider(free.FREE_PROVIDER, key_prefix="test"), model=model)


def request(tmp_path, **kwargs):
    return RuntimeRequest(
        prompt="Synthetic test only", cwd=tmp_path, task_name="free_test", **kwargs
    )


def completion(*, text="OK", finish="stop", calls=None):
    return {
        "id": "synthetic",
        "object": "chat.completion",
        "created": 1,
        "model": free.FREE_DEFAULT_MODEL,
        "choices": [
            {
                "index": 0,
                "finish_reason": finish,
                "message": {
                    "role": "assistant",
                    "content": text,
                    "tool_calls": calls,
                },
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def mock_http(monkeypatch, handler):
    import openai  # load SDK types before replacing the HTTP client class

    assert openai.AsyncOpenAI
    original = httpx.AsyncClient

    class MockClient(original):
        def __init__(self, **kwargs):
            assert kwargs.get("trust_env") is False
            assert kwargs.get("follow_redirects") is False
            super().__init__(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "AsyncClient", MockClient)


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["free", "free:mimo-v2.5-free"])
async def test_successful_switch_is_verified_before_atomic_commit(monkeypatch, tmp_path, choice):
    env = {"SECOND_BRAIN_RUNTIME_PROVIDER": "claude", "UNRELATED": "keep"}
    path = tmp_path / ".env"
    path.write_text("# preserve\nSECOND_BRAIN_RUNTIME_PROVIDER=claude\nUNRELATED=keep\n")
    before = path.read_bytes()

    async def probe(model):
        assert path.read_bytes() == before
        assert resolve_runtime_selection(env).lane == "claude_native"
        assert model == ("mimo-v2.5-free" if ":" in choice else free.FREE_DEFAULT_MODEL)

    monkeypatch.setattr(free, "probe_free_model", probe)
    reply = await model_switch.switch_runtime_model(choice, env_path=path, environ=env)
    assert not reply.is_error
    assert "probe succeeded" in reply and "paid fallback is disabled" in reply
    assert resolve_runtime_selection(env).generic_provider == free.FREE_PROVIDER
    assert "UNRELATED=keep" in path.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code", ["HTTP 401", "HTTP 429", "HTTP 503", "TIMEOUT", "INVALID_RESPONSE"]
)
async def test_failed_switch_preserves_config_and_reports_error(monkeypatch, tmp_path, code):
    env = {"SECOND_BRAIN_RUNTIME_PROVIDER": "claude"}
    before_env = dict(env)
    path = tmp_path / ".env"
    path.write_text("SECOND_BRAIN_RUNTIME_PROVIDER=claude\n")
    before = path.read_bytes()
    monkeypatch.setattr(
        free, "probe_free_model", AsyncMock(side_effect=free.FreeRuntimeError(code))
    )
    reply = await model_switch.switch_runtime_model("free", env_path=path, environ=env)
    assert reply.is_error and reply.error_code == code
    assert "Selection unchanged" in reply and "Claude" in reply
    assert path.read_bytes() == before and env == before_env


@pytest.mark.asyncio
async def test_delayed_probe_cannot_overwrite_newer_selection(monkeypatch, tmp_path):
    env = {"SECOND_BRAIN_RUNTIME_PROVIDER": "claude"}
    started, release = asyncio.Event(), asyncio.Event()

    async def probe(model):
        started.set()
        await release.wait()

    monkeypatch.setattr(free, "probe_free_model", probe)
    path = tmp_path / ".env"
    old = asyncio.create_task(model_switch.switch_runtime_model("free", env_path=path, environ=env))
    await started.wait()
    newer = await model_switch.switch_runtime_model("codex", env_path=path, environ=env)
    release.set()
    reply = await old
    assert not newer.is_error
    assert reply.is_error and reply.error_code == "SELECTION_CHANGED"
    assert resolve_runtime_selection(env).generic_provider == "openai-codex"


@pytest.mark.asyncio
async def test_external_config_edit_invalidates_probe(monkeypatch, tmp_path):
    env = {"SECOND_BRAIN_RUNTIME_PROVIDER": "claude"}
    path = tmp_path / ".env"
    path.write_text("SECOND_BRAIN_RUNTIME_PROVIDER=claude\n")

    async def probe(model):
        path.write_text("SECOND_BRAIN_RUNTIME_PROVIDER=gemini\n")

    monkeypatch.setattr(free, "probe_free_model", probe)
    result = await model_switch.switch_runtime_model("free", env_path=path, environ=env)
    assert result.is_error and result.error_code == "SELECTION_CHANGED"
    assert "gemini" in path.read_text()


@pytest.mark.asyncio
async def test_cancelled_probe_never_commits(monkeypatch, tmp_path):
    env = {"SECOND_BRAIN_RUNTIME_PROVIDER": "claude"}

    async def probe(model):
        raise asyncio.CancelledError

    monkeypatch.setattr(free, "probe_free_model", probe)
    with pytest.raises(asyncio.CancelledError):
        await model_switch.switch_runtime_model("free", env_path=tmp_path / ".env", environ=env)
    assert env == {"SECOND_BRAIN_RUNTIME_PROVIDER": "claude"}
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize("model_only", [False, True])
def test_free_route_never_builds_paid_profiles(monkeypatch, tmp_path, model_only):
    apply_runtime_selection_choice("free")
    req = request(tmp_path, model_only=model_only, disallowed_tools=["*"], allow_fallback=True)
    assert [p.provider for p in routing.resolve_runtime_profiles(req)] == [free.FREE_PROVIDER]
    assert [p.provider for p in routing.resolve_generic_runtime_profiles(req)] == [
        free.FREE_PROVIDER
    ]
    assert [p.provider for p in lane_router._resolve_lane_profiles(req)] == [free.FREE_PROVIDER]
    assert free.FREE_PROVIDER not in routing.GENERIC_TEXT_ROUTE
    assert free.FREE_PROVIDER not in routing.GENERIC_CALLER_TOOLS_ROUTE


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["HTTP 401", "HTTP 429", "HTTP 503", "TIMEOUT"])
@pytest.mark.parametrize("model_only", [False, True])
async def test_free_runtime_failure_never_invokes_paid_adapter(
    monkeypatch, tmp_path, code, model_only
):
    apply_runtime_selection_choice("free")
    seen = []

    class Adapter:
        def supports(self, req):
            return True

        def supports_model_only(self):
            return True

        async def run(self, req):
            assert req.allow_fallback is False and req.metadata["free_only"]
            raise free.FreeRuntimeError(code)

    def factory(p):
        seen.append(p.provider)
        assert p.provider == free.FREE_PROVIDER, "paid provider was reached"
        return Adapter()

    monkeypatch.setattr(lane_router, "_adapter_for", factory)
    req = request(tmp_path, model_only=model_only, disallowed_tools=["*"], allow_fallback=True)
    with pytest.raises(free.FreeRuntimeError, match=code):
        await lane_router.run_with_runtime_lanes(req)
    assert seen == [free.FREE_PROVIDER]
    assert resolve_runtime_selection().generic_provider == free.FREE_PROVIDER


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"allowed_tools": ["Bash"]},
        {"preferred_provider": "claude"},
        {"runtime_lane": "claude_native"},
        {"resume": "old-session"},
        {"hooks": {"x": "y"}},
        {"mcp_servers": ["native"]},
    ],
)
async def test_free_incompatible_request_refused_before_any_adapter(
    monkeypatch, tmp_path, overrides
):
    apply_runtime_selection_choice("free")
    monkeypatch.setattr(lane_router, "_adapter_for", lambda _: pytest.fail("adapter called"))
    with pytest.raises(free.FreeRuntimeError):
        await lane_router.run_with_runtime_lanes(request(tmp_path, **overrides))


@pytest.mark.asyncio
async def test_sdk_wire_is_anonymous_and_budgeted_harness_remains_model_only(monkeypatch, tmp_path):
    for key in ["OPENAI_API_KEY", "OPENROUTER_API_KEY", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID"]:
        monkeypatch.setenv(key, "synthetic-must-not-leak")
    seen = []

    def handler(wire):
        seen.append(wire)
        assert str(wire.url) == free.FREE_BASE_URL + "/chat/completions"
        assert wire.headers.get("authorization", "") == ""
        assert "synthetic-must-not-leak" not in str(wire.headers)
        assert free.FREE_SDK_KEY not in str(wire.headers)
        body = json.loads(wire.content)
        assert body["tools"] == [] and body["tool_choice"] == "none"
        assert body["max_completion_tokens"] <= 4096
        return httpx.Response(200, json=completion())

    mock_http(monkeypatch, handler)
    result = await OpenAICompatibleRuntime(profile()).run(
        request(
            tmp_path,
            model_only=True,
            disallowed_tools=["*"],
            max_budget_usd=0.01,
        )
    )
    assert len(seen) == 1 and result.provider == free.FREE_PROVIDER
    assert result.cost_usd == 0.0
    assert result.metadata["model_only"]["usd_budget"] == 0.01
    assert result.tool_call_count == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"base_url": "https://other.example/v1"},
        {"api_key": "paid-key"},
        {"auth_profile": "paid"},
    ],
)
def test_budget_exemption_cannot_be_used_for_keyed_or_other_endpoints(tmp_path, changes):
    req = request(tmp_path, model_only=True, disallowed_tools=["*"], max_budget_usd=0.01)
    with pytest.raises(free.FreeRuntimeError):
        _require_model_only_request(req, replace(profile(), **changes))


def test_paid_http_budget_restriction_remains(tmp_path):
    from runtime.errors import RuntimeUnsupportedCapabilityError

    req = request(tmp_path, model_only=True, disallowed_tools=["*"], max_budget_usd=0.01)
    with pytest.raises(RuntimeUnsupportedCapabilityError):
        _require_model_only_request(req, replace(profile(), provider="openrouter"))


@pytest.mark.parametrize("budget", [-1, float("nan"), float("inf"), True, "1"])
def test_free_rejects_invalid_budget(tmp_path, budget):
    req = request(tmp_path, model_only=True, disallowed_tools=["*"], max_budget_usd=budget)
    with pytest.raises(free.FreeRuntimeError, match="INVALID_BUDGET"):
        _require_model_only_request(req, profile())


@pytest.mark.asyncio
async def test_persistence_failure_does_not_change_process_selection(monkeypatch, tmp_path):
    env = {"SECOND_BRAIN_RUNTIME_PROVIDER": "claude"}
    path = tmp_path / ".env"
    path.write_text("SECOND_BRAIN_RUNTIME_PROVIDER=claude\n")
    monkeypatch.setattr(free, "probe_free_model", AsyncMock())

    def fail_write(*args):
        raise PermissionError("synthetic failure")

    monkeypatch.setattr(model_switch, "_write_updates", fail_write)
    reply = await model_switch.switch_runtime_model("free", env_path=path, environ=env)
    assert reply.is_error
    assert env == {"SECOND_BRAIN_RUNTIME_PROVIDER": "claude"}
    assert path.read_text() == "SECOND_BRAIN_RUNTIME_PROVIDER=claude\n"


@pytest.mark.asyncio
async def test_snapshot_failure_is_machine_error(monkeypatch, tmp_path):
    def fail_read(*args):
        raise PermissionError("synthetic failure")

    monkeypatch.setattr(model_switch, "_fingerprint", fail_read)
    probe = AsyncMock()
    monkeypatch.setattr(free, "probe_free_model", probe)
    reply = await model_switch.switch_runtime_model("free", env_path=tmp_path / ".env", environ={})
    assert reply.is_error
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_free_adapter_refuses_native_tools_even_with_caller_tools(tmp_path):
    with pytest.raises(free.FreeRuntimeError, match="UNSUPPORTED_CAPABILITY"):
        await OpenAICompatibleRuntime(profile()).run(
            request(
                tmp_path,
                allowed_tools=["Bash"],
                tool_defs=[{"type": "function"}],
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 429, 503])
async def test_probe_sends_only_synthetic_prompt_and_preserves_http_code(monkeypatch, status):
    def handler(wire):
        assert wire.headers.get("authorization", "") == ""
        assert json.loads(wire.content)["messages"] == [
            {"role": "user", "content": "Reply exactly OK."}
        ]
        return httpx.Response(status, json={"error": {"message": "secret-provider-body"}})

    mock_http(monkeypatch, handler)
    with pytest.raises(free.FreeRuntimeError) as caught:
        await free.probe_free_model(free.FREE_DEFAULT_MODEL)
    assert caught.value.code == f"HTTP {status}"
    assert "secret-provider-body" not in str(caught.value)


@pytest.mark.asyncio
async def test_probe_timeout_is_bounded(monkeypatch):
    async def handler(wire):
        await asyncio.sleep(10)
        return httpx.Response(200, json=completion())

    mock_http(monkeypatch, handler)
    monkeypatch.setattr(free, "FREE_PROBE_TIMEOUT", 0.02)
    with pytest.raises(free.FreeRuntimeError, match="TIMEOUT"):
        await free.probe_free_model(free.FREE_DEFAULT_MODEL)


@pytest.mark.asyncio
async def test_caller_tool_loop_uses_existing_dispatcher(monkeypatch, tmp_path):
    seen = []

    def handler(wire):
        body = json.loads(wire.content)
        seen.append(body)
        if len(seen) == 1:
            assert body["tools"][0]["function"]["name"] == "synthetic_read"
            return httpx.Response(
                200,
                json=completion(
                    text=None,
                    finish="tool_calls",
                    calls=[
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "synthetic_read", "arguments": "{}"},
                        }
                    ],
                ),
            )
        assert body["messages"][-1] == {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "synthetic-result",
        }
        return httpx.Response(200, json=completion())

    mock_http(monkeypatch, handler)
    dispatch = AsyncMock(return_value="synthetic-result")
    result = await OpenAICompatibleRuntime(profile()).run(
        request(
            tmp_path,
            tool_defs=[
                {
                    "type": "function",
                    "function": {"name": "synthetic_read", "parameters": {"type": "object"}},
                }
            ],
            tool_dispatch=dispatch,
        )
    )
    assert len(seen) == 2 and result.tool_call_count == 1
    dispatch.assert_awaited_once_with("synthetic_read", {})


@pytest.mark.asyncio
async def test_cli_failed_free_override_is_quiet_error_not_paid_execution(monkeypatch):
    import cli
    from click.testing import CliRunner

    monkeypatch.setattr(
        free, "probe_free_model", AsyncMock(side_effect=free.FreeRuntimeError("HTTP 429"))
    )
    monkeypatch.setattr(cli, "ensure_directories", lambda: None)
    monkeypatch.setattr(cli, "ConversationEngine", lambda *a: pytest.fail("engine reached"))
    # Click's command owns its event loop.
    result = await asyncio.to_thread(
        CliRunner().invoke, cli.main, ["chat", "-m", "free", "-q", "hi", "-Q"]
    )
    payload = json.loads(result.stdout.strip())
    assert payload["success"] is False and "HTTP 429" in payload["error"]
    assert result.exit_code == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["telegram", "discord"])
@pytest.mark.parametrize("fails", [False, True])
async def test_native_channel_model_command_and_next_turn(monkeypatch, tmp_path, platform, fails):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import core_handlers
    from adapters.discord import DiscordAdapter, get_discord_native_command_menu
    from adapters.telegram import TelegramAdapter
    from commands import CATEGORIES, COMMANDS, get_telegram_bot_commands
    from extension_manager import ExtensionManager
    from router import ChatRouter

    import config

    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    # Mark environment keys for pytest restoration even when the real switch
    # updates os.environ directly.
    for key in [
        "SECOND_BRAIN_RUNTIME_PROVIDER",
        "SECOND_BRAIN_RUNTIME_LANE",
        "SECOND_BRAIN_GENERIC_PROVIDER",
    ]:
        monkeypatch.setenv(key, "")
    apply_runtime_selection_choice("claude")
    monkeypatch.setattr(
        free,
        "probe_free_model",
        AsyncMock(
            side_effect=free.FreeRuntimeError("HTTP 429") if fails else None,
        ),
    )
    if platform == "discord":
        adapter = DiscordAdapter.__new__(DiscordAdapter)
        adapter.allowed_users = []
        adapter.allowed_guilds = []
        adapter._ingress_role = lambda _: "admin"
        adapter._enqueue = AsyncMock()
        interaction = SimpleNamespace(
            id=1,
            user=SimpleNamespace(id=2, display_name="Synthetic"),
            guild_id=None,
            channel_id=3,
            response=SimpleNamespace(defer=AsyncMock()),
        )
        await adapter._queue_native_slash_command(interaction, "model", "free")
        menu = dict(get_discord_native_command_menu())
    else:
        adapter = TelegramAdapter.__new__(TelegramAdapter)
        adapter.allowed_user_ids = []
        adapter._bot_username = "synthetic_bot"
        adapter._ingress_role = lambda _: "admin"
        adapter._enqueue = AsyncMock()
        message = SimpleNamespace(
            text="/model@synthetic_bot free",
            from_user=SimpleNamespace(id=2, first_name="Synthetic"),
            chat_id=3,
            chat=SimpleNamespace(type="private"),
            reply_to_message=None,
            message_id=1,
            to_dict=lambda: {},
        )
        await adapter._on_message(SimpleNamespace(message=message), None)
        menu = dict(get_telegram_bot_commands())
    assert "free" in menu["model"].lower()
    incoming = adapter._enqueue.await_args.args[0]
    assert incoming.text == "/model free"
    manager = ExtensionManager()
    manager.register_core_commands(COMMANDS, CATEGORIES, core_handlers.CORE_HANDLERS)
    router = ChatRouter(MagicMock(), manager)
    router._persist_router_turn_off_loop = AsyncMock()
    adapter.send = AsyncMock()
    await router._handle_inner(adapter, incoming)
    outgoing = adapter.send.await_args.args[0]
    assert outgoing.is_error is fails
    if fails:
        assert "HTTP 429" in outgoing.text
        assert resolve_runtime_selection().lane == "claude_native"
        return
    assert resolve_runtime_selection().generic_provider == free.FREE_PROVIDER
    seen = []

    class Adapter:
        def supports(self, req):
            return True

        async def run(self, req):
            seen.append(req)
            return RuntimeResult(
                text="OK",
                runtime_lane="generic_runtime",
                provider=free.FREE_PROVIDER,
                model=free.FREE_DEFAULT_MODEL,
            )

    monkeypatch.setattr(
        lane_router,
        "_adapter_for",
        lambda p: Adapter() if p.provider == free.FREE_PROVIDER else pytest.fail("paid provider"),
    )
    result = await lane_router.run_with_runtime_lanes(request(tmp_path, conversational=True))
    assert result.provider == free.FREE_PROVIDER and len(seen) == 1
    assert seen[0].allow_fallback is False
