"""Lane-first runtime orchestration.

PR1 scope:
- preserve existing adapter behavior
- introduce lane-first selection
- keep registry.py as a compatibility shim

PRD-8 Phase 7a WS4 — `requireEnabled("llm")` is invoked at the head of
`run_with_runtime_lanes` so any LLM lane execution is gated by the operator
kill-switch. Module-attribute lookup (Rule 3) — `from security import
kill_switches` then `kill_switches.requireEnabled(...)`. Top-level
`from security.kill_switches import requireEnabled` would defeat
monkeypatch propagation in tests.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from dataclasses import replace

from security import kill_switches

from .base import (
    RUNTIME_LANE_CLAUDE_NATIVE,
    RUNTIME_LANE_GENERIC,
    RuntimeRequest,
    RuntimeResult,
)
from .capabilities import TEXT_REASONING
from .claude_sdk import ClaudeSdkRuntime
from .errors import (
    RuntimeConfigError,
    RuntimeExecutionError,
    RuntimeRetryableError,
    RuntimeUnsupportedCapabilityError,
)
from .gemini_cli import GeminiCliRuntime
from .health import mark_profile_retryable_failure, mark_profile_success, mark_profile_unavailable
from .openai_codex import OpenAICodexRuntime
from .openai_compatible import OpenAICompatibleRuntime
from .profiles import (
    GENERIC_PROVIDER_REGISTRY,
    RuntimeProfile,
    build_profile_for_provider,
)
from .routing import resolve_generic_runtime_profiles
from .selection import resolve_runtime_selection

_logger = logging.getLogger(__name__)

# Per-adapter deadlines resolved at CALL time (Rule 1) — never bound as
# defaults. A wedged provider CLI (a Codex/Gemini child that never exits) or a
# stalled Claude SDK stream otherwise hangs every scheduled pipeline forever
# (heartbeat, reflection, weekly, dream, cabinet, persona learning — 25+ call
# sites with no outer deadline). `asyncio.wait_for` at this one lane chokepoint
# bounds all of them; `<=0` disables the deadline (escape hatch). Issue #133.
_DEFAULT_TIMEOUT_TEXT_S = 300.0
_DEFAULT_TIMEOUT_TOOL_S = 1800.0


def _adapter_timeout_seconds(request: RuntimeRequest) -> float | None:
    """Per-adapter deadline in seconds. ``<=0`` disables (escape hatch)."""
    if request.capability == TEXT_REASONING:
        raw = os.getenv("SECOND_BRAIN_RUNTIME_TIMEOUT_TEXT_SECONDS", "")
        default = _DEFAULT_TIMEOUT_TEXT_S
    else:
        raw = os.getenv("SECOND_BRAIN_RUNTIME_TIMEOUT_TOOL_SECONDS", "")
        default = _DEFAULT_TIMEOUT_TOOL_S
    try:
        value = float(raw) if raw.strip() else default
    except ValueError:
        value = default
    if not math.isfinite(value):
        # float("nan") parses cleanly (no ValueError) but every comparison
        # against NaN is False, so `value > 0` below would silently take the
        # <=0 disable branch instead of falling back to `default`.
        value = default
    return value if value > 0 else None


def resolve_runtime_lane(request: RuntimeRequest) -> str:
    """Choose the top-level runtime lane for a request."""

    if request.runtime_lane:
        return request.runtime_lane
    selection = resolve_runtime_selection()
    if selection.lane:
        return selection.lane
    if request.resume is not None:
        return RUNTIME_LANE_CLAUDE_NATIVE
    return RUNTIME_LANE_GENERIC


def _adapter_for(profile: RuntimeProfile):
    if profile.provider == "claude":
        return ClaudeSdkRuntime(profile)
    overlay = GENERIC_PROVIDER_REGISTRY.get(profile.provider)
    if overlay is not None:
        if overlay.transport == "subprocess_cli":
            # subprocess_cli still dispatches by provider key because
            # OpenAICodexRuntime and GeminiCliRuntime are distinct classes.
            if profile.provider == "openai-codex":
                return OpenAICodexRuntime(profile)
            if profile.provider == "gemini-cli":
                return GeminiCliRuntime(profile)
        if overlay.transport == "openai_responses":
            # openai_responses providers share one adapter class.
            return OpenAICompatibleRuntime(profile)
    raise RuntimeExecutionError(f"Unsupported runtime provider: {profile.provider}")


def _resolve_lane_profiles(request: RuntimeRequest) -> list[RuntimeProfile]:
    lane = resolve_runtime_lane(request)
    if lane == RUNTIME_LANE_CLAUDE_NATIVE:
        profile = build_profile_for_provider("claude", key_prefix="primary", request=request)
        return [profile] if profile else []

    return resolve_generic_runtime_profiles(request)


async def run_with_runtime_lanes(request: RuntimeRequest) -> RuntimeResult:
    """Run a request through the lane-first runtime facade."""

    # PRD-8 Phase 7a WS4 — operator kill-switch. Raises KillSwitchDisabled
    # when HOMIE_KILLSWITCH_LLM=disabled. Callers (engine.py, memory_reflect,
    # memory_weekly, memory_dream) catch this explicitly and degrade cleanly.
    kill_switches.requireEnabled("llm", caller="lane_router")

    lane = resolve_runtime_lane(request)
    effective_request = request
    if lane != RUNTIME_LANE_CLAUDE_NATIVE and request.resume is not None:
        # Runtime resume IDs are Claude-specific. A user-selected generic lane
        # must not be forced back to Claude by a stale Telegram/CLI session.
        effective_request = replace(request, resume=None)
    errors: list[str] = []

    for profile in _resolve_lane_profiles(effective_request):
        adapter = _adapter_for(profile)
        if not adapter.supports(effective_request):
            errors.append(
                f"{profile.key}: unsupported capability {effective_request.capability}"
            )
            continue

        # `wait_for` covers `adapter.run(...)` ONLY — never the health
        # bookkeeping below. On timeout the adapter is cancelled; the CLI
        # adapters reap their child on the way out.
        timeout_s = _adapter_timeout_seconds(effective_request)
        try:
            result = await asyncio.wait_for(adapter.run(effective_request), timeout=timeout_s)
        except RuntimeUnsupportedCapabilityError as exc:
            errors.append(f"{profile.key}: {exc}")
            continue
        except RuntimeRetryableError as exc:
            mark_profile_retryable_failure(profile, str(exc))
            errors.append(f"{profile.key}: retryable error {exc}")
            continue
        except RuntimeConfigError as exc:
            mark_profile_unavailable(profile, str(exc))
            errors.append(f"{profile.key}: unavailable {exc}")
            continue
        except TimeoutError:
            # asyncio.TimeoutError IS builtins.TimeoutError on 3.11+. Must
            # precede `except Exception` (TimeoutError ⊂ OSError ⊂ Exception),
            # else the generic arm mislabels the message. `asyncio.CancelledError`
            # is BaseException, so an external operator/shutdown cancel still
            # propagates untouched past every arm here.
            mark_profile_retryable_failure(profile, f"timed out after {timeout_s}s")
            errors.append(f"{profile.key}: timed out after {timeout_s}s")
            continue
        except Exception as exc:
            mark_profile_retryable_failure(profile, str(exc))
            errors.append(f"{profile.key}: {exc}")
            continue

        # Success bookkeeping stays OUTSIDE the provider try/except: an
        # exception here must never convert a successful run into a provider
        # failure or discard the result (2026-07-16 WinError 32 incident).
        try:
            mark_profile_success(profile)
        except Exception:
            _logger.warning(
                "health bookkeeping failed after successful run for %s",
                profile.key,
                exc_info=True,
            )
        result.runtime_lane = lane
        return result

    joined = "; ".join(errors) if errors else "no runtime profiles resolved"
    message = (
        f"No runtime could satisfy task '{request.task_name}' "
        f"({request.capability}) on lane '{lane}': {joined}"
    )
    raise RuntimeExecutionError(
        message
    )
