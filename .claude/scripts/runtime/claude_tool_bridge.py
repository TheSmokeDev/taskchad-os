"""Present caller-supplied tool definitions to the Claude Agent SDK.

The generic lane speaks OpenAI-format `tools=[...]` on the wire. The Claude
Agent SDK does not take a tools array at all — it runs its own agentic loop and
discovers tools through MCP. So "the same tool works on both lanes" is a
TRANSLATION problem, not a second implementation:

    OpenAI-format tool_defs  ->  in-process SDK MCP server  ->  tool_dispatch

`create_sdk_mcp_server` runs the server inside this process (no subprocess, no
IPC), so a handler is a plain Python call into the same
``request.tool_dispatch`` the generic lane uses. That is what keeps the ONE
chokepoint honest across lanes: guardrails, the kill switch, and the audit row
(#242) fire in exactly one place regardless of which provider served the turn.

The alternative — teaching the SDK path its own execution route — would have
produced two places to forget a guardrail and two behaviors to keep in sync.
A persona whose tool works differently depending on which lane happened to be
healthy is not lane-agnostic; it is two products.

Namespacing: the SDK exposes an SDK MCP tool as ``mcp__<server>__<tool>``, so
the model never sees the bare name. Callers keep using bare names everywhere
(registry, tool_defs, dispatch) and this module owns the mapping in both
directions.
"""

from __future__ import annotations

import json
import logging
from typing import Any

_logger = logging.getLogger(__name__)

# Server name is part of the tool namespace the model sees, so it is a contract,
# not a label. Changing it renames every tool mid-conversation.
TOOL_SERVER_NAME = "homie"


def namespaced_tool_name(bare_name: str) -> str:
    """Bare registry name -> the name the SDK exposes to the model."""
    return f"mcp__{TOOL_SERVER_NAME}__{bare_name}"


def build_tool_server(request: Any) -> tuple[Any, list[str]]:
    """Build an in-process MCP server from ``request.tool_defs``.

    Returns ``(server_config, namespaced_tool_names)``. The names must be added
    to ``allowed_tools`` — the SDK will not offer a tool the turn has not
    allowed, so skipping that step produces a server the model can see and
    never call, which reads exactly like a tool that does not work.

    Raises:
        ValueError: if the request carries definitions but no dispatcher. The
            model would otherwise be offered tools that cannot execute — the
            polite-drop shape this epic exists to eliminate.
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    tool_defs = list(getattr(request, "tool_defs", None) or [])
    dispatch = getattr(request, "tool_dispatch", None)
    if not tool_defs:
        raise ValueError("build_tool_server called with no tool_defs")
    if dispatch is None:
        raise ValueError(
            "request carries tool_defs but no tool_dispatch — the model would be "
            "offered tools that cannot execute"
        )

    sdk_tools: list[Any] = []
    names: list[str] = []

    for definition in tool_defs:
        fn = (definition.get("function") or {}) if isinstance(definition, dict) else {}
        name = fn.get("name") or ""
        if not name:
            _logger.warning("skipping a tool definition with no function name")
            continue
        description = fn.get("description") or name
        parameters = fn.get("parameters") or {"type": "object", "properties": {}}

        sdk_tools.append(_make_sdk_tool(tool, name, description, parameters, dispatch))
        names.append(namespaced_tool_name(name))

    if not sdk_tools:
        raise ValueError("no usable tool definitions after filtering")

    server = create_sdk_mcp_server(name=TOOL_SERVER_NAME, tools=sdk_tools)
    return server, names


def _make_sdk_tool(
    tool_decorator: Any,
    name: str,
    description: str,
    parameters: dict[str, Any],
    dispatch: Any,
) -> Any:
    """Wrap one definition as an SDK tool that calls back into ``dispatch``.

    A closure per tool rather than one generic handler: the SDK identifies a
    tool by the decorated function, so a shared handler could not tell which
    tool it was serving. ``name`` is bound here so the dispatcher receives the
    BARE name, matching the generic lane and the registry.
    """

    @tool_decorator(name, description, parameters)
    async def _handler(args: dict[str, Any]) -> dict[str, Any]:
        import inspect

        try:
            outcome = dispatch(name, args or {})
            if inspect.isawaitable(outcome):
                outcome = await outcome
            text = outcome if isinstance(outcome, str) else json.dumps(outcome, default=str)
            return {"content": [{"type": "text", "text": text}]}
        except Exception as exc:  # noqa: BLE001
            # Surfaced to the MODEL as a tool result, not raised into the SDK
            # loop. A failing tool is conversational input — the model should
            # see the error and get a chance to recover, which is exactly how
            # the generic lane behaves. Raising here would abort the turn and
            # trigger a lane fallback, and the model would never learn why.
            _logger.warning("tool %r raised during SDK dispatch: %s", name, exc)
            return {
                "content": [
                    {"type": "text", "text": json.dumps({"error": f"{type(exc).__name__}: {exc}"})}
                ],
                "isError": True,
            }

    return _handler


__all__ = ["TOOL_SERVER_NAME", "build_tool_server", "namespaced_tool_name"]
