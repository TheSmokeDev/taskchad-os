"""Tests for the registry-driven generic runtime provider system.

The registry in `runtime.profiles.GENERIC_PROVIDER_REGISTRY` is the single
source of truth for transport, auth, aliases, routing priorities, and display
names for the four generic-lane providers. These tests verify that every
derived surface (routes, aliases, legacy writes, display names, adapter
dispatch) stays in sync with the registry.
"""

from __future__ import annotations

import pytest

import runtime.profiles as profiles
from runtime.base import RuntimeRequest
from runtime.claude_sdk import ClaudeSdkRuntime
from runtime.gemini_cli import GeminiCliRuntime
from runtime.lane_router import _adapter_for
from runtime.openai_codex import OpenAICodexRuntime
from runtime.openai_compatible import OpenAICompatibleRuntime
from runtime.profiles import (
    GENERIC_PROVIDER_REGISTRY,
    PROVIDER_ALIASES,
    GenericProviderOverlay,
    RuntimeProfile,
    build_profile_for_provider,
)
from runtime.routing import GENERIC_TEXT_ROUTE, GENERIC_TOOL_ROUTE
from runtime.selection import (
    _GENERIC_PROVIDER_ALIASES,
    _LEGACY_PROVIDER_WRITE_VALUES,
    _PROVIDER_DISPLAY_NAMES,
)

CANONICAL_KEYS = ("openai-compatible", "openrouter", "openai-codex", "gemini-cli")


def test_registry_completeness() -> None:
    """All 4 canonical keys are present and every overlay field is populated."""

    assert set(GENERIC_PROVIDER_REGISTRY.keys()) == set(CANONICAL_KEYS)

    for key, overlay in GENERIC_PROVIDER_REGISTRY.items():
        assert isinstance(overlay, GenericProviderOverlay)
        assert overlay.transport in {"subprocess_cli", "openai_responses"}
        assert overlay.auth_type in {"codex", "gemini", "api_key"}
        assert overlay.display_name, f"{key}: display_name empty"
        assert overlay.model_env_var, f"{key}: model_env_var empty"
        assert overlay.default_model, f"{key}: default_model empty"
        assert overlay.aliases, f"{key}: aliases tuple empty"
        assert overlay.legacy_write_key, f"{key}: legacy_write_key empty"
        # transport-specific invariants
        if overlay.transport == "openai_responses":
            assert overlay.api_key_env_vars, f"{key}: HTTP transport needs api_key_env_vars"
        if overlay.transport == "subprocess_cli":
            assert not overlay.api_key_env_vars, (
                f"{key}: CLI transport should not set api_key_env_vars"
            )


def test_alias_uniqueness() -> None:
    """No alias maps to two different canonical providers."""

    seen: dict[str, str] = {}
    for canonical, overlay in GENERIC_PROVIDER_REGISTRY.items():
        for alias in overlay.aliases:
            assert alias not in seen or seen[alias] == canonical, (
                f"Alias {alias!r} maps to both {seen.get(alias)!r} and {canonical!r}"
            )
            seen[alias] = canonical


def test_tool_route_derivation() -> None:
    """GENERIC_TOOL_ROUTE excludes providers with tool_route_priority < 0."""

    assert GENERIC_TOOL_ROUTE == ("openai-codex", "gemini-cli")

    tool_priorities = [
        overlay.tool_route_priority
        for overlay in GENERIC_PROVIDER_REGISTRY.values()
        if overlay.tool_route_priority >= 0
    ]
    assert len(set(tool_priorities)) == len(tool_priorities), (
        "Duplicate tool_route_priority produces arbitrary tie-break ordering"
    )


def test_text_route_derivation() -> None:
    """GENERIC_TEXT_ROUTE includes every registry entry in text_route_priority order."""

    assert GENERIC_TEXT_ROUTE == (
        "openai-compatible",
        "openrouter",
        "openai-codex",
        "gemini-cli",
    )

    text_priorities = [
        overlay.text_route_priority for overlay in GENERIC_PROVIDER_REGISTRY.values()
    ]
    assert len(set(text_priorities)) == len(text_priorities), (
        "Duplicate text_route_priority produces arbitrary tie-break ordering"
    )


def test_provider_aliases_derivation() -> None:
    """PROVIDER_ALIASES contains every registry alias + claude/anthropic."""

    for canonical, overlay in GENERIC_PROVIDER_REGISTRY.items():
        for alias in overlay.aliases:
            actual = PROVIDER_ALIASES.get(alias)
            assert actual == canonical, (
                f"PROVIDER_ALIASES[{alias!r}] == {actual!r}, expected {canonical!r}"
            )

    # claude-native aliases are not in the generic registry but must still resolve
    assert PROVIDER_ALIASES["claude"] == "claude"
    assert PROVIDER_ALIASES["anthropic"] == "claude"


def test_generic_provider_aliases_derivation() -> None:
    """selection._GENERIC_PROVIDER_ALIASES mirrors the registry's alias tuples."""

    for canonical, overlay in GENERIC_PROVIDER_REGISTRY.items():
        for alias in overlay.aliases:
            assert _GENERIC_PROVIDER_ALIASES[alias] == canonical


def test_legacy_write_values_derivation() -> None:
    """selection._LEGACY_PROVIDER_WRITE_VALUES uses each overlay.legacy_write_key."""

    assert _LEGACY_PROVIDER_WRITE_VALUES == {
        "openai-codex": "openai_codex",
        "gemini-cli": "gemini",
        "openrouter": "openrouter",
        "openai-compatible": "openai",
    }

    for canonical, overlay in GENERIC_PROVIDER_REGISTRY.items():
        assert _LEGACY_PROVIDER_WRITE_VALUES[canonical] == overlay.legacy_write_key

    # display names are derived too; sanity-check Claude is still present
    assert _PROVIDER_DISPLAY_NAMES["claude"] == "Claude"
    for canonical, overlay in GENERIC_PROVIDER_REGISTRY.items():
        assert _PROVIDER_DISPLAY_NAMES[canonical] == overlay.display_name


@pytest.mark.parametrize(
    "provider, adapter_cls",
    [
        ("claude", ClaudeSdkRuntime),
        ("openai-codex", OpenAICodexRuntime),
        ("gemini-cli", GeminiCliRuntime),
        ("openai-compatible", OpenAICompatibleRuntime),
        ("openrouter", OpenAICompatibleRuntime),
    ],
)
def test_adapter_for_dispatch(provider: str, adapter_cls: type) -> None:
    """_adapter_for returns the correct adapter class for every registered provider."""

    profile = RuntimeProfile(key=f"test-{provider}", provider=provider, model="x")
    adapter = _adapter_for(profile)
    assert isinstance(adapter, adapter_cls)


def test_build_profile_returns_none_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_profile_for_provider returns None when auth/env prerequisites are missing."""

    # HTTP providers: clear API keys so _resolve_api_key_from_env_vars returns empty
    monkeypatch.setattr(profiles, "OPENAI_API_KEY", "")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    # CLI providers: mock auth as unavailable. profiles.py imports these names
    # at module load, so monkeypatching the source module is a no-op here.
    monkeypatch.setattr(profiles, "codex_auth_available", lambda _auth: False)
    monkeypatch.setattr(profiles, "gemini_auth_available", lambda _auth: False)

    request = RuntimeRequest(prompt="hi", cwd=".", task_name="memory_flush")
    for canonical in CANONICAL_KEYS:
        profile = build_profile_for_provider(canonical, key_prefix="primary", request=request)
        assert profile is None, f"{canonical}: expected None when unavailable, got {profile!r}"


def test_unknown_provider_returns_none() -> None:
    """Unknown providers return None rather than raising."""

    assert build_profile_for_provider("kimi-cli", key_prefix="test") is None
    assert build_profile_for_provider("not-a-real-provider", key_prefix="test") is None


def test_tool_route_priority_matches_membership() -> None:
    """An overlay's tool_route_priority >= 0 iff its canonical name is in GENERIC_TOOL_ROUTE."""

    for canonical, overlay in GENERIC_PROVIDER_REGISTRY.items():
        in_tool_route = canonical in GENERIC_TOOL_ROUTE
        priority_allows_tool = overlay.tool_route_priority >= 0
        assert in_tool_route == priority_allows_tool, (
            f"{canonical}: tool_route_priority={overlay.tool_route_priority}, "
            f"in_tool_route={in_tool_route}"
        )
