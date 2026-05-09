"""Cabinet text orchestrator.

PORTED FROM ClaudeClaw `src/warroom-text-orchestrator.ts:1-2047` per PRD-8 §0a
(Phase 5a scope: orchestrator entrypoint + primary-pick chain + intervener
loop + helpers + warmup + cancel/idle-wait. Slash-command handlers stay
unported until Phase 5b consumes the orchestrator from chat.).

B1 lock — every per-persona turn dispatches via
`runtime.lane_router.run_with_runtime_lanes(RuntimeRequest)` with
`disallowed_tools` and `mcp_servers` from `cabinet.tool_policy.cabinet_tool_policy`.
NO `claude_agent_sdk.query()` call — cabinet code never invokes a concrete
provider client.

B9 lock — Phase 5a does NOT write recall/memory-index data. Upstream's
runAgentTurn at warroom-text-orchestrator.ts:1919-1967 performs memory
ingestion that block is OUT OF SCOPE for Phase 5a (Phase 6 owns it). AST
guard rejects `recall_service.store/write/append` and memory-index writes.

M7 kill-switch chain — TWO LAYERS:
  1. `kill_switches.requireEnabled('cabinet')` — feature gate (explicit).
  2. lane_router's `kill_switches.requireEnabled('llm')` — model gate
     (automatic via `runtime/lane_router.py:90-94`).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import personas
from dashboard_db import get_connection
from runtime import lane_router
from runtime.base import RuntimeRequest
from runtime.capabilities import TEXT_REASONING
from security import kill_switches

from . import meeting_channel as _channels_mod
from .meeting_channel import MeetingChannel, get_channel
from .text_router import (
    GATE_TIMEOUT_S,
    InterventionContext,
    RosterAgentLite,
    RouterContext,
    RouterDecision,
    intervention_gate,
    route_message,
    router_fallback,
)
from .title import schedule_title_generation
from .tool_policy import cabinet_tool_policy, filter_mcp_servers

logger = logging.getLogger(__name__)

# PRD-8 Phase 7b WS1 (codex post-build F1) — log-message redaction at every
# cabinet log emit site. Module-attribute import (Rule 3); redact() unconditional
# (NOT kill-switch gated — see security/redact.py docstring).
from security import redact as _redact_mod  # noqa: E402
_redact = _redact_mod.redact


# ── Roster ────────────────────────────────────────────────────────────────

@dataclass
class RosterAgent:
    """Port warroom-text-orchestrator.ts:73-77 RosterAgent verbatim."""
    id: str
    name: str
    description: str
    tools: list[str] = field(default_factory=list)
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    auth_profile: str | None = None


_MAIN_AGENT: Final[RosterAgent] = RosterAgent(
    id="default",
    name="Main",
    description="General ops and triage",
)


def _roster_from_personas() -> list[RosterAgent]:
    """Read the persona registry and yield cabinet-eligible roster.

    Personas with no `cabinet` block in `config.yaml` are filtered out
    (per Phase 2 `_validate_cabinet_section` contract). Main is always
    first.
    """
    extras: list[RosterAgent] = []
    try:
        profiles = personas.lifecycle.list_profiles()
    except Exception as exc:  # noqa: BLE001
        logger.debug("cabinet roster: list_profiles failed: %s", _redact(str(exc)))
        return [_MAIN_AGENT]

    for profile in profiles:
        if profile.name == "default":
            continue  # default = main; already included.
        try:
            cfg = personas.load_persona_config(profile.name)
        except (FileNotFoundError, personas.ConfigShapeError):
            continue
        except Exception:  # noqa: BLE001
            continue
        cabinet_block = cfg.get("cabinet")
        if not isinstance(cabinet_block, dict):
            # Persona is not cabinet-eligible.
            continue
        persona_section = cfg.get("persona", {}) if isinstance(cfg.get("persona"), dict) else {}
        display_name = (
            persona_section.get("display_name")
            or persona_section.get("name")
            or profile.name
        )
        description = persona_section.get("role", "") or ""
        tools_raw = cabinet_block.get("tools")
        tools = list(tools_raw) if isinstance(tools_raw, list) else []
        extras.append(
            RosterAgent(
                id=profile.name,
                name=display_name,
                description=description,
                tools=tools,
            )
        )
    return [_MAIN_AGENT, *extras]


def get_roster() -> list[RosterAgent]:
    """Port warroom-text-orchestrator.ts:91-96 — main first, then extras."""
    return _roster_from_personas()


# ── Public turn API ──────────────────────────────────────────────────────


@dataclass
class HandleTurnOptions:
    """Port warroom-text-orchestrator.ts:100-103 HandleTurnOptions."""
    roster: list[RosterAgent] | None = None


@dataclass
class HandleTurnResult:
    """Port warroom-text-orchestrator.ts:105-112 HandleTurnResult."""
    accepted: bool
    turn_id: str | None = None
    deduped: bool | None = None
    error: str | None = None


# Per-meeting cancel-flag registry (rule-2 exception — runtime registry).
@dataclass
class _CancelEntry:
    meeting_id: int
    flag: dict[str, bool]  # mutable: {"cancelled": bool}


_active_cancel_flags: dict[str, _CancelEntry] = {}


# ── Turn-id generation ───────────────────────────────────────────────────

def _make_turn_id() -> str:
    """Port warroom-text-orchestrator.ts:135 — t_<base36-ts>_<hex6>.

    `<base36-ts>` is the current ms-epoch as base36; `<hex6>` is 6 hex
    chars of cryptographically random data.
    """
    ts_ms = int(time.time() * 1000)
    base36 = ""
    n = ts_ms
    if n == 0:
        base36 = "0"
    else:
        while n > 0:
            n, r = divmod(n, 36)
            base36 = "0123456789abcdefghijklmnopqrstuvwxyz"[r] + base36
    rand = secrets.token_hex(3)
    return f"t_{base36}_{rand}"


# ── Greeting/Acknowledgment short-circuit (port lines 1232-1272) ─────────

_GREETING_WORD_ONLY_RE = re.compile(
    r"^\s*(?:hi|hey|hello|yo|sup|howdy|gm|good morning|good afternoon|good evening)[!.\s]*$",
    re.IGNORECASE,
)
_GREETING_LEADS_RE = re.compile(
    r"^\s*(?:hi|hey|hello|yo|sup|howdy)\b",
    re.IGNORECASE,
)
_GREETING_QUESTION_RE = re.compile(
    r"^\s*(?:hey\s+)?(?:how(?:'s|\s+is)?|hows)\s+(?:it\s+)?(?:going|ya|you|things)(?:\s+doing)?[\s,.!?]*$",
    re.IGNORECASE,
)
_GREETING_WHATSUP_RE = re.compile(
    r"^\s*(?:(?:hey|hi|yo)[,.!?\s]+)?(?:what'?s\s+up|wassup|wazzup)[\s,.!?]*$",
    re.IGNORECASE,
)
_TASK_WORD_RE = re.compile(
    r"\b(?:can|could|would|should|will|help|make|create|write|build|draft|send|pull|find|give|show|tell|do|add|set|update|change|fix|check|search|look|schedule|cancel|email|post|call|book|plan|analyze|research|compare)\b",
    re.IGNORECASE,
)
_ACK_HEAD = (
    r"(?:thanks?|thank you|thx|ty|tysm|ok(?:ay)?|got it|cool|nice|great|"
    r"awesome|sounds? good|nvm|never mind|lol|haha|👍|🙏|👌|❤️|💯)"
)
_ACK_COLLECTIVE = r"(?:\s+(?:team|everyone|everybody|all|y'?all|folks|guys|gang|crew))?"
_ACK_RE = re.compile(r"^\s*" + _ACK_HEAD + _ACK_COLLECTIVE + r"[!.\s]*$", re.IGNORECASE)


def is_greeting(text: str) -> bool:
    """Port warroom-text-orchestrator.ts:1248-1255."""
    if _GREETING_WORD_ONLY_RE.match(text):
        return True
    if _GREETING_QUESTION_RE.match(text):
        return True
    if _GREETING_WHATSUP_RE.match(text):
        return True
    if len(text) <= 40 and _GREETING_LEADS_RE.match(text) and not _TASK_WORD_RE.search(text):
        return True
    return False


def is_acknowledgment(text: str) -> bool:
    """Port warroom-text-orchestrator.ts:1262-1266."""
    if _ACK_RE.match(text):
        return True
    # Pure-emoji message: defer-friendly heuristic — unicode emoji-only check
    # would require full unicodedata; the upstream EMOJI_ONLY_RE covers
    # Unicode `Emoji_Presentation`/`Extended_Pictographic`. Python's `re`
    # doesn't expose those properties, so we approximate: text composed
    # entirely of non-word, non-ASCII-letter characters AND non-empty.
    stripped = text.strip()
    if stripped and re.fullmatch(r"[^\w\s\.,!?;:'\"\-]+", stripped, re.UNICODE):
        return True
    return False


def is_social_message(text: str) -> bool:
    """Port warroom-text-orchestrator.ts:1270-1272."""
    return is_greeting(text) or is_acknowledgment(text)


# ── @-mention extraction (port warroom-text-orchestrator.ts:810-823) ─────

_AT_MENTION_RE = re.compile(r"(?:^|[\s,(\[{:;])@([a-z][a-z0-9_-]{0,29})\b", re.IGNORECASE)


def extract_all_at_mentions(text: str, roster: list[RosterAgent]) -> list[str]:
    """Port warroom-text-orchestrator.ts:810-823 verbatim."""
    roster_ids = {a.id for a in roster}
    seen: set[str] = set()
    out: list[str] = []
    for m in _AT_MENTION_RE.finditer(text):
        candidate = m.group(1).lower()
        if candidate not in roster_ids:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        out.append(candidate)
    return out


# ── Slash-command recognition (Phase 5a recognizes; Phase 5b consumes) ───

_SLASH_RE = re.compile(r"^/(standup|discuss)(?:\s+([\s\S]*))?$", re.IGNORECASE)


def parse_slash_command(text: str) -> dict[str, str] | None:
    """Port warroom-text-orchestrator.ts:839-845 — recognize-only in Phase 5a."""
    m = _SLASH_RE.match(text)
    if not m:
        return None
    return {"cmd": m.group(1).lower(), "args": (m.group(2) or "").strip()}


# ── Sticky-addressee (port warroom-text-orchestrator.ts:1190-1220) ───────

STICKY_MAX_TEXT_LEN: Final[int] = 400
STICKY_MAX_AGE_S: Final[int] = 600
_STICKY_BREAKERS_RE = re.compile(r"\b(new topic|different question|change subject|by the way|btw)\b", re.IGNORECASE)


def infer_sticky_addressee(
    meeting_id: int,
    current_text: str,
    roster: list[RosterAgent],
    before_user_id: int,
) -> str | None:
    """Port warroom-text-orchestrator.ts:1190-1220 — sticky-from-prior-mention."""
    if len(current_text) > STICKY_MAX_TEXT_LEN:
        return None
    if is_greeting(current_text) or is_acknowledgment(current_text):
        return None
    if _STICKY_BREAKERS_RE.search(current_text):
        return None
    if extract_all_at_mentions(current_text, roster):
        return None
    try:
        conn = get_connection()
        try:
            row = conn.execute(
                """SELECT text, created_at FROM cabinet_transcripts
                   WHERE meeting_id = ? AND speaker = ? AND id < ?
                   ORDER BY id DESC LIMIT 1""",
                (meeting_id, "user", before_user_id),
            ).fetchone()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None
    if row is None:
        return None
    age_s = int(time.time()) - int(row["created_at"])
    if age_s > STICKY_MAX_AGE_S:
        return None
    prior_mentions = extract_all_at_mentions(row["text"], roster)
    if len(prior_mentions) != 1:
        return None
    return prior_mentions[0]


# ── Warmup tracking (port warroom-text-orchestrator.ts:514-580) ──────────

_warmup_in_flight: asyncio.Future | None = None
_warmup_done: bool = False
_warmup_lock = asyncio.Lock()


async def warmup_meeting() -> None:
    """Port warroom-text-orchestrator.ts:526-575 — best-effort SDK pre-warm."""
    global _warmup_in_flight, _warmup_done
    if _warmup_done:
        return
    async with _warmup_lock:
        if _warmup_done:
            return
        if _warmup_in_flight is not None and not _warmup_in_flight.done():
            await _warmup_in_flight
            return

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        _warmup_in_flight = fut

    async def _run() -> None:
        global _warmup_done
        try:
            request = RuntimeRequest(
                prompt="say ok",
                cwd=Path.cwd(),
                task_name="cabinet_warmup",
                capability=TEXT_REASONING,
                model="claude-haiku-4-5-20251001",
                max_turns=1,
                allowed_tools=[],
                disallowed_tools=["*"],
                permission_mode="bypassPermissions",
                allow_fallback=False,
                metadata={"caller": "cabinet_warmup"},
            )
            await asyncio.wait_for(
                lane_router.run_with_runtime_lanes(request),
                timeout=10.0,
            )
            _warmup_done = True
        except Exception as exc:  # noqa: BLE001
            logger.debug("cabinet warmup failed (non-fatal): %s", exc)

    try:
        await _run()
    finally:
        if not fut.done():
            fut.set_result(None)


def is_warmup_done() -> bool:
    """Port warroom-text-orchestrator.ts:577-579."""
    return _warmup_done


def prewarm_agent_sdks(agent_ids: list[str]) -> None:
    """Port warroom-text-orchestrator.ts:663-667 — fire-and-forget."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for _ in agent_ids:
        # Best-effort: spawn the warmup; errors are swallowed inside.
        loop.create_task(warmup_meeting())


# ── Cancel + idle-wait (port warroom-text-orchestrator.ts:676-748) ───────


def cancel_turn(turn_id: str) -> bool:
    """Port warroom-text-orchestrator.ts:676-682."""
    entry = _active_cancel_flags.get(turn_id)
    if entry is None:
        return False
    entry.flag["cancelled"] = True
    return True


def cancel_meeting_turns(meeting_id: int) -> int:
    """Port warroom-text-orchestrator.ts:717-731."""
    count = 0
    for entry in _active_cancel_flags.values():
        if entry.meeting_id != meeting_id:
            continue
        if not entry.flag["cancelled"]:
            entry.flag["cancelled"] = True
            count += 1
    return count


def get_active_turn_ids(meeting_id: int) -> list[str]:
    """Port warroom-text-orchestrator.ts:686-692."""
    return [tid for tid, entry in _active_cancel_flags.items() if entry.meeting_id == meeting_id]


async def wait_for_meeting_turns_idle(meeting_id: int, timeout_ms: int = 5000) -> None:
    """Port warroom-text-orchestrator.ts:699-708."""
    deadline_s = time.monotonic() + (timeout_ms / 1000.0)
    while get_active_turn_ids(meeting_id):
        if time.monotonic() > deadline_s:
            return
        await asyncio.sleep(0.05)


# ── Audit-log helper ─────────────────────────────────────────────────────


def _audit_cabinet(action: str, meeting_id: int, persona_id: str, outcome: str, detail: dict) -> None:
    """Best-effort audit-log row write via `dashboard_api._audit_write`.

    Imports late to avoid circular imports.
    """
    try:
        from dashboard_api import _audit_write  # noqa: PLC0415 — late-bind
        _audit_write(
            operator_id="cabinet",
            action=action,
            target_persona_id=persona_id,
            outcome=outcome,
            detail={"meeting_id": meeting_id, **detail},
            blocked=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("cabinet audit-write failed (non-fatal): %s", exc)


# ── Per-persona turn (B1: lane-router-only dispatch) ─────────────────────


@dataclass
class _RunAgentArgs:
    persona_id: str
    meeting_id: int
    user_text: str
    role: str  # 'primary' | 'intervener'
    turn_id: str
    channel: MeetingChannel
    cancel_flag: dict[str, bool]
    turn_state: dict[str, bool]
    persona: RosterAgent
    roster: list[RosterAgent]


async def _run_agent_turn(args: _RunAgentArgs) -> str:
    """Run a single persona turn through the lane-router.

    Port of warroom-text-orchestrator.ts:1330-1975 (Phase 5a scope:
    streaming + tool-policy + transcript persistence; OUT-OF-SCOPE per B9
    is the recall/memory-ingestion block at 1919-1967).

    Cabinet code never invokes any concrete provider client — every per-
    persona turn dispatches via `runtime.lane_router.run_with_runtime_lanes`.
    """
    channel = args.channel

    # Skip if meeting/turn already finalized (post-watchdog cleanup).
    if channel.is_turn_finalized(args.turn_id):
        return ""
    if args.cancel_flag.get("cancelled"):
        return ""

    # Build tool-policy from persona config (M1 default-deny floor).
    policy = cabinet_tool_policy(args.persona_id, args.persona.tools or None)
    mcp_filtered = filter_mcp_servers(args.persona.mcp_servers, policy)
    mcp_names = list(mcp_filtered.keys())

    # Emit agent_typing so the UI can show the typing indicator.
    channel.emit({
        "type": "agent_typing",
        "turnId": args.turn_id,
        "agentId": args.persona_id,
        "role": args.role,
    })

    request = RuntimeRequest(
        prompt=args.user_text,
        cwd=Path.cwd(),
        task_name="cabinet_persona_turn",
        capability=TEXT_REASONING,
        # No model override — let lane_router resolve per persona/profile.
        max_turns=1,
        allowed_tools=list(policy.allowed_tools),
        disallowed_tools=list(policy.disallowed_tools),
        mcp_servers=mcp_names,
        permission_mode="bypassPermissions",
        allow_fallback=True,
        metadata={
            "meeting_id": args.meeting_id,
            "turn_id": args.turn_id,
            "persona_id": args.persona_id,
            "caller": "cabinet_orchestrator",
            "tool_policy": {
                "allowed_count": len(policy.allowed_tools),
                "disallowed_count": len(policy.disallowed_tools),
                "mcp_count": len(mcp_names),
            },
        },
        auth_profile=args.persona.auth_profile,
    )

    text = ""
    try:
        result = await lane_router.run_with_runtime_lanes(request)
        text = (result.text or "").strip()
        # Surface tool-call audit rows.
        for tc in result.tool_calls or []:
            _audit_cabinet(
                action="cabinet_tool_call",
                meeting_id=args.meeting_id,
                persona_id=args.persona_id,
                outcome="tool_use",
                detail={"tool": tc.name, "tool_use_id": tc.id},
            )
            channel.emit({
                "type": "tool_call",
                "turnId": args.turn_id,
                "agentId": args.persona_id,
                "toolUseId": tc.id,
                "tool": tc.name,
                "argsPreview": _preview_args(tc.arguments),
            })
    except kill_switches.KillSwitchDisabled as exc:
        logger.info("cabinet persona turn refused: %s", _redact(str(exc)))
        args.turn_state["anyIncomplete"] = True
        channel.emit({
            "type": "intervention_skipped",
            "turnId": args.turn_id,
            "agentId": args.persona_id,
            "reason": "llm kill-switch disabled",
        })
        return ""
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "cabinet persona turn failed (%s): %s",
            args.persona_id,
            _redact(str(exc)),
        )
        args.turn_state["anyIncomplete"] = True
        channel.emit({
            "type": "error",
            "turnId": args.turn_id,
            "agentId": args.persona_id,
            "message": str(exc),
            "recoverable": True,
        })
        return ""

    # Persist agent reply to transcript (durable). Capture the rowid for
    # the agent_done event so the UI can correlate.
    transcript_row_id: int | None = None
    if text:
        try:
            conn = get_connection()
            try:
                cur = conn.execute(
                    """INSERT INTO cabinet_transcripts (meeting_id, speaker, text)
                       VALUES (?, ?, ?)""",
                    (args.meeting_id, args.persona_id, text),
                )
                transcript_row_id = cur.lastrowid
                conn.execute(
                    "UPDATE cabinet_meetings SET entry_count = entry_count + 1 WHERE id = ?",
                    (args.meeting_id,),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("cabinet transcript write failed: %s", _redact(str(exc)))

    channel.emit({
        "type": "agent_done",
        "turnId": args.turn_id,
        "agentId": args.persona_id,
        "role": args.role,
        "text": text,
        "transcriptRowId": transcript_row_id,
        "incomplete": not bool(text),
    })

    if not text:
        args.turn_state["anyIncomplete"] = True
    return text


def _preview_args(arguments: Any) -> str:
    """Compact preview for tool_call event (capped at 200 chars)."""
    if arguments is None:
        return ""
    try:
        s = arguments if isinstance(arguments, str) else str(arguments)
    except Exception:  # noqa: BLE001
        return ""
    return s[:200]


# ── Persistence helpers ──────────────────────────────────────────────────


def _get_meeting(meeting_id: int) -> dict | None:
    """Read a single cabinet_meetings row. Returns None if not found."""
    try:
        conn = get_connection()
        try:
            row = conn.execute(
                """SELECT id, started_at, ended_at, mode, pinned_persona, entry_count
                   FROM cabinet_meetings WHERE id = ?""",
                (meeting_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("cabinet _get_meeting failed: %s", _redact(str(exc)))
        return None
    if row is None:
        return None
    return dict(row)


def _persist_user_message(meeting_id: int, text: str) -> int | None:
    """INSERT user transcript row + bump entry_count. Returns row id."""
    try:
        conn = get_connection()
        try:
            cur = conn.execute(
                """INSERT INTO cabinet_transcripts (meeting_id, speaker, text)
                   VALUES (?, ?, ?)""",
                (meeting_id, "user", text),
            )
            row_id = cur.lastrowid
            conn.execute(
                "UPDATE cabinet_meetings SET entry_count = entry_count + 1 WHERE id = ?",
                (meeting_id,),
            )
            conn.commit()
            return row_id
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("cabinet user-row write failed: %s", _redact(str(exc)))
        return None


def _remember_client_msg_id(meeting_id: int, client_msg_id: str) -> bool:
    """Dedup LRU. Returns True if NEW (not seen before), False if duplicate.

    `cabinet_client_msg_seen` table created via Phase 5a migration.
    """
    if not client_msg_id:
        return True
    try:
        conn = get_connection()
        try:
            cur = conn.execute(
                """INSERT OR IGNORE INTO cabinet_client_msg_seen
                   (meeting_id, client_msg_id) VALUES (?, ?)""",
                (meeting_id, client_msg_id),
            )
            conn.commit()
            return cur.rowcount == 1
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("cabinet dedup write failed: %s", _redact(str(exc)))
        return True  # fail-open — better to dispatch than silently drop.


def _build_router_context(
    meeting_id: int,
    user_text: str,
    roster: list[RosterAgent],
    pinned_agent: str | None,
    turn_id: str,
) -> RouterContext:
    """Build a RouterContext with the roster + recent transcript snapshot."""
    recent: list[dict[str, str]] = []
    try:
        conn = get_connection()
        try:
            rows = conn.execute(
                """SELECT speaker, text FROM cabinet_transcripts
                   WHERE meeting_id = ? ORDER BY id DESC LIMIT 6""",
                (meeting_id,),
            ).fetchall()
        finally:
            conn.close()
        # Reverse to chronological order.
        for r in reversed(rows):
            recent.append({"speaker": r["speaker"], "text": r["text"]})
    except Exception:  # noqa: BLE001
        pass
    return RouterContext(
        user_text=user_text,
        roster=[RosterAgentLite(id=a.id, name=a.name, description=a.description) for a in roster],
        recent_turns=recent,
        pinned_agent=pinned_agent,
        meeting_id=meeting_id,
        turn_id=turn_id,
    )


# ── Public entrypoint ────────────────────────────────────────────────────


async def handle_text_turn(
    meeting_id: int,
    user_text: str,
    client_msg_id: str,
    opts: HandleTurnOptions | None = None,
) -> HandleTurnResult:
    """Port warroom-text-orchestrator.ts:114-525 — orchestrator entrypoint.

    Step order (matches upstream contract):
      1. Re-fetch meeting AT TURN-EXECUTE TIME (not enqueue time).
      2. Dedup via _remember_client_msg_id.
      3. Persist user row BEFORE agent work.
      4. Resolve primary: @mention → pinned → sticky → router → greeting/ack short-circuit.
      5. Run agent via lane-router (B1 lock).
      6. Intervener loop (up to 2 sequential, gated by intervention_gate).
      7. Emit turn_complete (or turn_aborted if cancelled mid-work).

    M7 kill-switch chain: layer 1 (cabinet) here at function head; layer 2
    (llm) automatic via lane_router on every per-turn dispatch.

    Rule 1: opts=None sentinel resolved at body time.
    """
    resolved_opts = opts if opts is not None else HandleTurnOptions()

    # M7 layer 1: cabinet feature gate. Caller (POST /api/cabinet/send)
    # catches KillSwitchDisabled and returns 503.
    kill_switches.requireEnabled("cabinet", caller="cabinet_orchestrator")

    # 1. Re-fetch meeting.
    meeting = _get_meeting(meeting_id)
    if meeting is None:
        return HandleTurnResult(accepted=False, error="meeting_not_found")
    if meeting["ended_at"] is not None:
        return HandleTurnResult(accepted=False, error="meeting_ended")

    trimmed = (user_text or "").strip()
    if not trimmed:
        return HandleTurnResult(accepted=False, error="empty_message")

    # 2. Dedup.
    is_new = _remember_client_msg_id(meeting_id, client_msg_id)
    if not is_new:
        return HandleTurnResult(accepted=True, deduped=True)

    channel = get_channel(meeting_id)
    turn_id = _make_turn_id()
    pinned_agent = meeting.get("pinned_persona")

    # 3. Persist user row.
    user_row_id = _persist_user_message(meeting_id, trimmed)

    roster = resolved_opts.roster if resolved_opts.roster is not None else get_roster()
    roster_by_id = {a.id: a for a in roster}
    cancel_flag: dict[str, bool] = {"cancelled": False}
    turn_state: dict[str, bool] = {"anyIncomplete": False}
    _active_cancel_flags[turn_id] = _CancelEntry(meeting_id=meeting_id, flag=cancel_flag)

    channel.emit({
        "type": "turn_start",
        "turnId": turn_id,
        "clientMsgId": client_msg_id,
        "userText": trimmed,
        "userTs": int(time.time()),
        "userTranscriptRowId": user_row_id,
    })

    try:
        # Slash command branch — Phase 5a recognizes only; defers to Phase 5b
        # via system_note to operator.
        slash = parse_slash_command(trimmed)
        if slash is not None:
            channel.emit({
                "type": "system_note",
                "turnId": turn_id,
                "text": (
                    f"Slash command /{slash['cmd']} requires Phase 5b — not yet wired in this build."
                ),
                "tone": "info",
                "dismissable": True,
            })
            channel.emit({"type": "turn_complete", "turnId": turn_id})
            return HandleTurnResult(accepted=True, turn_id=turn_id)

        # 4. Resolve primary (@mention → pinned → sticky → router → social).
        mentions = extract_all_at_mentions(trimmed, roster)
        explicit_mentions: set[str] = set()

        sticky = (
            infer_sticky_addressee(meeting_id, trimmed, roster, user_row_id or 0)
            if (not mentions and not pinned_agent)
            else None
        )

        decision: RouterDecision

        if mentions:
            primary = mentions[0]
            interveners = [m for m in mentions[1:3] if m != primary]
            if len(mentions) > 3:
                skipped = mentions[3:]
                channel.emit({
                    "type": "system_note",
                    "turnId": turn_id,
                    "text": f"3 of {len(mentions)} mentioned agents will respond. Skipped: {', '.join('@' + s for s in skipped)}.",
                    "tone": "info",
                    "dismissable": True,
                })
            explicit_mentions.add(primary)
            for m in interveners:
                explicit_mentions.add(m)
            channel.emit({
                "type": "status_update",
                "turnId": turn_id,
                "phase": "starting",
                "label": f"Starting {roster_by_id.get(primary).name if primary in roster_by_id else primary}…",
                "agentId": primary,
            })
            reason = (
                f"explicit @{primary} + {', '.join('@' + m for m in interveners)}"
                if interveners
                else f"explicit @{primary}"
            )
            decision = RouterDecision(primary=primary, interveners=interveners, reason=reason, router_degraded=False)
        elif pinned_agent and pinned_agent in roster_by_id:
            channel.emit({
                "type": "status_update",
                "turnId": turn_id,
                "phase": "starting",
                "label": f"Starting {roster_by_id[pinned_agent].name}…",
                "agentId": pinned_agent,
            })
            decision = RouterDecision(
                primary=pinned_agent,
                interveners=[],
                reason=f"pinned {pinned_agent}",
                router_degraded=False,
            )
        elif sticky:
            channel.emit({
                "type": "status_update",
                "turnId": turn_id,
                "phase": "starting",
                "label": f"Starting {roster_by_id.get(sticky).name if sticky in roster_by_id else sticky}…",
                "agentId": sticky,
            })
            decision = RouterDecision(
                primary=sticky,
                interveners=[],
                reason=f"sticky from prior @{sticky}",
                router_degraded=False,
            )
        elif is_acknowledgment(trimmed):
            channel.emit({
                "type": "router_decision",
                "turnId": turn_id,
                "primary": None,
                "interveners": [],
                "reason": "acknowledgment — silent",
            })
            channel.emit({"type": "turn_complete", "turnId": turn_id})
            return HandleTurnResult(accepted=True, turn_id=turn_id)
        elif is_greeting(trimmed):
            channel.emit({
                "type": "status_update",
                "turnId": turn_id,
                "phase": "starting",
                "label": "Starting Main…",
                "agentId": "default",
            })
            decision = RouterDecision(
                primary="default",
                interveners=[],
                reason="greeting → default",
                router_degraded=False,
            )
        else:
            channel.emit({
                "type": "status_update",
                "turnId": turn_id,
                "phase": "routing",
                "label": "Routing…",
            })
            router_ctx = _build_router_context(meeting_id, trimmed, roster, pinned_agent, turn_id)
            try:
                decision = await route_message(router_ctx)
            except Exception as exc:  # noqa: BLE001 — router itself shouldn't throw, but be defensive
                logger.warning("cabinet router unexpected error: %s", _redact(str(exc)))
                decision = router_fallback(router_ctx)

        channel.emit({
            "type": "router_decision",
            "turnId": turn_id,
            "primary": decision.primary,
            "interveners": decision.interveners,
            "reason": decision.reason,
        })

        if decision.router_degraded:
            channel.emit({
                "type": "system_note",
                "turnId": turn_id,
                "text": "Routing fell back to the default agent.",
                "tone": "warn",
                "dismissable": True,
            })

        if decision.primary is None:
            # Silent for social/short messages; otherwise hint operator.
            if not is_social_message(trimmed) and len(trimmed) > 3:
                channel.emit({
                    "type": "system_note",
                    "turnId": turn_id,
                    "text": "Not sure who should take this — try @<agent> or add a specific detail.",
                    "tone": "info",
                    "dismissable": True,
                })
            channel.emit({"type": "turn_complete", "turnId": turn_id})
            return HandleTurnResult(accepted=True, turn_id=turn_id)

        if cancel_flag["cancelled"]:
            channel.emit({
                "type": "turn_aborted",
                "turnId": turn_id,
                "clearedAgents": [decision.primary, *decision.interveners],
            })
            return HandleTurnResult(accepted=True, turn_id=turn_id)

        # 5. Run primary.
        primary_persona = roster_by_id.get(decision.primary)
        if primary_persona is None:
            channel.emit({
                "type": "error",
                "turnId": turn_id,
                "message": f"primary persona '{decision.primary}' not in roster",
                "recoverable": False,
            })
            channel.emit({"type": "turn_complete", "turnId": turn_id})
            return HandleTurnResult(accepted=True, turn_id=turn_id)

        channel.emit({
            "type": "agent_selected",
            "turnId": turn_id,
            "agentId": decision.primary,
            "role": "primary",
        })
        for iid in decision.interveners:
            channel.emit({
                "type": "agent_selected",
                "turnId": turn_id,
                "agentId": iid,
                "role": "intervener",
            })

        primary_text = await _run_agent_turn(_RunAgentArgs(
            persona_id=decision.primary,
            meeting_id=meeting_id,
            user_text=trimmed,
            role="primary",
            turn_id=turn_id,
            channel=channel,
            cancel_flag=cancel_flag,
            turn_state=turn_state,
            persona=primary_persona,
            roster=roster,
        ))

        # M2: schedule background title generation if this looks like the
        # first user/assistant exchange (entry_count was 0 before this turn).
        # Both messages are passed; lane-router/Haiku produces 3-7 word title.
        if primary_text and (meeting.get("entry_count") or 0) == 0:
            try:
                schedule_title_generation(meeting_id, trimmed, primary_text)
            except Exception as exc:  # noqa: BLE001
                logger.debug("cabinet title schedule failed: %s", _redact(str(exc)))

        # 6. Intervener loop.
        if decision.interveners and not cancel_flag["cancelled"]:
            channel.emit({
                "type": "status_update",
                "turnId": turn_id,
                "phase": "checking_interveners",
                "label": "Checking if anyone wants to add…",
            })

        for candidate_id in decision.interveners:
            if cancel_flag["cancelled"]:
                break
            candidate = roster_by_id.get(candidate_id)
            if candidate is None:
                continue
            is_explicit = candidate_id in explicit_mentions

            if not is_explicit and not primary_text:
                channel.emit({
                    "type": "intervention_skipped",
                    "turnId": turn_id,
                    "agentId": candidate_id,
                    "role": "intervener",
                    "reason": "primary produced no reply",
                })
                continue

            agent_prompt = trimmed
            if not is_explicit:
                gate = await intervention_gate(InterventionContext(
                    user_text=trimmed,
                    primary_agent_id=decision.primary,
                    primary_reply=primary_text,
                    candidate_agent_id=candidate_id,
                    candidate_agent_description=candidate.description,
                    meeting_id=meeting_id,
                    turn_id=turn_id,
                ))
                if cancel_flag["cancelled"]:
                    break
                if not gate.speak:
                    channel.emit({
                        "type": "intervention_skipped",
                        "turnId": turn_id,
                        "agentId": candidate_id,
                        "reason": "gate declined",
                    })
                    continue
                primary_name = (
                    roster_by_id[decision.primary].name
                    if decision.primary in roster_by_id
                    else decision.primary
                )
                trunc_reply = gate.reply[:400] + ("…" if len(gate.reply) > 400 else "")
                agent_prompt = (
                    f"{trimmed}\n\n[You were pulled in to add your angle. The primary just spoke "
                    "(see Meeting so far above). You previously drafted a short add: "
                    f'"{trunc_reply}". Keep your reply to 1-3 conversational sentences building '
                    f"on that angle, not repeating what {primary_name} said.]"
                )

            channel.emit({
                "type": "status_update",
                "turnId": turn_id,
                "phase": "starting",
                "label": f"{candidate.name} is chiming in…",
                "agentId": candidate_id,
            })

            await _run_agent_turn(_RunAgentArgs(
                persona_id=candidate_id,
                meeting_id=meeting_id,
                user_text=agent_prompt,
                role="intervener",
                turn_id=turn_id,
                channel=channel,
                cancel_flag=cancel_flag,
                turn_state=turn_state,
                persona=candidate,
                roster=roster,
            ))

        # 7. turn_complete vs turn_aborted.
        cancelled_midway = cancel_flag["cancelled"] and turn_state["anyIncomplete"]
        if cancelled_midway:
            channel.emit({
                "type": "turn_aborted",
                "turnId": turn_id,
                "clearedAgents": [decision.primary, *decision.interveners],
            })
        else:
            channel.emit({"type": "turn_complete", "turnId": turn_id})
        return HandleTurnResult(accepted=True, turn_id=turn_id)

    except kill_switches.KillSwitchDisabled:
        # Caller (HTTP handler) catches and returns 503; re-raise.
        raise
    except Exception as exc:  # noqa: BLE001 — defensive top-level
        logger.error("cabinet handle_text_turn crashed: %s", _redact(str(exc)))
        channel.emit({
            "type": "error",
            "turnId": turn_id,
            "message": str(exc),
            "recoverable": True,
        })
        channel.emit({"type": "turn_complete", "turnId": turn_id})
        return HandleTurnResult(accepted=True, turn_id=turn_id)
    finally:
        _active_cancel_flags.pop(turn_id, None)


__all__ = [
    "HandleTurnOptions",
    "HandleTurnResult",
    "RosterAgent",
    "STICKY_MAX_AGE_S",
    "STICKY_MAX_TEXT_LEN",
    "cancel_meeting_turns",
    "cancel_turn",
    "extract_all_at_mentions",
    "get_active_turn_ids",
    "get_roster",
    "handle_text_turn",
    "infer_sticky_addressee",
    "is_acknowledgment",
    "is_greeting",
    "is_social_message",
    "is_warmup_done",
    "parse_slash_command",
    "prewarm_agent_sdks",
    "wait_for_meeting_turns_idle",
    "warmup_meeting",
]


# Silence unused-import lint warnings — these are intentional re-exports
# at module level for tests + Phase 5b shim consumers.
_ = (_channels_mod, GATE_TIMEOUT_S, os)
