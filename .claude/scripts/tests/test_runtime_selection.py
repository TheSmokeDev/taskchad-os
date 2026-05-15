from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_CHAT_DIR = str(Path(__file__).parent.parent.parent / "chat")
_SCRIPTS_DIR = str(Path(__file__).parent.parent)
if _CHAT_DIR not in sys.path:
    sys.path.insert(0, _CHAT_DIR)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from runtime.base import RUNTIME_LANE_CLAUDE_NATIVE, RUNTIME_LANE_GENERIC  # noqa: E402
from runtime.selection import (  # noqa: E402
    GENERIC_PROVIDER_ENV_KEY,
    LEGACY_RUNTIME_PROVIDER_KEY,
    RUNTIME_LANE_ENV_KEY,
    RuntimeSelection,
    apply_runtime_selection_choice,
    resolve_runtime_selection,
    runtime_env_updates_for_choice,
)


def test_resolve_runtime_selection_defaults_to_auto() -> None:
    selection = resolve_runtime_selection({})

    assert selection == RuntimeSelection()


def test_resolve_runtime_selection_maps_legacy_claude_pin() -> None:
    selection = resolve_runtime_selection({
        LEGACY_RUNTIME_PROVIDER_KEY: "claude",
    })

    assert selection.lane == RUNTIME_LANE_CLAUDE_NATIVE
    assert selection.generic_provider is None


def test_resolve_runtime_selection_maps_legacy_generic_pin() -> None:
    selection = resolve_runtime_selection({
        LEGACY_RUNTIME_PROVIDER_KEY: "openai_codex",
    })

    assert selection.lane == RUNTIME_LANE_GENERIC
    assert selection.generic_provider == "openai-codex"


def test_runtime_env_updates_for_choice_codex() -> None:
    updates = runtime_env_updates_for_choice("codex")

    assert updates == {
        RUNTIME_LANE_ENV_KEY: RUNTIME_LANE_GENERIC,
        GENERIC_PROVIDER_ENV_KEY: "openai-codex",
        LEGACY_RUNTIME_PROVIDER_KEY: "openai_codex",
    }


def test_runtime_env_updates_for_choice_auto_clears_state() -> None:
    updates = runtime_env_updates_for_choice("auto")

    assert updates == {
        RUNTIME_LANE_ENV_KEY: None,
        GENERIC_PROVIDER_ENV_KEY: None,
        LEGACY_RUNTIME_PROVIDER_KEY: None,
    }


def test_apply_runtime_selection_choice_updates_environ_and_callbacks() -> None:
    writes: list[tuple[str, str]] = []
    removals: list[str] = []
    env = {
        RUNTIME_LANE_ENV_KEY: RUNTIME_LANE_CLAUDE_NATIVE,
        LEGACY_RUNTIME_PROVIDER_KEY: "claude",
    }

    selection = apply_runtime_selection_choice(
        "gemini",
        environ=env,
        write_key=lambda key, value: writes.append((key, value)),
        delete_key=removals.append,
    )

    assert selection.lane == RUNTIME_LANE_GENERIC
    assert selection.generic_provider == "gemini-cli"
    assert env[RUNTIME_LANE_ENV_KEY] == RUNTIME_LANE_GENERIC
    assert env[GENERIC_PROVIDER_ENV_KEY] == "gemini-cli"
    assert env[LEGACY_RUNTIME_PROVIDER_KEY] == "gemini"
    assert writes == [
        (RUNTIME_LANE_ENV_KEY, RUNTIME_LANE_GENERIC),
        (GENERIC_PROVIDER_ENV_KEY, "gemini-cli"),
        (LEGACY_RUNTIME_PROVIDER_KEY, "gemini"),
    ]
    assert removals == []


def test_switch_provider_writes_lane_aware_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import config
    import core_handlers

    writes: list[tuple[str, str]] = []
    removals: list[str] = []

    monkeypatch.setattr(core_handlers, "_write_env_var", lambda _path, key, value: writes.append((key, value)))
    monkeypatch.setattr(core_handlers, "_delete_env_var", lambda _path, key: removals.append(key))
    monkeypatch.setattr(config, "reload_config", lambda: None)

    message = core_handlers._switch_provider("codex")

    assert "generic runtime via Codex" in message
    assert writes == [
        (RUNTIME_LANE_ENV_KEY, RUNTIME_LANE_GENERIC),
        (GENERIC_PROVIDER_ENV_KEY, "openai-codex"),
        (LEGACY_RUNTIME_PROVIDER_KEY, "openai_codex"),
    ]
    assert removals == []


def test_switch_provider_claude_clears_generic_preference(monkeypatch: pytest.MonkeyPatch) -> None:
    import config
    import core_handlers

    writes: list[tuple[str, str]] = []
    removals: list[str] = []

    monkeypatch.setattr(core_handlers, "_write_env_var", lambda _path, key, value: writes.append((key, value)))
    monkeypatch.setattr(core_handlers, "_delete_env_var", lambda _path, key: removals.append(key))
    monkeypatch.setattr(config, "reload_config", lambda: None)

    message = core_handlers._switch_provider("claude")

    assert "Claude native lane" in message
    assert (RUNTIME_LANE_ENV_KEY, RUNTIME_LANE_CLAUDE_NATIVE) in writes
    assert (LEGACY_RUNTIME_PROVIDER_KEY, "claude") in writes
    assert GENERIC_PROVIDER_ENV_KEY in removals


def test_provider_status_omits_legacy_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    import core_handlers
    import runtime.auth_profiles as auth_profiles
    import runtime.health as runtime_health
    import runtime.profiles as profiles
    import runtime.routing as routing
    import runtime.selection as selection

    monkeypatch.setattr(
        core_handlers,
        "resolve_runtime_selection",
        lambda _env=None: selection.RuntimeSelection(
            lane=RUNTIME_LANE_GENERIC,
            generic_provider="openai-codex",
        ),
    )
    monkeypatch.setattr(
        routing,
        "GENERIC_TEXT_ROUTE",
        ("openai", "openrouter"),
    )
    monkeypatch.setattr(
        routing,
        "GENERIC_TOOL_ROUTE",
        ("openai_codex", "gemini"),
    )
    monkeypatch.setattr(
        routing,
        "DEFAULT_PROVIDER_CHAIN",
        ("claude", "openai_codex"),
    )
    monkeypatch.setattr(
        profiles,
        "build_profile_for_provider",
        lambda provider, **_kwargs: object() if provider else None,
    )
    monkeypatch.setattr(runtime_health, "is_profile_available", lambda _profile: True)
    monkeypatch.setattr(
        auth_profiles,
        "codex_auth_status",
        lambda _profile: SimpleNamespace(available=True),
    )
    monkeypatch.setattr(
        auth_profiles,
        "gemini_auth_status",
        lambda _profile: SimpleNamespace(available=True),
    )
    monkeypatch.setattr(
        core_handlers,
        "provider_display_name",
        lambda provider: {
            "openai": "OpenAI",
            "openrouter": "OpenRouter",
            "openai-codex": "Codex",
            "openai_codex": "Codex",
            "gemini": "Gemini",
            "gemini-cli": "Gemini",
            "claude": "Claude",
        }.get(provider, provider),
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai")

    message = core_handlers._get_provider_status()

    assert "Generic text route: OpenAI -> OpenRouter" in message
    assert "Generic tool route: Codex -> Gemini" in message
    assert "generic preferred provider: Codex" in message
    assert "Chain:" not in message


@pytest.mark.asyncio
async def test_handle_diagnostics_omits_legacy_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    import core_handlers
    import diagnostics
    from diagnostics import DiagnosticsReport

    monkeypatch.setattr(
        diagnostics,
        "collect_diagnostics",
        lambda: DiagnosticsReport(
            timestamp="2026-04-11T00:00:00",
            uptime_seconds=1.0,
            runtime_lanes={"claude_native": "ON", "generic_runtime": "ON"},
            runtime_providers={"claude": "ON", "openai-codex": "ON"},
            runtime_selected_lane="generic_runtime",
            runtime_selected_generic_provider="openai-codex",
            runtime_generic_text_route=["openai-compatible"],
            runtime_generic_tool_route=["openai-codex"],
        ),
    )
    monkeypatch.setattr(
        core_handlers,
        "provider_display_name",
        lambda provider: {
            "openai-compatible": "OpenAI",
            "openai-codex": "Codex",
        }.get(provider, provider),
    )
    monkeypatch.setitem(core_handlers._ctx, "adapters", {})

    message = await core_handlers.handle_diagnostics(None, None, "")

    assert "generic preferred provider: Codex" in message
    assert "generic text route: OpenAI" in message
    assert "generic tool route: Codex" in message
    assert "Chain:" not in message


@pytest.mark.asyncio
async def test_handle_diagnostics_surfaces_runtime_and_lifecycle_attention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core_handlers
    import diagnostics
    from diagnostics import DiagnosticsReport

    monkeypatch.setattr(
        diagnostics,
        "collect_diagnostics",
        lambda: DiagnosticsReport(
            timestamp="2026-05-14T00:00:00",
            uptime_seconds=1.0,
            runtime_lanes={"claude_native": "ON", "generic_runtime": "ON"},
            runtime_providers={"openai-codex": "OFF", "gemini-cli": "ON"},
            runtime_selected_lane="generic_runtime",
            runtime_selected_generic_provider="openai-codex",
            runtime_generic_text_route=["openai-codex", "gemini-cli"],
            runtime_generic_tool_route=["openai-codex", "gemini-cli"],
            runtime_auth_issues={
                "openai-codex": "Codex CLI auth is stale. Run `codex login`.",
            },
            clear_lifecycle_recent_failures=2,
            clear_lifecycle_last_failure="session-end-flush.py: exit 1",
        ),
    )
    monkeypatch.setattr(
        core_handlers,
        "provider_display_name",
        lambda provider: {
            "openai-codex": "Codex",
            "gemini-cli": "Gemini",
        }.get(provider, provider),
    )
    monkeypatch.setitem(core_handlers._ctx, "adapters", {})

    message = await core_handlers.handle_diagnostics(None, None, "")

    assert "Auth attention:" in message
    assert "Codex: Codex CLI auth is stale" in message
    assert "*Lifecycle*:" in message
    assert "Clear lifecycle warnings/errors (recent): 2" in message
    assert "session-end-flush.py: exit 1" in message
