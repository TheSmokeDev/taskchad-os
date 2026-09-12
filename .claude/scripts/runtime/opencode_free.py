"""Anonymous OpenCode Free contract. No discovery, credentials, or paid fallback."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from .base import RUNTIME_LANE_GENERIC, RuntimeRequest
from .errors import RuntimeConfigError, RuntimeExecutionError, RuntimeUnsupportedCapabilityError

FREE_PROVIDER = "opencode-free"
FREE_BASE_URL = "https://opencode.ai/zen/v1"
FREE_DEFAULT_MODEL = "deepseek-v4-flash-free"
FREE_SDK_KEY = "opencode-free-keyless"
FREE_PROBE_TIMEOUT = 30.0


class FreeRuntimeError(RuntimeExecutionError):
    """Terminal refusal, deliberately NOT a caller-tool-degradation signal."""

    def __init__(self, code: str, detail: str = "Free request failed") -> None:
        self.code = code
        super().__init__(f"{detail} — {code}. No paid fallback was attempted. Try again later.")


def error_code(exc: BaseException) -> str:
    """Expose a useful code, never a provider response body or private prompt."""
    if isinstance(exc, FreeRuntimeError):
        return exc.code
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return f"HTTP {status}"
    if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
        return "TIMEOUT"
    if isinstance(exc, RuntimeUnsupportedCapabilityError):
        return "UNSUPPORTED_CAPABILITY"
    if isinstance(exc, (RuntimeConfigError, ValueError)):
        return "INVALID_CONFIGURATION"
    if "connect" in type(exc).__name__.lower():
        return "CONNECTION_ERROR"
    return "PROVIDER_ERROR"


def requires_free(request: RuntimeRequest) -> bool:
    from .profiles import normalize_provider
    from .selection import resolve_runtime_selection

    return (
        resolve_runtime_selection().generic_provider == FREE_PROVIDER
        or normalize_provider(request.preferred_provider or "") == FREE_PROVIDER
        or bool((request.metadata or {}).get("free_only"))
    )


def bind_free_request(request: RuntimeRequest) -> RuntimeRequest:
    """Pin before capability/route filtering; preserve the pin across retries."""
    if not requires_free(request):
        return request
    from .model_control import configured_model_for_provider
    from .profiles import normalize_provider

    preferred = normalize_provider(request.preferred_provider or "")
    if (preferred and preferred != FREE_PROVIDER) or (
        request.runtime_lane and request.runtime_lane != RUNTIME_LANE_GENERIC
    ):
        raise FreeRuntimeError("FREE_ONLY_CONFLICT", "Paid or other-lane override refused")
    if (
        request.allowed_tools
        or request.hooks
        or request.mcp_servers
        or request.read_only_tools
        or request.workspace_write_tools
    ):
        raise FreeRuntimeError(
            "UNSUPPORTED_CAPABILITY", "Free cannot execute provider-native tools or hooks"
        )
    # A stale CLI resume ID is not a portable conversation history. Refuse rather
    # than silently executing elsewhere or pretending the resume was honored.
    if request.resume is not None:
        raise FreeRuntimeError("UNSUPPORTED_RESUME", "Free cannot resume a provider-native session")
    model = (
        request.model
        if preferred == FREE_PROVIDER and request.model
        else configured_model_for_provider(FREE_PROVIDER)
    )
    return replace(
        request,
        runtime_lane=RUNTIME_LANE_GENERIC,
        preferred_provider=FREE_PROVIDER,
        model=model,
        allow_fallback=False,
        metadata={**(request.metadata or {}), "free_only": True},
    )


def validate_free_profile(profile) -> None:
    """Only this fixed, uncredentialed endpoint has a zero-dollar contract."""
    if (
        profile.provider != FREE_PROVIDER
        or profile.base_url != FREE_BASE_URL
        or profile.api_key != FREE_SDK_KEY
        or profile.auth_profile
    ):
        raise FreeRuntimeError("INVALID_CONFIGURATION", "Anonymous Free endpoint required")
    model = profile.model or ""
    if not model or any(c.isspace() for c in model) or "/" in model:
        raise FreeRuntimeError("INVALID_MODEL", "Invalid Free model identifier")
    # The relay, not a suffix heuristic, determines anonymous availability.
    # Unsupported/paid model IDs fail anonymously; no account can be charged.


def free_client_kwargs(profile) -> dict:
    """Do not inherit SDK account headers, proxy credentials, or redirects."""
    import httpx

    validate_free_profile(profile)
    return {
        "api_key": FREE_SDK_KEY,
        "base_url": FREE_BASE_URL,
        "organization": "",
        "project": "",
        "default_headers": {"Authorization": "", "X-Title": "The Homie"},
        "max_retries": 0,
        "http_client": httpx.AsyncClient(trust_env=False, follow_redirects=False),
    }


async def probe_free_model(model: str) -> None:
    """Bounded synthetic-only inference. Never invoke the Homie prompt pipeline."""
    import httpx

    from security import kill_switches

    from .profiles import RuntimeProfile

    kill_switches.requireEnabled("llm", caller="free_model_probe")
    validate_free_profile(
        RuntimeProfile(
            key="free-probe",
            provider=FREE_PROVIDER,
            model=model,
            base_url=FREE_BASE_URL,
            api_key=FREE_SDK_KEY,
        )
    )
    try:
        async with asyncio.timeout(FREE_PROBE_TIMEOUT):
            async with httpx.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                timeout=FREE_PROBE_TIMEOUT,
            ) as client:
                response = await client.post(
                    f"{FREE_BASE_URL}/chat/completions",
                    headers={"Authorization": "", "X-Title": "The Homie"},
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "Reply exactly OK."}],
                        "tools": [],
                        "tool_choice": "none",
                        "max_completion_tokens": 64,
                    },
                )
                response.raise_for_status()
                choices = response.json().get("choices", [])
                if len(choices) != 1:
                    raise FreeRuntimeError("INVALID_RESPONSE")
                choice = choices[0]
                message = choice.get("message") or {}
                if (
                    choice.get("finish_reason") != "stop"
                    or not str(message.get("content") or "").strip()
                    or message.get("tool_calls")
                    or message.get("function_call")
                ):
                    raise FreeRuntimeError("INCOMPLETE_RESPONSE")
    except Exception as exc:
        raise FreeRuntimeError(error_code(exc), "Free verification failed") from exc
