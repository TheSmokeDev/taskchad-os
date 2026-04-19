"""Lane-aware runtime selection helpers for operator and runtime surfaces."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass

from .base import RUNTIME_LANE_CLAUDE_NATIVE, RUNTIME_LANE_GENERIC
from .profiles import GENERIC_PROVIDER_REGISTRY, normalize_provider

RUNTIME_LANE_ENV_KEY = "SECOND_BRAIN_RUNTIME_LANE"
GENERIC_PROVIDER_ENV_KEY = "SECOND_BRAIN_GENERIC_PROVIDER"
LEGACY_RUNTIME_PROVIDER_KEY = "SECOND_BRAIN_RUNTIME_PROVIDER"


def _build_generic_aliases() -> dict[str, str]:
    return {
        alias: canonical
        for canonical, overlay in GENERIC_PROVIDER_REGISTRY.items()
        for alias in overlay.aliases
    }


def _build_legacy_write_values() -> dict[str, str]:
    return {
        canonical: overlay.legacy_write_key
        for canonical, overlay in GENERIC_PROVIDER_REGISTRY.items()
    }


def _build_display_names() -> dict[str, str]:
    names: dict[str, str] = {"claude": "Claude"}
    for canonical, overlay in GENERIC_PROVIDER_REGISTRY.items():
        names[canonical] = overlay.display_name
    return names


_GENERIC_PROVIDER_ALIASES = _build_generic_aliases()
_LEGACY_PROVIDER_WRITE_VALUES = _build_legacy_write_values()
_PROVIDER_DISPLAY_NAMES = _build_display_names()


@dataclass(slots=True, eq=True)
class RuntimeSelection:
    lane: str | None = None
    generic_provider: str | None = None

    @property
    def is_auto(self) -> bool:
        return self.lane is None and self.generic_provider is None


def _normalize_lane_name(raw: str | None) -> str | None:
    normalized = (raw or "").strip().lower().replace("-", "_")
    if not normalized or normalized == "auto":
        return None
    if normalized in {RUNTIME_LANE_CLAUDE_NATIVE, "claude", "anthropic"}:
        return RUNTIME_LANE_CLAUDE_NATIVE
    if normalized in {RUNTIME_LANE_GENERIC, "generic"}:
        return RUNTIME_LANE_GENERIC
    return None


def _normalize_generic_provider(raw: str | None) -> str | None:
    normalized = (raw or "").strip().lower()
    if not normalized or normalized == "auto":
        return None
    if normalized in _GENERIC_PROVIDER_ALIASES:
        return _GENERIC_PROVIDER_ALIASES[normalized]
    canonical = normalize_provider(normalized)
    if canonical in _PROVIDER_DISPLAY_NAMES:
        return canonical
    return None


def resolve_runtime_selection(
    env: Mapping[str, str] | None = None,
) -> RuntimeSelection:
    """Resolve lane + generic provider from canonical keys and legacy aliases."""

    values = env if env is not None else os.environ
    explicit_lane = _normalize_lane_name(values.get(RUNTIME_LANE_ENV_KEY))
    explicit_generic = _normalize_generic_provider(values.get(GENERIC_PROVIDER_ENV_KEY))
    legacy_provider = (values.get(LEGACY_RUNTIME_PROVIDER_KEY) or "").strip()
    legacy_lane = _normalize_lane_name(legacy_provider)
    legacy_generic = _normalize_generic_provider(legacy_provider)

    if explicit_lane == RUNTIME_LANE_CLAUDE_NATIVE:
        return RuntimeSelection(lane=RUNTIME_LANE_CLAUDE_NATIVE)
    if explicit_lane == RUNTIME_LANE_GENERIC:
        return RuntimeSelection(
            lane=RUNTIME_LANE_GENERIC,
            generic_provider=explicit_generic or legacy_generic,
        )
    if explicit_generic:
        return RuntimeSelection(
            lane=RUNTIME_LANE_GENERIC,
            generic_provider=explicit_generic,
        )
    if legacy_lane == RUNTIME_LANE_CLAUDE_NATIVE:
        return RuntimeSelection(lane=RUNTIME_LANE_CLAUDE_NATIVE)
    if legacy_generic:
        return RuntimeSelection(
            lane=RUNTIME_LANE_GENERIC,
            generic_provider=legacy_generic,
        )
    return RuntimeSelection()


def runtime_selection_choice(selection: RuntimeSelection) -> str:
    """Return a user-facing selector token for the current selection."""

    if selection.lane == RUNTIME_LANE_CLAUDE_NATIVE:
        return "claude"
    if selection.generic_provider == "openai-codex":
        return "codex"
    if selection.generic_provider == "gemini-cli":
        return "gemini"
    if selection.generic_provider == "openrouter":
        return "openrouter"
    if selection.generic_provider == "openai-compatible":
        return "openai"
    return "auto"


def provider_display_name(provider: str | None) -> str:
    canonical = _normalize_generic_provider(provider) or (provider or "").strip().lower()
    return _PROVIDER_DISPLAY_NAMES.get(canonical, provider or "unknown")


def describe_runtime_selection(selection: RuntimeSelection) -> str:
    if selection.lane == RUNTIME_LANE_CLAUDE_NATIVE:
        return "Claude native lane"
    if selection.lane == RUNTIME_LANE_GENERIC and selection.generic_provider:
        return f"generic runtime via {provider_display_name(selection.generic_provider)}"
    if selection.lane == RUNTIME_LANE_GENERIC:
        return "generic runtime"
    return "automatic lane/provider routing"


def runtime_env_updates_for_choice(choice: str) -> dict[str, str | None]:
    """Return canonical + legacy env updates for a user-facing selection token."""

    normalized = (choice or "").strip().lower()
    if normalized == "auto":
        return {
            RUNTIME_LANE_ENV_KEY: None,
            GENERIC_PROVIDER_ENV_KEY: None,
            LEGACY_RUNTIME_PROVIDER_KEY: None,
        }
    if normalized == "claude":
        return {
            RUNTIME_LANE_ENV_KEY: RUNTIME_LANE_CLAUDE_NATIVE,
            GENERIC_PROVIDER_ENV_KEY: None,
            LEGACY_RUNTIME_PROVIDER_KEY: "claude",
        }

    provider = _normalize_generic_provider(normalized)
    if provider is None:
        raise ValueError(f"Unknown runtime selection: {choice}")

    return {
        RUNTIME_LANE_ENV_KEY: RUNTIME_LANE_GENERIC,
        GENERIC_PROVIDER_ENV_KEY: provider,
        LEGACY_RUNTIME_PROVIDER_KEY: _LEGACY_PROVIDER_WRITE_VALUES.get(provider, normalized),
    }


def apply_runtime_selection_choice(
    choice: str,
    *,
    environ: MutableMapping[str, str] | None = None,
    write_key: Callable[[str, str], None] | None = None,
    delete_key: Callable[[str], None] | None = None,
) -> RuntimeSelection:
    """Apply a selection to process env and optional persistence hooks."""

    target_env = environ if environ is not None else os.environ
    updates = runtime_env_updates_for_choice(choice)

    for key, value in updates.items():
        if value is None:
            if delete_key is not None:
                delete_key(key)
            target_env.pop(key, None)
            continue
        if write_key is not None:
            write_key(key, value)
        target_env[key] = value

    return resolve_runtime_selection(target_env)
