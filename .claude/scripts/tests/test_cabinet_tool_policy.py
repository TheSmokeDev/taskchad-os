"""Test PRD-8 Phase 5a / WS1.3 — cabinet tool_policy port + M1 default-deny delta.

M1 — Homie default-deny delta: missing/empty `cabinet.tools` →
`allowed_tools=[]`. Stricter than upstream (which default-allows
SAFE_READONLY_TOOLS). Side-effect tools require explicit YAML opt-in.
"""
from __future__ import annotations

import pytest

from cabinet.tool_policy import (
    DEFAULT_AGENT_ALLOWLISTS,
    SAFE_READONLY_TOOLS,
    SIDE_EFFECT_TOOLS,
    cabinet_tool_policy,
    filter_mcp_servers,
)


def test_safe_readonly_tools_set() -> None:
    """Verbatim port of warroom-tool-policy.ts:41-48."""
    assert set(SAFE_READONLY_TOOLS) == {"Read", "Glob", "Grep", "WebSearch", "WebFetch", "TodoWrite"}


def test_side_effect_tools_set() -> None:
    """Verbatim port of warroom-tool-policy.ts:50-60."""
    assert set(SIDE_EFFECT_TOOLS) == {"Bash", "Write", "Edit", "NotebookEdit", "ExitPlanMode", "Skill"}


def test_default_agent_allowlists() -> None:
    """Verbatim port of warroom-tool-policy.ts:64-79.

    Q4 lock — Python uses canonical "default" id; upstream's "main" key is
    translated by Hono boundary (translate.ts inboundPersonaId).
    """
    assert DEFAULT_AGENT_ALLOWLISTS["default"] == []
    assert DEFAULT_AGENT_ALLOWLISTS["ops"] == ["Bash", "Skill"]
    assert DEFAULT_AGENT_ALLOWLISTS["comms"] == ["Bash", "Skill"]
    assert DEFAULT_AGENT_ALLOWLISTS["content"] == ["Skill", "Write"]
    assert DEFAULT_AGENT_ALLOWLISTS["research"] == []


# ── M1 Homie default-deny delta ──────────────────────────────────────────

def test_m1_missing_cabinet_tools_yields_empty_allowed() -> None:
    """M1 — `persona_tools=None` → allowed_tools=[]. Stricter than upstream."""
    p = cabinet_tool_policy("default", persona_tools=None)
    assert p.allowed_tools == []
    assert p.allowed_mcp_servers == []
    # disallowed list still contains all SIDE_EFFECT_TOOLS (defense in depth).
    assert set(p.disallowed_tools) == set(SIDE_EFFECT_TOOLS)


def test_m1_empty_cabinet_tools_yields_empty_allowed() -> None:
    """M1 — `persona_tools=[]` (explicitly empty) → allowed_tools=[]."""
    p = cabinet_tool_policy("ops", persona_tools=[])
    assert p.allowed_tools == []
    assert p.allowed_mcp_servers == []


def test_m1_explicit_opt_in_combines_with_safe_readonly() -> None:
    """Operator opt-in: explicit tools combine with SAFE_READONLY_TOOLS (upstream contract).

    The Homie diverges from upstream ONLY on the missing/empty case; the
    explicit-opt-in path matches upstream exactly per warroom-tool-policy.ts:91-92.
    """
    p = cabinet_tool_policy("ops", persona_tools=["Bash"])
    # SAFE_READONLY_TOOLS are auto-included once explicit opt-in is present.
    assert "Bash" in p.allowed_tools
    assert "Read" in p.allowed_tools
    assert "Glob" in p.allowed_tools
    # Bash explicitly allowed → not in disallowed list.
    assert "Bash" not in p.disallowed_tools
    assert "Write" in p.disallowed_tools


def test_mcp_prefix_opt_in_extracts_server_names() -> None:
    """`mcp:gmail` → allowed_mcp_servers=['gmail']. Plain entries ignored."""
    p = cabinet_tool_policy("comms", persona_tools=["Bash", "mcp:gmail", "mcp:slack"])
    assert p.allowed_mcp_servers == ["gmail", "slack"]
    # Bash present in allowed_tools (operator opt-in path).
    assert "Bash" in p.allowed_tools


def test_mcp_only_persona_still_blocks_side_effects() -> None:
    """A persona that only opts in MCPs still has side-effect tools disallowed."""
    p = cabinet_tool_policy("research", persona_tools=["mcp:asana"])
    assert "Write" in p.disallowed_tools
    assert "Bash" in p.disallowed_tools
    # SAFE_READONLY still auto-included.
    assert "Read" in p.allowed_tools


def test_filter_mcp_servers_drops_unallowed() -> None:
    p = cabinet_tool_policy("comms", persona_tools=["mcp:gmail"])
    servers = {"gmail": {"cmd": "gmail-mcp"}, "slack": {"cmd": "slack-mcp"}}
    filtered = filter_mcp_servers(servers, p)
    assert filtered == {"gmail": {"cmd": "gmail-mcp"}}


def test_filter_mcp_servers_empty_when_no_mcp_opt_in() -> None:
    """Default-deny floor — empty MCP allowlist → empty result, regardless of input."""
    p = cabinet_tool_policy("default", persona_tools=None)
    filtered = filter_mcp_servers({"gmail": {}, "slack": {}}, p)
    assert filtered == {}


def test_policy_shape_three_lists() -> None:
    """Mirror upstream WarRoomToolPolicy shape: allowed_tools, disallowed_tools, allowed_mcp_servers."""
    p = cabinet_tool_policy("ops", persona_tools=["Bash"])
    assert isinstance(p.allowed_tools, list)
    assert isinstance(p.disallowed_tools, list)
    assert isinstance(p.allowed_mcp_servers, list)
