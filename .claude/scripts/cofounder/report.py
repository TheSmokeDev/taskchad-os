"""Co-founder v2 WS5 — the reporting loop (results in, statuses up, checkout).

Run manually (testable without a heartbeat):

    cd .claude/scripts && uv run python -m cofounder.report [--test]

The last slice of the delegation circle, riding the heartbeat and fully
DETERMINISTIC (zero LLM calls):

1. **Ingest** — claim the personas' typed ``cofounder_result`` messages
   addressed to the cofounder (typed claim + the same stale-claim recovery
   the work loop uses), flip the matching agenda JSON line
   (``delegated -> done | failed | refused | dispatched``, locked RMW,
   result details stamped on the item), fail the convoy subtask for
   failed/refused results (the work loop already completed done ones), ack.
2. **Poll** — for recent agenda lines stuck at ``dispatched`` (code-mode
   Archon runs), one read-only archon.db row read each (v1's
   ``fetch_run_row``); finished runs flip to ``done``/``failed`` and
   complete/fail their subtasks.
3. **Intraday pulse** — when anything changed this tick, ONE batch card
   through the gated ``cofounder.notify`` sender (buttons off).
4. **EOD checkout** — once daily on/after ``COFOUNDER_CHECKOUT_HOUR``
   (local), one deterministic day-summary card: agenda lines by status,
   deliverables produced, delegations spent against the daily cap
   (operator resolution #3: morning agenda + intraday awareness + EOD
   checkout — "a real co-founder / executive assistant").

The Session Opening Brief picks the outcomes up for free: the work loop's
daily-log lines and the vault deliverables already feed reflection and
recall (the compounding loop).

Gate order: shared ``cofounder_delegation`` kill switch ->
``COFOUNDER_REPORT_ENABLED`` (default false — dormant family). Dry runs
never claim and never send.

No exception escapes :func:`run_report_pass`; one broken result never
stops the others.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# Boot-shim (PRP-7a): persona env overrides must apply BEFORE any
# config-touching import resolves paths.
from personas import apply_persona_override  # noqa: E402

apply_persona_override()

logger = logging.getLogger(__name__)

MSG_TYPE_RESULT = "cofounder_result"
REPORT_LEVEL = "report"

OUTCOME_COMPLETED = "completed"
OUTCOME_DISABLED = "disabled"
OUTCOME_REFUSED = "refused"
OUTCOME_IDLE = "idle"
OUTCOME_ERROR = "error"

_STATE_KEY = "report"
_LOCK_TIMEOUT_S = 5.0
_AGENDA_REF_RE = re.compile(r"AGENDA-(\d{4}-\d{2}-\d{2})\.md#(\d+)")

# Result statuses that finish an agenda line at ingest time; ``dispatched``
# stays open for the archon poll.
_TERMINAL_RESULTS = frozenset({"done", "failed", "refused"})

# archon.db statuses that mean a run is over (v1 run_pass's tolerance rule:
# anything else is conservatively still in flight).
_FINISHED_RUN_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "canceled", "error"}
)


@dataclass
class ReportResult:
    """What one reporting pass did. ``error`` is the only non-zero exit."""

    outcome: str
    dry_run: bool = False
    ingested: list[dict[str, Any]] = field(default_factory=list)
    polled: list[dict[str, Any]] = field(default_factory=list)
    checkout_sent: bool = False
    error: str | None = None

    @property
    def exit_code(self) -> int:
        return 1 if self.outcome == OUTCOME_ERROR else 0


def run_report_pass(
    *,
    dry_run: bool = False,
    settings=None,
    report_settings=None,
    services=None,
    notify=None,
    fetch_run_row=None,
    now: datetime | None = None,
    state_file: Path | str | None = None,
) -> ReportResult:
    """Run one reporting pass. Never raises.

    ``services`` is the injectable ``(convoy_service, mailbox_service)``
    pair; ``notify`` the card sender; ``fetch_run_row`` the archon.db
    reader (all ``None`` -> production paths).
    """
    try:
        from security import kill_switches  # Rule 3: module-attribute lookup

        try:
            kill_switches.requireEnabled(
                "cofounder_delegation", caller="cofounder.report"
            )
        except kill_switches.KillSwitchDisabled:
            logger.info("cofounder.report: refused by kill switch; quiet exit")
            return ReportResult(outcome=OUTCOME_REFUSED, dry_run=dry_run)

        import config

        if report_settings is None:
            report_settings = config.get_cofounder_report_settings()
        if not report_settings.enabled:
            logger.debug("cofounder.report: COFOUNDER_REPORT_ENABLED is false")
            return ReportResult(outcome=OUTCOME_DISABLED, dry_run=dry_run)
        if settings is None:
            settings = config.get_cofounder_settings()
        if now is None:
            now = config.now_local()

        if services is None:
            services = _build_services()
        convoy_service, mailbox_service = services

        ingested = _ingest_results(
            convoy_service,
            mailbox_service,
            settings,
            now,
            dry_run=dry_run,
        )
        polled = _poll_dispatched_runs(
            convoy_service,
            settings,
            report_settings,
            now,
            fetch_run_row=fetch_run_row,
            dry_run=dry_run,
        )

        changes = ingested + polled
        if changes and not dry_run and report_settings.notify:
            _send_card(
                settings,
                _render_pulse(changes, now),
                f"pulse-{now.strftime('%H%M')}",
                notify,
            )

        checkout_sent = False
        if not dry_run:
            checkout_sent = _maybe_checkout(
                settings, report_settings, now, state_file, notify
            )

        outcome = (
            OUTCOME_COMPLETED if (changes or checkout_sent) else OUTCOME_IDLE
        )
        logger.info(
            "cofounder.report: %s%s (%d ingested, %d polled, checkout=%s)",
            "[dry-run] " if dry_run else "",
            outcome,
            len(ingested),
            len(polled),
            checkout_sent,
        )
        return ReportResult(
            outcome=outcome,
            dry_run=dry_run,
            ingested=ingested,
            polled=polled,
            checkout_sent=checkout_sent,
        )
    except Exception as exc:  # the whole-pass wrap: nothing escapes
        logger.exception("cofounder.report: pass failed")
        return ReportResult(
            outcome=OUTCOME_ERROR,
            dry_run=dry_run,
            error=f"{type(exc).__name__}: {exc}",
        )


# =============================================================================
# 1 — Result ingestion.
# =============================================================================


def _ingest_results(
    convoy_service,
    mailbox_service,
    settings,
    now: datetime,
    *,
    dry_run: bool,
) -> list[dict[str, Any]]:
    """Claim, apply, ack every pending result. One bad result never stops
    the rest. Dry runs read the inbox and claim nothing."""
    from cofounder import delegate as delegate_mod
    from cofounder import worktick as worktick_mod

    ingested: list[dict[str, Any]] = []
    try:
        if dry_run:
            inbox = mailbox_service.get_inbox(
                delegate_mod.COFOUNDER_AGENT_ID, msg_type=MSG_TYPE_RESULT
            )
            for mwd in inbox:
                logger.info(
                    "cofounder.report: [dry-run] would ingest result message %s",
                    mwd.message.id,
                )
                ingested.append({"message_id": mwd.message.id, "status": "dry-run"})
            return ingested

        try:
            recovered = mailbox_service.recover_stale_claims(
                MSG_TYPE_RESULT, worktick_mod.STALE_CLAIM_SECONDS
            )
            if recovered:
                logger.warning(
                    "cofounder.report: recovered %d stale result claim(s)", recovered
                )
        except Exception:
            logger.warning(
                "cofounder.report: stale-claim recovery failed", exc_info=True
            )

        claimed = mailbox_service.claim_deliveries(
            delegate_mod.COFOUNDER_AGENT_ID, limit=20, msg_type=MSG_TYPE_RESULT
        )
        for mwd in claimed:
            record = {"message_id": mwd.message.id, "status": "error"}
            try:
                record = _apply_result(convoy_service, settings, mwd, now)
            except Exception:
                logger.exception(
                    "cofounder.report: result %s failed; continuing", mwd.message.id
                )
            ingested.append(record)
            try:
                delivery = next(
                    (
                        d
                        for d in mwd.deliveries
                        if d.recipient_agent == delegate_mod.COFOUNDER_AGENT_ID
                    ),
                    None,
                )
                if delivery is not None:
                    mailbox_service.ack_delivery(
                        delivery.id,
                        delegate_mod.COFOUNDER_AGENT_ID,
                        delivery.claim_token,
                    )
            except Exception:
                logger.warning("cofounder.report: ack failed", exc_info=True)
    except Exception:
        logger.warning("cofounder.report: ingestion failed", exc_info=True)
    return ingested


def _apply_result(
    convoy_service, settings, mwd, now: datetime
) -> dict[str, Any]:
    """Flip the agenda line for one result; fail the subtask when the work
    failed/was refused (done subtasks were completed by the work loop)."""
    from cofounder import delegate as delegate_mod

    payload = _parse(mwd.message.body)
    status = str(payload.get("status") or "").strip().lower()
    agenda_ref = str(payload.get("agenda_ref") or "")
    persona = mwd.message.from_agent
    record = {
        "message_id": mwd.message.id,
        "persona": persona,
        "agenda_ref": agenda_ref,
        "status": status or "unknown",
        "summary": str(payload.get("summary") or ""),
        "deliverable_path": payload.get("deliverable_path"),
        "run_id": payload.get("run_id"),
    }

    item_status = status if status in (_TERMINAL_RESULTS | {"dispatched"}) else "failed"
    _stamp_agenda_item(
        settings.projects_dir,
        agenda_ref,
        item_status,
        now,
        summary=record["summary"],
        deliverable_path=payload.get("deliverable_path"),
        run_id=payload.get("run_id"),
        branch=payload.get("branch"),
        subtask_id=payload.get("subtask_id"),
    )

    subtask_id = payload.get("subtask_id")
    if subtask_id and status in {"failed", "refused"}:
        try:
            convoy_service.handle_subtask_failure(
                int(subtask_id), error_message=record["summary"][:200]
            )
        except Exception:
            logger.warning("cofounder.report: subtask fail-mark failed", exc_info=True)

    delegate_mod._audit(
        persona,
        _line_from_ref(agenda_ref) or 0,
        f"report-{item_status}",
        record["summary"][:120] or agenda_ref,
        day=now.date().isoformat(),
        message_id=mwd.message.id,
    )
    return record


# =============================================================================
# 2 — Archon run polling (code-mode completion).
# =============================================================================


def _poll_dispatched_runs(
    convoy_service,
    settings,
    report_settings,
    now: datetime,
    *,
    fetch_run_row,
    dry_run: bool,
) -> list[dict[str, Any]]:
    """Flip recent ``dispatched`` agenda lines whose Archon run finished."""
    from cofounder import delegate as delegate_mod

    if fetch_run_row is None:
        from cofounder import engine_archon

        fetch_run_row = lambda run_id: engine_archon.fetch_run_row(  # noqa: E731
            run_id, db_path=settings.archon_db
        )

    flipped: list[dict[str, Any]] = []
    try:
        for day in _recent_days(now, report_settings.poll_days):
            path = _agenda_json_path(settings.projects_dir, day)
            if not path.is_file():
                continue
            data = _read_json(path)
            for item in (data or {}).get("items") or []:
                if not isinstance(item, dict) or item.get("status") != "dispatched":
                    continue
                run_id = item.get("run_id")
                if not run_id:
                    continue
                row = fetch_run_row(str(run_id))
                run_status = (
                    str(row.get("status") or "").strip().lower() if row else ""
                )
                if run_status not in _FINISHED_RUN_STATUSES:
                    continue  # unknown/unreadable = conservatively in flight
                final = "done" if run_status == "completed" else "failed"
                record = {
                    "agenda_ref": f"AGENDA-{day}.md#{item.get('n')}",
                    "persona": item.get("persona"),
                    "status": final,
                    "summary": f"archon run {run_id} {run_status}"
                    + (f" (branch {item.get('branch')})" if item.get("branch") else ""),
                    "run_id": run_id,
                }
                if dry_run:
                    record["status"] = "dry-run"
                    flipped.append(record)
                    continue
                if not _stamp_agenda_item(
                    settings.projects_dir,
                    record["agenda_ref"],
                    final,
                    now,
                    summary=record["summary"],
                ):
                    # The flip did not land (lock contention / IO): claim
                    # NOTHING this tick — no convoy move, no audit, no pulse
                    # line. The item stays dispatched and the next tick
                    # retries cleanly (adversarial-review finding 2).
                    logger.warning(
                        "cofounder.report: poll stamp did not land for %s; retrying next tick",
                        record["agenda_ref"],
                    )
                    continue
                # subtask_id was stamped onto the item when the dispatched
                # RESULT was ingested (the payload carries it).
                try:
                    if final == "done" and item.get("subtask_id"):
                        convoy_service.handle_subtask_completion(int(item["subtask_id"]))
                    elif final == "failed" and item.get("subtask_id"):
                        convoy_service.handle_subtask_failure(
                            int(item["subtask_id"]), error_message=record["summary"]
                        )
                except Exception:
                    logger.warning(
                        "cofounder.report: subtask flip failed", exc_info=True
                    )
                delegate_mod._audit(
                    str(item.get("persona") or ""),
                    int(item.get("n") or 0),
                    f"report-poll-{final}",
                    record["summary"][:120],
                    day=now.date().isoformat(),
                )
                flipped.append(record)
    except Exception:
        logger.warning("cofounder.report: run polling failed", exc_info=True)
    return flipped


# =============================================================================
# 3+4 — Cards (intraday pulse + EOD checkout). Deterministic renders.
# =============================================================================


_STATUS_MARKS = {
    "done": "✅",
    "dispatched": "🚀",
    "failed": "❌",
    "refused": "🚫",
}


def _render_pulse(changes: list[dict[str, Any]], now: datetime) -> str:
    lines = [f"Portfolio pulse — {len(changes)} update(s):"]
    for change in changes:
        mark = _STATUS_MARKS.get(str(change.get("status")), "▫️")
        who = change.get("persona") or "?"
        summary = str(change.get("summary") or "")[:140]
        lines.append(f"{mark} {who}: {summary} ({change.get('agenda_ref', '')})")
        if change.get("deliverable_path"):
            lines.append(f"   deliverable: {change['deliverable_path']}")
    return "\n".join(lines)


def _maybe_checkout(
    settings, report_settings, now: datetime, state_file, notify
) -> bool:
    """The once-daily EOD checkout card (state-marked, hour-gated).

    ONE lock spans read -> guard -> send -> stamp (the delegate.py
    double-tap lesson): a manual ``python -m cofounder.report`` racing the
    scheduled tick serializes instead of double-carding. The date stamp is
    written ONLY on a CONFIRMED send — a Telegram hiccup leaves the marker
    unset so the next tick retries instead of silently losing the day's
    checkout (adversarial-review findings 1+3).
    """
    try:
        from cofounder import state as state_mod
        from shared import file_lock

        today = now.date().isoformat()
        if now.hour < report_settings.checkout_hour:
            return False
        if not report_settings.notify:
            return False  # nothing to send; leave the marker for a day the
            # card is actually deliverable (flip notify back on = retry)
        state_path = state_mod._resolve_state_file(state_file)
        with file_lock(state_path, timeout=_LOCK_TIMEOUT_S):
            state = state_mod.load_state(state_path)
            entry = state.get(_STATE_KEY)
            if isinstance(entry, dict) and entry.get("checkout_date") == today:
                return False

            card = _render_checkout(settings, today)
            if not _send_card(settings, card, f"checkout-{today}", notify):
                logger.warning(
                    "cofounder.report: checkout card not confirmed; will retry"
                )
                return False

            if not isinstance(entry, dict):
                entry = {}
            state[_STATE_KEY] = entry
            entry["checkout_date"] = today
            state_mod._write_state(state, state_path)
        return True
    except Exception:
        logger.warning("cofounder.report: checkout failed", exc_info=True)
        return False


def _render_checkout(settings, today: str) -> str:
    """Day summary from the agenda JSON + the delegation ledger. No LLM."""
    from cofounder import delegate as delegate_mod

    path = _agenda_json_path(settings.projects_dir, today)
    data = _read_json(path) if path.is_file() else None
    lines = [f"End-of-day checkout — {today}"]
    if not data:
        lines.append("No agenda today.")
        return "\n".join(lines)

    items = [i for i in data.get("items") or [] if isinstance(i, dict)]
    counts: dict[str, int] = {}
    for item in items:
        counts[str(item.get("status", "proposed"))] = (
            counts.get(str(item.get("status", "proposed")), 0) + 1
        )
    summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
    lines.append(f"Agenda: {len(items)} line(s) — {summary}")
    for item in items:
        mark = _STATUS_MARKS.get(str(item.get("status")), "▫️")
        lines.append(
            f"{mark} {item.get('n')}. {item.get('persona')}: "
            f"{str(item.get('task') or '')[:90]}"
        )
        if item.get("deliverable_path"):
            lines.append(f"   deliverable: {item['deliverable_path']}")

    import config

    sent = delegate_mod._count_sent_today(today)
    cap = config.get_cofounder_delegation_settings().max_assignments_per_day
    lines.append(f"Delegations spent: {sent}/{cap}")
    lines.append("Tomorrow's agenda lands with the morning pass.")
    return "\n".join(lines)


def _send_card(settings, text: str, slug_suffix: str, notify) -> bool:
    """One gated card (kill switch + capability + audit; buttons off).

    Same mute contract as the agenda card: an operator-emptied
    COFOUNDER_NOTIFY_LEVELS silences everything. Returns the sender's
    CONFIRMED-send bool (False on mute, refusal, or failure) — the checkout
    marker depends on it.
    """
    try:
        if not settings.notify_levels:
            logger.info("cofounder.report: COFOUNDER_NOTIFY_LEVELS empty; card muted")
            return False
        if notify is None:
            from cofounder import notify as notify_mod

            notify = notify_mod.notify
        from types import SimpleNamespace

        card_settings = settings._replace(
            notify_levels=(*settings.notify_levels, REPORT_LEVEL)
        )
        return bool(
            notify(
                SimpleNamespace(slug=f"report-{slug_suffix}", path=None),
                text,
                REPORT_LEVEL,
                settings=card_settings,
                with_buttons=False,
            )
        )
    except Exception:
        logger.warning("cofounder.report: card send failed", exc_info=True)
        return False


# =============================================================================
# Agenda JSON plumbing.
# =============================================================================


def _stamp_agenda_item(
    projects_dir: Path | str,
    agenda_ref: str,
    status: str,
    now: datetime,
    *,
    summary: str = "",
    deliverable_path: Any = None,
    run_id: Any = None,
    branch: Any = None,
    subtask_id: Any = None,
) -> bool:
    """Locked RMW of one agenda line's status (fail-open to False)."""
    try:
        from cofounder import project_model
        from shared import file_lock

        parsed = _AGENDA_REF_RE.search(agenda_ref or "")
        if not parsed:
            return False
        day, line = parsed.group(1), int(parsed.group(2))
        path = _agenda_json_path(projects_dir, day)
        if not path.is_file():
            return False
        with file_lock(path, timeout=_LOCK_TIMEOUT_S):
            data = _read_json(path)
            if not data:
                return False
            for item in data.get("items") or []:
                if isinstance(item, dict) and item.get("n") == line:
                    item["status"] = status
                    if summary:
                        item["result_summary"] = summary[:280]
                    if deliverable_path:
                        item["deliverable_path"] = str(deliverable_path)
                    if run_id:
                        item["run_id"] = str(run_id)
                    if branch:
                        item["branch"] = str(branch)
                    if subtask_id:
                        item["subtask_id"] = int(subtask_id)
                    item["reported_at"] = now.isoformat(timespec="seconds")
                    break
            else:
                return False
            project_model._atomic_write(path, json.dumps(data, indent=2))
        return True
    except Exception:
        logger.warning("cofounder.report: agenda stamp failed", exc_info=True)
        return False


def _agenda_json_path(projects_dir: Path | str, day: str) -> Path:
    from cofounder.agenda import AGENDAS_SUBDIR

    return Path(projects_dir) / AGENDAS_SUBDIR / f"AGENDA-{day}.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        logger.warning("cofounder.report: json unreadable: %s", path, exc_info=True)
        return None


def _recent_days(now: datetime, days: int) -> list[str]:
    return [
        (now.date() - timedelta(days=offset)).isoformat()
        for offset in range(max(1, days))
    ]


def _line_from_ref(agenda_ref: str) -> int | None:
    parsed = _AGENDA_REF_RE.search(agenda_ref or "")
    return int(parsed.group(2)) if parsed else None


def _parse(body: str) -> dict[str, Any]:
    try:
        data = json.loads(body or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _build_services():
    import config
    from orchestration.convoy_service import ConvoyService
    from orchestration.db import OrchestrationDB
    from orchestration.mailbox_service import MailboxService

    db = OrchestrationDB(config.ORCHESTRATION_DB_PATH)
    return ConvoyService(db), MailboxService(db)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cofounder.report",
        description="Run one co-founder reporting pass (deterministic).",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="dry run: read-only inbox/agenda scan + logging, no writes/cards",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    result = run_report_pass(dry_run=args.test)
    logger.info(
        "cofounder.report: outcome=%s ingested=%d polled=%d checkout=%s",
        result.outcome,
        len(result.ingested),
        len(result.polled),
        result.checkout_sent,
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
