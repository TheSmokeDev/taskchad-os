"""OpenAI Codex runtime adapter backed by local ChatGPT subscription auth.

Supports both text-only and tool-capable execution via the Codex CLI.
Text tasks use read-only sandbox. Tool tasks use full sandbox with disk access.
Fallback provider in the chain: Claude → Codex → Gemini.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path

from .auth_profiles import CodexAuthProfile, codex_auth_status
from .base import RuntimeRequest, RuntimeResult, RuntimeToolCall
from .capabilities import TEXT_REASONING, TOOL_REASONING
from .errors import (
    RuntimeConfigError,
    RuntimeExecutionError,
    RuntimeRetryableError,
    RuntimeUnsupportedCapabilityError,
)
from .profiles import RuntimeProfile
from .prompt_builder import render_cli_prompt


class OpenAICodexRuntime:
    """Subscription-backed runtime via local Codex CLI.

    Supports both TEXT_REASONING (read-only sandbox) and TOOL_REASONING
    (full sandbox with disk access). Session resume and hooks are not
    supported — those are Claude-specific features.
    """

    def __init__(self, profile: RuntimeProfile) -> None:
        self.profile = profile

    def supports(self, request: RuntimeRequest) -> bool:
        if request.capability not in {TEXT_REASONING, TOOL_REASONING}:
            return False
        # Session resume is Claude-specific. Hooks are allowed but ignored
        # (the Codex CLI handles its own sandbox/safety).
        if request.resume is not None:
            return False
        return True

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        if not self.supports(request):
            raise RuntimeUnsupportedCapabilityError(
                f"OpenAI Codex runtime does not support capability {request.capability}"
            )

        auth_profile = CodexAuthProfile(
            key=self.profile.auth_profile or "default",
            command=self.profile.command or "codex",
        )
        status = codex_auth_status(auth_profile)
        if not status.available:
            raise RuntimeConfigError(
                "Codex subscription auth is not ready. "
                f"Check `codex login status`. Detail: {status.detail}"
            )

        # Use the profile's own model — request.model is provider-specific
        # (e.g. claude-sonnet-4-6 won't work on Codex)
        model = request.fallback_model or self.profile.model
        prompt_text = render_cli_prompt(request)
        last_message_path = _reserve_output_path()
        command = self.profile.command or "codex"
        # Windows npm shims are .CMD files — resolve full path for subprocess
        resolved = shutil.which(command) or command
        is_tool_task = request.capability == TOOL_REASONING

        # Tool tasks get full sandbox; text tasks stay read-only
        sandbox_mode = "danger-full-access" if is_tool_task else "read-only"

        args = [
            resolved,
            "exec",
            "-",
            "--json",
            "--cd",
            str(request.cwd),
            "--sandbox",
            sandbox_mode,
            "--skip-git-repo-check",
            "--ephemeral",
            "--color",
            "never",
            "--output-last-message",
            str(last_message_path),
        ]
        args.extend(_codex_config_args(request))
        if model and model != "chatgpt-plan-default":
            args.extend(["--model", model])

        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_build_exec_env(request),
            )
            stdout, stderr = await process.communicate(prompt_text.encode("utf-8"))
        except FileNotFoundError as exc:
            raise RuntimeConfigError(f"Codex CLI not found: {command}") from exc
        except Exception as exc:
            raise RuntimeExecutionError(str(exc)) from exc
        finally:
            output_text = _read_last_message(last_message_path)
            try:
                last_message_path.unlink(missing_ok=True)
            except Exception:
                pass

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        event_summary = _parse_codex_json_events(stdout_text)
        combined_output = "\n".join(
            part.strip()
            for part in (
                event_summary["error_text"],
                event_summary["non_json_text"],
                stderr_text,
            )
            if part and part.strip()
        )

        if process.returncode != 0:
            raise _map_codex_error(combined_output or output_text or "Codex exec failed")

        text = output_text.strip()
        if not text:
            raise RuntimeExecutionError(
                f"Codex exec returned no final message. Output: {combined_output or '<empty>'}"
            )

        return RuntimeResult(
            text=text,
            provider=self.profile.provider,
            model=model,
            profile_key=self.profile.key,
            tool_call_count=int(event_summary["tool_call_count"]),
            tool_names_used=list(event_summary["tool_names_used"]),
            tool_calls=list(event_summary["tool_calls"]),
        )



def _build_exec_env(request: RuntimeRequest) -> dict[str, str]:
    """Merge process environment with explicit runtime env overrides."""

    env = dict(os.environ)
    if request.env:
        env.update(request.env)
    return env


def _reserve_output_path() -> Path:
    """Reserve a temp output path without leaving an open file handle on Windows."""

    fd, path = tempfile.mkstemp(prefix="thehomie-codex-", suffix=".txt")
    os.close(fd)
    return Path(path)


def _codex_config_args(request: RuntimeRequest | None = None) -> list[str]:
    """Apply safe Codex CLI overrides for subscription-backed background tasks."""

    reasoning_effort = _codex_reasoning_effort(request)
    return [
        "--config",
        f'model_reasoning_effort="{reasoning_effort}"',
    ]


def _codex_reasoning_effort(request: RuntimeRequest | None = None) -> str:
    """Choose reasoning effort for Codex CLI requests.

    The default remains medium, but tiny one-shot text chat turns do not need
    planner-grade deliberation. Lowering them to ``low`` reduces latency for
    smoke checks and short user-facing replies without changing tool-capable
    or multi-step tasks.
    """

    default_effort = os.getenv("SECOND_BRAIN_CODEX_REASONING_EFFORT", "medium").strip() or "medium"
    if request is None:
        return default_effort

    prompt = request.prompt.strip().lower()
    is_tiny_chat_turn = (
        request.task_name == "chat_turn"
        and request.capability == TEXT_REASONING
        and not request.allowed_tools
        and len(prompt) <= 160
        and any(
            marker in prompt
            for marker in (
                "reply with exactly",
                "reply exactly",
                "nothing else",
            )
        )
    )
    if is_tiny_chat_turn:
        return "low"
    return default_effort


def _read_last_message(path: Path) -> str:
    """Read the last-message output file if present."""

    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _parse_codex_json_events(stdout_text: str) -> dict[str, object]:
    """Extract tool telemetry and error text from Codex JSONL output.

    Codex CLI emits JSONL events when run with ``--json``. The main tool-like
    items we've observed are ``command_execution`` items. We count unique item
    ids so started/completed pairs do not double-count. Error events and any
    non-JSON lines are preserved for diagnostics.
    """

    tool_calls_by_id: dict[str, RuntimeToolCall] = {}
    error_messages: list[str] = []
    non_json_lines: list[str] = []

    for raw_line in stdout_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            non_json_lines.append(line)
            continue

        if event.get("type") == "error":
            message = event.get("message")
            if isinstance(message, str) and message.strip():
                error_messages.append(message.strip())

        item = event.get("item")
        if not isinstance(item, dict):
            continue

        item_type = item.get("type")
        item_id = item.get("id")
        if item_type == "command_execution" and isinstance(item_id, str) and item_id:
            existing = tool_calls_by_id.get(item_id)
            raw_command = item.get("command")
            if _is_internal_framework_command(raw_command):
                continue
            arguments: dict[str, object] | None = None
            if isinstance(raw_command, str) and raw_command.strip():
                arguments = {"command": raw_command.strip()}
            tool_calls_by_id[item_id] = RuntimeToolCall(
                id=item_id,
                name="command_execution",
                arguments=arguments if arguments is not None else (existing.arguments if existing else None),
                provider_type=item_type,
                status=str(item.get("status")) if item.get("status") is not None else (existing.status if existing else None),
            )

    tool_calls = list(tool_calls_by_id.values())
    tool_names_used = sorted({tool.name for tool in tool_calls if tool.name})

    return {
        "tool_call_count": len(tool_calls),
        "tool_names_used": tool_names_used,
        "tool_calls": tool_calls,
        "error_text": "\n".join(error_messages),
        "non_json_text": "\n".join(non_json_lines),
    }


def _is_internal_framework_command(raw_command: object) -> bool:
    """Return True for framework housekeeping commands we do not want to count.

    These commands are infrastructure noise from the local environment, not
    user-facing tool work. Keeping them out prevents inflated tool counts and
    cleaner downstream analytics.
    """

    if not isinstance(raw_command, str):
        return False

    text = raw_command.lower()
    return any(
        marker in text
        for marker in (
            "check_live_chat.py",
            "\\.claude\\hooks\\check_live_chat.py",
            "~/.claude/hooks/check_live_chat.py",
        )
    )


def _map_codex_error(message: str) -> Exception:
    """Map CLI output into structured runtime errors."""

    text = message.lower()
    if any(
        token in text
        for token in ("not logged in", "sign in", "login required", "device auth")
    ):
        return RuntimeConfigError(message)
    if any(token in text for token in ("rate limit", "quota", "429", "usage limit")):
        return RuntimeRetryableError(message)
    return RuntimeExecutionError(message)
