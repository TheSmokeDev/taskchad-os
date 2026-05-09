"""Cabinet text router and intervention gate.

PORTED FROM ClaudeClaw `src/warroom-text-router.ts:1-343` per PRD-8 §0a.

Both functions issue a locked-down lane-router-dispatched RuntimeRequest
(NEVER a direct provider client call — B1 lock). The Haiku classifier runs
with `allowed_tools=[]` and `disallowed_tools=['*']` so a prompt-injected
roster description can't drive tools.

M7 kill-switch chain — TWO LAYERS:
  1. `kill_switches.requireEnabled('cabinet')` — feature gate (explicit).
  2. lane_router's `kill_switches.requireEnabled('llm')` — model gate
     (automatic via `runtime/lane_router.py:90-94`).
Both must be enabled for a router/gate call to execute.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from runtime import lane_router
from runtime.base import RuntimeRequest
from runtime.capabilities import TEXT_REASONING
from security import kill_switches

logger = logging.getLogger(__name__)

# PRD-8 Phase 7b WS1 (codex post-build F1) — log-message redaction at every
# cabinet log emit site. Module-attribute import (Rule 3); redact() unconditional.
from security import redact as _redact_mod  # noqa: E402
_redact = _redact_mod.redact


# Match warroom-text-router.ts:21-26.
ROUTER_MODEL: Final[str] = "claude-haiku-4-5-20251001"
ROUTER_TIMEOUT_S: Final[float] = 20.0
GATE_TIMEOUT_S: Final[float] = 25.0


@dataclass
class RosterAgentLite:
    """Compact roster shape for router context.

    Mirrors warroom-text-router.ts:31-36 RouterContext.roster element shape.
    """
    id: str
    name: str
    description: str


@dataclass
class RouterContext:
    """Port warroom-text-router.ts:28-36 RouterContext."""
    user_text: str
    roster: list[RosterAgentLite]
    recent_turns: list[dict[str, str]] = field(default_factory=list)
    pinned_agent: str | None = None
    meeting_id: int | None = None
    turn_id: str | None = None


@dataclass
class RouterDecision:
    """Port warroom-text-router.ts:38-44 RouterDecision."""
    primary: str | None
    interveners: list[str]
    reason: str
    router_degraded: bool


@dataclass
class InterventionContext:
    """Port warroom-text-router.ts:46-52 InterventionContext."""
    user_text: str
    primary_agent_id: str
    primary_reply: str
    candidate_agent_id: str
    candidate_agent_description: str
    meeting_id: int | None = None
    turn_id: str | None = None


@dataclass
class InterventionDecision:
    """Port warroom-text-router.ts:54-58 InterventionDecision."""
    speak: bool
    reply: str


# ── Port: sanitizeForPromptBlock ───────────────────────────────────────
# warroom-text-router.ts:90-93 — Critical injection-defense.

_TRIPLE_QUOTE_RE = re.compile(r'"""')


def sanitize_for_prompt_block(s: str) -> str:
    """Port warroom-text-router.ts:90-93 verbatim.

    Replaces `\"\"\"` with `'''` so user text can't escape our prompt block.
    """
    if not s:
        return ""
    return _TRIPLE_QUOTE_RE.sub("'''", s)


def _roster_block(roster: list[RosterAgentLite]) -> str:
    """Port warroom-text-router.ts:95-97."""
    return "\n".join(
        f"- {a.id}: {a.name} — {sanitize_for_prompt_block(a.description)}"
        for a in roster
    )


def _recent_block(turns: list[dict[str, str]]) -> str:
    """Port warroom-text-router.ts:99-102."""
    if not turns:
        return "(empty — first message of the meeting)"
    return "\n".join(
        f"{t.get('speaker', 'unknown')}: {sanitize_for_prompt_block(t.get('text', ''))}"
        for t in turns
    )


def build_router_prompt(ctx: RouterContext) -> str:
    """Port warroom-text-router.ts:104-141 verbatim. Wording calibrated for Haiku."""
    pin_line = (
        f"\nPinned agent (user has locked this agent as primary): {ctx.pinned_agent}"
        if ctx.pinned_agent
        else ""
    )
    return (
        "You're dispatching for a text group chat. Imagine a real meeting room: "
        "people speak up when the topic is clearly theirs, and stay quiet when it isn't.\n\n"
        "Roster (agent_id: NAME — description):\n"
        f"{_roster_block(ctx.roster)}\n\n"
        "Recent transcript (oldest first, up to last 6 turns):\n"
        f"{_recent_block(ctx.recent_turns)}{pin_line}\n\n"
        "New user message:\n"
        '"""\n'
        f"{sanitize_for_prompt_block(ctx.user_text)}\n"
        '"""\n\n'
        "Your job is to pick who speaks this turn.\n\n"
        "Primary (one agent leads the response):\n"
        "- Compare the user's message to each agent's description. The single most relevant specialist leads.\n"
        "- When the topic is generic, triage-style, or doesn't map cleanly to a specialist → primary = \"default\".\n"
        "- Social messages (thanks/ok/emoji) or truly unclear ones → primary = null.\n\n"
        "Interveners (0 to 2 others raise their hand):\n"
        "- Include an agent when their description genuinely overlaps the message in a way the primary couldn't fully cover. "
        "Think of it as someone in a meeting saying \"I've got something to add on that.\"\n"
        "- Don't add someone just to echo the primary. Distinct value only.\n"
        "- Multi-topic messages (two or three distinct domains in one ask) usually pull in one intervener per extra domain, up to 2.\n\n"
        "Rules:\n"
        "- If a pinned agent is set, primary = pinned unless the user explicitly names a different agent with @.\n"
        "- Never invent an agent_id — pick only from the roster above.\n"
        "- Order interveners by who should speak first.\n\n"
        "Respond with ONLY a JSON object, no prose, no code fences:\n"
        '{"primary": "<agent_id>" | null, "interveners": ["<agent_id>", ...], "reason": "<one short sentence saying why, in plain terms>"}'
    )


def build_gate_prompt(ctx: InterventionContext) -> str:
    """Port warroom-text-router.ts:277-298 verbatim."""
    return (
        f"You are {ctx.candidate_agent_id} "
        f"({sanitize_for_prompt_block(ctx.candidate_agent_description)}) "
        "in a group chat meeting with the user and a teammate.\n\n"
        "The user asked:\n"
        '"""\n'
        f"{sanitize_for_prompt_block(ctx.user_text)}\n"
        '"""\n\n'
        f"{ctx.primary_agent_id} just responded:\n"
        '"""\n'
        f"{sanitize_for_prompt_block(ctx.primary_reply)}\n"
        '"""\n\n'
        "You were pulled in because your domain is relevant. Default: speak up with your angle — "
        "that's the meeting vibe we want. People raise their hand when the topic touches their lane.\n\n"
        "- Speak if your domain is genuinely in scope, even by one degree of separation. Add your specific perspective from that angle.\n"
        "- Only stay silent if the primary literally said everything you would (rare) or if your domain truly has nothing to contribute here.\n"
        "- When you speak: 1-3 sentences, conversational, don't preamble with \"To add to that\" or \"Building on what "
        f"{ctx.primary_agent_id} said\". Just say your thing.\n\n"
        "Respond with ONLY a JSON object, no prose, no code fences:\n"
        '{"speak": true | false, "reply": "<your 1-3 sentence contribution, empty string if speak is false>"}'
    )


_CODE_FENCE_LEADING_RE = re.compile(r"^```(?:json)?\s*", re.IGNORECASE)
_CODE_FENCE_TRAILING_RE = re.compile(r"```\s*$", re.IGNORECASE)
_OBJECT_BLOCK_RE = re.compile(r"\{[\s\S]*\}")


def parse_json(text: str) -> Any | None:
    """Port warroom-text-router.ts:143-160 verbatim.

    Tolerates code-fence wrapping and prose preambles by extracting the
    first `{...}` block.
    """
    if not text:
        return None
    stripped = text.strip()
    stripped = _CODE_FENCE_LEADING_RE.sub("", stripped)
    stripped = _CODE_FENCE_TRAILING_RE.sub("", stripped).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        m = _OBJECT_BLOCK_RE.search(stripped)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
        return None


def sanitize_decision(raw: Any, ctx: RouterContext) -> dict[str, Any] | None:
    """Port warroom-text-router.ts:162-195 verbatim.

    Returns dict with `primary`, `interveners`, `reason` (no
    routerDegraded — caller adds that). Returns None if raw is bad JSON
    shape so caller falls back deterministically.
    """
    if not isinstance(raw, dict):
        return None
    valid_ids = {a.id for a in ctx.roster}

    primary_raw = raw.get("primary")
    if primary_raw is None:
        primary: str | None = None
    elif isinstance(primary_raw, str) and primary_raw in valid_ids:
        primary = primary_raw
    else:
        return None

    inter_raw = raw.get("interveners")
    interveners: list[str] = []
    if isinstance(inter_raw, list):
        for entry in inter_raw:
            if not isinstance(entry, str):
                continue
            if entry not in valid_ids:
                continue
            if entry == primary:
                continue
            if entry in interveners:
                continue
            if len(interveners) >= 2:
                break
            interveners.append(entry)

    reason_raw = raw.get("reason")
    reason = reason_raw[:200] if isinstance(reason_raw, str) else ""
    return {"primary": primary, "interveners": interveners, "reason": reason}


def router_fallback(ctx: RouterContext) -> RouterDecision:
    """Port warroom-text-router.ts:197-204 verbatim.

    Deterministic fallback used when the router LLM call fails, times
    out, or returns unparseable JSON. `routerDegraded=True` so the UI
    surfaces a "degraded routing" indicator.
    """
    return RouterDecision(
        primary=ctx.pinned_agent if ctx.pinned_agent else "default",
        interveners=[],
        reason="router unavailable — fell back to primary-only",
        router_degraded=True,
    )


def _classifier_request(prompt: str, *, ctx_meta: dict[str, Any]) -> RuntimeRequest:
    """Build a locked-down RuntimeRequest for Haiku classifier dispatch.

    Mirrors warroom-text-router.ts:219-231 SDK options lockdown — empty
    allowed_tools, disallowedTools=['*'], maxTurns=1, bypassPermissions.
    Cabinet code never invokes any concrete provider client; lane-router
    routes through `runtime.claude_sdk.ClaudeSdkRuntime` and forwards
    `disallowed_tools` and `mcp_servers` per WS1.0.
    """
    return RuntimeRequest(
        prompt=prompt,
        cwd=Path.cwd(),
        task_name="cabinet_router",
        capability=TEXT_REASONING,
        model=ROUTER_MODEL,
        max_turns=1,
        allowed_tools=[],
        disallowed_tools=["*"],
        mcp_servers=[],
        setting_sources=[],
        permission_mode="bypassPermissions",
        allow_fallback=False,
        metadata=ctx_meta,
    )


async def route_message(ctx: RouterContext) -> RouterDecision:
    """Port warroom-text-router.ts:206-267 — Haiku classifier with 20s budget.

    M7 kill-switch chain: layer 1 — `cabinet` feature gate (explicit
    here); layer 2 — `llm` model gate (automatic in lane_router). On
    `cabinet` disabled, returns router_fallback (deterministic primary)
    so the meeting still completes; the per-persona kill-switch then
    refuses cleanly when the agent turn would dispatch.

    Never throws — every error path is fallback.
    """
    # M7 layer 1: cabinet feature gate. requireEnabled raises
    # KillSwitchDisabled when HOMIE_KILLSWITCH_CABINET=disabled. We catch
    # and degrade to fallback so the cabinet meeting still completes (the
    # lane_router's `llm` gate will then block any subsequent SDK call).
    try:
        kill_switches.requireEnabled("cabinet", caller="cabinet_router")
    except kill_switches.KillSwitchDisabled:
        return router_fallback(ctx)

    prompt = build_router_prompt(ctx)
    request = _classifier_request(
        prompt,
        ctx_meta={
            "meeting_id": ctx.meeting_id,
            "turn_id": ctx.turn_id,
            "caller": "cabinet_router",
        },
    )

    try:
        result = await asyncio.wait_for(
            lane_router.run_with_runtime_lanes(request),
            timeout=ROUTER_TIMEOUT_S,
        )
    except (TimeoutError, Exception) as exc:  # noqa: BLE001 — never let router exception escape
        logger.warning("cabinet router failed: %s", _redact(str(exc)))
        return router_fallback(ctx)

    raw = parse_json(result.text)
    if raw is None:
        # PRD-8 Phase 7b WS1 (iter2 F1) — model output snippets can echo
        # secrets the prompt-injected user text smuggled in.
        logger.warning(
            "cabinet router produced unparseable output: %r",
            _redact(result.text[:200]),
        )
        return router_fallback(ctx)
    clean = sanitize_decision(raw, ctx)
    if clean is None:
        logger.warning(
            "cabinet router produced bad shape: %r",
            _redact(result.text[:200]),
        )
        return router_fallback(ctx)
    return RouterDecision(
        primary=clean["primary"],
        interveners=clean["interveners"],
        reason=clean["reason"],
        router_degraded=False,
    )


async def intervention_gate(ctx: InterventionContext) -> InterventionDecision:
    """Port warroom-text-router.ts:300-340 — single-agent yes/no with 25s budget.

    Returns `{speak: false, reply: ''}` on disabled/timeout/parse-fail.
    Never throws.
    """
    # M7 layer 1: cabinet feature gate.
    try:
        kill_switches.requireEnabled("cabinet", caller="cabinet_gate")
    except kill_switches.KillSwitchDisabled:
        return InterventionDecision(speak=False, reply="")

    prompt = build_gate_prompt(ctx)
    request = _classifier_request(
        prompt,
        ctx_meta={
            "meeting_id": ctx.meeting_id,
            "turn_id": ctx.turn_id,
            "candidate": ctx.candidate_agent_id,
            "caller": "cabinet_gate",
        },
    )

    try:
        result = await asyncio.wait_for(
            lane_router.run_with_runtime_lanes(request),
            timeout=GATE_TIMEOUT_S,
        )
    except (TimeoutError, Exception) as exc:  # noqa: BLE001
        logger.warning("cabinet intervention gate failed: %s", _redact(str(exc)))
        return InterventionDecision(speak=False, reply="")

    raw = parse_json(result.text)
    if raw is None or not isinstance(raw, dict):
        return InterventionDecision(speak=False, reply="")
    speak = raw.get("speak") is True
    reply_raw = raw.get("reply")
    reply = reply_raw[:800] if isinstance(reply_raw, str) else ""
    if speak and not reply.strip():
        return InterventionDecision(speak=False, reply="")
    return InterventionDecision(speak=speak, reply=reply)


__all__ = [
    "GATE_TIMEOUT_S",
    "InterventionContext",
    "InterventionDecision",
    "ROUTER_MODEL",
    "ROUTER_TIMEOUT_S",
    "RosterAgentLite",
    "RouterContext",
    "RouterDecision",
    "build_gate_prompt",
    "build_router_prompt",
    "intervention_gate",
    "parse_json",
    "route_message",
    "router_fallback",
    "sanitize_decision",
    "sanitize_for_prompt_block",
]
