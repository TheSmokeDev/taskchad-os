"""Read-only portfolio brief for ``/cofounder brief`` (autonomy T4).

The composite the operator (and the engine, through the prefetch-only
``cofounder`` intent) reads to answer "what are we building?": today's agenda
lines with their live delegation status, the un-acked assignments still in
flight, and the last 24h of delegation outcomes. Distinct from the SESSION
opening brief (``cognition/proactive_brief.py``) — that one decides whether to
wake a conversation; this one just renders portfolio state on request.

Every part is an independent Rule-2 physical read through the surface that
already owns it (``delegate.render_agenda_status`` for the agenda, the mailbox
service for in-flight, the append-only delegation ledger for outcomes) — never
a second parser.

Read-only contract: this module never mutates and never raises. A missing
agenda, an unreadable ledger, or a dead mailbox degrades to the parts that
worked; the trailing command echo always survives truncation, because the
mutating path stays a typed command the operator has to name.

The in-flight read fails OPEN (a persona whose mailbox errors is omitted) —
it is display, not a guard. ``delegate._check_caps`` stays fail-CLOSED and is
the only thing that gates a send.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_BRIEF_MAX_CHARS = 2400
RECENT_WINDOW_HOURS = 24
MAX_RECENT_ROWS = 8
AUTOPILOT_APPROVER = "cofounder-autopilot"

_OUTCOME_MARKS = {
    "sent": "✅",
    "refused_killswitch": "🚫",
    "scope-denied": "🚫",
    "capped": "🧱",
    "busy": "⏳",
    "already-delegated": "↩️",
    "no-agenda": "▫️",
    "bad-line": "▫️",
    "error": "❌",
}

COMMAND_ECHO = (
    "Read-only. Mutations stay typed: `/cofounder run <n>` (delegate a line), "
    "`/cofounder steer <slug> <text>`, `/cofounder pause <slug>`, "
    "`/cofounder show <slug>`."
)


def render_cofounder_brief(
    *,
    date: str | None = None,
    max_chars: int = DEFAULT_BRIEF_MAX_CHARS,
    now: datetime | None = None,
    services: tuple[Any, Any] | None = None,
    audit_path: Path | str | None = None,
) -> str:
    """The chat-ready portfolio brief. Never raises, never mutates."""
    try:
        import config

        day = date or config.now_local().date().isoformat()
        now_utc = now or datetime.now(UTC)

        parts: list[str] = []

        agenda = _agenda_block(day)
        if agenda:
            parts.append(agenda)

        rows, sent_personas = _read_ledger(now_utc, audit_path=audit_path)

        inflight = _inflight_block(sent_personas, services=services)
        if inflight:
            parts.append(inflight)

        recent = _recent_block(rows)
        if recent:
            parts.append(recent)

        body = "\n\n".join(parts)
        if len(body) > max_chars:
            body = body[:max_chars]
            last_newline = body.rfind("\n")
            if last_newline > 0:
                body = body[:last_newline]
            body = body.rstrip() + "\n[brief truncated]"

        return "\n\n".join([f"*Co-Founder brief — {day}*", body, COMMAND_ECHO])
    except Exception as exc:
        logger.exception("cofounder.brief: render failed")
        return (
            f"Could not build the co-founder brief: {type(exc).__name__}: {exc}\n\n"
            + COMMAND_ECHO
        )


def _agenda_block(day: str) -> str:
    """Today's agenda lines with delegation markers, or '' when unreadable."""
    try:
        from cofounder import delegate

        return delegate.render_agenda_status(date=day).strip()
    except Exception:
        logger.warning("cofounder.brief: agenda block failed", exc_info=True)
        return ""


def _read_ledger(
    now_utc: datetime,
    *,
    audit_path: Path | str | None = None,
    window_hours: int = RECENT_WINDOW_HOURS,
) -> tuple[list[dict[str, Any]], list[str]]:
    """One pass over the delegation ledger -> (recent rows, sent personas).

    The persona list is every persona ever ``sent`` an assignment — the exact
    superset of who can still be holding one, so the in-flight read needs no
    persona registry (and no dependency on the agenda pass's roster).
    """
    rows: list[dict[str, Any]] = []
    personas: list[str] = []
    try:
        from cofounder import delegate

        path = Path(delegate._resolve_audit_path(audit_path))
        if not path.is_file():
            return [], []
        cutoff = now_utc - timedelta(hours=window_hours)
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                persona = row.get("persona")
                if (
                    row.get("outcome") == delegate.OUTCOME_SENT
                    and isinstance(persona, str)
                    and persona
                    and persona not in personas
                ):
                    personas.append(persona)
                stamped = _parse_timestamp(row.get("timestamp"))
                if stamped is not None and stamped >= cutoff:
                    rows.append(row)
    except Exception:
        logger.warning("cofounder.brief: ledger read failed", exc_info=True)
    return rows, personas


def _parse_timestamp(value: Any) -> datetime | None:
    """The ledger's UTC ``timestamp`` as an aware datetime, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        stamped = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamped.replace(tzinfo=UTC) if stamped.tzinfo is None else stamped


def _inflight_block(personas: list[str], *, services: tuple[Any, Any] | None) -> str:
    """Un-acked ``cofounder_assignment`` deliveries per persona, or ''."""
    if not personas:
        return ""
    try:
        from cofounder import delegate

        _convoy_service, mailbox_service = services or delegate._build_services()
        counts: list[tuple[str, int]] = []
        for persona in personas:
            try:
                inbox = mailbox_service.get_inbox(persona, msg_type=delegate.MSG_TYPE)
            except Exception:
                logger.debug(
                    "cofounder.brief: inbox read failed for %s; omitted", persona
                )
                continue
            if inbox:
                counts.append((persona, len(inbox)))
        if not counts:
            return ""
        total = sum(count for _persona, count in counts)
        return "\n".join(
            [f"*In flight* — {total} un-acked assignment(s):"]
            + [f"  {persona} — {count}" for persona, count in counts]
        )
    except Exception:
        logger.warning("cofounder.brief: in-flight read failed", exc_info=True)
        return ""


def _recent_block(rows: list[dict[str, Any]]) -> str:
    """Last-24h delegation outcomes: a count summary plus the newest rows."""
    if not rows:
        return f"*Last {RECENT_WINDOW_HOURS}h* — no delegation attempts."
    try:
        tally: dict[str, int] = {}
        for row in rows:
            outcome = str(row.get("outcome") or "unknown")
            tally[outcome] = tally.get(outcome, 0) + 1
        summary = ", ".join(
            f"{count} {outcome}" for outcome, count in sorted(tally.items())
        )
        lines = [f"*Last {RECENT_WINDOW_HOURS}h* — {summary}:"]
        for row in reversed(rows[-MAX_RECENT_ROWS:]):
            lines.append(_recent_line(row))
        return "\n".join(lines)
    except Exception:
        logger.warning("cofounder.brief: outcome render failed", exc_info=True)
        return ""


def _recent_line(row: dict[str, Any]) -> str:
    outcome = str(row.get("outcome") or "unknown")
    mark = _OUTCOME_MARKS.get(outcome, "▫️")
    persona = row.get("persona") or "-"
    by = " (self-assigned)" if row.get("approved_by") == AUTOPILOT_APPROVER else ""
    detail = " ".join(str(row.get("detail") or "").split())[:80]
    suffix = f" — {detail}" if detail and outcome != "sent" else ""
    return f"  {mark} line {row.get('line')} -> {persona}{by}: {outcome}{suffix}"
