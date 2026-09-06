"""Core runtime request / result types."""

from __future__ import annotations

from contextvars import ContextVar

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .capabilities import TEXT_REASONING

RUNTIME_LANE_CLAUDE_NATIVE = "claude_native"
RUNTIME_LANE_GENERIC = "generic_runtime"


# The provider attempt follows copied async/thread context even when a cancelled
# adapter leaves a host tool finishing after fallback has begun.
_ATTEMPT_CONTEXT: ContextVar[dict | None] = ContextVar("runtime_attempt", default=None)


def current_runtime_attempt() -> dict:
    return dict(_ATTEMPT_CONTEXT.get() or {})


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
    # Explicit per-request provider selection for paired learning evaluation.
    # Credential selection remains auth_profile; ordinary callers keep ambient
    # lane selection. A pinned request model belongs only to this provider.
    preferred_provider: str | None = None
    # User-facing conversational turn (cabinet personas, chat replies). When True,
    # the CLI prompt builder uses an in-character preamble instead of the backstage
    # "safe text-only reasoning task" framing, so the homie never narrates the
    # runtime/lanes/tools to the user. Provider-agnostic (Codex + Gemini share the
    # builder); ignored on the claude_native lane, which has no such preamble.
    conversational: bool = False
    # Scheduling only; never selects a lane or changes tool authority. Existing
    # conversational callers count as foreground automatically.
    workload: str = "auto"  # auto | foreground | background | learning
    # Homie Mobile M7 — per-message cockpit controls. `effort` maps to the SDK
    # options `effort` knob (low|medium|high|xhigh|max) on the claude_native lane;
    # generic lanes ignore it. `on_tool_event` is a fail-open callback the
    # claude_sdk message loop invokes per streamed ToolUseBlock
    # ({id, name, input_preview}); it is consumed by the runtime loop itself,
    # never forwarded into SDK options, and generic lanes emit no live events.
    effort: str | None = None
    on_tool_event: Any | None = None
    # Read-only multimodal work (for example `/watch` frame inspection).
    # Generic CLI adapters use these fields to attach images without granting
    # write/shell authority; Claude keeps only its Read tool.  Additive defaults
    # preserve every existing caller.
    image_paths: list[Path | str] = field(default_factory=list)
    read_only_tools: bool = False
    # Approved local-file application lane. Adapters must contain this more
    # tightly than ordinary TOOL_REASONING (workspace-write / edit tools only).
    workspace_write_tools: bool = False
    # Strict model-only reasoning. The lane router admits only adapters that
    # explicitly prove they can remove every provider-owned tool surface.
    # This is stronger than `allowed_tools=[]`: several CLIs interpret an
    # empty allowlist as "use defaults." Curriculum study uses this for
    # untrusted transcripts so quota fallback cannot silently grant shell,
    # filesystem, MCP, extension, hook, or browser authority.
    model_only: bool = False
    # Epic #236 — caller-supplied tools (persona tool calling).
    #
    # `tool_defs` carries OpenAI-format definitions:
    #     [{"type": "function", "function": {name, description, parameters}}]
    # This is the wire format on purpose. Portability comes from the FORMAT,
    # not from a vendor SDK — the identical dict produced a structured call on
    # Kimi K3 and was ignored outright by the Codex CLI, and neither outcome
    # involved an SDK-specific field.
    #
    # These are NOT `allowed_tools`. That field names built-ins the provider
    # already owns; a non-empty value there makes `openai_compatible.supports()`
    # return False and silently pins the request to Claude. `tool_defs` is the
    # opposite: definitions the CALLER supplies and the CALLER executes.
    #
    # `tool_dispatch` is the single execution chokepoint: `(name, arguments)`
    # -> result string. Modeled as ONE callable rather than a dict of handlers
    # so there is structurally nowhere else for a tool call to be executed.
    # Two execution paths means two places to forget a guardrail — the bridge
    # tools in #245 must land here too, not beside it.
    #
    # Both default to None, so every existing caller and all 23 fields above
    # are byte-identical unchanged.
    tool_defs: list[dict[str, Any]] | None = None
    tool_dispatch: Callable[..., Any] | None = None
    tool_scope_version: str | None = None
    # Host callback, excluded from prompt/metadata serialization.
    attempt_observer: Callable[..., Any] | None = None


def assert_model_only_contract(request: RuntimeRequest) -> None:
    """Reject contradictory authority on a strict model-only request."""
    if not request.model_only:
        return
    violations: list[str] = []
    if request.capability != TEXT_REASONING:
        violations.append("capability must be text_reasoning")
    if request.allowed_tools:
        violations.append("allowed_tools must be empty")
    if request.disallowed_tools != ["*"]:
        violations.append("disallowed_tools must be ['*']")
    if request_carries_tools(request):
        violations.append("tool_defs must be empty")
    if request.mcp_servers:
        violations.append("mcp_servers must be empty")
    if request.hooks:
        violations.append("hooks must be empty")
    if request.setting_sources:
        violations.append("setting_sources must be empty")
    if request.read_only_tools:
        violations.append("read_only_tools must be false")
    if request.workspace_write_tools:
        violations.append("workspace_write_tools must be false")
    if violations:
        raise ValueError(
            "model_only runtime request violates the zero-tool contract: " + "; ".join(violations)
        )


def request_carries_tools(request: RuntimeRequest) -> bool:
    """True when this request supplies its own tool definitions.

    This is the ONLY definition of "a tool turn" for routing purposes, and it
    is deliberately narrow: it keys off `tool_defs` being non-empty, NOT off
    `capability == TOOL_REASONING`.

    The distinction matters. `TOOL_REASONING` means "this request may use
    tools", which for the CLI lanes means *their own* shell and edit tools —
    Codex and Gemini both serve those turns perfectly well and must keep doing
    so. Keying the routing exclusion off the capability tier instead would
    strip both CLI lanes from every existing tool turn in the framework and
    collapse the fallback chain to Claude alone, which is the exact failure
    this epic exists to prevent, inverted.

    Uses an explicit length check rather than truthiness: a list SUBCLASS may
    override ``__bool__`` and report falsey while holding entries, which would
    classify a genuine tool turn as a plain one and route it to a lane that
    drops the definitions (adversarial review, Codex).
    """
    defs = request.tool_defs
    if defs is None:
        return False
    try:
        return len(defs) > 0
    except TypeError:
        # Not sized — treat any non-None value as carrying. Fail CLOSED: the
        # request claims to have tools, so it must not be handed to a lane that
        # would silently ignore them.
        return True


def assert_tool_defs_are_registered(request: RuntimeRequest) -> None:
    """Refuse tool definitions that did not come from the tool registry.

    THE BYPASS THIS CLOSES (adversarial review, Codex — BLOCKER):
    ``tool_registry`` enforces "all tools must be part of a toolset to be
    accessible" by only ever emitting names a toolset resolved to. But
    ``tool_defs`` is a plain ``list[dict]`` on the request, so any caller could
    hand-assemble a schema for an unregistered — or deliberately out-of-scope —
    tool and hand it straight to a provider, walking around the registry
    entirely. Correct assembly in the persona layer would then be *convention*,
    not "default-deny by construction", and the difference between those two is
    the whole security model.

    So provenance is checked HERE, at the runtime boundary every lane crosses,
    rather than trusting each caller to have used the registry.

    What this does and does not prove:

    * It proves every carried name is REGISTERED. That is a provenance check.
    * It does NOT prove the name is in scope for a particular persona — the
      request does not carry a persona identity, and inventing one here would
      duplicate scoping logic that belongs in the assembly layer (#244).
      Scope stays where the toolsets are resolved; this is the backstop that
      makes an unregistered tool unreachable no matter who assembled it.

    Fails OPEN only when the registry module itself is unavailable (an adopter
    running the runtime slice without the registry). It never fails open on a
    name it could not find — that is the case it exists to catch.
    """
    if not request_carries_tools(request):
        return

    try:
        from runtime import tool_registry
    except ImportError:
        # Registry slice absent — nothing to validate against. Deliberate: the
        # runtime must remain usable without it.
        return

    unregistered: list[str] = []
    schema_mismatches: list[str] = []
    for definition in request.tool_defs or []:
        name = ""
        if isinstance(definition, dict):
            name = ((definition.get("function") or {}) or {}).get("name", "")
        entry = tool_registry.get_entry(name) if name else None
        if entry is None:
            unregistered.append(name or "<unnamed>")
        elif definition != entry.schema:
            # A registered NAME is not provenance for a caller-authored schema.
            # Without exact equality, a caller can reuse an allowed name while
            # changing its description/arguments to widen what the model sees.
            schema_mismatches.append(name)

    if unregistered:
        raise ValueError(
            "tool_defs contains definitions that did not come from the tool "
            f"registry: {', '.join(sorted(unregistered))}. Build the array with "
            "tool_registry.get_tool_definitions(<toolsets>) — a hand-assembled "
            "schema bypasses toolset scoping entirely."
        )
    if schema_mismatches:
        raise ValueError(
            "tool_defs contains schemas that do not exactly match the immutable "
            "registry snapshot: "
            f"{', '.join(sorted(schema_mismatches))}. Registered-name-only "
            "provenance is insufficient."
        )


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
    usage: dict[str, int] | None = None
    execution_time_ms: int | None = None
