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


PROVIDER_ALIASES = {
    "claude": "claude",
    "anthropic": "claude",
    "gemini": "gemini-cli",
    "gemini-cli": "gemini-cli",
    "google": "gemini-cli",
    "openai-codex": "openai-codex",
    "openai_codex": "openai-codex",
    "codex": "openai-codex",
    "openai": "openai-compatible",
    "openai-compatible": "openai-compatible",
    "openrouter": "openrouter",
}


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
    defaults = [
        "gemini-3-flash-preview",
        "gemini-3-pro-preview",
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


def _openai_codex_profile(*, key_prefix: str, model: str | None = None) -> RuntimeProfile | None:
    auth_profile = resolve_codex_auth_profile()
    if not codex_auth_available(auth_profile):
        return None

    return RuntimeProfile(
        key=f"{key_prefix}-openai-codex",
        provider="openai-codex",
        model=model or _model_from_env("SECOND_BRAIN_CODEX_MODEL", "chatgpt-plan-default"),
        command=auth_profile.command,
        auth_profile=auth_profile.key,
    )


def _claude_profile(*, key_prefix: str, model: str | None = None) -> RuntimeProfile:
    return RuntimeProfile(
        key=f"{key_prefix}-claude",
        provider="claude",
        model=model or _primary_model_for_provider("claude"),
    )


def _gemini_profile(
    *,
    key_prefix: str,
    model: str | None = None,
    ladder_env: str = "SECOND_BRAIN_GEMINI_MODEL_LADDER",
) -> RuntimeProfile | None:
    auth_profile = resolve_gemini_auth_profile()
    if not gemini_auth_available(auth_profile):
        return None

    primary_model = model or _primary_model_for_provider("gemini")

    return RuntimeProfile(
        key=f"{key_prefix}-gemini-cli",
        provider="gemini-cli",
        model=primary_model,
        command=auth_profile.command,
        auth_profile=auth_profile.auth_type,
        candidate_models=_gemini_model_ladder(primary_model, env_var=ladder_env),
    )


def _openai_profile(*, key_prefix: str, model: str | None = None) -> RuntimeProfile | None:
    if not OPENAI_API_KEY:
        return None
    return RuntimeProfile(
        key=f"{key_prefix}-openai",
        provider="openai-compatible",
        model=model or _primary_model_for_provider("openai-compatible"),
        api_key=OPENAI_API_KEY,
        base_url=os.getenv("SECOND_BRAIN_RUNTIME_BASE_URL", "").strip() or None,
    )


def _openrouter_profile(*, key_prefix: str, model: str | None = None) -> RuntimeProfile | None:
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return None
    return RuntimeProfile(
        key=f"{key_prefix}-openrouter",
        provider="openrouter",
        model=model or _primary_model_for_provider("openrouter"),
        api_key=api_key,
        base_url=(
            os.getenv("SECOND_BRAIN_OPENROUTER_BASE_URL", "").strip()
            or "https://openrouter.ai/api/v1"
        ),
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
    requested_model = model or (
        request.fallback_model if key_prefix.startswith("fallback") and request else None
    ) or (request.model if request else None)

    # Model names are provider-specific (claude-sonnet-4-6 won't work on Codex).
    # Only pass the request model to Claude. Other providers use their own defaults.
    if provider == "claude":
        return _claude_profile(key_prefix=key_prefix, model=requested_model)
    if provider == "gemini-cli":
        ladder_env = (
            "SECOND_BRAIN_GEMINI_FALLBACK_MODEL_LADDER"
            if key_prefix.startswith("fallback")
            else "SECOND_BRAIN_GEMINI_MODEL_LADDER"
        )
        return _gemini_profile(key_prefix=key_prefix, model=None, ladder_env=ladder_env)
    if provider == "openai-codex":
        return _openai_codex_profile(key_prefix=key_prefix, model=None)
    if provider == "openrouter":
        return _openrouter_profile(key_prefix=key_prefix, model=None)
    if provider == "openai-compatible":
        return _openai_profile(key_prefix=key_prefix, model=None)
    return None


def resolve_runtime_profiles(request: RuntimeRequest) -> list[RuntimeProfile]:
    """Resolve runtime profiles via the routing policy layer."""

    from .routing import resolve_runtime_profiles as resolve_via_routing

    return resolve_via_routing(request)
