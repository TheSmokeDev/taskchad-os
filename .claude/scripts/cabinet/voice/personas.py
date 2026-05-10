"""Cabinet voice personas — port of ClaudeClaw warroom/personas.py.

VERBATIM port of:

  * ``SHARED_RULES`` (warroom/personas.py:21-42) — Python string constant.
  * ``AUTO_ROUTER_PERSONA`` (warroom/personas.py:106-134) — Python string
    constant for the auto-mode router persona.
  * ``_generate_persona`` (warroom/personas.py:137-157) — fallback shape
    when a persona has no upstream entry.
  * ``_build_auto_roster_block`` (warroom/personas.py:160-182) — dynamic
    roster injection helper for AUTO_ROUTER_PERSONA.
  * ``get_persona(agent_id, mode)`` (warroom/personas.py:185-203) — lookup
    surface used by :mod:`cabinet.voice.agent_bridge`.

NOT ported per Q5 single-config-yaml lock:

  * ``AGENT_PERSONAS`` hardcoded dict (upstream lines 45-92). Per-persona
    voice prompt now comes from ``<profile>/config.yaml.cabinet.voice_persona_prompt``
    via :func:`personas.services.load_persona_config`. Falls back to
    ``_generate_persona(agent_id) + SHARED_RULES`` when the field is unset
    so default personas still get a working prompt.

Q4 wire-id translation site lock: the upstream wire string ``"main"`` is
preserved on the JS↔Python boundary (RTVI server-message frames + AgentRouteFrame).
The Python-side translation to internal id ``"default"`` happens HERE in
:func:`get_persona` so the rest of the codebase stays consistent.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from . import config as voice_config

logger = logging.getLogger("cabinet.voice.personas")

# PRD-8 Phase 7b — log-message redaction (Rule 3 module-attribute lookup).
from security import redact as _redact_mod  # noqa: E402
_redact = _redact_mod.redact


# ─── SHARED_RULES — VERBATIM port from warroom/personas.py:21-42 ──────────


SHARED_RULES = """HARD RULES (never break these):
- No em dashes. Ever.
- No AI clichés. Never say "Certainly", "Great question", "I'd be happy to", "As an AI", "absolutely", or any variation.
- No sycophancy. Don't validate, flatter, or soften things unnecessarily.
- Don't narrate what you're about to do. Just do it.
- Keep responses conversational and concise. Usually 1-3 sentences unless the user asks for detail.

HOW YOU OPERATE:
Answer from your own knowledge first. Most questions, opinions, and quick asks don't need delegation. You're smart, just talk.

Only delegate when:
1. The user explicitly asks you to pass it to another agent ("have research look into X").
2. The task requires real execution that you can't do conversationally (send an email, run a web search, schedule a meeting, write a long document, run shell commands).
3. Another agent's specialty clearly fits better than yours.

When you do delegate, use the delegate_to_agent tool. The sub-agent runs the task asynchronously through the full Claude Code stack and pings the user on Telegram when done.

If you think delegation would help but the user didn't ask for it, OFFER first: "want me to loop in research for this?" or "I can kick that to comms if you want." Don't just silently delegate.

CRITICAL: When you call delegate_to_agent, speak your verbal confirmation ONCE, and only AFTER the tool call completes. Do NOT speak before calling the tool, and do NOT read the tool's result message verbatim. Keep it to one short line like "Cool, I'm on it" or "Kicked it over to research." Never repeat yourself.

For tiny questions ("what time is it", "who's on my team"), use the inline tools (get_time, list_agents)."""


# ─── AUTO_ROUTER_PERSONA — VERBATIM port from warroom/personas.py:106-134 ─


AUTO_ROUTER_PERSONA = (
    """You are the front desk of the War Room. Five specialist agents sit around you:

- main: Hand of the King. General ops, triage, anything that doesn't clearly fit another agent.
- research: Grand Maester. Deep web research, academic sources, competitive intel, trend analysis.
- comms: Master of Whisperers. Email, Slack, Telegram, WhatsApp, customer comms, inbox triage.
- content: Royal Bard. Writing, YouTube scripts, LinkedIn posts, blog copy, creative direction.
- ops: Master of War. Calendar, scheduling, cron, system operations, MCP tool work, automations.

YOUR JOB IS TO ROUTE, NOT TO ANSWER.

When the user speaks:
1. Decide which agent is the best fit based on the roles above.
2. Speak ONE short acknowledgment first ("checking", "one sec", "on it"). One or two words. Nothing more.
3. Call the answer_as_agent tool with that agent id and the user's full question.
4. When the tool returns, read the text field VERBATIM. Do not paraphrase. Do not add commentary. Do not prefix with "they said" or "the answer is". Just speak the text.

EXCEPTIONS (answer yourself, do NOT call the tool):
- Conversational noise: "hey", "thanks", "cool", "got it", "nevermind", "that's all", goodbyes.
- Meta questions about the team itself: "who's on my team", "who can I ask". Use list_agents for these.
- Clock questions: "what time is it". Use get_time.

If the user uses a name prefix like "research, what's X" or "ask ops about Y", honor that routing and skip the classification step. They already picked.

If you genuinely cannot decide between two agents, route to main and let main triage. Do not stall asking clarifying questions.

"""
    + SHARED_RULES
)


# ─── Persona fallback + roster injection ─────────────────────────────────


def _generate_persona(agent_id: str) -> str:
    """Generate a basic persona for agents not in the hardcoded list.

    Verbatim port of warroom/personas.py:137-157. Reads the dynamic roster
    from :data:`config.ROSTER_PATH` (renamed from /tmp/warroom-agents.json
    -> /tmp/cabinet-roster.json) for the agent's display name + description.
    """
    try:
        roster = json.loads(Path(voice_config.ROSTER_PATH).read_text())
        for a in roster:
            if a.get("id") == agent_id:
                name = a.get("name", agent_id.title())
                desc = a.get("description", "a specialist agent")
                return (
                    f"You are {name} in the War Room. {desc}. "
                    f"Personality: focused, competent, and concise.\n\n"
                ) + SHARED_RULES
    except Exception as exc:  # noqa: BLE001 — match upstream's broad catch
        logger.debug("roster fallback read failed: %s", _redact(str(exc)))
    # Ultimate fallback: generic agent persona (matches upstream's last resort).
    return (
        f"You are {agent_id.title()} in the War Room. "
        f"You are a specialist agent. Be focused and concise.\n\n"
    ) + SHARED_RULES


def _build_auto_roster_block() -> str:
    """Build the agent roster lines for the auto-router persona.

    Verbatim port of warroom/personas.py:160-182. The 5 known role
    descriptions are preserved verbatim; for unknown roles the function
    falls back to the persona's own description.
    """
    _known = {
        "main": "Hand of the King. General ops, triage, anything that doesn't clearly fit another agent.",
        "research": "Grand Maester. Deep web research, academic sources, competitive intel, trend analysis.",
        "comms": "Master of Whisperers. Email, Slack, Telegram, WhatsApp, customer comms, inbox triage.",
        "content": "Royal Bard. Writing, YouTube scripts, LinkedIn posts, blog copy, creative direction.",
        "ops": "Master of War. Calendar, scheduling, cron, system operations, MCP tool work, automations.",
    }
    try:
        agents = json.loads(Path(voice_config.ROSTER_PATH).read_text())
        lines: list[str] = []
        for a in agents:
            aid = a.get("id")
            if not aid:
                continue
            desc = _known.get(aid, a.get("description", "Specialist agent."))
            lines.append(f"- {aid}: {desc}")
        if lines:
            return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001 — match upstream
        logger.debug("auto-roster build failed: %s", _redact(str(exc)))
    return "\n".join(f"- {k}: {v}" for k, v in _known.items())


# ─── get_persona — lookup surface (Q5 + Q4 wire translation) ──────────────


def resolve_internal_persona_id(wire_id: str) -> str:
    """Translate the upstream wire id to the Homie internal persona id.

    Q4 lock: the upstream wire string ``"main"`` is preserved verbatim on
    the JS↔Python boundary; the actual persona file is registered under id
    ``"default"`` (see :data:`cabinet.text_orchestrator._MAIN_AGENT.id`).
    This is the single translation site.
    """
    if wire_id == voice_config.DEFAULT_AGENT_WIRE:
        return voice_config.DEFAULT_AGENT_INTERNAL
    return wire_id


def get_persona(agent_id: str, mode: str = "direct") -> str:
    """Return the persona prompt for an agent.

    Verbatim port of warroom/personas.py:185-203 with two Homie deviations:

    1. Q5 lock — per-persona prompt source is
       ``<profile>/config.yaml.cabinet.voice_persona_prompt`` (NOT the
       upstream ``AGENT_PERSONAS`` hardcoded dict).
    2. Q4 lock — wire id ``"main"`` resolves to internal persona id
       ``"default"`` at the persona-config lookup boundary.

    Args:
        agent_id: wire-side persona id (may be ``"main"``).
        mode: ``"direct"`` (default) returns the per-persona prompt;
            ``"auto"`` returns :data:`AUTO_ROUTER_PERSONA` with a dynamic
            roster block injected (matches upstream).

    Returns:
        System prompt string ready to use as the runtime persona prompt.

    Rule 1: ``mode`` default ``"direct"`` is a primitive literal (NOT a
    config-derived constant), so this is forward-additive without a
    sentinel-resolve dance.
    """
    if mode == "auto":
        # Inject dynamic roster into the auto-router persona (matches
        # upstream's string.replace approach verbatim).
        roster_block = _build_auto_roster_block()
        return AUTO_ROUTER_PERSONA.replace(
            "- main: Hand of the King. General ops, triage, anything that doesn't clearly fit another agent.\n"
            "- research: Grand Maester. Deep web research, academic sources, competitive intel, trend analysis.\n"
            "- comms: Master of Whisperers. Email, Slack, Telegram, WhatsApp, customer comms, inbox triage.\n"
            "- content: Royal Bard. Writing, YouTube scripts, LinkedIn posts, blog copy, creative direction.\n"
            "- ops: Master of War. Calendar, scheduling, cron, system operations, MCP tool work, automations.",
            roster_block,
        )

    # Direct mode — read persona's voice_persona_prompt from config.yaml.
    internal_id = resolve_internal_persona_id(agent_id)
    try:
        # Late-bind import — keeps voice subprocess startup cheap when no
        # voice turn has fired yet.
        import personas  # noqa: PLC0415

        cfg: dict[str, Any] = personas.load_persona_config(internal_id)
    except Exception as exc:  # noqa: BLE001 — fail-open, fall through.
        logger.debug(
            "persona config load failed for %s (wire=%s): %s",
            _redact(internal_id),
            _redact(agent_id),
            _redact(str(exc)),
        )
        return _generate_persona(agent_id)

    # Dict-safe access (Q5 lock — load_persona_config returns dict[str, Any]
    # per .claude/scripts/personas/services.py:393).
    cabinet = cfg.get("cabinet") or {}
    if not isinstance(cabinet, dict):
        return _generate_persona(agent_id)

    voice_prompt = cabinet.get("voice_persona_prompt")
    if isinstance(voice_prompt, str) and voice_prompt.strip():
        # Compose with SHARED_RULES suffix to match upstream's
        # ``"...persona text..." + SHARED_RULES`` pattern.
        return voice_prompt.rstrip() + "\n\n" + SHARED_RULES

    # Fallback: synthesize a minimal persona prompt from the persona's
    # display name + description, matching the upstream dynamic generation
    # path. Keeps default personas working end-to-end on a fresh install.
    return _generate_persona(agent_id)


__all__ = [
    "AUTO_ROUTER_PERSONA",
    "SHARED_RULES",
    "_build_auto_roster_block",
    "_generate_persona",
    "resolve_internal_persona_id",
    "get_persona",
]
