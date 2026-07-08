"""Co-founder v2 WS3 — the delegation transport (approval-gated, dormant).

Turns ONE approved agenda line into real assigned work: a convoy (the work
record) plus a typed ``cofounder_assignment`` mailbox message (the delivery)
addressed to a department-head persona. Uses the EXISTING orchestration
service layer directly (the ``thehomie convoy``/``mailbox`` CLI precedent —
SQLite-backed, cross-process safe); no new store, no HTTP hop.

The approval contract (operator resolution #4, 2026-07-05):

- The operator's per-line approval (``/cofounder run <n>`` or the "run it"
  reply) ALWAYS executes — ``COFOUNDER_DELEGATION_ENABLED`` gates only
  AUTONOMOUS delegation, which nothing exercises yet (that flip is the
  end-state, after propose-only earns trust).
- The ``cofounder_delegation`` kill switch
  (``HOMIE_KILLSWITCH_COFOUNDER_DELEGATION``) is the emergency stop for the
  WHOLE surface — it refuses approved lines too, with counted refusals.

Fail-closed grain (Rule 4 — authorization granularity == storage granularity):

- The persona-side grant is the ``delegation:`` block in the persona's own
  config.yaml. No block -> the persona is NOT a delegation target, period.
- Repo-scoped work additionally requires the item's repo slug in
  ``delegation.repos``. The check runs at SEND time against the LIVE config
  (never a cached matrix); WS4's claim side re-checks at claim.
- Delegation grants WORK, never new capabilities: the persona's own
  default-deny gates (social writes, dial/text, integration actions) are
  untouched by an assignment.

Caps (both Rule-2 physical-state reads):

- ``COFOUNDER_MAX_ASSIGNMENTS_PER_DAY`` — counted from today's ``sent``
  audit rows (the append-only jsonl is the send ledger).
- ``COFOUNDER_MAX_INFLIGHT_PER_PERSONA`` — counted from un-acked
  ``cofounder_assignment`` deliveries in the mailbox DB (delivery status
  IS the in-flight truth until WS4's work loop acks).

Every attempt — sent, refused, denied, capped, invalid — lands one
append-only audit row at ``DATA_DIR/cofounder_delegation.jsonl``.

No exception escapes :func:`run_agenda_line`; every outcome is a
:class:`DelegationResult` whose ``message`` is chat-ready text.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Boot-shim (PRP-7a): persona env overrides must apply BEFORE any
# config-touching import resolves paths.
from personas import apply_persona_override  # noqa: E402

apply_persona_override()

logger = logging.getLogger(__name__)

MSG_TYPE = "cofounder_assignment"
COFOUNDER_AGENT_ID = "cofounder"
AUDIT_FILENAME = "cofounder_delegation.jsonl"

OUTCOME_SENT = "sent"
OUTCOME_REFUSED = "refused_killswitch"
OUTCOME_NO_AGENDA = "no-agenda"
OUTCOME_BAD_LINE = "bad-line"
OUTCOME_ALREADY = "already-delegated"
OUTCOME_SCOPE_DENIED = "scope-denied"
OUTCOME_CAPPED = "capped"
OUTCOME_BUSY = "busy"
OUTCOME_ERROR = "error"

_LOCK_TIMEOUT_S = 5.0


@dataclass
class DelegationResult:
    """What one delegation attempt did. ``message`` is chat-ready text."""

    outcome: str
    message: str
    convoy_id: int | None = None
    message_id: int | None = None
    persona: str | None = None


def run_agenda_line(
    line_number: int,
    *,
    date: str | None = None,
    approved_by: str = "operator",
    settings=None,
    delegation_settings=None,
    services=None,
    now: datetime | None = None,
) -> DelegationResult:
    """Delegate agenda line ``line_number`` (1-based) for ``date`` (today).

    ``services`` is an injectable ``(convoy_service, mailbox_service)``
    pair; ``None`` builds the direct-Python service layer (the CLI shape).
    Never raises.
    """
    try:
        from security import kill_switches  # Rule 3: module-attribute lookup

        try:
            kill_switches.requireEnabled(
                "cofounder_delegation", caller="cofounder.delegate"
            )
        except kill_switches.KillSwitchDisabled:
            _audit(None, line_number, OUTCOME_REFUSED, "kill switch disabled")
            return DelegationResult(
                outcome=OUTCOME_REFUSED,
                message=(
                    "Delegation is stopped by the kill switch "
                    "(`HOMIE_KILLSWITCH_COFOUNDER_DELEGATION`)."
                ),
            )

        import config

        if settings is None:
            settings = config.get_cofounder_settings()
        if delegation_settings is None:
            delegation_settings = config.get_cofounder_delegation_settings()
        if now is None:
            # The canonical operator-local clock (HEARTBEAT_TIMEZONE) — the
            # ledger's local_date and the agenda file name must agree on
            # what "today" means (adversarial-review Critical #1).
            now = config.now_local()
        if date:
            day = date
            agenda_path = _agenda_json_path(settings.projects_dir, day)
        else:
            # Same fallback window as the portfolio digest: between midnight
            # and the morning pass, "run <n>" must target the agenda the
            # digest is SHOWING (yesterday's), not a not-yet-existing today.
            from datetime import timedelta

            day = now.date().isoformat()
            agenda_path = _agenda_json_path(settings.projects_dir, day)
            for offset in range(1, 3):
                if agenda_path.is_file():
                    break
                day = (now.date() - timedelta(days=offset)).isoformat()
                agenda_path = _agenda_json_path(settings.projects_dir, day)
        if not agenda_path.is_file():
            return DelegationResult(
                outcome=OUTCOME_NO_AGENDA,
                message=(
                    f"No machine-readable agenda for {day} (or the 2 days "
                    "prior). Run `uv run python -m cofounder.agenda --force` "
                    "to produce one."
                ),
            )

        # ONE lock spans read -> guards -> send -> stamp so a Telegram
        # double-tap (two overlapping approvals of the same line) serializes:
        # the second holder re-reads status == delegated and no-ops
        # (adversarial-review Critical #2). The stamp helper below assumes
        # the caller holds this lock — file_lock is NOT re-entrant.
        from shared import file_lock

        try:
            with file_lock(agenda_path, timeout=_LOCK_TIMEOUT_S):
                return _locked_delegation(
                    agenda_path,
                    line_number,
                    day,
                    approved_by,
                    delegation_settings,
                    services,
                    now,
                )
        except TimeoutError:
            return DelegationResult(
                outcome=OUTCOME_BUSY,
                message=(
                    f"Another approval for {day} is mid-flight; "
                    "try again in a moment."
                ),
            )
    except Exception as exc:  # the whole-attempt wrap: nothing escapes
        logger.exception("cofounder.delegate: line %d failed", line_number)
        _audit(None, line_number, OUTCOME_ERROR, f"{type(exc).__name__}: {exc}")
        return DelegationResult(
            outcome=OUTCOME_ERROR,
            message=f"Delegation failed: {type(exc).__name__}: {exc}",
        )


def _locked_delegation(
    agenda_path: Path,
    line_number: int,
    day: str,
    approved_by: str,
    delegation_settings,
    services,
    now: datetime,
) -> DelegationResult:
    """The guarded body. The caller holds ``file_lock(agenda_path)``."""
    agenda = _read_agenda_json(agenda_path)
    if agenda is None:
        return DelegationResult(
            outcome=OUTCOME_NO_AGENDA,
            message=f"Agenda {day} exists but is unreadable; regenerate it.",
        )

    items = agenda.get("items") or []
    item = next(
        (i for i in items if isinstance(i, dict) and i.get("n") == line_number),
        None,
    )
    if item is None:
        return DelegationResult(
            outcome=OUTCOME_BAD_LINE,
            message=f"Agenda {day} has no line {line_number} (1-{len(items)}).",
        )
    if item.get("status") == "delegated":
        return DelegationResult(
            outcome=OUTCOME_ALREADY,
            message=(
                f"Line {line_number} was already delegated "
                f"(convoy {item.get('convoy_id', '?')})."
            ),
            persona=item.get("persona"),
        )

    persona = str(item.get("persona") or "")
    repo = item.get("repo")
    scope_error = _check_persona_scope(persona, repo)
    if scope_error:
        _audit(persona, line_number, OUTCOME_SCOPE_DENIED, scope_error, day=day)
        return DelegationResult(
            outcome=OUTCOME_SCOPE_DENIED, message=scope_error, persona=persona
        )

    if services is None:
        services = _build_services()
    convoy_service, mailbox_service = services

    cap_error = _check_caps(mailbox_service, persona, day, delegation_settings)
    if cap_error:
        _audit(persona, line_number, OUTCOME_CAPPED, cap_error, day=day)
        return DelegationResult(
            outcome=OUTCOME_CAPPED, message=cap_error, persona=persona
        )

    task = str(item.get("task") or "")
    convoy, subtask_id = _create_assignment_convoy(
        convoy_service, persona, task, day, line_number
    )

    from orchestration.models import CofounderAssignmentPayload

    payload = CofounderAssignmentPayload(
        subtask_id=subtask_id,
        task=task,
        repo=repo,
        why=str(item.get("why") or ""),
        priority=int(item.get("priority") or 2),
        agenda_ref=f"AGENDA-{day}.md#{line_number}",
        due=None,
        mode=str(item.get("mode") or "draft"),
    )
    message = mailbox_service.send_cofounder_assignment(
        COFOUNDER_AGENT_ID, persona, payload, convoy_id=convoy.id
    )

    _audit(
        persona,
        line_number,
        OUTCOME_SENT,
        task,
        day=day,
        convoy_id=convoy.id,
        message_id=message.id,
        approved_by=approved_by,
    )
    _stamp_item_delegated_locked(
        agenda_path, line_number, convoy.id, message.id, now
    )
    logger.info(
        "cofounder.delegate: line %d -> %s (convoy %s, message %s)",
        line_number,
        persona,
        convoy.id,
        message.id,
    )
    return DelegationResult(
        outcome=OUTCOME_SENT,
        message=(
            f"Delegated line {line_number} to {persona}: {task[:120]}\n"
            f"convoy {convoy.id}, assignment message {message.id}. "
            "The persona work loop (WS4) claims it from the mailbox."
        ),
        convoy_id=convoy.id,
        message_id=message.id,
        persona=persona,
    )


def render_agenda_status(
    *, date: str | None = None, settings=None
) -> str:
    """Chat-ready listing of the day's agenda lines with live status."""
    try:
        import config

        if settings is None:
            settings = config.get_cofounder_settings()
        day = date or config.now_local().date().isoformat()
        _, agenda = _load_agenda_json(settings.projects_dir, day)
        if agenda is None:
            return (
                f"No agenda for {day}. The morning pass produces one "
                "(or run `uv run python -m cofounder.agenda --force`)."
            )
        lines = [f"Co-Founder agenda {day}:"]
        summary = str(agenda.get("summary") or "").strip()
        if summary:
            lines += [summary, ""]
        markers = {
            "delegated": "⏳",
            "done": "✅",
            "dispatched": "🚀",
            "failed": "❌",
            "refused": "🚫",
        }
        for item in agenda.get("items") or []:
            if not isinstance(item, dict):
                continue
            status = item.get("status", "proposed")
            marker = markers.get(status, "▫️")
            target = f" -> {item['repo']}" if item.get("repo") else ""
            mode = item.get("mode", "draft")
            lines.append(
                f"{marker} {item.get('n')}. [P{item.get('priority', 2)}|{mode}] "
                f"{item.get('persona')}{target}: {item.get('task')}"
            )
        lines.append("")
        lines.append("Approve a line with `/cofounder run <n>`.")
        return "\n".join(lines)
    except Exception as exc:
        logger.exception("cofounder.delegate: agenda render failed")
        return f"Could not read the agenda: {type(exc).__name__}: {exc}"


# =============================================================================
# Gates and lookups.
# =============================================================================


def _check_persona_scope(persona: str, repo: Any) -> str | None:
    """The Rule-4 send-side grain check. Returns a chat-ready error or None.

    Fail-closed at every seam: unknown persona, unreadable config, missing
    ``delegation:`` block, or an ungranted repo slug all refuse. The config
    is read LIVE per attempt (Rule 2 — never a cached matrix).
    """
    if not persona:
        return "That agenda line has no persona — nothing to delegate to."
    try:
        from personas import core as personas_core
        from personas import services as personas_services

        # Defense-in-depth: the agenda validator only writes exact registry
        # ids, but a tampered/corrupted artifact must not reach the config
        # loader with a traversal-shaped id (adversarial review).
        personas_core.validate_persona_name(persona)
        cfg = personas_services.load_persona_config(persona)
    except Exception as exc:
        return (
            f"Persona `{persona}` is not a delegable target "
            f"(config unreadable: {type(exc).__name__})."
        )
    delegation = cfg.get("delegation")
    if not isinstance(delegation, dict):
        return (
            f"Persona `{persona}` has no `delegation:` grant in its "
            "config.yaml — it cannot receive cofounder assignments "
            "(fail-closed). Grant it with e.g.\n"
            "delegation:\n  repos: [YourProduct]"
        )
    if repo is not None:
        repos = delegation.get("repos")
        granted = (
            [r for r in repos if isinstance(r, str)]
            if isinstance(repos, list)
            else []
        )
        if str(repo) not in granted:
            return (
                f"Persona `{persona}` is not granted repo `{repo}` "
                f"(delegation.repos = {granted or '[]'}). Fail-closed."
            )
    return None


def _check_caps(
    mailbox_service, persona: str, day: str, delegation_settings
) -> str | None:
    """Daily + per-persona in-flight caps. Returns a chat-ready error or None.

    Daily count = today's ``sent`` audit rows (the send ledger). In-flight =
    un-acked ``cofounder_assignment`` deliveries for the persona (physical
    mailbox state — WS4's ack is what releases a slot).
    """
    sent_today = _count_sent_today(day)
    if sent_today >= delegation_settings.max_assignments_per_day:
        return (
            f"Daily delegation cap reached ({sent_today}/"
            f"{delegation_settings.max_assignments_per_day}). "
            "Raise COFOUNDER_MAX_ASSIGNMENTS_PER_DAY or wait for tomorrow."
        )
    try:
        inbox = mailbox_service.get_inbox(persona, msg_type=MSG_TYPE)
    except Exception:
        # Fail CLOSED on ANY inbox failure — including a future get_inbox
        # signature drift (TypeError). A cap that silently vanishes is the
        # opposite of conservative (adversarial review).
        logger.warning(
            "cofounder.delegate: inbox read failed; refusing conservatively",
            exc_info=True,
        )
        return (
            f"Cannot read `{persona}`'s mailbox to verify the in-flight cap; "
            "refusing conservatively. Check the orchestration DB."
        )
    inflight = len(inbox)
    if inflight >= delegation_settings.max_inflight_per_persona:
        return (
            f"`{persona}` already has {inflight} un-acked assignment(s) "
            f"(cap {delegation_settings.max_inflight_per_persona}). "
            "Wait for the work loop to claim/ack, or raise "
            "COFOUNDER_MAX_INFLIGHT_PER_PERSONA."
        )
    return None


def _count_sent_today(day: str, audit_path: Path | str | None = None) -> int:
    """The day's ``sent`` rows in the delegation ledger (fail-open to 0).

    Matches on the row's ``local_date`` field — stamped from the SAME
    operator-local clock that names the agenda file — never the UTC
    ``timestamp`` (a prefix match against a local day leaks the cap for the
    whole evening west of UTC; adversarial-review Critical #1).
    """
    try:
        path = _resolve_audit_path(audit_path)
        if not path.is_file():
            return 0
        count = 0
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("outcome") == OUTCOME_SENT and row.get("local_date") == day:
                    count += 1
        return count
    except Exception:
        logger.warning("cofounder.delegate: audit count failed", exc_info=True)
        return 0


# =============================================================================
# Transport plumbing.
# =============================================================================


def _build_services():
    """Direct-Python orchestration services (the thehomie-CLI shape)."""
    import config
    from orchestration.convoy_service import ConvoyService
    from orchestration.db import OrchestrationDB
    from orchestration.mailbox_service import MailboxService

    db = OrchestrationDB(config.ORCHESTRATION_DB_PATH)
    return ConvoyService(db), MailboxService(db)


def _create_assignment_convoy(
    convoy_service, persona: str, task: str, day: str, line_number: int
):
    """One convoy with one subtask for the assignment. Returns (convoy, sid)."""
    from orchestration.models import CreateConvoyInput, CreateSubtaskInput

    created = convoy_service.create_convoy(
        CreateConvoyInput(
            title=f"[cofounder] {task[:80]}",
            description=(
                f"Cofounder delegation — agenda AGENDA-{day}.md line "
                f"{line_number}, assigned to {persona}."
            ),
            created_by=COFOUNDER_AGENT_ID,
            subtasks=[
                CreateSubtaskInput(
                    title=task[:120],
                    description=f"Assigned to {persona} (agenda {day} #{line_number}).",
                    assigned_agent_id=persona,
                )
            ],
        )
    )
    subtask_id = created.subtasks[0].id if created.subtasks else 0
    return created.convoy, subtask_id


def _agenda_json_path(projects_dir: Path | str, day: str) -> Path:
    from cofounder.agenda import AGENDAS_SUBDIR

    return Path(projects_dir) / AGENDAS_SUBDIR / f"AGENDA-{day}.json"


def _read_agenda_json(path: Path) -> dict[str, Any] | None:
    """The agenda dict, or None when unreadable. No locking (caller's job)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        logger.warning("cofounder.delegate: agenda json unreadable", exc_info=True)
        return None


def _load_agenda_json(
    projects_dir: Path | str, day: str
) -> tuple[Path, dict[str, Any] | None]:
    """The day's machine-readable agenda, or (path, None) when absent.

    Unlocked read — for READ-ONLY surfaces (:func:`render_agenda_status`).
    The delegation path reads under its own lock instead.
    """
    path = _agenda_json_path(projects_dir, day)
    if not path.is_file():
        return path, None
    return path, _read_agenda_json(path)


def _stamp_item_delegated_locked(
    agenda_path: Path,
    line_number: int,
    convoy_id: int | None,
    message_id: int | None,
    now: datetime,
) -> None:
    """Stamp the item delegated. The CALLER holds ``file_lock(agenda_path)``
    (the lock is not re-entrant — acquiring here would deadlock).

    Best-effort: the assignment is already sent, so a stamp failure only
    costs the already-delegated guard for a LATER attempt (the audit ledger
    still shows the first send). Never fails the delegation.
    """
    try:
        from cofounder import project_model

        data = json.loads(agenda_path.read_text(encoding="utf-8"))
        for item in data.get("items") or []:
            if isinstance(item, dict) and item.get("n") == line_number:
                item["status"] = "delegated"
                item["convoy_id"] = convoy_id
                item["message_id"] = message_id
                item["delegated_at"] = now.isoformat(timespec="seconds")
                break
        project_model._atomic_write(agenda_path, json.dumps(data, indent=2))
    except Exception:
        logger.warning("cofounder.delegate: status stamp failed", exc_info=True)


# =============================================================================
# Audit ledger.
# =============================================================================


def build_arg_parser() -> "argparse.ArgumentParser":
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m cofounder.delegate",
        description=(
            "Delegate one approved agenda line (the same gated path as "
            "/cofounder run <n> — kill switch, scope, caps, audit all apply)."
        ),
    )
    parser.add_argument("action", choices=["run"], help="only 'run' exists")
    parser.add_argument("line", type=int, help="agenda line number (1-based)")
    parser.add_argument(
        "--date", default=None, help="agenda date (default: latest/today)"
    )
    parser.add_argument(
        "--by",
        default="operator-chat-confirm",
        help="who approved (lands in the audit ledger's approved_by)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI seam for the confirm-then-do chat flow: the co-founder OFFERS a
    line in conversation, the operator confirms in plain words, and the
    engine executes THIS command — one auditable entrypoint, byte-identical
    gates to ``/cofounder run <n>``."""
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    result = run_agenda_line(args.line, date=args.date, approved_by=args.by)
    print(result.message)
    return 1 if result.outcome == OUTCOME_ERROR else 0


def _resolve_audit_path(audit_path: Path | str | None = None) -> Path:
    if audit_path is not None:
        return Path(audit_path)
    import config

    return Path(config.DATA_DIR) / AUDIT_FILENAME


def _audit(
    persona: str | None,
    line_number: int,
    outcome: str,
    detail: str,
    *,
    day: str | None = None,
    convoy_id: int | None = None,
    message_id: int | None = None,
    approved_by: str | None = None,
    audit_path: Path | str | None = None,
) -> None:
    """One append-only ledger row per attempt (best-effort, never raises).

    ``local_date`` carries the operator-local day the attempt belongs to —
    the daily-cap key. The UTC ``timestamp`` stays for cross-ledger
    correlation but is never used for day math.
    """
    try:
        path = _resolve_audit_path(audit_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
            "local_date": day,
            "integration": "cofounder",
            "action": "delegate",
            "persona": persona,
            "line": line_number,
            "outcome": outcome,
            "detail": detail[:200],
            "convoy_id": convoy_id,
            "message_id": message_id,
            "approved_by": approved_by,
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception as exc:
        logger.warning("cofounder.delegate: audit write failed (%s)", exc)


if __name__ == "__main__":
    raise SystemExit(main())
