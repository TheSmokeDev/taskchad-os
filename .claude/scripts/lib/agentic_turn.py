"""Give a SCHEDULED desk turn the same tools a conversational turn already has.

The alpha desks run unattended every 2h and their one LLM call shipped as
`max_turns=1, allowed_tools=[]` -- a mind that gets a single look at pre-scraped
text and cannot check anything. Epic #199 fixed the pipeline AROUND that call
(credibility scoring, CA extraction, the ledger, tiered verification) but never
the call itself, so the desk stayed a script with better plumbing:

    scrape -> [one cheap toolless pass] -> template -> post

This module is the missing seam. It resolves the SAME per-persona scope the chat
engine, the cabinet, and the Discord persona channel already resolve -- through
`build_persona_tool_payload`, which owns the kill switch, the per-call scope
re-check, and the audit row -- so a scheduled turn cannot get tools without also
getting the guardrails. That is the whole reason this defers to the existing
assembler instead of building tool_defs itself.

Fails OPEN in one direction only: any resolution failure returns no tools, and
the caller falls back to the one-shot behavior that shipped. A desk that cannot
resolve its scope still posts cards; it just posts them the dumb way, loudly.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

_logger = logging.getLogger(__name__)

# Ships ON. A kill switch turns a feature OFF; it does not birth it dark.
# `HOMIE_KILLSWITCH_AGENTIC_SCAN=disabled` reverts both desks to the one-shot
# toolless digest without a code change or a restart of anything but the task.
KILL_SWITCH_NAME = "agentic_scan"

# The desk persona. Both desks post as the crypto homie and share his ledger,
# so both resolve his scope -- one persona, one set of tools, one audit trail.
DEFAULT_DESK_PERSONA = "crypto"

_DEFAULT_MAX_TURNS = 6
_DEFAULT_TIER = "quality"

# Scheduled rounds are a materially narrower authority surface than Crypto
# Homie's interactive chat.  This is an explicit list, not a toolset include:
# profile `crypto` intentionally includes operator execution, browser reads,
# X search, and the live bracket tool for operator-directed conversations.
SCHEDULED_TOOL_ALLOWLIST: frozenset[str] = frozenset(
    {
        "memory_search",
        "recall",
        "crypto_candles",
        "crypto_indicators",
        "crypto_levels",
        "crypto_funding",
        "crypto_bar_clock",
        "crypto_desk_snapshot",
        "crypto_dexscreener",
        "crypto_polymarket",
        "crypto_last30days_read",
        "crypto_prediction_markets",
        "crypto_prediction_book",
        "crypto_source_tape",
        "crypto_position_size",
        "crypto_liquidation",
        "crypto_safety_check",
        "crypto_proof",
        "crypto_call_anchor",
        "crypto_hit_rate",
        "crypto_looks_read",
        "crypto_plays_read",
        "crypto_paper_read",
    }
)

SCHEDULED_TOOL_DENYLIST: frozenset[str] = frozenset(
    {
        "terminal",
        "process",
        "read_file",
        "write_file",
        "patch",
        "skill_manage",
        "browser_status",
        "browser_tabs",
        "browser_navigate",
        "browser_snapshot",
        "browser_console",
        "x_search",
        "crypto_submit_bracket",
    }
)


def agentic_max_turns() -> int:
    """Turn budget for a scheduled desk turn. Resolved at CALL time (Rule 1).

    Higher than the chat path's 8: a scan legitimately looks at several
    candidates in one pass, and each candidate can cost a chart read plus a
    safety check before the desk can judge it. Too low is the silent failure --
    the model runs out of turns mid-look and answers from what it happened to
    have, which is indistinguishable from the one-shot behavior this replaces.
    """
    raw = os.getenv("AGENTIC_SCAN_MAX_TURNS", "").strip()
    if not raw:
        return _DEFAULT_MAX_TURNS
    try:
        parsed = int(raw)
    except ValueError:
        _logger.warning("AGENTIC_SCAN_MAX_TURNS=%r is not an int; using %d", raw, _DEFAULT_MAX_TURNS)
        return _DEFAULT_MAX_TURNS
    # A turn budget of 0/negative would silently disable the loop while the
    # tools stayed attached -- the model would call one and never see a result.
    return parsed if parsed > 0 else _DEFAULT_MAX_TURNS


def agentic_model_tier() -> str:
    """Model tier for an agentic desk turn. Resolved at CALL time (Rule 1).

    Deliberately NOT the `fast` tier the one-shot digest uses. Driving a tool
    loop -- decide what to look at, read the result, decide again -- is a
    different job from summarizing pre-fetched text into JSON, and the cheap
    tier is chosen for the latter. Overriding to `fast` is supported and will
    mostly produce a model that calls one tool and stops.
    """
    return os.getenv("AGENTIC_SCAN_TIER", "").strip() or _DEFAULT_TIER


def agentic_enabled() -> bool:
    """True when a scheduled desk turn should get tools. CALL time (Rule 1).

    The kill switch is the only OFF control. Its absence (module missing, store
    unreadable) must not disable a working feature, so an exception here means
    ENABLED -- the switch grants nothing, it only revokes.
    """
    try:
        from security import kill_switches

        if kill_switches.is_disabled(KILL_SWITCH_NAME):
            _logger.info("agentic scan disabled by operator kill switch; desks run one-shot")
            return False
    except Exception:  # noqa: BLE001 -- absence of the switch is not a denial
        pass
    return True


# Tools the model is never TOLD about are decorative. The shipped Discord
# persona path had the mirror-image bug -- a preamble that said "Do NOT run any
# commands, tools, or scripts" while tools were attached -- so this preamble is
# applied by the same code that resolves the tools, and only when it resolves
# some. A prompt that promises tools to a turn that has none is the worse
# failure: the model announces a check it cannot perform.
#
# The last line is load-bearing. The desks parse the response as strict JSON;
# a tool loop whose FINAL message is prose yields zero plays and the source
# messages stay undigested. Tool calls are intermediate turns and do not count.
_DESK_TOOL_PREAMBLE = """You have tools, and a room full of strangers' claims. Check before you judge.

Do not take the room's word for anything you can verify yourself:
- Any play carrying a contract address: run crypto_safety_check on it BEFORE you
  call it a play. Honeypot, sell tax, mint/freeze authority still live, LP not
  locked -- any of those and it is not a play, it is a trap. Say so in the thesis.
- Liquidity and price reality: crypto_dexscreener. A "play" with no depth is a
  story, not a trade.
- Chart context for a named liquid asset: crypto_indicators and crypto_levels.
  Whether it is extended or at support changes the action, not just the thesis.
- Your own book: crypto_plays_read and crypto_hit_rate. If you already called
  this, say what happened last time instead of calling it again cold.

Judgment you are expected to exercise:
- Drop plays you cannot stand behind after looking. A short honest list beats a
  long credulous one -- the operator reads every card, so a bad one costs him.
- conviction reflects what you VERIFIED, not how loud the room was.

When you are done looking, your FINAL message must be the JSON object and
nothing else -- no prose around it, no code fences. Tool calls along the way are
expected and do not count as your answer.

---

"""


def tool_preamble(persona_id: str | None = None) -> str:
    """The instruction block that turns attached tools into used tools.

    Prepended by the caller that resolved the tools, never by the prompt
    builder -- so it cannot drift out of sync with whether tools are actually
    present.
    """
    actual_id = persona_id or DEFAULT_DESK_PERSONA
    try:
        from crypto_round.identity import build_identity_context

        identity = build_identity_context(actual_id)
    except Exception:  # identity failure cannot prevent a scheduled read
        identity = ""
    if not identity:
        return _DESK_TOOL_PREAMBLE
    return _DESK_TOOL_PREAMBLE + identity + "\n\n---\n\n"


def scheduled_profile_root(persona_id: str) -> Path:
    """Named profile root, independent of repository-owned ``HOMIE_HOME``."""
    return (Path.home() / ".homie" / "profiles" / persona_id).resolve(strict=False)


def resolve_desk_tools(persona_id: str | None = None):
    """Return ``(tool_defs, tool_dispatch)`` for a scheduled desk turn.

    ``(None, None)`` means "run the one-shot path" and is a normal outcome, not
    an error: the kill switch is off, the persona declares no scope, or the
    registry could not assemble. Every one of those must degrade to the shipped
    behavior rather than failing the 2-hourly run.

    ``persona_id`` is a None sentinel resolved here (Rule 1) so a test or a
    future second desk can override it without the default being frozen at
    import time.
    """
    if persona_id is None:
        persona_id = DEFAULT_DESK_PERSONA

    if not agentic_enabled():
        return None, None

    try:
        import personas
        from runtime.persona_tools import build_persona_tool_payload

        cfg = personas.load_persona_config(
            persona_id,
            profile_root=scheduled_profile_root(persona_id),
        )
        payload = build_persona_tool_payload(
            persona_id,
            cfg,
            allowed_tool_names=SCHEDULED_TOOL_ALLOWLIST,
        )
    except Exception:  # noqa: BLE001 -- a scope failure must never kill the run
        _logger.warning(
            "agentic scan: tool scope resolution failed for %s; falling back to one-shot",
            persona_id,
            exc_info=True,
        )
        return None, None

    if payload is None:
        # Default-deny answered "no scope". Not an error -- but say so, because
        # a desk that silently reverted to one-shot looks identical to a desk
        # that was never upgraded, and that ambiguity is what hid the third
        # persona surface for an entire epic.
        _logger.info(
            "agentic scan: persona %s resolved no tools; running one-shot", persona_id
        )
        return None, None

    tool_defs, tool_dispatch = payload
    names = {
        str((definition.get("function") or {}).get("name") or "")
        for definition in tool_defs
    }
    forbidden = names & SCHEDULED_TOOL_DENYLIST
    if forbidden:
        _logger.error(
            "agentic scan: scheduled scope contains denied tools for %s: %s",
            persona_id,
            sorted(forbidden),
        )
        return None, None
    from runtime.persona_tools import persona_tool_scope_version

    scope_hash = persona_tool_scope_version(persona_id, tool_defs)
    _logger.info(
        "agentic scan: persona %s armed with %d scheduled tools, %d turns, scope=%s",
        persona_id,
        len(tool_defs),
        agentic_max_turns(),
        scope_hash,
    )
    return tool_defs, tool_dispatch


__all__ = [
    "DEFAULT_DESK_PERSONA",
    "KILL_SWITCH_NAME",
    "SCHEDULED_TOOL_ALLOWLIST",
    "SCHEDULED_TOOL_DENYLIST",
    "agentic_enabled",
    "agentic_max_turns",
    "agentic_model_tier",
    "resolve_desk_tools",
    "scheduled_profile_root",
    "tool_preamble",
]
