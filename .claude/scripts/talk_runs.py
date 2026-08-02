"""Unified async-run registry for Talk mode.

Voice never blocks. Anything slower than a couple of seconds — a skill pack,
a background agent, an Archon workflow, a screen look — starts here, returns
a spoken receipt immediately, and is polled by the Talk page until it lands.

One registry, four kinds:

- ``skill``  — a SKILL.md executed through an engine lane subprocess
- ``agent``  — a delegated task executed through an engine lane subprocess
- ``archon`` — a detached Archon workflow (its own process tree)
- ``look``   — a screenshot described by a vision model

The sentinel string is the contract between this module, the Realtime model,
and the browser poller: a handler returns ``WORK_STARTED #<id> kind=<kind>``
and the page starts polling ``/api/talk/runs/<id>``.

Runs live in memory and die with the API process (the durable receipts are
elsewhere: the work-queue row for ``agent``, ``archon.db`` for ``archon``).
Terminal entries are evicted so a long-lived process cannot grow this dict
without bound.

The registry additionally TEES every start and terminal transition to a
JSONL history file (``STATE_DIR/talk-runs.jsonl``) so the dashboard Runs
panel can show runs that predate the current API process. The tee is
fail-open — history IO must never break the registry — and the file is
compacted in place when it outgrows its byte cap. A history row that says
``running`` but has no live registry entry belonged to a DEAD API process:
it is reported as ``lost`` (for ``archon`` kind that means the WATCHER was
lost — the detached workflow itself may well have finished fine, and
``archon.db`` remains its authoritative ledger).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Callable

import config

_log = logging.getLogger(__name__)

RUN_KINDS = ("skill", "agent", "archon", "look")
TERMINAL_STATUSES = ("done", "failed")

# Eviction policy — terminal runs only; a running entry is never evicted.
_RUN_TTL_S = 24 * 60 * 60
_MAX_RUNS = 200

_RUNS: dict[int, dict] = {}
_RUN_LOCK = threading.Lock()
_RUN_SEQ = 0

# History tee knobs. Output is capped per record — a run's full text lives in
# the registry while it lives; history is a telemetry record, not a store.
_HISTORY_FILENAME = "talk-runs.jsonl"
_HISTORY_MAX_BYTES = 512_000
_HISTORY_COMPACT_KEEP = 300
_HISTORY_OUTPUT_CAP = 2_000
_HISTORY_TAIL_LINES = 600
# Lock ordering: _HISTORY_LOCK is always taken WITHOUT _RUN_LOCK held (tees
# happen after the registry mutation completes).
_HISTORY_LOCK = threading.Lock()


def started_sentinel(run_id: int, kind: str, label: str) -> str:
    """The receipt a tool handler returns so the browser starts polling."""

    return f"WORK_STARTED #{run_id} kind={kind} ({label})"


def _history_path():
    """STATE_DIR resolved at call time (Rule 1) — tests repoint it."""

    return config.STATE_DIR / _HISTORY_FILENAME


def _history_enabled() -> bool:
    """Inert under pytest unless a test explicitly opts in.

    Many suites exercise this registry transitively WITHOUT repointing
    ``config.STATE_DIR`` — an always-on tee would write test junk into the
    operator's real state dir from any of them. History tests monkeypatch
    this to ``lambda: True`` alongside a repointed STATE_DIR.
    """

    return "PYTEST_CURRENT_TEST" not in os.environ


def _append_history(record: dict) -> None:
    """Fail-open tee. History IO must never break the registry."""

    if not _history_enabled():
        return
    try:
        with _HISTORY_LOCK:
            path = _history_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            if path.stat().st_size > _HISTORY_MAX_BYTES:
                _compact_history_locked(path)
    except Exception as exc:  # noqa: BLE001 — telemetry, not truth
        _log.warning("talk run history append failed: %s", exc)


def _compact_history_locked(path) -> None:
    """Rewrite keeping the newest record per run, newest runs first dropped last.

    Caller holds ``_HISTORY_LOCK``. Atomic via tmp + ``os.replace``.
    ``errors="replace"`` so one torn/invalid byte cannot wedge compaction
    forever (a strict decode would raise BEFORE per-line handling, letting
    the file grow unbounded) — the torn line fails ``json.loads`` and is
    dropped, which is the designed degradation.
    """

    records: dict[int, dict] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
            records[int(rec["runId"])] = rec
        except Exception:  # noqa: BLE001 — a torn line is dropped, not fatal
            continue
    keep = sorted(records.keys(), reverse=True)[:_HISTORY_COMPACT_KEEP]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        "".join(
            json.dumps(records[rid], ensure_ascii=False) + "\n"
            for rid in sorted(keep)
        ),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _history_seed_floor() -> int:
    """Highest persisted run id, distinguishing 'no history' from 'unreadable'.

    An absent file (or a disabled tee) seeds from zero. A file that EXISTS
    but cannot be read must NOT — seeding from zero would mint ids that
    collide with persisted history the merge and compactor key on. Wall-
    clock seconds is a floor no plausible sequence has reached, so the
    restarted process stays collision-free at the cost of a big id.
    """

    if not _history_enabled():
        return 0
    try:
        with _HISTORY_LOCK:
            path = _history_path()
            if not path.exists():
                return 0
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        ids = []
        for line in lines:
            try:
                ids.append(int(json.loads(line)["runId"]))
            except Exception:  # noqa: BLE001 — a torn line costs itself only
                continue
        return max(ids, default=0)
    except Exception as exc:  # noqa: BLE001 — unreadable-but-present history
        _log.warning("talk run history unreadable at seed time: %s", exc)
        return int(time.time())


def _load_history() -> dict[int, dict]:
    """Newest record per run from the JSONL tail. Fail-open to empty."""

    if not _history_enabled():
        return {}
    try:
        with _HISTORY_LOCK:
            path = _history_path()
            if not path.exists():
                return {}
            # errors="replace": a torn byte must cost one line, not the file.
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        records: dict[int, dict] = {}
        for line in lines[-_HISTORY_TAIL_LINES:]:
            try:
                rec = json.loads(line)
                records[int(rec["runId"])] = rec
            except Exception:  # noqa: BLE001
                continue
        return records
    except Exception as exc:  # noqa: BLE001
        _log.warning("talk run history read failed: %s", exc)
        return {}


def _evict_locked() -> None:
    """Drop stale terminal runs. Caller holds the lock."""

    if not _RUNS:
        return
    cutoff = time.time() - _RUN_TTL_S
    for run_id in [
        rid
        for rid, run in _RUNS.items()
        if run["status"] in TERMINAL_STATUSES and run["updated"] < cutoff
    ]:
        _RUNS.pop(run_id, None)
    if len(_RUNS) <= _MAX_RUNS:
        return
    terminal = sorted(
        (rid for rid, run in _RUNS.items() if run["status"] in TERMINAL_STATUSES),
        key=lambda rid: _RUNS[rid]["updated"],
    )
    for run_id in terminal[: len(_RUNS) - _MAX_RUNS]:
        _RUNS.pop(run_id, None)


def start_run(
    kind: str,
    label: str,
    worker: Callable[[int], str],
    *,
    meta: dict | None = None,
) -> int:
    """Register a run and spawn its daemon worker thread.

    ``worker(run_id)`` returns the final text to speak. Raising marks the run
    ``failed`` with the exception text — the registry always terminates.
    """

    if kind not in RUN_KINDS:
        raise ValueError(f"unknown run kind: {kind!r}")

    global _RUN_SEQ
    # Seed the sequence past the persisted history once per process so run
    # ids stay monotonic across API restarts — the history merge keys on
    # runId, and a restarted process must not mint colliding ids.
    if _RUN_SEQ == 0:
        floor = _history_seed_floor()
        with _RUN_LOCK:
            if _RUN_SEQ == 0:
                _RUN_SEQ = floor
    with _RUN_LOCK:
        _RUN_SEQ += 1
        run_id = _RUN_SEQ
        now = time.time()
        _RUNS[run_id] = {
            "kind": kind,
            "label": label,
            "status": "running",
            "output": "",
            "meta": dict(meta or {}),
            "ts": now,
            "updated": now,
        }
        # Evict AFTER inserting so the cap holds for the registry as it now
        # stands; the entry just added is running, so it is never a candidate.
        _evict_locked()
    _append_history(
        {
            "runId": run_id,
            "kind": kind,
            "label": label,
            "status": "running",
            "ts": now,
            "updated": now,
        }
    )
    thread = threading.Thread(
        target=_run_worker,
        args=(run_id, worker),
        name=f"talk-run-{kind}-{run_id}",
        daemon=True,
    )
    thread.start()
    return run_id


def _run_worker(run_id: int, worker: Callable[[int], str]) -> None:
    try:
        output = worker(run_id)
        finish_run(run_id, "done", output)
    except Exception as exc:  # noqa: BLE001 — a run must always terminate
        _log.warning("talk run %s failed: %s: %s", run_id, type(exc).__name__, exc)
        finish_run(run_id, "failed", f"{type(exc).__name__}: {exc}")


def finish_run(run_id: int, status: str, output: str) -> bool:
    """Mark a run terminal. Unknown ids and double-finishes are no-ops.

    THE terminal invariant (K3 design gate): terminal transitions are
    compare-and-set under ``_RUN_LOCK`` — FIRST WRITER WINS, and every
    later finish from any path (worker budget/timeout/cap, a second
    cancel) is a no-op. This is what makes finish-first cancel unforgeable
    without scattering cancelled() checks across the worker: a cancel that
    won can never have its status or output overwritten.

    Returns True when THIS call performed the transition — the cancel path
    needs to know whether it won the race against a worker finishing
    naturally (a lost race means "already finished", never a kill).

    A run that finishes with steers still queued (follow-up cap exhausted,
    worker exception/timeout, operator cancel) must not let them silently
    vanish: they move to ``meta["undelivered_steers"]`` and the output gains
    one sentence naming them. No-op for steer-less entries, so the
    zero-steer path is byte-identical; steers never reach the history tee.
    """

    if status not in TERMINAL_STATUSES:
        raise ValueError(f"not a terminal status: {status!r}")
    with _RUN_LOCK:
        run = _RUNS.get(run_id)
        if run is None or run["status"] in TERMINAL_STATUSES:
            return False
        pending = run.get("steers") or []
        if pending:
            run["steers"] = []
            # Verbatim steering text stays in ephemeral registry meta ONLY —
            # the output string is teed to durable history, and operator
            # course-corrections must never persist there. The count is the
            # durable fact; check_work/get read the text from meta.
            run["meta"]["undelivered_steers"] = list(pending)
            noun = "steer" if len(pending) == 1 else "steers"
            output = (
                f"{output} ({len(pending)} {noun} arrived too late to be "
                "delivered — manage_run get has the text)"
            )
        run["status"] = status
        run["output"] = output
        run["updated"] = time.time()
        tee = _terminal_tee_locked(run_id, run)
    _append_history(tee)
    return True


def _terminal_tee_locked(run_id: int, run: dict) -> dict:
    """The history record for a terminal transition. Caller holds the lock."""

    return {
        "runId": run_id,
        "kind": run["kind"],
        "label": run["label"],
        "status": run["status"],
        "output": str(run["output"] or "")[:_HISTORY_OUTPUT_CAP],
        "ts": run["ts"],
        "updated": run["updated"],
    }


#: Steering — operator course-corrections for background agent runs,
#: delivered at the worker's next turn boundary. Process-local by design:
#: the queue's lifetime is coherent with the worker thread that consumes it.
STEER_QUEUE_CAP = 10


def queue_steer(run_id: int, text: str) -> str:
    """Queue operator steering for a background agent run.

    Speakable contract: always returns text for the voice model, refusal or
    receipt. Agent-kind only — archon runs steer through their own gate
    machinery, skill/look runs have no conversation to redirect.
    """

    text = str(text or "").strip()
    if not text:
        return "There's nothing to send — say what the agent should do differently."
    with _RUN_LOCK:
        run = _RUNS.get(run_id)
        if run is None:
            return f"I don't have a run #{run_id}."
        if run["kind"] != "agent":
            return (
                f"Run #{run_id} is a {run['kind']} run — steering reaches "
                "background agents only."
            )
        if run["status"] in TERMINAL_STATUSES:
            return f"Run #{run_id} already finished — delegate a follow-up task instead."
        steers = run.setdefault("steers", [])
        if len(steers) >= STEER_QUEUE_CAP:
            return (
                f"Run #{run_id} already has {STEER_QUEUE_CAP} steers queued — "
                "let it catch up first."
            )
        steers.append(text)
        run["updated"] = time.time()
        pending = len(steers)
    return (
        f"Queued for run #{run_id} — it lands at the next turn boundary "
        f"({pending} pending)."
    )


def drain_steers_or_finish(run_id: int, output: str) -> list[str]:
    """Atomically hand pending steers to the worker OR finish the run done.

    ONE lock acquisition closes the steer-after-final-drain race: a steer
    queued while the worker was deciding either comes back from this call
    (the run stays running) or arrives after the run is terminal and gets
    the queue_steer refusal — it can never land in a window where it would
    silently vanish. Returns drained steers (run stays running) or ``[]``
    (the run is now done, or was already terminal/unknown — e.g. cancelled
    — in which case the worker just exits).
    """

    tee: dict | None = None
    with _RUN_LOCK:
        run = _RUNS.get(run_id)
        if run is None or run["status"] in TERMINAL_STATUSES:
            return []
        steers = run.get("steers") or []
        if steers:
            # Drain REPLACES the list — snapshots hold their own copies.
            run["steers"] = []
            run["updated"] = time.time()
            return list(steers)
        run["status"] = "done"
        run["output"] = output
        run["updated"] = time.time()
        tee = _terminal_tee_locked(run_id, run)
    if tee is not None:
        _append_history(tee)
    return []


def attach_pid(run_id: int, pid: int) -> bool:
    """Atomically attach a live subprocess pid to an ACTIVE run.

    Returns False when the run is terminal or unknown — the caller must
    kill the process it just spawned instead of running it. This closes the
    cancel-vs-spawn race: a cancel that lands between the worker's status
    check and the Popen would otherwise see no pid, report success, and let
    a brand-new process burn the remaining budget.
    """

    with _RUN_LOCK:
        run = _RUNS.get(run_id)
        if run is None or run["status"] in TERMINAL_STATUSES:
            return False
        run["meta"]["pid"] = pid
        run["updated"] = time.time()
        return True


def annotate_run(run_id: int, **fields: Any) -> None:
    """Merge worker-observed facts (archon run id, pid, phase) into the entry."""

    with _RUN_LOCK:
        run = _RUNS.get(run_id)
        if run is None:
            return
        run["meta"].update(fields)
        run["updated"] = time.time()


def get_run(run_id: int) -> dict | None:
    """Snapshot one run for the poll route; ``None`` for unknown ids."""

    with _RUN_LOCK:
        run = _RUNS.get(run_id)
        if run is None:
            return None
        snapshot = dict(run)
        snapshot["meta"] = dict(run["meta"])
        snapshot["steers"] = list(run.get("steers") or [])
        return snapshot


def list_runs(limit: int = 10, include_history: bool = False) -> list[dict]:
    """Most-recent runs first, newest ``limit`` entries.

    With ``include_history`` the live registry is merged over the persisted
    JSONL tail: live entries win, history-only entries carry
    ``fromHistory: True``, and a history entry still marked ``running`` with
    no live counterpart is reported as ``lost`` — it belonged to an API
    process that died (for ``archon`` kind the WATCHER died; the detached
    workflow itself is judged by ``archon.db``).
    """

    limit = max(1, min(int(limit), 100))
    with _RUN_LOCK:
        live: dict[int, dict] = {}
        for run_id, run in _RUNS.items():
            snapshot = dict(run)
            snapshot["meta"] = dict(run["meta"])
            snapshot["steers"] = list(run.get("steers") or [])
            snapshot["runId"] = run_id
            live[run_id] = snapshot

    if not include_history:
        run_ids = sorted(live.keys(), reverse=True)[:limit]
        return [live[rid] for rid in run_ids]

    merged: dict[int, dict] = {}
    for rid, rec in _load_history().items():
        status = str(rec.get("status") or "")
        entry = {
            "runId": rid,
            "kind": rec.get("kind"),
            "label": rec.get("label") or "",
            "status": status if status in TERMINAL_STATUSES else "lost",
            "output": str(rec.get("output") or ""),
            "meta": {},
            "ts": rec.get("ts"),
            "updated": rec.get("updated"),
            "fromHistory": True,
        }
        merged[rid] = entry
    merged.update(live)
    run_ids = sorted(merged.keys(), reverse=True)[:limit]
    return [merged[rid] for rid in run_ids]


def reset_for_tests() -> None:
    """Clear registry state between tests (never called in production)."""

    global _RUN_SEQ
    with _RUN_LOCK:
        _RUNS.clear()
        _RUN_SEQ = 0


__all__ = [
    "RUN_KINDS",
    "STEER_QUEUE_CAP",
    "TERMINAL_STATUSES",
    "annotate_run",
    "attach_pid",
    "drain_steers_or_finish",
    "finish_run",
    "get_run",
    "list_runs",
    "queue_steer",
    "reset_for_tests",
    "start_run",
    "started_sentinel",
]
