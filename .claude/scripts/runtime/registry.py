"""Runtime selection and fallback execution."""

from __future__ import annotations

from .base import RuntimeRequest, RuntimeResult
from .claude_sdk import ClaudeSdkRuntime
from .errors import (
    RuntimeConfigError,
    RuntimeExecutionError,
    RuntimeRetryableError,
    RuntimeUnsupportedCapabilityError,
)
from .gemini_cli import GeminiCliRuntime
from .health import mark_profile_retryable_failure, mark_profile_success, mark_profile_unavailable
from .langfuse_setup import is_langfuse_enabled
from .openai_codex import OpenAICodexRuntime
from .openai_compatible import OpenAICompatibleRuntime
from .profiles import RuntimeProfile
from .routing import resolve_runtime_profiles


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


async def _run_with_fallback_inner(request: RuntimeRequest) -> RuntimeResult:
    """Core fallback logic — separated so Langfuse can wrap it."""

    errors: list[str] = []

    for profile in resolve_runtime_profiles(request):
        adapter = _adapter_for(profile)
        if not adapter.supports(request):
            errors.append(f"{profile.key}: unsupported capability {request.capability}")
            continue

        try:
            result = await adapter.run(request)
            mark_profile_success(profile)

            # Update Langfuse span with result metadata
            if is_langfuse_enabled():
                try:
                    from langfuse import get_client
                    langfuse = get_client()
                    langfuse.update_current_span(
                        metadata={
                            "provider": result.provider,
                            "model": result.model or "",
                            "profile_key": result.profile_key,
                            "cost_usd": result.cost_usd,
                            "tool_call_count": result.tool_call_count,
                            "tool_names": result.tool_names_used,
                            "task_name": request.task_name,
                            "capability": request.capability,
                        },
                        usage={
                            "total_cost": result.cost_usd or 0.0,
                        },
                        model=result.model or profile.provider,
                    )
                except Exception:
                    pass  # Never let tracing break runtime

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
    raise RuntimeExecutionError(
        f"No runtime could satisfy task '{request.task_name}' ({request.capability}): {joined}"
    )


async def run_with_fallback(request: RuntimeRequest) -> RuntimeResult:
    """Run a request through the resolved runtime plan, with Langfuse tracing."""

    if not is_langfuse_enabled():
        return await _run_with_fallback_inner(request)

    from langfuse import observe
    # Apply @observe dynamically — avoids import-time dependency on langfuse
    traced = observe(name="run_with_fallback", as_type="span")(_run_with_fallback_inner)
    return await traced(request)
