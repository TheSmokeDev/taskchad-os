"""Tests for caller-tool execution on the claude_native lane (#240).

The property: **the same registry tool runs on both lanes.** A persona whose
tool behaves differently depending on which lane happened to be healthy is not
lane-agnostic; it is two products, and a mid-conversation fallback silently
changes what it can do.

The generic lane speaks OpenAI `tools=[...]`; the Claude Agent SDK takes no
tools array at all and discovers tools through MCP. So parity is a TRANSLATION
(`claude_tool_bridge`), not a second execution path — handlers call back into
the same `request.tool_dispatch`, keeping one chokepoint across lanes.

Live proof recorded 2026-07-27, same tool + same dispatcher on both lanes:
  Kimi K3       -> dispatched [('get_weather', {'city': 'Reykjavik'})]
  claude_native -> dispatched [('get_weather', {'city': 'Reykjavik'})]
                   tool_names_used ['ToolSearch', 'mcp__homie__get_weather']
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from runtime import claude_tool_bridge  # noqa: E402
from runtime.base import RuntimeRequest  # noqa: E402
from runtime.capabilities import TOOL_REASONING  # noqa: E402
from runtime.claude_sdk import ClaudeSdkRuntime  # noqa: E402
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


def _profile():
    """`run()` reads `self.profile.model` before options are built."""
    return RuntimeProfile(key="claude-test", provider="claude", model="claude-sonnet-5")


def _request(**kw):
    base = {
        "prompt": "temperature in Reykjavik?",
        "cwd": Path.cwd(),
        "task_name": "test_turn",
        "capability": TOOL_REASONING,
    }
    base.update(kw)
    return RuntimeRequest(**base)


async def _invoke(sdk_tool, args):
    """Call an SdkMcpTool's handler regardless of where the SDK stores it."""
    handler = getattr(sdk_tool, "handler", None) or getattr(sdk_tool, "func", None)
    assert handler is not None, f"cannot locate handler on {sdk_tool!r}"
    return await handler(args)


def _text_of(result):
    return result["content"][0]["text"]


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------


def test_claude_lane_declares_it_carries_caller_tools():
    assert ClaudeSdkRuntime(profile=None).supports_caller_tool_defs() is True


def test_claude_lane_accepts_tool_carrying_requests():
    runtime = ClaudeSdkRuntime(profile=None)
    assert runtime.supports(_request(tool_defs=[GET_WEATHER])) is True


# ---------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------


def test_tools_are_namespaced_for_the_model_but_dispatch_stays_bare():
    """The SDK exposes an SDK MCP tool as mcp__<server>__<tool>.

    Callers use bare names everywhere (registry, tool_defs, dispatch); the
    bridge owns the mapping in BOTH directions. If the bare name leaked into
    allowed_tools the SDK would never offer the tool; if the namespaced name
    reached the dispatcher, the registry lookup would miss.
    """
    _server, names = claude_tool_bridge.build_tool_server(
        _request(tool_defs=[GET_WEATHER], tool_dispatch=lambda n, a: "x")
    )
    assert names == ["mcp__homie__get_weather"]
    assert claude_tool_bridge.namespaced_tool_name("get_weather") == "mcp__homie__get_weather"


@pytest.mark.asyncio
async def test_handler_dispatches_with_the_BARE_name():
    seen = []
    from claude_agent_sdk import tool

    sdk_tool = claude_tool_bridge._make_sdk_tool(
        tool, "get_weather", "desc", GET_WEATHER["function"]["parameters"],
        lambda n, a: seen.append((n, a)) or "2C",
    )
    result = await _invoke(sdk_tool, {"city": "Reykjavik"})

    assert seen == [("get_weather", {"city": "Reykjavik"})], (
        "dispatcher must receive the bare name — the registry has no namespaced entry"
    )
    assert _text_of(result) == "2C"


@pytest.mark.asyncio
async def test_async_dispatchers_are_awaited():
    from claude_agent_sdk import tool

    async def dispatch(name, args):
        return "-1C"

    sdk_tool = claude_tool_bridge._make_sdk_tool(
        tool, "get_weather", "d", GET_WEATHER["function"]["parameters"], dispatch
    )
    assert _text_of(await _invoke(sdk_tool, {"city": "Oslo"})) == "-1C"


@pytest.mark.asyncio
async def test_a_failing_tool_is_returned_to_the_model_not_raised():
    """Same contract as the generic lane: a tool error is conversational.

    Raising into the SDK loop would abort the turn and trigger a lane
    fallback — and the model would never learn what went wrong, so it could
    not recover or explain.
    """
    from claude_agent_sdk import tool

    def dispatch(name, args):
        raise ValueError("unknown city")

    sdk_tool = claude_tool_bridge._make_sdk_tool(
        tool, "get_weather", "d", GET_WEATHER["function"]["parameters"], dispatch
    )
    result = await _invoke(sdk_tool, {"city": "Nowhere"})

    assert result.get("isError") is True
    body = json.loads(_text_of(result))
    assert "ValueError" in body["error"] and "unknown city" in body["error"]


@pytest.mark.asyncio
async def test_non_string_results_are_json_encoded():
    from claude_agent_sdk import tool

    sdk_tool = claude_tool_bridge._make_sdk_tool(
        tool, "get_weather", "d", GET_WEATHER["function"]["parameters"],
        lambda n, a: {"temp_c": 2, "conditions": "snow"},
    )
    assert json.loads(_text_of(await _invoke(sdk_tool, {}))) == {
        "temp_c": 2, "conditions": "snow"
    }


def test_definitions_without_a_dispatcher_are_refused():
    """Offering tools that cannot execute is the polite-drop shape."""
    with pytest.raises(ValueError, match="no tool_dispatch"):
        claude_tool_bridge.build_tool_server(_request(tool_defs=[GET_WEATHER]))


def test_unnamed_definitions_are_skipped_and_an_all_bad_set_is_refused():
    nameless = {"type": "function", "function": {"description": "no name"}}
    with pytest.raises(ValueError, match="no usable tool definitions"):
        claude_tool_bridge.build_tool_server(
            _request(tool_defs=[nameless], tool_dispatch=lambda n, a: "x")
        )


# ---------------------------------------------------------------------------
# The wiring — options actually handed to the SDK
# ---------------------------------------------------------------------------


class _OptionsCapture:
    """Capture ClaudeAgentOptions without running a real query."""

    def __init__(self):
        self.kwargs = None

    def install(self, monkeypatch):
        import claude_agent_sdk

        captured = self

        class _FakeOptions:
            def __init__(self, **kwargs):
                captured.kwargs = kwargs

        async def _fake_query(prompt, options):
            return
            yield  # pragma: no cover — makes this an async generator

        monkeypatch.setattr(claude_agent_sdk, "ClaudeAgentOptions", _FakeOptions)
        monkeypatch.setattr(claude_agent_sdk, "query", _fake_query)


@pytest.mark.asyncio
async def test_namespaced_names_reach_allowed_tools(monkeypatch):
    """The SDK will not offer a tool the turn has not ALLOWED.

    Building the server without extending allowed_tools yields a server the
    model can see and never call — which reads exactly like a tool that does
    not work.
    """
    capture = _OptionsCapture()
    capture.install(monkeypatch)

    runtime = ClaudeSdkRuntime(_profile())
    with contextlib.suppress(Exception):
        await runtime.run(_request(tool_defs=[GET_WEATHER], tool_dispatch=lambda n, a: "x"))

    assert capture.kwargs is not None, "options were never constructed"
    assert "mcp__homie__get_weather" in capture.kwargs["allowed_tools"]
    assert claude_tool_bridge.TOOL_SERVER_NAME in capture.kwargs["mcp_servers"]


@pytest.mark.asyncio
async def test_default_deny_marker_does_not_strip_the_caller_tool_server(monkeypatch):
    """The trap: `tools: []` advertises NOTHING.

    A cabinet persona carries `disallowed_tools=["*"]` with empty
    `allowed_tools` — the default-deny floor. That branch sets `tools: []`,
    which would strip the very MCP tools the request exists to offer, and the
    persona would look tool-less for reasons nothing in the config explains.

    The floor still holds: `allowed_tools` lists ONLY the caller's namespaced
    tools, so no built-ins are advertised.
    """
    capture = _OptionsCapture()
    capture.install(monkeypatch)

    runtime = ClaudeSdkRuntime(_profile())
    with contextlib.suppress(Exception):
        await runtime.run(
            _request(
                tool_defs=[GET_WEATHER],
                tool_dispatch=lambda n, a: "x",
                allowed_tools=[],
                disallowed_tools=["*"],
            )
        )

    assert capture.kwargs.get("tools") != [], (
        "tools: [] stripped the caller tool server for a default-deny persona"
    )
    assert capture.kwargs["allowed_tools"] == ["mcp__homie__get_weather"], (
        "the default-deny floor must still admit ONLY the caller's tools"
    )


@pytest.mark.asyncio
async def test_ordinary_turns_are_untouched(monkeypatch):
    """No caller tools -> no server, no namespaced names, `tools: []` intact."""
    capture = _OptionsCapture()
    capture.install(monkeypatch)

    runtime = ClaudeSdkRuntime(_profile())
    with contextlib.suppress(Exception):
        await runtime.run(_request(allowed_tools=[], disallowed_tools=["*"]))

    assert capture.kwargs.get("tools") == [], "default-deny behavior regressed"
    assert capture.kwargs["allowed_tools"] == []
