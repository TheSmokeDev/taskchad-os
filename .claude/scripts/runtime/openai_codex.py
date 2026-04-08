"""OpenAI Codex runtime adapter backed by local ChatGPT subscription auth.

Supports both text-only and tool-capable execution via the Codex CLI.
Text tasks use read-only sandbox. Tool tasks use full sandbox with disk access.
Fallback provider in the chain: Claude → Codex → Gemini.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

from .auth_profiles import CodexAuthProfile, codex_auth_status
from .base import RuntimeRequest, RuntimeResult
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
        args.extend(_codex_config_args())
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
        combined_output = "\n".join(
            part.strip() for part in (stdout_text, stderr_text) if part and part.strip()
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


def _codex_config_args() -> list[str]:
    """Apply safe Codex CLI overrides for subscription-backed background tasks."""

    reasoning_effort = (
        os.getenv("SECOND_BRAIN_CODEX_REASONING_EFFORT", "medium").strip() or "medium"
    )
    return [
        "--config",
        f'model_reasoning_effort="{reasoning_effort}"',
    ]


def _read_last_message(path: Path) -> str:
    """Read the last-message output file if present."""

    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


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
