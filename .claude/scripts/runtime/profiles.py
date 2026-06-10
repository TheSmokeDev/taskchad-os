"""Runtime profile resolution."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .auth_profiles import (
    codex_auth_available,
    gemini_auth_available,
    resolve_codex_auth_profile,
    resolve_gemini_auth_profile,
)
from .base import RuntimeRequest

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()


@dataclass(slots=True)
class RuntimeProfile:
    """Resolved runtime profile."""

    key: str
    provider: str
    model: str
    api_key: str | None = None
    base_url: str | None = None
    command: str | None = None
    auth_profile: str | None = None
    candidate_models: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class GenericProviderOverlay:
    """Metadata for a single generic-lane provider.

    Single source of truth for transport, auth, aliases, routing priorities,
    display name, and the legacy env-var write value. Claude-native is not
    represented here.
    """

    transport: str                  # "subprocess_cli" | "openai_responses"
    auth_type: str                  # "codex" | "gemini" | "api_key"
    display_name: str
    model_env_var: str
    default_model: str
    text_route_priority: int        # 0 = first in GENERIC_TEXT_ROUTE
    tool_route_priority: int        # -1 = excluded from GENERIC_TOOL_ROUTE
    aliases: tuple[str, ...]
    legacy_write_key: str
    # openai_responses only:
    api_key_env_vars: tuple[str, ...] = ()
    base_url: str | None = None
    base_url_env_var: str | None = None


GENERIC_PROVIDER_REGISTRY: dict[str, GenericProviderOverlay] = {
    "openai-compatible": GenericProviderOverlay(
        transport="openai_responses",
        auth_type="api_key",
        display_name="OpenAI-compatible",
        model_env_var="SECOND_BRAIN_OPENAI_MODEL",
        default_model="gpt-4.1-mini",
        text_route_priority=0,
        tool_route_priority=-1,
        aliases=("openai", "openai-compatible"),
        legacy_write_key="openai",
        api_key_env_vars=("OPENAI_API_KEY",),
        base_url_env_var="SECOND_BRAIN_RUNTIME_BASE_URL",
    ),
    "openrouter": GenericProviderOverlay(
        transport="openai_responses",
        auth_type="api_key",
        display_name="OpenRouter",
        model_env_var="SECOND_BRAIN_OPENROUTER_MODEL",
        default_model="openrouter/auto",
        text_route_priority=1,
        tool_route_priority=-1,
        aliases=("openrouter",),
        legacy_write_key="openrouter",
        api_key_env_vars=("OPENROUTER_API_KEY",),
        base_url="https://openrouter.ai/api/v1",
        base_url_env_var="SECOND_BRAIN_OPENROUTER_BASE_URL",
    ),
    "openai-codex": GenericProviderOverlay(
        transport="subprocess_cli",
        auth_type="codex",
        display_name="Codex",
        model_env_var="SECOND_BRAIN_CODEX_MODEL",
        default_model="chatgpt-plan-default",
        text_route_priority=2,
        tool_route_priority=0,
        aliases=("codex", "codec", "openai_codex", "openai-codex", "chatgpt", "gpt", "gbt"),
        legacy_write_key="openai_codex",
    ),
    "gemini-cli": GenericProviderOverlay(
        transport="subprocess_cli",
        auth_type="gemini",
        display_name="Gemini",
        model_env_var="SECOND_BRAIN_GEMINI_MODEL",
        default_model="gemini-2.5-flash",
        text_route_priority=3,
        tool_route_priority=1,
        aliases=("gemini", "gemini-cli", "google"),
        legacy_write_key="gemini",
    ),
}


def _build_provider_aliases() -> dict[str, str]:
    aliases: dict[str, str] = {"claude": "claude", "anthropic": "claude"}
    for canonical, overlay in GENERIC_PROVIDER_REGISTRY.items():
        for alias in overlay.aliases:
            aliases[alias] = canonical
    return aliases


PROVIDER_ALIASES: dict[str, str] = _build_provider_aliases()


def _model_from_env(var_name: str, default: str) -> str:
    return os.getenv(var_name, default).strip() or default


def _dedupe_models(models: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for model in models:
        normalized = model.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return tuple(ordered)


def _gemini_model_ladder(
    primary_model: str | None = None,
    *,
    env_var: str = "SECOND_BRAIN_GEMINI_MODEL_LADDER",
) -> tuple[str, ...]:
    configured = [
        part.strip()
        for part in os.getenv(env_var, "").split(",")
        if part.strip()
    ]
    # gemini-2.5-* are reachable on the free oauth/Code-Assist tier the CLI uses.
    # gemini-3.5-flash is GA but requires a paid Gemini API tier (returns 404 on
    # the oauth lane), so it is opt-in via SECOND_BRAIN_GEMINI_MODEL, not a default.
    defaults = [
        "gemini-2.5-flash",
        "gemini-2.5-pro",
    ]
    base = configured or defaults
    return _dedupe_models(([primary_model] if primary_model else []) + base)


def _primary_model_for_provider(provider: str) -> str:
    provider = normalize_provider(provider)
    explicit = os.getenv("SECOND_BRAIN_RUNTIME_MODEL", "").strip()
    if explicit:
        return explicit

    if provider == "gemini-cli":
        explicit_gemini = os.getenv("SECOND_BRAIN_GEMINI_MODEL", "").strip()
        if explicit_gemini:
            return explicit_gemini
        return _gemini_model_ladder()[0]
    if provider == "openai-codex":
        return _model_from_env("SECOND_BRAIN_CODEX_MODEL", "chatgpt-plan-default")
    if provider == "openai-compatible":
        return _model_from_env("SECOND_BRAIN_OPENAI_MODEL", "gpt-4.1-mini")
    if provider == "openrouter":
        return _model_from_env("SECOND_BRAIN_OPENROUTER_MODEL", "openrouter/auto")
    return _model_from_env("SECOND_BRAIN_CLAUDE_MODEL", "claude-sonnet-4-6")


def _resolve_api_key_from_env_vars(env_vars: tuple[str, ...]) -> str:
    """Resolve an API key from a tuple of candidate env var names.

    Module-level string constants take precedence over os.getenv — the only
    current example is OPENAI_API_KEY, which tests monkeypatch via
    ``setattr(profiles, "OPENAI_API_KEY", "")``. First non-empty value wins.
    """

    for env_var in env_vars:
        module_value = globals().get(env_var)
        if isinstance(module_value, str):
            value = module_value.strip()
        else:
            value = os.getenv(env_var, "").strip()
        if value:
            return value
    return ""


def _subprocess_profile(
    *, key_prefix: str, provider: str, overlay: GenericProviderOverlay
) -> RuntimeProfile | None:
    """Build a profile for subprocess_cli providers (Codex, Gemini).

    Dispatches on ``overlay.auth_type``. A new subprocess_cli provider that
    shares an existing auth_type needs only a GENERIC_PROVIDER_REGISTRY
    entry. A genuinely new auth pattern adds one branch here (and a line in
    ``lane_router._adapter_for`` if it also ships a new adapter class).
    """

    if overlay.auth_type == "codex":
        auth = resolve_codex_auth_profile()
        if not codex_auth_available(auth):
            return None
        return RuntimeProfile(
            key=f"{key_prefix}-{provider}",
            provider=provider,
            model=_model_from_env(overlay.model_env_var, overlay.default_model),
            command=auth.command,
            auth_profile=auth.key,
        )

    if overlay.auth_type == "gemini":
        auth = resolve_gemini_auth_profile()
        if not gemini_auth_available(auth):
            return None
        ladder_env = (
            "SECOND_BRAIN_GEMINI_FALLBACK_MODEL_LADDER"
            if key_prefix.startswith("fallback")
            else "SECOND_BRAIN_GEMINI_MODEL_LADDER"
        )
        primary = _model_from_env(overlay.model_env_var, overlay.default_model)
        return RuntimeProfile(
            key=f"{key_prefix}-{provider}",
            provider=provider,
            model=primary,
            command=auth.command,
            auth_profile=auth.auth_type,
            candidate_models=_gemini_model_ladder(primary, env_var=ladder_env),
        )

    return None


def _http_profile(
    *, key_prefix: str, provider: str, overlay: GenericProviderOverlay
) -> RuntimeProfile | None:
    """Build a profile for openai_responses providers (OpenAI, OpenRouter)."""

    api_key = _resolve_api_key_from_env_vars(overlay.api_key_env_vars)
    if not api_key:
        return None
    base_url = (
        (overlay.base_url_env_var and os.getenv(overlay.base_url_env_var, "").strip())
        or overlay.base_url
        or None
    )
    return RuntimeProfile(
        key=f"{key_prefix}-{provider}",
        provider=provider,
        model=_model_from_env(overlay.model_env_var, overlay.default_model),
        api_key=api_key,
        base_url=base_url,
    )


def _claude_profile(*, key_prefix: str, model: str | None = None) -> RuntimeProfile:
    return RuntimeProfile(
        key=f"{key_prefix}-claude",
        provider="claude",
        model=model or _primary_model_for_provider("claude"),
    )


# Thin wrappers that delegate to _subprocess_profile / _http_profile via the
# registry. Retained so tests that monkeypatch these symbols directly (see
# tests/test_openai_codex_runtime.py and tests/test_gemini_cli_runtime.py)
# continue to work — a registry edit still flows through here unchanged.
def _openai_codex_profile(*, key_prefix: str, model: str | None = None) -> RuntimeProfile | None:
    return _subprocess_profile(
        key_prefix=key_prefix,
        provider="openai-codex",
        overlay=GENERIC_PROVIDER_REGISTRY["openai-codex"],
    )


def _gemini_profile(
    *,
    key_prefix: str,
    model: str | None = None,
    ladder_env: str = "SECOND_BRAIN_GEMINI_MODEL_LADDER",
) -> RuntimeProfile | None:
    del ladder_env  # ladder env derived inside _subprocess_profile from key_prefix
    return _subprocess_profile(
        key_prefix=key_prefix,
        provider="gemini-cli",
        overlay=GENERIC_PROVIDER_REGISTRY["gemini-cli"],
    )


def _openai_profile(*, key_prefix: str, model: str | None = None) -> RuntimeProfile | None:
    return _http_profile(
        key_prefix=key_prefix,
        provider="openai-compatible",
        overlay=GENERIC_PROVIDER_REGISTRY["openai-compatible"],
    )


def _openrouter_profile(*, key_prefix: str, model: str | None = None) -> RuntimeProfile | None:
    return _http_profile(
        key_prefix=key_prefix,
        provider="openrouter",
        overlay=GENERIC_PROVIDER_REGISTRY["openrouter"],
    )


def normalize_provider(provider: str) -> str:
    """Normalize provider aliases into canonical runtime provider ids."""

    normalized = provider.strip().lower()
    return PROVIDER_ALIASES.get(normalized, normalized)


def build_profile_for_provider(
    provider: str,
    *,
    key_prefix: str,
    request: RuntimeRequest | None = None,
    model: str | None = None,
) -> RuntimeProfile | None:
    """Build a runtime profile for a canonical provider id or alias."""

    provider = normalize_provider(provider)

    if provider == "claude":
        # Only Claude accepts the request model; generic providers use their
        # own default models because model names are provider-specific.
        requested_model = model or (
            request.fallback_model if key_prefix.startswith("fallback") and request else None
        ) or (request.model if request else None)
        return _claude_profile(key_prefix=key_prefix, model=requested_model)

    overlay = GENERIC_PROVIDER_REGISTRY.get(provider)
    if overlay is None:
        return None

    # Dispatch through provider-specific wrappers so tests that monkeypatch
    # them (e.g. ``profiles._openai_codex_profile`` → lambda) still take
    # effect on ``resolve_runtime_profiles``. Each wrapper itself defers to
    # the registry-driven ``_subprocess_profile`` / ``_http_profile``.
    if provider == "openai-codex":
        return _openai_codex_profile(key_prefix=key_prefix)
    if provider == "gemini-cli":
        ladder_env = (
            "SECOND_BRAIN_GEMINI_FALLBACK_MODEL_LADDER"
            if key_prefix.startswith("fallback")
            else "SECOND_BRAIN_GEMINI_MODEL_LADDER"
        )
        return _gemini_profile(key_prefix=key_prefix, ladder_env=ladder_env)
    if provider == "openai-compatible":
        return _openai_profile(key_prefix=key_prefix)
    if provider == "openrouter":
        return _openrouter_profile(key_prefix=key_prefix)
    return None


def resolve_runtime_profiles(request: RuntimeRequest) -> list[RuntimeProfile]:
    """Resolve runtime profiles via the routing policy layer."""

    from .routing import resolve_runtime_profiles as resolve_via_routing

    return resolve_via_routing(request)
