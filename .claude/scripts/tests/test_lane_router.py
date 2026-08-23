from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import runtime.lane_router as lane_router
from runtime.base import (
    RUNTIME_LANE_CLAUDE_NATIVE,
    RUNTIME_LANE_GENERIC,
    RuntimeRequest,
    RuntimeResult,
)
from runtime.capabilities import TOOL_REASONING
from runtime.errors import RuntimeCallerToolTransportError, RuntimeExecutionError
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


@pytest.mark.asyncio
async def test_model_only_routing_skips_adapter_without_literal_guarantee(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = RuntimeRequest(
        prompt="untrusted evidence",
        cwd=".",
        task_name="curriculum_study",
        runtime_lane=RUNTIME_LANE_GENERIC,
        allowed_tools=[],
        disallowed_tools=["*"],
        model_only=True,
    )
    profiles = [
        RuntimeProfile(key="codex", provider="openai-codex", model="gpt"),
        RuntimeProfile(key="gemini", provider="gemini-cli", model="gemini"),
    ]
    monkeypatch.setattr(lane_router, "_resolve_lane_profiles", lambda _request: profiles)

    class UnsafeAdapter:
        def supports_model_only(self) -> bool:
            return False

        def supports(self, _request: RuntimeRequest) -> bool:
            return True

        async def run(self, _request: RuntimeRequest) -> RuntimeResult:
            raise AssertionError("unsafe adapter must be skipped")

    class SafeAdapter:
        def supports_model_only(self) -> bool:
            return True

        def supports(self, _request: RuntimeRequest) -> bool:
            return True

        async def run(self, _request: RuntimeRequest) -> RuntimeResult:
            return RuntimeResult(
                text="safe",
                runtime_lane=RUNTIME_LANE_GENERIC,
                provider="gemini-cli",
                model="gemini",
            )

    adapters = {"openai-codex": UnsafeAdapter(), "gemini-cli": SafeAdapter()}
    monkeypatch.setattr(lane_router, "_adapter_for", lambda profile: adapters[profile.provider])
    monkeypatch.setattr(lane_router, "mark_profile_success", lambda _profile: None)

    result = await lane_router.run_with_runtime_lanes(request)
    assert result.text == "safe"
    assert result.provider == "gemini-cli"


def test_model_only_contract_rejects_tool_authority() -> None:
    with pytest.raises(ValueError, match="zero-tool contract"):
        lane_router._base.assert_model_only_contract(
            RuntimeRequest(
                prompt="bad",
                cwd=".",
                task_name="bad",
                allowed_tools=["Read"],
                disallowed_tools=["*"],
                model_only=True,
            )
        )

    for forbidden in (
        {"hooks": {"PreToolUse": []}},
        {"setting_sources": ["project"]},
    ):
        with pytest.raises(ValueError, match="zero-tool contract"):
            lane_router._base.assert_model_only_contract(
                RuntimeRequest(
                    prompt="bad",
                    cwd=".",
                    task_name="bad",
                    disallowed_tools=["*"],
                    model_only=True,
                    **forbidden,
                )
            )


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


# ---------------------------------------------------------------------------
# #529 — universal caller-tool transport through the REAL route resolver
# ---------------------------------------------------------------------------
#
# The pre-existing end-to-end tool-turn tests monkeypatch
# `_resolve_lane_profiles`, which is precisely the component #529 fixes — they
# would stay green with the starvation fully intact. These tests patch only the
# ADAPTER and HEALTH seams and let the real
# `_resolve_lane_profiles -> resolve_generic_runtime_profiles ->
# _generic_provider_order_for_request` chain produce the candidate list.

_GET_WEATHER_PARAMS = {
    "type": "object",
    "properties": {"city": {"type": "string"}},
    "required": ["city"],
}


@pytest.fixture
def registered_get_weather():
    """Register the fixture tool so the provenance gate admits its schema.

    `run_with_runtime_lanes` refuses hand-assembled `tool_defs`, so a routing
    test MUST go through the registry — the same path a real equipped turn
    takes.
    """
    from runtime import tool_registry

    entry = tool_registry.register_tool(
        "get_weather",
        "Get the current temperature for a city.",
        toolset="test_ts_529",
        parameters=_GET_WEATHER_PARAMS,
    )
    try:
        yield entry.schema
    finally:
        tool_registry.unregister_tool("get_weather")


class _ProviderRecordingAdapter:
    """Records whether it was CONTACTED, and consumes what it was handed.

    A carrying fake that ignored `tool_defs`/`tool_dispatch` would model the
    exact polite-drop bug under test, so the carrying branch asserts it
    received the definitions and actually invokes the dispatcher.
    """

    def __init__(self, provider: str, *, carries: bool, contacted: list[str]):
        self.provider = provider
        self._carries = carries
        self._contacted = contacted
        self.received_tool_defs: list[dict] | None = None
        self.dispatched: list[str] = []

    def supports_caller_tool_defs(self) -> bool:
        return self._carries

    def supports(self, _request: RuntimeRequest) -> bool:
        return True

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        self._contacted.append(self.provider)
        if self._carries:
            assert request.tool_defs, "carrying adapter received no tool_defs"
            assert request.tool_dispatch is not None, (
                "carrying adapter received no dispatcher - it could not execute "
                "a tool call even if the model emitted one"
            )
            self.received_tool_defs = list(request.tool_defs)
            name = request.tool_defs[0]["function"]["name"]
            self.dispatched.append(request.tool_dispatch(name, {"city": "Reykjavik"}))
        return RuntimeResult(
            text="ok",
            runtime_lane=RUNTIME_LANE_GENERIC,
            provider=self.provider,
            model="fake-1",
        )


def _install_real_route_seams(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pinned_provider: str,
    carrying_providers: set[str],
    contacted: list[str],
) -> dict[str, _ProviderRecordingAdapter]:
    """Patch ONLY the profile/adapter seams; routing stays real."""
    import runtime.routing as routing

    # Synthesize a profile for every provider the ROUTE names, so the resolved
    # candidate list is exactly the route and nothing else. Depending on this
    # box's real credentials instead would make the assertions machine-specific
    # (a missing GEMINI auth silently deletes the pinned candidate, and the
    # test would then pass for the wrong reason). No provider is contacted:
    # `_adapter_for` below is a fake.
    monkeypatch.setattr(
        routing,
        "build_profile_for_provider",
        lambda provider, *, key_prefix, request: RuntimeProfile(
            key=f"{key_prefix}-{provider}", provider=provider, model="fake-1"
        ),
    )
    monkeypatch.setattr(
        routing,
        "resolve_runtime_selection",
        lambda: SimpleNamespace(
            lane=RUNTIME_LANE_GENERIC, generic_provider=pinned_provider
        ),
    )
    # Real health state is machine- and time-dependent; a cooling-down provider
    # would silently drop a candidate and make this test lie about routing.
    monkeypatch.setattr(routing, "is_profile_available", lambda _profile: True)
    # Never write real runtime health state from a routing test.
    monkeypatch.setattr(lane_router, "mark_profile_success", lambda _profile: None)
    monkeypatch.setattr(
        lane_router, "mark_profile_retryable_failure", lambda _profile, _reason: None
    )

    adapters: dict[str, _ProviderRecordingAdapter] = {}

    def _adapter_for(profile):
        if profile.provider not in adapters:
            adapters[profile.provider] = _ProviderRecordingAdapter(
                profile.provider,
                carries=profile.provider in carrying_providers,
                contacted=contacted,
            )
        return adapters[profile.provider]

    monkeypatch.setattr(lane_router, "_adapter_for", _adapter_for)
    return adapters


def _tool_request(schema: dict, **kw) -> RuntimeRequest:
    base = {
        "prompt": "what is the temperature in Reykjavik?",
        "cwd": ".",
        "task_name": "persona_turn",
        "capability": TOOL_REASONING,
        "runtime_lane": RUNTIME_LANE_GENERIC,
        "tool_defs": [schema],
        "tool_dispatch": lambda name, args: f"{name}:18C",
    }
    base.update(kw)
    return RuntimeRequest(**base)


@pytest.mark.asyncio
async def test_noncarrying_pin_reaches_a_carrying_fallback_with_zero_contact(
    monkeypatch: pytest.MonkeyPatch, registered_get_weather
) -> None:
    """AC-529-01/02/03 end to end, through the real route resolver.

    The pinned provider cannot carry schemas. It must receive ZERO `run()`
    calls - not a request that it politely drops - and the first carrying
    candidate behind it must execute the EXACT registered schema and reach the
    dispatcher.
    """
    contacted: list[str] = []
    adapters = _install_real_route_seams(
        monkeypatch,
        pinned_provider="gemini-cli",
        carrying_providers={"kimi"},
        contacted=contacted,
    )

    request = _tool_request(registered_get_weather)
    resolved = [p.provider for p in lane_router._resolve_lane_profiles(request)]
    assert resolved[0] == "gemini-cli", (
        f"the operator pin is no longer first in the real route: {resolved}"
    )
    assert "kimi" in resolved, (
        f"the real resolver starved the equipped turn of a carrier: {resolved}"
    )

    result = await lane_router.run_with_runtime_lanes(request)

    assert result.provider == "kimi"
    assert contacted == ["kimi"], (
        f"a noncarrying provider was contacted for an equipped turn: {contacted}"
    )
    assert adapters["gemini-cli"].received_tool_defs is None
    carrier = adapters["kimi"]
    assert carrier.received_tool_defs == [registered_get_weather], (
        "the carrying fallback did not receive the exact registered schema"
    )
    assert carrier.dispatched == ["get_weather:18C"], (
        "routing succeeded while the capability it exists to deliver did not"
    )


@pytest.mark.asyncio
async def test_no_carrying_candidate_fails_typed_with_no_text_only_success(
    monkeypatch: pytest.MonkeyPatch, registered_get_weather
) -> None:
    """AC-529-04. Every candidate is noncarrying, so the turn must FAIL.

    A text answer from a provider that silently dropped the schemas is the
    failure mode this epic exists to kill; it is strictly worse than a loud
    typed error because it is indistinguishable from the persona refusing.
    """
    contacted: list[str] = []
    _install_real_route_seams(
        monkeypatch,
        pinned_provider="gemini-cli",
        carrying_providers=set(),
        contacted=contacted,
    )

    with pytest.raises(RuntimeCallerToolTransportError) as exc_info:
        await lane_router.run_with_runtime_lanes(_tool_request(registered_get_weather))

    assert contacted == [], f"a provider was contacted with no carrier: {contacted}"
    assert "caller-supplied tool definitions" in str(exc_info.value)


@pytest.mark.asyncio
async def test_fallback_disabled_pin_fails_rather_than_widening(
    monkeypatch: pytest.MonkeyPatch, registered_get_weather
) -> None:
    """A caller who said "this provider or nothing" is not silently widened.

    `kimi` carries here, so the ONLY thing keeping it out of the route is the
    request's own `allow_fallback=False` contract - which makes this a real
    test of that boundary rather than of an empty candidate pool.
    """
    contacted: list[str] = []
    _install_real_route_seams(
        monkeypatch,
        pinned_provider="gemini-cli",
        carrying_providers={"kimi"},
        contacted=contacted,
    )

    request = _tool_request(registered_get_weather, allow_fallback=False)
    resolved = [p.provider for p in lane_router._resolve_lane_profiles(request)]
    assert resolved == ["gemini-cli"], (
        f"fallback-disabled route widened: {resolved}"
    )

    with pytest.raises(RuntimeCallerToolTransportError):
        await lane_router.run_with_runtime_lanes(request)
    assert contacted == []


@pytest.mark.asyncio
async def test_carrying_pin_wins_immediately_and_no_fallback_is_contacted(
    monkeypatch: pytest.MonkeyPatch, registered_get_weather
) -> None:
    """The rollback criterion: a carrying preference is still selected first."""
    contacted: list[str] = []
    _install_real_route_seams(
        monkeypatch,
        pinned_provider="gemini-cli",
        carrying_providers={"gemini-cli", "kimi"},
        contacted=contacted,
    )

    result = await lane_router.run_with_runtime_lanes(
        _tool_request(registered_get_weather)
    )

    assert result.provider == "gemini-cli"
    assert contacted == ["gemini-cli"], (
        f"a carrying pin did not win immediately: {contacted}"
    )


@pytest.mark.asyncio
async def test_carrying_pin_does_not_probe_blocking_unused_fallbacks(
    monkeypatch: pytest.MonkeyPatch, registered_get_weather
) -> None:
    """A preferred carrier executes before unused fallback auth discovery.

    Subprocess-backed profile construction may synchronously run an auth probe.
    Eagerly building the whole fallback route therefore stalls the event loop
    even when the first carrying provider will win.  The synthetic Codex build
    below is deliberately blocking: without lazy execution discovery this test
    records the unused probe and delays the concurrent ticker by 250 ms.
    """
    import runtime.routing as routing

    contacted: list[str] = []
    _install_real_route_seams(
        monkeypatch,
        pinned_provider="kimi",
        carrying_providers={"kimi"},
        contacted=contacted,
    )

    unused_fallback_probes: list[str] = []

    def _build_profile(provider, *, key_prefix, request):
        del request
        if provider == "openai-codex":
            unused_fallback_probes.append(provider)
            time.sleep(0.25)
        return RuntimeProfile(
            key=f"{key_prefix}-{provider}", provider=provider, model="fake-1"
        )

    monkeypatch.setattr(routing, "build_profile_for_provider", _build_profile)

    started = time.perf_counter()
    tick_delays: list[float] = []

    async def _ticker() -> None:
        await asyncio.sleep(0)
        tick_delays.append(time.perf_counter() - started)

    ticker = asyncio.create_task(_ticker())
    result = await lane_router.run_with_runtime_lanes(
        _tool_request(registered_get_weather)
    )
    await ticker

    assert result.provider == "kimi"
    assert contacted == ["kimi"]
    assert unused_fallback_probes == [], (
        "a carrying preference still triggered blocking auth discovery for an "
        f"unused fallback: {unused_fallback_probes}"
    )
    assert tick_delays and tick_delays[0] < 0.15, (
        "profile discovery blocked the async runtime before the preferred "
        f"carrier could run (ticker delay={tick_delays})"
    )


@pytest.mark.asyncio
async def test_text_only_turn_keeps_the_single_provider_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-529-07. No caller definitions, no route change - pin stays exclusive.

    The noncarrying pinned provider must still SERVE this turn: the exclusion
    applies only to turns carrying caller-supplied schemas.
    """
    contacted: list[str] = []
    _install_real_route_seams(
        monkeypatch,
        pinned_provider="gemini-cli",
        carrying_providers={"kimi"},
        contacted=contacted,
    )

    request = RuntimeRequest(
        prompt="hello",
        cwd=".",
        task_name="persona_turn",
        runtime_lane=RUNTIME_LANE_GENERIC,
    )
    resolved = [p.provider for p in lane_router._resolve_lane_profiles(request)]
    assert resolved == ["gemini-cli"], f"a text turn was widened: {resolved}"

    result = await lane_router.run_with_runtime_lanes(request)
    assert result.provider == "gemini-cli"
    assert contacted == ["gemini-cli"]
