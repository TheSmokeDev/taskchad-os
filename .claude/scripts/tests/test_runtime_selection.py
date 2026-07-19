from __future__ import annotations

import os
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
from runtime.model_control import (  # noqa: E402
    CODEX_PLAN_DEFAULT_MODEL,
    apply_runtime_model_choice,
    configured_model_for_provider,
    model_observability_warning,
    resolve_runtime_model_choice,
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


def test_resolve_runtime_model_choice_maps_bare_claude_alias() -> None:
    choice = resolve_runtime_model_choice("sonnet")

    assert choice is not None
    assert choice.provider == "claude"
    assert choice.model == "claude-sonnet-4-6"
    assert choice.persist_model == "claude-sonnet-4-6"


def test_apply_runtime_model_choice_writes_explicit_codex_pin() -> None:
    writes: list[tuple[str, str]] = []
    removals: list[str] = []
    env: dict[str, str] = {}

    choice = apply_runtime_model_choice(
        "codex:gpt-5.5",
        environ=env,
        write_key=lambda key, value: writes.append((key, value)),
        delete_key=removals.append,
    )

    assert choice.provider == "openai-codex"
    assert choice.model == "gpt-5.5"
    assert env[RUNTIME_LANE_ENV_KEY] == RUNTIME_LANE_GENERIC
    assert env[GENERIC_PROVIDER_ENV_KEY] == "openai-codex"
    assert env[LEGACY_RUNTIME_PROVIDER_KEY] == "openai_codex"
    assert env["SECOND_BRAIN_CODEX_MODEL"] == "gpt-5.5"
    assert ("SECOND_BRAIN_CODEX_MODEL", "gpt-5.5") in writes
    assert removals == []


@pytest.mark.parametrize(
    "raw_choice",
    [
        "gpt5.5",
        "gpt 5.5",
        "gbt 5.5",
        "codex 5.5",
        "codec 5.5",
        "codex:gpt 5.5",
    ],
)
def test_apply_runtime_model_choice_normalizes_codex_shortcuts(raw_choice: str) -> None:
    env: dict[str, str] = {}

    choice = apply_runtime_model_choice(raw_choice, environ=env)

    assert choice.provider == "openai-codex"
    assert choice.model == "gpt-5.5"
    assert env[RUNTIME_LANE_ENV_KEY] == RUNTIME_LANE_GENERIC
    assert env[GENERIC_PROVIDER_ENV_KEY] == "openai-codex"
    assert env[LEGACY_RUNTIME_PROVIDER_KEY] == "openai_codex"
    assert env["SECOND_BRAIN_CODEX_MODEL"] == "gpt-5.5"


def test_apply_runtime_model_choice_codex_latest_shortcut_clears_model_pin() -> None:
    removals: list[str] = []
    env = {"SECOND_BRAIN_CODEX_MODEL": "gpt-5.5"}

    choice = apply_runtime_model_choice(
        "gpt latest",
        environ=env,
        delete_key=removals.append,
    )

    assert choice.provider == "openai-codex"
    assert choice.model == CODEX_PLAN_DEFAULT_MODEL
    assert choice.persist_model is None
    assert "SECOND_BRAIN_CODEX_MODEL" not in env
    assert "SECOND_BRAIN_CODEX_MODEL" in removals


def test_apply_runtime_model_choice_codex_default_clears_model_pin() -> None:
    writes: list[tuple[str, str]] = []
    removals: list[str] = []
    env = {"SECOND_BRAIN_CODEX_MODEL": "gpt-5.5"}

    choice = apply_runtime_model_choice(
        "codex:default",
        environ=env,
        write_key=lambda key, value: writes.append((key, value)),
        delete_key=removals.append,
    )

    assert choice.provider == "openai-codex"
    assert choice.model == CODEX_PLAN_DEFAULT_MODEL
    assert choice.persist_model is None
    assert "SECOND_BRAIN_CODEX_MODEL" not in env
    assert "SECOND_BRAIN_CODEX_MODEL" in removals
    assert ("SECOND_BRAIN_CODEX_MODEL", CODEX_PLAN_DEFAULT_MODEL) not in writes


def test_configured_model_for_provider_uses_codex_sentinel_default() -> None:
    model = configured_model_for_provider("codex", {})

    assert model == CODEX_PLAN_DEFAULT_MODEL
    warning = model_observability_warning("openai-codex", model)
    assert warning is not None
    assert "hidden model" in warning


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


def test_switch_provider_codex_default_warns_and_clears_model_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import config
    import core_handlers

    writes: list[tuple[str, str]] = []
    removals: list[str] = []

    monkeypatch.setenv("SECOND_BRAIN_CODEX_MODEL", "gpt-5.5")
    monkeypatch.setattr(core_handlers, "_write_env_var", lambda _path, key, value: writes.append((key, value)))
    monkeypatch.setattr(core_handlers, "_delete_env_var", lambda _path, key: removals.append(key))
    monkeypatch.setattr(config, "reload_config", lambda: None)

    message = core_handlers._switch_provider("codex:default")

    assert "Codex configured model: chatgpt-plan-default (default)" in message
    assert "hidden model" in message
    assert (GENERIC_PROVIDER_ENV_KEY, "openai-codex") in writes
    assert "SECOND_BRAIN_CODEX_MODEL" in removals
    assert "SECOND_BRAIN_CODEX_MODEL" not in os.environ


def test_write_env_var_creates_missing_profile_env(tmp_path: Path) -> None:
    import core_handlers

    env_path = tmp_path / "profile" / ".env"

    core_handlers._write_env_var(env_path, "SECOND_BRAIN_RUNTIME_LANE", "generic_runtime")

    assert env_path.read_text(encoding="utf-8").strip() == (
        "SECOND_BRAIN_RUNTIME_LANE=generic_runtime"
    )


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


@pytest.mark.asyncio
async def test_handle_provider_offloads_status_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """/provider must not block the event loop on a slow codex CLI (#130).

    ``_get_provider_status`` runs ``subprocess.run(['codex','login','status'],
    timeout=15)`` twice (~30s worst case). ``handle_provider`` must offload it via
    ``asyncio.to_thread``. Completion ORDER is the proof: if the status check ran
    on the loop, its synchronous sleep would block the 0.05s ticker and "status"
    would land before "ticked".
    """
    import asyncio

    import core_handlers

    order: list[str] = []

    def _slow_status() -> str:
        import time

        time.sleep(0.2)  # the ~30s double codex-CLI probe, scaled down for the test
        order.append("status")
        return "provider-ok"

    monkeypatch.setattr(core_handlers, "_get_provider_status", _slow_status)

    async def _ticker() -> None:
        await asyncio.sleep(0.05)
        order.append("ticked")

    result, _ = await asyncio.gather(
        core_handlers.handle_provider(adapter=None, incoming=None, args=""),
        _ticker(),
    )

    assert result == "provider-ok"
    assert order == ["ticked", "status"]


@pytest.mark.asyncio
async def test_handle_diagnostics_offloads_collect(monkeypatch: pytest.MonkeyPatch) -> None:
    """/diagnostics must not block the loop on collect_diagnostics() (#130).

    collect_diagnostics() reaches a synchronous browser_readiness() CDP probe
    (~9s worst case) plus blocking file/subprocess reads. handle_diagnostics must
    offload it via asyncio.to_thread. Completion ORDER is the proof: if it ran on
    the loop, its synchronous sleep would block the 0.05s ticker and "collected"
    would land before "ticked".
    """
    import asyncio

    import core_handlers
    from diagnostics import DiagnosticsReport

    order: list[str] = []

    def _slow_collect() -> DiagnosticsReport:
        import time

        time.sleep(0.2)  # the ~9s worst-case CDP + probe chain, scaled down
        order.append("collected")
        return DiagnosticsReport(
            timestamp="2026-07-17T00:00:00",
            uptime_seconds=1.0,
        )

    # handle_diagnostics does `from diagnostics import collect_diagnostics` at
    # call time, so patching the diagnostics-module attribute is what takes.
    import diagnostics as _diag

    monkeypatch.setattr(_diag, "collect_diagnostics", _slow_collect)
    monkeypatch.setitem(core_handlers._ctx, "adapters", {})

    async def _ticker() -> None:
        await asyncio.sleep(0.05)
        order.append("ticked")

    result, _ = await asyncio.gather(
        core_handlers.handle_diagnostics(adapter=None, incoming=None, args=""),
        _ticker(),
    )

    assert isinstance(result, str)
    assert order == ["ticked", "collected"]
