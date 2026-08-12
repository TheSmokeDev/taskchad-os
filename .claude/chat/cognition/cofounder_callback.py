"""The co-founder's own callback on outcomes the operator has not seen yet.

The autopilot works overnight through the existing gates; this renders what it
DID into the operator's next conversation, once. Eligibility is a physical
fact: autopilot ``sent`` rows and ``scope-denied`` refusals in the append-only
delegation ledger, plus terminal agenda-JSON flips stamped by ``report.py``
(``reported_at``) — the same two sources the session brief counts, read the
same way (``normalize_physical_timestamp`` stays the one timestamp owner).

Dedup is a durable watermark at ``STATE_DIR/cofounder-callback-state.json``
advanced on build (the crypto-plays claim semantics): an outcome is surfaced
exactly once, and a first run with no watermark seeds it and stays silent
rather than dumping five weeks of history. The watermark is this module's own
state — it never reads or writes session-brief state, so brief/callback
overlap is possible and accepted by design.

Mutations stay behind typed commands: this renderer echoes the exact
``/cofounder`` commands and never maps conversation onto an action. Any
failure returns a bare turn with a visible receipt.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from cognition.proactive_brief import normalize_physical_timestamp  # noqa: E402

STATE_FILE_NAME = "cofounder-callback-state.json"
LEDGER_NAME = "cofounder_delegation.jsonl"
# Local mirror of cofounder.agenda.AGENDAS_SUBDIR — importing that module runs
# the persona boot-shim (env overrides), which the chat path must not trigger.
AGENDAS_SUBDIR = "agendas"
AUTOPILOT_APPROVER = "cofounder-autopilot"
OUTCOME_SENT = "sent"
OUTCOME_SCOPE_DENIED = "scope-denied"
TERMINAL_STATUSES = frozenset({"done", "failed"})
# Bounds the completion scan to report.py's archon-poll lookback window
# (COFOUNDER_REPORT_POLL_DAYS default) — older agendas can no longer flip.
AGENDA_SCAN_FILES = 7
MAX_PER_GROUP = 5

HEADER = "# Co-Founder Callback (private context — voice once, in your own voice)"
COMMAND_ECHO = (
    "The operator steers with these exact commands — echo one only when it "
    "fits: `/cofounder show <slug>`, `/cofounder steer <slug> <text>`, "
    "`/cofounder pause <slug>`, `/cofounder run <n>`."
)


def read_callback_watermark(*, state_dir: Path | str | None = None) -> datetime | None:
    """The instant through which outcomes were already surfaced, or None.

    Missing or corrupt state -> None (the caller seeds it). Never raises.
    """
    try:
        if state_dir is None:
            from config import STATE_DIR

            state_dir = STATE_DIR
        path = Path(state_dir) / STATE_FILE_NAME
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return normalize_physical_timestamp(data.get("last_seen"))
    except Exception:
        return None


def write_callback_watermark(
    last_seen: datetime,
    *,
    state_dir: Path | str | None = None,
    now: datetime | None = None,
) -> None:
    """Atomically advance the watermark (fail-open, never raises).

    A failed write costs at most one repeated callback — the opposite trade
    (advancing before the render) would silently eat an outcome.
    """
    try:
        if state_dir is None:
            from config import STATE_DIR

            state_dir = STATE_DIR
        if now is None:
            now = datetime.now()
        root = Path(state_dir)
        root.mkdir(parents=True, exist_ok=True)
        path = root / STATE_FILE_NAME
        payload = {
            "last_seen": last_seen.isoformat(),
            "updated_at": now.isoformat(),
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass


def build_cofounder_callback(
    *,
    state_dir: Path | str | None = None,
    delegation_ledger_file: Path | str | None = None,
    agenda_dir: Path | str | None = None,
    now: datetime | None = None,
    settings=None,
) -> tuple[str, dict[str, Any]]:
    """Render the un-surfaced co-founder outcomes, or "" with a reason."""

    decision: dict[str, Any] = {
        "fired": False,
        "reason": "gate_closed",
        "delegations": 0,
        "completions": 0,
        "blockers": 0,
    }
    try:
        if settings is None:
            from config import get_cofounder_callback_settings

            settings = get_cofounder_callback_settings()
        if not settings.enabled:
            decision["reason"] = "disabled"
            return "", decision

        from security import kill_switches

        if kill_switches.is_disabled("cofounder"):
            decision["reason"] = "kill_switch"
            return "", decision

        if now is None:
            now = datetime.now()
        watermark = read_callback_watermark(state_dir=state_dir)
        if watermark is None:
            # Nothing here records what a PREVIOUS install already said, so a
            # cold start seeds the boundary and stays quiet rather than
            # replaying the whole ledger into one turn.
            write_callback_watermark(now, state_dir=state_dir, now=now)
            decision["reason"] = "initialized"
            return "", decision

        delegations: list[dict[str, str]] = []
        blockers: list[dict[str, str]] = []
        try:
            if delegation_ledger_file is None:
                from config import DATA_DIR

                delegation_ledger_file = Path(DATA_DIR) / LEDGER_NAME
            delegations, blockers = _read_ledger_events(
                Path(delegation_ledger_file), watermark
            )
        except Exception:
            delegations, blockers = [], []

        completions: list[dict[str, str]] = []
        try:
            if agenda_dir is None:
                from config import get_cofounder_settings

                agenda_dir = (
                    Path(get_cofounder_settings().projects_dir) / AGENDAS_SUBDIR
                )
            completions = _read_completions(Path(agenda_dir), watermark)
        except Exception:
            completions = []

        decision.update(
            delegations=len(delegations),
            completions=len(completions),
            blockers=len(blockers),
        )
        events = delegations + completions + blockers
        if not events:
            decision["reason"] = "no_new_outcomes"
            return "", decision

        block = _render(delegations, completions, blockers)
        # Advance past EVERY event read this build, including any the group
        # caps dropped — the "+N more" line is their receipt, and a watermark
        # that lagged the caps would replay the same overflow every turn.
        latest = max(
            (normalize_physical_timestamp(event["at"]) or watermark)
            for event in events
        )
        write_callback_watermark(latest, state_dir=state_dir, now=now)
        decision.update(fired=True, reason="fired")
        return block, decision
    except Exception as exc:
        decision["reason"] = "error"
        print(f"[cofounder_callback] non-blocking failure: {exc!r}", flush=True)
        return "", decision


def _read_ledger_events(
    path: Path, watermark: datetime
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """One pass over the delegation ledger -> (self-assignments, blockers).

    Only ``approved_by="cofounder-autopilot"`` sends count: a line the
    operator approved himself was already surfaced by the conversation that
    approved it. ``scope-denied`` rows are the actionable blocker — the
    persona is missing the grant the send-side Rule-4 check demands.
    """
    delegations: list[dict[str, str]] = []
    blockers: list[dict[str, str]] = []
    if not path.is_file():
        return delegations, blockers
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        outcome = row.get("outcome")
        if outcome not in (OUTCOME_SENT, OUTCOME_SCOPE_DENIED):
            continue
        approver = str(row.get("approved_by") or "")
        if outcome == OUTCOME_SENT and approver != AUTOPILOT_APPROVER:
            continue
        stamped = normalize_physical_timestamp(row.get("timestamp"))
        if stamped is None or stamped <= watermark:
            continue
        event = {
            "at": stamped.isoformat(),
            "line": str(row.get("line") if row.get("line") is not None else "?"),
            "persona": str(row.get("persona") or "?"),
            "detail": str(row.get("detail") or ""),
        }
        if outcome == OUTCOME_SENT:
            delegations.append(event)
        else:
            blockers.append(event)
    return delegations, blockers


def _read_completions(agenda_dir: Path, watermark: datetime) -> list[dict[str, str]]:
    """Agenda lines flipped to a terminal status after the watermark.

    ``reported_at`` is stamped by report.py's single flip site on every status
    change, so there is no separate completion clock to read.
    """
    events: list[dict[str, str]] = []
    for path in sorted(agenda_dir.glob("AGENDA-*.json"), reverse=True)[
        :AGENDA_SCAN_FILES
    ]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for item in data.get("items") or []:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "").strip().lower()
            if status not in TERMINAL_STATUSES:
                continue
            flipped = normalize_physical_timestamp(item.get("reported_at"))
            if flipped is None or flipped <= watermark:
                continue
            events.append(
                {
                    "at": flipped.isoformat(),
                    "line": str(item.get("n") if item.get("n") is not None else "?"),
                    "persona": str(item.get("persona") or "?"),
                    "status": status,
                    "detail": str(item.get("result_summary") or item.get("task") or ""),
                }
            )
    return events


def _sanitize_line(text: str, max_chars: int) -> str:
    """Deterministic single-line sanitizer (the proactive_brief shape).

    Ledger detail and agenda summaries are model-written text on their way
    into a prompt — strip control chars, quoting, and collapse whitespace
    before they get there.
    """
    s = str(text or "")
    s = s.replace("`", "").replace('"', "").replace("'", "")
    s = "".join(" " if (ord(ch) < 32 or ord(ch) == 127) else ch for ch in s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_chars:
        cut = s[:max_chars]
        boundary = cut.rfind(" ")
        if boundary > 0:
            cut = cut[:boundary]
        s = cut.strip()
    return s


def _newest_first(events: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(events, key=lambda event: event.get("at", ""), reverse=True)


def _render(
    delegations: list[dict[str, str]],
    completions: list[dict[str, str]],
    blockers: list[dict[str, str]],
) -> str:
    """Deterministic render: newest first inside each group, capped per group."""
    lines: list[str] = []
    dropped = 0
    for event in _newest_first(delegations)[:MAX_PER_GROUP]:
        line = _sanitize_line(event.get("line", "") or "?", 8)
        persona = _sanitize_line(event.get("persona", "") or "?", 40)
        detail = _sanitize_line(event.get("detail", "") or "(no detail)", 120)
        lines.append(f"- self-assigned line {line} to {persona}: {detail}")
    for event in _newest_first(completions)[:MAX_PER_GROUP]:
        line = _sanitize_line(event.get("line", "") or "?", 8)
        persona = _sanitize_line(event.get("persona", "") or "?", 40)
        detail = _sanitize_line(event.get("detail", "") or "(no detail)", 120)
        status = _sanitize_line(event.get("status", "") or "?", 16)
        lines.append(f"- line {line} ({persona}) {status}: {detail}")
    for event in _newest_first(blockers)[:MAX_PER_GROUP]:
        line = _sanitize_line(event.get("line", "") or "?", 8)
        persona = _sanitize_line(event.get("persona", "") or "?", 40)
        lines.append(f"- {persona} needs a delegation grant to take line {line}")
    for group in (delegations, completions, blockers):
        dropped += max(0, len(group) - MAX_PER_GROUP)
    if dropped:
        lines.append(f"- (+{dropped} more outcome(s) not listed)")
    return "\n".join(
        [
            HEADER,
            "Work you ran on your own since the last time you reported in:",
            *lines,
            "",
            "Report it the way a co-founder gives a status — short, first "
            "person, no tool narration. Use only the outcomes above: do not "
            "invent a deliverable, a status, or a next step. " + COMMAND_ECHO,
        ]
    )
