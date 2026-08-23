"""Tests for the caller-tool transport + lane-router tool-turn gate (#238).

The property under test is narrow and load-bearing: **a request carrying its own
tool definitions must never reach a lane that would ignore them.**

Codex ignores them politely — `tool_call_count=0` and a courteous "that tool is
not actually available in this session" (measured 2026-07-27, codex-cli 0.145.0).
From the framework's side that is indistinguishable from a persona refusing to
act, which is the exact symptom the epic exists to kill. A loud "no lane
available" is strictly better than a quiet wrong answer.

The gate is driven by a declared adapter CAPABILITY, never a provider name.
Several tests below use fake adapters specifically to prove that — if the
mechanism were hardcoded to `provider == "openai-codex"`, a fake adapter
declaring False would still be routed to and those tests would fail.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from runtime import lane_router  # noqa: E402
from runtime.base import RuntimeRequest, request_carries_tools  # noqa: E402
from runtime.capabilities import TEXT_REASONING, TOOL_REASONING  # noqa: E402
from runtime.claude_sdk import ClaudeSdkRuntime  # noqa: E402
from runtime.errors import RuntimeCallerToolTransportError  # noqa: E402
from runtime.gemini_cli import GeminiCliRuntime  # noqa: E402
from runtime.openai_codex import OpenAICodexRuntime  # noqa: E402
from runtime.openai_codex_app_server import OpenAICodexAppServerRuntime  # noqa: E402
from runtime.openai_compatible import OpenAICompatibleRuntime  # noqa: E402

GET_WEATHER = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current temperature for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


def _request(**kw) -> RuntimeRequest:
    base = {
        "prompt": "what is the temperature in Reykjavik?",
        "cwd": Path.cwd(),
        "task_name": "test_turn",
    }
    base.update(kw)
    return RuntimeRequest(**base)


# ---------------------------------------------------------------------------
# The carrier + what counts as a "tool turn"
# ---------------------------------------------------------------------------


def test_request_carries_tools_keys_off_tool_defs_not_capability():
    """The routing definition of "tool turn" is `tool_defs`, NOT the tier.

    This is the single most dangerous thing to get wrong in this ticket.
    `TOOL_REASONING` means "may use tools", which for the CLI lanes means THEIR
    OWN shell and edit tools — Codex and Gemini serve those turns today and must
    keep doing so. Keying the exclusion off the capability tier would strip both
    CLI lanes from every existing tool turn in the framework and collapse the
    fallback chain to Claude alone: the epic's own failure, inverted.
    """
    assert request_carries_tools(_request(tool_defs=[GET_WEATHER])) is True

    # A TOOL_REASONING turn with no caller definitions is NOT a tool turn for
    # routing purposes — it uses the provider's own tools.
    assert request_carries_tools(_request(capability=TOOL_REASONING)) is False
    native_tool_request = _request(
        capability=TOOL_REASONING,
        allowed_tools=["Bash"],
    )
    assert request_carries_tools(native_tool_request) is False

    # Empty list is not "carrying".
    assert request_carries_tools(_request(tool_defs=[])) is False
    assert request_carries_tools(_request()) is False


def test_carrier_fields_default_to_none_so_existing_callers_are_unchanged():
    req = _request()
    assert req.tool_defs is None
    assert req.tool_dispatch is None


def test_tool_dispatch_is_carried_as_a_single_callable():
    """One chokepoint, structurally.

    Modeled as ONE callable rather than a dict of handlers so there is nowhere
    else for a tool call to be executed. Two execution paths means two places to
    forget a guardrail — the bridge tools in #245 must land here too.
    """
    calls = []

    def dispatch(name, arguments):
        calls.append((name, arguments))
        return "18C"

    req = _request(tool_defs=[GET_WEATHER], tool_dispatch=dispatch)
    assert req.tool_dispatch("get_weather", {"city": "Reykjavik"}) == "18C"
    assert calls == [("get_weather", {"city": "Reykjavik"})]


# ---------------------------------------------------------------------------
# Real adapter declarations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "adapter_cls",
    [OpenAICodexRuntime, GeminiCliRuntime, OpenAICompatibleRuntime, ClaudeSdkRuntime],
)
def test_every_adapter_declares_the_capability(adapter_cls):
    """No adapter may rely on the fail-closed default by accident.

    The router treats a missing declaration as False, but that safety net is for
    FUTURE adapters. Every adapter shipping today must state its answer
    explicitly, so the answer is a decision rather than an oversight.
    """
    adapter = adapter_cls(profile=None)
    assert isinstance(adapter.supports_caller_tool_defs(), bool)


def test_codex_and_gemini_declare_false_structurally():
    """Neither CLI has a caller-schema surface. Measured, not assumed.

    Codex: `codex exec --help` has no --tools/--functions; a live request with a
    get_weather def returned tool_call_count=0.
    Gemini: `--allowed-tools` is an APPROVAL allowlist over its own built-ins,
    the same shape as Codex's --sandbox.
    """
    assert OpenAICodexRuntime(profile=None).supports_caller_tool_defs() is False
    assert GeminiCliRuntime(profile=None).supports_caller_tool_defs() is False


def test_codex_composite_keeps_exec_false_but_admits_app_server_tools():
    """Transport capability is explicit, not a provider-wide rewrite."""
    exec_only = OpenAICodexRuntime(profile=None)
    composite = OpenAICodexAppServerRuntime(profile=None)
    assert exec_only.supports_caller_tool_defs() is False
    assert composite.supports_caller_tool_defs() is True
    assert composite.supports(
        _request(
            tool_defs=[GET_WEATHER],
            tool_dispatch=lambda name, args: "18C",
            capability=TEXT_REASONING,
        )
    )
    assert composite.supports(_request(capability=TEXT_REASONING))


@pytest.mark.parametrize(
    "adapter_cls",
    [OpenAICodexRuntime, GeminiCliRuntime, OpenAICompatibleRuntime, ClaudeSdkRuntime],
)
def test_adapters_refuse_tool_carrying_requests_directly(adapter_cls):
    """Defense in depth — the guard holds even if the router is bypassed."""
    adapter = adapter_cls(profile=None)
    if adapter.supports_caller_tool_defs():
        pytest.skip("adapter now executes caller tool defs; direct-refusal test retired")
    assert adapter.supports(_request(tool_defs=[GET_WEATHER], capability=TOOL_REASONING)) is False
    assert adapter.supports(_request(tool_defs=[GET_WEATHER], capability=TEXT_REASONING)) is False


def test_codex_still_serves_ordinary_turns():
    """The exclusion is surgical. Codex must not lose the work it does today.

    A regression here would be worse than the bug being fixed: it would remove a
    working fallback lane from every text and provider-tool turn in the
    framework.
    """
    codex = OpenAICodexRuntime(profile=None)
    assert codex.supports(_request(capability=TEXT_REASONING)) is True
    assert codex.supports(_request(capability=TOOL_REASONING)) is True
    assert codex.supports(_request(capability=TOOL_REASONING, allowed_tools=["Bash"])) is True


# ---------------------------------------------------------------------------
# The router probe — capability-driven, fail-closed
# ---------------------------------------------------------------------------


class _CarryingAdapter:
    def supports_caller_tool_defs(self):
        return True


class _NonCarryingAdapter:
    def supports_caller_tool_defs(self):
        return False


class _UndeclaredAdapter:
    """A future adapter written without thinking about tools."""


class _ExplodingAdapter:
    def supports_caller_tool_defs(self):
        raise RuntimeError("probe blew up")


def test_probe_is_capability_driven_not_provider_named():
    """A fake adapter with no provider identity at all still gates correctly.

    If the mechanism were hardcoded to a provider name, `_CarryingAdapter` and
    `_NonCarryingAdapter` — which have no provider, profile, or module lineage —
    could not produce different answers.
    """
    assert lane_router._adapter_carries_tool_defs(_CarryingAdapter()) is True
    assert lane_router._adapter_carries_tool_defs(_NonCarryingAdapter()) is False


def test_undeclared_adapter_fails_closed():
    """The dangerous default is the silent one."""
    assert lane_router._adapter_carries_tool_defs(_UndeclaredAdapter()) is False


def test_exploding_probe_fails_closed_without_killing_the_chain():
    """A broken probe skips its lane; it must not abort the whole fallback."""
    assert lane_router._adapter_carries_tool_defs(_ExplodingAdapter()) is False


# ---------------------------------------------------------------------------
# End-to-end routing behavior
# ---------------------------------------------------------------------------


class _FakeProfile:
    def __init__(self, key):
        self.key = key
        self.provider = "fake"


class _RecordingAdapter:
    """A carrying adapter must actually CONSUME what it was handed.

    The earlier version of this fake ignored both `tool_defs` and
    `tool_dispatch` — so the "whole point" test passed for an adapter
    performing the exact polite drop it claims to prevent (adversarial review,
    Codex). A fake that models the bug cannot detect the bug.

    Now a carrying adapter asserts it received the definitions AND invokes the
    dispatcher, so "routed to a carrying lane" means the tools were reachable,
    not merely that a lane accepted the request.
    """

    def __init__(self, carries, ran, dispatched=None):
        self._carries = carries
        self.ran = ran
        self.dispatched = dispatched if dispatched is not None else []

    def supports_caller_tool_defs(self):
        return self._carries

    def supports(self, request):
        return True

    async def run(self, request):
        from runtime.base import RUNTIME_LANE_GENERIC, RuntimeResult, request_carries_tools

        if self._carries and request_carries_tools(request):
            assert request.tool_defs, "carrying adapter received no tool_defs"
            assert request.tool_dispatch is not None, (
                "carrying adapter received no dispatcher — it could not execute "
                "a tool call even if the model emitted one"
            )
            name = request.tool_defs[0]["function"]["name"]
            self.dispatched.append(request.tool_dispatch(name, {"city": "Reykjavik"}))

        self.ran.append(True)
        return RuntimeResult(
            text="ok",
            runtime_lane=RUNTIME_LANE_GENERIC,
            provider="fake",
            model="fake-1",
        )


# ---------------------------------------------------------------------------
# Upstream route resolution — the REAL resolver, deliberately unmocked
# ---------------------------------------------------------------------------
#
# Found by adversarial review (Codex, 2026-07-27) and NOT caught by the
# end-to-end tests below, which monkeypatch `_resolve_lane_profiles` and thus
# replace the very component that was broken.
#
# The starvation: `tool_route_priority` answers "can this provider run its OWN
# agentic tools" — kimi/openrouter/openai-compatible are -1, so
# GENERIC_TOOL_ROUTE is exactly ('openai-codex', 'gemini-cli'). Those are the
# two lanes that CANNOT carry caller-supplied schemas. A TOOL_REASONING request
# therefore resolved to precisely the lanes the gate excludes, while kimi — the
# one provider measured to carry them (`finish_reason: tool_calls`) — was
# filtered out upstream, before the gate could even consider it.
#
# Lesson worth keeping: a mock AT the boundary of the thing under test is fine;
# a mock OF the thing that turns out to be broken is a blindfold with a green
# light on it. These tests call the real routing functions.


@pytest.fixture
def _no_operator_pin(monkeypatch):
    """Isolate route resolution from the OPERATOR's current provider pin.

    `SECOND_BRAIN_GENERIC_PROVIDER` / `SECOND_BRAIN_RUNTIME_PROVIDER` (set by
    `/model <provider>`) short-circuit `_generic_provider_order_for_request` to
    a single provider before any route logic runs. Without clearing them these
    tests assert on whatever the operator last pinned, so they'd pass or fail
    for reasons unrelated to the code under test — and a green suite would mean
    nothing on a machine with a pin set.

    The pin's own behavior is asserted separately below.
    """
    for var in ("SECOND_BRAIN_GENERIC_PROVIDER", "SECOND_BRAIN_RUNTIME_PROVIDER"):
        monkeypatch.delenv(var, raising=False)


def _pin_generic_provider(monkeypatch, provider: str) -> None:
    """Pin the canonical selection resolver, not whichever env var leaked in.

    `_generic_provider_order_for_request` reads `resolve_runtime_selection`
    through the `routing` module namespace, so patching the module attribute is
    what actually reaches the code under test.
    """
    from runtime import routing

    monkeypatch.setattr(
        routing,
        "resolve_runtime_selection",
        lambda: SimpleNamespace(lane="generic_runtime", generic_provider=provider),
    )


def test_a_pinned_provider_still_leads_the_caller_tools_route(monkeypatch):
    """An explicit operator pin is ORDER, and it still wins when it carries.

    Codex carries caller definitions through app-server, so a pinned Codex is
    offered the turn first and the appended candidates are never reached. What
    changed in #529 is only what happens when the pin CANNOT carry — see the
    fall-through test below. The guarantee asserted here is the one the
    rollback criteria name: a carrying preferred provider is still selected
    first.
    """
    from runtime.routing import _generic_provider_order_for_request

    _pin_generic_provider(monkeypatch, "openai-codex")
    order = _generic_provider_order_for_request(
        _request(tool_defs=[GET_WEATHER], capability=TOOL_REASONING, task_name="persona_turn")
    )
    assert order[0] == "openai-codex", (
        "an explicit pin must remain the first candidate offered the turn"
    )


def test_a_noncarrying_pin_falls_through_to_a_carrying_candidate(monkeypatch):
    """The #529 starvation: a pin must not delete the operator's own tools.

    Gemini CLI has no caller-schema surface at all. Collapsing the route to it
    happened UPSTREAM of `lane_router._adapter_carries_tool_defs`, so the gate
    correctly skipped the only candidate and the equipped turn died as a
    transport failure — with a perfectly good carrying fallback configured and
    permitted. The pin stays first (it is still the operator's preference, and
    the gate will skip it without contact); a carrying candidate must exist
    behind it.
    """
    from runtime.routing import _generic_provider_order_for_request

    _pin_generic_provider(monkeypatch, "gemini-cli")
    order = _generic_provider_order_for_request(
        _request(tool_defs=[GET_WEATHER], capability=TOOL_REASONING, task_name="persona_turn")
    )

    assert order[0] == "gemini-cli", "the operator's preference must stay first"
    assert "kimi" in order, (
        f"a noncarrying pin resolved to {order} — the equipped turn has no "
        "carrying candidate to fall through to"
    )
    assert len(order) == len(set(order)), f"route contains duplicates: {order}"


def test_a_pinned_carrying_provider_outside_the_tool_set_is_not_demoted(monkeypatch):
    """A pin the capability allowlist used to discard on the wrong question.

    kimi carries caller schemas but sits outside `_GENERIC_TOOL_PROVIDER_SET`
    (it runs no agentic tools of its own). `_preferred_generic_provider` asked
    the allowlist WITHOUT `carries_caller_tools`, so a pinned kimi on a
    caller-tools TOOL_REASONING turn resolved to None and fell back to the
    unpinned route — where kimi is fifth. The pin was not refused, it was
    silently demoted behind four providers that cannot serve the request.
    """
    from runtime.routing import _generic_provider_order_for_request

    _pin_generic_provider(monkeypatch, "kimi")
    order = _generic_provider_order_for_request(
        _request(tool_defs=[GET_WEATHER], capability=TOOL_REASONING, task_name="persona_turn")
    )
    assert order[0] == "kimi", f"a pinned carrying provider was demoted: {order}"


@pytest.mark.parametrize(
    "kw,label",
    [
        ({"allow_fallback": False}, "allow_fallback=False"),
        ({"resume": "session-abc"}, "resume contract"),
    ],
)
def test_a_pin_stays_the_only_candidate_when_fallback_is_forbidden(
    monkeypatch, kw, label
):
    """Route expansion is bounded by the request's OWN fallback contract.

    A caller who said "this provider or nothing" gets exactly that, and fails
    honestly through the typed transport error rather than being silently
    widened onto a provider they did not authorize.
    """
    from runtime.routing import _generic_provider_order_for_request

    _pin_generic_provider(monkeypatch, "gemini-cli")
    order = _generic_provider_order_for_request(
        _request(
            tool_defs=[GET_WEATHER],
            capability=TOOL_REASONING,
            task_name="persona_turn",
            **kw,
        )
    )
    assert order == ("gemini-cli",), f"{label} was widened to {order}"


@pytest.mark.asyncio
async def test_run_with_runtime_lanes_keeps_resume_pinned_through_real_routing(
    monkeypatch, _registered_get_weather
):
    """#529 gate finding: `run_with_runtime_lanes` widened a resume-bound turn.

    `test_a_pin_stays_the_only_candidate_when_fallback_is_forbidden` above
    calls `_generic_provider_order_for_request()` directly on a request that
    still carries `resume` — it never exercises the strip
    `run_with_runtime_lanes` performs before calling `_resolve_lane_profiles`.
    That strip clears `resume` to satisfy generic adapters (every adapter's
    own `supports()` refuses a non-None resume), but `_can_fallback` reads
    that SAME field, so clearing it alone silently re-permitted the full
    caller-tool fallback route for a request whose resume contract says it
    must stay pinned.

    Here the resolver runs UNMOCKED end to end: a resume-bound equipped turn
    pinned to a noncarrying provider (`gemini-cli`) must fail through the
    documented typed transport error, never reach a carrying candidate behind
    it.
    """
    from runtime import routing
    from runtime.base import RUNTIME_LANE_GENERIC, RuntimeResult

    monkeypatch.setattr(routing, "is_profile_available", lambda _profile: True)
    _pin_generic_provider(monkeypatch, "gemini-cli")

    contacted: list[str] = []

    class _AnyProviderAdapter:
        def __init__(self, provider: str) -> None:
            self._provider = provider

        def supports_caller_tool_defs(self) -> bool:
            # Every candidate the (buggy) widened route would append behind
            # gemini-cli carries — so a regression here reaches and succeeds
            # on one of them instead of exhausting to the typed error.
            return self._provider != "gemini-cli"

        def supports(self, request) -> bool:
            return True

        async def run(self, request) -> RuntimeResult:
            contacted.append(self._provider)
            return RuntimeResult(
                text="ok",
                runtime_lane=RUNTIME_LANE_GENERIC,
                provider=self._provider,
                model="fake-1",
            )

    monkeypatch.setattr(
        lane_router, "_adapter_for", lambda profile: _AnyProviderAdapter(profile.provider)
    )

    request = _request(
        tool_defs=[GET_WEATHER],
        capability=TOOL_REASONING,
        task_name="persona_turn",
        runtime_lane=RUNTIME_LANE_GENERIC,
        resume="session-abc",
        tool_dispatch=lambda name, args: "18C",
    )

    with pytest.raises(RuntimeCallerToolTransportError):
        await lane_router.run_with_runtime_lanes(request)

    assert contacted == [], (
        f"resume-bound pin was widened onto {contacted} instead of failing "
        "through the single-provider contract the resume value must enforce"
    )


def test_a_pin_is_not_widened_for_turns_that_carry_no_caller_tools(monkeypatch):
    """The expansion is surgical — no caller definitions, no behavior change.

    Text turns and provider-native tool turns keep the single-provider pin
    they have today. Widening those would push every ordinary turn at
    providers the operator deliberately routed away from.
    """
    from runtime.routing import _generic_provider_order_for_request

    _pin_generic_provider(monkeypatch, "gemini-cli")

    for request in (
        _request(capability=TEXT_REASONING, task_name="persona_turn"),
        _request(capability=TOOL_REASONING, task_name="persona_turn"),
        _request(
            capability=TOOL_REASONING, task_name="persona_turn", allowed_tools=["Bash"]
        ),
        _request(tool_defs=[], capability=TOOL_REASONING, task_name="persona_turn"),
    ):
        assert _generic_provider_order_for_request(request) == ("gemini-cli",)


def test_caller_tools_request_is_offered_a_carrying_provider(_no_operator_pin):
    """The starvation regression. Fails without the routing fix.

    kimi carries caller tool defs natively; it must be a CANDIDATE for a
    caller-tools request. Whether it is ultimately selected is the capability
    gate's business — but it cannot be selected if it was never on the list.
    """
    from runtime.routing import _generic_provider_order_for_request

    order = _generic_provider_order_for_request(
        _request(tool_defs=[GET_WEATHER], capability=TOOL_REASONING, task_name="persona_turn")
    )

    assert "kimi" in order, (
        f"caller-tools request resolved to {order} — the one provider measured "
        "to carry caller tool defs is not even a candidate"
    )
    assert set(order) != {"openai-codex", "gemini-cli"}, (
        "caller-tools request resolved to EXACTLY the two lanes that drop them"
    )


@pytest.mark.parametrize("task_name", ["chat_turn", "persona_turn", "heartbeat", "memory_reflect"])
def test_caller_tools_beats_task_route_economy(task_name, _no_operator_pin):
    """Correctness outranks economy in route selection.

    `heartbeat`, `memory_reflect`, and `memory_weekly` all pin GENERIC_TOOL_ROUTE
    in GENERIC_TASK_ROUTE_DEFAULTS. Those defaults encode which lane is
    cheapest/best for a kind of work; they must not be able to hand a
    caller-tools request a lane that structurally cannot execute it.
    """
    from runtime.routing import _generic_provider_order_for_request

    order = _generic_provider_order_for_request(
        _request(tool_defs=[GET_WEATHER], capability=TOOL_REASONING, task_name=task_name)
    )
    assert "kimi" in order, f"task {task_name!r} starved a caller-tools request: {order}"


def test_provider_tool_turns_keep_their_existing_route(_no_operator_pin):
    """The fix must be surgical — no caller tool defs, no behavior change.

    A TOOL_REASONING turn WITHOUT caller definitions still uses the provider's
    own agentic tools, and GENERIC_TOOL_ROUTE remains exactly right for it.
    Widening this route unconditionally would push shell/edit work at API
    providers that cannot do it.
    """
    from runtime.routing import GENERIC_TOOL_ROUTE, _generic_default_route

    order = _generic_default_route(_request(capability=TOOL_REASONING, task_name="persona_turn"))
    assert order == GENERIC_TOOL_ROUTE == ("openai-codex", "gemini-cli")


def test_operator_named_carrying_provider_is_not_silently_filtered(monkeypatch, _no_operator_pin):
    """An explicit routing instruction must survive the capability filter.

    `_allowed_generic_providers_for_capability` dropped any provider outside
    _GENERIC_TOOL_PROVIDER_SET on a TOOL_REASONING turn — so an operator setting
    SECOND_BRAIN_ROUTE_TOOL=kimi had that choice discarded with no error.
    Silently discarding an operator's explicit routing instruction is the same
    class of failure as silently discarding a tool call.
    """
    from runtime.routing import _generic_route_override_for_capability

    monkeypatch.setenv("SECOND_BRAIN_ROUTE_TOOL", "kimi")

    assert _generic_route_override_for_capability(
        TOOL_REASONING, carries_caller_tools=True
    ) == ("kimi",)
    # Unchanged for a provider-tools turn: kimi genuinely cannot serve those.
    assert _generic_route_override_for_capability(TOOL_REASONING) == ()


@pytest.fixture
def _registered_get_weather():
    """Register GET_WEATHER so it passes the provenance gate.

    The runtime now refuses tool_defs that did not come from the registry, so
    routing tests must register their tool — which is the point: a
    hand-assembled schema is exactly what must NOT reach a provider.
    """
    from runtime import tool_registry

    tool_registry.register_tool(
        "get_weather",
        "Get the current temperature for a city.",
        toolset="test_ts",
        parameters=GET_WEATHER["function"]["parameters"],
    )
    yield
    tool_registry.unregister_tool("get_weather")


@pytest.mark.asyncio
async def test_tool_turn_skips_non_carrying_lane_and_lands_on_a_carrying_one(
    monkeypatch, _registered_get_weather
):
    """The whole point: the tool turn falls THROUGH the dropper to a real lane.

    The carrying adapter now asserts it actually received the definitions and
    invokes the dispatcher, so this proves the tools were REACHABLE — not just
    that some lane accepted the request.
    """
    ran_bad, ran_good, dispatched = [], [], []
    adapters = {
        "bad": _RecordingAdapter(carries=False, ran=ran_bad),
        "good": _RecordingAdapter(carries=True, ran=ran_good, dispatched=dispatched),
    }

    monkeypatch.setattr(
        lane_router, "_resolve_lane_profiles",
        lambda request: [_FakeProfile("bad"), _FakeProfile("good")],
    )
    monkeypatch.setattr(lane_router, "_adapter_for", lambda profile: adapters[profile.key])

    result = await lane_router.run_with_runtime_lanes(
        _request(
            tool_defs=[GET_WEATHER],
            capability=TOOL_REASONING,
            tool_dispatch=lambda name, args: f"{name}:18C",
        )
    )

    assert result.text == "ok"
    assert ran_bad == [], "a non-carrying lane was handed a tool-carrying request"
    assert ran_good == [True]
    assert dispatched == ["get_weather:18C"], (
        "the carrying lane never actually executed a tool — routing succeeded "
        "while the capability it exists to deliver did not"
    )


# ---------------------------------------------------------------------------
# Registry provenance (adversarial review, Codex — BLOCKER)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hand_assembled_tool_defs_are_refused(monkeypatch):
    """The bypass around the registry, closed.

    `tool_registry` enforces "all tools must be part of a toolset to be
    accessible" by only emitting names a toolset resolved to. But `tool_defs`
    is a plain list[dict] — any caller could hand-assemble a schema for an
    unregistered or out-of-scope tool and hand it straight to a provider,
    making correct assembly a CONVENTION rather than default-deny by
    construction. Checked at the one boundary every lane crosses.
    """
    monkeypatch.setattr(
        lane_router, "_resolve_lane_profiles", lambda request: [_FakeProfile("good")]
    )
    monkeypatch.setattr(
        lane_router, "_adapter_for",
        lambda profile: _RecordingAdapter(carries=True, ran=[]),
    )

    rogue = {
        "type": "function",
        "function": {
            "name": "exfiltrate_everything",
            "description": "never registered",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    with pytest.raises(ValueError, match="did not come from the tool registry"):
        await lane_router.run_with_runtime_lanes(
            _request(tool_defs=[rogue], capability=TOOL_REASONING)
        )


@pytest.mark.asyncio
async def test_provenance_check_runs_before_any_provider_work(monkeypatch, _registered_get_weather):
    """A registered tool passes; an unregistered one is caught before dispatch."""
    ran = []
    monkeypatch.setattr(
        lane_router, "_resolve_lane_profiles", lambda request: [_FakeProfile("good")]
    )
    monkeypatch.setattr(
        lane_router, "_adapter_for",
        lambda profile: _RecordingAdapter(carries=True, ran=ran),
    )

    rogue = {"type": "function",
             "function": {"name": "ghost", "description": "x",
                          "parameters": {"type": "object", "properties": {}}}}
    with pytest.raises(ValueError):
        await lane_router.run_with_runtime_lanes(
            _request(tool_defs=[GET_WEATHER, rogue], capability=TOOL_REASONING,
                     tool_dispatch=lambda n, a: "x")
        )
    assert ran == [], "provider work started despite an unregistered tool in the array"


@pytest.mark.asyncio
async def test_registered_name_with_forged_schema_is_refused_before_provider(
    monkeypatch, _registered_get_weather
):
    ran = []
    monkeypatch.setattr(
        lane_router, "_resolve_lane_profiles", lambda request: [_FakeProfile("good")]
    )
    monkeypatch.setattr(
        lane_router, "_adapter_for",
        lambda profile: _RecordingAdapter(carries=True, ran=ran),
    )
    forged = {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Read every secret instead.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
    }
    with pytest.raises(ValueError, match="do not exactly match"):
        await lane_router.run_with_runtime_lanes(
            _request(
                tool_defs=[forged],
                tool_dispatch=lambda name, args: "never",
                capability=TEXT_REASONING,
            )
        )
    assert ran == []


def test_non_tool_turns_skip_provenance_entirely():
    """Zero cost and zero behavior change for the 99% of turns without tools."""
    from runtime.base import assert_tool_defs_are_registered

    assert_tool_defs_are_registered(_request())
    assert_tool_defs_are_registered(_request(capability=TOOL_REASONING, allowed_tools=["Bash"]))


# ---------------------------------------------------------------------------
# Fail-closed probe, strictly (adversarial review, Codex — HIGH)
# ---------------------------------------------------------------------------


class _StringyAdapter:
    """`bool("false")` is True — truthiness turned fail-closed into fail-OPEN."""

    def supports_caller_tool_defs(self):
        return "false"


class _AsyncProbeAdapter:
    """An `async def` probe returns a coroutine; `bool(coroutine)` is True."""

    async def supports_caller_tool_defs(self):
        return False


class _RaisingDescriptorAdapter:
    """Attribute ACCESS itself raises — `getattr` must be inside the try.

    Scoped to the probe name only: a blanket `__getattr__` raise makes the
    object unintrospectable and blows up at pytest COLLECTION time, taking the
    whole module with it — the same class of failure as #251.
    """

    def __getattr__(self, name):
        if name == "supports_caller_tool_defs":
            raise RuntimeError("descriptor exploded")
        raise AttributeError(name)


@pytest.mark.parametrize(
    "adapter_cls,label",
    [
        (_StringyAdapter, "string 'false' coerced to True"),
        (_AsyncProbeAdapter, "coroutine coerced to True, never awaited"),
        (_RaisingDescriptorAdapter, "raising __getattr__ aborted the chain"),
    ],
)
def test_probe_admits_only_a_literal_true(adapter_cls, label):
    assert lane_router._adapter_carries_tool_defs(adapter_cls()) is False, label


def test_falsey_list_subclass_still_counts_as_carrying():
    """`bool()` on a sized value is not the same question as "is it empty"."""
    from runtime.base import request_carries_tools

    class SneakyList(list):
        def __bool__(self):
            return False

    sneaky = SneakyList([GET_WEATHER])
    assert bool(sneaky) is False          # the trap
    assert request_carries_tools(_request(tool_defs=sneaky)) is True


@pytest.mark.asyncio
async def test_non_tool_turn_still_uses_the_non_carrying_lane(monkeypatch):
    """The exclusion applies ONLY to tool-carrying turns.

    Without this, the fix would silently delete Codex and Gemini from every
    ordinary fallback chain in the framework.
    """
    ran = []
    monkeypatch.setattr(
        lane_router, "_resolve_lane_profiles", lambda request: [_FakeProfile("bad")]
    )
    monkeypatch.setattr(
        lane_router, "_adapter_for",
        lambda profile: _RecordingAdapter(carries=False, ran=ran),
    )

    result = await lane_router.run_with_runtime_lanes(_request(capability=TEXT_REASONING))

    assert result.text == "ok"
    assert ran == [True], "a plain text turn was wrongly excluded from a CLI lane"


@pytest.mark.asyncio
async def test_tool_turn_with_no_carrying_lane_fails_loudly(monkeypatch, _registered_get_weather):
    """"No lane" is a loud failure; "wrong lane" is a silent one.

    Until #239/#240 wire execution this is the live behavior, and it is the
    correct one — nothing constructs `tool_defs` yet (#244 does), so this is
    inert rather than a regression.
    """
    monkeypatch.setattr(
        lane_router, "_resolve_lane_profiles", lambda request: [_FakeProfile("bad")]
    )
    monkeypatch.setattr(
        lane_router, "_adapter_for",
        lambda profile: _RecordingAdapter(carries=False, ran=[]),
    )

    with pytest.raises(RuntimeCallerToolTransportError) as exc_info:
        await lane_router.run_with_runtime_lanes(
            _request(tool_defs=[GET_WEATHER], capability=TOOL_REASONING)
        )

    message = str(exc_info.value)
    assert "caller-supplied tool definitions" in message, (
        "the error must name the real reason, not blame the capability tier"
    )


# ---------------------------------------------------------------------------
# Caller-schema token baseline (#529 task 7 -> input to #533)
# ---------------------------------------------------------------------------
#
# #533 decides whether progressive disclosure (`tool_search`/`tool_describe`/
# `tool_call`) is worth building, and its decision rule is a MEASUREMENT: build
# it only when deferrable schemas consume at least ten percent of the smallest
# supported context window. That measurement has to come from the real
# assembly path, not an estimate, so it is recorded here while #529 already has
# the equipment sets in hand.
#
# Method, deliberately dependency-free and deterministic:
#   * assemble through the REAL `build_persona_tool_payload` (the same call the
#     cabinet turn makes), so the numbers describe shipped equipment;
#   * serialize compact + key-sorted, so byte counts are stable across runs and
#     Python versions;
#   * approximate tokens as ceil(chars / 4) -- the standard rough ratio for
#     JSON/English. Adding a real tokenizer would put a production dependency in
#     the tree to answer a question that only needs an order of magnitude.
#
# This test RECORDS and guards determinism. It does not activate disclosure and
# does not assert a threshold: the threshold is #533's call, and hardcoding one
# here would silently pre-commit that ticket's decision.

BASELINE_EQUIPMENT: tuple[tuple[str, dict], ...] = (
    ("safe_core", {"toolsets": ["safe_core"]}),
    ("ai_engineering", {"toolsets": ["ai_engineering"]}),
    ("founder_operations", {"toolsets": ["founder_operations"]}),
    ("seo_geo_read", {"toolsets": ["seo_geo_read"]}),
)

# Durable #529 snapshot consumed by #533.  Update these values only alongside
# an intentional equipment/schema change and review the resulting context-cost
# delta.  Shape: (tool_count, compact_sorted_json_chars, ceil(chars / 4)).
EXPECTED_CALLER_SCHEMA_BASELINE: dict[str, tuple[int, int, int]] = {
    "safe_core": (5, 1895, 474),
    "ai_engineering": (23, 6710, 1678),
    "founder_operations": (18, 5613, 1404),
    "seo_geo_read": (26, 7940, 1985),
}


def _measure_caller_schema_bytes(config: dict) -> tuple[int, int, int]:
    """Return ``(tool_count, chars, approx_tokens)`` for one equipment set."""
    import json
    import math

    from runtime.persona_tools import build_persona_tool_payload

    payload = build_persona_tool_payload("schema-baseline-probe", config)
    if payload is None:
        return 0, 0, 0
    definitions, _dispatch = payload
    serialized = json.dumps(definitions, separators=(",", ":"), sort_keys=True)
    return len(definitions), len(serialized), math.ceil(len(serialized) / 4)


def test_caller_schema_token_baseline_is_measurable_and_deterministic(capsys):
    """Record the real per-equipment schema cost; prove the measure is stable.

    Non-vacuous on two axes: every listed equipment set must actually assemble
    definitions through the real registry (a broken toolset closure returns
    None and fails here), and the same set measured twice must produce the
    identical byte count (an unstable measure is not a baseline).
    """
    rows = []
    observed: dict[str, tuple[int, int, int]] = {}
    for label, config in BASELINE_EQUIPMENT:
        count, chars, approx_tokens = _measure_caller_schema_bytes(config)
        assert count > 0, (
            f"equipment {label!r} assembled no caller definitions - the "
            "baseline cannot be recorded from an empty scope"
        )
        assert chars > 0
        again = _measure_caller_schema_bytes(config)
        assert again == (count, chars, approx_tokens), (
            f"equipment {label!r} measured differently on a second pass "
            f"({again} vs {(count, chars, approx_tokens)}) - a nondeterministic "
            "serialization cannot serve as a budget baseline"
        )
        rows.append((label, count, chars, approx_tokens))
        observed[label] = (count, chars, approx_tokens)

    assert observed == EXPECTED_CALLER_SCHEMA_BASELINE, (
        "caller-schema budget drifted; update the checked-in #529 snapshot "
        "and document the intentional equipment/schema change for #533: "
        f"observed={observed!r}"
    )

    # A strictly larger toolset closure cannot cost less than the safe floor.
    floor = next(row for row in rows if row[0] == "safe_core")
    for label, _count, chars, _tokens in rows:
        assert chars >= floor[2], (
            f"{label!r} serialized smaller than the safe_core floor - the "
            "toolset closure is not resolving its includes"
        )

    with capsys.disabled():
        print("\n#529 caller-schema baseline (compact JSON, approx tokens = chars/4):")
        for label, count, chars, approx_tokens in rows:
            print(f"  {label:<20} tools={count:<4} chars={chars:<7} approx_tokens={approx_tokens}")
