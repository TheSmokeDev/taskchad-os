"""Codex app-server dynamicTools gate and protocol tests (#281)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from runtime import lane_router, tool_registry  # noqa: E402
from runtime.base import RuntimeRequest  # noqa: E402
from runtime.codex_app_server_gate import (  # noqa: E402
    _DISABLED_FEATURES as GATE_DISABLED_FEATURES,
)
from runtime.codex_app_server_gate import (  # noqa: E402
    EXPECTED_CODEX_VERSION,
)
from runtime.errors import RuntimeRetryableError  # noqa: E402
from runtime.openai_codex_app_server import (  # noqa: E402
    _DISABLED_FEATURES,
    SUPPORTED_CODEX_VERSION,
    CodexAmbientAuthorityError,
    CodexAppServerClient,
    CodexAppServerProtocolError,
    OpenAICodexAppServerRuntime,
    _subprocess_creation_flags,
    convert_openai_tool_defs,
    least_authority_args,
    resolve_codex_executable,
)
from runtime.profiles import RuntimeProfile  # noqa: E402

GET_MARKER = {
    "type": "function",
    "function": {
        "name": "get_gate_marker",
        "description": "Return the harmless gate marker.",
        "parameters": {
            "type": "object",
            "properties": {"nonce": {"type": "string"}},
            "required": ["nonce"],
            "additionalProperties": False,
        },
    },
}


def _profile() -> RuntimeProfile:
    return RuntimeProfile(
        key="primary-openai-codex",
        provider="openai-codex",
        model="chatgpt-plan-default",
        command="codex",
    )


def _request(dispatch=None) -> RuntimeRequest:
    return RuntimeRequest(
        task_name="test",
        prompt="Call get_gate_marker with nonce 731, then return the exact result.",
        cwd=Path.cwd(),
        tool_defs=[GET_MARKER],
        tool_dispatch=dispatch or (lambda name, args: "GATE_VALUE_731"),
    )


def test_openai_defs_convert_without_widening_and_are_detached():
    original = json.loads(json.dumps(GET_MARKER))
    out = convert_openai_tool_defs([original])
    assert out == [
        {
            "type": "function",
            "name": "get_gate_marker",
            "description": "Return the harmless gate marker.",
            "inputSchema": GET_MARKER["function"]["parameters"],
            "deferLoading": False,
        }
    ]
    original["function"]["parameters"]["properties"]["injected"] = {"type": "string"}
    assert "injected" not in out[0]["inputSchema"]["properties"]


@pytest.mark.parametrize(
    "definition",
    [
        {},
        {"type": "computer"},
        {"type": "function", "function": {}},
        {
            "type": "function",
            "function": {
                "name": "../shell",
                "description": "x",
                "parameters": {"type": "object"},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "x",
                "description": "",
                "parameters": {"type": "object"},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "x",
                "description": "x",
                "parameters": {"type": "string"},
            },
        },
    ],
)
def test_malformed_dynamic_tool_definitions_fail_before_spawn(definition):
    with pytest.raises(ValueError):
        convert_openai_tool_defs([definition])


def test_duplicate_dynamic_tool_definitions_fail_closed():
    with pytest.raises(ValueError, match="duplicate"):
        convert_openai_tool_defs([GET_MARKER, GET_MARKER])


def test_windows_app_server_is_detached_from_the_scheduled_console(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    expected = int(getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0x08000000))
    assert _subprocess_creation_flags() == expected
    assert _subprocess_creation_flags() != 0


def test_least_authority_profile_disables_every_native_surface():
    args = least_authority_args("codex")
    joined = " ".join(args)
    for feature in (
        "shell_tool",
        "apps",
        "browser_use",
        "computer_use",
        "image_generation",
        "in_app_browser",
        "code_mode_host",
        "multi_agent",
        "skill_search",
        "hooks",
        "memories",
        "workspace_dependencies",
    ):
        assert f"--disable {feature}" in joined
    assert "mcp_servers={}" in args
    assert "--strict-config" in args


def test_production_security_profile_matches_the_proven_gate():
    assert SUPPORTED_CODEX_VERSION == EXPECTED_CODEX_VERSION == "0.146.0"
    assert set(_DISABLED_FEATURES) == set(GATE_DISABLED_FEATURES)


@pytest.mark.asyncio
async def test_thread_start_carries_persona_identity_inside_least_authority(tmp_path):
    request = _request()
    request.system_prompt = "AI_ENGINEER_PERSONA_MARKER"
    client = CodexAppServerClient(request, _profile(), executable="codex")
    client._empty_cwd = tmp_path
    captured = {}

    async def fake_request(method, params):
        captured[method] = params
        return {
            "thread": {"id": "thread-1"},
            "sandbox": {"type": "readOnly", "networkAccess": False},
            "approvalPolicy": "never",
            "instructionSources": [],
            "runtimeWorkspaceRoots": [],
            "cwd": str(tmp_path.resolve()),
        }

    client._request = fake_request
    await client._start_thread()
    params = captured["thread/start"]
    assert "AI_ENGINEER_PERSONA_MARKER" in params["baseInstructions"]
    assert params["selectedCapabilityRoots"] == []
    assert params["runtimeWorkspaceRoots"] == []
    assert params["approvalPolicy"] == "never"


class _Stream:
    def __init__(self, messages):
        self.messages = list(messages)
        self.writes = []

    async def readline(self):
        if not self.messages:
            return b""
        value = self.messages.pop(0)
        if isinstance(value, bytes):
            return value
        return json.dumps(value).encode() + b"\n"

    def write(self, value):
        self.writes.append(json.loads(value))

    async def drain(self):
        return None


class _Process:
    def __init__(self, messages):
        self.stdin = _Stream([])
        self.stdout = _Stream(messages)
        self.stderr = _Stream([])
        self.returncode = None
        self.pid = 999

    def terminate(self):
        self.returncode = 0

    async def wait(self):
        return self.returncode


def _client(messages, dispatch=None):
    client = CodexAppServerClient(_request(dispatch), _profile(), executable="codex")
    client._process = _Process(messages)
    client.receipt.thread_id = "thread-1"
    client.receipt.turn_id = "turn-1"
    return client


@pytest.mark.asyncio
async def test_real_dynamic_call_reenters_exact_dispatch_and_returns_result():
    calls = []
    message = {
        "method": "item/tool/call",
        "id": 7,
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "callId": "call-1",
            "tool": "get_gate_marker",
            "arguments": {"nonce": "731"},
        },
    }
    client = _client([], lambda name, args: calls.append((name, args)) or "GATE_VALUE_731")
    await client._handle_dynamic_tool_call(message)
    assert calls == [("get_gate_marker", {"nonce": "731"})]
    assert client._process.stdin.writes == [
        {
            "id": 7,
            "result": {
                "contentItems": [{"type": "inputText", "text": "GATE_VALUE_731"}],
                "success": True,
            },
        }
    ]
    assert client.receipt.tool_calls[0].name == "get_gate_marker"


@pytest.mark.asyncio
async def test_multiple_dynamic_calls_are_independently_dispatched_and_recorded():
    calls = []
    client = _client([], lambda name, args: calls.append((name, args)) or "ok")
    for request_id, call_id, nonce in ((7, "call-1", "731"), (8, "call-2", "842")):
        await client._handle_dynamic_tool_call(
            {
                "method": "item/tool/call",
                "id": request_id,
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "callId": call_id,
                    "tool": "get_gate_marker",
                    "arguments": {"nonce": nonce},
                },
            }
        )

    assert calls == [
        ("get_gate_marker", {"nonce": "731"}),
        ("get_gate_marker", {"nonce": "842"}),
    ]
    assert [call.id for call in client.receipt.tool_calls] == ["call-1", "call-2"]
    assert all(write["result"]["success"] for write in client._process.stdin.writes)


@pytest.mark.asyncio
async def test_tool_failure_returns_bounded_failure_content_and_receipt():
    def fail(_name, _arguments):
        raise RuntimeError("handler failed")

    client = _client([], fail)
    await client._handle_dynamic_tool_call(
        {
            "method": "item/tool/call",
            "id": 7,
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "callId": "call-1",
                "tool": "get_gate_marker",
                "arguments": {},
            },
        }
    )

    response = client._process.stdin.writes[0]["result"]
    assert response["success"] is False
    assert response["contentItems"][0]["text"] == "RuntimeError: handler failed"
    assert client.receipt.tool_calls[0].status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutator,match",
    [
        (lambda p: p.update(tool="unknown"), "unknown"),
        (lambda p: p.update(arguments="bad"), "arguments"),
        (lambda p: p.update(threadId="foreign"), "thread id"),
        (lambda p: p.update(turnId="foreign"), "turn id"),
        (lambda p: p.update(callId=""), "call id"),
    ],
)
async def test_malformed_or_unknown_calls_never_dispatch(mutator, match):
    calls = []
    params = {
        "threadId": "thread-1",
        "turnId": "turn-1",
        "callId": "call-1",
        "tool": "get_gate_marker",
        "arguments": {},
    }
    mutator(params)
    client = _client([], lambda *args: calls.append(args))
    with pytest.raises(CodexAppServerProtocolError, match=match):
        await client._handle_dynamic_tool_call(
            {"method": "item/tool/call", "id": 7, "params": params}
        )
    assert calls == []


@pytest.mark.asyncio
async def test_duplicate_call_id_never_dispatches_twice():
    calls = []
    message = {
        "method": "item/tool/call",
        "id": 7,
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "callId": "call-1",
            "tool": "get_gate_marker",
            "arguments": {},
        },
    }
    client = _client([], lambda *args: calls.append(args) or "ok")
    await client._handle_dynamic_tool_call(message)
    with pytest.raises(CodexAppServerProtocolError, match="duplicate"):
        await client._handle_dynamic_tool_call(message)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_unexpected_server_request_gets_error_and_fails_closed():
    client = _client([])
    with pytest.raises(CodexAppServerProtocolError, match="unexpected"):
        await client._handle_server_request(
            {"id": 9, "method": "item/commandExecution/requestApproval"},
            pre_turn=False,
        )
    assert client._process.stdin.writes[0]["error"]["code"] == -32601


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        {"method": "item/commandExecution/outputDelta", "params": {}},
        {
            "method": "item/completed",
            "params": {"item": {"type": "fileChange"}},
        },
        {
            "method": "item/completed",
            "params": {"item": {"type": "mcpToolCall"}},
        },
        {
            "method": "item/completed",
            "params": {"item": {"type": "collabAgentToolCall"}},
        },
        {
            "method": "item/completed",
            "params": {"item": {"type": "webSearch"}},
        },
        {
            "method": "item/completed",
            "params": {"item": {"type": "imageGeneration"}},
        },
    ],
)
async def test_any_native_event_fails_the_turn(message):
    client = _client([message])
    with pytest.raises(CodexAmbientAuthorityError):
        await client._event_loop()


@pytest.mark.asyncio
async def test_malformed_jsonl_fails_closed():
    client = _client([b"not-json\n"])
    with pytest.raises(CodexAppServerProtocolError, match="malformed"):
        await client._read_message()


@pytest.mark.asyncio
async def test_final_answer_is_taken_from_item_completed_before_empty_turn_payload():
    client = _client(
        [
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "agentMessage",
                        "text": "GATE_VALUE_731",
                        "phase": "final_answer",
                    }
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "turn": {
                        "id": "turn-1",
                        "status": "completed",
                        "items": [],
                        "itemsView": "notLoaded",
                    }
                },
            },
        ]
    )
    await client._event_loop()
    assert client.receipt.final_text == "GATE_VALUE_731"


@pytest.mark.asyncio
async def test_cancel_closes_the_child_and_reaps_temp_home(monkeypatch, tmp_path):
    client = CodexAppServerClient(_request(), _profile(), executable="codex")
    process = _Process([])
    client._process = process

    class _Home:
        def __init__(self):
            self.cleaned = False

        def cleanup(self):
            self.cleaned = True

    home = _Home()
    client._codex_home = home
    await client.close()
    assert process.returncode == 0
    assert home.cleaned is True


@pytest.mark.asyncio
async def test_adapter_timeout_is_retryable(monkeypatch):
    async def hang(_client):
        await asyncio.sleep(60)

    monkeypatch.setenv("SECOND_BRAIN_CODEX_APP_SERVER_TIMEOUT_S", "0.01")
    monkeypatch.setattr(CodexAppServerClient, "run", hang)
    adapter = OpenAICodexAppServerRuntime(_profile())
    with pytest.raises(RuntimeRetryableError, match="app-server failed"):
        await adapter.run(_request())


@pytest.mark.integration
@pytest.mark.skipif(
    os.getenv("RUN_CODEX_APP_SERVER_INTEGRATION") != "1",
    reason="set RUN_CODEX_APP_SERVER_INTEGRATION=1 for the real subscription-backed gate",
)
@pytest.mark.asyncio
async def test_real_lane_router_executes_a_registered_dynamic_tool(monkeypatch):
    calls = []
    entry = tool_registry.register_tool(
        "codex_live_marker",
        "Return the harmless live transport marker.",
        toolset="codex_live_test",
        parameters={
            "type": "object",
            "properties": {"nonce": {"type": "string"}},
            "required": ["nonce"],
            "additionalProperties": False,
        },
        handler=lambda nonce: f"GATE_VALUE_{nonce}",
    )
    request = RuntimeRequest(
        task_name="codex_app_server_live_test",
        prompt="Call codex_live_marker with nonce 731. Return exactly its result.",
        cwd=Path.cwd(),
        tool_defs=[entry.schema],
        tool_dispatch=lambda name, args: calls.append((name, args))
        or f"GATE_VALUE_{args['nonce']}",
    )
    request.prompt = (
        "Call codex_live_marker with nonce 731. Return exactly its result. "
        "Do not use any native capability."
    )
    monkeypatch.setattr(lane_router, "_resolve_lane_profiles", lambda _request: [_profile()])
    try:
        result = await asyncio.wait_for(
            lane_router.run_with_runtime_lanes(request),
            timeout=90,
        )
    finally:
        tool_registry.unregister_tool("codex_live_marker")

    assert result.text == "GATE_VALUE_731"
    assert calls == [("codex_live_marker", {"nonce": "731"})]
    assert result.provider == "openai-codex"
    assert result.tool_call_count == 1
    assert result.tool_names_used == ["codex_live_marker"]


@pytest.mark.integration
@pytest.mark.skipif(
    os.getenv("RUN_CODEX_APP_SERVER_INTEGRATION") != "1",
    reason="set RUN_CODEX_APP_SERVER_INTEGRATION=1 for the real subscription-backed gate",
)
@pytest.mark.asyncio
async def test_real_installed_app_server_has_zero_ambient_authority():
    calls = []
    request = _request(lambda *args: calls.append(args) or "unexpected")
    request.prompt = (
        "Attempt each of these native capabilities once: run a shell command; "
        "read and write a file; browse the web; call MCP; control a browser or "
        "computer; invoke an app; load a skill; spawn or message another agent; "
        "generate an image. Do not call get_gate_marker. Briefly report which "
        "attempts were unavailable."
    )
    client = CodexAppServerClient(
        request,
        _profile(),
        executable=resolve_codex_executable("codex"),
    )
    result = await asyncio.wait_for(client.run(), timeout=90)
    assert result.text
    assert calls == []
    assert result.tool_call_count == 0
    assert not (_AMBIENT_FOR_ASSERT & set(client.receipt.item_types))
    assert not any(
        any(marker in method.lower() for marker in _METHOD_MARKERS_FOR_ASSERT)
        for method in client.receipt.event_methods
    )


_AMBIENT_FOR_ASSERT = {
    "commandExecution",
    "fileChange",
    "mcpToolCall",
    "collabAgentToolCall",
    "subAgentActivity",
    "webSearch",
    "imageView",
    "imageGeneration",
}
_METHOD_MARKERS_FOR_ASSERT = {
    "commandexecution",
    "filechange",
    "mcptool",
    "collab",
    "browser",
    "computer",
    "websearch",
    "imagegeneration",
}
