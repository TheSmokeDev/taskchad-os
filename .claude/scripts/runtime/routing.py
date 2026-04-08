"""Runtime routing policy for task-specific provider selection."""

from __future__ import annotations

import os

from .base import RuntimeRequest
from .capabilities import TEXT_REASONING, TOOL_REASONING
from .health import is_profile_available
from .profiles import RuntimeProfile, build_profile_for_provider, normalize_provider

# Hermes-style: every provider is a full agent runtime.
# Use Claude (primary sub) for everything. When it burns out,
# fall to Codex (ChatGPT sub). Then Gemini (Google sub).
# OpenRouter/OpenAI API are pay-per-call fallbacks at the end.
DEFAULT_PROVIDER_CHAIN = (
    "claude",
    "openai_codex",
    "gemini",
    "openrouter",
    "openai",
)

# Legacy text-only route for background jobs that don't need tools.
# Prefers cheaper providers first since tools aren't needed.
DEFAULT_TEXT_ROUTE = (
    "gemini",
    "openai_codex",
    "openrouter",
    "openai",
    "claude",
)

# Tool-capable route: all subscription agents, then API fallbacks.
DEFAULT_TOOL_ROUTE = DEFAULT_PROVIDER_CHAIN

TASK_ROUTE_DEFAULTS = {
    # Background text jobs — use cheap providers first
    "memory_flush": DEFAULT_TEXT_ROUTE,
    "heartbeat_formatter": DEFAULT_TEXT_ROUTE,
    # Tool-heavy jobs — use the full chain
    "heartbeat": DEFAULT_PROVIDER_CHAIN,
    "memory_reflect": DEFAULT_PROVIDER_CHAIN,
    "memory_weekly": DEFAULT_PROVIDER_CHAIN,
    # Chat turns — use the full chain (Claude first, fallback to Codex/Gemini)
    "chat_turn": DEFAULT_PROVIDER_CHAIN,
}


def resolve_runtime_profiles(request: RuntimeRequest) -> list[RuntimeProfile]:
    """Resolve runtime profiles from task policy and provider health state."""

    provider_order = _provider_order_for_request(request)
    healthy = _build_profiles(
        provider_order,
        request,
        respect_health=True,
        ignore_primary_health=bool(_pinned_primary_provider()),
    )
    if healthy:
        return healthy
    return _build_profiles(
        provider_order,
        request,
        respect_health=False,
        ignore_primary_health=False,
    )


def _provider_order_for_request(request: RuntimeRequest) -> tuple[str, ...]:
    override = _route_override_for_task(request.task_name) or _route_override_for_capability(
        request.capability
    )
    pinned_primary = _pinned_primary_provider()
    base_order = list(override or ([pinned_primary] if pinned_primary else _default_route(request)))

    if _can_fallback(request):
        extras = _fallback_route_for_request(
            request,
            override=bool(override),
            pinned=bool(pinned_primary),
        )
        base_order.extend(extras)

    return _dedupe_order(base_order)


def _build_profiles(
    provider_order: tuple[str, ...],
    request: RuntimeRequest,
    *,
    respect_health: bool,
    ignore_primary_health: bool,
) -> list[RuntimeProfile]:
    resolved: list[RuntimeProfile] = []

    for index, provider in enumerate(provider_order):
        prefix = "primary" if index == 0 else f"fallback{index}"
        profile = build_profile_for_provider(provider, key_prefix=prefix, request=request)
        if not profile:
            continue
        if respect_health and not (ignore_primary_health and index == 0):
            if not is_profile_available(profile):
                continue
        resolved.append(profile)

    return resolved


def _route_override_for_task(task_name: str) -> tuple[str, ...]:
    env_var = f"SECOND_BRAIN_ROUTE_{task_name.upper()}"
    return _parse_provider_list(os.getenv(env_var, ""))


def _route_override_for_capability(capability: str) -> tuple[str, ...]:
    env_var = (
        "SECOND_BRAIN_ROUTE_TEXT"
        if capability == TEXT_REASONING
        else "SECOND_BRAIN_ROUTE_TOOL"
    )
    return _parse_provider_list(os.getenv(env_var, ""))


def _pinned_primary_provider() -> str | None:
    raw = os.getenv("SECOND_BRAIN_RUNTIME_PROVIDER", "").strip()
    if not raw or raw.lower() == "auto":
        return None
    return normalize_provider(raw)


def _default_route(request: RuntimeRequest) -> tuple[str, ...]:
    if request.task_name in TASK_ROUTE_DEFAULTS:
        return _dedupe_order(list(TASK_ROUTE_DEFAULTS[request.task_name]))
    if request.capability == TOOL_REASONING:
        return DEFAULT_TOOL_ROUTE
    return DEFAULT_TEXT_ROUTE


def _fallback_route_for_request(
    request: RuntimeRequest,
    *,
    override: bool,
    pinned: bool,
) -> tuple[str, ...]:
    explicit = _parse_provider_list(os.getenv("SECOND_BRAIN_FALLBACK_PROVIDER", ""))
    if explicit:
        return explicit
    if override and not pinned:
        return ()
    # Hermes-style: all providers can fallback for any capability.
    # The adapter's supports() method gates what each provider can handle.
    if request.capability == TOOL_REASONING:
        return DEFAULT_PROVIDER_CHAIN
    return DEFAULT_TEXT_ROUTE


def _can_fallback(request: RuntimeRequest) -> bool:
    if not request.allow_fallback:
        return False
    # Session resume is Claude-only. Hooks are allowed for fallback to
    # other subscription providers (Gemini/Codex).
    if request.resume:
        return False
    return True


def _parse_provider_list(raw: str) -> tuple[str, ...]:
    if not raw.strip():
        return ()
    return _dedupe_order([normalize_provider(part) for part in raw.split(",") if part.strip()])


def _dedupe_order(providers: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for provider in providers:
        normalized = normalize_provider(provider)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return tuple(ordered)
