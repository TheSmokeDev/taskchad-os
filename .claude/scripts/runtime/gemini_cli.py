"""Gemini CLI runtime adapter backed by local Google-account auth.

Supports both text-only and tool-capable execution via the Gemini CLI.
Text tasks run as one-shot prompts. Tool tasks use --yolo (auto-approve tools).
Fallback provider in the chain: Claude → Codex → Gemini.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys

from .auth_profiles import GeminiAuthProfile, gemini_auth_status
from .base import RUNTIME_LANE_GENERIC, RuntimeRequest, RuntimeResult
from .capabilities import TEXT_REASONING, TOOL_REASONING
from .errors import (
    RuntimeConfigError,
    RuntimeExecutionError,
    RuntimeRetryableError,
    RuntimeUnsupportedCapabilityError,
)
from .profiles import RuntimeProfile
from .prompt_builder import GEMINI_GUIDANCE, render_cli_prompt
from .subprocess_env import get_scrubbed_tool_sandbox_env

_logger = logging.getLogger(__name__)


class GeminiCliRuntime:
    """Subscription-backed runtime via local Gemini CLI.

    Supports both TEXT_REASONING (one-shot prompt) and TOOL_REASONING
    (yolo mode with full tool access). Session resume and hooks are not
    supported — those are Claude-specific features.
    """

    def __init__(self, profile: RuntimeProfile) -> None:
        self.profile = profile

    def supports(self, request: RuntimeRequest) -> bool:
        if request.capability not in {TEXT_REASONING, TOOL_REASONING}:
            return False
        # Session resume is Claude-specific. Hooks are allowed but ignored
        # (the Gemini CLI handles its own tool approval/safety).
        if request.resume is not None:
            return False
        return True

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        if not self.supports(request):
            raise RuntimeUnsupportedCapabilityError(
                f"Gemini CLI runtime does not support capability {request.capability}"
            )

        auth_profile = GeminiAuthProfile(
            key=self.profile.auth_profile or "oauth-personal",
            command=self.profile.command or "gemini",
            auth_type=self.profile.auth_profile or "oauth-personal",
        )
        status = gemini_auth_status(auth_profile)
        if not status.available:
            raise RuntimeConfigError(
                "Gemini auth is not ready. "
                f"Check the local Gemini CLI login. Detail: {status.detail}"
            )

        prompt_text = render_cli_prompt(request, model_guidance=GEMINI_GUIDANCE)
        command = self.profile.command or "gemini"
        # Windows npm shims are .CMD files — resolve full path for subprocess
        resolved = shutil.which(command) or command
        candidate_models = _candidate_models(self.profile, request)
        errors: list[str] = []

        is_tool_task = request.capability == TOOL_REASONING

        # Build env with correct GCP project + integration-relevant vars.
        # Issue #128 — scrubbed base env, never a raw os.environ copy.
        # gate/#140 — merge request.env into the parent env BEFORE the single
        # scrub, so a secret-shaped key arriving via request.env is dropped
        # instead of surviving; non-secret overrides (HOMIE_HOME, GOOGLE_CLOUD_
        # PROJECT) still pass through. The GOOGLE_CLOUD_PROJECT auto-injection
        # runs LAST (non-secret, must still win / be injected).
        base = os.environ.copy()
        if request.env:
            base.update(request.env)
        env = get_scrubbed_tool_sandbox_env(parent_env=base)
        gcp_project = (
            base.get("GOOGLE_CLOUD_PROJECT")
            or os.getenv("GOOGLE_CLOUD_PROJECT", "")
        ).strip()
        if gcp_project:
            env["GOOGLE_CLOUD_PROJECT"] = gcp_project

        for model in candidate_models:
            args = [resolved, "--model", model]

            if is_tool_task:
                # Visual evidence analysis is deliberately read-only. Gemini
                # receives exact paths in the prompt and may only use read_file.
                if request.read_only_tools:
                    args.extend([
                        "--approval-mode", "default",
                        "--allowed-tools", "read_file",
                        "--output-format", "text", "-",
                    ])
                elif request.workspace_write_tools:
                    args.extend([
                        "--approval-mode", "auto_edit",
                        "--allowed-tools", "read_file,write_file,replace,glob,grep_search,list_directory",
                        "--output-format", "text", "-",
                    ])
                else:
                    # Prompt via stdin (dash arg) — no CLI arg length limit
                    args.extend(["--yolo", "--output-format", "text", "-"])
            else:
                args.extend(["--output-format", "text", "-"])

            process = None
            try:
                process = await asyncio.create_subprocess_exec(
                    *args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(request.cwd),
                    env=env,
                )
                stdout, stderr = await process.communicate(prompt_text.encode("utf-8"))
            except asyncio.CancelledError:
                # The lane-level wait_for (or an operator cancel) fired mid-run.
                # Cancelling communicate() does NOT kill the child — the orphaned
                # Gemini CLI keeps running and its pipes keep the transport alive.
                # Kill, reap (bounded), re-raise. Do NOT advance the model ladder
                # on cancel — the deadline is per-adapter, not per-model.
                _reap_process(process)
                if process is not None:
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except Exception:
                        _logger.warning(
                            "failed to reap cancelled Gemini subprocess pid=%s after kill()",
                            getattr(process, "pid", "?"),
                            exc_info=True,
                        )
                raise
            except FileNotFoundError as exc:
                raise RuntimeConfigError(f"Gemini CLI not found: {command}") from exc
            except Exception as exc:
                raise RuntimeExecutionError(str(exc)) from exc

            stdout_text = stdout.decode("utf-8", errors="replace")
            stderr_text = stderr.decode("utf-8", errors="replace")
            combined_output = "\n".join(
                part.strip() for part in (stdout_text, stderr_text) if part and part.strip()
            )

            if process.returncode != 0:
                mapped = _map_gemini_error(combined_output or "Gemini CLI execution failed")
                errors.append(f"{model}: {mapped}")
                if _should_try_next_model(mapped, combined_output, model, candidate_models):
                    continue
                raise mapped

            text = _extract_response_text(stdout_text)
            if text:
                return RuntimeResult(
                    text=text,
                    runtime_lane=RUNTIME_LANE_GENERIC,
                    provider=self.profile.provider,
                    model=model,
                    profile_key=self.profile.key,
                )

            errors.append(f"{model}: Gemini CLI returned no final text")

        if errors:
            raise RuntimeRetryableError("; ".join(errors))
        raise RuntimeExecutionError("Gemini CLI execution failed with no candidate models")



def _reap_process(process: asyncio.subprocess.Process | None) -> None:
    """Best-effort kill of a cancelled adapter child. Never raises.

    Duplicated per CLI adapter on purpose — a shared module for two call sites
    would be premature abstraction (Rule: no generic helper before a third use).
    """
    if process is None or process.returncode is not None:
        return
    if sys.platform == "win32":
        # The CLI resolves to an npm `.CMD` wrapper: process.kill() terminates
        # only the wrapper while its Node.js descendant — the actual wedged
        # CLI — survives (#133 gate). taskkill /T tears down the whole tree.
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            _logger.warning(
                "failed to tree-kill cancelled Gemini subprocess pid=%s",
                getattr(process, "pid", "?"),
                exc_info=True,
            )
        return
    try:
        process.kill()
    except (ProcessLookupError, OSError):
        _logger.warning(
            "failed to kill cancelled Gemini subprocess pid=%s",
            getattr(process, "pid", "?"),
            exc_info=True,
        )


def _candidate_models(profile: RuntimeProfile, request: RuntimeRequest) -> tuple[str, ...]:
    """Resolve the ordered Gemini model ladder for a request."""

    # Use the profile's own model — request.model AND request.fallback_model are
    # provider-specific (e.g. claude-sonnet-4-6 / gpt-4.1-mini won't work on
    # Gemini), so the gemini lane ignores them and uses its resolved model.
    primary = profile.model
    ladder = list(profile.candidate_models) or [profile.model]
    return tuple(
        model
        for index, model in enumerate([primary, *ladder])
        if model and model not in [primary, *ladder][:index]
    )


def _extract_response_text(stdout_text: str) -> str:
    """Strip Gemini CLI boilerplate from a successful text response."""

    cleaned = stdout_text.strip()
    if not cleaned:
        return ""

    if cleaned.startswith("{"):
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            text = payload.get("response") or payload.get("text") or payload.get("content")
            if isinstance(text, str) and text.strip():
                return text.strip()

    ignore_prefixes = (
        "Loaded cached credentials.",
        "Data collection is disabled.",
    )
    lines = [
        line
        for line in cleaned.splitlines()
        if line.strip() and not line.strip().startswith(ignore_prefixes)
    ]
    return "\n".join(lines).strip()


def _map_gemini_error(message: str) -> Exception:
    """Map Gemini CLI output into structured runtime errors."""

    text = message.lower()
    if any(
        token in text
        for token in (
            "429",
            "resource_exhausted",
            "rate limit",
            "ratelimitexceeded",
            "no capacity available",
            "capacity exhausted",
        )
    ):
        return RuntimeRetryableError(message)
    if any(
        token in text
        for token in (
            "login",
            "authenticate",
            "not logged in",
            "auth type",
            "credential file",
            "invalid_grant",
            "permission denied",
            "forbidden",
            "iam_permission_denied",
        )
    ):
        return RuntimeConfigError(message)
    return RuntimeExecutionError(message)


def _should_try_next_model(
    exc: Exception,
    message: str,
    model: str,
    candidate_models: tuple[str, ...],
) -> bool:
    """Return True when Gemini should advance to the next model in the ladder."""

    if model == candidate_models[-1]:
        return False
    if isinstance(exc, RuntimeRetryableError):
        return True

    text = message.lower()
    return any(
        token in text
        for token in (
            "model not found",
            "not found for api version",
            "unsupported model",
            "not available on the server",
            "preview models are only available",
        )
    )
