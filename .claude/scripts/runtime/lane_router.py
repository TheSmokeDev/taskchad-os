"""Lane-first runtime orchestration.

PR1 scope:
- preserve existing adapter behavior
- introduce lane-first selection
- keep registry.py as a compatibility shim
"""

from __future__ import annotations

from .base import (
    RUNTIME_LANE_CLAUDE_NATIVE,
    RUNTIME_LANE_GENERIC,
    RuntimeRequest,
    RuntimeResult,
)
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
from .profiles import RuntimeProfile, build_profile_for_provider
from .routing import resolve_generic_runtime_profiles
from .selection import resolve_runtime_selection


def resolve_runtime_lane(request: RuntimeRequest) -> str:
    """Choose the top-level runtime lane for a request."""

    if request.runtime_lane:
        return request.runtime_lane
    if request.resume is not None:
        return RUNTIME_LANE_CLAUDE_NATIVE
    selection = resolve_runtime_selection()
    if selection.lane:
        return selection.lane
    return RUNTIME_LANE_GENERIC


def _adapter_for(profile: RuntimeProfile):
    if profile.provider == "claude":
        return ClaudeSdkRuntime(profile)
    if profile.provider == "gemini-cli":
        return GeminiCliRuntime(profile)
    if profile.provider == "openai-codex":
        return OpenAICodexRuntime(profile)
    if profile.provider == "openrouter":
        return OpenAICompatibleRuntime(profile)
    if profile.provider == "openai-compatible":
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

    lane = resolve_runtime_lane(request)
    errors: list[str] = []

    for profile in _resolve_lane_profiles(request):
        adapter = _adapter_for(profile)
        if not adapter.supports(request):
            errors.append(f"{profile.key}: unsupported capability {request.capability}")
            continue

        try:
            result = await adapter.run(request)
            mark_profile_success(profile)
            result.runtime_lane = lane
            return result
        except RuntimeUnsupportedCapabilityError as exc:
            errors.append(f"{profile.key}: {exc}")
        except RuntimeRetryableError as exc:
            mark_profile_retryable_failure(profile, str(exc))
            errors.append(f"{profile.key}: retryable error {exc}")
            continue
        except RuntimeConfigError as exc:
            mark_profile_unavailable(profile, str(exc))
            errors.append(f"{profile.key}: unavailable {exc}")
            continue
        except Exception as exc:
            errors.append(f"{profile.key}: {exc}")
            break

    joined = "; ".join(errors) if errors else "no runtime profiles resolved"
    message = (
        f"No runtime could satisfy task '{request.task_name}' "
        f"({request.capability}) on lane '{lane}': {joined}"
    )
    raise RuntimeExecutionError(
        message
    )
