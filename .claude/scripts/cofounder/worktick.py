"""Co-founder v2 WS4 — the persona work loop (claim -> execute -> report).

Run manually (testable without a heartbeat):

    cd .claude/scripts && uv run python -m cofounder.worktick [--test]

Rides the heartbeat like the agenda pass: each tick claims delivered
``cofounder_assignment`` mailbox messages for the delegable personas and
EXECUTES them per the OPERATOR-APPROVED mode carried in the payload:

- ``draft`` (the default): one direct, no-tools runtime run on the
  background QUALITY tier, speaking AS the persona (its SOUL + the repo
  page's operating notes + the task). The output lands as a vault
  deliverable (``<memory>/cofounder/deliverables/DELIVERABLE-<day>-<persona>
  -<ref>.md``) — recallable, reflectable, greppable.
- ``code``: one detached Archon worktree dispatch through v1's proven
  ``engine_archon.dispatch`` (archon.db receipt or the attempt failed),
  carrying v1's PR-for-review merge policy. WS4 reports ``dispatched``;
  run-completion tracking is WS5's reporting loop.

Every outcome reports back up as a typed ``cofounder_result`` mailbox
message to the cofounder, acks the delivery (releasing the persona's
in-flight cap slot), appends one audit row to the delegation ledger, and
writes one compact daily-log line so the shipped reflection routing carries
the dispatch onto the repo page (the compounding loop).

Gate order (quiet no-op exits, never heartbeat errors):

1. Kill switch ``cofounder_delegation`` — shared with the SEND side: one
   emergency stop for the whole delegation surface (refusals counted).
2. ``COFOUNDER_WORKLOOP_ENABLED`` (default false — dormant family).
3. ``COFOUNDER_WORKLOOP_MAX_PER_TICK`` across all personas.

Rule 4's second half: the delegation scope is RE-checked at claim against
the persona's live config — a grant revoked after send turns the assignment
into a ``refused`` result (acked, audited), never executed work.

Dry runs (``--test``) NEVER claim: a claimed delivery has no lease expiry,
so a dry-run claim would strand the assignment invisible to a later real
tick. ``--test`` reads the inbox (read-only) and logs what a real tick
would execute.

No exception escapes :func:`run_worktick`; one broken assignment never
stops the others (per-assignment containment inside the tick).
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# Boot-shim (PRP-7a): persona env overrides must apply BEFORE any
# config-touching import resolves paths.
from personas import apply_persona_override  # noqa: E402

apply_persona_override()

logger = logging.getLogger(__name__)

TASK_NAME = "cofounder_worktick"
MSG_TYPE_ASSIGNMENT = "cofounder_assignment"
DELIVERABLES_SUBDIR = "deliverables"

OUTCOME_COMPLETED = "completed"
OUTCOME_DISABLED = "disabled"
OUTCOME_REFUSED = "refused"
OUTCOME_IDLE = "idle"
OUTCOME_ERROR = "error"

# Per-assignment outcomes (WorktickResult.executed values + result statuses).
EXEC_DONE = "done"
EXEC_DISPATCHED = "dispatched"
EXEC_FAILED = "failed"
EXEC_REFUSED = "refused"

# Prompt assembly caps (orientation, not the whole vault).
SOUL_PROMPT_CAP = 2000
REPO_NOTES_CAP = 1200
DELIVERABLE_SUMMARY_CAP = 280
MAX_TURNS = 1

# A claimed-but-never-acked assignment (process killed mid-execution) ages
# back to pending after this many seconds — the suggestions-store precedent.
# ~4 heartbeat ticks: long enough that a slow draft can't be double-claimed,
# short enough that a crash frees the persona's in-flight slot same-day.
STALE_CLAIM_SECONDS = 2 * 60 * 60

_STATE_KEY = "worktick"


@dataclass
class WorktickResult:
    """What one tick did. ``error`` is the only non-zero exit code."""

    outcome: str
    dry_run: bool = False
    executed: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    @property
    def exit_code(self) -> int:
        return 1 if self.outcome == OUTCOME_ERROR else 0


def run_worktick(
    *,
    dry_run: bool = False,
    settings=None,
    worktick_settings=None,
    services=None,
    run_draft=None,
    dispatch_code=None,
    now: datetime | None = None,
    state_file: Path | str | None = None,
) -> WorktickResult:
    """Run one work-loop tick. Never raises.

    ``services`` is an injectable ``(convoy_service, mailbox_service)``
    pair (None builds the CLI-shape direct service layer). ``run_draft``
    (``prompt -> text``) and ``dispatch_code`` (``(workflow, branch,
    message, repo_path, ref) -> run_id|None``) are the execution seams —
    ``None`` resolves the production runtime/Archon paths.
    """
    try:
        from security import kill_switches  # Rule 3: module-attribute lookup

        try:
            kill_switches.requireEnabled(
                "cofounder_delegation", caller="cofounder.worktick"
            )
        except kill_switches.KillSwitchDisabled:
            logger.info("cofounder.worktick: refused by kill switch; quiet exit")
            return WorktickResult(outcome=OUTCOME_REFUSED, dry_run=dry_run)

        import config

        if worktick_settings is None:
            worktick_settings = config.get_cofounder_worktick_settings()
        if not worktick_settings.enabled:
            logger.debug("cofounder.worktick: COFOUNDER_WORKLOOP_ENABLED is false")
            return WorktickResult(outcome=OUTCOME_DISABLED, dry_run=dry_run)
        if settings is None:
            settings = config.get_cofounder_settings()
        if now is None:
            now = config.now_local()

        personas = _delegable_personas()
        if not personas:
            return WorktickResult(outcome=OUTCOME_IDLE, dry_run=dry_run)

        # Cross-tick fairness: rotate the starting persona each tick (a
        # persisted offset) so an always-busy early-alphabet persona can
        # never starve the rest when max_per_tick < persona count.
        offset = _rotation_offset(state_file)
        personas = personas[offset % len(personas):] + personas[: offset % len(personas)]

        if services is None:
            services = _build_services()
        convoy_service, mailbox_service = services

        if not dry_run:
            try:
                recovered = mailbox_service.recover_stale_claims(
                    MSG_TYPE_ASSIGNMENT, STALE_CLAIM_SECONDS
                )
                if recovered:
                    logger.warning(
                        "cofounder.worktick: recovered %d stale claimed "
                        "assignment(s) back to pending",
                        recovered,
                    )
            except Exception:
                logger.warning(
                    "cofounder.worktick: stale-claim recovery failed", exc_info=True
                )

        budget = max(0, int(worktick_settings.max_per_tick))
        executed: list[dict[str, Any]] = []
        for persona in personas:
            if budget <= 0:
                break
            try:
                if dry_run:
                    # NEVER claim on a dry run — claims have no lease expiry,
                    # so a dry-run claim would strand the delivery.
                    inbox = mailbox_service.get_inbox(
                        persona, msg_type=MSG_TYPE_ASSIGNMENT
                    )
                    pending = [
                        m
                        for m in inbox
                        if any(
                            d.recipient_agent == persona and d.status == "pending"
                            for d in m.deliveries
                        )
                    ]
                    # Mirror the REAL claim shape (limit=1 per persona per
                    # tick) — a dry run must preview the same fairness the
                    # real tick enforces, never one persona's whole queue.
                    for mwd in pending[:1]:
                        logger.info(
                            "cofounder.worktick: [dry-run] would execute message "
                            "%s for %s",
                            mwd.message.id,
                            persona,
                        )
                        executed.append(
                            {"persona": persona, "message_id": mwd.message.id,
                             "status": "dry-run"}
                        )
                        budget -= 1
                    continue

                claimed = mailbox_service.claim_deliveries(
                    persona, limit=1, msg_type=MSG_TYPE_ASSIGNMENT
                )
                for mwd in claimed:
                    if budget <= 0:
                        break
                    record = _execute_assignment(
                        mwd,
                        persona,
                        settings,
                        worktick_settings,
                        convoy_service,
                        mailbox_service,
                        run_draft,
                        dispatch_code,
                        now,
                    )
                    executed.append(record)
                    budget -= 1
            except Exception:  # one broken persona never stops the others
                logger.exception(
                    "cofounder.worktick: persona %s failed; continuing", persona
                )
                executed.append({"persona": persona, "status": EXEC_FAILED})

        outcome = OUTCOME_COMPLETED if executed else OUTCOME_IDLE
        if not dry_run and executed:
            _bump_rotation(offset, state_file)
        logger.info(
            "cofounder.worktick: %s%s (%d assignment(s))",
            "[dry-run] " if dry_run else "",
            outcome,
            len(executed),
        )
        return WorktickResult(outcome=outcome, dry_run=dry_run, executed=executed)
    except Exception as exc:  # the whole-tick wrap: nothing escapes the caller
        logger.exception("cofounder.worktick: tick failed")
        return WorktickResult(
            outcome=OUTCOME_ERROR,
            dry_run=dry_run,
            error=f"{type(exc).__name__}: {exc}",
        )


# =============================================================================
# One assignment, fully contained.
# =============================================================================


def _execute_assignment(
    mwd,
    persona: str,
    settings,
    worktick_settings,
    convoy_service,
    mailbox_service,
    run_draft,
    dispatch_code,
    now: datetime,
) -> dict[str, Any]:
    """Claim-side pipeline for one message. Always acks; never raises."""
    from cofounder import delegate as delegate_mod

    message = mwd.message
    delivery = next(
        (d for d in mwd.deliveries if d.recipient_agent == persona), None
    )

    payload = _parse_payload(message.body)
    task = str(payload.get("task") or "")
    repo = payload.get("repo")
    mode = str(payload.get("mode") or "draft").strip().lower()
    agenda_ref = str(payload.get("agenda_ref") or "")
    subtask_id = payload.get("subtask_id")
    record: dict[str, Any] = {
        "persona": persona,
        "message_id": message.id,
        "agenda_ref": agenda_ref,
    }

    # Rule 4's second half — the scope is re-checked at CLAIM against the
    # persona's LIVE config. A revoked grant refuses the work (never
    # executes), reports refused, and acks so the delivery can't loop.
    scope_error = delegate_mod._check_persona_scope(persona, repo)
    status: str
    summary: str
    deliverable_path: str | None = None
    run_id: str | None = None
    branch: str | None = None

    if scope_error:
        status, summary = EXEC_REFUSED, scope_error
    elif not task:
        status, summary = EXEC_FAILED, "assignment payload has no task text"
    elif mode == "code":
        status, summary, run_id, branch = _execute_code(
            persona,
            task,
            repo,
            agenda_ref,
            worktick_settings,
            dispatch_code,
            now,
        )
    else:
        status, summary, deliverable_path = _execute_draft(
            persona, task, payload, agenda_ref, run_draft, now
        )

    record["status"] = status

    # Report up (typed), ack the delivery, drive the convoy, audit, and
    # leave the daily-log line — each seam individually fail-open so one
    # failure never blocks the rest.
    try:
        from orchestration.models import CofounderResultPayload

        mailbox_service.send_cofounder_result(
            persona,
            delegate_mod.COFOUNDER_AGENT_ID,
            CofounderResultPayload(
                subtask_id=int(subtask_id) if subtask_id is not None else 0,
                agenda_ref=agenda_ref,
                status=status,
                summary=_cap(summary, DELIVERABLE_SUMMARY_CAP),
                deliverable_path=deliverable_path,
                run_id=run_id,
                branch=branch,
            ),
            convoy_id=message.convoy_id,
        )
    except Exception:
        logger.warning("cofounder.worktick: result send failed", exc_info=True)

    try:
        if delivery is not None:
            mailbox_service.ack_delivery(
                delivery.id, persona, delivery.claim_token
            )
    except Exception:
        logger.warning("cofounder.worktick: ack failed", exc_info=True)

    try:
        if subtask_id and status == EXEC_DONE:
            convoy_service.handle_subtask_completion(int(subtask_id))
        elif subtask_id and status == EXEC_DISPATCHED and branch:
            convoy_service.update_subtask_fields(
                int(subtask_id),
                {"assigned_agent_id": persona, "worktree_branch": branch},
            )
    except Exception:
        logger.warning("cofounder.worktick: convoy update failed", exc_info=True)

    delegate_mod._audit(
        persona,
        0,
        f"worktick-{status}",
        f"{agenda_ref}: {task[:120]}",
        day=now.date().isoformat(),
        convoy_id=message.convoy_id,
        message_id=message.id,
    )

    try:
        from shared import append_to_daily_log

        target = f" [{repo}]" if repo else ""
        line = (
            f"[cofounder-worktick] {persona}{target} {status}: {task[:120]} "
            f"({agenda_ref}"
            + (f", deliverable {deliverable_path}" if deliverable_path else "")
            + (f", archon run {run_id} branch {branch}" if run_id else "")
            + ")"
        )
        append_to_daily_log(line, section_name="Co-Founder Worktick")
    except Exception:
        logger.warning("cofounder.worktick: daily-log line failed", exc_info=True)

    return record


def _execute_draft(
    persona: str,
    task: str,
    payload: dict[str, Any],
    agenda_ref: str,
    run_draft,
    now: datetime,
) -> tuple[str, str, str | None]:
    """One no-tools background-quality run as the persona -> vault file."""
    try:
        prompt = build_draft_prompt(persona, task, payload, now)
        if run_draft is None:
            run_draft = _llm_draft
        text = (run_draft(prompt) or "").strip()
        if not text:
            return EXEC_FAILED, "draft run returned no text", None
        path = _write_deliverable(persona, agenda_ref, task, text, now)
        first_line = text.splitlines()[0] if text.splitlines() else ""
        return EXEC_DONE, f"deliverable written: {first_line}", str(path)
    except Exception as exc:
        logger.exception("cofounder.worktick: draft execution failed")
        return EXEC_FAILED, f"{type(exc).__name__}: {exc}", None


def _execute_code(
    persona: str,
    task: str,
    repo: Any,
    agenda_ref: str,
    worktick_settings,
    dispatch_code,
    now: datetime,
) -> tuple[str, str, str | None, str | None]:
    """One detached Archon dispatch (v1's receipt-or-failed contract)."""
    try:
        from cofounder import repos as repos_mod
        from cofounder.run_pass import MERGE_POLICY_INSTRUCTION

        resolution = repos_mod.resolve_repo(str(repo or ""))
        if resolution.local_path is None:
            return EXEC_FAILED, f"repo {repo!r} has no local path", None, None

        ref_slug = _ref_slug(agenda_ref)
        branch = f"cofounder/assign-{ref_slug}"
        message = (
            f"Assignment from the co-founder (persona: {persona}, "
            f"{agenda_ref}):\n\n{task}\n\n{MERGE_POLICY_INSTRUCTION}"
        )
        if dispatch_code is None:
            dispatch_code = _archon_dispatch
        run_id = dispatch_code(
            worktick_settings.code_workflow,
            branch,
            message,
            resolution.local_path,
            ref_slug,
        )
        if run_id is None:
            return (
                EXEC_FAILED,
                "archon dispatch produced no archon.db receipt",
                None,
                None,
            )
        return (
            EXEC_DISPATCHED,
            f"archon run {run_id} dispatched (PR-for-review); completion "
            "tracking lands with WS5",
            str(run_id),
            branch,
        )
    except Exception as exc:
        logger.exception("cofounder.worktick: code dispatch failed")
        return EXEC_FAILED, f"{type(exc).__name__}: {exc}", None, None


def _archon_dispatch(workflow, branch, message, repo_path, slug):
    from cofounder import engine_archon

    result = engine_archon.dispatch(
        workflow, branch, message, repo_path, slug=f"worktick-{slug}", iteration=1
    )
    return result.run_id


# =============================================================================
# Prompt + deliverable.
# =============================================================================


def build_draft_prompt(
    persona: str, task: str, payload: dict[str, Any], now: datetime
) -> str:
    """The lane-agnostic persona work prompt (plain text, markdown out)."""
    soul = _persona_soul(persona)
    repo_notes = _repo_notes(payload.get("repo"))
    lines = [
        f"You are the `{persona}` department-head persona of this operator's",
        "company, executing ONE assignment the operator approved from the",
        "co-founder's agenda. Produce the deliverable itself as clean",
        "markdown — no preamble, no meta-commentary about being an AI.",
        "",
        f"Date: {now.date().isoformat()}",
        f"Assignment: {task}",
    ]
    why = str(payload.get("why") or "")
    if why:
        lines.append(f"Why it matters: {why}")
    if payload.get("repo"):
        lines.append(f"Repo in scope: {payload['repo']}")
    lines += [
        "",
        "Hard rules:",
        "- Deliver the artifact (checklist, brief, plan, packet) — concrete,",
        "  checkable items, no filler.",
        "- Never claim work was executed, deployed, or verified — you are",
        "  drafting for operator review.",
        "- If the assignment needs information you do not have, say exactly",
        "  what is missing in a final 'Open questions' section.",
    ]
    if soul:
        lines += ["", "Your identity (speak in this voice):", soul]
    if repo_notes:
        lines += ["", "Repo operating notes:", repo_notes]
    return "\n".join(lines)


def _persona_soul(persona: str) -> str:
    try:
        from personas import core as personas_core

        path = (
            personas_core.get_persona_paths(persona)["memory"] / "SOUL.md"
        )
        if not path.is_file():
            return ""
        return _cap(path.read_text(encoding="utf-8").strip(), SOUL_PROMPT_CAP)
    except Exception:
        logger.debug("cofounder.worktick: soul read failed", exc_info=True)
        return ""


def _repo_notes(repo: Any) -> str:
    if not repo:
        return ""
    try:
        import config
        import repository_memory

        page = (
            Path(config.MEMORY_DIR)
            / repository_memory.REPOSITORY_PAGES_DIR
            / f"{repo}.md"
        )
        content = repository_memory.read_text_safe(page)
        if not content.strip():
            return ""
        parts = []
        for heading in ("Identity", "Workflow Preferences"):
            body = repository_memory.extract_h2_section(content, heading).strip()
            if body:
                parts.append(body)
        return _cap("\n\n".join(parts), REPO_NOTES_CAP)
    except Exception:
        logger.debug("cofounder.worktick: repo notes read failed", exc_info=True)
        return ""


def _llm_draft(prompt: str) -> str:
    """One background-QUALITY runtime call (the orchestrate/agenda shape)."""
    import asyncio

    import config
    from runtime import registry  # module-attribute call site (patchable)
    from runtime.base import RuntimeRequest
    from runtime.capabilities import TEXT_REASONING

    request = RuntimeRequest(
        prompt=prompt,
        cwd=config.PROJECT_ROOT,
        task_name=TASK_NAME,
        capability=TEXT_REASONING,
        model=config.get_background_models()["quality"],
        max_turns=MAX_TURNS,
        allowed_tools=[],  # personas execute with NO tools here — the draft
        # is text; every external mutation keeps its own default-deny gate.
    )
    result = asyncio.run(registry.run_with_fallback(request))
    return getattr(result, "text", "") or ""


def _write_deliverable(
    persona: str, agenda_ref: str, task: str, text: str, now: datetime
) -> Path:
    """Atomic write of the deliverable vault artifact."""
    import config
    from cofounder import project_model
    from shared import file_lock

    day = now.date().isoformat()
    ref_slug = _ref_slug(agenda_ref)
    safe_persona = "".join(c for c in persona if c.isalnum() or c in "._-")
    deliverables_dir = (
        Path(config.MEMORY_DIR) / "cofounder" / DELIVERABLES_SUBDIR
    )
    deliverables_dir.mkdir(parents=True, exist_ok=True)
    path = deliverables_dir / f"DELIVERABLE-{ref_slug}-{safe_persona}.md"
    content = "\n".join(
        [
            "---",
            "tags: [system, cofounder, deliverable]",
            f"date: {day}",
            f"persona: {safe_persona}",
            f"agenda_ref: {agenda_ref}",
            "status: draft-for-review",
            "---",
            f"# Deliverable — {task[:120]}",
            "",
            "_Drafted by the persona work loop for operator review — nothing",
            "here has been executed, deployed, or verified._",
            "",
            text,
            "",
        ]
    )
    with file_lock(path, timeout=5.0):
        project_model._atomic_write(path, content)
    return path


# =============================================================================
# Discovery + plumbing.
# =============================================================================


def _delegable_personas() -> list[str]:
    """Profiles whose config carries a ``delegation:`` block (fail-open [])."""
    found: list[str] = []
    try:
        from personas import core as personas_core
        from personas import services as personas_services

        profiles_root = personas_core.get_default_homie_root() / "profiles"
        if not profiles_root.is_dir():
            return []
        for entry in sorted(profiles_root.iterdir()):
            if not entry.is_dir():
                continue
            try:
                cfg = personas_services.load_persona_config(entry.name)
            except Exception:
                continue
            if isinstance(cfg.get("delegation"), dict):
                found.append(entry.name)
    except Exception:
        logger.warning("cofounder.worktick: persona scan failed", exc_info=True)
    return found


def _build_services():
    import config
    from orchestration.convoy_service import ConvoyService
    from orchestration.db import OrchestrationDB
    from orchestration.mailbox_service import MailboxService

    db = OrchestrationDB(config.ORCHESTRATION_DB_PATH)
    return ConvoyService(db), MailboxService(db)


def _rotation_offset(state_file: Path | str | None = None) -> int:
    """The persisted round-robin start offset (fail-open to 0)."""
    try:
        from cofounder import state as state_mod

        state = state_mod.load_state(state_mod._resolve_state_file(state_file))
        entry = state.get(_STATE_KEY)
        if isinstance(entry, dict) and isinstance(entry.get("offset"), int):
            return max(0, entry["offset"])
    except Exception:
        logger.debug("cofounder.worktick: rotation read failed", exc_info=True)
    return 0


def _bump_rotation(offset: int, state_file: Path | str | None = None) -> None:
    """Advance the round-robin offset (locked RMW; fail-open — losing it
    costs fairness for one tick, never correctness)."""
    try:
        from cofounder import state as state_mod
        from shared import file_lock

        path = state_mod._resolve_state_file(state_file)
        with file_lock(path, timeout=5.0):
            state = state_mod.load_state(path)
            entry = state.get(_STATE_KEY)
            if not isinstance(entry, dict):
                entry = {}
            state[_STATE_KEY] = entry
            entry["offset"] = (offset + 1) % 1_000_000
            state_mod._write_state(state, path)
    except Exception:
        logger.debug("cofounder.worktick: rotation bump failed", exc_info=True)


def _parse_payload(body: str) -> dict[str, Any]:
    try:
        data = json.loads(body or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _ref_slug(agenda_ref: str) -> str:
    """A filesystem/branch/argv-safe slug from an agenda ref.

    The ref normally comes from delegate.py's own f-string, but the mailbox
    body is local-DB-writable — a tampered ref must not traverse paths
    (``../``), split branch names, or start an argv element with ``-``.
    Allowlist only; empty/garbage degrades to ``"assignment"``.
    """
    raw = (agenda_ref or "").replace("AGENDA-", "").replace(".md#", "-line")
    safe = "".join(c for c in raw if c.isalnum() or c in "._-")
    safe = safe.lstrip(".-")  # no dotfiles, no argv-flag-shaped leading dash
    return safe[:60] or "assignment"


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " [...]"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cofounder.worktick",
        description="Run one co-founder persona work-loop tick.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="dry run: read-only inbox scan + logging, no claim/execute/writes",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    result = run_worktick(dry_run=args.test)
    logger.info(
        "cofounder.worktick: outcome=%s executed=%d",
        result.outcome,
        len(result.executed),
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
