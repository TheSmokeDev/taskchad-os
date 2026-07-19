from __future__ import annotations

import asyncio

import pytest

import runtime.lane_router as lane_router
from runtime.base import (
    RUNTIME_LANE_CLAUDE_NATIVE,
    RUNTIME_LANE_GENERIC,
    RuntimeRequest,
    RuntimeResult,
)
from runtime.capabilities import TOOL_REASONING
from runtime.errors import RuntimeExecutionError
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


def test_resolve_runtime_lane_uses_claude_for_auto_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_LANE", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_GENERIC_PROVIDER", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_PROVIDER", raising=False)

    lane = lane_router.resolve_runtime_lane(
        RuntimeRequest(prompt="continue", cwd=".", task_name="chat_turn", resume="sess-1")
    )
    assert lane == RUNTIME_LANE_CLAUDE_NATIVE


def test_resolve_runtime_lane_honors_generic_selection_with_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_LANE", RUNTIME_LANE_GENERIC)
    monkeypatch.setenv("SECOND_BRAIN_GENERIC_PROVIDER", "openai-codex")
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_PROVIDER", raising=False)

    lane = lane_router.resolve_runtime_lane(
        RuntimeRequest(prompt="continue", cwd=".", task_name="chat_turn", resume="sess-1")
    )
    assert lane == RUNTIME_LANE_GENERIC


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
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_LANE", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_GENERIC_PROVIDER", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_PROVIDER", raising=False)
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


@pytest.mark.asyncio
async def test_run_with_runtime_lanes_drops_resume_for_generic_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_LANE", RUNTIME_LANE_GENERIC)
    monkeypatch.setenv("SECOND_BRAIN_GENERIC_PROVIDER", "openai-codex")
    request = RuntimeRequest(prompt="continue", cwd=".", task_name="chat_turn", resume="sess-1")
    captured: dict[str, str | None] = {}

    monkeypatch.setattr(
        lane_router,
        "_resolve_lane_profiles",
        lambda _request: [
            RuntimeProfile(
                key="primary-openai-codex",
                provider="openai-codex",
                model="gpt-5.5",
            )
        ],
    )

    class SuccessAdapter:
        def supports(self, runtime_request: RuntimeRequest) -> bool:
            captured["supports_resume"] = runtime_request.resume
            return runtime_request.resume is None

        async def run(self, runtime_request: RuntimeRequest) -> RuntimeResult:
            captured["run_resume"] = runtime_request.resume
            return RuntimeResult(
                text="ok",
                runtime_lane=RUNTIME_LANE_GENERIC,
                provider="openai-codex",
                model="gpt-5.5",
            )

    monkeypatch.setattr(lane_router, "_adapter_for", lambda _profile: SuccessAdapter())

    result = await lane_router.run_with_runtime_lanes(request)

    assert result.runtime_lane == RUNTIME_LANE_GENERIC
    assert result.provider == "openai-codex"
    assert captured == {"supports_resume": None, "run_resume": None}


@pytest.mark.asyncio
async def test_success_result_survives_health_bookkeeping_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-07-16 regression: a health-write failure after a successful run
    (WinError 32 runtime-health.json collision between concurrent scheduled
    jobs) must not discard the result, mark the provider failed, or escape
    to the caller. Patches lane_router.mark_profile_success — the name the
    router actually calls — so the invariant is proven at the boundary, not
    inside health.py."""
    request = RuntimeRequest(
        prompt="hi",
        cwd=".",
        task_name="social_draft_generator",
        runtime_lane=RUNTIME_LANE_GENERIC,
    )

    monkeypatch.setattr(
        lane_router,
        "_resolve_lane_profiles",
        lambda _request: [
            RuntimeProfile(
                key="primary-openai-codex",
                provider="openai-codex",
                model="gpt-5.5",
            )
        ],
    )

    class SuccessAdapter:
        def supports(self, _request: RuntimeRequest) -> bool:
            return True

        async def run(self, _request: RuntimeRequest) -> RuntimeResult:
            return RuntimeResult(
                text="ok",
                runtime_lane=RUNTIME_LANE_GENERIC,
                provider="openai-codex",
                model="gpt-5.5",
            )

    monkeypatch.setattr(lane_router, "_adapter_for", lambda _profile: SuccessAdapter())

    def _boom(_profile: RuntimeProfile) -> None:
        raise OSError(32, "simulated runtime-health.json collision")

    failure_marks: list[str] = []
    monkeypatch.setattr(lane_router, "mark_profile_success", _boom)
    monkeypatch.setattr(
        lane_router,
        "mark_profile_retryable_failure",
        lambda _profile, error: failure_marks.append(error),
    )
    monkeypatch.setattr(
        lane_router,
        "mark_profile_unavailable",
        lambda _profile, error: failure_marks.append(error),
    )

    result = await lane_router.run_with_runtime_lanes(request)

    assert result.text == "ok"
    assert result.runtime_lane == RUNTIME_LANE_GENERIC
    assert failure_marks == []


@pytest.mark.asyncio
async def test_run_with_runtime_lanes_drops_resume_for_explicit_generic_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = RuntimeRequest(
        prompt="continue",
        cwd=".",
        task_name="chat_turn",
        resume="sess-1",
        runtime_lane=RUNTIME_LANE_GENERIC,
    )
    captured: dict[str, str | None] = {}

    monkeypatch.setattr(
        lane_router,
        "_resolve_lane_profiles",
        lambda _request: [
            RuntimeProfile(
                key="primary-openai-codex",
                provider="openai-codex",
                model="gpt-5.5",
            )
        ],
    )

    class SuccessAdapter:
        def supports(self, runtime_request: RuntimeRequest) -> bool:
            captured["supports_resume"] = runtime_request.resume
            return runtime_request.resume is None

        async def run(self, runtime_request: RuntimeRequest) -> RuntimeResult:
            captured["run_resume"] = runtime_request.resume
            return RuntimeResult(
                text="ok",
                runtime_lane=RUNTIME_LANE_GENERIC,
                provider="openai-codex",
                model="gpt-5.5",
            )

    monkeypatch.setattr(lane_router, "_adapter_for", lambda _profile: SuccessAdapter())

    result = await lane_router.run_with_runtime_lanes(request)

    assert result.runtime_lane == RUNTIME_LANE_GENERIC
    assert result.provider == "openai-codex"
    assert captured == {"supports_resume": None, "run_resume": None}


# --- Issue #133: per-adapter fallback timeout -----------------------------


def test_adapter_timeout_seconds_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """The call-time resolver (Rule 1): capability-keyed knob selection,
    default fallback, garbage/empty tolerance, and <=0 disable."""
    text_req = RuntimeRequest(prompt="hi", cwd=".", task_name="t")  # TEXT default
    tool_req = RuntimeRequest(prompt="hi", cwd=".", task_name="t", capability=TOOL_REASONING)

    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_TIMEOUT_TEXT_SECONDS", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_TIMEOUT_TOOL_SECONDS", raising=False)
    assert lane_router._adapter_timeout_seconds(text_req) == lane_router._DEFAULT_TIMEOUT_TEXT_S
    assert lane_router._adapter_timeout_seconds(tool_req) == lane_router._DEFAULT_TIMEOUT_TOOL_S

    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_TIMEOUT_TEXT_SECONDS", "12.5")
    assert lane_router._adapter_timeout_seconds(text_req) == 12.5
    # The TEXT knob must not leak into the TOOL capability.
    assert lane_router._adapter_timeout_seconds(tool_req) == lane_router._DEFAULT_TIMEOUT_TOOL_S

    for disabling in ("0", "-5", "0.0"):
        monkeypatch.setenv("SECOND_BRAIN_RUNTIME_TIMEOUT_TEXT_SECONDS", disabling)
        assert lane_router._adapter_timeout_seconds(text_req) is None

    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_TIMEOUT_TEXT_SECONDS", "not-a-number")
    assert lane_router._adapter_timeout_seconds(text_req) == lane_router._DEFAULT_TIMEOUT_TEXT_S

    monkeypatch.delenv("SECOND_BRAIN_RUNTIME_TIMEOUT_TEXT_SECONDS", raising=False)
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_TIMEOUT_TOOL_SECONDS", "45")
    assert lane_router._adapter_timeout_seconds(tool_req) == 45.0
    # The TOOL knob must not leak into the TEXT capability.
    assert lane_router._adapter_timeout_seconds(text_req) != 45.0

    for disabling in ("0", "-5", "0.0"):
        monkeypatch.setenv("SECOND_BRAIN_RUNTIME_TIMEOUT_TOOL_SECONDS", disabling)
        assert lane_router._adapter_timeout_seconds(tool_req) is None

    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_TIMEOUT_TOOL_SECONDS", "not-a-number")
    assert lane_router._adapter_timeout_seconds(tool_req) == lane_router._DEFAULT_TIMEOUT_TOOL_S

    for non_finite in ("nan", "inf", "-inf"):
        monkeypatch.setenv("SECOND_BRAIN_RUNTIME_TIMEOUT_TEXT_SECONDS", non_finite)
        assert lane_router._adapter_timeout_seconds(text_req) == lane_router._DEFAULT_TIMEOUT_TEXT_S


@pytest.mark.asyncio
async def test_hung_adapter_times_out_and_advances_to_next_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wedged first profile must time out, be marked retryable, and the loop
    must fall through to the healthy second profile — no hang."""
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_TIMEOUT_TEXT_SECONDS", "0.05")
    request = RuntimeRequest(
        prompt="hi",
        cwd=".",
        task_name="memory_flush",
        runtime_lane=RUNTIME_LANE_GENERIC,
    )

    monkeypatch.setattr(
        lane_router,
        "_resolve_lane_profiles",
        lambda _request: [
            RuntimeProfile(key="hung-openai-codex", provider="openai-codex", model="gpt-5.5"),
            RuntimeProfile(
                key="healthy-gemini-cli", provider="gemini-cli", model="gemini-2.5-flash"
            ),
        ],
    )

    class HangAdapter:
        def supports(self, _request: RuntimeRequest) -> bool:
            return True

        async def run(self, _request: RuntimeRequest) -> RuntimeResult:
            await asyncio.sleep(3600)
            raise AssertionError("unreachable — should have been cancelled")

    class HealthyAdapter:
        def supports(self, _request: RuntimeRequest) -> bool:
            return True

        async def run(self, _request: RuntimeRequest) -> RuntimeResult:
            return RuntimeResult(
                text="ok",
                runtime_lane=RUNTIME_LANE_GENERIC,
                provider="gemini-cli",
                model="gemini-2.5-flash",
            )

    adapters = {"openai-codex": HangAdapter(), "gemini-cli": HealthyAdapter()}
    monkeypatch.setattr(lane_router, "_adapter_for", lambda profile: adapters[profile.provider])

    failures: list[tuple[str, str]] = []
    monkeypatch.setattr(
        lane_router,
        "mark_profile_retryable_failure",
        lambda profile, error: failures.append((profile.key, error)),
    )
    monkeypatch.setattr(lane_router, "mark_profile_success", lambda _profile: None)

    result = await lane_router.run_with_runtime_lanes(request)

    assert result.text == "ok"
    assert result.provider == "gemini-cli"
    assert failures and failures[0][0] == "hung-openai-codex"
    assert "timed out after" in failures[0][1]


@pytest.mark.asyncio
async def test_hung_adapter_fails_cleanly_when_no_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single wedged profile must raise RuntimeExecutionError promptly with a
    "timed out after" message — the loop must not hang forever."""
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_TIMEOUT_TEXT_SECONDS", "0.05")
    request = RuntimeRequest(
        prompt="hi",
        cwd=".",
        task_name="memory_flush",
        runtime_lane=RUNTIME_LANE_GENERIC,
    )

    monkeypatch.setattr(
        lane_router,
        "_resolve_lane_profiles",
        lambda _request: [
            RuntimeProfile(key="hung-openai-codex", provider="openai-codex", model="gpt-5.5")
        ],
    )

    class HangAdapter:
        def supports(self, _request: RuntimeRequest) -> bool:
            return True

        async def run(self, _request: RuntimeRequest) -> RuntimeResult:
            await asyncio.sleep(3600)
            raise AssertionError("unreachable — should have been cancelled")

    monkeypatch.setattr(lane_router, "_adapter_for", lambda _profile: HangAdapter())
    monkeypatch.setattr(lane_router, "mark_profile_retryable_failure", lambda *_a, **_k: None)

    with pytest.raises(RuntimeExecutionError) as excinfo:
        await lane_router.run_with_runtime_lanes(request)
    assert "timed out after" in str(excinfo.value)


@pytest.mark.asyncio
async def test_hung_adapter_receives_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wait_for must CANCEL the adapter coroutine (so the CLI reap path is
    reachable), not merely abandon it. The adapter observes CancelledError."""
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_TIMEOUT_TEXT_SECONDS", "0.05")
    request = RuntimeRequest(
        prompt="hi",
        cwd=".",
        task_name="memory_flush",
        runtime_lane=RUNTIME_LANE_GENERIC,
    )

    monkeypatch.setattr(
        lane_router,
        "_resolve_lane_profiles",
        lambda _request: [
            RuntimeProfile(key="hung-openai-codex", provider="openai-codex", model="gpt-5.5")
        ],
    )

    observed = {"cancelled": False}

    class HangAdapter:
        def supports(self, _request: RuntimeRequest) -> bool:
            return True

        async def run(self, _request: RuntimeRequest) -> RuntimeResult:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                observed["cancelled"] = True
                raise
            raise AssertionError("unreachable")

    monkeypatch.setattr(lane_router, "_adapter_for", lambda _profile: HangAdapter())
    monkeypatch.setattr(lane_router, "mark_profile_retryable_failure", lambda *_a, **_k: None)

    with pytest.raises(RuntimeExecutionError):
        await lane_router.run_with_runtime_lanes(request)
    assert observed["cancelled"] is True


@pytest.mark.asyncio
async def test_adapter_timeout_disabled_with_nonpositive_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The escape hatch: <=0 → wait_for(timeout=None) → no deadline. A slow
    adapter completes instead of being killed."""
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_TIMEOUT_TEXT_SECONDS", "0")
    request = RuntimeRequest(
        prompt="hi",
        cwd=".",
        task_name="memory_flush",
        runtime_lane=RUNTIME_LANE_GENERIC,
    )

    monkeypatch.setattr(
        lane_router,
        "_resolve_lane_profiles",
        lambda _request: [
            RuntimeProfile(key="slow-gemini-cli", provider="gemini-cli", model="gemini-2.5-flash")
        ],
    )

    class SlowAdapter:
        def supports(self, _request: RuntimeRequest) -> bool:
            return True

        async def run(self, _request: RuntimeRequest) -> RuntimeResult:
            await asyncio.sleep(0.1)
            return RuntimeResult(
                text="slow-ok",
                runtime_lane=RUNTIME_LANE_GENERIC,
                provider="gemini-cli",
                model="gemini-2.5-flash",
            )

    monkeypatch.setattr(lane_router, "_adapter_for", lambda _profile: SlowAdapter())
    monkeypatch.setattr(lane_router, "mark_profile_success", lambda _profile: None)

    result = await lane_router.run_with_runtime_lanes(request)
    assert result.text == "slow-ok"


@pytest.mark.asyncio
async def test_adapter_timeout_disabled_actually_removes_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """<=0 must produce a REAL None deadline, not a silent fallback to the
    module default. Shrinks the default so a fallback bug (e.g. a future
    `timeout_s or _DEFAULT_TIMEOUT_TEXT_S`) is distinguishable from correct
    disable behavior within a fast test — 0.1s alone can't tell "no deadline"
    apart from "a 300s deadline that happens to be bigger than 0.1s"."""
    monkeypatch.setattr(lane_router, "_DEFAULT_TIMEOUT_TEXT_S", 0.05)
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_TIMEOUT_TEXT_SECONDS", "0")
    request = RuntimeRequest(
        prompt="hi",
        cwd=".",
        task_name="memory_flush",
        runtime_lane=RUNTIME_LANE_GENERIC,
    )

    monkeypatch.setattr(
        lane_router,
        "_resolve_lane_profiles",
        lambda _request: [
            RuntimeProfile(key="slow-gemini-cli", provider="gemini-cli", model="gemini-2.5-flash")
        ],
    )

    class SlowAdapter:
        def supports(self, _request: RuntimeRequest) -> bool:
            return True

        async def run(self, _request: RuntimeRequest) -> RuntimeResult:
            await asyncio.sleep(0.2)  # longer than the shrunk 0.05s default
            return RuntimeResult(
                text="slow-ok",
                runtime_lane=RUNTIME_LANE_GENERIC,
                provider="gemini-cli",
                model="gemini-2.5-flash",
            )

    monkeypatch.setattr(lane_router, "_adapter_for", lambda _profile: SlowAdapter())
    monkeypatch.setattr(lane_router, "mark_profile_success", lambda _profile: None)

    result = await lane_router.run_with_runtime_lanes(request)
    assert result.text == "slow-ok"


@pytest.mark.asyncio
async def test_tool_capability_uses_tool_timeout_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TOOL_REASONING request keys the TOOL knob, not the TEXT knob. Both are
    set to distinct small values so a wrong-knob impl fails fast with a
    distinguishable message rather than hanging on the default."""
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_TIMEOUT_TOOL_SECONDS", "0.05")
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_TIMEOUT_TEXT_SECONDS", "0.5")
    request = RuntimeRequest(
        prompt="hi",
        cwd=".",
        task_name="heartbeat",
        capability=TOOL_REASONING,
        runtime_lane=RUNTIME_LANE_GENERIC,
    )

    monkeypatch.setattr(
        lane_router,
        "_resolve_lane_profiles",
        lambda _request: [
            RuntimeProfile(key="hung-openai-codex", provider="openai-codex", model="gpt-5.5")
        ],
    )

    class HangAdapter:
        def supports(self, _request: RuntimeRequest) -> bool:
            return True

        async def run(self, _request: RuntimeRequest) -> RuntimeResult:
            await asyncio.sleep(3600)
            raise AssertionError("unreachable — should have been cancelled")

    monkeypatch.setattr(lane_router, "_adapter_for", lambda _profile: HangAdapter())
    failures: list[str] = []
    monkeypatch.setattr(
        lane_router,
        "mark_profile_retryable_failure",
        lambda _profile, error: failures.append(error),
    )

    with pytest.raises(RuntimeExecutionError):
        await lane_router.run_with_runtime_lanes(request)
    assert failures and "timed out after 0.05s" in failures[0]
