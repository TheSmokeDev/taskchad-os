from __future__ import annotations

import pytest

import runtime.profiles as profiles
import runtime.registry as registry
from runtime.base import RuntimeRequest, RuntimeResult
from runtime.capabilities import TOOL_REASONING
from runtime.errors import RuntimeConfigError, RuntimeRetryableError
from runtime.profiles import RuntimeProfile


def test_resolve_runtime_profiles_adds_openai_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(profiles, "OPENAI_API_KEY", "sk-test-key")
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_PROVIDER", "claude")
    monkeypatch.setenv("SECOND_BRAIN_ENABLE_OPENAI_FALLBACK", "true")
    monkeypatch.setenv("SECOND_BRAIN_FALLBACK_PROVIDER", "openai")

    request = RuntimeRequest(prompt="hi", cwd=".", task_name="memory_flush")
    resolved = profiles.resolve_runtime_profiles(request)

    assert [profile.provider for profile in resolved] == ["claude", "openai-compatible"]


def test_resolve_runtime_profiles_chains_all_providers_for_tool_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hermes-style: all subscription providers available for tool tasks."""
    monkeypatch.setattr(profiles, "OPENAI_API_KEY", "sk-test-key")

    request = RuntimeRequest(
        prompt="hi",
        cwd=".",
        task_name="memory_reflect",
        capability=TOOL_REASONING,
        allowed_tools=["Read"],
    )
    resolved = profiles.resolve_runtime_profiles(request)
    providers = [profile.provider for profile in resolved]

    # Multiple providers should be available — not just Claude
    assert len(providers) > 1, "Should have fallback providers for tool reasoning"
    # At least one subscription-backed CLI should be in the chain
    sub_providers = {"claude", "openai-codex", "gemini-cli"}
    assert sub_providers & set(providers), "At least one subscription provider should resolve"


@pytest.mark.asyncio
async def test_run_with_fallback_uses_next_profile_on_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = RuntimeRequest(prompt="hi", cwd=".", task_name="memory_flush")
    resolved = [
        RuntimeProfile(key="primary", provider="claude", model="claude-sonnet-4-6"),
        RuntimeProfile(
            key="fallback",
            provider="openai-compatible",
            model="gpt-4.1-mini",
            api_key="sk-test-key",
        ),
    ]

    class RetryAdapter:
        def supports(self, _request: RuntimeRequest) -> bool:
            return True

        async def run(self, _request: RuntimeRequest) -> RuntimeResult:
            raise RuntimeRetryableError("429")

    class SuccessAdapter:
        def supports(self, _request: RuntimeRequest) -> bool:
            return True

        async def run(self, _request: RuntimeRequest) -> RuntimeResult:
            return RuntimeResult(text="ok", provider="openai-compatible", model="gpt-4.1-mini")

    adapters = iter([RetryAdapter(), SuccessAdapter()])
    monkeypatch.setattr(registry, "resolve_runtime_profiles", lambda _request: resolved)
    monkeypatch.setattr(registry, "_adapter_for", lambda _profile: next(adapters))

    result = await registry.run_with_fallback(request)

    assert result.text == "ok"
    assert result.provider == "openai-compatible"


@pytest.mark.asyncio
async def test_run_with_fallback_uses_next_profile_on_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = RuntimeRequest(prompt="hi", cwd=".", task_name="memory_flush")
    resolved = [
        RuntimeProfile(key="primary", provider="gemini-cli", model="gemini-3-flash-preview"),
        RuntimeProfile(
            key="fallback",
            provider="openai-codex",
            model="chatgpt-plan-default",
            command="codex",
        ),
    ]

    class ConfigErrorAdapter:
        def supports(self, _request: RuntimeRequest) -> bool:
            return True

        async def run(self, _request: RuntimeRequest) -> RuntimeResult:
            raise RuntimeConfigError("403 forbidden")

    class SuccessAdapter:
        def supports(self, _request: RuntimeRequest) -> bool:
            return True

        async def run(self, _request: RuntimeRequest) -> RuntimeResult:
            return RuntimeResult(text="ok", provider="openai-codex", model="chatgpt-plan-default")

    adapters = iter([ConfigErrorAdapter(), SuccessAdapter()])
    monkeypatch.setattr(registry, "resolve_runtime_profiles", lambda _request: resolved)
    monkeypatch.setattr(registry, "_adapter_for", lambda _profile: next(adapters))
    monkeypatch.setattr(registry, "mark_profile_unavailable", lambda _profile, _error: None)

    result = await registry.run_with_fallback(request)

    assert result.text == "ok"
    assert result.provider == "openai-codex"
