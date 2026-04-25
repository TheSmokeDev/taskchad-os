from __future__ import annotations

import pytest

import runtime.lane_router as lane_router
from runtime.base import (
    RUNTIME_LANE_CLAUDE_NATIVE,
    RUNTIME_LANE_GENERIC,
    RuntimeRequest,
    RuntimeResult,
)
from runtime.profiles import RuntimeProfile


def test_resolve_runtime_lane_defaults_to_generic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_LANE", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_GENERIC_PROVIDER", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_PROVIDER", raising=False)

    lane = lane_router.resolve_runtime_lane(
        RuntimeRequest(prompt="hi", cwd=".", task_name="chat_turn")
    )
    assert lane == RUNTIME_LANE_GENERIC


def test_resolve_runtime_lane_uses_claude_for_resume() -> None:
    lane = lane_router.resolve_runtime_lane(
        RuntimeRequest(prompt="continue", cwd=".", task_name="chat_turn", resume="sess-1")
    )
    assert lane == RUNTIME_LANE_CLAUDE_NATIVE


def test_resolve_runtime_lane_honors_explicit_override() -> None:
    lane = lane_router.resolve_runtime_lane(
        RuntimeRequest(
            prompt="hi",
            cwd=".",
            task_name="chat_turn",
            runtime_lane=RUNTIME_LANE_CLAUDE_NATIVE,
        )
    )
    assert lane == RUNTIME_LANE_CLAUDE_NATIVE


def test_resolve_runtime_lane_honors_env_lane_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_LANE", RUNTIME_LANE_CLAUDE_NATIVE)

    lane = lane_router.resolve_runtime_lane(
        RuntimeRequest(prompt="hi", cwd=".", task_name="chat_turn")
    )

    assert lane == RUNTIME_LANE_CLAUDE_NATIVE


def test_resolve_runtime_lane_maps_legacy_claude_pin_to_native_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `.env` has SECOND_BRAIN_GENERIC_PROVIDER=openai-codex which short-circuits
    # selection before legacy_provider="claude" can map to claude_native. Must clear.
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_LANE", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_GENERIC_PROVIDER", raising=False)
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_PROVIDER", "claude")

    lane = lane_router.resolve_runtime_lane(
        RuntimeRequest(prompt="hi", cwd=".", task_name="chat_turn")
    )

    assert lane == RUNTIME_LANE_CLAUDE_NATIVE


def test_explicit_runtime_lane_beats_legacy_provider_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_LANE", raising=False)
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_PROVIDER", "claude")

    lane = lane_router.resolve_runtime_lane(
        RuntimeRequest(
            prompt="hi",
            cwd=".",
            task_name="chat_turn",
            runtime_lane=RUNTIME_LANE_GENERIC,
        )
    )

    assert lane == RUNTIME_LANE_GENERIC


@pytest.mark.asyncio
async def test_run_with_runtime_lanes_sets_lane_on_result(monkeypatch: pytest.MonkeyPatch) -> None:
    request = RuntimeRequest(prompt="continue", cwd=".", task_name="chat_turn", resume="sess-1")

    monkeypatch.setattr(
        lane_router,
        "_resolve_lane_profiles",
        lambda _request: [
            RuntimeProfile(
                key="primary-claude",
                provider="claude",
                model="claude-sonnet-4-6",
            )
        ],
    )

    class SuccessAdapter:
        def supports(self, _request: RuntimeRequest) -> bool:
            return True

        async def run(self, _request: RuntimeRequest) -> RuntimeResult:
            return RuntimeResult(
                text="ok",
                runtime_lane=RUNTIME_LANE_CLAUDE_NATIVE,
                provider="claude",
                model="claude-sonnet-4-6",
            )

    monkeypatch.setattr(lane_router, "_adapter_for", lambda _profile: SuccessAdapter())

    result = await lane_router.run_with_runtime_lanes(request)

    assert result.runtime_lane == RUNTIME_LANE_CLAUDE_NATIVE
    assert result.provider == "claude"
