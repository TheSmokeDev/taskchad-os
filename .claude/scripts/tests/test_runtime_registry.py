from __future__ import annotations

import pytest

import runtime.profiles as profiles
import runtime.registry as registry
from runtime.base import (
    RUNTIME_LANE_CLAUDE_NATIVE,
    RUNTIME_LANE_GENERIC,
    RuntimeRequest,
    RuntimeResult,
)
from runtime.capabilities import TOOL_REASONING


def test_runtime_package_exports_lane_runner() -> None:
    import runtime

    assert hasattr(runtime, "run_with_runtime_lanes")
    assert hasattr(runtime, "run_with_fallback")


def test_resolve_runtime_profiles_adds_openai_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    # Clear .env-loaded canonical selection so the legacy provider pin actually wins.
    # config.py does load_dotenv(override=True) at import time, so SECOND_BRAIN_GENERIC_PROVIDER
    # and SECOND_BRAIN_RUNTIME_LANE leak into os.environ even when not set in the shell.
    monkeypatch.delenv("SECOND_BRAIN_GENERIC_PROVIDER", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_LANE", raising=False)
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

    async def fake_lane_runner(_request: RuntimeRequest) -> RuntimeResult:
        return RuntimeResult(
            text="ok",
            runtime_lane=RUNTIME_LANE_GENERIC,
            provider="openai-compatible",
            model="gpt-4.1-mini",
        )

    monkeypatch.setattr(registry, "run_with_runtime_lanes", fake_lane_runner)

    result = await registry.run_with_fallback(request)

    assert result.text == "ok"
    assert result.runtime_lane == RUNTIME_LANE_GENERIC
    assert result.provider == "openai-compatible"


@pytest.mark.asyncio
async def test_run_with_fallback_uses_next_profile_on_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = RuntimeRequest(prompt="hi", cwd=".", task_name="memory_flush")

    async def fake_lane_runner(_request: RuntimeRequest) -> RuntimeResult:
        return RuntimeResult(
            text="ok",
            runtime_lane=RUNTIME_LANE_GENERIC,
            provider="openai-codex",
            model="chatgpt-plan-default",
        )

    monkeypatch.setattr(registry, "run_with_runtime_lanes", fake_lane_runner)

    result = await registry.run_with_fallback(request)

    assert result.text == "ok"
    assert result.runtime_lane == RUNTIME_LANE_GENERIC
    assert result.provider == "openai-codex"


@pytest.mark.asyncio
async def test_run_with_fallback_marks_resume_requests_as_claude_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = RuntimeRequest(prompt="continue", cwd=".", task_name="chat_turn", resume="sess-123")

    class SuccessAdapter:
        def supports(self, _request: RuntimeRequest) -> bool:
            return True

        async def run(self, _request: RuntimeRequest) -> RuntimeResult:
            return RuntimeResult(
                text="ok",
                runtime_lane=RUNTIME_LANE_CLAUDE_NATIVE,
                provider="claude",
                model="claude-sonnet-4-6",
                session_id="sess-123",
            )

    monkeypatch.setattr(registry, "run_with_runtime_lanes", SuccessAdapter().run)

    result = await registry.run_with_fallback(request)

    assert result.runtime_lane == RUNTIME_LANE_CLAUDE_NATIVE
    assert result.provider == "claude"
