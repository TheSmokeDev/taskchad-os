"""Tests for the registry-driven generic runtime provider system.

The registry in `runtime.profiles.GENERIC_PROVIDER_REGISTRY` is the single
source of truth for transport, auth, aliases, routing priorities, and display
names for the five generic-lane providers. These tests verify that every
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

CANONICAL_KEYS = ("openai-compatible", "openrouter", "openai-codex", "gemini-cli", "kimi")


def test_registry_completeness() -> None:
    """All 5 canonical keys are present and every overlay field is populated."""

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
        "kimi",
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
        "kimi": "kimi",
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
        ("kimi", OpenAICompatibleRuntime),
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
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)

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


def test_openai_key_rotation_honored_without_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule 1: profile resolution reads OPENAI_API_KEY at call time (#137).

    reload_config() re-runs load_dotenv(override=True), so a key rotated in
    .env + /reload must be honored by the openai-compatible lane without a bot
    restart. The old import-time module snapshot masked the new value.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-old")
    p1 = profiles.build_profile_for_provider("openai-compatible", key_prefix="primary")
    assert p1 is not None and p1.api_key == "sk-old"

    monkeypatch.setenv("OPENAI_API_KEY", "sk-new")  # what /reload's load_dotenv does
    p2 = profiles.build_profile_for_provider("openai-compatible", key_prefix="primary")
    assert p2 is not None and p2.api_key == "sk-new"


def test_kimi_profile_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """kimi resolves through the registry: chat-completions wire, coding base URL, k3 default.

    Wire probe 2026-07-17: api.kimi.com/coding/v1/responses = 404,
    /chat/completions = 200 — hence wire_api="chat_completions".
    """

    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi-test")
    monkeypatch.delenv("SECOND_BRAIN_KIMI_MODEL", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_KIMI_BASE_URL", raising=False)

    profile = build_profile_for_provider("kimi", key_prefix="primary")

    assert profile is not None
    assert profile.provider == "kimi"
    assert profile.model == "k3"
    assert profile.base_url == "https://api.kimi.com/coding/v1"
    assert profile.api_key == "sk-kimi-test"
    assert GENERIC_PROVIDER_REGISTRY["kimi"].wire_api == "chat_completions"


def test_kimi_selection_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """'/model kimi' persists canonical + legacy keys and reads back as the kimi token."""

    from runtime.base import RUNTIME_LANE_GENERIC
    from runtime.selection import (
        GENERIC_PROVIDER_ENV_KEY,
        LEGACY_RUNTIME_PROVIDER_KEY,
        RUNTIME_LANE_ENV_KEY,
        RuntimeSelection,
        apply_runtime_selection_choice,
        runtime_selection_choice,
    )

    env: dict[str, str] = {}
    selection = apply_runtime_selection_choice("kimi", environ=env)
    assert selection.generic_provider == "kimi"
    assert env[GENERIC_PROVIDER_ENV_KEY] == "kimi"
    assert env[LEGACY_RUNTIME_PROVIDER_KEY] == "kimi"
    assert env[RUNTIME_LANE_ENV_KEY] == RUNTIME_LANE_GENERIC
    assert runtime_selection_choice(
        RuntimeSelection(lane=RUNTIME_LANE_GENERIC, generic_provider="kimi")
    ) == "kimi"


def test_kimi_model_pin_uses_kimi_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """'/model kimi:<model>' writes SECOND_BRAIN_KIMI_MODEL via model_control."""

    from runtime.model_control import resolve_runtime_model_choice

    choice = resolve_runtime_model_choice("kimi:k3")
    assert choice is not None
    assert choice.provider == "kimi"
    assert choice.model == "k3"
    assert choice.model_env_key == "SECOND_BRAIN_KIMI_MODEL"

    default = resolve_runtime_model_choice("kimi:default")
    assert default is not None
    assert default.model == "k3"
    assert default.persist_model is None


def test_kimi_adapter_ignores_claude_lane_request_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chat_completions wire must not leak the Claude-lane request model.

    engine.py passes SECOND_BRAIN_CLAUDE_MODEL as request.model on every turn;
    the kimi lane must send its own pinned model (profile.model) instead.
    """

    import asyncio
    import sys
    import types

    from runtime.base import RuntimeRequest
    from runtime.openai_compatible import OpenAICompatibleRuntime
    from runtime.profiles import RuntimeProfile

    captured: dict = {}

    class _FakeMessage:
        content = "OK"

    class _FakeChoice:
        message = _FakeMessage()

    class _FakeUsage:
        prompt_tokens = 89
        completion_tokens = 31
        total_tokens = 120
        cached_tokens = 89

    class _FakeCompletion:
        choices = [_FakeChoice()]
        usage = _FakeUsage()

    class _FakeChatCompletions:
        async def create(self, *, model, messages):
            captured["model"] = model
            return _FakeCompletion()

    class _FakeClient:
        def __init__(self, **_kwargs):
            self.chat = types.SimpleNamespace(completions=_FakeChatCompletions())

    monkeypatch.setitem(
        sys.modules, "openai", types.SimpleNamespace(AsyncOpenAI=_FakeClient)
    )

    profile = RuntimeProfile(
        key="primary-kimi",
        provider="kimi",
        model="k3",
        api_key="sk-test",
        base_url="https://api.kimi.com/coding/v1",
    )
    request = RuntimeRequest(
        prompt="hi", cwd=".", task_name="chat_turn", model="claude-sonnet-4-6"
    )

    result = asyncio.run(OpenAICompatibleRuntime(profile).run(request))

    assert captured["model"] == "k3"
    assert result.model == "k3"
    assert result.provider == "kimi"
    assert result.usage == {
        "prompt_tokens": 89,
        "completion_tokens": 31,
        "total_tokens": 120,
        "cached_tokens": 89,
    }
