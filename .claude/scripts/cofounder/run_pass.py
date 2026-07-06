"""Co-founder orchestration pass — shell (US-005) + deterministic pipeline (US-011).

Run manually (testable without a heartbeat):

    cd .claude/scripts && uv run python -m cofounder.run_pass [--test] [--project <slug>]

Gate order (both gates are quiet no-op exit 0 — operator-intended states,
never errors the heartbeat can trip over):

1. Kill switch ``cofounder`` (HOMIE_KILLSWITCH_COFOUNDER=disabled refuses,
   refusal counted via ``security.kill_switches``).
2. ``COFOUNDER_ENABLED`` (default false until the operator's Phase 9 flip).

Re-entrancy: ``shared.file_lock`` on ``cofounder-state.json`` — a second pass
finding the lock held exits quietly with no state writes. The lock is NOT
re-entrant, so everything inside the pass that writes state must use
``state._write_state`` (the caller-holds-lock seam), never ``save_state``.

Per-project pipeline (prd.md Phase 3 — everything mechanical resolves in pure
Python BEFORE any model call, Global Invariant 5):

1. ``done`` -> archive to ``done/``.
2. Caps: ``iterations >= max_iterations`` OR wall clock exceeded flips to
   ``awaiting-human`` with ONE recorded notify event. Frontmatter owns the
   per-project caps (Rule 2 — the file is the truth); both comparison sides
   are folded into aware-UTC so mixed-tz values can never crash the math.
3. Poll ``current_job_id``: one read-only archon.db query per job, prefetched
   across ALL discovered projects so the concurrency cap counts globally.
   An unreadable row is conservatively in flight — never double-dispatch.
4. Steering: new ``[steer]`` Activity Log lines past the state-json reply
   cursor. Parked projects (blocked / awaiting-human) wake only on steering.
5. The hard gate: job in flight + no new steering = one small Activity Log
   note, return. NEVER dispatch while a job runs. Zombie upkeep lives here:
   classify against the PRIOR pass's worktree mtime snapshot, THEN refresh
   the snapshot (that ordering makes "no growth across a full cycle" true).
6. Completion: ``testing`` runs the executable ``completion_check`` in the
   build worktree — green flips to ``done`` (or ``awaiting-human`` when
   ``subjective_gate``); the same check failing twice (fail streak in the
   state json) flips to ``blocked``. The agent's self-report never counts.
7. LLM-needed classification (pure code): a real decision remains only for
   a new project, a finished job, or a human reply. The ``decide`` callable
   (US-012; defaults to ``cofounder.orchestrate.decide`` — the US-020 ship
   wiring) is invoked ONLY then; CODE executes the decision — the model
   never runs shell, can never mint ``done``, and its dispatch wish is
   refused while a job runs or past ``COFOUNDER_MAX_CONCURRENT``.
8. Machine state is re-stamped in code after every step; ``archive_to_done``
   on done; notify hooks fire once per terminal flip through the gated
   Telegram sender (``cofounder.notify.notify`` — the US-017 default wiring;
   routine progress is Activity Log only, never a notify).

Merge policy (US-017, default-deny): the orchestrator itself NEVER merges.
Every dispatch into a pre-existing repo carries the PR-for-review
instruction (:data:`MERGE_POLICY_INSTRUCTION`); only greenfield
(system-owned) repos may commit straight to their default branch, and there
is deliberately NO knob for automatic merging in v1.

Each project's pipeline runs inside the ``cofounder_pass`` dual-lane span
(``orchestration.observability.orchestration_span``; metadata: project,
action, status_flip, latency_ms) — observability is strictly fail-open and
can never break the pass. ``author`` decisions flow through
``cofounder.workflow_author`` (US-013): CODE validates, stamps, and writes
the drafted YAML, and every authored workflow is RE-stamped after each pass
so an LLM edit can never drift the backend provider/model.

No exception escapes ``run_pass`` — every outcome is a :class:`PassResult`;
the CLI maps it to an exit code (0 for every quiet outcome, 1 only for
``error``), and one broken project never stops the others. Kept import-light
at module level so the heartbeat seam (US-006) can lazy-import this module
cheaply.
"""

from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Boot-shim (PRP-7a): persona env overrides must apply BEFORE any
# config-touching import resolves paths. Idempotent and import-light
# (personas.boot has no config dependency), so the heartbeat's lazy import
# of this module stays cheap; a direct ``python -m cofounder.run_pass``
# needs the call here.
from personas import apply_persona_override  # noqa: E402

apply_persona_override()

logger = logging.getLogger(__name__)

# Re-entrancy is a fast-fail check, not a queue: if another pass holds the
# lock we want out in well under a heartbeat tick, not a 5s wait.
_PASS_LOCK_TIMEOUT_S = 0.5

OUTCOME_COMPLETED = "completed"
OUTCOME_DISABLED = "disabled"
OUTCOME_REFUSED = "refused"
OUTCOME_LOCKED = "locked"
OUTCOME_ERROR = "error"

# Per-project pipeline outcomes (PassResult.project_outcomes values).
PROJECT_ARCHIVED = "archived"
PROJECT_CAPS_TRIPPED = "caps-awaiting-human"
PROJECT_PARKED = "parked"
PROJECT_RUNNING = "running"
PROJECT_ZOMBIE_RECOVERED = "zombie-recovered"
PROJECT_TESTING_DRY = "testing-dry-run"
PROJECT_CHECK_FAILED = "check-failed"
PROJECT_BLOCKED = "blocked"
PROJECT_DONE = "done"
PROJECT_AWAITING_VERDICT = "awaiting-verdict"
PROJECT_QUEUED = "queued"
PROJECT_DECISION_PENDING = "decision-pending"
PROJECT_DECIDED = "decided"
PROJECT_DECIDED_DRY = "decided-dry-run"
PROJECT_DECISION_NOOP = "decision-noop"
PROJECT_AUTHORED = "authored"
PROJECT_DISPATCHED = "dispatched"
PROJECT_DISPATCH_FAILED = "dispatch-failed"
PROJECT_DISPATCH_REFUSED = "dispatch-refused"
PROJECT_ERROR = "error"

# Decision actions (US-012 output contract; decided there, EXECUTED here).
ACTION_REUSE = "reuse"
ACTION_AUTHOR = "author"
ACTION_TEST = "test"
ACTION_PARK = "park"

# Pure-code reasons a real decision remains (Invariant 5: classification
# never needs a model).
REASON_HUMAN_REPLIED = "human-replied"
REASON_JOB_FINISHED = "job-finished"
REASON_NEW_PROJECT = "new-project"

_STEER_MARKER = "[steer]"

# Merge policy (US-017): appended to every dispatched build message for a
# pre-existing repo. Phrased without any literal merge command so the
# no-merge-invocation source scan stays meaningful.
MERGE_POLICY_INSTRUCTION = (
    "Merge policy: commit only to the assigned worktree branch and leave the "
    "work as a pull request for operator review. Never merge the work "
    "yourself and never enable automatic merging."
)

# archon.db statuses that mean the run is over. Anything else — including
# blank, "pending", or a status this code has never seen — is conservatively
# in flight, mirroring the status machine's non-enum tolerance: polling
# continues and a double-dispatch can never happen on an unknown.
_FINISHED_JOB_STATUSES = frozenset({"completed", "failed", "cancelled", "canceled", "error"})

# Index doc (US-018): the always-loaded discoverability surface, a sibling of
# the watched projects dir. The pass refreshes ONLY the Active Projects
# section; every other byte (ownership rules, worked example) is operator-owned.
INDEX_DOC_NAME = "COFOUNDER-PROJECTS.md"
INDEX_ACTIVE_PROJECTS_SECTION = "Active Projects"


@dataclass
class PassResult:
    """What one pass did. ``error`` is the only non-zero exit code."""

    outcome: str
    dry_run: bool = False
    projects_seen: tuple[str, ...] = ()
    error: str | None = None
    project_outcomes: dict[str, str] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        return 1 if self.outcome == OUTCOME_ERROR else 0


def run_pass(
    *,
    dry_run: bool = False,
    only_project: str | None = None,
    settings=None,
    state_file: Path | str | None = None,
    decide: Callable | None = None,
    notify: Callable | None = None,
) -> PassResult:
    """Run one orchestration pass. Never raises.

    ``settings`` / ``state_file`` are None-sentinels resolved at call time
    (Rule 1): ``config.get_cofounder_settings()`` and
    ``STATE_DIR/cofounder-state.json`` respectively.

    ``decide`` is the LLM decision step (US-012): called ONLY when pure-code
    classification says a real decision remains; ``None`` resolves the
    production decider (``cofounder.orchestrate.decide`` — US-020 wiring),
    failing open to a logged pending decision when the module is broken.
    ``notify`` is the terminal-flip hook (US-014); ``None`` resolves the
    gated Telegram sender (``cofounder.notify.notify`` — US-017 wiring),
    failing open to a logging stub. Both are fail-open.
    """
    try:
        from security import kill_switches  # Rule 3: module-attribute lookup

        try:
            kill_switches.requireEnabled("cofounder", caller="cofounder.run_pass")
        except kill_switches.KillSwitchDisabled:
            logger.info("cofounder: pass refused by kill switch; quiet exit")
            return PassResult(outcome=OUTCOME_REFUSED, dry_run=dry_run)

        import config

        if settings is None:
            settings = config.get_cofounder_settings()
        if not settings.enabled:
            logger.debug("cofounder: COFOUNDER_ENABLED is false; quiet no-op")
            return PassResult(outcome=OUTCOME_DISABLED, dry_run=dry_run)

        from cofounder import state as state_mod
        from shared import file_lock

        state_path = state_mod._resolve_state_file(state_file)
        with ExitStack() as stack:
            try:
                stack.enter_context(file_lock(state_path, timeout=_PASS_LOCK_TIMEOUT_S))
            except TimeoutError:
                logger.info("cofounder: another pass holds the lock; quiet exit")
                return PassResult(outcome=OUTCOME_LOCKED, dry_run=dry_run)
            return _locked_pass(
                settings,
                state_path,
                dry_run=dry_run,
                only_project=only_project,
                decide=decide,
                notify=notify,
            )
    except Exception as exc:  # the whole-pass wrap: nothing escapes the caller
        logger.exception("cofounder: pass failed")
        return PassResult(
            outcome=OUTCOME_ERROR,
            dry_run=dry_run,
            error=f"{type(exc).__name__}: {exc}",
        )


def _locked_pass(
    settings,
    state_path: Path,
    *,
    dry_run: bool,
    only_project: str | None,
    decide: Callable | None,
    notify: Callable | None,
) -> PassResult:
    """The pass body. The caller holds the pass lock on ``state_path``."""
    from cofounder import engine_archon, project_model
    from cofounder import state as state_mod

    if notify is None:
        notify = _resolve_notify()
    if decide is None:
        decide = _resolve_decide()

    projects = project_model.discover_projects(settings.projects_dir)

    # One read-only poll per in-flight job, over ALL discovered projects —
    # the concurrency cap is global, so a --project pass still counts the
    # jobs it is not processing this time.
    job_rows: dict[str, dict[str, Any] | None] = {}
    for project in projects:
        job_id = project.frontmatter.current_job_id
        if job_id:
            job_rows[project.slug] = engine_archon.fetch_run_row(
                str(job_id), db_path=settings.archon_db
            )
    in_flight = sum(
        1
        for p in projects
        if p.frontmatter.current_job_id and _job_in_flight(job_rows.get(p.slug))
    )

    if only_project is not None:
        wanted = only_project.strip()
        projects = [p for p in projects if p.slug == wanted]
        if not projects:
            logger.warning(
                "cofounder: --project %s matched no discovered project", wanted
            )

    state = state_mod.load_state(state_path)
    now = datetime.now(UTC)
    outcomes: dict[str, str] = {}
    for project in projects:
        try:
            outcome, in_flight = _observed_project_pass(
                project,
                settings,
                state,
                state_path,
                row=job_rows.get(project.slug),
                in_flight=in_flight,
                now=now,
                dry_run=dry_run,
                decide=decide,
                notify=notify,
            )
        except Exception:  # one broken project never stops the others
            logger.exception(
                "cofounder: project %s failed; continuing pass", project.slug
            )
            outcome = PROJECT_ERROR
        outcomes[project.slug] = outcome

    if not dry_run:
        # US-013: re-stamp every authored workflow after the pass so an LLM
        # edit inside the repo can never drift the backend provider/model.
        _restamp_authored_workflows(state)
        # US-018: refresh the index doc's Active Projects list from post-pass
        # disk state (--test skips along with every other write).
        _refresh_index_doc(settings)
        # Pass-level key, sibling of "projects" (schema is namespaced so this
        # can't collide with a slug). US-010 reads it as the cycle boundary.
        state["last_pass_at"] = now.isoformat()
        state_mod._write_state(state, state_path)  # we hold the lock

    return PassResult(
        outcome=OUTCOME_COMPLETED,
        dry_run=dry_run,
        projects_seen=tuple(p.slug for p in projects),
        project_outcomes=outcomes,
    )


# =============================================================================
# US-013 — the cofounder_pass span + the after-pass workflow re-stamp.
# =============================================================================


def _observed_project_pass(project, *args, **kwargs) -> tuple[str, int]:
    """One project's pipeline inside the ``cofounder_pass`` dual-lane span.

    Observability is strictly fail-open (Invariant 6): a broken orchestration
    import or span setup runs the pipeline bare, and a pipeline exception
    still flows to the caller's per-project containment (the span records it
    on the way through). The span metadata dict is mutated in place —
    ``orchestration_span`` re-reads the same object at exit, so the final
    action / status_flip / latency_ms land in both lanes.
    """
    metadata: dict[str, Any] = {
        "project": project.slug,
        "action": None,
        "status_flip": None,
        "latency_ms": None,
    }
    before_status = project.frontmatter.status
    obs = None
    with ExitStack() as stack:
        try:
            from orchestration import observability as obs_mod  # Rule 3 lookup

            stack.enter_context(
                obs_mod.orchestration_span("cofounder_pass", metadata=metadata)
            )
            obs = obs_mod
        except Exception:  # a broken span must never block the pipeline
            logger.debug("cofounder: cofounder_pass span unavailable", exc_info=True)
            obs = None
        started = time.perf_counter()
        try:
            outcome, in_flight = _project_pass(project, *args, **kwargs)
            metadata["action"] = outcome
            return outcome, in_flight
        except Exception:
            metadata["action"] = PROJECT_ERROR
            raise
        finally:
            metadata["latency_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
            after_status = _status_on_disk(project)
            if after_status is not None and after_status != before_status:
                metadata["status_flip"] = f"{before_status}->{after_status}"
            if obs is not None:
                try:
                    obs.update_observation(metadata=metadata)
                except Exception:
                    pass


def _status_on_disk(project) -> str | None:
    """The project's post-pass status re-read from the file (Rule 2).

    An archived file (moved to done/) reads as ``"archived"``; any read
    failure degrades to ``None`` (telemetry only — never a guard input).
    """
    from cofounder import project_model

    try:
        path = Path(project.path)
        if not path.exists():
            return "archived"
        return project_model.parse_project_file(path).frontmatter.status
    except Exception:
        return None


def _restamp_authored_workflows(state: dict[str, Any]) -> None:
    """Re-stamp every recorded authored workflow (the LLM-drift guard).

    Walks each project entry's ``authored_workflows`` list and re-applies the
    backend-knob stamp; files that no longer exist are pruned from the list
    (physical state is truth). Fail-open at every seam — a re-stamp failure
    never breaks the pass.
    """
    try:
        from cofounder import workflow_author

        projects_map = state.get("projects")
        if not isinstance(projects_map, dict):
            return
        for entry in projects_map.values():
            paths = entry.get("authored_workflows") if isinstance(entry, dict) else None
            if not isinstance(paths, list):
                continue
            for raw in list(paths):
                if not Path(str(raw)).exists():
                    paths.remove(raw)
                    logger.info(
                        "cofounder: authored workflow %s is gone; untracked", raw
                    )
                    continue
                workflow_author.restamp_workflow(raw)
    except Exception:
        logger.warning("cofounder: authored-workflow re-stamp failed", exc_info=True)


# =============================================================================
# US-018 — the index doc's auto-refreshed Active Projects list.
# =============================================================================


def _refresh_index_doc(settings) -> None:
    """Refresh the index doc's Active Projects list from post-pass disk state.

    The index doc (:data:`INDEX_DOC_NAME`, a sibling of the projects dir) is
    the discoverability surface every session reads. Projects are
    RE-discovered after the pipeline ran (Rule 2: a project archived this
    pass vanishes from the list now, not next pass) and only the
    ``## Active Projects`` section body is spliced, through the same
    file_lock + atomic-write helpers the project writers use. A missing doc
    or section is a quiet skip; any failure is fail-open (Invariant 6) - the
    list is discoverability, never a guard input.
    """
    try:
        from cofounder import project_model
        from shared import file_lock

        index_path = Path(settings.projects_dir).parent / INDEX_DOC_NAME
        if not index_path.is_file():
            logger.debug("cofounder: index doc %s absent; refresh skipped", index_path)
            return
        projects = project_model.discover_projects(settings.projects_dir)
        lines = [
            f"- **{p.slug}** - {p.frontmatter.status}"
            f" (iterations {p.frontmatter.iterations}/{p.frontmatter.max_iterations},"
            f" job {p.frontmatter.current_job_id or 'none'})"
            for p in projects
        ]
        listing = "\n".join(lines) if lines else "_No active projects._"
        stamp = f"_Auto-refreshed by the co-founder pass at {_local_now_iso()}._"
        with file_lock(index_path, timeout=5.0):
            content = index_path.read_text(encoding="utf-8")
            head, body = project_model._split_raw(content)
            start, end = project_model._section_span(
                body, INDEX_ACTIVE_PROJECTS_SECTION
            )
            followed = end < len(body)
            segment = f"{stamp}\n\n{listing}" + ("\n\n" if followed else "\n")
            project_model._atomic_write(
                index_path, head + body[:start] + segment + body[end:]
            )
    except Exception:
        logger.warning("cofounder: index doc refresh failed", exc_info=True)


# =============================================================================
# US-011 — the deterministic per-project pipeline.
# =============================================================================


def _project_pass(
    project,
    settings,
    state: dict[str, Any],
    state_path: Path,
    *,
    row: dict[str, Any] | None,
    in_flight: int,
    now: datetime,
    dry_run: bool,
    decide: Callable | None,
    notify: Callable,
) -> tuple[str, int]:
    """Run one project through the pipeline; returns (outcome, in_flight)."""
    from cofounder import project_model
    from cofounder import state as state_mod
    from cofounder import status as status_mod

    fm = project.frontmatter
    slug = project.slug
    prefix = "[dry-run] " if dry_run else ""
    logger.info(
        "cofounder: %sproject %s (status=%s, job=%s)",
        prefix,
        slug,
        fm.status,
        fm.current_job_id or "none",
    )

    # Link this project's entry into the shared state mapping up front: every
    # in-place mutation below lands in the single end-of-pass write (and in a
    # dry run the mapping is simply never persisted).
    entry = state_mod.get_project_state(state, slug)
    projects_map = state.get("projects")
    if not isinstance(projects_map, dict):
        projects_map = {}
        state["projects"] = projects_map
    projects_map[slug] = entry

    # Step 1 — a done project only needs archiving (the completion path
    # archives its own flips directly; this catches an operator's hand-stamp).
    if status_mod.is_terminal(fm.status):
        if dry_run:
            logger.info("cofounder: [dry-run] would archive %s to done/", slug)
            return PROJECT_ARCHIVED, in_flight
        archived = project_model.archive_to_done(project.path)
        logger.info("cofounder: archived %s -> %s", slug, archived)
        return PROJECT_ARCHIVED, in_flight

    # Step 2 — caps (active projects only; parked projects already wait).
    if status_mod.is_active(fm.status):
        caps_reason = _caps_reason(fm, entry, now)
        if caps_reason:
            outcome = _trip_caps(
                project, fm, entry, caps_reason, dry_run=dry_run, notify=notify, now=now
            )
            return outcome, in_flight

    # Step 3 — poll result was prefetched (one read-only query per job).
    job_id = str(fm.current_job_id) if fm.current_job_id else None
    if job_id is None:
        job_status = None
    else:
        job_status = str(row.get("status") or "").strip() if row else "unknown"
    working_path = row.get("working_path") if row else None
    job_running = job_id is not None and _job_in_flight(row)
    job_finished = job_id is not None and not job_running

    # Step 4 — steering: new [steer] lines past the state-json reply cursor.
    steering, log_lines_total = _new_steering(project, entry)

    # Parked projects wait for a human; only a reply wakes them.
    if status_mod.is_parked(fm.status) and not steering:
        logger.debug("cofounder: %s is parked (%s); waiting for a human", slug, fm.status)
        return PROJECT_PARKED, in_flight

    # Step 5 — the hard gate: NEVER dispatch while a job runs.
    if job_running:
        if not dry_run and row is not None:
            recovered = _zombie_upkeep(
                project, fm, entry, row, job_id, settings, state, state_path, now
            )
            if recovered is not None:
                return recovered, in_flight
        if not steering:
            if not dry_run:
                project_model.append_activity_log(
                    project.path,
                    f"[note] job {job_id} still in flight (status={job_status})",
                )
                project_model.update_frontmatter(
                    project.path, last_run=_local_now_iso()
                )
            return PROJECT_RUNNING, in_flight
        # New steering while a job runs: the human replied, so a decision
        # remains — the dispatch guard in _execute_dispatch still refuses.

    # Step 6 — completion: testing runs the executable check (never while the
    # job still runs; a mid-flight worktree cannot prove completion).
    if fm.status == "testing" and not job_running:
        outcome = _completion_path(
            project, fm, entry, working_path, dry_run=dry_run, notify=notify, now=now
        )
        return outcome, in_flight

    # Step 7 — LLM-needed classification (pure code; Invariant 5).
    if steering:
        reason = REASON_HUMAN_REPLIED
    elif job_finished:
        reason = REASON_JOB_FINISHED
    elif job_id is None and status_mod.is_active(fm.status):
        reason = REASON_NEW_PROJECT
    else:  # unreachable by the status partition; fail quiet, never crash
        logger.debug("cofounder: %s has nothing to do this pass", slug)
        return PROJECT_PARKED, in_flight

    # Concurrency cap: excess NEW work waits in discovery (queued) order —
    # and never burns an LLM call it could not act on.
    if reason == REASON_NEW_PROJECT and in_flight >= settings.max_concurrent:
        logger.info(
            "cofounder: %s waits in queue (%d in flight >= max_concurrent %d)",
            slug,
            in_flight,
            settings.max_concurrent,
        )
        return PROJECT_QUEUED, in_flight

    if decide is None:
        logger.info(
            "cofounder: %s%s needs a decision (%s) but no decider is available",
            prefix,
            slug,
            reason,
        )
        return PROJECT_DECISION_PENDING, in_flight

    context = {
        "reason": reason,
        "job_status": job_status,
        "working_path": working_path,
        "new_steering": list(steering),
        "iterations": fm.iterations,
        "in_flight": in_flight,
        "max_concurrent": settings.max_concurrent,
        "dry_run": dry_run,
    }
    decision = decide(project, context)
    if dry_run:
        logger.info(
            "cofounder: [dry-run] %s decision (%s): %r — not executed",
            slug,
            reason,
            decision,
        )
        return PROJECT_DECIDED_DRY, in_flight

    outcome, in_flight = _execute_decision(
        project,
        fm,
        entry,
        decision,
        settings,
        job_running=job_running,
        in_flight=in_flight,
        now=now,
        notify=notify,
    )
    # Steering consumed by this decision: advance the reply cursor to the
    # lines that existed when we read them (later appends stay fresh).
    entry["reply_cursor"] = log_lines_total
    return outcome, in_flight


def _job_in_flight(row: dict[str, Any] | None) -> bool:
    """A job counts as in flight unless its row PROVES it finished.

    ``row is None`` covers both an unreadable archon.db and a vanished run
    row — either way there is no physical proof the run ended (Rule 2), so
    the safe reading is in flight: polling continues, dispatch stays refused.
    """
    if row is None:
        return True
    status = str(row.get("status") or "").strip().lower()
    return status not in _FINISHED_JOB_STATUSES


def _new_steering(project, entry: dict[str, Any]) -> tuple[list[str], int]:
    """New ``[steer]`` Activity Log lines past the reply cursor.

    The cursor counts non-empty log lines already shown to a decision. It is
    clamped to the current line count so a rewritten log can never replay
    stale steering, and it only advances when a decision actually consumed
    the lines (dry runs and decider-less passes leave it untouched).
    """
    lines = [line for line in project.activity_log.splitlines() if line.strip()]
    cursor = entry.get("reply_cursor")
    if not isinstance(cursor, int) or cursor < 0:
        cursor = 0
    cursor = min(cursor, len(lines))
    steering = [line for line in lines[cursor:] if _STEER_MARKER in line]
    return steering, len(lines)


def _to_aware_utc(value: Any) -> datetime | None:
    """Fold an ISO string/datetime into ONE clock domain (aware UTC).

    Naive inputs are assumed UTC. A mixed naive/aware comparison crashed the
    reference build; folding both sides here makes the caps math
    tz-consistent no matter what an operator or an older pass wrote.
    Garbage degrades to ``None`` (no cap ever trips on an unreadable value).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip())
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _caps_reason(fm, entry: dict[str, Any], now: datetime) -> str | None:
    """Why the caps tripped, or None. Frontmatter owns the per-project caps
    (Rule 2 — the file is the truth); the COFOUNDER_MAX_* knobs seed new
    project files, they do not override a file's own values."""
    if fm.iterations >= fm.max_iterations:
        return f"iterations {fm.iterations} >= max_iterations {fm.max_iterations}"
    start = _to_aware_utc(entry.get("wall_clock_start"))
    if start is not None and now - start >= timedelta(hours=fm.max_wall_clock_hours):
        elapsed_hours = (now - start).total_seconds() / 3600.0
        return (
            f"wall clock {elapsed_hours:.1f}h >= max_wall_clock_hours "
            f"{fm.max_wall_clock_hours:g}"
        )
    return None


def _trip_caps(
    project,
    fm,
    entry: dict[str, Any],
    caps_reason: str,
    *,
    dry_run: bool,
    notify: Callable,
    now: datetime,
) -> str:
    """Flip a capped project to awaiting-human with ONE recorded notify."""
    from cofounder import project_model
    from cofounder import status as status_mod

    slug = project.slug
    if dry_run:
        logger.info(
            "cofounder: [dry-run] caps tripped for %s (%s); would park", slug, caps_reason
        )
        return PROJECT_CAPS_TRIPPED
    status_mod.transition(fm.status, "awaiting-human")
    project_model.update_frontmatter(
        project.path, status="awaiting-human", last_run=_local_now_iso()
    )
    project_model.append_activity_log(
        project.path, f"[caps] {caps_reason}; awaiting human"
    )
    _notify_once(
        project,
        entry,
        "awaiting-human:caps",
        "awaiting-human",
        f"{slug}: {caps_reason}; parked awaiting human",
        notify,
        now,
    )
    return PROJECT_CAPS_TRIPPED


def _zombie_upkeep(
    project,
    fm,
    entry: dict[str, Any],
    row: dict[str, Any],
    job_id: str,
    settings,
    state: dict[str, Any],
    state_path: Path,
    now: datetime,
) -> str | None:
    """Zombie side of the running gate; returns an outcome when it recovered.

    Classifies against the PRIOR pass's worktree mtime snapshot, then
    refreshes the snapshot — that ordering is what makes "no growth across a
    full pass cycle" true (a run's first sighting can never classify).
    """
    from cofounder import engine_archon, project_model
    from cofounder import state as state_mod

    slug = project.slug
    if engine_archon.classify_zombie(
        row, entry, stale_minutes=settings.zombie_stale_minutes, now=now
    ):
        args = entry.get("last_dispatch_args")
        if not (
            isinstance(args, dict)
            and args.get("workflow")
            and args.get("message")
            and args.get("repo_path")
        ):
            logger.warning(
                "cofounder: %s run %s classified zombie but no stored dispatch "
                "args; leaving for the operator",
                slug,
                job_id,
            )
            return None
        iteration = fm.iterations + 1
        branch = engine_archon.worktree_branch(slug, iteration)
        result = engine_archon.recover_zombie(
            project.path,
            job_id,
            args["workflow"],
            branch,
            args["message"],
            args["repo_path"],
            slug=slug,
            iteration=iteration,
            state=state,
            state_file=state_path,
            db_path=settings.archon_db,
        )
        # recover_zombie replaced this slug's entry inside `state`; fold the
        # rewrite back into our linked entry object.
        fresh = state_mod.get_project_state(state, slug)
        entry.clear()
        entry.update(fresh)
        state["projects"][slug] = entry
        if result.run_id is not None:
            project_model.update_frontmatter(
                project.path,
                current_job_id=result.run_id,
                iterations=iteration,
                branch=branch,
                last_run=_local_now_iso(),
            )
            entry["last_dispatch_at"] = _utc_iso(now)
        else:
            # The old run is dead (marked failed in local state) and the
            # re-dispatch is unconfirmed: clear the job id so the next pass
            # classifies a fresh decision — never a phantom `building`.
            project_model.update_frontmatter(
                project.path, current_job_id=None, last_run=_local_now_iso()
            )
            entry["last_dispatch_failed_at"] = _utc_iso(now)
        return PROJECT_ZOMBIE_RECOVERED
    snapshot = engine_archon.worktree_snapshot(job_id, row.get("working_path"))
    if snapshot is not None:
        entry[engine_archon.MTIME_SNAPSHOT_KEY] = snapshot
    return None


def _completion_path(
    project,
    fm,
    entry: dict[str, Any],
    working_path,
    *,
    dry_run: bool,
    notify: Callable,
    now: datetime,
) -> str:
    """Run the executable completion check for a ``testing`` project.

    Executable completion only: green flips to done (or parks for a human
    verdict when ``subjective_gate``); the same check failing twice — the
    fail streak lives in the state json — flips to blocked. A missing
    worktree counts as a failing check, so a mis-stamped ``testing`` surfaces
    to the operator through the same blocked path instead of spinning.
    """
    from cofounder import engine_archon, project_model
    from cofounder import status as status_mod

    slug = project.slug
    if dry_run:
        logger.info(
            "cofounder: [dry-run] would run completion check for %s in %r",
            slug,
            working_path,
        )
        return PROJECT_TESTING_DRY

    passed, output = engine_archon.completion_env(working_path, fm.completion_check)
    if passed:
        entry["fail_streak"] = 0
        if fm.subjective_gate:
            status_mod.transition(fm.status, "awaiting-human")
            project_model.update_frontmatter(
                project.path, status="awaiting-human", last_run=_local_now_iso()
            )
            project_model.append_activity_log(
                project.path, "[check] completion check green; awaiting human verdict"
            )
            _notify_once(
                project,
                entry,
                "awaiting-human:subjective",
                "awaiting-human",
                f"{slug}: completion check green; awaiting your verdict",
                notify,
                now,
            )
            return PROJECT_AWAITING_VERDICT
        status_mod.transition(fm.status, "done")
        project_model.update_frontmatter(
            project.path, status="done", last_run=_local_now_iso()
        )
        project_model.append_activity_log(
            project.path, "[check] completion check green; done"
        )
        _notify_once(
            project,
            entry,
            "done",
            "done",
            f"{slug}: completion check green; project done",
            notify,
            now,
        )
        archived = project_model.archive_to_done(project.path)
        logger.info("cofounder: %s done; archived -> %s", slug, archived)
        return PROJECT_DONE

    streak = int(entry.get("fail_streak") or 0) + 1
    entry["fail_streak"] = streak
    logger.warning(
        "cofounder: %s completion check failed (streak %d): %s",
        slug,
        streak,
        output[:500],
    )
    if streak >= 2:
        status_mod.transition(fm.status, "blocked")
        project_model.update_frontmatter(
            project.path, status="blocked", last_run=_local_now_iso()
        )
        project_model.append_activity_log(
            project.path, f"[check] completion check failed twice (streak {streak}); blocked"
        )
        _notify_once(
            project,
            entry,
            "blocked",
            "blocked",
            f"{slug}: completion check failed twice; blocked",
            notify,
            now,
        )
        return PROJECT_BLOCKED
    project_model.append_activity_log(
        project.path, f"[check] completion check failed (streak {streak}); will retry"
    )
    project_model.update_frontmatter(project.path, last_run=_local_now_iso())
    return PROJECT_CHECK_FAILED


def _execute_decision(
    project,
    fm,
    entry: dict[str, Any],
    decision: Any,
    settings,
    *,
    job_running: bool,
    in_flight: int,
    now: datetime,
    notify: Callable,
) -> tuple[str, int]:
    """Execute one decision — the model decides, CODE executes.

    Deterministic guards the model cannot override: no dispatch while a job
    runs, no dispatch past the concurrency cap, no minting ``done`` (only the
    executable completion path produces done — the action set has no such
    move), status derived from the action (never free-typed), and section
    ownership enforced by the project_model writers. An unusable decision is
    a logged no-op, never a partial write that corrupts state.
    """
    from cofounder import project_model

    slug = project.slug
    if not isinstance(decision, dict):
        logger.warning(
            "cofounder: %s decision is not a mapping (%r); no-op", slug, decision
        )
        return PROJECT_DECISION_NOOP, in_flight

    action = str(decision.get("action") or "").strip().lower()

    log_line = decision.get("log_line")
    if log_line:
        # A decision line may never mint steering for the next pass.
        safe_line = str(log_line).replace(_STEER_MARKER, "[steer-ref]")
        try:
            project_model.append_activity_log(project.path, safe_line)
        except Exception as exc:
            logger.warning("cofounder: %s decision log_line rejected (%s)", slug, exc)

    plan = decision.get("plan")
    if plan is not None:
        try:
            project_model.write_plan(project.path, str(plan))
        except Exception as exc:
            logger.warning("cofounder: %s decision plan rejected (%s)", slug, exc)

    if action == ACTION_PARK:
        outcome = _apply_status(
            project,
            fm,
            entry,
            "awaiting-human",
            now=now,
            notify=notify,
            notify_key="awaiting-human:parked",
            text=f"{slug}: parked awaiting human",
        )
        return outcome, in_flight
    if action == ACTION_TEST:
        return _apply_status(project, fm, entry, "testing", now=now), in_flight
    if action == ACTION_AUTHOR:
        return _execute_author(project, fm, entry, decision, now=now), in_flight
    if action == ACTION_REUSE:
        return _execute_dispatch(
            project,
            fm,
            entry,
            decision,
            settings,
            job_running=job_running,
            in_flight=in_flight,
            now=now,
        )
    logger.warning("cofounder: %s unknown decision action %r; no-op", slug, action)
    return PROJECT_DECISION_NOOP, in_flight


def _execute_author(
    project,
    fm,
    entry: dict[str, Any],
    decision: dict[str, Any],
    *,
    now: datetime,
) -> str:
    """Author decision (US-013): the model drafted; CODE validates and writes.

    The drafted YAML rides ``decision["message"]``. ``workflow_author`` owns
    validation, the backend-knob stamp (both levels), and the atomic write to
    ``<repo>/.archon/workflows/``. An invalid draft is a no-op with one
    ``[warn]`` Activity Log line; a written workflow is recorded in the state
    entry so every later pass re-stamps it against LLM drift.
    """
    from cofounder import project_model, repos, workflow_author

    slug = project.slug
    draft = str(decision.get("message") or "").strip()
    if not draft:
        logger.warning(
            "cofounder: %s author decision carries no draft in message; no-op", slug
        )
        return PROJECT_DECISION_NOOP

    try:
        resolution = repos.resolve_repo(fm.repo or "")
    except project_model.ProjectParseError as exc:  # RepoResolutionError included
        logger.warning(
            "cofounder: %s repo resolution failed (%s); no authoring", slug, exc
        )
        return PROJECT_DECISION_NOOP
    if resolution.local_path is None:
        logger.warning(
            "cofounder: %s resolves to no local path; no authoring", slug
        )
        return PROJECT_DECISION_NOOP

    written = workflow_author.author_workflow(resolution.local_path, draft)
    if written is None:
        try:
            project_model.append_activity_log(
                project.path, "[warn] authored workflow draft invalid; no-op"
            )
        except Exception as exc:
            logger.warning("cofounder: %s warn line append failed (%s)", slug, exc)
        return PROJECT_DECISION_NOOP

    authored = entry.get("authored_workflows")
    if not isinstance(authored, list):
        authored = []
        entry["authored_workflows"] = authored
    if str(written) not in authored:
        authored.append(str(written))
    project_model.append_activity_log(
        project.path, f"[author] workflow {written.stem} written to .archon/workflows/"
    )
    project_model.update_frontmatter(project.path, last_run=_local_now_iso())
    return PROJECT_AUTHORED


def _apply_status(
    project,
    fm,
    entry: dict[str, Any],
    target: str,
    *,
    now: datetime,
    notify: Callable | None = None,
    notify_key: str | None = None,
    text: str | None = None,
) -> str:
    """Flip a project's status through the machine and re-stamp the file."""
    from cofounder import project_model
    from cofounder import status as status_mod

    if fm.status != target:  # self-transitions are illegal by construction
        try:
            status_mod.transition(fm.status, target)
        except status_mod.IllegalTransitionError as exc:
            logger.warning(
                "cofounder: %s refused status flip (%s)", project.slug, exc
            )
            return PROJECT_DECISION_NOOP
        project_model.update_frontmatter(
            project.path, status=target, last_run=_local_now_iso()
        )
    else:
        project_model.update_frontmatter(project.path, last_run=_local_now_iso())
    if notify is not None and notify_key:
        _notify_once(
            project, entry, notify_key, target, text or project.slug, notify, now
        )
    return PROJECT_DECIDED


def _execute_dispatch(
    project,
    fm,
    entry: dict[str, Any],
    decision: dict[str, Any],
    settings,
    *,
    job_running: bool,
    in_flight: int,
    now: datetime,
) -> tuple[str, int]:
    """Dispatch one detached Archon run for a decision, fully guarded."""
    from cofounder import engine_archon, project_model, repos
    from cofounder import status as status_mod

    slug = project.slug
    if job_running:
        logger.warning(
            "cofounder: %s decision wants a dispatch while job %s is in flight; "
            "REFUSED (never dispatch while a job runs)",
            slug,
            fm.current_job_id,
        )
        return PROJECT_DISPATCH_REFUSED, in_flight
    if in_flight >= settings.max_concurrent:
        logger.info(
            "cofounder: %s dispatch queued (%d in flight >= max_concurrent %d)",
            slug,
            in_flight,
            settings.max_concurrent,
        )
        return PROJECT_QUEUED, in_flight

    workflow = str(decision.get("workflow") or fm.archon_workflow or "").strip()
    message = str(decision.get("message") or "").strip()
    if not workflow or not message:
        logger.warning(
            "cofounder: %s dispatch decision missing workflow or message; no-op", slug
        )
        return PROJECT_DECISION_NOOP, in_flight

    try:
        resolution = repos.resolve_repo(fm.repo or "")
    except project_model.ProjectParseError as exc:  # RepoResolutionError included
        logger.warning(
            "cofounder: %s repo resolution failed (%s); no dispatch", slug, exc
        )
        return PROJECT_DECISION_NOOP, in_flight
    if resolution.local_path is None:
        logger.warning(
            "cofounder: %s resolves to no local path (greenfield dispatch is not "
            "supported yet); no dispatch",
            slug,
        )
        return PROJECT_DECISION_NOOP, in_flight

    # Merge policy (US-017): a build into a pre-existing repo must leave a
    # PR for operator review — the orchestrator itself NEVER merges. The
    # amended message is what gets stored in last_dispatch_args, so a zombie
    # re-dispatch replays the policy too.
    message = _with_merge_policy(message, resolution)

    iteration = fm.iterations + 1
    branch = engine_archon.worktree_branch(slug, iteration)
    result = engine_archon.dispatch(
        workflow,
        branch,
        message,
        resolution.local_path,
        slug=slug,
        iteration=iteration,
        db_path=settings.archon_db,
    )
    if result.run_id is None:
        # No archon.db receipt within grace: the attempt is failed and NO
        # current_job_id is stamped — a phantom `building` can never exist.
        entry["last_dispatch_failed_at"] = _utc_iso(now)
        try:
            project_model.append_activity_log(
                project.path,
                f"[dispatch-failed] iteration {iteration}: no archon.db receipt "
                "within grace; attempt marked failed",
            )
        except Exception as exc:
            logger.warning(
                "cofounder: %s dispatch-failed log line failed (%s)", slug, exc
            )
        return PROJECT_DISPATCH_FAILED, in_flight

    if fm.status != "building":
        status_mod.transition(fm.status, "building")
    project_model.update_frontmatter(
        project.path,
        status="building",
        current_job_id=result.run_id,
        branch=branch,
        iterations=iteration,
        last_run=_local_now_iso(),
    )
    project_model.append_activity_log(
        project.path,
        f"[dispatch] iteration {iteration}: workflow {workflow} run {result.run_id}",
    )
    entry["last_dispatch_at"] = _utc_iso(now)
    if not entry.get("wall_clock_start"):
        entry["wall_clock_start"] = _utc_iso(now)
    entry["last_dispatch_args"] = {
        "workflow": workflow,
        "message": message,
        "repo_path": str(resolution.local_path),
    }
    entry[engine_archon.MTIME_SNAPSHOT_KEY] = None  # fresh run starts a fresh cycle
    entry["notified"] = {}  # new cycle: terminal-flip markers reset
    entry["fail_streak"] = 0
    return PROJECT_DISPATCHED, in_flight + 1


def _notify_once(
    project,
    entry: dict[str, Any],
    key: str,
    level: str,
    text: str,
    notify: Callable,
    now: datetime,
) -> bool:
    """Record ONE notify event per marker key, then deliver via the hook.

    The state-json marker is the recorded event (US-017's notify-once seam);
    the hook itself is a logging stub until US-014 and fail-open either way —
    a notify failure never breaks a pass (Invariant 6).
    """
    notified = entry.get("notified")
    if not isinstance(notified, dict):
        notified = {}
        entry["notified"] = notified
    if key in notified:
        return False
    notified[key] = _utc_iso(now)
    try:
        notify(project, text, level)
    except Exception as exc:
        logger.warning("cofounder: notify hook failed for %s (%s)", project.slug, exc)
    return True


def _with_merge_policy(message: str, resolution) -> str:
    """Append the PR-for-review instruction for pre-existing repos.

    Greenfield (system-owned) repos may commit straight to their default
    branch, so the sentinel resolution is exempt. The append is idempotent —
    a stored message replayed by a zombie re-dispatch never stacks it.
    """
    if resolution.greenfield or MERGE_POLICY_INSTRUCTION in message:
        return message
    return f"{message}\n\n{MERGE_POLICY_INSTRUCTION}"


def _resolve_decide() -> Callable | None:
    """The production default decider: the US-012 LLM orchestration step.

    Resolved at call time through the module attribute (Rule 3 — tests
    monkeypatch ``cofounder.orchestrate.decide`` and the pass picks it up);
    a broken orchestrate module fails open to ``None``, which the pipeline
    logs as a pending decision (Invariant 6: the deterministic pass keeps
    polling, archiving, and gating even when the decider cannot load).
    """
    try:
        from cofounder import orchestrate as orchestrate_mod

        return orchestrate_mod.decide
    except Exception:
        logger.warning(
            "cofounder: orchestrate module unavailable; decisions stay pending",
            exc_info=True,
        )
        return None


def _resolve_notify() -> Callable:
    """The production default notify hook: the gated Telegram sender.

    Resolved at call time through the module attribute (Rule 3 — tests
    monkeypatch ``cofounder.notify.notify`` and the pass picks it up); a
    broken notify module fails open to the logging stub (Invariant 6: a
    notify failure never breaks a pass).
    """
    try:
        from cofounder import notify as notify_mod

        return notify_mod.notify
    except Exception:
        logger.warning(
            "cofounder: notify module unavailable; using the logging stub",
            exc_info=True,
        )
        return _stub_notify


def _stub_notify(project, text: str, level: str) -> bool:
    """Fail-open fallback when the gated Telegram sender cannot be imported."""
    logger.info("cofounder: [notify:%s] %s", level, text)
    return False


def _local_now_iso() -> str:
    """Naive-local ISO for the human-facing frontmatter ``last_run`` stamp."""
    return datetime.now().isoformat(timespec="seconds")


def _utc_iso(now: datetime) -> str:
    """Aware-UTC ISO for state-json bookkeeping (the caps-math clock domain)."""
    return now.isoformat()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cofounder.run_pass",
        description="Run one co-founder orchestration pass.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="dry run: full discovery + decision logging, no dispatch/notify/state writes",
    )
    parser.add_argument(
        "--project",
        metavar="SLUG",
        default=None,
        help="restrict the pass to one project slug",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    result = run_pass(dry_run=args.test, only_project=args.project)
    logger.info("cofounder: pass outcome=%s projects=%d", result.outcome, len(result.projects_seen))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
