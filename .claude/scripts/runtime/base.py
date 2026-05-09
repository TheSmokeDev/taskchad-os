"""Core runtime request / result types."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .capabilities import TEXT_REASONING

RUNTIME_LANE_CLAUDE_NATIVE = "claude_native"
RUNTIME_LANE_GENERIC = "generic_runtime"


@dataclass(slots=True)
class RuntimeRequest:
    """Normalized runtime request for background jobs and chat flows.

    PRD-8 Phase 5a / WS1.0 (NB2): four additive fields for the cabinet
    port — `disallowed_tools` and `mcp_servers` thread tool-policy results
    from `cabinet.tool_policy.cabinet_tool_policy()` into the SDK options
    dict (forwarded by `runtime/claude_sdk.py:183-214`); `metadata` and
    `auth_profile` are lane-router/Langfuse routing context (NOT forwarded
    to SDK options). All four default to `None` so existing 19 fields and
    every existing caller keep working unchanged.
    """

    prompt: str
    cwd: Path | str
    task_name: str
    capability: str = TEXT_REASONING
    model: str | None = None
    fallback_model: str | None = None
    max_turns: int = 1
    max_budget_usd: float | None = None
    allowed_tools: list[str] = field(default_factory=list)
    permission_mode: str | None = None
    setting_sources: list[str] = field(default_factory=list)
    system_prompt: dict[str, Any] | str | None = None
    hooks: dict[str, Any] | None = None
    thinking: dict[str, Any] | None = None
    env: dict[str, str] | None = None
    resume: str | None = None
    stderr: Any | None = None
    allow_fallback: bool = True
    runtime_lane: str | None = None
    # PRD-8 Phase 5a / WS1.0 (NB2) — cabinet tool-policy + persona auth.
    disallowed_tools: list[str] | None = None
    mcp_servers: list[str] | None = None
    metadata: dict[str, Any] | None = None
    auth_profile: str | None = None


@dataclass(slots=True)
class RuntimeToolCall:
    """Normalized tool-call record across providers."""

    id: str = ""
    name: str = ""
    arguments: dict[str, Any] | str | None = None
    provider_type: str | None = None
    status: str | None = None


@dataclass(slots=True)
class RuntimeResult:
    """Normalized runtime result."""

    text: str
    runtime_lane: str
    provider: str
    model: str
    profile_key: str | None = None
    session_id: str | None = None
    cost_usd: float | None = None
    subtype: str | None = None
    tool_call_count: int = 0
    tool_names_used: list[str] = field(default_factory=list)
    tool_calls: list[RuntimeToolCall] = field(default_factory=list)
