"""Co-founder autonomy — the autonomous delegation pass (the flag's code path).

Run manually (testable without a heartbeat):

    cd .claude/scripts && uv run python -m cofounder.autopilot [--test]

``COFOUNDER_DELEGATION_ENABLED`` has always gated AUTONOMOUS delegation
(operator resolution #4, 2026-07-05) while nothing exercised it: the machine
proposed every morning and then starved at the human-approval step. This pass
is the missing half — the co-founder approving his own agenda.

A THIN SELECTOR over the EXISTING transport, never a second delegation path.
Each selected line goes through ``delegate.run_agenda_line(n, date=today,
approved_by="cofounder-autopilot")``, so the Rule-4 scope check, the daily and
per-persona caps, the kill switch, the one-lock delegation span (which also
serializes autopilot against a manual double-tap) and the audit ledger all run
UNCHANGED. The ``approved_by`` stamp is what makes a self-delegation greppable
in the ledger and renderable as "self-assigned" downstream.

Gate order (quiet no-op exits, never heartbeat errors):

1. Kill switch ``cofounder_delegation`` — shared with the send side and the
   work loop: one emergency stop for the whole delegation surface (counted).
2. ``COFOUNDER_DELEGATION_ENABLED`` (default false). Off is a STRUCTURAL
   no-op: no agenda read, no ledger read, no state write.
3. TODAY's agenda JSON on disk (Rule 2 — physical state, never a state-file
   claim about what was proposed). ``date=`` is passed EXPLICITLY: the
   transport's no-date fallback walks back two days, which is a HUMAN
   convenience ("run 3" at 00:50 targets the card the operator is reading).
   Autonomy must never resurrect yesterday's unexecuted proposals.

Selection is priority ascending, then line order. Budgets
(``COFOUNDER_AUTO_*``) ship wide open — they are retreat levers, not the
containment; ``code`` mode delegates too, its blast radius bounded by
worktick's PR-for-review dispatch policy and per-tick cap.

Bounded retries: a line gets at most ``MAX_ATTEMPTS_PER_LINE`` autopilot
attempts per day, memoized in ``cofounder-state.json`` (the agenda
``attempts``-map pattern). A permanently scope-denied line therefore costs two
cheap refusals, not one every tick all day. Transient outcomes (the daily cap,
a busy lock) never burn an attempt, and refused/denied/errored attempts never
burn a delegation slot either — the daily cap counts ``sent`` rows only.

No exception escapes :func:`run_autopilot_pass`; one failing line never stops
the rest of the pass.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

# Boot-shim (PRP-7a): persona env overrides must apply BEFORE any
# config-touching import resolves paths.
from personas import apply_persona_override  # noqa: E402

apply_persona_override()

logger = logging.getLogger(__name__)

TASK_NAME = "cofounder_autopilot"

# The ledger stamp: every autonomous send is greppable by this, and the
# surfaces that render outcomes read it as "self-assigned".
APPROVED_BY = "cofounder-autopilot"

OUTCOME_COMPLETED = "completed"
OUTCOME_DISABLED = "disabled"
OUTCOME_REFUSED = "refused"
OUTCOME_NO_AGENDA = "no-agenda"
OUTCOME_BUDGET = "budget-reached"
OUTCOME_IDLE = "idle"
OUTCOME_ERROR = "error"

# Only lines the operator (or a previous tick) has not already acted on.
STATUS_PROPOSED = "proposed"
DEFAULT_PRIORITY = 2
DEFAULT_MODE = "draft"
CODE_MODE = "code"

# Retries per line per day. Two is enough to survive a transient service
# failure and small enough that a permanently ungranted persona/repo pair
# goes quiet within one morning.
MAX_ATTEMPTS_PER_LINE = 2

_STATE_KEY = "autopilot"
_STATE_LOCK_TIMEOUT_S = 5.0


@dataclass
class AutopilotResult:
    """What one autopilot pass did. ``error`` is the only non-zero exit."""

    outcome: str
    dry_run: bool = False
    attempted: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    @property
    def sent(self) -> int:
        from cofounder import delegate as delegate_mod

        return sum(
            1
            for row in self.attempted
            if row.get("outcome") == delegate_mod.OUTCOME_SENT
        )

    @property
    def exit_code(self) -> int:
        return 1 if self.outcome == OUTCOME_ERROR else 0


def run_autopilot_pass(
    *,
    dry_run: bool = False,
    settings=None,
    delegation_settings=None,
    autopilot_settings=None,
    state_file: Path | str | None = None,
    now: datetime | None = None,
    delegate_line: Callable | None = None,
) -> AutopilotResult:
    """Run one autonomous-delegation pass. Never raises.

    ``settings`` / ``delegation_settings`` / ``autopilot_settings`` /
    ``state_file`` are None-sentinels resolved at call time (Rule 1).
    ``delegate_line`` is the transport seam (``(n, date=, approved_by=) ->
    DelegationResult``); ``None`` resolves ``delegate.run_agenda_line``.
    ``dry_run`` selects and logs but NEVER delegates and NEVER writes state.
    """
    try:
        from security import kill_switches  # Rule 3: module-attribute lookup

        try:
            kill_switches.requireEnabled(
                "cofounder_delegation", caller="cofounder.autopilot"
            )
        except kill_switches.KillSwitchDisabled:
            logger.info("cofounder.autopilot: refused by kill switch; quiet exit")
            return AutopilotResult(outcome=OUTCOME_REFUSED, dry_run=dry_run)

        import config

        if delegation_settings is None:
            delegation_settings = config.get_cofounder_delegation_settings()
        if not delegation_settings.enabled:
            # The flag's whole point: propose-only stays propose-only until
            # the operator flips it. Nothing below this line runs.
            logger.debug(
                "cofounder.autopilot: COFOUNDER_DELEGATION_ENABLED is false; no-op"
            )
            return AutopilotResult(outcome=OUTCOME_DISABLED, dry_run=dry_run)

        if settings is None:
            settings = config.get_cofounder_settings()
        if autopilot_settings is None:
            autopilot_settings = config.get_cofounder_autopilot_settings()
        if now is None:
            # The canonical operator-local clock (HEARTBEAT_TIMEZONE) — the
            # SAME clock that names the agenda file and keys the send ledger.
            now = config.now_local()
        today = now.date().isoformat()

        agenda = _read_agenda(settings.projects_dir, today)
        if agenda is None:
            logger.debug("cofounder.autopilot: no agenda for %s; nothing to do", today)
            return AutopilotResult(outcome=OUTCOME_NO_AGENDA, dry_run=dry_run)

        modes = _line_modes(agenda)
        candidates = _candidates(agenda, autopilot_settings)
        if not candidates:
            logger.debug("cofounder.autopilot: no eligible lines for %s", today)
            return AutopilotResult(outcome=OUTCOME_IDLE, dry_run=dry_run)

        attempts = _attempts_today(today, state_file)
        eligible = []
        for item in candidates:
            burned = attempts.get(str(item["n"]), 0)
            if burned >= MAX_ATTEMPTS_PER_LINE:
                logger.info(
                    "cofounder.autopilot: line %d already failed %d time(s) "
                    "today; skipping until tomorrow",
                    item["n"],
                    burned,
                )
                continue
            eligible.append(item)
        if not eligible:
            return AutopilotResult(outcome=OUTCOME_IDLE, dry_run=dry_run)

        sent_lines = _sent_lines_today(today)
        budget = int(autopilot_settings.max_per_day) - len(sent_lines)
        if budget <= 0:
            logger.info(
                "cofounder.autopilot: daily autopilot budget spent (%d/%d)",
                len(sent_lines),
                autopilot_settings.max_per_day,
            )
            return AutopilotResult(outcome=OUTCOME_BUDGET, dry_run=dry_run)
        code_sent = sum(1 for n in sent_lines if modes.get(n) == CODE_MODE)
        code_cap = autopilot_settings.code_max_per_day

        from cofounder import delegate as delegate_mod

        if delegate_line is None:
            delegate_line = delegate_mod.run_agenda_line
        # A failure that is the LINE's fault repeats every tick, so it spends
        # a retry. The daily cap and a busy delegation lock are transient and
        # global — they say nothing about the line.
        no_burn = {
            delegate_mod.OUTCOME_SENT,
            delegate_mod.OUTCOME_ALREADY,
            delegate_mod.OUTCOME_CAPPED,
            delegate_mod.OUTCOME_BUSY,
        }
        stop_pass = {delegate_mod.OUTCOME_REFUSED, delegate_mod.OUTCOME_NO_AGENDA}

        attempted: list[dict[str, Any]] = []
        for item in eligible:
            if budget <= 0:
                logger.info("cofounder.autopilot: daily budget reached mid-pass")
                break
            number = int(item["n"])
            mode = modes.get(number, DEFAULT_MODE)
            if mode == CODE_MODE and code_cap is not None and code_sent >= code_cap:
                logger.info(
                    "cofounder.autopilot: code-mode budget spent (%d/%d); "
                    "line %d deferred",
                    code_sent,
                    code_cap,
                    number,
                )
                continue

            row = {
                "line": number,
                "priority": _priority(item),
                "mode": mode,
                "persona": item.get("persona"),
            }
            if dry_run:
                logger.info(
                    "cofounder.autopilot: [dry-run] would delegate line %d "
                    "[P%s|%s] -> %s: %s",
                    number,
                    row["priority"],
                    mode,
                    row["persona"],
                    str(item.get("task") or "")[:120],
                )
                row["outcome"] = "dry-run"
                attempted.append(row)
                budget -= 1
                if mode == CODE_MODE:
                    code_sent += 1
                continue

            try:
                result = delegate_line(number, date=today, approved_by=APPROVED_BY)
            except Exception:  # one broken line never stops the others
                logger.exception(
                    "cofounder.autopilot: line %d raised; continuing", number
                )
                row["outcome"] = OUTCOME_ERROR
                attempted.append(row)
                _record_attempt(today, number, state_file)
                continue
            row["outcome"] = result.outcome
            row["convoy_id"] = result.convoy_id
            row["message_id"] = result.message_id
            attempted.append(row)

            if result.outcome == delegate_mod.OUTCOME_SENT:
                budget -= 1
                if mode == CODE_MODE:
                    code_sent += 1
                logger.info(
                    "cofounder.autopilot: self-delegated line %d -> %s (convoy %s)",
                    number,
                    row["persona"],
                    result.convoy_id,
                )
                continue

            logger.warning(
                "cofounder.autopilot: line %d not delegated (%s): %s",
                number,
                result.outcome,
                result.message,
            )
            if result.outcome not in no_burn:
                _record_attempt(today, number, state_file)
            if result.outcome in stop_pass:
                break

        outcome = OUTCOME_COMPLETED if attempted else OUTCOME_IDLE
        result_obj = AutopilotResult(
            outcome=outcome, dry_run=dry_run, attempted=attempted
        )
        logger.info(
            "cofounder.autopilot: %s%s (%d attempt(s), %d sent)",
            "[dry-run] " if dry_run else "",
            outcome,
            len(attempted),
            result_obj.sent,
        )
        return result_obj
    except Exception as exc:  # the whole-pass wrap: nothing escapes the caller
        logger.exception("cofounder.autopilot: pass failed")
        return AutopilotResult(
            outcome=OUTCOME_ERROR,
            dry_run=dry_run,
            error=f"{type(exc).__name__}: {exc}",
        )


# =============================================================================
# Selection over physical agenda state.
# =============================================================================


def _read_agenda(projects_dir: Path | str, day: str) -> dict[str, Any] | None:
    """Today's agenda dict, or None when absent/unreadable (fail-open).

    Reuses the transport's own READ-ONLY loader — one reader, one path
    convention. The delegation call re-reads under its own lock.
    """
    try:
        from cofounder import delegate as delegate_mod

        _, agenda = delegate_mod._load_agenda_json(projects_dir, day)
        return agenda
    except Exception:
        logger.warning("cofounder.autopilot: agenda read failed", exc_info=True)
        return None


def _candidates(agenda: dict[str, Any], autopilot_settings) -> list[dict[str, Any]]:
    """Still-proposed lines within the priority ceiling, best work first.

    Order is priority ascending (1 = most urgent), then line order — so a
    budget that runs out spends itself on the day's most important lines.
    """
    ceiling = autopilot_settings.max_priority
    items: list[dict[str, Any]] = []
    for item in agenda.get("items") or []:
        if not isinstance(item, dict) or not isinstance(item.get("n"), int):
            continue
        if str(item.get("status") or STATUS_PROPOSED) != STATUS_PROPOSED:
            continue
        priority = _priority(item)
        if ceiling is not None and priority > ceiling:
            logger.debug(
                "cofounder.autopilot: line %s is P%d (ceiling P%d); skipped",
                item.get("n"),
                priority,
                ceiling,
            )
            continue
        items.append(item)
    items.sort(key=lambda entry: (_priority(entry), int(entry["n"])))
    return items


def _line_modes(agenda: dict[str, Any]) -> dict[int, str]:
    """Line number -> execution mode, from the agenda artifact itself."""
    modes: dict[int, str] = {}
    for item in agenda.get("items") or []:
        if isinstance(item, dict) and isinstance(item.get("n"), int):
            modes[int(item["n"])] = str(item.get("mode") or DEFAULT_MODE)
    return modes


def _priority(item: dict[str, Any]) -> int:
    try:
        return int(item.get("priority") or DEFAULT_PRIORITY)
    except (TypeError, ValueError):
        return DEFAULT_PRIORITY


# =============================================================================
# Budgets and the retry memo (both Rule-2 physical-state reads).
# =============================================================================


def _sent_lines_today(day: str) -> list[int]:
    """Line numbers autopilot already sent today, from the send ledger.

    Fail-open to ``[]``: the AUTHORITATIVE containment is the transport's own
    daily cap over the same ledger (which fails open the same way). This
    budget is a retreat lever on top of it, never the only thing standing
    between a bug and a runaway.
    """
    import json

    try:
        from cofounder import delegate as delegate_mod

        path = Path(delegate_mod._resolve_audit_path())
        if not path.is_file():
            return []
        lines: list[int] = []
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if (
                    row.get("outcome") == delegate_mod.OUTCOME_SENT
                    and row.get("local_date") == day
                    and row.get("approved_by") == APPROVED_BY
                ):
                    try:
                        lines.append(int(row.get("line")))
                    except (TypeError, ValueError):
                        continue
        return lines
    except Exception:
        logger.warning("cofounder.autopilot: ledger read failed", exc_info=True)
        return []


def _attempts_today(day: str, state_file: Path | str | None) -> dict[str, int]:
    """Today's per-line failed-attempt counts (fail-open to empty).

    A different date in the memo means yesterday's counters — they expire
    with the day, exactly like the agenda pass's own attempt map.
    """
    try:
        from cofounder import state as state_mod

        state = state_mod.load_state(state_mod._resolve_state_file(state_file))
        entry = state.get(_STATE_KEY)
        if not isinstance(entry, dict) or entry.get("date") != day:
            return {}
        lines = entry.get("lines")
        if not isinstance(lines, dict):
            return {}
        return {str(key): int(value or 0) for key, value in lines.items()}
    except Exception:
        logger.warning("cofounder.autopilot: attempt memo read failed", exc_info=True)
        return {}


def _record_attempt(
    day: str, line_number: int, state_file: Path | str | None
) -> None:
    """Count one failed autopilot attempt for a line (locked read-modify-write).

    Fail-open: losing the memo costs at most a few extra cheap refusals, never
    a delegation (Rule 2 — this file is bookkeeping, not truth).
    """
    try:
        from cofounder import state as state_mod
        from shared import file_lock

        path = state_mod._resolve_state_file(state_file)
        with file_lock(path, timeout=_STATE_LOCK_TIMEOUT_S):
            state = state_mod.load_state(path)
            entry = state.get(_STATE_KEY)
            if not isinstance(entry, dict) or entry.get("date") != day:
                entry = {"date": day, "lines": {}}
            lines = entry.get("lines")
            if not isinstance(lines, dict):
                lines = {}
            key = str(line_number)
            try:
                current = int(lines.get(key) or 0)
            except (TypeError, ValueError):
                current = 0
            lines[key] = current + 1
            entry["lines"] = lines
            state[_STATE_KEY] = entry
            state_mod._write_state(state, path)
    except Exception:
        logger.warning("cofounder.autopilot: attempt memo write failed", exc_info=True)


# =============================================================================
# CLI.
# =============================================================================


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cofounder.autopilot",
        description=(
            "Run one autonomous delegation pass over today's agenda "
            "(the same gated transport as /cofounder run <n>)."
        ),
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="dry run: select and log would-send lines, delegate nothing",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    result = run_autopilot_pass(dry_run=args.test)
    logger.info(
        "cofounder.autopilot: outcome=%s attempted=%d sent=%d",
        result.outcome,
        len(result.attempted),
        result.sent,
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
