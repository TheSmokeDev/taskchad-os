"""Runtime model selection helpers for operator surfaces.

The lane selector owns *where* a request runs. This module owns the smaller
provider-model layer used by `/model provider:model` and status reporting.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass

from .profiles import GENERIC_PROVIDER_REGISTRY, normalize_provider
from .selection import RuntimeSelection, apply_runtime_selection_choice, provider_display_name

CLAUDE_MODEL_ENV_KEY = "SECOND_BRAIN_CLAUDE_MODEL"
CLAUDE_DEFAULT_MODEL = "claude-sonnet-5"
CODEX_PLAN_DEFAULT_MODEL = GENERIC_PROVIDER_REGISTRY["openai-codex"].default_model


@dataclass(frozen=True)
class RuntimeModelConfig:
    provider: str
    model_env_key: str
    default_model: str
    aliases: Mapping[str, str]


@dataclass(frozen=True)
class RuntimeModelChoice:
    provider: str
    model: str
    model_env_key: str
    persist_model: str | None
    used_default: bool
    input_value: str

    @property
    def selection_choice(self) -> str:
        return self.provider


MODEL_CONFIGS: dict[str, RuntimeModelConfig] = {
    "claude": RuntimeModelConfig(
        provider="claude",
        model_env_key=CLAUDE_MODEL_ENV_KEY,
        default_model=CLAUDE_DEFAULT_MODEL,
        aliases={
            "sonnet": "claude-sonnet-5",
            "opus": "claude-opus-4-8",
            "fable": "claude-fable-5",
        },
    ),
    # Named GPT-5.6 tier shortcuts for the Codex lane (verified 2026-07-18:
    # gpt-5.6-sol flagship, terra balanced, luna high-volume).
    "openai-codex": RuntimeModelConfig(
        provider="openai-codex",
        model_env_key=GENERIC_PROVIDER_REGISTRY["openai-codex"].model_env_var,
        default_model=GENERIC_PROVIDER_REGISTRY["openai-codex"].default_model,
        aliases={
            "sol": "gpt-5.6-sol",
            "terra": "gpt-5.6-terra",
            "luna": "gpt-5.6-luna",
        },
    ),
    **{
        provider: RuntimeModelConfig(
            provider=provider,
            model_env_key=overlay.model_env_var,
            default_model=overlay.default_model,
            aliases={},
        )
        for provider, overlay in GENERIC_PROVIDER_REGISTRY.items()
        if provider != "openai-codex"
    },
}

_DEFAULT_MODEL_ALIASES = {"default", "latest", "auto"}
_CODEX_DEFAULT_ALIASES = {
    "default",
    "latest",
    "auto",
    "plan-default",
    CODEX_PLAN_DEFAULT_MODEL,
}
_BARE_CODEX_SHORTHAND_RE = re.compile(
    r"^(?:codex|codec|gpt|gbt)\s*[-_: ]?\s*(\d+(?:\.\d+)?(?:[-_][a-z0-9.]+)*)$",
    re.IGNORECASE,
)
_CODEX_MODEL_SHORTHAND_RE = re.compile(
    r"^(?:(?:gpt|gbt)-?)?(\d+(?:\.\d+)?(?:-[a-z0-9.]+)*)$",
    re.IGNORECASE,
)


def resolve_runtime_model_choice(raw_choice: str) -> RuntimeModelChoice | None:
    """Resolve a provider:model selection or bare model alias.

    Returns ``None`` when ``raw_choice`` is not a model-control token, allowing
    callers to fall back to lane/provider-only selection.
    """

    raw = (raw_choice or "").strip()
    if not raw:
        return None

    lowered = raw.lower()
    claude_config = MODEL_CONFIGS["claude"]
    if lowered in claude_config.aliases:
        return RuntimeModelChoice(
            provider="claude",
            model=claude_config.aliases[lowered],
            model_env_key=claude_config.model_env_key,
            persist_model=claude_config.aliases[lowered],
            used_default=False,
            input_value=raw,
        )

    bare_codex_model = _bare_codex_model_shorthand(raw)
    if bare_codex_model is not None:
        return _choice_for_provider_model(MODEL_CONFIGS["openai-codex"], bare_codex_model)

    parsed = _split_provider_model(raw)
    if parsed is None:
        return None

    provider_token, model_token = parsed
    provider = normalize_provider(provider_token)
    config = MODEL_CONFIGS.get(provider)
    if config is None or not model_token:
        return None

    return _choice_for_provider_model(config, model_token)


def apply_runtime_model_choice(
    raw_choice: str,
    *,
    environ: MutableMapping[str, str] | None = None,
    write_key: Callable[[str, str], None] | None = None,
    delete_key: Callable[[str], None] | None = None,
) -> RuntimeModelChoice:
    """Apply a provider:model choice to process env and optional persistence."""

    choice = resolve_runtime_model_choice(raw_choice)
    if choice is None:
        raise ValueError(f"Unknown runtime model selection: {raw_choice}")

    target_env = environ if environ is not None else os.environ
    apply_runtime_selection_choice(
        choice.selection_choice,
        environ=target_env,
        write_key=write_key,
        delete_key=delete_key,
    )
    if choice.persist_model is None:
        if delete_key is not None:
            delete_key(choice.model_env_key)
        target_env.pop(choice.model_env_key, None)
    else:
        if write_key is not None:
            write_key(choice.model_env_key, choice.persist_model)
        target_env[choice.model_env_key] = choice.persist_model
    return choice


def configured_model_for_provider(
    provider: str,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Return the configured effective model for a provider."""

    canonical = normalize_provider(provider)
    config = MODEL_CONFIGS.get(canonical)
    if config is None:
        return None
    values = env if env is not None else os.environ
    return (values.get(config.model_env_key) or "").strip() or config.default_model


def configured_runtime_models(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return effective configured models for all runtime providers."""

    return {
        provider: configured_model_for_provider(provider, env) or config.default_model
        for provider, config in MODEL_CONFIGS.items()
    }


def selected_runtime_model(
    selection: RuntimeSelection,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Return the configured model for the current lane selection."""

    if selection.lane == "claude_native":
        return configured_model_for_provider("claude", env)
    if selection.generic_provider:
        return configured_model_for_provider(selection.generic_provider, env)
    return None


def runtime_model_warnings(
    selection: RuntimeSelection,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Return operator-facing warnings about configured model observability."""

    provider = selection.generic_provider if selection.generic_provider else None
    if provider != "openai-codex":
        return []
    model = configured_model_for_provider(provider, env)
    warning = model_observability_warning(provider, model)
    return [warning] if warning else []


def model_observability_warning(provider: str | None, model: str | None) -> str | None:
    """Warn when the configured model is a sentinel rather than observed model."""

    if normalize_provider(provider or "") == "openai-codex" and model == CODEX_PLAN_DEFAULT_MODEL:
        return (
            "Codex is configured as chatgpt-plan-default; the Codex CLI / "
            "ChatGPT plan chooses the concrete backend model, and this status "
            "surface has not observed that hidden model."
        )
    return None


def format_model_choice(choice: RuntimeModelChoice) -> str:
    """Human-readable summary for command responses."""

    label = provider_display_name(choice.provider)
    pin_state = "default" if choice.used_default else "pinned"
    return f"{label} configured model: {choice.model} ({pin_state})"


def _split_provider_model(raw: str) -> tuple[str, str] | None:
    for separator in (":", "="):
        if separator in raw:
            left, right = raw.split(separator, 1)
            return left.strip(), right.strip()
    parts = raw.split(None, 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return None


def _choice_for_provider_model(
    config: RuntimeModelConfig,
    model_token: str,
) -> RuntimeModelChoice:
    alias_key = model_token.strip().lower()
    aliases = dict(config.aliases)
    if config.provider == "openai-codex" and alias_key in _CODEX_DEFAULT_ALIASES:
        return RuntimeModelChoice(
            provider=config.provider,
            model=config.default_model,
            model_env_key=config.model_env_key,
            persist_model=None,
            used_default=True,
            input_value=model_token,
        )
    if alias_key in _DEFAULT_MODEL_ALIASES:
        return RuntimeModelChoice(
            provider=config.provider,
            model=config.default_model,
            model_env_key=config.model_env_key,
            persist_model=None,
            used_default=True,
            input_value=model_token,
        )
    if alias_key in aliases:
        model = aliases[alias_key]
        return RuntimeModelChoice(
            provider=config.provider,
            model=model,
            model_env_key=config.model_env_key,
            persist_model=model,
            used_default=False,
            input_value=model_token,
        )
    model = _normalize_model_token(config, model_token)
    return RuntimeModelChoice(
        provider=config.provider,
        model=model,
        model_env_key=config.model_env_key,
        persist_model=model,
        used_default=False,
        input_value=model_token,
    )


def _bare_codex_model_shorthand(raw: str) -> str | None:
    match = _BARE_CODEX_SHORTHAND_RE.fullmatch(raw.strip())
    if not match:
        return None
    return match.group(1)


def _normalize_model_token(config: RuntimeModelConfig, model_token: str) -> str:
    model = model_token.strip()
    if config.provider != "openai-codex":
        return model

    normalized = re.sub(r"[\s_]+", "-", model.lower())
    match = _CODEX_MODEL_SHORTHAND_RE.fullmatch(normalized)
    if match:
        return f"gpt-{match.group(1)}"
    return model
