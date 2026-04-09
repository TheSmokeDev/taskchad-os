"""Claude Agent SDK runtime adapter."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

from .base import RuntimeRequest, RuntimeResult, RuntimeToolCall
from .capabilities import TEXT_REASONING, TOOL_REASONING
from .errors import RuntimeConfigError, RuntimeRetryableError, RuntimeUnsupportedCapabilityError
from .profiles import RuntimeProfile


def _system_cli_path() -> str | None:
    """Return the path to the system-installed Claude Code CLI, if available.

    Prefers the cli_path option (supported since SDK 0.1.x) over monkey-patching.
    On Windows the npm global install exposes a Node.js entry point; we return
    that as a two-element list [node, cli.js] encoded as a single string so the
    caller can split on it — but ClaudeAgentOptions.cli_path accepts a Path or
    str, so we return the bare .js path and let the transport pick node.

    Returns None if the system CLI cannot be located.
    """
    if sys.platform == "win32":
        js = Path.home() / "AppData/Roaming/npm/node_modules/@anthropic-ai/claude-code/cli.js"
        if js.exists():
            return str(js)
    else:
        cli = shutil.which("claude")
        if cli:
            return cli
    return None


_PATCHED = False


def _ensure_system_cli_patch() -> None:
    """Patch SubprocessCLITransport to use the system Node.js CLI on Windows.

    The SDK sets self._cli_path in __init__ from _find_cli() before any option
    is applied.  On Windows the bundled claude.exe may fail with FileNotFoundError
    when PATH is minimal (e.g. launched from a bash script), because the exe
    depends on the Node.js runtime being visible on PATH.

    The previous approach only patched _build_command, but _check_claude_version
    also calls anyio.open_process([self._cli_path, "-v"]) directly — so the
    FileNotFoundError was thrown before _build_command was ever reached.

    This patch fixes both call sites by:
      1. Patching _find_cli to return the system cli.js path (so _cli_path is
         set correctly at init time and _check_claude_version uses node+cli.js).
      2. Patching _build_command to prepend `node` so the final subprocess call
         is [node, cli.js, ...args] instead of [cli.js, ...args].
      3. Patching _check_claude_version to use [node, cli.js, "-v"] as well.
    """

    global _PATCHED
    if _PATCHED:
        return

    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    node_path = shutil.which("node") or "node"
    # On Windows, paths with spaces (e.g. "C:\Program Files\nodejs\node.EXE")
    # can cause FileNotFoundError in asyncio's ProactorEventLoop CreateProcess.
    # Use the 8.3 short path to avoid this.
    if sys.platform == "win32" and " " in node_path:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.kernel32.GetShortPathNameW(node_path, buf, 260):
            node_path = buf.value
    node = node_path
    system_js = _system_cli_path()
    if not system_js:
        return

    # 1. Patch _find_cli so self._cli_path is set to cli.js at construction time.
    #    This also fixes _check_claude_version which reads self._cli_path directly.
    def _patched_find_cli(self) -> str:  # type: ignore[no-untyped-def]
        return system_js

    SubprocessCLITransport._find_cli = _patched_find_cli

    # 2. Patch _build_command to prepend node so anyio.open_process gets a real
    #    executable as cmd[0] instead of a .js file.
    original_build = SubprocessCLITransport._build_command

    def _patched_build_command(self):  # type: ignore[no-untyped-def]
        cmd = original_build(self)
        # cmd[0] is now system_js (set by _patched_find_cli above)
        if cmd and cmd[0].endswith(".js"):
            cmd = [node] + cmd
        return cmd

    SubprocessCLITransport._build_command = _patched_build_command

    # 3. Patch _check_claude_version to also prepend node for the version probe.
    import re as _re
    from subprocess import PIPE as _PIPE

    import anyio as _anyio

    async def _patched_check_version(self) -> None:  # type: ignore[no-untyped-def]
        version_process = None
        try:
            with _anyio.fail_after(5):
                version_process = await _anyio.open_process(
                    [node, system_js, "-v"],
                    stdout=_PIPE,
                    stderr=_PIPE,
                )
                if version_process.stdout:
                    stdout_bytes = await version_process.stdout.receive()
                    version_output = stdout_bytes.decode().strip()
                    match = _re.match(r"([0-9]+\.[0-9]+\.[0-9]+)", version_output)
                    if match:
                        from claude_agent_sdk._internal.transport.subprocess_cli import (
                            MINIMUM_CLAUDE_CODE_VERSION,
                        )
                        version = match.group(1)
                        version_parts = [int(x) for x in version.split(".")]
                        min_parts = [int(x) for x in MINIMUM_CLAUDE_CODE_VERSION.split(".")]
                        if version_parts < min_parts:
                            import sys as _sys
                            import logging as _logging
                            _logging.getLogger(__name__).warning(
                                f"Claude Code {version} < minimum {MINIMUM_CLAUDE_CODE_VERSION}"
                            )
                            print(
                                f"Warning: Claude Code {version} < minimum {MINIMUM_CLAUDE_CODE_VERSION}",
                                file=_sys.stderr,
                            )
        except Exception:
            pass
        finally:
            if version_process:
                from contextlib import suppress as _suppress
                with _suppress(Exception):
                    version_process.terminate()
                with _suppress(Exception):
                    await version_process.wait()

    SubprocessCLITransport._check_claude_version = _patched_check_version

    _PATCHED = True


class ClaudeSdkRuntime:
    """Runtime adapter that wraps claude_agent_sdk.query."""

    def __init__(self, profile: RuntimeProfile) -> None:
        self.profile = profile

    def supports(self, request: RuntimeRequest) -> bool:
        if request.capability not in {TEXT_REASONING, TOOL_REASONING}:
            return False
        return True

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        if not self.supports(request):
            raise RuntimeUnsupportedCapabilityError(
                f"Claude runtime does not support capability {request.capability}"
            )

        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            ToolUseBlock,
            query,
        )

        options_kwargs: dict[str, Any] = {
            "cwd": str(request.cwd),
            "max_turns": request.max_turns,
            "allowed_tools": request.allowed_tools,
        }

        # Redirect SDK to system CLI instead of bundled CLI.
        # On Windows, cli.js can't be executed directly — must use monkey-patch
        # which prepends `node` to the command. cli_path only works for native binaries.
        _ensure_system_cli_patch()

        if request.model or self.profile.model:
            options_kwargs["model"] = request.model or self.profile.model
        if request.max_budget_usd is not None:
            options_kwargs["max_budget_usd"] = request.max_budget_usd
        if request.permission_mode is not None:
            options_kwargs["permission_mode"] = request.permission_mode
        if request.setting_sources:
            options_kwargs["setting_sources"] = request.setting_sources
        if request.system_prompt is not None:
            options_kwargs["system_prompt"] = request.system_prompt
        if request.hooks is not None:
            options_kwargs["hooks"] = request.hooks
        if request.thinking is not None:
            options_kwargs["thinking"] = request.thinking
        if request.env is not None:
            options_kwargs["env"] = request.env
        if request.resume is not None:
            options_kwargs["resume"] = request.resume
        if request.stderr is not None:
            options_kwargs["stderr"] = request.stderr

        response_text = ""
        session_id: str | None = None
        cost_usd: float | None = None
        subtype: str | None = None
        tool_call_count = 0
        tool_names_used: list[str] = []
        tool_calls: list[RuntimeToolCall] = []

        try:
            async for message in query(
                prompt=request.prompt,
                options=ClaudeAgentOptions(**options_kwargs),
            ):
                if isinstance(message, AssistantMessage):
                    turn_text = ""
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            turn_text += block.text
                        elif isinstance(block, ToolUseBlock):
                            tool_call_count += 1
                            tool_names_used.append(block.name)
                            tool_calls.append(
                                RuntimeToolCall(
                                    id=getattr(block, "id", ""),
                                    name=block.name,
                                    arguments=getattr(block, "input", None),
                                    provider_type="tool_use",
                                )
                            )
                    if turn_text.strip():
                        response_text = turn_text
                elif isinstance(message, ResultMessage):
                    session_id = message.session_id
                    cost_usd = message.total_cost_usd
                    subtype = message.subtype
                    if message.result:
                        response_text = message.result
        except Exception as exc:
            text = str(exc).lower()
            if any(
                token in text
                for token in ("rate limit", "quota", "429", "unavailable", "overloaded")
            ):
                raise RuntimeRetryableError(str(exc)) from exc
            if "auth" in text or "credential" in text or "login" in text:
                raise RuntimeConfigError(str(exc)) from exc
            raise

        return RuntimeResult(
            text=response_text.strip(),
            provider=self.profile.provider,
            model=request.model or self.profile.model,
            profile_key=self.profile.key,
            session_id=session_id,
            cost_usd=cost_usd,
            subtype=subtype,
            tool_call_count=tool_call_count,
            tool_names_used=tool_names_used,
            tool_calls=tool_calls,
        )
