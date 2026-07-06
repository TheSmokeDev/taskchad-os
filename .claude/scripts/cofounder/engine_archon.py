"""Archon engine adapter — poll, dispatch, completion, zombies (US-008/009/010).

Rule 2: the Archon SQLite rows are the ONLY truth about in-flight builds —
never a cached claim, never the model's own bookkeeping. This adapter opens
``~/.archon/archon.db`` with a ``file:...?mode=ro`` URI so it is physically
incapable of writing the DB (workspace safety rule: never modify archon.db)
and so concurrent Archon writes stay WAL-consistent under our reads.

Every failure degrades instead of raising (Global Invariant 6): missing db,
locked db, garbage file, missing table, missing row — all fold into
``("unknown", None)`` so a broken poll can never crash a pass or the
heartbeat riding above it.

Dispatch (US-009) is DETACHED (Global Invariant 7): the archon child is
spawned via ``shared.spawn_detached`` so it survives the pass exiting, its
stdout is never awaited, and the archon.db row is the only dispatch receipt.
:func:`capture_run_id` recovers the run_id from that row within a bounded
grace window; expiry means the dispatch is presumed dead and the caller must
mark the attempt failed WITHOUT stamping ``current_job_id`` (no phantom
``building`` state can exist).

Completion + zombies (US-010): :func:`completion_env` runs a project's
executable ``completion_check`` INSIDE the build worktree with a bounded
timeout — the agent's self-report is never a completion signal, the exit
code is. :func:`classify_zombie` applies the two-signal rule (DB staleness
AND no worktree mtime growth across a full pass cycle — either alone
false-positives) and :func:`recover_zombie` marks the dead run in LOCAL
state, re-dispatches detached, and appends exactly one Activity Log line.

The DB path resolves through ``config.get_cofounder_settings().archon_db``
at call time (Rule 1) when not passed explicitly; tests point
``COFOUNDER_ARCHON_DB`` (or the ``db_path`` arg) at a fixture db.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

RUNS_TABLE = "remote_agent_workflow_runs"

# Columns this adapter reads (verified against the live archon.db DDL).
# started_at / completed_at / last_activity_at are naive-UTC strings from
# SQLite's datetime('now') — e.g. "2026-07-04 05:51:50" (US-009/US-010 must
# compare in that clock domain).
RUN_COLUMNS = (
    "status",
    "working_path",
    "started_at",
    "completed_at",
    "last_activity_at",
)

UNKNOWN_STATUS = "unknown"

# Bounded busy-wait on a locked db; expiry degrades to unknown, never a hang.
_CONNECT_TIMEOUT_S = 2.0


def _resolve_db_path(db_path: Path | str | None) -> Path:
    """None resolves ``settings.archon_db`` at call time (Rule 1)."""
    if db_path is not None:
        return Path(db_path)
    import config

    return config.get_cofounder_settings().archon_db


def _read_only_uri(db_path: Path) -> str:
    """``file:///...?mode=ro`` URI — write-refusing at the driver level.

    ``Path.as_uri()`` percent-encodes and emits the ``file:///C:/...`` form
    sqlite3 accepts on Windows; ``mode=ro`` makes any write attempt raise
    inside SQLite itself, WAL databases included.
    """
    return db_path.absolute().as_uri() + "?mode=ro"


def fetch_run_row(
    run_id: str | None, db_path: Path | str | None = None
) -> dict[str, Any] | None:
    """One run's :data:`RUN_COLUMNS` as a dict, or ``None`` on ANY failure.

    The fail-open boundary for archon.db reads: empty run_id, unopenable or
    non-SQLite file, missing table, and missing row all log a warning and
    return ``None`` — :func:`poll` folds that into ``("unknown", None)``.
    """
    if run_id is None or not str(run_id).strip():
        return None
    path = _resolve_db_path(db_path)
    query = f"SELECT {', '.join(RUN_COLUMNS)} FROM {RUNS_TABLE} WHERE id = ?"
    try:
        connection = sqlite3.connect(
            _read_only_uri(path), uri=True, timeout=_CONNECT_TIMEOUT_S
        )
        try:
            row = connection.execute(query, (str(run_id),)).fetchone()
        finally:
            connection.close()
    except Exception as exc:
        logger.warning(
            "cofounder: archon.db poll failed for run %s at %s (%s)",
            run_id,
            path,
            exc,
        )
        return None
    if row is None:
        logger.warning("cofounder: run %s not found in %s", run_id, path)
        return None
    return dict(zip(RUN_COLUMNS, row))


def poll(
    run_id: str | None, db_path: Path | str | None = None
) -> tuple[str, str | None]:
    """``(status, working_path)`` for one Archon run.

    ANY read failure — missing db, locked, garbage file, missing row, NULL
    or blank status — returns ``(UNKNOWN_STATUS, None)``; never raises.
    ``working_path`` is ``None`` when the row has none.
    """
    row = fetch_run_row(run_id, db_path=db_path)
    if row is None:
        return (UNKNOWN_STATUS, None)
    status = row.get("status")
    if status is None or not str(status).strip():
        return (UNKNOWN_STATUS, None)
    working_path = row.get("working_path")
    return (str(status), str(working_path) if working_path else None)


# === US-009: detached dispatch + run_id capture ===

# Fixed literals (not env-tunable knobs); the None-sentinel args let tests
# shrink them without touching module state.
GRACE_SECONDS_DEFAULT = 90.0
POLL_INTERVAL_DEFAULT = 3.0

LOGS_SUBDIR = "cofounder"


@dataclass(frozen=True)
class DispatchResult:
    """One dispatch attempt's receipt.

    ``run_id is None`` means the dispatch is UNCONFIRMED (no archon.db row
    within the grace window, spawn failure, or missing repo path) — the
    caller must mark the attempt failed and stamp NO ``current_job_id``.
    """

    run_id: str | None
    pid: int | None
    argv: tuple[str, ...]
    log_path: Path
    dispatched_at: str  # naive-UTC "YYYY-MM-DD HH:MM:SS" (archon.db clock domain)


def worktree_branch(slug: str, iteration: int) -> str:
    """Worktree branch name for one dispatch: ``cofounder/<slug>-<iteration>``."""
    return f"cofounder/{slug}-{iteration}"


def _resolve_archon_bin(archon_bin: Path | str | None) -> str:
    """Explicit path wins; else the standard install, else PATH lookup.

    The standard install is ``~/.archon/bin/archon.exe`` (workspace layout);
    resolved at call time so tests can repoint ``Path.home()``. When absent,
    the bare name relies on the pinned child PATH (:func:`build_child_env`).
    """
    if archon_bin is not None:
        return str(archon_bin)
    exe = "archon.exe" if sys.platform == "win32" else "archon"
    default = Path.home() / ".archon" / "bin" / exe
    if default.exists():
        return str(default)
    return "archon"


def build_dispatch_argv(
    workflow: str,
    branch: str,
    message: str,
    repo_path: Path | str,
    archon_bin: Path | str | None = None,
) -> list[str]:
    """``archon workflow run <workflow> --branch <branch> --cwd <repo> <message>``.

    List-form argv — no shell, no quoting pitfalls; the message rides as one
    argument exactly as typed.
    """
    return [
        _resolve_archon_bin(archon_bin),
        "workflow",
        "run",
        str(workflow),
        "--branch",
        str(branch),
        "--cwd",
        str(repo_path),
        str(message),
    ]


def build_child_env(
    archon_bin: Path | str | None = None,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Child env for a detached archon spawn.

    Copy of ``os.environ`` with every ``CLAUDECODE*`` key removed (Archon
    hangs under a Claude Code session env — repo memory) and PATH pinned to
    include the archon bin dir + git's dir (headless Task Scheduler shells
    ship a minimal PATH).
    """
    source = os.environ if base_env is None else base_env
    env = {
        key: value
        for key, value in source.items()
        if not key.upper().startswith("CLAUDECODE")
    }
    pinned: list[str] = []
    bin_parent = str(Path(_resolve_archon_bin(archon_bin)).parent)
    if bin_parent in ("", "."):
        # Bare "archon" fallback — pin the standard install dir anyway.
        bin_parent = str(Path.home() / ".archon" / "bin")
    pinned.append(bin_parent)
    git = shutil.which("git")
    if git:
        pinned.append(str(Path(git).parent))
    elif sys.platform == "win32":
        default_git = Path("C:/Program Files/Git/cmd")
        if (default_git / "git.exe").exists():
            pinned.append(str(default_git))
    parts = [part for part in env.get("PATH", "").split(os.pathsep) if part]
    for entry in reversed(pinned):
        if entry not in parts:
            parts.insert(0, entry)
    env["PATH"] = os.pathsep.join(parts)
    return env


def dispatch_log_path(
    slug: str, iteration: int, logs_dir: Path | str | None = None
) -> Path:
    """Per-dispatch log: ``.claude/data/logs/cofounder/<slug>-<n>.log``.

    ``logs_dir=None`` derives ``config.DATA_DIR`` at call time (Rule 1;
    same discipline as ``state._resolve_state_file`` — no COFOUNDER module
    constant may live in config).
    """
    if logs_dir is None:
        import config

        logs_dir = Path(config.DATA_DIR) / "logs" / LOGS_SUBDIR
    return Path(logs_dir) / f"{slug}-{iteration}.log"


def _utc_db_now() -> str:
    """Naive-UTC ``YYYY-MM-DD HH:MM:SS`` — archon.db's ``datetime('now')``
    clock domain (verified live; local time or T-separated isoformat would
    compare wrong)."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def capture_run_id(
    workflow: str,
    message: str,
    dispatched_at: str,
    db_path: Path | str | None = None,
    grace_seconds: float | None = None,
    poll_interval: float | None = None,
) -> str | None:
    """Recover a dispatch's run_id from archon.db (Rule 2: the row IS the
    receipt).

    Polls ``remote_agent_workflow_runs`` for the newest row matching
    ``workflow_name`` + ``user_message`` with ``started_at >= dispatched_at``
    (string compare in the fixed naive-UTC format). Returns the id when
    found; ``None`` when the grace window expires — the dispatch is presumed
    dead and must NOT be stamped as ``current_job_id``.
    """
    if grace_seconds is None:
        grace_seconds = GRACE_SECONDS_DEFAULT
    if poll_interval is None:
        poll_interval = POLL_INTERVAL_DEFAULT
    path = _resolve_db_path(db_path)
    query = (
        f"SELECT id FROM {RUNS_TABLE} "
        "WHERE workflow_name = ? AND user_message = ? AND started_at >= ? "
        "ORDER BY started_at DESC LIMIT 1"
    )
    deadline = time.monotonic() + grace_seconds
    while True:
        row = None
        try:
            connection = sqlite3.connect(
                _read_only_uri(path), uri=True, timeout=_CONNECT_TIMEOUT_S
            )
            try:
                row = connection.execute(
                    query, (workflow, message, dispatched_at)
                ).fetchone()
            finally:
                connection.close()
        except Exception as exc:
            logger.warning(
                "cofounder: run_id capture query failed at %s (%s)", path, exc
            )
        if row is not None and row[0]:
            return str(row[0])
        if time.monotonic() >= deadline:
            logger.warning(
                "cofounder: no archon.db row for workflow %s within %.0fs grace; "
                "dispatch presumed dead",
                workflow,
                grace_seconds,
            )
            return None
        time.sleep(poll_interval)


def dispatch(
    workflow: str,
    branch: str,
    message: str,
    repo_path: Path | str,
    *,
    slug: str,
    iteration: int,
    db_path: Path | str | None = None,
    grace_seconds: float | None = None,
    poll_interval: float | None = None,
    logs_dir: Path | str | None = None,
    archon_bin: Path | str | None = None,
) -> DispatchResult:
    """Spawn one detached Archon run and confirm it against archon.db.

    Never raises: a missing repo path, a spawn failure, or an expired grace
    window all return a :class:`DispatchResult` with ``run_id=None`` so the
    pass records a failed attempt instead of a phantom ``building`` row.
    """
    argv = build_dispatch_argv(workflow, branch, message, repo_path, archon_bin)
    log_path = dispatch_log_path(slug, iteration, logs_dir=logs_dir)
    dispatched_at = _utc_db_now()  # pre-spawn: the row's started_at is >= this
    if not Path(repo_path).exists():
        logger.warning(
            "cofounder: dispatch refused for %s — repo path %s does not exist",
            slug,
            repo_path,
        )
        return DispatchResult(
            run_id=None,
            pid=None,
            argv=tuple(argv),
            log_path=log_path,
            dispatched_at=dispatched_at,
        )
    import shared  # module-attribute lookup so monkeypatched spawns propagate

    try:
        pid: int | None = shared.spawn_detached(
            argv,
            env=build_child_env(archon_bin),
            log_path=log_path,
            cwd=repo_path,
        )
    except Exception as exc:
        logger.warning("cofounder: dispatch spawn failed for %s (%s)", slug, exc)
        return DispatchResult(
            run_id=None,
            pid=None,
            argv=tuple(argv),
            log_path=log_path,
            dispatched_at=dispatched_at,
        )
    run_id = capture_run_id(
        workflow,
        message,
        dispatched_at,
        db_path=db_path,
        grace_seconds=grace_seconds,
        poll_interval=poll_interval,
    )
    return DispatchResult(
        run_id=run_id,
        pid=pid,
        argv=tuple(argv),
        log_path=log_path,
        dispatched_at=dispatched_at,
    )


# === US-010: completion checks + zombie detection ===

# Fixed literals (US-009 precedent): not env knobs; the None-sentinel args
# let tests shrink the timeout without touching module state.
COMPLETION_TIMEOUT_DEFAULT = 600.0
COMPLETION_OUTPUT_CAP = 10_000

# Per-project state-entry key for the worktree mtime snapshot (US-004's open
# schema; Rule 2 — physical disk state only, taken by the pass each cycle).
MTIME_SNAPSHOT_KEY = "mtime_snapshot"

# Only a DB row that still CLAIMS to be running can be a zombie.
ZOMBIE_STATUS = "running"


def _cap_output(text: str, limit: int = COMPLETION_OUTPUT_CAP) -> str:
    """Keep the TAIL of oversized check output — the verdict lines live there."""
    if len(text) <= limit:
        return text
    return "[output truncated]\n" + text[-limit:]


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill ONLY this check's process tree — never a kill-all (process safety
    rule). ``taskkill /T`` reaps grandchildren the shell spawned; without it
    a timed-out ``cmd.exe`` would die while its pytest child runs on holding
    our pipe open."""
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
            )
        else:
            proc.kill()
    except Exception:
        pass


def completion_env(
    working_path: Path | str | None,
    completion_check: str | None,
    timeout_seconds: float | None = None,
) -> tuple[bool, str]:
    """Run a project's ``completion_check`` inside the build worktree.

    Executable completion only (prd.md Phase 7): the agent's self-report
    never counts — ``passed`` is True iff the operator-authored check exits
    0 within the timeout, with ``cwd=working_path`` and stdout+stderr
    captured (merged, tail-capped). A timeout kills the check's process
    tree and returns failed — never a hang; a missing worktree, empty
    check, or spawn failure also returns ``(False, <reason>)``, never an
    exception.
    """
    if timeout_seconds is None:
        timeout_seconds = COMPLETION_TIMEOUT_DEFAULT
    check = str(completion_check or "").strip()
    if not check:
        return (False, "no completion_check configured")
    if working_path is None or not Path(working_path).is_dir():
        return (False, f"working path does not exist: {working_path!r}")
    try:
        proc = subprocess.Popen(
            check,
            shell=True,  # the check is an operator-authored shell line
            cwd=str(working_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
    except Exception as exc:
        return (False, f"completion_check failed to start ({exc})")
    try:
        output, _ = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        try:  # drain what the dead tree left in the pipe, bounded
            output, _ = proc.communicate(timeout=5.0)
        except Exception:
            output = ""
        note = f"completion_check timed out after {timeout_seconds:.0f}s"
        tail = (output or "").strip()
        return (False, _cap_output(f"{note}\n{tail}" if tail else note))
    except Exception as exc:
        _kill_process_tree(proc)
        return (False, f"completion_check errored ({exc})")
    return (proc.returncode == 0, _cap_output((output or "").strip()))


def worktree_mtime(working_path: Path | str | None) -> float | None:
    """Newest ``st_mtime`` anywhere under the worktree (Rule 2: physical
    disk state). ``None`` when the path is missing, not a directory, or
    unreadable — an 'unknown' worktree can never feed a zombie verdict."""
    if working_path is None or not str(working_path).strip():
        return None
    root = Path(working_path)
    try:
        if not root.is_dir():
            return None
        newest = root.stat().st_mtime
        for dirpath, _dirnames, filenames in os.walk(root):
            paths = [dirpath] + [os.path.join(dirpath, name) for name in filenames]
            for candidate in paths:
                try:
                    mtime = os.stat(candidate).st_mtime
                except OSError:
                    continue  # files can vanish mid-scan
                if mtime > newest:
                    newest = mtime
        return newest
    except Exception as exc:
        logger.warning(
            "cofounder: worktree mtime scan failed for %s (%s)", working_path, exc
        )
        return None


def worktree_snapshot(
    run_id: str | None, working_path: Path | str | None
) -> dict[str, Any] | None:
    """One pass's mtime snapshot, stored by the pass (US-011) under
    :data:`MTIME_SNAPSHOT_KEY` in the project's state entry. ``None`` when
    the worktree is unusable — an untrustable snapshot is never written.

    The pass must classify against the PRIOR pass's snapshot BEFORE
    refreshing it — that ordering is what makes 'no growth across a full
    pass cycle' true.
    """
    mtime = worktree_mtime(working_path)
    if mtime is None:
        return None
    return {
        "run_id": str(run_id) if run_id else None,
        "path": str(working_path),
        "mtime": mtime,
        "taken_at": _utc_db_now(),
    }


def _parse_db_timestamp(value: Any) -> datetime | None:
    """archon.db naive-UTC ``YYYY-MM-DD HH:MM:SS`` -> naive-UTC datetime.

    Aware inputs are folded INTO naive-UTC so every comparison stays in one
    clock domain (the reference build crashed on a mixed-tz comparison).
    Garbage degrades to ``None``.
    """
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def classify_zombie(
    run_row: dict[str, Any] | None,
    state: dict[str, Any] | None,
    *,
    stale_minutes: int | None = None,
    now: datetime | None = None,
) -> bool:
    """Two-signal zombie rule (prd.md Phase 4).

    True ONLY when the DB row still claims ``running`` with
    ``last_activity_at`` staler than ``COFOUNDER_ZOMBIE_STALE_MINUTES`` AND
    the worktree's newest mtime shows no growth over the prior pass's
    snapshot (:data:`MTIME_SNAPSHOT_KEY` in the per-project state entry).
    Either signal alone false-positives, so every unknown — missing row,
    unparseable timestamp, missing working_path, no prior snapshot, snapshot
    from a different worktree — degrades to False: recovery fires only on
    positive physical proof (Rule 2). Pure read; no state is written here.
    """
    if not isinstance(run_row, dict):
        return False
    if run_row.get("status") != ZOMBIE_STATUS:
        return False
    last_activity = _parse_db_timestamp(run_row.get("last_activity_at"))
    if last_activity is None:
        return False
    if stale_minutes is None:
        import config

        stale_minutes = config.get_cofounder_settings().zombie_stale_minutes
    if now is None:
        now = datetime.now(UTC).replace(tzinfo=None)
    elif now.tzinfo is not None:
        now = now.astimezone(UTC).replace(tzinfo=None)
    if now - last_activity < timedelta(minutes=stale_minutes):
        return False
    working_path = run_row.get("working_path")
    if working_path is None or not str(working_path).strip():
        return False
    current = worktree_mtime(working_path)
    if current is None:
        return False
    snapshot = state.get(MTIME_SNAPSHOT_KEY) if isinstance(state, dict) else None
    if not isinstance(snapshot, dict):
        return False  # no prior-cycle snapshot -> growth is unprovable yet
    if str(snapshot.get("path", "")) != str(working_path):
        return False  # a snapshot of another worktree proves nothing
    previous = snapshot.get("mtime")
    if not isinstance(previous, (int, float)):
        return False
    return current <= previous


def recover_zombie(
    project_path: Path | str,
    run_id: str | None,
    workflow: str,
    branch: str,
    message: str,
    repo_path: Path | str,
    *,
    slug: str,
    iteration: int,
    state: dict[str, Any],
    state_file: Path | str | None = None,
    db_path: Path | str | None = None,
    grace_seconds: float | None = None,
    poll_interval: float | None = None,
    logs_dir: Path | str | None = None,
    archon_bin: Path | str | None = None,
) -> DispatchResult:
    """Recover one classified zombie: mark the run failed in LOCAL state,
    re-dispatch detached, append exactly ONE Activity Log line.

    ``state`` is the loaded full-state mapping and the write goes through
    ``state._write_state`` — the CALLER (the pass) already holds the pass
    lock on cofounder-state.json and ``file_lock`` is not re-entrant, so
    ``update_project_state`` here would deadlock against our own pass
    (US-005 discipline). archon.db is never written (Rule 2); the failed
    mark lives in bookkeeping only. Never raises: a state or log-line
    failure warns and the recovery continues (Invariant 6). The returned
    ``DispatchResult`` may still carry ``run_id=None`` — the caller must
    treat that as a failed attempt, never a phantom ``building``.
    """
    try:
        from cofounder import state as state_mod

        entry = state_mod.get_project_state(state, slug)
        entry["last_zombie_run_id"] = str(run_id) if run_id else None
        entry[MTIME_SNAPSHOT_KEY] = None  # the new run starts a fresh cycle
        projects = state.get("projects")
        if not isinstance(projects, dict):
            projects = {}
            state["projects"] = projects
        projects[slug] = entry
        state_mod._write_state(state, state_mod._resolve_state_file(state_file))
    except Exception as exc:
        logger.warning("cofounder: zombie state mark failed for %s (%s)", slug, exc)
    result = dispatch(
        workflow,
        branch,
        message,
        repo_path,
        slug=slug,
        iteration=iteration,
        db_path=db_path,
        grace_seconds=grace_seconds,
        poll_interval=poll_interval,
        logs_dir=logs_dir,
        archon_bin=archon_bin,
    )
    line = (
        f"[zombie] run {run_id or 'unknown'} stale with no worktree growth; "
        f"marked failed, re-dispatched ({result.run_id or 'unconfirmed'})"
    )
    try:
        from cofounder import project_model

        project_model.append_activity_log(Path(project_path), line)
    except Exception as exc:
        logger.warning("cofounder: zombie log line failed for %s (%s)", slug, exc)
    return result
