"""Archon live-telemetry ingest — read-only cursor-tail of the run ledger.

Epic #252 / ticket #254. Architecture: ``PRD-archon-execution-spine.architecture.md``
F6 **as amended 2026-07-27** — the spike falsified consuming Archon's own
``/api/stream/__dashboard__``: ``registerStream`` is single-slot and CLOSES any
existing stream on register (``transport.ts:70-77``), so an open Archon Console
and the Homie evict each other in an EventSource-reconnect war (measured three
times, each connection dead within ~3s). The framework therefore consumes NO
Archon SSE. The live-read path is this module: a ``mode=ro`` cursor-tail of
``remote_agent_workflow_events``.

Semantics are mirrored from Archon's own ``DashboardEventPoller``
(``packages/server/src/adapters/web/dashboard-event-poller.ts``):

  * ``created_at >= cursor`` (NOT ``>``) — SQLite's ``datetime('now')`` has
    1-second resolution, so a strict ``>`` skips rows that land late inside the
    boundary second.
  * a ``seen_ids`` boundary set suppresses re-emitting rows already sent at
    exactly that second.
  * the cursor starts at BOOT — history is never replayed onto the stream. The
    REST snapshot (:func:`read_recent_events`) is what backfills a page load.

with ONE deliberate divergence: Archon's poller filters to
``DASHBOARD_SOURCE_EVENT_TYPES``, which strips the high-frequency ``tool_*``
rows. We do NOT filter — the whole point of reading the DB instead of the SSE
is that the rows carry the full vocabulary uniformly for every run type
(``workflow_*``, ``node_*``, ``tool_called``/``tool_completed`` with
``tool_name`` + ``tool_input`` + ``duration_ms``, ``approval_*``,
``hook_activity``, ``task_activity``). Receipt: run
``23c6c29ad89b24d6e662af355bbd4158``.

Boundaries this module holds:

  * **Never writes archon.db.** Every connection is a ``file:...?mode=ro`` URI,
    so a write is refused inside SQLite itself (WAL databases included).
  * **Degrades, never raises.** Archon server down is irrelevant (the DB is the
    ledger). A missing / locked / garbage db, a missing table, or a malformed
    row all fold into an empty read plus a status string — callers surface
    ``status`` instead of a 500.
  * **Timestamps are naive-UTC** ``"YYYY-MM-DD HH:MM:SS"`` — archon.db's
    ``datetime('now')`` clock domain. Cursor comparisons are lexicographic on
    that fixed-width format, which is ordinally correct.
  * **``data`` is hostile input.** It carries LLM-authored ``tool_input`` and
    node output. Every value is redacted (``security.redact``) and capped, then
    the whole object is trimmed to a per-event budget.
  * **No sync DB work on an event loop.** :class:`ArchonEventPoller` runs the
    blocking read through ``asyncio.to_thread`` and resolves the db path INSIDE
    the thread (to_thread ARGUMENTS evaluate on the loop).

Knobs resolve through ``config.get_archon_events_settings()`` at call time
(Rule 1); nothing here is bound as a default arg.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)

EVENTS_TABLE: Final[str] = "remote_agent_workflow_events"
RUNS_TABLE: Final[str] = "remote_agent_workflow_runs"

# Columns read from each table (verified against the live archon.db DDL).
EVENT_COLUMNS: Final[tuple[str, ...]] = (
    "id",
    "workflow_run_id",
    "event_type",
    "step_index",
    "step_name",
    "data",
    "created_at",
)
RUN_COLUMNS: Final[tuple[str, ...]] = (
    "id",
    "conversation_id",
    "workflow_name",
    "status",
    "started_at",
    "completed_at",
    "last_activity_at",
    "working_path",
)

# Run statuses that mean "this run is over" — a per-run stream closes on these
# instead of holding a socket open on a finished run.
TERMINAL_RUN_STATUSES: Final[frozenset[str]] = frozenset({
    "completed",
    "failed",
    "cancelled",
    "canceled",
    "abandoned",
    "error",
})

# Event types that mean the same thing at the event level (a run can finish
# between two drains, so the stream watches the events too, not just the row).
TERMINAL_EVENT_TYPES: Final[frozenset[str]] = frozenset({
    "workflow_completed",
    "workflow_failed",
    "workflow_cancelled",
    "workflow_abandoned",
})

# Node-lifecycle event types -> the node status each one means. Measured against
# the live ledger 2026-07-28: 14,714 node rows (node_completed 5,623 ·
# node_started 5,497 · node_skipped 2,419 · node_skipped_prior_success 670 ·
# node_failed 505), EVERY one carrying a non-empty `step_name`.
#
# The node NAME only exists here. `remote_agent_workflow_runs.current_step_index`
# looks like the obvious pointer and is NULL on every live run (including
# in-flight ones) — Rule 2: the events are the physical state, the column is a
# promise the writer does not keep.
NODE_EVENT_STATUS: Final[dict[str, str]] = {
    "node_started": "running",
    "node_completed": "completed",
    "node_failed": "failed",
    "node_skipped": "skipped",
    "node_skipped_prior_success": "skipped",
}

# archon.db's datetime('now') format. Cursor math stays in this clock domain.
TIMESTAMP_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"

# Longest key we keep from a `data` blob; a hostile key is truncated, not trusted.
_MAX_DATA_KEY_CHARS: Final[int] = 64
# Per-value cap applied BEFORE the whole-object budget.
_MAX_DATA_VALUE_CHARS: Final[int] = 800

# Status strings surfaced to the operator via REST/SSE. `ok` means the read
# succeeded (even with zero rows); the others each name a distinct failure.
STATUS_OK: Final[str] = "ok"
STATUS_DB_MISSING: Final[str] = "db_missing"
STATUS_DB_UNREADABLE: Final[str] = "db_unreadable"


# ─────────────────────────────────────────────────────────────────────────────
# Read-only DB access
# ─────────────────────────────────────────────────────────────────────────────
def _resolve_db_path(db_path: Path | str | None) -> Path:
    """``None`` resolves ``settings.db_path`` at call time (Rule 1)."""
    if db_path is not None:
        return Path(db_path)
    import config  # noqa: PLC0415 — late import keeps this module import-cheap

    return config.get_archon_events_settings().db_path


def read_only_uri(db_path: Path) -> str:
    """``file:///...?mode=ro`` URI — write-refusing at the driver level.

    ``Path.as_uri()`` percent-encodes and emits the ``file:///C:/...`` form
    sqlite3 accepts on Windows; ``mode=ro`` makes any write attempt raise
    inside SQLite itself. Mirrors ``cofounder/engine_archon._read_only_uri``.
    """
    return db_path.absolute().as_uri() + "?mode=ro"


def _connect(db_path: Path, timeout_s: float) -> sqlite3.Connection:
    """Open the ledger read-only. Raises — callers own the fail-open boundary."""
    connection = sqlite3.connect(read_only_uri(db_path), uri=True, timeout=timeout_s)
    connection.row_factory = sqlite3.Row
    return connection


def _query(
    sql: str,
    params: tuple,
    *,
    db_path: Path | str | None,
    timeout_s: float | None,
) -> tuple[list[sqlite3.Row], str]:
    """Run one read-only query. Returns ``(rows, status)`` — NEVER raises.

    Rule 2: the ``db_missing`` status is decided by a physical ``exists()``
    check on the resolved path, not by a cached claim that Archon is installed.
    """
    import config  # noqa: PLC0415

    # Config resolution and the path check are INSIDE the fail-open boundary.
    # They were above it, so a malformed knob (ARCHON_EVENTS_SNAPSHOT_LIMIT=
    # not-an-int) raised ValueError straight out of a function whose contract
    # says it never raises — turning an honest "telemetry unavailable" into a
    # 500 on the endpoint that consumes it. A fat-fingered .env must degrade,
    # not break the page.
    try:
        settings = config.get_archon_events_settings()
        path = _resolve_db_path(db_path)
        if timeout_s is None:
            timeout_s = settings.connect_timeout_s
        if not path.exists():
            return ([], STATUS_DB_MISSING)
    except Exception as exc:  # noqa: BLE001 — same boundary as the read below
        logger.warning(
            "archon_events: unusable configuration: %s: %s", type(exc).__name__, exc
        )
        return ([], STATUS_DB_UNREADABLE)
    try:
        connection = _connect(path, timeout_s)
        try:
            rows = list(connection.execute(sql, params))
        finally:
            connection.close()
    except Exception as exc:  # noqa: BLE001 — fail-open boundary for ledger reads
        # Receipt for the swallow: a locked/garbage/table-less db must be
        # visible in the log, not just as an empty list at the API.
        logger.warning("archon_events: read failed at %s (%s)", path, exc)
        return ([], STATUS_DB_UNREADABLE)
    return (rows, STATUS_OK)


# ─────────────────────────────────────────────────────────────────────────────
# Normalization — `data` is hostile input
# ─────────────────────────────────────────────────────────────────────────────
def _scrub(text: str, limit: int) -> str:
    """Redact secrets then cap. Rule 3 — module-attribute lookup on redact."""
    from security import redact as _redact_mod  # noqa: PLC0415

    scrubbed = _redact_mod.redact_sensitive_text(text)
    if scrubbed is None:
        scrubbed = ""
    if len(scrubbed) > limit:
        return scrubbed[:limit] + "…[truncated]"
    return scrubbed


def _sanitize_value(value: Any) -> Any:
    """Coerce one ``data`` value to a JSON-safe, redacted, capped primitive.

    Nested structures (notably ``tool_input``) are flattened to a capped JSON
    STRING rather than kept as live objects: the consumer renders them, and a
    flat string cannot smuggle unbounded depth or size into the wire payload.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) or isinstance(value, float):
        return value
    if isinstance(value, str):
        return _scrub(value, _MAX_DATA_VALUE_CHARS)
    try:
        rendered = json.dumps(value, default=str)
    except Exception:  # noqa: BLE001 — an unserializable value degrades to repr
        rendered = str(value)
    return _scrub(rendered, _MAX_DATA_VALUE_CHARS)


def sanitize_event_data(raw: str | None, *, max_chars: int) -> dict[str, Any]:
    """Parse + harden one row's ``data`` blob into a bounded dict.

    Fail-open at every seam: a NULL blob, invalid JSON, or a non-object JSON
    value never raises — the first two yield ``{}``, the third is preserved
    under a ``value`` key so the information is not silently dropped.

    When the sanitized object still exceeds ``max_chars``, the largest values
    are replaced with ``"[truncated]"`` (largest first) and a ``_truncated``
    marker is set, so an operator can tell a trimmed event from a small one.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001 — a malformed blob is hostile input, not a crash
        return {"value": _scrub(str(raw), _MAX_DATA_VALUE_CHARS)}
    if not isinstance(parsed, dict):
        return {"value": _sanitize_value(parsed)}

    out: dict[str, Any] = {}
    for key, value in parsed.items():
        out[str(key)[:_MAX_DATA_KEY_CHARS]] = _sanitize_value(value)

    # Whole-object budget. Drop the biggest values first so the small,
    # high-signal fields (tool_name, duration_ms) survive the trim.
    def _size() -> int:
        try:
            return len(json.dumps(out, default=str))
        except Exception:  # noqa: BLE001
            return 0

    if _size() > max_chars:
        ranked = sorted(
            out.items(),
            key=lambda kv: len(str(kv[1])),
            reverse=True,
        )
        for key, _value in ranked:
            if _size() <= max_chars:
                break
            out[key] = "[truncated]"
        out["_truncated"] = True
    return out


def normalize_event_row(row: Any, *, max_data_chars: int) -> dict[str, Any]:
    """One ledger row -> the camelCase wire event the dashboard consumes.

    camelCase mirrors the cabinet SSE contract so the Preact consumer reads one
    shape. ``data`` keys stay in Archon's own snake_case — they are Archon's
    vocabulary, not ours, and renaming them would invent a contract.
    """
    mapping = dict(row)
    step_index = mapping.get("step_index")
    return {
        "id": str(mapping.get("id") or ""),
        "runId": str(mapping.get("workflow_run_id") or ""),
        "type": str(mapping.get("event_type") or ""),
        "stepIndex": step_index if isinstance(step_index, int) else None,
        "stepName": (
            _scrub(str(mapping["step_name"]), _MAX_DATA_KEY_CHARS)
            if mapping.get("step_name")
            else None
        ),
        "createdAt": str(mapping.get("created_at") or ""),
        "data": sanitize_event_data(mapping.get("data"), max_chars=max_data_chars),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Cursor state machine (mirrors Archon's DashboardEventPoller)
# ─────────────────────────────────────────────────────────────────────────────
def now_cursor() -> str:
    """Boot cursor in archon.db's naive-UTC clock domain."""
    return datetime.now(UTC).strftime(TIMESTAMP_FORMAT)


@dataclass
class DrainResult:
    """One drain pass: what to emit, and the cursor state to carry forward."""

    events: list[dict[str, Any]] = field(default_factory=list)
    cursor: str = ""
    last_rowid: int = 0
    seen_ids: frozenset[str] = frozenset()
    status: str = STATUS_OK


def drain_events_since(
    cursor: str,
    seen_ids: frozenset[str] | None = None,
    *,
    last_rowid: int = 0,
    limit: int | None = None,
    max_data_chars: int | None = None,
    db_path: Path | str | None = None,
    timeout_s: float | None = None,
) -> DrainResult:
    """Tail one batch of events at/after ``cursor``. NEVER raises.

    ``created_at >= cursor`` (Archon's inclusivity, load-bearing against
    SQLite's 1-second timestamp resolution) is paired with a **rowid
    watermark** rather than Archon's id-set alone.

    Why the watermark: Archon can rely on an id-set because its poller filters
    to dashboard event types, which keeps a one-second bucket well under the
    drain limit. We deliberately do NOT filter (the full ``tool_*`` vocabulary
    is the whole reason to read the DB), so a busy second routinely exceeds the
    limit — and an id-set alone DEADLOCKS there: the ``LIMIT`` window keeps
    returning the same already-seen head rows and the cursor never advances.
    ``rowid > last_rowid`` gives the query a strictly-advancing pagination key
    while preserving the boundary semantics, because a row that arrives late in
    the boundary second is inserted later and therefore has a HIGHER rowid.

    ``seen_ids`` is kept as a second, in-Python dedup layer over the boundary
    second (defense in depth, and the ticket's stated semantics).

    Known edge: SQLite reuses a freed rowid when the highest row is deleted
    without AUTOINCREMENT. Archon deletes events only by run-cascade, so this
    needs a run deleted AND a replacement event landing inside the same
    boundary second to skip a row. Accepted; the REST snapshot still shows it.

    On any read failure the cursor, watermark, and id-set are returned
    UNCHANGED, so a transient DB lock costs latency, never events.
    """
    import config  # noqa: PLC0415

    settings = config.get_archon_events_settings()
    if limit is None:
        limit = settings.drain_limit
    if max_data_chars is None:
        max_data_chars = settings.max_data_chars
    if seen_ids is None:
        seen_ids = frozenset()

    columns = ", ".join(EVENT_COLUMNS)
    sql = (
        f"SELECT rowid AS _rowid, {columns} FROM {EVENTS_TABLE} "
        "WHERE created_at >= ? AND rowid > ? "
        "ORDER BY created_at ASC, rowid ASC LIMIT ?"
    )
    rows, status = _query(
        sql, (cursor, last_rowid, limit), db_path=db_path, timeout_s=timeout_s
    )
    if status != STATUS_OK or not rows:
        return DrainResult(
            events=[],
            cursor=cursor,
            last_rowid=last_rowid,
            seen_ids=seen_ids,
            status=status,
        )

    max_ts = cursor
    max_rowid = last_rowid
    emitted: list[dict[str, Any]] = []
    for row in rows:
        # The watermark advances over EVERY returned row, including ones the
        # id-set skips — a fully-skipped page must still move the window.
        row_rowid = int(row["_rowid"])
        if row_rowid > max_rowid:
            max_rowid = row_rowid
        created_at = str(row["created_at"] or "")
        if created_at > max_ts:
            max_ts = created_at
        if str(row["id"]) in seen_ids:
            continue
        emitted.append(normalize_event_row(row, max_data_chars=max_data_chars))

    # Remember every id at exactly the new boundary second — including ones we
    # skipped this pass — so the next `>= cursor` query cannot re-emit them.
    next_seen = frozenset(
        str(row["id"]) for row in rows if str(row["created_at"] or "") == max_ts
    )
    return DrainResult(
        events=emitted,
        cursor=max_ts,
        last_rowid=max_rowid,
        seen_ids=next_seen,
        status=status,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot reads (REST + the SSE subscribe-time frame)
# ─────────────────────────────────────────────────────────────────────────────
def read_run_rows(
    *,
    run_id: str | None = None,
    conversation_id: str | None = None,
    parent_conversation_id: str | None = None,
    limit: int | None = None,
    db_path: Path | str | None = None,
    timeout_s: float | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Run-ledger rows for a run id or a correlation key. NEVER raises.

    ``conversation_id`` IS the correlation key from the architecture's data
    shape (the Homie's work item -> Archon's conversation id).

    ``parent_conversation_id`` is the DISPATCH join and a different column on
    purpose: for a web-dispatched workflow Archon spawns a separate WORKER
    conversation and puts THAT in the run's own ``conversation_id``, leaving the
    dispatching conversation in ``parent_conversation_id``
    (``talk_archon.run_id_for_conversation`` documents the same asymmetry).
    Filtering the natural-looking way matches nothing, and an empty result reads
    exactly like "not started yet" — so a ledger row a correlation key CAN reach
    is only reachable through this filter. Additive keyword: an unset value
    leaves the SQL byte-identical for existing callers.
    """
    import config  # noqa: PLC0415

    if limit is None:
        limit = config.get_archon_events_settings().snapshot_limit
    clauses: list[str] = []
    params: list[Any] = []
    if run_id:
        clauses.append("id = ?")
        params.append(str(run_id))
    if conversation_id:
        clauses.append("conversation_id = ?")
        params.append(str(conversation_id))
    if parent_conversation_id:
        clauses.append("parent_conversation_id = ?")
        params.append(str(parent_conversation_id))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        f"SELECT {', '.join(RUN_COLUMNS)} FROM {RUNS_TABLE}{where} "
        "ORDER BY started_at DESC LIMIT ?"
    )
    params.append(limit)
    rows, status = _query(sql, tuple(params), db_path=db_path, timeout_s=timeout_s)
    runs = [
        {
            "runId": str(row["id"] or ""),
            "conversationId": str(row["conversation_id"] or ""),
            "workflowName": _scrub(str(row["workflow_name"] or ""), _MAX_DATA_KEY_CHARS),
            "status": str(row["status"] or ""),
            "startedAt": str(row["started_at"] or "") or None,
            "completedAt": str(row["completed_at"] or "") or None,
            "lastActivityAt": str(row["last_activity_at"] or "") or None,
            "workingPath": _scrub(str(row["working_path"] or ""), _MAX_DATA_VALUE_CHARS)
            or None,
        }
        for row in rows
    ]
    return (runs, status)


def read_recent_events(
    *,
    run_id: str | None = None,
    conversation_id: str | None = None,
    limit: int | None = None,
    max_data_chars: int | None = None,
    db_path: Path | str | None = None,
    timeout_s: float | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Most recent events for a run / correlation key, oldest-first. NEVER raises.

    The query selects the NEWEST ``limit`` rows and reverses them, so a long
    run shows its tail (what is happening now) rather than its head.

    An unfiltered call (no run id, no conversation id) returns the newest rows
    across every run — the global "what is Archon doing" view.
    """
    import config  # noqa: PLC0415

    settings = config.get_archon_events_settings()
    if limit is None:
        limit = settings.snapshot_limit
    if max_data_chars is None:
        max_data_chars = settings.max_data_chars

    run_ids: list[str] = []
    if conversation_id:
        runs, run_status = read_run_rows(
            conversation_id=conversation_id,
            db_path=db_path,
            timeout_s=timeout_s,
        )
        if run_status != STATUS_OK:
            return ([], run_status)
        run_ids = [run["runId"] for run in runs if run["runId"]]
        if run_id:
            # Both filters given — intersect rather than widening the scope.
            run_ids = [rid for rid in run_ids if rid == str(run_id)]
        if not run_ids:
            return ([], STATUS_OK)
    elif run_id:
        run_ids = [str(run_id)]

    params: list[Any] = []
    where = ""
    if run_ids:
        placeholders = ",".join("?" for _ in run_ids)
        where = f" WHERE workflow_run_id IN ({placeholders})"
        params.extend(run_ids)
    sql = (
        f"SELECT {', '.join(EVENT_COLUMNS)} FROM {EVENTS_TABLE}{where} "
        "ORDER BY created_at DESC, rowid DESC LIMIT ?"
    )
    params.append(limit)
    rows, status = _query(sql, tuple(params), db_path=db_path, timeout_s=timeout_s)
    events = [
        normalize_event_row(row, max_data_chars=max_data_chars)
        for row in reversed(rows)
    ]
    return (events, status)


def read_gate_data_raw(
    run_id: str,
    *,
    event_type: str = "approval_requested",
    db_path: Path | str | None = None,
    timeout_s: float | None = None,
) -> tuple[dict[str, Any], str]:
    """The newest gate event's ``data`` blob, UNTRUNCATED. NEVER raises.

    :func:`read_recent_events` is a DISPLAY reader: ``_sanitize_value`` caps
    every string at :data:`_MAX_DATA_VALUE_CHARS` (800) so a 28,000-character
    tool dump cannot wreck the sidebar or the SSE wire. That cap is correct
    there and fatal here. An approval gate's message carries the verbatim
    phrase the downstream check node demands (``APPROVE SPEND`` /
    ``APPROVE DEPLOY``) and the preview URL, and the workflow substitutes run
    config and preflight output AHEAD of both. The live ledger already holds
    approval messages of 2,009, 2,156, and 28,605 characters, so reading the
    phrase through the display reader silently returned an empty phrase and a
    bare ``approve`` — which every deterministic check node then failed.

    So: same read-only ledger, same never-raises contract, no value cap. The
    result is CONTROL-PLANE ONLY. It is LLM-authored, operator-adjacent text —
    treat it as hostile, and never hand it to the wire payload, which is what
    the capped reader is for.
    """
    sql = (
        f"SELECT {', '.join(EVENT_COLUMNS)} FROM {EVENTS_TABLE} "
        "WHERE workflow_run_id = ? AND event_type = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1"
    )
    rows, status = _query(
        sql, (str(run_id), str(event_type)), db_path=db_path, timeout_s=timeout_s
    )
    if status != STATUS_OK or not rows:
        return ({}, status)
    raw = dict(rows[0]).get("data")
    if not raw:
        return ({}, status)
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:  # noqa: BLE001 — a malformed blob is absence, not an error
        return ({}, status)
    return ((parsed if isinstance(parsed, dict) else {}), status)


def read_current_node(
    run_id: str,
    *,
    db_path: Path | str | None = None,
    timeout_s: float | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """The node a run is on right now, from its newest node event. NEVER raises.

    Returns ``(node, status)``; ``node`` is ``None`` when the run has no node
    events yet (dispatched but not started), when the run id is unknown, or when
    the ledger could not be read — the three cases are told apart by ``status``
    plus the caller's own run-row read, never by an invented placeholder node.

    Ordering is ``created_at DESC, rowid DESC``. The rowid tiebreak is
    load-bearing, not cosmetic: archon.db timestamps have 1-second resolution
    and a node handoff lands BOTH ``node_completed <prev>`` and
    ``node_started <next>`` inside the same second (measured on run
    ``2c6810717e185807a369f009ee7c0414``). Ordering by timestamp alone picks
    whichever row SQLite returns first, so the row would flap between the
    finished node and the running one on consecutive polls.

    ``step_name`` is authored workflow YAML, not model output, but it is scrubbed
    on the same terms as every other ledger string — this module treats
    everything crossing the boundary as hostile.
    """
    if not run_id:
        return (None, STATUS_OK)
    placeholders = ",".join("?" for _ in NODE_EVENT_STATUS)
    sql = (
        "SELECT event_type, step_name, created_at "
        f"FROM {EVENTS_TABLE} "
        f"WHERE workflow_run_id = ? AND event_type IN ({placeholders}) "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1"
    )
    params = (str(run_id), *NODE_EVENT_STATUS)
    rows, status = _query(sql, params, db_path=db_path, timeout_s=timeout_s)
    if status != STATUS_OK or not rows:
        return (None, status)
    row = rows[0]
    event_type = str(row["event_type"] or "")
    name = str(row["step_name"] or "").strip()
    if not name:
        # Every live node row carries a step_name; a nameless one is a row we
        # cannot render honestly, so it is not reported as the current node.
        return (None, status)
    return (
        {
            "currentNode": _scrub(name, _MAX_DATA_KEY_CHARS),
            "nodeStatus": NODE_EVENT_STATUS.get(event_type, "unknown"),
            "eventType": event_type,
            "at": str(row["created_at"] or "") or None,
        },
        status,
    )


def run_is_terminal(
    run_id: str,
    *,
    db_path: Path | str | None = None,
    timeout_s: float | None = None,
) -> bool:
    """True iff the ledger says this run is finished (Rule 2 — the row decides).

    An unreadable ledger or an unknown run is NOT terminal: a stream should
    stay open on uncertainty rather than lie that the work is done.
    """
    if not run_id:
        return False
    runs, status = read_run_rows(run_id=run_id, db_path=db_path, timeout_s=timeout_s)
    if status != STATUS_OK or not runs:
        return False
    return str(runs[0].get("status") or "").strip().lower() in TERMINAL_RUN_STATUSES


def event_matches(
    event: dict[str, Any],
    *,
    run_id: str | None,
    run_ids: frozenset[str] | None = None,
) -> bool:
    """Subscriber-side filter. One global tail, per-subscriber scoping.

    ``run_ids`` carries the correlation-key expansion (conversation -> its
    runs); ``None`` means unscoped. An EMPTY frozenset is a real answer
    ("that conversation has no runs") and matches nothing — distinct from
    ``None``, so a mis-typed correlation key cannot silently open the firehose.
    """
    if run_ids is not None and event.get("runId") not in run_ids:
        return False
    if run_id and event.get("runId") != run_id:
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Fan-out channel — one global tail, many scoped subscribers
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class ChannelEntry:
    """One buffered event with its monotonic SSE id."""

    seq: int
    ts: int  # ms epoch (wall clock, for idle bookkeeping only)
    event: dict[str, Any]


class ArchonEventChannel:
    """In-memory ring buffer + subscriber fan-out for the ingested tail.

    Same-process invariant (identical to ``cabinet/meeting_channel.py``): the
    producer (:class:`ArchonEventPoller`) and the subscribers (the SSE handler
    in ``dashboard_api.py``) BOTH live in the orchestration API process. A
    module-local singleton bridges them; no other process imports this.

    ``seq`` is globally monotonic across ALL runs so ``Last-Event-ID`` resume
    works for a subscriber that later widens or narrows its filter.
    """

    def __init__(self, max_buffer: int = 1000):
        self._seq = 0
        self._buffer: deque[ChannelEntry] = deque(maxlen=max_buffer)
        self.max_buffer = max_buffer
        self.last_activity_at = int(time.time() * 1000)
        # queue -> dropped-since-last-consume marker (Kimi R1 MAJOR 2): an
        # overflow must become a visible gap signal on the stream, never a
        # silent hole the client can't know to refetch around.
        self._subscribers: dict[asyncio.Queue[ChannelEntry], bool] = {}

    def emit(self, event: dict[str, Any]) -> int:
        """Buffer + fan out one event; returns its assigned seq."""
        self._seq += 1
        entry = ChannelEntry(seq=self._seq, ts=int(time.time() * 1000), event=event)
        self._buffer.append(entry)
        self.last_activity_at = entry.ts
        for queue in self._subscribers:
            try:
                queue.put_nowait(entry)
            except asyncio.QueueFull:
                # A slow subscriber drops the live frame. The ring + REST
                # snapshot still hold the event, but the client cannot know
                # to refetch unless told — mark the drop so the stream emits
                # an `events_dropped` gap frame on its next iteration.
                self._subscribers[queue] = True
        return self._seq

    def consume_dropped(self, queue: asyncio.Queue[ChannelEntry]) -> bool:
        """Read AND clear the overflow marker — True once per drop episode."""
        if self._subscribers.get(queue):
            self._subscribers[queue] = False
            return True
        return False

    def since(self, since_seq: int) -> list[ChannelEntry]:
        """Buffered entries strictly newer than ``since_seq``."""
        return [entry for entry in self._buffer if entry.seq > since_seq]

    def oldest_seq(self) -> int:
        return self._buffer[0].seq if self._buffer else 0

    def latest_seq(self) -> int:
        return self._seq

    def subscribe(self, queue_size: int = 200) -> tuple[asyncio.Queue[ChannelEntry], Any]:
        """Returns ``(queue, unsubscribe)``. The unsubscribe is idempotent."""
        self.last_activity_at = int(time.time() * 1000)
        queue: asyncio.Queue[ChannelEntry] = asyncio.Queue(maxsize=queue_size)
        self._subscribers[queue] = False

        def unsub() -> None:
            self._subscribers.pop(queue, None)

        return queue, unsub

    def listener_count(self) -> int:
        return len(self._subscribers)

    def close(self) -> None:
        self._subscribers.clear()
        self._buffer.clear()


# Module-local singleton — the same Rule 2 exception cabinet's channel registry
# documents: this is a registry of LIVE in-memory objects, not a cache of
# resolved config state.
_CHANNEL: ArchonEventChannel | None = None


def get_channel() -> ArchonEventChannel:
    """Lazy-create the process-wide channel with the configured buffer depth."""
    global _CHANNEL
    if _CHANNEL is None:
        import config  # noqa: PLC0415

        _CHANNEL = ArchonEventChannel(
            max_buffer=config.get_archon_events_settings().buffer_size
        )
    return _CHANNEL


def _reset_channel() -> None:
    """@internal for tests — drop the singleton and any live subscribers."""
    global _CHANNEL
    if _CHANNEL is not None:
        _CHANNEL.close()
    _CHANNEL = None


# ─────────────────────────────────────────────────────────────────────────────
# The poller — off the bot loop, off the request path
# ─────────────────────────────────────────────────────────────────────────────
# Consecutive failed drains before the log escalates warn -> error (parity with
# Archon's own poller: a sustained outage should be alertable, a blip should not).
FAILURE_ESCALATION_THRESHOLD: Final[int] = 5


class ArchonEventPoller:
    """Async cursor-tail driving :class:`ArchonEventChannel`.

    Lives in the orchestration API process — NEVER the bot's event loop (the
    architecture's absolute event-loop rule). The blocking SQLite read runs in
    ``asyncio.to_thread``; the db path is resolved INSIDE the thread because
    to_thread ARGUMENTS evaluate on the loop.

    Idle behaviour matches Archon's poller: with zero subscribers the query is
    skipped entirely and the cursor is kept fresh, so an unopened dashboard
    costs one no-op tick per interval and never replays an idle gap. The
    subscribe-time REST snapshot is what covers that gap.
    """

    def __init__(self) -> None:
        self.cursor: str = now_cursor()
        self.last_rowid: int = 0
        self.seen_ids: frozenset[str] = frozenset()
        self.status: str = STATUS_OK
        self.last_drain_at: str | None = None
        self.last_error: str | None = None
        self.consecutive_failures: int = 0
        self.drain_count: int = 0
        self._task: asyncio.Task | None = None
        self._draining: bool = False

    # -- lifecycle -------------------------------------------------------
    def is_running(self) -> bool:
        """Rule 2 — physical task state, not a bool we set and hope stays true."""
        return self._task is not None and not self._task.done()

    def start(self, interval_s: float | None = None) -> bool:
        """Idempotent start. Returns False when there is no running loop.

        A False return is not an error: the module is importable (and fully
        testable) outside an event loop; the REST snapshot path never needs the
        poller at all.
        """
        if self.is_running():
            return True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        if interval_s is None:
            import config  # noqa: PLC0415

            interval_s = config.get_archon_events_settings().poll_interval_s
        # Reset the cursor on (re)start — never replay history onto the stream.
        # The watermark resets with it: `created_at >= now` already excludes
        # everything older, so a zero floor cannot resurrect history.
        self.cursor = now_cursor()
        self.last_rowid = 0
        self.seen_ids = frozenset()
        self._task = loop.create_task(self._loop(interval_s))
        logger.info("archon_events: poller started (interval=%.2fs)", interval_s)
        return True

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        self._task = None

    async def _loop(self, interval_s: float) -> None:
        while True:
            try:
                await asyncio.sleep(interval_s)
                await self.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the tail never dies on one bad pass
                logger.warning("archon_events: poll tick failed (%s)", exc)

    # -- drain -----------------------------------------------------------
    async def drain_once(self) -> int:
        """One tail pass. Returns the number of events emitted. NEVER raises."""
        if self._draining:
            # Coalesce overlapping drains rather than double-reading the cursor.
            return 0
        channel = get_channel()
        if channel.listener_count() == 0:
            # Cheap when idle: skip the query, keep the cursor fresh so a
            # later subscriber streams only new events.
            self.cursor = now_cursor()
            self.last_rowid = 0
            self.seen_ids = frozenset()
            return 0

        self._draining = True
        try:
            result = await asyncio.to_thread(
                self._drain_blocking, self.cursor, self.seen_ids, self.last_rowid
            )
        except Exception as exc:  # noqa: BLE001 — thread failure degrades, never raises
            self.consecutive_failures += 1
            self.last_error = str(exc)
            self.status = STATUS_DB_UNREADABLE
            self._log_failure(exc)
            return 0
        finally:
            self._draining = False

        self.status = result.status
        self.last_drain_at = now_cursor()
        self.drain_count += 1
        if result.status != STATUS_OK:
            self.consecutive_failures += 1
            self.last_error = result.status
            self._log_failure(result.status)
            return 0

        self.consecutive_failures = 0
        self.last_error = None
        self.cursor = result.cursor
        self.last_rowid = result.last_rowid
        self.seen_ids = result.seen_ids
        for event in result.events:
            channel.emit(event)
        return len(result.events)

    @staticmethod
    def _drain_blocking(
        cursor: str, seen_ids: frozenset[str], last_rowid: int
    ) -> DrainResult:
        """Runs in a worker thread — resolves its own settings there.

        to_thread ARGUMENTS evaluate on the loop, so nothing that touches the
        filesystem or env is passed in; only the cursor state crosses.
        """
        return drain_events_since(cursor, seen_ids, last_rowid=last_rowid)

    def _log_failure(self, detail: Any) -> None:
        if self.consecutive_failures >= FAILURE_ESCALATION_THRESHOLD:
            logger.error(
                "archon_events: drain failing persistently (%d consecutive): %s",
                self.consecutive_failures,
                detail,
            )
        else:
            logger.warning("archon_events: drain failed (%s)", detail)

    # -- operator surface -------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """Poller state for the REST/SSE ``poller`` field."""
        return {
            "running": self.is_running(),
            "status": self.status,
            "cursor": self.cursor,
            "lastRowid": self.last_rowid,
            "lastDrainAt": self.last_drain_at,
            "lastError": self.last_error,
            "consecutiveFailures": self.consecutive_failures,
            "drainCount": self.drain_count,
        }


_POLLER: ArchonEventPoller | None = None


def get_poller() -> ArchonEventPoller:
    """Lazy-create the process-wide poller."""
    global _POLLER
    if _POLLER is None:
        _POLLER = ArchonEventPoller()
    return _POLLER


def ensure_poller_started() -> bool:
    """Start the tail if a loop is available. Idempotent; never raises.

    Called from the REST + SSE handlers rather than a startup hook: the
    orchestration app has no lifespan seam, and a lazy start means a process
    that never serves an Archon route never runs the tail.
    """
    try:
        return get_poller().start()
    except Exception as exc:  # noqa: BLE001 — a failed start degrades to REST-only
        logger.warning("archon_events: poller start failed (%s)", exc)
        return False


def _reset_poller() -> None:
    """@internal for tests — stop and drop the singleton."""
    global _POLLER
    if _POLLER is not None:
        _POLLER.stop()
    _POLLER = None


__all__ = [
    "ArchonEventChannel",
    "ArchonEventPoller",
    "ChannelEntry",
    "DrainResult",
    "EVENT_COLUMNS",
    "EVENTS_TABLE",
    "FAILURE_ESCALATION_THRESHOLD",
    "RUNS_TABLE",
    "RUN_COLUMNS",
    "STATUS_DB_MISSING",
    "STATUS_DB_UNREADABLE",
    "STATUS_OK",
    "TERMINAL_EVENT_TYPES",
    "TERMINAL_RUN_STATUSES",
    "TIMESTAMP_FORMAT",
    "drain_events_since",
    "ensure_poller_started",
    "event_matches",
    "get_channel",
    "get_poller",
    "normalize_event_row",
    "now_cursor",
    "read_gate_data_raw",
    "read_only_uri",
    "read_recent_events",
    "read_run_rows",
    "run_is_terminal",
    "sanitize_event_data",
]
