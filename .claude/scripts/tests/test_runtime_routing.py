from __future__ import annotations

import pytest

import runtime.health as health
import runtime.profiles as profiles
import runtime.routing as routing
from runtime.base import RuntimeRequest
from runtime.profiles import RuntimeProfile


def _profile(provider: str, key_prefix: str = "primary") -> RuntimeProfile:
    return RuntimeProfile(
        key=f"{key_prefix}-{provider}",
        provider=profiles.normalize_provider(provider),
        model=f"{provider}-model",
    )


def test_default_text_route_prefers_gemini_then_codex_then_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_LANE", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_GENERIC_PROVIDER", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_PROVIDER", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_FALLBACK_PROVIDER", raising=False)
    monkeypatch.setattr(
        routing,
        "build_profile_for_provider",
        lambda provider, *, key_prefix, request=None: _profile(provider, key_prefix),
    )
    monkeypatch.setattr(routing, "is_profile_available", lambda _profile: True)

    resolved = routing.resolve_runtime_profiles(
        RuntimeRequest(prompt="hi", cwd=".", task_name="memory_flush")
    )

    assert [profile.provider for profile in resolved] == [
        "gemini-cli",
        "openai-codex",
        "openrouter",
        "openai-compatible",
        "claude",
    ]


def test_routing_skips_unhealthy_primary_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_LANE", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_GENERIC_PROVIDER", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_PROVIDER", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_FALLBACK_PROVIDER", raising=False)
    monkeypatch.setattr(
        routing,
        "build_profile_for_provider",
        lambda provider, *, key_prefix, request=None: _profile(provider, key_prefix),
    )
    monkeypatch.setattr(
        routing,
        "is_profile_available",
        lambda profile: profile.provider != "gemini-cli",
    )

    resolved = routing.resolve_runtime_profiles(
        RuntimeRequest(prompt="hi", cwd=".", task_name="memory_flush")
    )

    assert resolved[0].provider == "openai-codex"


def test_chat_turn_text_only_prefers_text_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_LANE", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_GENERIC_PROVIDER", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_PROVIDER", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_FALLBACK_PROVIDER", raising=False)
    monkeypatch.setattr(
        routing,
        "build_profile_for_provider",
        lambda provider, *, key_prefix, request=None: _profile(provider, key_prefix),
    )
    monkeypatch.setattr(routing, "is_profile_available", lambda _profile: True)

    resolved = routing.resolve_runtime_profiles(
        RuntimeRequest(prompt="hi", cwd=".", task_name="chat_turn")
    )

    assert [profile.provider for profile in resolved] == [
        "gemini-cli",
        "openai-codex",
        "openrouter",
        "openai-compatible",
        "claude",
    ]


def test_chat_turn_tool_mode_uses_full_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_LANE", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_GENERIC_PROVIDER", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_PROVIDER", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_FALLBACK_PROVIDER", raising=False)
    monkeypatch.setattr(
        routing,
        "build_profile_for_provider",
        lambda provider, *, key_prefix, request=None: _profile(provider, key_prefix),
    )
    monkeypatch.setattr(routing, "is_profile_available", lambda _profile: True)

    resolved = routing.resolve_runtime_profiles(
        RuntimeRequest(
            prompt="read file",
            cwd=".",
            task_name="chat_turn",
            capability="tool_reasoning",
            allowed_tools=["Read"],
        )
    )

    assert [profile.provider for profile in resolved] == [
        "claude",
        "openai-codex",
        "gemini-cli",
        "openrouter",
        "openai-compatible",
        "kimi",
    ]


def test_generic_text_route_prefers_api_profiles_before_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_LANE", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_PROVIDER", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_GENERIC_PROVIDER", raising=False)
    monkeypatch.setattr(
        routing,
        "build_profile_for_provider",
        lambda provider, *, key_prefix, request=None: _profile(provider, key_prefix),
    )
    monkeypatch.setattr(routing, "is_profile_available", lambda _profile: True)

    resolved = routing.resolve_generic_runtime_profiles(
        RuntimeRequest(prompt="hi", cwd=".", task_name="memory_flush")
    )

    assert [profile.provider for profile in resolved] == [
        "openai-compatible",
        "openrouter",
        "openai-codex",
        "gemini-cli",
        "kimi",
    ]


def test_generic_tool_route_uses_only_tool_capable_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_PROVIDER", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_GENERIC_PROVIDER", raising=False)
    monkeypatch.setattr(
        routing,
        "build_profile_for_provider",
        lambda provider, *, key_prefix, request=None: _profile(provider, key_prefix),
    )
    monkeypatch.setattr(routing, "is_profile_available", lambda _profile: True)

    resolved = routing.resolve_generic_runtime_profiles(
        RuntimeRequest(
            prompt="read file",
            cwd=".",
            task_name="chat_turn",
            capability="tool_reasoning",
            allowed_tools=["Read"],
        )
    )

    assert [profile.provider for profile in resolved] == [
        "openai-codex",
        "gemini-cli",
    ]


def test_pinned_generic_provider_does_not_append_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_LANE", "generic_runtime")
    monkeypatch.setenv("SECOND_BRAIN_GENERIC_PROVIDER", "openai-codex")
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_PROVIDER", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_FALLBACK_PROVIDER", raising=False)
    monkeypatch.setattr(
        routing,
        "build_profile_for_provider",
        lambda provider, *, key_prefix, request=None: _profile(provider, key_prefix),
    )
    monkeypatch.setattr(routing, "is_profile_available", lambda _profile: True)

    resolved = routing.resolve_generic_runtime_profiles(
        RuntimeRequest(prompt="hi", cwd=".", task_name="chat_turn")
    )

    assert [profile.provider for profile in resolved] == ["openai-codex"]


def test_pinned_generic_provider_beats_route_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_LANE", "generic_runtime")
    monkeypatch.setenv("SECOND_BRAIN_GENERIC_PROVIDER", "openai-codex")
    monkeypatch.setenv("SECOND_BRAIN_ROUTE_TEXT", "gemini")
    monkeypatch.setattr(
        routing,
        "build_profile_for_provider",
        lambda provider, *, key_prefix, request=None: _profile(provider, key_prefix),
    )
    monkeypatch.setattr(routing, "is_profile_available", lambda _profile: True)

    resolved = routing.resolve_generic_runtime_profiles(
        RuntimeRequest(prompt="hi", cwd=".", task_name="chat_turn")
    )

    assert [profile.provider for profile in resolved] == ["openai-codex"]


def test_unavailable_pinned_generic_provider_does_not_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_LANE", "generic_runtime")
    monkeypatch.setenv("SECOND_BRAIN_GENERIC_PROVIDER", "openai-codex")
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_PROVIDER", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_FALLBACK_PROVIDER", raising=False)

    def fake_build(provider, *, key_prefix, request=None):
        if provider == "openai-codex":
            return None
        return _profile(provider, key_prefix)

    monkeypatch.setattr(routing, "build_profile_for_provider", fake_build)
    monkeypatch.setattr(routing, "is_profile_available", lambda _profile: True)

    resolved = routing.resolve_generic_runtime_profiles(
        RuntimeRequest(prompt="hi", cwd=".", task_name="chat_turn")
    )

    assert resolved == []


def test_generic_route_ignores_legacy_claude_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_PROVIDER", "claude")
    monkeypatch.delenv("SECOND_BRAIN_GENERIC_PROVIDER", raising=False)
    monkeypatch.setattr(
        routing,
        "build_profile_for_provider",
        lambda provider, *, key_prefix, request=None: _profile(provider, key_prefix),
    )
    monkeypatch.setattr(routing, "is_profile_available", lambda _profile: True)

    resolved = routing.resolve_generic_runtime_profiles(
        RuntimeRequest(prompt="hi", cwd=".", task_name="memory_flush")
    )

    assert [profile.provider for profile in resolved] == [
        "openai-compatible",
        "openrouter",
        "openai-codex",
        "gemini-cli",
        "kimi",
    ]


def test_openrouter_profile_is_distinct_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key")
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_MODEL", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_OPENROUTER_MODEL", raising=False)

    profile = profiles.build_profile_for_provider(
        "openrouter",
        key_prefix="fallback1",
        request=RuntimeRequest(prompt="hi", cwd=".", task_name="memory_flush"),
    )

    assert profile is not None
    assert profile.provider == "openrouter"
    assert profile.base_url == "https://openrouter.ai/api/v1"
    assert profile.model == "openrouter/auto"


def test_runtime_health_cooldown_and_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(health, "RUNTIME_HEALTH_FILE", tmp_path / "runtime-health.json")
    monkeypatch.setenv("SECOND_BRAIN_PROVIDER_COOLDOWN_SECONDS", "60")
    monkeypatch.setenv("SECOND_BRAIN_MODEL_COOLDOWN_SECONDS", "60")
    profile = RuntimeProfile(
        key="primary-gemini-cli",
        provider="gemini-cli",
        model="gemini-3-flash-preview",
    )

    assert health.is_profile_available(profile) is True

    health.mark_profile_retryable_failure(profile, "429")
    assert health.is_profile_available(profile) is False

    health.mark_profile_success(profile)
    assert health.is_profile_available(profile) is True


# === 2026-07-16 WinError 32 regression: health bookkeeping is fail-open ===


def test_mark_profile_success_swallows_state_write_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(health, "RUNTIME_HEALTH_FILE", tmp_path / "runtime-health.json")

    def _sharing_violation(state, state_file):
        raise OSError(
            32,
            "The process cannot access the file because it is being used "
            "by another process",
        )

    monkeypatch.setattr(health, "save_state", _sharing_violation)

    health.mark_profile_success(_profile("openai-codex"))


def test_mark_profile_failure_swallows_state_write_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    # mark_profile_retryable_failure is called from inside lane_router's
    # except handlers — if it raised, the whole router would crash mid-fallback.
    monkeypatch.setattr(health, "RUNTIME_HEALTH_FILE", tmp_path / "runtime-health.json")

    def _sharing_violation(state, state_file):
        raise OSError(32, "sharing violation")

    monkeypatch.setattr(health, "save_state", _sharing_violation)

    health.mark_profile_retryable_failure(_profile("openai-codex"), "429")
    health.mark_profile_unavailable(_profile("openai-codex"), "no key")


def test_is_profile_available_fails_open_on_read_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(health, "RUNTIME_HEALTH_FILE", tmp_path / "runtime-health.json")

    def _unreadable(state_file):
        raise OSError(32, "sharing violation")

    monkeypatch.setattr(health, "load_state", _unreadable)

    assert health.is_profile_available(_profile("openai-codex")) is True


_RACE_ITERATIONS = 15


def _health_race_worker(health_file, provider, iterations, start_event, errors):
    """Spawned worker for the cross-process race test.

    Runs in a fresh interpreter (multiprocessing spawn — monkeypatched module
    state does NOT propagate), so it repoints RUNTIME_HEALTH_FILE itself.
    Each iteration writes a unique model key: any lost update (one process's
    save clobbering another's) leaves that key permanently missing for the
    parent to detect.
    """
    try:
        from pathlib import Path as _Path

        import runtime.health as health_mod
        from runtime.profiles import RuntimeProfile as _RuntimeProfile

        health_mod.RUNTIME_HEALTH_FILE = _Path(health_file)
        start_event.wait(timeout=15)
        for index in range(iterations):
            profile = _RuntimeProfile(
                key=f"primary-{provider}",
                provider=provider,
                model=f"m{index}",
            )
            health_mod.mark_profile_retryable_failure(profile, "race probe")
            health_mod.mark_profile_success(profile)
    except Exception as exc:  # pragma: no cover - surfaced via parent assert
        errors.put(f"{provider}: {exc!r}")


def test_concurrent_marks_do_not_collide_or_lose_updates(tmp_path) -> None:
    """Cross-process WinError 32 regression (2026-07-16): concurrent
    load-modify-save on runtime-health.json must neither raise nor lose
    entries. Sequential calls cannot exercise the file_lock — only true
    multi-process interleaving can."""
    import json
    import multiprocessing

    ctx = multiprocessing.get_context("spawn")
    health_file = tmp_path / "runtime-health.json"
    start_event = ctx.Event()
    errors = ctx.Queue()
    providers = ("race-a", "race-b", "race-c")

    workers = [
        ctx.Process(
            target=_health_race_worker,
            args=(str(health_file), provider, _RACE_ITERATIONS, start_event, errors),
        )
        for provider in providers
    ]
    for worker in workers:
        worker.start()
    start_event.set()
    for worker in workers:
        worker.join(timeout=120)
        if worker.is_alive():  # pragma: no cover - hang guard
            worker.terminate()
            pytest.fail("race worker hung")

    worker_errors = []
    while not errors.empty():
        worker_errors.append(errors.get())
    assert worker_errors == []
    assert [worker.exitcode for worker in workers] == [0, 0, 0]

    state = json.loads(health_file.read_text(encoding="utf-8"))
    entities = state["entities"]
    for provider in providers:
        for index in range(_RACE_ITERATIONS):
            key = f"model:{provider}:m{index}"
            assert key in entities, f"lost update: {key} missing"
            assert "last_success_at" in entities[key]
