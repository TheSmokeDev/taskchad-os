"""SQLite persistence for the dashboard slice (PRD-8 Phase 3 / WS1).

Owns ``dashboard.db`` — schema and connection helper for the operator-facing
dashboard surface that replaces the retired mission-control Next.js app.

Slice ownership: this module is the ONLY Python entry point for opening
``dashboard.db`` connections. Phase 3 endpoint handlers in
``dashboard_api.py`` call ``get_connection()`` (or instantiate ``DashboardDB``)
on every request — there is NO module-level connection cache (Rule 2) and NO
``def`` -time bind to ``config.DASHBOARD_DB_PATH`` (Rule 1).

Schema (forward-only-additive — Phase 5/7 future tables ship NOW per Q3 lock):
    1. scheduled_tasks       — Phase 3 CRUD (data plane only; runner deferred)
    2. agent_file_history    — Phase 3 file-PATCH version history
    3. dashboard_settings    — Phase 3 key/value (sidebar/theme)
    4. cabinet_meetings      — Phase 5 (empty in Phase 3)
    5. cabinet_transcripts   — Phase 5 (empty in Phase 3)
    6. audit_log             — Phase 3 hard-delete writes; Phase 7 expands writers

Pragmas applied on every connection (matches OrchestrationDB pattern at
``.claude/scripts/orchestration/db.py``):
    - PRAGMA journal_mode=WAL          — concurrent readers + single writer
    - PRAGMA busy_timeout=5000         — 5s wait for SQLite locks
    - PRAGMA foreign_keys=ON           — cabinet_transcripts FK to cabinet_meetings

Anti-pattern rules (R4 NB3 + Phase 2 codification):
    - Rule 1: ``db_path=None`` sentinel resolved inside the function body to
      ``config.DASHBOARD_DB_PATH``. NEVER ``def __init__(self, db_path=config.X)``.
    - Rule 2: no module-level cache of the resolved path or the connection.
      Every call resolves fresh and opens a fresh connection.

WS1 → WS2 contract (locked at PRP §1565-1580):
    class DashboardDB:
        def __init__(
            self,
            db_path: Path | None = None,
            *,
            check_same_thread: bool = False,
        ) -> None: ...
        def connect(self) -> sqlite3.Connection: ...

    def get_connection(
        db_path: Path | None = None,
        *,
        check_same_thread: bool = False,
    ) -> sqlite3.Connection: ...
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

__all__ = ["DashboardDB", "get_connection"]


# ── Schema DDL ─────────────────────────────────────────────────────────────
# All tables use ``CREATE TABLE IF NOT EXISTS`` so init_schema() is idempotent
# on fresh DB and on every subsequent connection. Forward-only-additive Q3
# lock — Phase 5/7 future tables ship now as empty CREATEs; later phases
# insert rows but do NOT migrate the schema.

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_id TEXT NOT NULL DEFAULT 'default',
    prompt TEXT NOT NULL,
    schedule TEXT NOT NULL,
    next_run INTEGER,
    last_run INTEGER,
    last_result TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'completed', 'failed')),
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_scheduled_persona
    ON scheduled_tasks(persona_id, status);
CREATE INDEX IF NOT EXISTS idx_scheduled_next_run
    ON scheduled_tasks(next_run)
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS agent_file_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    content TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT 'dashboard',
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_agent_file_history_persona_filename
    ON agent_file_history(persona_id, filename, created_at DESC);

CREATE TABLE IF NOT EXISTS dashboard_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);

CREATE TABLE IF NOT EXISTS cabinet_meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    ended_at INTEGER,
    duration_s INTEGER,
    mode TEXT,
    pinned_persona TEXT,
    entry_count INTEGER NOT NULL DEFAULT 0,
    title TEXT,
    chat_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_cabinet_meetings_started
    ON cabinet_meetings(started_at DESC);
-- idx_cabinet_meetings_chat_open is created AFTER `_apply_phase_5a_columns`
-- runs in `init_schema()` so older DBs that pre-date the `chat_id` column
-- don't crash during initial migration. CREATE INDEX must follow
-- column-add ordering (Phase 5a backwards-compat path).

CREATE TABLE IF NOT EXISTS cabinet_transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES cabinet_meetings(id) ON DELETE CASCADE,
    speaker TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_cabinet_transcripts_meeting
    ON cabinet_transcripts(meeting_id, created_at);
CREATE INDEX IF NOT EXISTS idx_cabinet_transcripts_meeting_id_desc
    ON cabinet_transcripts(meeting_id, id DESC);

-- PRD-8 Phase 5a / WS3 — additive Q3 forward-only.
-- cabinet_text_meetings: per-meeting roster snapshot (port of
--   ClaudeClaw warroom_text_meetings; Phase 5a uses cabinet_meetings
--   for primary state, this table records the immutable roster + pin
--   AS-OF meeting creation for replay determinism).
CREATE TABLE IF NOT EXISTS cabinet_text_meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL UNIQUE REFERENCES cabinet_meetings(id) ON DELETE CASCADE,
    roster_json TEXT NOT NULL DEFAULT '[]',
    pinned_agent TEXT,
    started_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    ended_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_cabinet_text_meetings_meeting
    ON cabinet_text_meetings(meeting_id);

-- cabinet_client_msg_seen: dedup LRU for client_msg_id.
CREATE TABLE IF NOT EXISTS cabinet_client_msg_seen (
    meeting_id INTEGER NOT NULL,
    client_msg_id TEXT NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    PRIMARY KEY (meeting_id, client_msg_id)
);
CREATE INDEX IF NOT EXISTS idx_cabinet_client_msg_seen_age
    ON cabinet_client_msg_seen(created_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_id TEXT NOT NULL DEFAULT 'default',
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    blocked INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    operator_id TEXT NOT NULL DEFAULT 'system',
    target_persona_id TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT 'unknown'
);
CREATE INDEX IF NOT EXISTS idx_audit_time
    ON audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_persona
    ON audit_log(persona_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action
    ON audit_log(action, created_at DESC);
"""


def _resolve_db_path(db_path: Path | None) -> Path:
    """Resolve the dashboard.db path.

    Rule 1 enforcement: caller passes ``None`` (the canonical sentinel) and
    this helper resolves to ``config.DASHBOARD_DB_PATH`` at CALL TIME. The
    ``import config`` happens inside the function body so a test can
    monkeypatch ``config.DASHBOARD_DB_PATH`` and the next call sees the
    patched value.
    """
    if db_path is not None:
        return Path(db_path)
    # Late-bind the import. Rule 2 — do NOT cache the resolved value at
    # module scope; resolve on every call so HOMIE_HOME / DASHBOARD_DB_PATH
    # env-overrides applied mid-process take effect immediately.
    import config as _config  # noqa: PLC0415 — late-bind by design (Rule 1/2)
    return Path(_config.DASHBOARD_DB_PATH)


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the set of column names for *table* (empty if table missing)."""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {row[1] for row in rows}


def _apply_phase_5a_columns(conn: sqlite3.Connection) -> None:
    """Forward-only-additive Phase 5a column additions on cabinet_meetings.

    Pre-Phase-5a deployments shipped `cabinet_meetings` without `title` or
    `chat_id` (see Phase 3 schema). ALTER TABLE ADD COLUMN is the only way
    to add them on a live DB. Each ADD is guarded by a PRAGMA inspection
    so re-invocations are no-ops.

    Rule 2 — physical-state-first: PRAGMA inspects sqlite_master directly
    rather than trusting a meta/version row.
    """
    cols = _column_names(conn, "cabinet_meetings")
    if "title" not in cols:
        conn.execute("ALTER TABLE cabinet_meetings ADD COLUMN title TEXT")
    if "chat_id" not in cols:
        conn.execute(
            "ALTER TABLE cabinet_meetings ADD COLUMN chat_id TEXT NOT NULL DEFAULT ''"
        )


def _apply_phase_6_columns(conn: sqlite3.Connection) -> None:
    """Forward-only-additive Phase 6 column additions on cabinet_meetings.

    Phase 6 (cabinet voice) snapshots the voice-broadcast persona order at
    meeting create time so the voice subprocess can iterate broadcast turns
    in stable order even if the live persona registry changes mid-meeting.
    Stored as JSON-encoded list[str] in the new ``broadcast_order`` column.

    Pre-Phase-6 deployments shipped `cabinet_meetings` without
    ``broadcast_order``. ALTER TABLE ADD COLUMN with PRAGMA guard makes
    this re-runnable on a live DB.

    Rule 2 — physical-state-first: PRAGMA inspects sqlite_master directly
    rather than trusting a meta/version row.
    """
    cols = _column_names(conn, "cabinet_meetings")
    if "broadcast_order" not in cols:
        conn.execute(
            "ALTER TABLE cabinet_meetings ADD COLUMN broadcast_order TEXT"
        )


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """Set the canonical pragmas on a fresh connection.

    Mirrors ``OrchestrationDB`` (``orchestration/db.py:210-211``):
        - WAL journal mode (concurrent readers + single writer)
        - busy_timeout=5000ms (matches the criterion locked in the JSON contract)
        - foreign_keys=ON (cabinet_transcripts FK to cabinet_meetings)

    journal_mode persists in the DB header once set — re-asserting it on
    every connection is safe (the SQLite docs explicitly call this out).
    busy_timeout is per-connection and MUST be set on every connect.
    """
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")


class DashboardDB:
    """Thin SQLite wrapper for dashboard.db persistence.

    Connection model: one ``DashboardDB`` per request. ``connect()`` opens a
    fresh connection (FastAPI threadpool compatibility). The class does NOT
    cache the connection — callers close via the ``connect()`` return value
    or via ``close()``.

    Construction is cheap (no I/O — just stashes the path). The first call
    to ``connect()`` (or ``init_schema()``) is what opens the file and runs
    the schema DDL.
    """

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        check_same_thread: bool = False,
    ) -> None:
        # Rule 1: db_path=None sentinel; the actual default is resolved at
        # call time via _resolve_db_path so config overrides land. Rule 2:
        # we stash the resolved Path on the instance, but every NEW instance
        # re-resolves — there is no module-level cache.
        self.db_path: Path = _resolve_db_path(db_path)
        self._check_same_thread: bool = check_same_thread
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> sqlite3.Connection:
        """Open a fresh connection with pragmas + schema applied.

        Returns the connection. Stores it on ``self._conn`` so a later
        ``close()`` call works, but each call to ``connect()`` opens a NEW
        connection — no caching. FastAPI handlers should call this once per
        request and close at the end (or use a try/finally / context-manager
        wrapper).
        """
        # Make sure the parent directory exists. dashboard.db lives under
        # .claude/data/ which is created elsewhere via config.ensure_directories,
        # but we don't want to require that to have run before the first
        # connection on a fresh checkout — sqlite3.connect will fail if the
        # parent directory is missing.
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=self._check_same_thread,
        )
        conn.row_factory = sqlite3.Row
        _apply_pragmas(conn)
        self.init_schema(conn)
        self._conn = conn
        return conn

    def init_schema(self, conn: sqlite3.Connection | None = None) -> None:
        """Create all tables idempotently.

        Uses ``executescript`` so the entire DDL runs in a single transaction
        — no partial-init half-state is possible. CREATE IF NOT EXISTS makes
        re-invocation a no-op. Rule 2: the DDL inspects the actual SQLite
        backend (via CREATE IF NOT EXISTS), not a sidecar 'schema_version'
        flag, so meta lies cannot make us skip a table that physically went
        missing.

        Phase 5a additive migration — `cabinet_meetings.title` and
        `cabinet_meetings.chat_id` columns are added via ALTER TABLE if
        missing on a pre-Phase-5a database (Q3 forward-only-additive).
        Idempotent — re-runs are no-ops.
        """
        if conn is None:
            conn = self.connect()
            return  # connect() already calls init_schema(conn) on the fresh conn
        conn.executescript(_SCHEMA_SQL)
        _apply_phase_5a_columns(conn)
        # PRD-8 Phase 6 — additive `broadcast_order` column on cabinet_meetings.
        _apply_phase_6_columns(conn)
        # Indexes that depend on backwards-compat-added columns must run
        # AFTER the column-ensure step (older DBs would crash with
        # "no such column" otherwise — same class as orchestration/db.py
        # msg_type ordering fix).
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cabinet_meetings_chat_open "
            "ON cabinet_meetings(chat_id, started_at DESC) "
            "WHERE ended_at IS NULL"
        )
        conn.commit()

    def close(self) -> None:
        """Close the most-recently-opened connection if one is held."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def get_connection(
    db_path: Path | None = None,
    *,
    check_same_thread: bool = False,
) -> sqlite3.Connection:
    """Return a fresh sqlite3.Connection with pragmas + schema applied.

    Convenience helper for Phase 3 endpoint handlers in ``dashboard_api.py``
    that don't need the ``DashboardDB`` wrapper. Functionally equivalent to
    ``DashboardDB(db_path, check_same_thread=...).connect()``.

    Rule 1: db_path=None default sentinel — resolved INSIDE the function
    body via ``_resolve_db_path``. Tests that monkeypatch
    ``config.DASHBOARD_DB_PATH`` see the patched value on the next call.
    """
    db = DashboardDB(db_path, check_same_thread=check_same_thread)
    return db.connect()
