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
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
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
MEMORY_PROMPT_CAP = 1600
RECALL_PROMPT_CAP = 1800
DELIVERABLE_SUMMARY_CAP = 280
MAX_TURNS = 1

# Work-turn read-back (#110 parity). Top-K over the persona's OWN index,
# keyed on a few distinctive task terms — see ``_recall_query`` for why the
# raw task text can never be the query.
RECALL_MAX_RESULTS = 3
RECALL_QUERY_TERMS = 4
RECALL_MIN_TERM_LEN = 3

# Task-shaped noise. Dropping the mode verbs ("draft", "write", "plan") and
# ordinary English keeps the ANDed FTS terms on the SUBJECT of the work, which
# is the only part the persona's own notes can match on.
_QUERY_STOPWORDS = frozenset(
    """
    about all also and any are been being but can create draft each first for
    from get give got has have help her his how into its just made make more
    most need needs new next not now off one only onto other our out over own
    per plan please prep prepare put same should some than that the their them
    then these they this those too top two use using very want was were what
    when where which who why will with write you your
    """.split()
)

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
    # The persona's OWN output text, carried out of the draft run so the
    # experience note can quote it without re-reading the deliverable.
    output_text: str = ""

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
        status, summary, deliverable_path, output_text = _execute_draft(
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

    # Issue #420 — the persona's OWN experience trail. Deterministic and
    # zero-LLM: facts from code, prose from the execution's existing output.
    # Every outcome is recorded (done/dispatched/failed/refused) — a refused
    # grant teaches as much as a shipped deliverable. Fail-open at the import
    # boundary too (the crypto_round/service.py:376-387 shape): an assignment
    # that already executed can never be failed by its note.
    #
    # MUST run before ack: acked deliveries drop out of both the inbox
    # (get_inbox only returns pending/claimed) and stale-claim recovery
    # (recover_stale_claims only resets claimed rows), so a crash between
    # ack and this write would strand the assignment with no note and no
    # way to retry it. The writer's own in-file dedup key makes a retried
    # note-then-crash-before-ack safe to attempt again on the next tick.
    try:
        from personas import experience as experience_mod

        record["experience_note"] = experience_mod.write_assignment_note(
            persona_id=persona,
            agenda_ref=agenda_ref,
            message_id=message.id,
            mode=mode,
            status=status,
            task=task,
            repo=str(repo) if repo else None,
            summary=summary,
            deliverable_path=deliverable_path,
            run_id=run_id,
            branch=branch,
            output_excerpt=output_text,
            local_time=now,
        )
    except Exception as exc:  # noqa: BLE001 - fail-open contract
        logger.warning("cofounder.worktick: experience note failed", exc_info=True)
        from shared import safe_exc_text

        record["experience_note"] = {
            "status": "error",
            "detail": safe_exc_text(exc),
        }

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
) -> tuple[str, str, str | None, str]:
    """One no-tools background-quality run as the persona -> vault file.

    Returns ``(status, summary, deliverable_path, output_text)``. The raw
    output text rides out so #420's experience note can quote the persona's
    OWN words without re-reading (and re-parsing) the deliverable file.
    """
    try:
        prompt = build_draft_prompt(persona, task, payload, now)
        if run_draft is None:
            run_draft = _llm_draft
        text = (run_draft(prompt) or "").strip()
        if not text:
            return EXEC_FAILED, "draft run returned no text", None, ""
        path = _write_deliverable(persona, agenda_ref, task, text, now)
        first_line = text.splitlines()[0] if text.splitlines() else ""
        return EXEC_DONE, f"deliverable written: {first_line}", str(path), text
    except Exception as exc:
        logger.exception("cofounder.worktick: draft execution failed")
        from shared import safe_exc_text

        # safe_exc_text, not an f-string: a hostile provider exception whose
        # own __str__ raises must not escape this except block — an escape
        # here skips the caller's ack/experience-note/audit tail entirely.
        return EXEC_FAILED, safe_exc_text(exc), None, ""


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
        from shared import safe_exc_text

        # Same hostile-__str__ hazard as _execute_draft above.
        return EXEC_FAILED, safe_exc_text(exc), None, None


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
    """The lane-agnostic persona work prompt (plain text, markdown out).

    The persona writes AND re-reads its own memory here (#110 parity for work
    turns): capped durable MEMORY.md plus top-K recall over its own index,
    keyed on the assignment. Both blocks are additive and fail open — a
    persona with no memory tree gets exactly the prompt it got before.

    #421 pinned this on #425: the SOUL.md and MEMORY.md reads used to be
    interpolated RAW on a "first-party identity, nothing model-authored ever
    lands here" premise. #425 invalidates that premise on both files — the
    notes distiller now writes model-authored lessons derived from work notes
    that carry external research titles and quoted third-party prose into the
    persona's MEMORY.md, and a steered amendment reaches SOUL.md the same way.
    So both go through ``_fenced_identity_block`` — the identical
    ``sanitize_recalled_content`` + ``wrap_recalled_memory`` containment
    ``_persona_recall`` already uses in this file. The host-authored framing
    lines ("speak in this voice") stay OUTSIDE the fence; only file content
    goes inside, where the wrapper tells the model not to follow instructions
    found in it.
    """
    soul = _fenced_identity_block(_persona_soul(persona))
    repo_notes = _repo_notes(payload.get("repo"))
    memory = _fenced_identity_block(_persona_memory(persona))
    recalled = _persona_recall(persona, task)
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
    if memory:
        lines += ["", "What you have learned so far (your durable memory):", memory]
    if recalled:
        lines += ["", "Recalled from your own past work:", recalled]
    return "\n".join(lines)


def _ensure_chat_path() -> None:
    """Put ``.claude/chat`` on sys.path so ``cognition.*`` resolves.

    ``_persona_recall`` does this inline before its own recall_service import;
    the identity fence runs EARLIER in ``build_draft_prompt`` (and on a persona
    with no index ``_persona_recall`` returns before reaching that line), so the
    bootstrap cannot be left to that call site.
    """
    import sys

    chat_dir = Path(__file__).resolve().parent.parent.parent / "chat"
    if str(chat_dir) not in sys.path:
        sys.path.insert(0, str(chat_dir))


def _identity_chunks(text: str) -> list[str]:
    """Split an identity file into its ``## `` sections (preamble kept).

    Screening unit, not a formatting choice. ``sanitize_recalled_content``
    is rejection-only — it returns "" for the WHOLE string it is given — and
    its patterns include ``act\\s+as\\s+a`` and ``system\\s*prompt``, both of
    which a legitimate hand-written SOUL.md can contain ("Act as a seasoned
    operator"). Screening the whole file as one unit would silently delete the
    persona's entire identity from every draft prompt on a false positive.
    Per-section screening drops only the offending section — the same choice
    ``memory_reflect.split_note_sections`` made for the notes corpus and
    ``_sanitized_recall_block`` made per recall item.
    """
    chunks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("## ") and current:
            chunks.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        chunks.append("\n".join(current).strip())
    return [chunk for chunk in chunks if chunk]


def _fenced_identity_block(text: str) -> str:
    """Contain a persona identity-file read before it enters the draft prompt.

    Same containment ``_persona_recall`` routes recalled chunks through:
    reject known injection patterns, HTML-escape what survives, and wrap the
    result in the ``<recalled-memory safety="untrusted">`` fence that tells
    the model not to follow instructions found inside.

    Fails CLOSED. An empty read stays empty (unchanged behavior for a persona
    with no memory tree); a cognition import that does not resolve drops the
    block rather than falling back to a raw interpolation, because after #425
    the premise that made raw interpolation safe no longer holds.
    """
    if not text:
        return ""
    _ensure_chat_path()
    try:
        from cognition.injection import sanitize_recalled_content, wrap_recalled_memory
    except ImportError:
        logger.debug("cofounder.worktick: identity fence unavailable", exc_info=True)
        return ""

    items: list[str] = []
    for chunk in _identity_chunks(text):
        safe = sanitize_recalled_content(chunk)
        if safe:
            items.append(safe)
    return wrap_recalled_memory(items)


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


def _persona_memory(persona: str) -> str:
    """Capped read of the persona's OWN MEMORY.md (the ``_persona_soul`` shape).

    Same trust class as SOUL.md — but that class is no longer "first-party,
    inject raw". #425 makes the notes distiller write model-authored lessons
    into this file, distilled from work notes carrying external research titles
    and quoted third-party prose, and the amendment ledger can put a steered
    proposal in SOUL.md by the same route. Both reads therefore return RAW file
    text here and are contained by ``_fenced_identity_block`` at the one
    assembly boundary that matters (``build_draft_prompt``) — the same place
    ``_sanitized_recall_block`` fixes the recall path, so a future caller of
    either reader cannot silently reopen the hole for this prompt.
    """
    try:
        from personas import core as personas_core

        path = (
            personas_core.get_persona_paths(persona)["memory"] / "MEMORY.md"
        )
        if not path.is_file():
            return ""
        return _cap(path.read_text(encoding="utf-8").strip(), MEMORY_PROMPT_CAP)
    except Exception:
        logger.debug("cofounder.worktick: memory read failed", exc_info=True)
        return ""


def _persona_recall(persona: str, task: str) -> str:
    """Top-K recall over the persona's OWN index, keyed on the assignment.

    #110's inference-time read-back pointed at work turns. Deliberate
    choices, each load-bearing:

    - **KEYWORD mode, not AUTO.** ``recall_service`` is the sole recall
      entrypoint (Invariant I-3), and its AUTO/HYBRID path runs
      ``run_recall_pipeline``, whose step 4.5 fires the haiku ``_llm_rerank``
      whenever ``len(merged) > 3``. ``_merge_and_rank(top_n=...)`` REORDERS
      but does not truncate, so ``max_results=3`` does NOT bound that list —
      AUTO would put a live LLM call on the work-turn path. KEYWORD is pure
      FTS5 over the persona's own DB: zero LLM, zero embedding-model load in
      the heartbeat process, and it still routes through the same injection
      posture (``sanitize_recalled_content`` + ``wrap_recalled_memory``)
      inside ``recall_service._keyword_only_recall``. Semantic read-back is
      the named follow-up: it needs a per-call rerank opt-out first.
    - **Postgres backend fails closed, on the backend's OWN truth (Rule 2).**
      ``memory_search.search_keyword`` calls ``db.get_memory_db(db_path=...)``,
      but ``get_memory_db`` IGNORES ``db_path`` entirely and returns the single
      shared ``PostgresMemoryDB`` whenever a Postgres URL is configured —
      Postgres has no persona/tenant column, so every persona's search would
      hit the SAME table. The SQLite path check below only proves a FILE is the
      persona's own; it says nothing about which BACKEND actually gets queried.
      Reading ``config.DATABASE_URL`` here would NOT answer that either:
      ``db.py`` binds its own ``DATABASE_URL`` at import time
      (``from config import ... DATABASE_URL``), so after any supported config
      reload/override the two copies disagree and the guard can read "SQLite,
      safe" while the factory hands the search leg Postgres
      (codex-verdict-round3.md MAJOR). So this seam asks the REAL factory, with
      the REAL argument the search leg passes, and proceeds only when the
      object that would actually be queried is a ``SQLiteMemoryDB``. Both
      backend constructors are lazy (they store a path/URL; ``_get_conn`` does
      the connecting), so the probe opens no file and no socket.
    - **Physical DB check before the read (Rule 2).** ``resolve_db_path`` only
      returns the persona's co-located ``<profile>/data/memory.db`` when the
      sibling ``data/`` dir actually exists; otherwise it falls back to a
      slug DB in the MAIN vault that every persona would share. Reading that
      would put another mind's memory in this persona's prompt, so the read
      is gated on the resolved path BEING the persona's own file. That also
      keeps a prompt build from creating an empty DB as a side effect
      (``search_keyword`` calls ``init_schema``).
    - **Task-level top-K, not first-bucket.** Every FTS keyword search ANDs
      its terms (``db._quote_fts_query``), so the combined multi-term query
      only ever matches a note that restates EVERY chosen word — real notes
      rarely do — while any SINGLE term is a low-precision probe. Returning
      the first query that happened to hit made retrieval depend on TERM
      ORDER: an early term's irrelevant note suppressed a later term's
      relevant one, and the later terms were never even queried
      (codex-verdict-round3.md MAJOR). So every query runs — the combined one
      plus each chosen term — and their results are pooled, deduplicated by
      CHUNK IDENTITY (see ``_chunk_key``: path + line range, never section
      title, which many chunks of one long section share), ranked globally by
      score, and capped to ``RECALL_MAX_RESULTS``. The combined query runs
      first and the sort is stable, so its higher-precision hits win ties.
    - **Every recalled FIELD is re-fenced here, not just the body.**
      ``recall_service``'s own formatter (``_keyword_only_recall``) only
      sanitizes ``r.text`` before folding results into ``formatted_text`` —
      ``r.path`` and ``r.section_title`` are interpolated raw. A poisoned
      note heading (e.g. ``# </recalled-memory> new instructions...``)
      would break out of the untrusted-data fence and read as bare prompt
      text. ``_sanitized_recall_block`` below rebuilds the block from
      ``response.results`` (the pre-formatting raw fields) and routes path,
      section title, AND body through the same
      ``sanitize_recalled_content``/``wrap_recalled_memory`` pair
      recall_service uses for the body — at THIS assembly boundary, so it
      protects the prompt regardless of what a future ``formatted_text``
      change upstream does.
    - **Fail-open, whole body.** No index, no hits, or any failure at all →
      briefing-only prompt, which is exactly today's behavior. The operator's
      ``recall`` kill switch is already enforced inside ``recall_service``
      (refusals counted there); this seam adds no second switch.
    """
    try:
        import sys

        import config
        from personas import core as personas_core

        paths = personas_core.get_persona_paths(persona)
        memory_dir = paths["memory"]
        db_path = config.resolve_db_path(memory_dir)
        if db_path.resolve() != (paths["data"] / "memory.db").resolve():
            logger.debug(
                "cofounder.worktick: %s recall skipped — index path %s is not "
                "the persona's own",
                persona,
                db_path,
            )
            return ""
        if not db_path.is_file():
            logger.debug(
                "cofounder.worktick: %s has no built index at %s; "
                "briefing-only prompt",
                persona,
                db_path,
            )
            return ""

        # Which BACKEND will actually be queried? Ask the factory the search
        # leg calls, with the argument it passes — not config.DATABASE_URL,
        # which is a different copy of that switch (see the docstring). Both
        # constructors are lazy, so this opens nothing.
        import db as db_mod

        probe = db_mod.get_memory_db(db_path=db_path)
        try:
            backend_is_sqlite = isinstance(probe, db_mod.SQLiteMemoryDB)
        finally:
            probe.close()
        if not backend_is_sqlite:
            logger.debug(
                "cofounder.worktick: %s recall skipped — get_memory_db returns "
                "%s for the resolved db_path, not SQLiteMemoryDB; that backend "
                "has no persona/tenant column, so persona-index isolation "
                "cannot be guaranteed",
                persona,
                type(probe).__name__,
            )
            return ""

        terms = _recall_terms(task)
        if not terms:
            return ""

        chat_dir = Path(__file__).resolve().parent.parent.parent / "chat"
        if str(chat_dir) not in sys.path:
            sys.path.insert(0, str(chat_dir))
        import asyncio

        import recall_service  # module-attribute call site (patchable)

        async def _search():
            # Every query runs, then one global ranking — see the
            # "Task-level top-K" note above. Tightest query first: every term
            # must co-occur (best precision when it hits), then each term
            # alone so a note sharing just ONE distinctive word is still
            # reachable. Stopping at the first query that hit would let an
            # early term's irrelevant note bury a later term's relevant one.
            pooled: dict[tuple[str, str], Any] = {}
            for query in (" ".join(terms), *terms):
                response = await recall_service.recall(
                    query=query,
                    memory_dir=memory_dir,
                    search_mode=recall_service.SearchMode.KEYWORD,
                    caller=TASK_NAME,
                    max_results=RECALL_MAX_RESULTS,
                )
                for row in getattr(response, "results", None) or ():
                    # Same chunk retrieved by several terms: keep its best
                    # score rather than counting it twice.
                    key = _chunk_key(row)
                    prior = pooled.get(key)
                    if prior is None or _row_score(row) > _row_score(prior):
                        pooled[key] = row
            # Stable sort — equal scores keep first-seen (combined-query-first)
            # order instead of an arbitrary one.
            ranked = sorted(pooled.values(), key=_row_score, reverse=True)
            return SimpleNamespace(results=ranked[:RECALL_MAX_RESULTS])

        response = asyncio.run(_search())
        return _cap_recall(
            _sanitized_recall_block(response).strip(),
            RECALL_PROMPT_CAP,
        )
    except Exception:
        logger.warning(
            "cofounder.worktick: persona recall failed (non-blocking)",
            exc_info=True,
        )
        return ""


def _chunk_key(row: Any) -> tuple[str, str]:
    """Pool identity for a recall row: the CHUNK, not the section it sits in.

    ``memory_index.chunk_markdown`` deliberately emits MANY chunks under one
    heading — it splits whenever a chunk reaches ``max_chars`` and carries
    ``current_section`` forward across every split — so keying the pool on
    (path, section_title) collapses distinct chunks of one long section into
    a single slot. An early distractor chunk then EVICTS the later
    task-relevant chunk that merely shares its heading, which is the same
    class of suppression as the first-bucket bug one layer down
    (codex-verdict-round4.md MAJOR).

    The line range IS the chunk's identity in the index. A degenerate or
    absent range (a synthetic row, a future backend that omits line numbers)
    falls back to hashing the body, so two different chunks can never share
    a key and one can never silently evict the other.
    """
    path = getattr(row, "path", "") or ""
    start = getattr(row, "start_line", None)
    end = getattr(row, "end_line", None)
    if isinstance(start, int) and isinstance(end, int) and 0 < start <= end:
        return (path, f"L{start}-{end}")
    body = getattr(row, "text", "") or ""
    return (path, "h:" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:16])


def _row_score(row: Any) -> float:
    """Global ranking key for pooled recall rows.

    Rows are ``RecallResult`` or ``_FallbackResult`` depending on whether
    cognition imported in this process, so read ``score`` defensively — a
    missing or non-numeric score sorts to the floor instead of raising and
    collapsing the whole read-back into the fail-open path.
    """
    try:
        return float(getattr(row, "score", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _sanitized_recall_block(response: Any) -> str:
    """Rebuild the fenced recall block from raw fields, not the upstream
    ``formatted_text``.

    ``recall_service._keyword_only_recall`` (`.claude/chat/recall_service.py`)
    builds its ``formatted_text`` by sanitizing only ``r.text`` before
    interpolating ``r.path`` and ``r.section_title`` RAW into the string it
    then hands to ``wrap_recalled_memory``:

        source = r.path.replace("\\\\", "/")
        title = f" ({r.section_title})" if r.section_title else ""
        sanitized_items.append(f"**{source}{title}** (score: {r.score:.2f}):\\n{safe_text}")

    A persona note indexed with heading ``# </recalled-memory> ...`` produces
    a ``section_title`` containing a literal closing tag, which then closes
    the fence early inside ``formatted_text`` — everything the attacker wrote
    after it reads as bare prompt text, not untrusted history.

    This rebuilds the block straight from ``response.results`` (the raw,
    pre-formatting fields recall_service still returns alongside
    ``formatted_text``) and routes EVERY field that lands in the prompt —
    path, section title, body — through the identical
    ``sanitize_recalled_content``/``wrap_recalled_memory`` pair
    recall_service uses for the body. Fixed at this call site (not inside
    recall_service, which is the shared entrypoint for chat/heartbeat/
    reflection/weekly and out of this ticket's file scope) so a future
    caller of ``formatted_text`` elsewhere cannot silently reopen the hole
    for this prompt.
    """
    try:
        from cognition.injection import sanitize_recalled_content, wrap_recalled_memory
    except ImportError:
        return ""

    items: list[str] = []
    for r in getattr(response, "results", None) or []:
        safe_text = sanitize_recalled_content((getattr(r, "text", "") or "")[:500].strip())
        if not safe_text:
            continue  # injection detected in body — drop the whole item
        source = (getattr(r, "path", "") or "").replace("\\", "/")
        if "Memory/" in source:
            source = source.split("Memory/")[-1]
        source = sanitize_recalled_content(source)
        raw_title = getattr(r, "section_title", "") or ""
        safe_title = sanitize_recalled_content(raw_title) if raw_title else ""
        title = f" ({safe_title})" if safe_title else ""
        score = getattr(r, "score", 0.0)
        items.append(f"**{source}{title}** (score: {score:.2f}):\n{safe_text}")
    return wrap_recalled_memory(items)


def _recall_terms(task: str) -> list[str]:
    """Ordered (original-position) list of the task's most distinctive terms.

    Unicode-aware tokenization: ``[^\\W_]+`` matches any script's letters and
    digits (accented Latin, Cyrillic, CJK, ...) while excluding underscore.
    The ``[A-Za-z0-9]+`` this replaces shredded every accented word at its
    diacritic (``"análisis"`` -> ``"an"`` + ``"lisis"``), so a non-English
    assignment produced query terms that appeared nowhere in the persona's
    own accented notes. No FTS5 metacharacter (``"``, ``*``, ``:``, ``^``,
    ``NEAR``, ``(``, ``)``) is ever a word character, so hostile task text
    still can't reach the MATCH expression.
    """
    seen: set[str] = set()
    ranked: list[tuple[int, int, str]] = []
    for position, word in enumerate(re.findall(r"[^\W_]+", (task or "").lower())):
        if len(word) < RECALL_MIN_TERM_LEN or word in _QUERY_STOPWORDS:
            continue
        if word in seen:
            continue
        seen.add(word)
        ranked.append((-len(word), position, word))
    ranked.sort()
    chosen = sorted(ranked[:RECALL_QUERY_TERMS], key=lambda item: item[1])
    return [word for _, _, word in chosen]


def _recall_query(task: str) -> str:
    """A compact, FTS-safe keyword query from free-form assignment text.

    ``db.keyword_search`` quotes every whitespace-separated term and joins
    them with AND (``db._quote_fts_query``), so a whole sentence demands that
    all ~15 words co-occur inside one chunk — a structurally zero-hit query.
    This is the space-joined AND form of ``_recall_terms``; ``_persona_recall``
    also retries those terms one at a time (see there for why the combined
    query alone is not enough).
    """
    return " ".join(_recall_terms(task))


def _cap_recall(text: str, limit: int) -> str:
    """Cap the recall block WITHOUT breaking its untrusted-data fence.

    ``wrap_recalled_memory`` returns an XML-fenced block; a blind ``_cap``
    would drop the closing tag and leave recalled text reading as prompt.
    Cut on a newline boundary and re-close with whatever trailer the wrapper
    itself used, so the fence stays intact if that wrapper ever changes.
    """
    if len(text) <= limit:
        return text
    trailer = text.rsplit("\n", 1)[-1] if text.endswith(">") else ""
    head = text[:limit]
    if "\n" in head:
        head = head[: head.rfind("\n")]
    parts = [head.rstrip(), "[...]"]
    if trailer:
        parts.append(trailer)
    return "\n".join(parts)


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
