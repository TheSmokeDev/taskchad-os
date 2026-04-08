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
