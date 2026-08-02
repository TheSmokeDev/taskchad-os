"""Least-authority Codex app-server transport for caller-supplied tools.

This transport is intentionally separate from :mod:`runtime.openai_codex`.
The existing adapter uses ``codex exec`` and must continue to report that it
cannot carry caller definitions.  App-server's experimental ``dynamicTools``
protocol is a different transport with a different authority boundary.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import base as _base
from .base import RUNTIME_LANE_GENERIC, RuntimeRequest, RuntimeResult, RuntimeToolCall
from .capabilities import TEXT_REASONING, TOOL_REASONING
from .errors import RuntimeConfigError, RuntimeExecutionError, RuntimeRetryableError
from .openai_codex import OpenAICodexRuntime
from .profiles import RuntimeProfile
from .subprocess_env import get_scrubbed_tool_sandbox_env

_logger = logging.getLogger(__name__)

SUPPORTED_CODEX_VERSION = "0.146.0"
_MAX_JSONL_BYTES = 1_000_000
_MAX_SCHEMA_BYTES = 32_768
_MAX_RESULT_CHARS = 8_192
_TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
# Keep this synchronized with the real-binary proof harness in
# codex_app_server_gate.py. A regression test makes drift fail loudly.
_DISABLED_FEATURES = (
    "apps",
    "artifact",
    "auth_elicitation",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "code_mode",
    "code_mode_buffered_exec",
    "code_mode_host",
    "code_mode_only",
    "computer_use",
    "current_time_reminder",
    "default_mode_request_user_input",
    "deferred_executor",
    "deferred_tool_world_state",
    "enable_mcp_apps",
    "exec_permission_approvals",
    "executor_capability_discovery",
    "external_agent_memory_import",
    "goals",
    "guardian_approval",
    "guardianv2",
    "hooks",
    "image_generation",
    "in_app_browser",
    "memories",
    "mcp_2026_07_28",
    "multi_agent",
    "multi_agent_v2",
    "network_proxy",
    "non_prefixed_mcp_tool_names",
    "plugin_sharing",
    "plugins",
    "remote_plugin",
    "request_permissions_tool",
    "respect_system_proxy",
    "realtime_conversation",
    "rollout_budget",
    "runtime_metrics",
    "shell_snapshot",
    "shell_tool",
    "skill_mcp_dependency_install",
    "skill_search",
    "standalone_web_search",
    "token_budget",
    "terminal_visualization_instructions",
    "tool_call_mcp_elicitation",
    "tool_suggest",
    "unified_exec",
    "use_agent_identity",
    "workspace_dependencies",
)
_ALLOWED_ITEM_TYPES = frozenset(
    {"userMessage", "reasoning", "dynamicToolCall", "agentMessage"}
)
_AMBIENT_METHOD_MARKERS = (
    "commandexecution",
    "filechange",
    "mcptoolcall",
    "mcpserver",
    "collab",
    "browser",
    "computer",
    "websearch",
    "imagegeneration",
    "imageview",
    "skills/",
)
_FINAL_PHASES = {None, "final_answer"}


class CodexAppServerProtocolError(RuntimeExecutionError):
    """The child violated the bounded JSONL protocol."""


class CodexAmbientAuthorityError(CodexAppServerProtocolError):
    """A native Codex capability appeared in a caller-tool-only turn."""


@dataclass(slots=True)
class CodexAppServerReceipt:
    """PII-free evidence retained for the gate and runtime result."""

    thread_id: str = ""
    turn_id: str = ""
    model: str = ""
    provider: str = ""
    final_text: str = ""
    tool_calls: list[RuntimeToolCall] = field(default_factory=list)
    event_methods: list[str] = field(default_factory=list)
    item_types: list[str] = field(default_factory=list)
    duration_ms: int = 0


def convert_openai_tool_defs(tool_defs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert an exact OpenAI function snapshot to Codex ``dynamicTools``.

    The conversion is deliberately narrow.  Unsupported shapes are rejected
    before app-server starts, so malformed schemas cannot turn into a provider
    interpretation problem after spend.
    """

    if not isinstance(tool_defs, list) or not tool_defs:
        raise ValueError("tool_defs must be a non-empty list")

    converted: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, definition in enumerate(tool_defs):
        if not isinstance(definition, dict) or definition.get("type") != "function":
            raise ValueError(f"tool_defs[{index}] must be an OpenAI function definition")
        function = definition.get("function")
        if not isinstance(function, dict):
            raise ValueError(f"tool_defs[{index}].function must be an object")

        name = function.get("name")
        description = function.get("description")
        parameters = function.get("parameters")
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
            raise ValueError(f"tool_defs[{index}] has an invalid function name")
        if name in seen:
            raise ValueError(f"duplicate tool definition: {name}")
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"tool {name!r} must have a non-empty description")
        if len(description) > 4_096:
            raise ValueError(f"tool {name!r} description exceeds the size limit")
        if any(ord(char) < 32 and char not in "\t\n\r" for char in description):
            raise ValueError(f"tool {name!r} description contains control characters")
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            raise ValueError(f"tool {name!r} parameters must be an object JSON schema")

        # JSON round-trip detaches the provider payload from caller-owned
        # mutable dicts.  The request snapshot cannot be widened mid-turn.
        try:
            encoded_schema = json.dumps(parameters, ensure_ascii=True, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"tool {name!r} parameters are not JSON serializable") from exc
        if len(encoded_schema.encode("utf-8")) > _MAX_SCHEMA_BYTES:
            raise ValueError(f"tool {name!r} parameter schema exceeds the size limit")
        schema_snapshot = json.loads(encoded_schema)
        converted.append(
            {
                "type": "function",
                "name": name,
                "description": description,
                "inputSchema": schema_snapshot,
                "deferLoading": bool(function.get("deferLoading", False)),
            }
        )
        seen.add(name)
    return converted


def least_authority_args(executable: str) -> list[str]:
    """Return the explicit app-server launch profile proven by issue #281."""

    args = [executable, "app-server", "--stdio", "--strict-config"]
    for feature in _DISABLED_FEATURES:
        args.extend(["--disable", feature])
    # An isolated CODEX_HOME removes user plugins/skills/config.  This explicit
    # empty map is defense in depth and makes accidental MCP inheritance visible
    # if a future Codex release changes merge behavior.
    args.extend(
        [
            "-c",
            "mcp_servers={}",
            "-c",
            'web_search="disabled"',
            "-c",
            "include_apps_instructions=false",
            "-c",
            "include_environment_context=false",
        ]
    )
    return args


def resolve_codex_executable(command: str = "codex") -> str:
    """Resolve the native binary, avoiding the Windows npm wrapper process."""

    resolved = Path(shutil.which(command) or command)
    if sys.platform == "win32" and resolved.suffix.lower() in {".cmd", ".ps1", ""}:
        npm_root = resolved.parent / "node_modules" / "@openai" / "codex"
        native = (
            npm_root
            / "node_modules"
            / "@openai"
            / "codex-win32-x64"
            / "vendor"
            / "x86_64-pc-windows-msvc"
            / "bin"
            / "codex.exe"
        )
        if native.is_file():
            return str(native)
    if resolved.is_file():
        return str(resolved)
    raise RuntimeConfigError(f"Codex CLI not found: {command}")


def _auth_path() -> Path:
    configured = os.environ.get("CODEX_HOME", "").strip()
    root = Path(configured) if configured else Path.home() / ".codex"
    return root / "auth.json"


def _isolated_codex_home() -> tempfile.TemporaryDirectory[str]:
    source = _auth_path()
    if not source.is_file():
        raise RuntimeConfigError("Codex subscription auth is unavailable (auth.json missing)")
    temp_home = tempfile.TemporaryDirectory(prefix="homie-codex-app-server-")
    root = Path(temp_home.name)
    codex_home = root / ".codex"
    codex_home.mkdir()
    (root / "empty").mkdir()
    target = codex_home / "auth.json"
    shutil.copy2(source, target)
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return temp_home


def _child_env(codex_home: Path, home_root: Path) -> dict[str, str]:
    # RuntimeRequest.env may contain integration credentials for Homie-owned
    # handlers. Those handlers execute in the parent, so the model child never
    # receives them.
    env = get_scrubbed_tool_sandbox_env()
    env["CODEX_HOME"] = str(codex_home)
    env["HOME"] = str(home_root)
    env["USERPROFILE"] = str(home_root)
    env.pop("CODEX_CONFIG_DIR", None)
    return env


def _native_item_type(message: dict[str, Any]) -> str | None:
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    item = params.get("item")
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    return item_type if isinstance(item_type, str) else None


def _assert_no_ambient_event(message: dict[str, Any]) -> None:
    method = message.get("method")
    if isinstance(method, str):
        lowered = method.lower()
        if any(marker in lowered for marker in _AMBIENT_METHOD_MARKERS):
            raise CodexAmbientAuthorityError(
                f"Codex emitted forbidden native method {method!r}"
            )
    item_type = _native_item_type(message)
    if item_type is not None and item_type not in _ALLOWED_ITEM_TYPES:
        raise CodexAmbientAuthorityError(
            f"Codex emitted forbidden native item type {item_type!r}"
        )


class CodexAppServerClient:
    """One-process, one-thread, one-turn JSONL client.

    A client instance is single-use.  That property keeps call IDs, tool
    snapshots, and cleanup ownership unambiguous.
    """

    def __init__(
        self,
        request: RuntimeRequest,
        profile: RuntimeProfile,
        *,
        executable: str | None = None,
    ) -> None:
        self.request = request
        self.profile = profile
        self.executable = executable or resolve_codex_executable(profile.command or "codex")
        if request.allowed_tools:
            raise ValueError("Codex app-server refuses provider-owned allowed_tools")
        if request.mcp_servers:
            raise ValueError("Codex app-server refuses caller-supplied MCP servers")
        if request.image_paths:
            raise ValueError("Codex app-server refuses image inputs")
        if request.workspace_write_tools:
            raise ValueError("Codex app-server refuses workspace-write authority")
        self.dynamic_tools = convert_openai_tool_defs(list(request.tool_defs or []))
        if request.tool_dispatch is None:
            raise ValueError("caller-tool request has no tool_dispatch")
        self._dispatch = request.tool_dispatch
        self._allowed_names = frozenset(tool["name"] for tool in self.dynamic_tools)
        self._seen_call_ids: set[str] = set()
        self._next_id = 1
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_lines: list[str] = []
        self._codex_home: tempfile.TemporaryDirectory[str] | None = None
        self._codex_home_path: Path | None = None
        self._empty_cwd: Path | None = None
        self.receipt = CodexAppServerReceipt()

    async def run(self) -> RuntimeResult:
        started = time.monotonic()
        try:
            await self._start()
            await self._initialize()
            thread = await self._start_thread()
            thread_record = thread.get("thread")
            if not isinstance(thread_record, dict) or not isinstance(
                thread_record.get("id"), str
            ):
                raise CodexAppServerProtocolError("thread/start omitted the thread id")
            self.receipt.thread_id = thread_record["id"]
            self.receipt.model = str(thread.get("model") or self.profile.model or "")
            self.receipt.provider = str(
                thread.get("modelProvider") or self.profile.provider or "openai-codex"
            )
            await self._start_turn()
            await self._event_loop()
        except asyncio.CancelledError:
            await self.close()
            raise
        except TimeoutError as exc:
            raise RuntimeRetryableError("Codex app-server turn timed out") from exc
        finally:
            self.receipt.duration_ms = int((time.monotonic() - started) * 1000)
            await self.close()

        if not self.receipt.final_text.strip():
            raise CodexAppServerProtocolError(
                "Codex app-server completed without a final assistant message"
            )
        return RuntimeResult(
            text=self.receipt.final_text.strip(),
            runtime_lane=RUNTIME_LANE_GENERIC,
            provider=self.profile.provider,
            model=self.receipt.model or self.profile.model,
            profile_key=self.profile.key,
            session_id=self.receipt.thread_id,
            tool_call_count=len(self.receipt.tool_calls),
            tool_names_used=[call.name for call in self.receipt.tool_calls],
            tool_calls=list(self.receipt.tool_calls),
            execution_time_ms=self.receipt.duration_ms,
        )

    async def _start(self) -> None:
        self._codex_home = _isolated_codex_home()
        root = Path(self._codex_home.name)
        self._codex_home_path = root / ".codex"
        self._empty_cwd = root / "empty"
        args = least_authority_args(self.executable)
        try:
            self._process = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(self._empty_cwd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_child_env(self._codex_home_path, root),
            )
        except FileNotFoundError as exc:
            raise RuntimeConfigError(f"Codex CLI not found: {self.executable}") from exc
        assert self._process.stderr is not None
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def _read_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        while True:
            line = await self._process.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                self._stderr_lines.append(text)

    async def _send(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise CodexAppServerProtocolError("app-server stdin is unavailable")
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        process.stdin.write(encoded)
        await process.stdin.drain()

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        await self._send({"id": request_id, "method": method, "params": params})
        while True:
            message = await self._read_message()
            _assert_no_ambient_event(message)
            self._record_event(message)
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise RuntimeRetryableError(
                        f"Codex app-server {method} failed: {message['error']}"
                    )
                result = message.get("result")
                if not isinstance(result, dict):
                    raise CodexAppServerProtocolError(
                        f"Codex app-server {method} returned a malformed result"
                    )
                return result
            if "method" in message and "id" in message:
                await self._handle_server_request(message, pre_turn=True)

    async def _read_message(self) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdout is None:
            raise CodexAppServerProtocolError("app-server stdout is unavailable")
        line = await process.stdout.readline()
        if not line:
            detail = " | ".join(self._stderr_lines[-5:]) or "no stderr"
            raise RuntimeRetryableError(
                f"Codex app-server exited before protocol completion: {detail}"
            )
        if len(line) > _MAX_JSONL_BYTES:
            raise CodexAppServerProtocolError("Codex app-server JSONL frame exceeds limit")
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CodexAppServerProtocolError("Codex app-server emitted malformed JSONL") from exc
        if not isinstance(message, dict):
            raise CodexAppServerProtocolError("Codex app-server emitted a non-object message")
        return message

    def _record_event(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if isinstance(method, str):
            self.receipt.event_methods.append(method)
        item_type = _native_item_type(message)
        if item_type:
            self.receipt.item_types.append(item_type)

    async def _initialize(self) -> None:
        initialized = await self._request(
            "initialize",
            {
                "clientInfo": {"name": "the-homie", "version": "2"},
                "capabilities": {"experimentalApi": True},
            },
        )
        user_agent = str(initialized.get("userAgent") or "")
        if f"/{SUPPORTED_CODEX_VERSION} " not in user_agent:
            raise RuntimeConfigError(
                "Codex app-server version has not passed the production gate: "
                f"expected {SUPPORTED_CODEX_VERSION}"
            )
        assert self._codex_home_path is not None
        reported_home = Path(str(initialized.get("codexHome") or ""))
        if os.path.normcase(os.path.abspath(reported_home)) != os.path.normcase(
            os.path.abspath(self._codex_home_path)
        ):
            raise CodexAppServerProtocolError(
                "Codex app-server did not use the isolated CODEX_HOME"
            )
        await self._send({"method": "initialized", "params": {}})

    async def _start_thread(self) -> dict[str, Any]:
        # The child is pointed at its empty temporary home, never at the project
        # or user home. Dynamic tool handlers still execute in the Homie parent.
        assert self._empty_cwd is not None
        empty_cwd = str(self._empty_cwd.resolve())
        persona_instructions = (self.request.system_prompt or "").strip()
        safety_instructions = (
            "Use only the caller-supplied dynamic tools. Native shell, file, "
            "web, MCP, app, skill, browser, computer, image, and collaboration "
            "capabilities are unavailable and must not be attempted. Never "
            "invent a tool result."
        )
        params: dict[str, Any] = {
            "ephemeral": True,
            "cwd": empty_cwd,
            "sandbox": "read-only",
            "approvalPolicy": "never",
            "environments": [],
            "selectedCapabilityRoots": [],
            "runtimeWorkspaceRoots": [],
            "baseInstructions": (
                safety_instructions
                + (
                    "\n\n# Persona Instructions\n" + persona_instructions
                    if persona_instructions
                    else ""
                )
            ),
            "dynamicTools": self.dynamic_tools,
        }
        model = self.request.fallback_model or self.profile.model
        if model and model != "chatgpt-plan-default":
            params["model"] = model
        result = await self._request("thread/start", params)
        if result.get("sandbox") != {"type": "readOnly", "networkAccess": False}:
            raise CodexAppServerProtocolError(
                f"thread sandbox drifted from read-only/no-network: {result.get('sandbox')!r}"
            )
        if result.get("approvalPolicy") != "never":
            raise CodexAppServerProtocolError("thread approval policy drifted from never")
        if result.get("instructionSources", []) != []:
            raise CodexAppServerProtocolError("ambient instruction/skill sources were loaded")
        if result.get("runtimeWorkspaceRoots", []) != []:
            raise CodexAppServerProtocolError("ambient runtime workspace roots were loaded")
        returned_cwd = os.path.normcase(os.path.abspath(str(result.get("cwd") or "")))
        if returned_cwd != os.path.normcase(os.path.abspath(empty_cwd)):
            raise CodexAppServerProtocolError("thread cwd escaped the disposable directory")
        return result

    async def _start_turn(self) -> None:
        params: dict[str, Any] = {
            "threadId": self.receipt.thread_id,
            "input": [{"type": "text", "text": self.request.prompt}],
            "environments": [],
            "runtimeWorkspaceRoots": [],
            "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
        }
        metadata = {"task_name": self.request.task_name}
        if self.request.tool_scope_version:
            metadata["tool_scope_version"] = self.request.tool_scope_version
        persona_id = (self.request.metadata or {}).get("persona_id")
        if isinstance(persona_id, str) and persona_id:
            metadata["persona_id"] = persona_id
        params["responsesapiClientMetadata"] = metadata
        result = await self._request("turn/start", params)
        turn = result.get("turn")
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
            raise CodexAppServerProtocolError("turn/start omitted the turn id")
        self.receipt.turn_id = turn["id"]

    async def _event_loop(self) -> None:
        while True:
            message = await self._read_message()
            _assert_no_ambient_event(message)
            self._record_event(message)

            method = message.get("method")
            if method == "item/tool/call" and "id" in message:
                await self._handle_dynamic_tool_call(message)
                continue
            if "method" in message and "id" in message:
                await self._handle_server_request(message, pre_turn=False)
                continue
            if method == "item/completed":
                self._consume_completed_item(message)
            if method == "turn/completed":
                self._consume_turn_completed(message)
                return

    async def _handle_server_request(
        self, message: dict[str, Any], *, pre_turn: bool
    ) -> None:
        request_id = message.get("id")
        await self._send(
            {
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": f"unexpected server request: {message.get('method')}",
                },
            }
        )
        phase = "before turn start" if pre_turn else "during turn"
        raise CodexAppServerProtocolError(
            f"unexpected server request {message.get('method')!r} {phase}"
        )

    async def _handle_dynamic_tool_call(self, message: dict[str, Any]) -> None:
        params = message.get("params")
        if not isinstance(params, dict):
            raise CodexAppServerProtocolError("dynamic tool call params are malformed")
        call_id = params.get("callId")
        name = params.get("tool")
        arguments = params.get("arguments")
        if params.get("threadId") != self.receipt.thread_id:
            raise CodexAppServerProtocolError("dynamic tool call thread id mismatch")
        if params.get("turnId") != self.receipt.turn_id:
            raise CodexAppServerProtocolError("dynamic tool call turn id mismatch")
        if not isinstance(call_id, str) or not call_id:
            raise CodexAppServerProtocolError("dynamic tool call has no call id")
        if call_id in self._seen_call_ids:
            raise CodexAppServerProtocolError(f"duplicate dynamic tool call id: {call_id}")
        self._seen_call_ids.add(call_id)
        if not isinstance(name, str) or name not in self._allowed_names:
            raise CodexAppServerProtocolError(f"unknown dynamic tool call: {name!r}")
        if not isinstance(arguments, dict):
            raise CodexAppServerProtocolError(
                f"dynamic tool {name!r} arguments must be an object"
            )

        started = time.monotonic()
        success = True
        status = "completed"
        try:
            output = self._dispatch(name, arguments)
            if inspect.isawaitable(output):
                output = await output
            text = output if isinstance(output, str) else json.dumps(output)
        except Exception as exc:  # noqa: BLE001 - provider needs a tool result
            success = False
            status = "failed"
            text = f"{type(exc).__name__}: {exc}"

        if len(text) > _MAX_RESULT_CHARS:
            text = text[: _MAX_RESULT_CHARS - 3] + "..."

        await self._send(
            {
                "id": message["id"],
                "result": {
                    "contentItems": [{"type": "inputText", "text": text}],
                    "success": success,
                },
            }
        )
        self.receipt.tool_calls.append(
            RuntimeToolCall(
                id=call_id,
                name=name,
                arguments=json.loads(json.dumps(arguments)),
                provider_type="dynamicToolCall",
                status=status,
            )
        )
        _logger.debug(
            "Codex dynamic tool %s completed in %dms",
            name,
            int((time.monotonic() - started) * 1000),
        )

    def _consume_completed_item(self, message: dict[str, Any]) -> None:
        params = message.get("params")
        item = params.get("item") if isinstance(params, dict) else None
        if not isinstance(item, dict) or item.get("type") != "agentMessage":
            return
        phase = item.get("phase")
        text = item.get("text")
        if phase in _FINAL_PHASES and isinstance(text, str):
            self.receipt.final_text = text

    def _consume_turn_completed(self, message: dict[str, Any]) -> None:
        params = message.get("params")
        turn = params.get("turn") if isinstance(params, dict) else None
        if not isinstance(turn, dict):
            raise CodexAppServerProtocolError("turn/completed omitted the turn")
        if turn.get("id") != self.receipt.turn_id:
            raise CodexAppServerProtocolError("turn/completed id mismatch")
        status = turn.get("status")
        if status != "completed":
            raise RuntimeRetryableError(
                f"Codex app-server turn ended with status {status!r}: {turn.get('error')}"
            )

    async def close(self) -> None:
        process = self._process
        if process is not None and process.returncode is None:
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=3)
            except Exception:
                _reap_process(process)
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except Exception:
                    _logger.warning("failed to reap Codex app-server pid=%s", process.pid)
        if self._stderr_task is not None:
            if not self._stderr_task.done():
                self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._codex_home is not None:
            self._codex_home.cleanup()
            self._codex_home = None


def _reap_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            _logger.warning("failed to tree-kill Codex app-server pid=%s", process.pid)
        return
    try:
        process.kill()
    except (ProcessLookupError, OSError):
        pass


class OpenAICodexAppServerRuntime(OpenAICodexRuntime):
    """Composite Codex adapter with explicit request-sensitive transport.

    ``codex exec`` remains the path for text and Codex-native tool turns.
    Requests carrying Homie definitions use the isolated app-server path.
    Subclassing preserves the existing provider-registry type contract while
    leaving :class:`OpenAICodexRuntime`'s exec-only capability claim unchanged.
    """

    def supports_caller_tool_defs(self) -> bool:
        return True

    def supports(self, request: RuntimeRequest) -> bool:
        if not _base.request_carries_tools(request):
            return super().supports(request)
        return (
            request.capability in {TEXT_REASONING, TOOL_REASONING}
            and request.resume is None
            and request.tool_dispatch is not None
            and not request.allowed_tools
            and not request.mcp_servers
            and not request.image_paths
            and not request.workspace_write_tools
        )

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        if not _base.request_carries_tools(request):
            return await super().run(request)
        if not self.supports(request):
            raise RuntimeConfigError(
                "Codex app-server requires non-empty tool_defs and tool_dispatch"
            )
        timeout = float(os.getenv("SECOND_BRAIN_CODEX_APP_SERVER_TIMEOUT_S", "120"))
        client = CodexAppServerClient(request, self.profile)
        try:
            return await asyncio.wait_for(client.run(), timeout=timeout)
        except (RuntimeConfigError, CodexAppServerProtocolError):
            raise
        except RuntimeRetryableError:
            raise
        except Exception as exc:
            raise RuntimeRetryableError(f"Codex app-server failed: {exc}") from exc


__all__ = [
    "CodexAmbientAuthorityError",
    "CodexAppServerClient",
    "CodexAppServerProtocolError",
    "CodexAppServerReceipt",
    "OpenAICodexAppServerRuntime",
    "SUPPORTED_CODEX_VERSION",
    "convert_openai_tool_defs",
    "least_authority_args",
    "resolve_codex_executable",
]
