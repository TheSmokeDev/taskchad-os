"""Adversarial tests for the real Codex app-server dynamicTools gate (#281)."""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

import runtime.codex_app_server_gate as gate
from runtime import tool_registry
from runtime.base import RuntimeRequest

TOOL_NAME = "test_codex_gate_echo_281"
TOKEN = "GATE-281-LIVE-9C4E"
TOOLSET_REGISTRY = {
    "codex_gate_test": {
        "tools": [TOOL_NAME],
        "includes": [],
    }
}


@pytest.fixture
def tool_definition() -> dict[str, Any]:
    tool_registry.register_tool(
        TOOL_NAME,
        "Return the supplied nonce exactly.",
        toolset="codex_gate_test",
        parameters={
            "type": "object",
            "properties": {"nonce": {"type": "string"}},
            "required": ["nonce"],
            "additionalProperties": False,
        },
        handler=lambda nonce: nonce,
        effect="read",
    )
    try:
        definitions = tool_registry.get_tool_definitions(
            enabled_toolsets=["codex_gate_test"],
            registry=TOOLSET_REGISTRY,
        )
        assert len(definitions) == 1
        yield definitions[0]
    finally:
        tool_registry.unregister_tool(TOOL_NAME)


def _request(
    tool_definition: dict[str, Any],
    dispatch,
    **overrides: Any,
) -> RuntimeRequest:
    values: dict[str, Any] = {
        "prompt": f"Call the tool with nonce {TOKEN}.",
        "cwd": Path.cwd(),
        "task_name": "codex_app_server_gate_test",
        "tool_defs": [tool_definition],
        "tool_dispatch": dispatch,
    }
    values.update(overrides)
    return RuntimeRequest(**values)


def _state(tool_definition: dict[str, Any]) -> gate._GateState:
    function = tool_definition["function"]
    return gate._GateState(
        tool_name=function["name"],
        input_schema=function["parameters"],
        thread_id="thread-281",
        turn_id="turn-281",
    )


def _tool_call(
    *,
    request_id: int = 40,
    call_id: str = "call-281",
    tool: str = TOOL_NAME,
    arguments: Any = None,
    method: str = "item/tool/call",
) -> dict[str, Any]:
    if arguments is None:
        arguments = {"nonce": TOKEN}
    return {
        "id": request_id,
        "method": method,
        "params": {
            "threadId": "thread-281",
            "turnId": "turn-281",
            "callId": call_id,
            "namespace": None,
            "tool": tool,
            "arguments": arguments,
        },
    }


def test_gate_contract_translates_one_real_registry_definition(
    tool_definition: dict[str, Any],
) -> None:
    request = _request(tool_definition, lambda _name, _args: TOKEN)

    translated = gate._validate_gate_request(request)

    assert translated == {
        "type": "function",
        "name": TOOL_NAME,
        "description": "Return the supplied nonce exactly.",
        "inputSchema": tool_definition["function"]["parameters"],
    }
    assert translated["inputSchema"] is not tool_definition["function"]["parameters"]


@pytest.mark.parametrize(
    "request_change, message",
    [
        ({"tool_defs": []}, "exactly one"),
        ({"tool_dispatch": None}, "callable"),
        ({"allowed_tools": ["Read"]}, "allowed_tools"),
        ({"mcp_servers": ["ambient"]}, "MCP"),
        ({"image_paths": ["secret.png"]}, "image"),
        ({"workspace_write_tools": True}, "workspace-write"),
    ],
)
def test_gate_contract_rejects_ambient_or_incomplete_requests_before_runtime(
    tool_definition: dict[str, Any],
    request_change: dict[str, Any],
    message: str,
) -> None:
    request = _request(
        tool_definition,
        lambda _name, _args: TOKEN,
        **request_change,
    )

    with pytest.raises(ValueError, match=message):
        gate._validate_gate_request(request)


def test_missing_experimental_opt_in_fails_before_protocol_work() -> None:
    with pytest.raises(ValueError, match="experimentalApi=true"):
        gate._initialize_request(experimental_api=False)


def test_unsupported_experimental_thread_start_fails_closed() -> None:
    message = {
        "id": 2,
        "error": {
            "code": -32600,
            "message": "dynamicTools is experimental and unsupported",
        },
    }

    with pytest.raises(gate._ProtocolViolation, match="failed closed"):
        gate._require_response(message, 2, "thread/start")


@pytest.mark.asyncio
async def test_unknown_dynamic_tool_name_never_dispatches(
    tool_definition: dict[str, Any],
) -> None:
    calls: list[tuple[str, Any]] = []
    request = _request(
        tool_definition,
        lambda name, args: calls.append((name, args)),
    )

    with pytest.raises(gate._ProtocolViolation, match="unknown dynamic tool"):
        await gate._handle_dynamic_tool_call(
            _state(tool_definition),
            request,
            _tool_call(tool="ambient_shell"),
        )

    assert calls == []


@pytest.mark.parametrize(
    "arguments, expected",
    [
        ("not-an-object", "must be an object"),
        ({}, "missing required"),
        ({"nonce": 281}, "wrong type"),
        ({"nonce": TOKEN, "extra": True}, "unknown fields"),
    ],
)
@pytest.mark.asyncio
async def test_malformed_dynamic_tool_arguments_never_dispatch(
    tool_definition: dict[str, Any],
    arguments: Any,
    expected: str,
) -> None:
    calls: list[tuple[str, Any]] = []
    request = _request(
        tool_definition,
        lambda name, args: calls.append((name, args)),
    )

    with pytest.raises(gate._ProtocolViolation, match=expected):
        await gate._handle_dynamic_tool_call(
            _state(tool_definition),
            request,
            _tool_call(arguments=arguments),
        )

    assert calls == []


@pytest.mark.asyncio
async def test_duplicate_call_id_cannot_dispatch_twice(
    tool_definition: dict[str, Any],
) -> None:
    calls: list[tuple[str, Any]] = []

    def dispatch(name: str, arguments: Any) -> str:
        calls.append((name, arguments))
        return TOKEN

    request = _request(tool_definition, dispatch)
    state = _state(tool_definition)
    first = await gate._handle_dynamic_tool_call(
        state,
        request,
        _tool_call(),
    )

    with pytest.raises(gate._ProtocolViolation, match="duplicate"):
        await gate._handle_dynamic_tool_call(
            state,
            request,
            _tool_call(),
        )

    assert first["result"]["contentItems"][0]["text"] == TOKEN
    assert calls == [(TOOL_NAME, {"nonce": TOKEN})]


@pytest.mark.asyncio
async def test_unexpected_server_request_never_dispatches(
    tool_definition: dict[str, Any],
) -> None:
    calls: list[tuple[str, Any]] = []
    request = _request(
        tool_definition,
        lambda name, args: calls.append((name, args)),
    )

    with pytest.raises(gate._ProtocolViolation, match="unexpected server request"):
        await gate._handle_dynamic_tool_call(
            _state(tool_definition),
            request,
            _tool_call(method="item/commandExecution/requestApproval"),
        )

    assert calls == []


@pytest.mark.asyncio
async def test_dispatch_runtime_failure_is_swallowed_with_receipt(
    tool_definition: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    def dispatch(_name: str, _arguments: Any) -> str:
        raise RuntimeError("sentinel dispatch failure")

    with pytest.raises(gate._ProtocolViolation, match="no result was trusted"):
        await gate._handle_dynamic_tool_call(
            _state(tool_definition),
            _request(tool_definition, dispatch),
            _tool_call(),
        )

    receipt = json.loads(capsys.readouterr().err)
    assert receipt["codex_app_server_gate"] == "tool_dispatch_failure"
    assert receipt["verdict"] == "FALSIFIED"


@pytest.mark.parametrize(
    "item_type",
    [
        "commandExecution",
        "fileChange",
        "mcpToolCall",
        "collabAgentToolCall",
        "webSearch",
        "imageView",
        "imageGeneration",
        "subAgentActivity",
    ],
)
def test_every_native_authority_event_falsifies_the_gate(
    tool_definition: dict[str, Any],
    item_type: str,
) -> None:
    state = _state(tool_definition)

    with pytest.raises(gate._ProtocolViolation, match="ambient native authority"):
        gate._record_notification(
            state,
            {
                "method": "item/started",
                "params": {"item": {"id": "native-1", "type": item_type}},
            },
        )

    assert state.native_event_types == [item_type]


def test_process_args_disable_every_ambient_authority_family() -> None:
    args = gate._app_server_args("codex")
    disabled = {
        args[index + 1]
        for index, value in enumerate(args)
        if value == "--disable"
    }

    assert {
        "apps",
        "browser_use",
        "computer_use",
        "image_generation",
        "in_app_browser",
        "multi_agent",
        "plugins",
        "shell_tool",
        "skill_search",
        "standalone_web_search",
    } <= disabled
    assert "mcp_servers={}" in args
    assert 'web_search="disabled"' in args
    assert "--strict-config" in args


def test_isolated_home_physically_seeds_only_auth(
    tmp_path: Path,
) -> None:
    (tmp_path / "auth.json").write_text('{"auth_mode":"test"}', encoding="utf-8")
    (tmp_path / "config.toml").write_text("[mcp_servers.ambient]", encoding="utf-8")
    prepared = gate._prepare_isolated_runtime(
        codex_command="codex",
        auth_home=tmp_path,
    )
    try:
        assert prepared.seed_files == ("auth.json",)
        assert not (prepared.codex_home / "config.toml").exists()
        assert prepared.env["CODEX_HOME"] == str(prepared.codex_home)
        assert prepared.env["HOME"] == str(prepared.root)
        assert prepared.env["USERPROFILE"] == str(prepared.root)
        assert list(prepared.empty_cwd.iterdir()) == []
    finally:
        prepared.cleanup()


class _FakeWriter:
    def __init__(self) -> None:
        self.closed = False
        self.messages: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.messages.append(data)

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _HangingReader:
    def __init__(self, started: asyncio.Event) -> None:
        self.started = started

    async def readline(self) -> bytes:
        self.started.set()
        await asyncio.sleep(3_600)
        return b""


class _EmptyReader:
    async def readline(self) -> bytes:
        return b""


class _FakeProcess:
    def __init__(self, started: asyncio.Event) -> None:
        self.pid = 424281
        self.returncode: int | None = None
        self.stdin = _FakeWriter()
        self.stdout = _HangingReader(started)
        self.stderr = _EmptyReader()

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(3_600)
        return self.returncode


def _prepared_for_process_test() -> gate._PreparedRuntime:
    temp_dir = tempfile.TemporaryDirectory(prefix="codex-gate-process-test-")
    root = Path(temp_dir.name)
    codex_home = root / ".codex"
    empty_cwd = root / "empty"
    codex_home.mkdir()
    empty_cwd.mkdir()
    return gate._PreparedRuntime(
        temporary_directory=temp_dir,
        root=root,
        codex_home=codex_home,
        empty_cwd=empty_cwd,
        env={},
        command="codex",
        seed_files=("auth.json",),
    )


@pytest.mark.asyncio
async def test_timeout_reaps_app_server_child(
    tool_definition: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    process = _FakeProcess(started)
    reaped: list[int] = []

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return process

    async def fake_terminate(child) -> None:
        reaped.append(child.pid)
        child.returncode = -9

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(gate, "_terminate_process_tree", fake_terminate)
    prepared = _prepared_for_process_test()
    try:
        receipt = await gate._run_protocol(
            _request(tool_definition, lambda _name, _args: TOKEN),
            gate._validate_gate_request(
                _request(tool_definition, lambda _name, _args: TOKEN)
            ),
            prepared,
            timeout_s=0.01,
        )
    finally:
        prepared.cleanup()

    assert started.is_set()
    assert receipt.verdict == "FALSIFIED"
    assert "timed out" in receipt.reason
    assert reaped == [424281]


@pytest.mark.asyncio
async def test_cancellation_reaps_app_server_child_before_propagating(
    tool_definition: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    process = _FakeProcess(started)
    reaped: list[int] = []

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return process

    async def fake_terminate(child) -> None:
        reaped.append(child.pid)
        child.returncode = -9

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(gate, "_terminate_process_tree", fake_terminate)
    prepared = _prepared_for_process_test()
    task = asyncio.create_task(
        gate._run_protocol(
            _request(tool_definition, lambda _name, _args: TOKEN),
            gate._validate_gate_request(
                _request(tool_definition, lambda _name, _args: TOKEN)
            ),
            prepared,
            timeout_s=120,
        )
    )
    try:
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        prepared.cleanup()

    assert reaped == [424281]


@pytest.mark.asyncio
async def test_windows_reaper_kills_the_entire_wrapper_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(asyncio.Event())
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        process.returncode = -9
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(gate.sys, "platform", "win32")
    monkeypatch.setattr(gate.subprocess, "run", fake_run)

    await gate._terminate_process_tree(process)

    assert calls == [["taskkill", "/T", "/F", "/PID", "424281"]]


@pytest.mark.asyncio
async def test_real_installed_app_server_dynamic_tool_has_zero_ambient_authority(
    tool_definition: dict[str, Any],
) -> None:
    """The non-vacuous acceptance proof: real binary, auth, model, and JSONL."""

    calls: list[tuple[str, Any]] = []

    def dispatch(name: str, arguments: dict[str, Any]) -> str:
        calls.append((name, arguments))
        assert name == TOOL_NAME
        assert arguments == {"nonce": TOKEN}
        return TOKEN

    receipt = await gate.run_codex_app_server_gate(
        _request(tool_definition, dispatch),
        timeout_s=120,
    )

    assert receipt.verdict == "PROVEN", receipt.to_dict()
    assert receipt.codex_version == "0.146.0"
    assert "/0.146.0 " in receipt.user_agent
    assert receipt.sandbox == {"type": "readOnly", "networkAccess": False}
    assert receipt.isolated_seed_files == ("auth.json",)
    assert receipt.tool_call_count == 1
    assert calls == [(TOOL_NAME, {"nonce": TOKEN})]
    assert receipt.dispatch_result == TOKEN
    assert TOKEN in receipt.final_answer
    assert receipt.native_event_types == ()
    assert set(receipt.observed_item_types) == {
        "userMessage",
        "reasoning",
        "dynamicToolCall",
        "agentMessage",
    }
