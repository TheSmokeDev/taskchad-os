"""Runtime routing policy for compatibility and generic-lane resolution."""

from __future__ import annotations

import os
from collections.abc import Iterator

from . import base as _base
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

# Provider ORDERING for fallback routes, task defaults, and provider-status
# surfaces. #133 part B doctrine clarification: this tuple does NOT implement
# cross-LANE auto-failover — lane_router resolves the claude_native lane to
# Claude alone, and a pinned generic provider yields a one-element route before
# fallback is consulted. "claude" leading this chain orders status displays and
# the tool-capability fallback WITHIN whatever lane is executing; a Claude
# failure never falls through to Codex/Gemini (and vice versa) via this chain.
# The framework doctrine is request PORTABILITY across lanes (the assembled
# RuntimeRequest must survive on any provider), not runtime auto-failover.
DEFAULT_PROVIDER_CHAIN = (
    "claude",
    "openai_codex",
    "gemini",
    "openrouter",
    "openai",
    "kimi",
    "nvidia",
)

DEFAULT_TEXT_ROUTE = (
    "gemini",
    "openai_codex",
    "openrouter",
    "openai",
    "claude",
    "nvidia",
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

# Caller-supplied tool definitions are a DIFFERENT AXIS from provider-owned
# tools, and the two answers INVERT.
#
# `tool_route_priority` answers "can this provider run its OWN agentic tools"
# — the Codex and Gemini CLIs can (shell, apply_patch, web), the API-backed
# providers cannot, which is why kimi/openrouter/openai-compatible sit at -1
# and GENERIC_TOOL_ROUTE is just ('openai-codex', 'gemini-cli').
#
# For CALLER-supplied OpenAI-format schemas the answer flips: kimi carries them
# natively (measured 2026-07-27: `finish_reason: tool_calls`), while both CLIs
# ignore them outright (no --tools flag; MCP is their only extension path).
# Routing a caller-tools request through GENERIC_TOOL_ROUTE therefore offers
# EXACTLY the two lanes that cannot serve it and withholds the one that can —
# a starvation upstream of, and invisible to, the capability gate in
# lane_router.
#
# The fix is deliberately NOT a second, inverted priority table. Two tables
# encoding "who can run tools" would drift the moment a provider is added, and
# the drift would be silent. Instead a caller-tools request draws from the FULL
# generic pool and `lane_router._adapter_carries_tool_defs` does the filtering
# — one source of truth, asked of the adapter itself.
#
# Same membership as GENERIC_TEXT_ROUTE by construction (every registry entry
# participates in the text route); aliased rather than re-derived so the two
# cannot diverge.
GENERIC_CALLER_TOOLS_ROUTE: tuple[str, ...] = GENERIC_TEXT_ROUTE

_GENERIC_TEXT_PROVIDER_SET = {
    normalize_provider(provider) for provider in GENERIC_TEXT_ROUTE
}
_GENERIC_TOOL_PROVIDER_SET = {
    normalize_provider(provider) for provider in GENERIC_TOOL_ROUTE
}
_GENERIC_CALLER_TOOLS_PROVIDER_SET = {
    normalize_provider(provider) for provider in GENERIC_CALLER_TOOLS_ROUTE
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

    return list(iter_generic_runtime_profiles(request))


def iter_generic_runtime_profiles(request: RuntimeRequest) -> Iterator[RuntimeProfile]:
    """Yield generic-lane profiles lazily in effective route order.

    Building a subprocess-backed profile can perform a synchronous auth probe
    (for example, ``codex login status``).  The execution lane must therefore
    stop profile discovery as soon as a carrying preferred provider succeeds;
    eagerly constructing every unused fallback would block the async hot path
    before the first adapter was contacted.  The list-returning public resolver
    above remains a compatibility wrapper for diagnostics and callers that
    explicitly need the complete configured inventory.
    """

    provider_order = _generic_provider_order_for_request(request)
    if request.model_only:
        # Health selection happens before the lane router can ask adapters for
        # the zero-tool capability. If the only healthy profile is Codex, the
        # resolver would return early with that unsafe adapter and never expose
        # a cooling-down-but-now-repaired Gemini/OpenAI profile for the
        # fail-closed adapter gate to evaluate. Model-only jobs are sparse and
        # already bounded, so return the complete configured route here; the
        # lane router still skips every adapter without a literal guarantee and
        # records fresh health from the actual attempt.
        yield from _iter_profiles(
            provider_order,
            request,
            respect_health=False,
            ignore_primary_health=False,
        )
        return

    yielded_healthy = False
    for profile in _iter_profiles(
        provider_order,
        request,
        respect_health=True,
        ignore_primary_health=bool(_preferred_generic_provider(request)),
    ):
        yielded_healthy = True
        yield profile

    if yielded_healthy:
        return

    # Preserve the existing physical-health retry contract: only when the
    # healthy pass produced no configured profile at all do we expose the same
    # route again without the cooldown filter.
    yield from _iter_profiles(
        provider_order,
        request,
        respect_health=False,
        ignore_primary_health=False,
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
    carries_caller_tools = _base.request_carries_tools(request)
    preferred_provider = _preferred_generic_provider(request)
    if preferred_provider:
        # An operator provider choice is route ORDER, not exclusive
        # eligibility, for a turn that carries caller-supplied schemas.
        #
        # Collapsing the route to the single preferred provider here happens
        # UPSTREAM of `lane_router._adapter_carries_tool_defs`, so a preferred
        # provider that cannot carry schemas left the request with nowhere to
        # go: the capability gate correctly skipped the only candidate and the
        # turn died as a transport failure even though a carrying fallback was
        # configured and permitted. The operator asked for a provider, not for
        # their equipped tools to be dropped.
        #
        # The preferred provider stays FIRST, so a carrying preference still
        # wins immediately and never reaches the appended candidates. Nothing
        # is widened for anyone else: the expansion applies only when the
        # request carries caller definitions AND its own contract already
        # permits fallback. `allow_fallback=False` and resume both keep the
        # exact single-provider route they have today.
        #
        # `GENERIC_CALLER_TOOLS_ROUTE` is the FULL generic pool, so this is the
        # same candidate set the unpinned branch below resolves to — the pin
        # only moves one provider to the front of it.
        if carries_caller_tools and _can_fallback(request):
            return _dedupe_order([preferred_provider, *GENERIC_CALLER_TOOLS_ROUTE])
        return (preferred_provider,)

    override = _generic_route_override_for_task(
        request.task_name,
        capability=request.capability,
        carries_caller_tools=carries_caller_tools,
    ) or _generic_route_override_for_capability(
        request.capability, carries_caller_tools=carries_caller_tools
    )
    base_order = list(
        override or _generic_default_route(request)
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
    return list(
        _iter_profiles(
            provider_order,
            request,
            respect_health=respect_health,
            ignore_primary_health=ignore_primary_health,
        )
    )


def _iter_profiles(
    provider_order: tuple[str, ...],
    request: RuntimeRequest,
    *,
    respect_health: bool,
    ignore_primary_health: bool,
) -> Iterator[RuntimeProfile]:
    """Build profiles one at a time so unused fallbacks stay undiscovered."""

    for index, provider in enumerate(provider_order):
        prefix = "primary" if index == 0 else f"fallback{index}"
        profile = build_profile_for_provider(provider, key_prefix=prefix, request=request)
        if not profile:
            continue
        if respect_health and not (ignore_primary_health and index == 0):
            if not is_profile_available(profile):
                continue
        yield profile


def _route_override_for_task(task_name: str) -> tuple[str, ...]:
    env_var = f"SECOND_BRAIN_ROUTE_{task_name.upper()}"
    return _parse_provider_list(os.getenv(env_var, ""))


def _generic_route_override_for_task(
    task_name: str, *, capability: str, carries_caller_tools: bool = False
) -> tuple[str, ...]:
    env_var = f"SECOND_BRAIN_ROUTE_{task_name.upper()}"
    return _filter_generic_provider_list(
        os.getenv(env_var, ""),
        capability=capability,
        carries_caller_tools=carries_caller_tools,
    )


def _route_override_for_capability(capability: str) -> tuple[str, ...]:
    env_var = (
        "SECOND_BRAIN_ROUTE_TEXT"
        if capability == TEXT_REASONING
        else "SECOND_BRAIN_ROUTE_TOOL"
    )
    return _parse_provider_list(os.getenv(env_var, ""))


def _generic_route_override_for_capability(
    capability: str, *, carries_caller_tools: bool = False
) -> tuple[str, ...]:
    env_var = (
        "SECOND_BRAIN_ROUTE_TEXT"
        if capability == TEXT_REASONING
        else "SECOND_BRAIN_ROUTE_TOOL"
    )
    return _filter_generic_provider_list(
        os.getenv(env_var, ""),
        capability=capability,
        carries_caller_tools=carries_caller_tools,
    )


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
    # Same allowlist the route overrides use, asked with the same question.
    # Omitting `carries_caller_tools` here dropped an operator pin on exactly
    # the providers that CAN carry schemas: kimi sits outside
    # _GENERIC_TOOL_PROVIDER_SET, so a pinned kimi on a caller-tools
    # TOOL_REASONING turn resolved to None and fell back to the unpinned route
    # — where kimi is fifth. The pin was not refused, it was silently demoted.
    allowed = _allowed_generic_providers_for_capability(
        request.capability,
        carries_caller_tools=_base.request_carries_tools(request),
    )
    if provider in allowed:
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
    # Caller-supplied tool defs are checked FIRST, ahead of the task-name and
    # capability branches. Those branches encode provider ECONOMY (which lane
    # is cheapest/best for this kind of work); this one encodes CORRECTNESS
    # (which lanes can execute the request at all). A task default that
    # resolved to GENERIC_TOOL_ROUTE would hand a caller-tools request the two
    # lanes guaranteed to drop it — heartbeat/memory_reflect/memory_weekly all
    # do exactly that today. Correctness wins over economy.
    if _base.request_carries_tools(request):
        return _dedupe_order(list(GENERIC_CALLER_TOOLS_ROUTE))
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
        carries_caller_tools=_base.request_carries_tools(request),
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


def _filter_generic_provider_list(
    raw: str, *, capability: str, carries_caller_tools: bool = False
) -> tuple[str, ...]:
    if not raw.strip():
        return ()
    allowed = _allowed_generic_providers_for_capability(
        capability, carries_caller_tools=carries_caller_tools
    )
    return _dedupe_order([
        provider
        for provider in (normalize_provider(part) for part in raw.split(",") if part.strip())
        if provider in allowed
    ])


def _allowed_generic_providers_for_capability(
    capability: str, *, carries_caller_tools: bool = False
) -> set[str]:
    # A caller-tools request is allowed the full generic pool regardless of
    # capability tier. Without this, an operator who explicitly names a carrying
    # provider (SECOND_BRAIN_ROUTE_TOOL=kimi, or an explicit fallback list) has
    # that choice SILENTLY discarded on a TOOL_REASONING turn, because kimi is
    # not in _GENERIC_TOOL_PROVIDER_SET. Silently dropping an operator's
    # explicit routing instruction is the same class of failure as silently
    # dropping a tool call.
    if carries_caller_tools:
        return set(_GENERIC_CALLER_TOOLS_PROVIDER_SET)
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
