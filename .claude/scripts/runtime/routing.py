"""Runtime routing policy for compatibility and generic-lane resolution."""

from __future__ import annotations

import os

from .base import RuntimeRequest
from .capabilities import TEXT_REASONING, TOOL_REASONING
from .health import is_profile_available
from .profiles import (
    GENERIC_PROVIDER_REGISTRY,
    RuntimeProfile,
    build_profile_for_provider,
    normalize_provider,
)
from .selection import resolve_runtime_selection

# Compatibility/provider-status ordering kept for legacy shims and health checks
# during the PR2 migration.
DEFAULT_PROVIDER_CHAIN = (
    "claude",
    "openai_codex",
    "gemini",
    "openrouter",
    "openai",
)

DEFAULT_TEXT_ROUTE = (
    "gemini",
    "openai_codex",
    "openrouter",
    "openai",
    "claude",
)

DEFAULT_TOOL_ROUTE = DEFAULT_PROVIDER_CHAIN

TASK_ROUTE_DEFAULTS = {
    "memory_flush": DEFAULT_TEXT_ROUTE,
    "heartbeat_formatter": DEFAULT_TEXT_ROUTE,
    "heartbeat": DEFAULT_PROVIDER_CHAIN,
    "memory_reflect": DEFAULT_PROVIDER_CHAIN,
    "memory_weekly": DEFAULT_PROVIDER_CHAIN,
}

# Derived from GENERIC_PROVIDER_REGISTRY: tool_route_priority >= 0 means the
# provider participates in the tool route, and the int is its ordering key.
GENERIC_TOOL_ROUTE: tuple[str, ...] = tuple(
    key
    for key, _overlay in sorted(
        (
            (k, v)
            for k, v in GENERIC_PROVIDER_REGISTRY.items()
            if v.tool_route_priority >= 0
        ),
        key=lambda kv: kv[1].tool_route_priority,
    )
)

# Derived from GENERIC_PROVIDER_REGISTRY: every entry participates in the
# text route, ordered by text_route_priority.
GENERIC_TEXT_ROUTE: tuple[str, ...] = tuple(
    key
    for key, _overlay in sorted(
        GENERIC_PROVIDER_REGISTRY.items(),
        key=lambda kv: kv[1].text_route_priority,
    )
)

GENERIC_TASK_ROUTE_DEFAULTS = {
    "memory_flush": GENERIC_TEXT_ROUTE,
    "heartbeat_formatter": GENERIC_TEXT_ROUTE,
    "heartbeat": GENERIC_TOOL_ROUTE,
    "memory_reflect": GENERIC_TOOL_ROUTE,
    "memory_weekly": GENERIC_TOOL_ROUTE,
}

_GENERIC_TEXT_PROVIDER_SET = {
    normalize_provider(provider) for provider in GENERIC_TEXT_ROUTE
}
_GENERIC_TOOL_PROVIDER_SET = {
    normalize_provider(provider) for provider in GENERIC_TOOL_ROUTE
}


def resolve_runtime_profiles(request: RuntimeRequest) -> list[RuntimeProfile]:
    """Resolve profiles from the compatibility provider ordering."""

    provider_order = _provider_order_for_request(request)
    return _resolve_profiles(
        provider_order,
        request,
        ignore_primary_health=bool(_pinned_primary_provider()),
    )


def resolve_generic_runtime_profiles(request: RuntimeRequest) -> list[RuntimeProfile]:
    """Resolve profiles for the generic runtime lane only."""

    provider_order = _generic_provider_order_for_request(request)
    return _resolve_profiles(
        provider_order,
        request,
        ignore_primary_health=bool(_preferred_generic_provider(request)),
    )


def _resolve_profiles(
    provider_order: tuple[str, ...],
    request: RuntimeRequest,
    *,
    ignore_primary_health: bool,
) -> list[RuntimeProfile]:
    healthy = _build_profiles(
        provider_order,
        request,
        respect_health=True,
        ignore_primary_health=ignore_primary_health,
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


def _generic_provider_order_for_request(request: RuntimeRequest) -> tuple[str, ...]:
    override = _generic_route_override_for_task(
        request.task_name,
        capability=request.capability,
    ) or _generic_route_override_for_capability(request.capability)
    preferred_provider = _preferred_generic_provider(request)
    base_order = list(
        override
        or ([preferred_provider] if preferred_provider else _generic_default_route(request))
    )

    if _can_fallback(request):
        extras = _generic_fallback_route_for_request(
            request,
            override=bool(override),
            pinned=bool(preferred_provider),
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


def _generic_route_override_for_task(task_name: str, *, capability: str) -> tuple[str, ...]:
    env_var = f"SECOND_BRAIN_ROUTE_{task_name.upper()}"
    return _filter_generic_provider_list(os.getenv(env_var, ""), capability=capability)


def _route_override_for_capability(capability: str) -> tuple[str, ...]:
    env_var = (
        "SECOND_BRAIN_ROUTE_TEXT"
        if capability == TEXT_REASONING
        else "SECOND_BRAIN_ROUTE_TOOL"
    )
    return _parse_provider_list(os.getenv(env_var, ""))


def _generic_route_override_for_capability(capability: str) -> tuple[str, ...]:
    env_var = (
        "SECOND_BRAIN_ROUTE_TEXT"
        if capability == TEXT_REASONING
        else "SECOND_BRAIN_ROUTE_TOOL"
    )
    return _filter_generic_provider_list(os.getenv(env_var, ""), capability=capability)


def _pinned_primary_provider() -> str | None:
    selection = resolve_runtime_selection()
    if selection.lane == "claude_native":
        return "claude"
    return selection.generic_provider


def _preferred_generic_provider(request: RuntimeRequest) -> str | None:
    selection = resolve_runtime_selection()
    if selection.lane == "claude_native":
        return None
    provider = selection.generic_provider
    if provider is None:
        return None
    if provider in _allowed_generic_providers_for_capability(request.capability):
        return provider
    return None


def _default_route(request: RuntimeRequest) -> tuple[str, ...]:
    if request.task_name == "chat_turn":
        return DEFAULT_TOOL_ROUTE if request.capability == TOOL_REASONING else DEFAULT_TEXT_ROUTE
    if request.task_name in TASK_ROUTE_DEFAULTS:
        return _dedupe_order(list(TASK_ROUTE_DEFAULTS[request.task_name]))
    if request.capability == TOOL_REASONING:
        return DEFAULT_TOOL_ROUTE
    return DEFAULT_TEXT_ROUTE


def _generic_default_route(request: RuntimeRequest) -> tuple[str, ...]:
    if request.task_name == "chat_turn":
        route = GENERIC_TOOL_ROUTE if request.capability == TOOL_REASONING else GENERIC_TEXT_ROUTE
        return _dedupe_order(list(route))
    if request.task_name in GENERIC_TASK_ROUTE_DEFAULTS:
        return _dedupe_order(list(GENERIC_TASK_ROUTE_DEFAULTS[request.task_name]))
    if request.capability == TOOL_REASONING:
        return _dedupe_order(list(GENERIC_TOOL_ROUTE))
    return _dedupe_order(list(GENERIC_TEXT_ROUTE))


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
    if request.capability == TOOL_REASONING:
        return DEFAULT_PROVIDER_CHAIN
    return DEFAULT_TEXT_ROUTE


def _generic_fallback_route_for_request(
    request: RuntimeRequest,
    *,
    override: bool,
    pinned: bool,
) -> tuple[str, ...]:
    explicit = _filter_generic_provider_list(
        os.getenv("SECOND_BRAIN_FALLBACK_PROVIDER", ""),
        capability=request.capability,
    )
    if explicit:
        return explicit
    if override and not pinned:
        return ()
    return _generic_default_route(request)


def _can_fallback(request: RuntimeRequest) -> bool:
    if not request.allow_fallback:
        return False
    if request.resume:
        return False
    return True


def _parse_provider_list(raw: str) -> tuple[str, ...]:
    if not raw.strip():
        return ()
    return _dedupe_order([normalize_provider(part) for part in raw.split(",") if part.strip()])


def _filter_generic_provider_list(raw: str, *, capability: str) -> tuple[str, ...]:
    if not raw.strip():
        return ()
    allowed = _allowed_generic_providers_for_capability(capability)
    return _dedupe_order([
        provider
        for provider in (normalize_provider(part) for part in raw.split(",") if part.strip())
        if provider in allowed
    ])


def _allowed_generic_providers_for_capability(capability: str) -> set[str]:
    if capability == TOOL_REASONING:
        return set(_GENERIC_TOOL_PROVIDER_SET)
    return set(_GENERIC_TEXT_PROVIDER_SET)


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
