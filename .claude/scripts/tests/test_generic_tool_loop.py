"""Tests for the generic-lane chat_completions tool loop (#239).

This is the ticket that makes a tool actually RUN on a non-Claude lane. The
properties worth proving are behavioral, not structural:

* a tool call is dispatched, its result is fed BACK, and the model gets to use it
* every execution goes through `request.tool_dispatch` — the one chokepoint
* a name the model was never offered is refused, not executed
* a failing tool is conversational input, not a failed turn
* usage sums across round trips instead of reporting only the last one

The fake client below returns scripted completions so the loop is exercised
end to end without a network call. The live proof that the WIRE carries tools
is separate and already measured: Kimi K3 returned `finish_reason: tool_calls`
for an OpenAI-format definition on 2026-07-27.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from runtime.base import RuntimeRequest  # noqa: E402
from runtime.capabilities import TEXT_REASONING, TOOL_REASONING  # noqa: E402
from runtime.openai_compatible import OpenAICompatibleRuntime  # noqa: E402
from runtime.profiles import RuntimeProfile  # noqa: E402

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


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _tool_call(call_id: str, name: str, arguments: str):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _completion(content="", tool_calls=None, usage=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))],
        usage=usage,
    )


class _FakeClient:
    """Replays scripted completions and records every request it received."""

    def __init__(self, script):
        self._script = list(script)
        self.requests: list[dict] = []
        outer = self

        class _Completions:
            async def create(self, **kwargs):
                outer.requests.append(kwargs)
                if not outer._script:
                    return _completion(content="(script exhausted)")
                return outer._script.pop(0)

        self.chat = SimpleNamespace(completions=_Completions())


def _profile(provider="kimi"):
    return RuntimeProfile(
        key=f"{provider}-test",
        provider=provider,
        model="k3",
        api_key="test-key",
        base_url="https://example.invalid/v1",
    )


def _request(**kw):
    base = {
        "prompt": "what is the temperature in Reykjavik?",
        "cwd": Path.cwd(),
        "task_name": "test_turn",
    }
    base.update(kw)
    return RuntimeRequest(**base)


async def _run(runtime, request, client):
    """Drive the loop directly, bypassing client construction."""
    messages = [{"role": "user", "content": request.prompt}]
    usage: dict[str, int] = {}
    text, calls = await runtime._run_chat_completions(client, "k3", request, messages, usage)
    return text, calls, usage, messages


# ---------------------------------------------------------------------------
# Capability is per-WIRE, not per-class
# ---------------------------------------------------------------------------


def test_chat_completions_wire_carries_tools_and_responses_wire_does_not():
    """One adapter class serves several providers; they do NOT share a wire.

    Declaring True class-wide would make the `responses` providers claim a
    capability whose code path builds a request with no tools parameter at all
    — a polite drop, which is the exact failure this epic exists to kill.
    """
    assert OpenAICompatibleRuntime(_profile("kimi")).supports_caller_tool_defs() is True
    for responses_provider in ("openai-compatible", "openrouter"):
        assert (
            OpenAICompatibleRuntime(_profile(responses_provider)).supports_caller_tool_defs()
            is False
        ), f"{responses_provider} speaks the responses wire and cannot carry tool defs yet"


def test_supports_accepts_tool_turns_on_either_capability_tier():
    """Carrying the definitions is what makes a turn executable, not the tier."""
    runtime = OpenAICompatibleRuntime(_profile("kimi"))
    for tier in (TEXT_REASONING, TOOL_REASONING):
        assert runtime.supports(_request(tool_defs=[GET_WEATHER], capability=tier)) is True


def test_responses_wire_refuses_tool_turns():
    runtime = OpenAICompatibleRuntime(_profile("openrouter"))
    assert runtime.supports(_request(tool_defs=[GET_WEATHER], capability=TOOL_REASONING)) is False
    # Plain text turns are unaffected.
    assert runtime.supports(_request(capability=TEXT_REASONING)) is True


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_call_is_dispatched_and_the_result_feeds_back():
    """The whole point: the model calls, we execute, it SEES the answer.

    A loop that dispatches but never returns the result would leave the model
    answering from nothing — working plumbing, useless behavior.
    """
    client = _FakeClient([
        _completion(tool_calls=[_tool_call("c1", "get_weather", '{"city":"Reykjavik"}')]),
        _completion(content="It is 2C in Reykjavik."),
    ])
    seen = []

    def dispatch(name, args):
        seen.append((name, args))
        return "2C"

    runtime = OpenAICompatibleRuntime(_profile())
    text, calls, _usage, messages = await _run(
        runtime,
        _request(tool_defs=[GET_WEATHER], capability=TOOL_REASONING, tool_dispatch=dispatch),
        client,
    )

    assert seen == [("get_weather", {"city": "Reykjavik"})], "arguments were not JSON-decoded"
    assert text == "It is 2C in Reykjavik."
    assert [c.name for c in calls] == ["get_weather"]
    assert calls[0].status == "completed"

    tool_msg = [m for m in messages if m.get("role") == "tool"]
    assert tool_msg and tool_msg[0]["content"] == "2C"
    assert tool_msg[0]["tool_call_id"] == "c1", "result must reference the requesting call"

    # Second round trip carried the tool result back.
    assert len(client.requests) == 2
    roles = [m["role"] for m in client.requests[1]["messages"]]
    assert "assistant" in roles and "tool" in roles


@pytest.mark.asyncio
async def test_tools_are_sent_on_the_wire():
    client = _FakeClient([_completion(content="hi")])
    runtime = OpenAICompatibleRuntime(_profile())
    await _run(
        runtime,
        _request(tool_defs=[GET_WEATHER], capability=TOOL_REASONING, tool_dispatch=lambda n, a: ""),
        client,
    )
    assert client.requests[0]["tools"] == [GET_WEATHER]


@pytest.mark.asyncio
async def test_no_tool_defs_is_one_round_trip_with_no_tools_parameter():
    """Byte-identical behavior for the 99% of turns that carry no tools."""
    client = _FakeClient([_completion(content="plain answer")])
    runtime = OpenAICompatibleRuntime(_profile())
    text, calls, _usage, _messages = await _run(runtime, _request(), client)

    assert text == "plain answer"
    assert calls == []
    assert len(client.requests) == 1
    assert "tools" not in client.requests[0]


@pytest.mark.asyncio
async def test_async_dispatchers_are_awaited():
    client = _FakeClient([
        _completion(tool_calls=[_tool_call("c1", "get_weather", '{"city":"Oslo"}')]),
        _completion(content="done"),
    ])

    async def dispatch(name, args):
        return "-1C"

    runtime = OpenAICompatibleRuntime(_profile())
    _text, calls, _usage, messages = await _run(
        runtime,
        _request(tool_defs=[GET_WEATHER], capability=TOOL_REASONING, tool_dispatch=dispatch),
        client,
    )
    assert calls[0].status == "completed"
    assert [m for m in messages if m.get("role") == "tool"][0]["content"] == "-1C"


@pytest.mark.asyncio
async def test_parallel_tool_calls_in_one_turn_all_execute():
    client = _FakeClient([
        _completion(tool_calls=[
            _tool_call("c1", "get_weather", '{"city":"Oslo"}'),
            _tool_call("c2", "get_weather", '{"city":"Lima"}'),
        ]),
        _completion(content="both"),
    ])
    seen = []
    runtime = OpenAICompatibleRuntime(_profile())
    _text, calls, _usage, messages = await _run(
        runtime,
        _request(
            tool_defs=[GET_WEATHER],
            capability=TOOL_REASONING,
            tool_dispatch=lambda n, a: seen.append(a["city"]) or "ok",
        ),
        client,
    )
    assert seen == ["Oslo", "Lima"]
    assert len(calls) == 2
    assert len([m for m in messages if m.get("role") == "tool"]) == 2


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_name_that_was_not_offered_is_refused_not_executed():
    """Scope guard. The model can emit a name it was never given.

    A hallucinated name — or a stale one replayed from history — must not
    reach the dispatcher, or the turn executes past the toolset scope the
    persona was actually granted.
    """
    client = _FakeClient([
        _completion(tool_calls=[_tool_call("c1", "wire_transfer", '{"amount":9999}')]),
        _completion(content="ok"),
    ])
    dispatched = []

    runtime = OpenAICompatibleRuntime(_profile())
    _text, calls, _usage, messages = await _run(
        runtime,
        _request(
            tool_defs=[GET_WEATHER],
            capability=TOOL_REASONING,
            tool_dispatch=lambda n, a: dispatched.append(n) or "SHOULD NOT RUN",
        ),
        client,
    )

    assert dispatched == [], "an un-offered tool reached the dispatcher"
    assert calls[0].status == "refused"
    body = json.loads([m for m in messages if m.get("role") == "tool"][0]["content"])
    assert "was not offered" in body["error"]


@pytest.mark.asyncio
async def test_missing_dispatcher_fails_loudly():
    """Definitions without a dispatcher is a caller bug worth shouting about.

    The model has already decided to call a tool; returning its empty text
    would read as a considered answer.
    """
    from runtime.errors import RuntimeExecutionError

    client = _FakeClient([
        _completion(tool_calls=[_tool_call("c1", "get_weather", "{}")]),
    ])
    runtime = OpenAICompatibleRuntime(_profile())
    with pytest.raises(RuntimeExecutionError, match="no tool_dispatch"):
        await _run(
            runtime,
            _request(tool_defs=[GET_WEATHER], capability=TOOL_REASONING),
            client,
        )


@pytest.mark.asyncio
async def test_a_failing_tool_is_conversational_not_fatal():
    """The model should SEE the error and get a chance to recover.

    Raising instead would turn a recoverable tool error into a failed turn and
    a lane fallback — the model never learns what went wrong.
    """
    client = _FakeClient([
        _completion(tool_calls=[_tool_call("c1", "get_weather", '{"city":"Nowhere"}')]),
        _completion(content="That city does not exist."),
    ])

    def dispatch(name, args):
        raise ValueError("unknown city")

    runtime = OpenAICompatibleRuntime(_profile())
    text, calls, _usage, messages = await _run(
        runtime,
        _request(tool_defs=[GET_WEATHER], capability=TOOL_REASONING, tool_dispatch=dispatch),
        client,
    )

    assert text == "That city does not exist."
    assert calls[0].status == "failed"
    body = json.loads([m for m in messages if m.get("role") == "tool"][0]["content"])
    assert "ValueError" in body["error"] and "unknown city" in body["error"]


@pytest.mark.asyncio
async def test_malformed_arguments_are_preserved_for_the_audit_trail():
    client = _FakeClient([
        _completion(tool_calls=[_tool_call("c1", "get_weather", "{not json")]),
        _completion(content="ok"),
    ])
    runtime = OpenAICompatibleRuntime(_profile())
    _text, calls, _usage, _messages = await _run(
        runtime,
        _request(
            tool_defs=[GET_WEATHER], capability=TOOL_REASONING, tool_dispatch=lambda n, a: "x"
        ),
        client,
    )
    assert calls[0].arguments == "{not json", "raw string must survive for the audit row"


@pytest.mark.asyncio
async def test_loop_is_bounded_and_returns_rather_than_hanging(monkeypatch):
    """A model that calls tools forever must not spin forever."""
    monkeypatch.setenv("SECOND_BRAIN_GENERIC_TOOL_MAX_ITERATIONS", "3")
    client = _FakeClient([
        _completion(content=f"round {i}", tool_calls=[_tool_call(f"c{i}", "get_weather", "{}")])
        for i in range(10)
    ])
    runtime = OpenAICompatibleRuntime(_profile())
    text, calls, _usage, _messages = await _run(
        runtime,
        _request(
            tool_defs=[GET_WEATHER], capability=TOOL_REASONING, tool_dispatch=lambda n, a: "x"
        ),
        client,
    )
    assert len(client.requests) == 3, "iteration bound not honored"
    assert len(calls) == 3
    assert text == "round 2", "the last assistant text should still come back"


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_sums_across_round_trips():
    """A tool turn is several calls; reporting only the last under-reports it."""
    client = _FakeClient([
        _completion(
            tool_calls=[_tool_call("c1", "get_weather", "{}")],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10, total_tokens=110),
        ),
        _completion(
            content="done",
            usage=SimpleNamespace(prompt_tokens=150, completion_tokens=20, total_tokens=170),
        ),
    ])
    runtime = OpenAICompatibleRuntime(_profile())
    _text, _calls, usage, _messages = await _run(
        runtime,
        _request(
            tool_defs=[GET_WEATHER], capability=TOOL_REASONING, tool_dispatch=lambda n, a: "x"
        ),
        client,
    )
    assert usage["prompt_tokens"] == 250
    assert usage["completion_tokens"] == 30
    assert usage["total_tokens"] == 280


@pytest.mark.asyncio
async def test_result_carries_the_normalized_tool_telemetry(monkeypatch):
    """tool_calls / tool_names_used / tool_call_count reach RuntimeResult.

    Runs through the real `run()` so the wiring between the loop and the
    result object is exercised, not just the loop in isolation.
    """
    client = _FakeClient([
        _completion(tool_calls=[
            _tool_call("c1", "get_weather", '{"city":"Oslo"}'),
            _tool_call("c2", "get_weather", '{"city":"Lima"}'),
        ]),
        _completion(content="both reported"),
    ])
    runtime = OpenAICompatibleRuntime(_profile())
    monkeypatch.setattr(
        "openai.AsyncOpenAI", lambda **kwargs: client, raising=False
    )

    result = await runtime.run(
        _request(
            tool_defs=[GET_WEATHER],
            capability=TOOL_REASONING,
            tool_dispatch=lambda n, a: "ok",
        )
    )

    assert result.text == "both reported"
    assert result.tool_call_count == 2
    assert result.tool_names_used == ["get_weather"], "names must be deduped and sorted"
    assert [c.id for c in result.tool_calls] == ["c1", "c2"]
    assert result.provider == "kimi"
