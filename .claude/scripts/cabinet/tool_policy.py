"""Cabinet tool / MCP policy.

PORTED FROM ClaudeClaw `src/warroom-tool-policy.ts:1-128` per PRD-8 §0a.

M1 Homie security delta — DEFAULT-DENY floor (stricter than upstream):
when persona's `cabinet.tools` is missing OR explicitly empty (`[]`),
`cabinet_tool_policy()` resolves `allowed_tools=[]`. Side-effect tools
require explicit YAML opt-in via `cabinet.tools=[...]` enumeration.
Upstream default-allows SAFE_READONLY_TOOLS even when the agent's
allowlist is empty; The Homie does NOT.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# Built-in tool names from the Anthropic Agent SDK. Match warroom-tool-policy.ts:41-48.
SAFE_READONLY_TOOLS: Final[tuple[str, ...]] = (
    "Read",
    "Glob",
    "Grep",
    "WebSearch",
    "WebFetch",
    "TodoWrite",
)

# Side-effect built-ins. Match warroom-tool-policy.ts:50-60.
SIDE_EFFECT_TOOLS: Final[tuple[str, ...]] = (
    "Bash",
    "Write",
    "Edit",
    "NotebookEdit",
    "ExitPlanMode",
    "Skill",
)

# Per-default-agent overrides. Match warroom-tool-policy.ts:64-79.
# Q4 lock — Python uses canonical "default" id; upstream's "main" key is
# translated by Hono boundary (translate.ts inboundPersonaId).
DEFAULT_AGENT_ALLOWLISTS: Final[dict[str, list[str]]] = {
    "default": [],
    "ops": ["Bash", "Skill"],
    "comms": ["Bash", "Skill"],
    "content": ["Skill", "Write"],
    "research": [],
}


@dataclass(frozen=True)
class CabinetToolPolicy:
    """Port warroom-tool-policy.ts:29-36 WarRoomToolPolicy verbatim."""

    allowed_tools: list[str]
    disallowed_tools: list[str]
    allowed_mcp_servers: list[str]


def cabinet_tool_policy(
    persona_id: str,
    persona_tools: list[str] | None = None,
) -> CabinetToolPolicy:
    """Build the tool/MCP policy for a given persona in the cabinet.

    Port of warroom-tool-policy.ts:86-112 with M1 Homie default-deny delta.

    Args:
        persona_id: persona slug (e.g. 'default', 'ops', 'comms').
            Canonical "default" matches `_MAIN_AGENT.id` per Q4 lock; the
            "main" alias is a browser-only convention translated at the
            Hono boundary (translate.ts inboundPersonaId).
        persona_tools: result of `persona.cabinet.tools` from config.yaml.
            * `None` → no `cabinet.tools` section in YAML → DEFAULT-DENY.
            * `[]`   → explicitly empty list → DEFAULT-DENY.
            * `[...names...]` → operator opted in to those tools.

    Returns:
        CabinetToolPolicy with allowed_tools, disallowed_tools, and
        allowed_mcp_servers. `mcp:` prefix entries in `persona_tools`
        opt agents into MCP servers.

    M1 — Homie default-deny floor:
        Upstream returns SAFE_READONLY_TOOLS when overrides is None/empty.
        The Homie returns `allowed_tools=[]` so a misconfigured persona
        with no `cabinet.tools` block has ZERO tool surface in the
        cabinet. Operator must affirmatively opt in via YAML.

    Rule 1 enforcement: `persona_tools=None` is the canonical sentinel
    — resolved at body-time inside the function. NEVER bind a config
    constant as the default.
    """
    # M1 Homie default-deny: empty overrides → empty allowlist (NO
    # SAFE_READONLY_TOOLS auto-grant). Stricter than upstream.
    has_overrides = persona_tools is not None and len(persona_tools) > 0

    if has_overrides:
        # Operator opted in explicitly. Pull non-mcp: entries into
        # `allowed_tools` (in addition to SAFE_READONLY_TOOLS — upstream
        # contract). Per upstream warroom-tool-policy.ts:91-92, the user's
        # opt-in extras combine with SAFE_READONLY_TOOLS.
        # `persona_tools` is non-None and non-empty in this branch — narrow
        # for type-checkers.
        assert persona_tools is not None  # noqa: S101 — branch invariant
        non_mcp = [t for t in persona_tools if not t.startswith("mcp:")]
        allowed_set: dict[str, None] = {}
        for t in SAFE_READONLY_TOOLS:
            allowed_set[t] = None
        for t in non_mcp:
            allowed_set[t] = None
        allowed = list(allowed_set.keys())
    else:
        # M1 — Homie default-deny. Empty allowed list when no operator opt-in.
        allowed = []

    # Disallow EVERY side-effect tool the persona didn't explicitly opt into.
    # Defense-in-depth — even if `allowed_tools=[]` (which the SDK treats
    # as an explicit empty allow-set), list dangerous tools so a future
    # change that flips to a non-empty allowlist still has a floor.
    disallowed = [t for t in SIDE_EFFECT_TOOLS if t not in allowed]

    # MCP servers default to none. `mcp:<name>` entries opt agents in.
    if persona_tools is None:
        mcp_servers: list[str] = []
    else:
        mcp_servers = [
            t[len("mcp:"):]
            for t in persona_tools
            if t.startswith("mcp:") and len(t) > len("mcp:")
        ]

    return CabinetToolPolicy(
        allowed_tools=allowed,
        disallowed_tools=disallowed,
        allowed_mcp_servers=mcp_servers,
    )


def filter_mcp_servers[T](
    servers: dict[str, T],
    policy: CabinetToolPolicy,
) -> dict[str, T]:
    """Port warroom-tool-policy.ts:118-128 verbatim.

    Filter a map of MCP servers to only those the policy permits.
    `policy.allowed_mcp_servers=[]` → empty result (default-deny floor).
    """
    if not policy.allowed_mcp_servers:
        return {}
    return {
        name: cfg
        for name, cfg in servers.items()
        if name in policy.allowed_mcp_servers
    }


__all__ = [
    "CabinetToolPolicy",
    "DEFAULT_AGENT_ALLOWLISTS",
    "SAFE_READONLY_TOOLS",
    "SIDE_EFFECT_TOOLS",
    "cabinet_tool_policy",
    "filter_mcp_servers",
]
