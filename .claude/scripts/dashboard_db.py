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

import re
import sqlite3
import time
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

CREATE TABLE IF NOT EXISTS pair_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bootstrap_hash TEXT NOT NULL UNIQUE,
    gateway_url TEXT NOT NULL,
    remote_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'issued',
    device_name TEXT NOT NULL DEFAULT '',
    device_platform TEXT NOT NULL DEFAULT '',
    poll_secret_hash TEXT NOT NULL DEFAULT '',
    released INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    expires_at INTEGER NOT NULL,
    claimed_at INTEGER,
    decided_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pair_status
    ON pair_requests(status, created_at DESC);

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


_SCHEMA_BUSY_TIMEOUT_MS = 5000
_SCHEMA_INIT_ATTEMPTS = 5
_SCHEMA_RETRY_DELAY_S = 0.01

_EXPECTED_CABINET_MEETING_COLUMNS = frozenset({"title", "chat_id", "broadcast_order"})



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




def _schema_statements() -> tuple[str, ...]:
    """Split the static schema script into single executable statements.

    ``Connection.executescript`` commits an active transaction before running
    its script. Schema initialization needs the opposite guarantee: every
    CREATE, physical-column inspection, ALTER, and dependent index must stay
    inside the same ``BEGIN IMMEDIATE`` transaction. ``complete_statement``
    lets us execute the existing schema text statement-by-statement without
    changing its public shape or relying on a fragile semicolon split.
    """
    statements: list[str] = []
    pending: list[str] = []
    for line in _SCHEMA_SQL.splitlines():
        pending.append(line)
        candidate = "\n".join(pending).strip()
        if candidate and sqlite3.complete_statement(candidate):
            statements.append(candidate)
            pending.clear()

    trailing = "\n".join(pending).strip()
    if trailing and any(line.strip() and not line.lstrip().startswith("--") for line in pending):
        raise RuntimeError("dashboard schema contains an incomplete SQL statement")
    return tuple(statements)


def _execute_schema(conn: sqlite3.Connection) -> None:
    """Execute all static DDL without escaping the caller's transaction."""
    for statement in _schema_statements():
        conn.execute(statement)


def _expected_schema_objects() -> tuple[set[str], set[str]]:
    """Derive required object names from the active private/public schema text."""
    tables: set[str] = set()
    indexes = {"idx_cabinet_meetings_chat_open"}
    pattern = re.compile(
        r"\bCREATE\s+(TABLE|INDEX)\s+IF\s+NOT\s+EXISTS\s+([A-Za-z0-9_]+)",
        re.IGNORECASE,
    )
    for statement in _schema_statements():
        match = pattern.search(statement)
        if match is None:
            continue
        target = tables if match.group(1).casefold() == "table" else indexes
        target.add(match.group(2))
    return tables, indexes




def _verify_physical_schema(conn: sqlite3.Connection) -> None:
    """Fail unless the required physical tables, indexes, and columns exist."""
    rows = conn.execute(
        "SELECT type, name FROM sqlite_master "
        "WHERE type IN ('table', 'index') AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    tables = {row[1] for row in rows if row[0] == "table"}
    indexes = {row[1] for row in rows if row[0] == "index"}
    columns = _column_names(conn, "cabinet_meetings")
    expected_tables, expected_indexes = _expected_schema_objects()

    missing_tables = sorted(expected_tables - tables)
    missing_indexes = sorted(expected_indexes - indexes)
    missing_columns = sorted(_EXPECTED_CABINET_MEETING_COLUMNS - columns)
    if missing_tables or missing_indexes or missing_columns:
        raise sqlite3.DatabaseError(
            "dashboard schema physical verification failed: "
            f"tables={missing_tables!r}, indexes={missing_indexes!r}, "
            f"cabinet_meetings_columns={missing_columns!r}"
        )



def _physical_schema_is_current(conn: sqlite3.Connection) -> bool:
    try:
        _verify_physical_schema(conn)
    except sqlite3.Error:
        return False
    return True




def _is_schema_contention(exc: sqlite3.OperationalError) -> bool:
    """Return whether *exc* is a retryable schema-initialization race."""
    message = str(exc).casefold()
    return "locked" in message or "busy" in message or "duplicate column name" in message


def _rollback_schema_attempt(
    conn: sqlite3.Connection,
    exc: sqlite3.Error,
) -> None:
    """Rollback a failed attempt without hiding rollback failures."""
    try:
        conn.rollback()
    except sqlite3.Error as rollback_exc:
        raise rollback_exc from exc


def _initialize_schema(conn: sqlite3.Connection) -> None:
    """Atomically initialize/migrate schema with bounded contention retry.

    ``BEGIN IMMEDIATE`` serializes all first-use writers before any physical
    schema inspection occurs. A waiter therefore starts only after the first
    initializer commits, then re-reads ``PRAGMA table_info`` in its own write
    transaction instead of acting on a stale missing-column decision.

    Lock or duplicate-column contention is never accepted on exception text
    alone. A retry reruns the physical inspection and the successful path
    always verifies the schema shape before returning. At the retry boundary,
    an already-complete physical schema is the only condition that can classify
    the contended initialization as successful.
    """
    # Preserve the prior ``executescript`` behavior for callers that pass a
    # connection with an active transaction: schema initialization begins only
    # after their pending transaction has been committed.
    conn.commit()

    last_contention: sqlite3.OperationalError | None = None
    for attempt in range(_SCHEMA_INIT_ATTEMPTS):
        try:
            conn.execute("BEGIN IMMEDIATE")
            _execute_schema(conn)
            _apply_phase_5a_columns(conn)
            _apply_phase_6_columns(conn)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cabinet_meetings_chat_open "
                "ON cabinet_meetings(chat_id, started_at DESC) "
                "WHERE ended_at IS NULL"
            )
            _verify_physical_schema(conn)
            conn.commit()
            return
        except sqlite3.DatabaseError as exc:
            _rollback_schema_attempt(conn, exc)
            if not isinstance(exc, sqlite3.OperationalError) or not _is_schema_contention(exc):
                raise
            last_contention = exc
            if attempt + 1 < _SCHEMA_INIT_ATTEMPTS:
                time.sleep(_SCHEMA_RETRY_DELAY_S * (attempt + 1))

    assert last_contention is not None
    try:
        _verify_physical_schema(conn)
    except sqlite3.Error:
        raise last_contention


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
        conn.execute("ALTER TABLE cabinet_meetings ADD COLUMN chat_id TEXT NOT NULL DEFAULT ''")


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
        conn.execute("ALTER TABLE cabinet_meetings ADD COLUMN broadcast_order TEXT")


def _apply_connection_pragmas(conn: sqlite3.Connection) -> None:
    """Set connection-local pragmas without acquiring a write reservation."""
    conn.execute(f"PRAGMA busy_timeout={_SCHEMA_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")


def _ensure_wal_mode(conn: sqlite3.Connection) -> None:
    """Transition a new/stale database to WAL with bounded contention retry.

    Mirrors ``OrchestrationDB`` (``orchestration/db.py:210-211``):
        - WAL journal mode (concurrent readers + single writer).

    ``PRAGMA journal_mode=WAL`` may need a write lock. Callers first prove the
    physical schema and persisted journal mode so an already-current WAL file
    never executes this transition on a read connection.
    """
    last_contention: sqlite3.OperationalError | None = None
    for attempt in range(_SCHEMA_INIT_ATTEMPTS):
        try:
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        except sqlite3.OperationalError as exc:
            if not _is_schema_contention(exc):
                raise
            last_contention = exc
            if attempt + 1 < _SCHEMA_INIT_ATTEMPTS:
                time.sleep(_SCHEMA_RETRY_DELAY_S * (attempt + 1))
                continue
            break
        if str(mode).casefold() != "wal":
            raise sqlite3.DatabaseError(f"dashboard database failed to enter WAL mode: {mode!r}")
        break
    else:  # pragma: no cover - loop exits by success or the final break
        raise AssertionError("unreachable WAL retry state")

    if last_contention is not None and attempt + 1 == _SCHEMA_INIT_ATTEMPTS:
        # A contended WAL transition is successful only when a physical re-read
        # proves another initializer completed the transition.
        try:
            current_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        except sqlite3.Error:
            raise last_contention
        if str(current_mode).casefold() != "wal":
            raise last_contention


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Use a read-only fast path; reserve writes only for absent/stale schema."""
    _apply_connection_pragmas(conn)
    schema_is_current = _physical_schema_is_current(conn)
    journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
    if schema_is_current and journal_mode == "wal":
        return
    if journal_mode != "wal":
        _ensure_wal_mode(conn)
    if not schema_is_current:
        _initialize_schema(conn)


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
            timeout=_SCHEMA_BUSY_TIMEOUT_MS / 1000,
        )
        conn.row_factory = sqlite3.Row
        try:
            _ensure_schema(conn)
        except BaseException:
            conn.close()
            raise
        self._conn = conn
        return conn

    def init_schema(self, conn: sqlite3.Connection | None = None) -> None:
        """Create all tables idempotently.

        Uses an explicit ``BEGIN IMMEDIATE`` write transaction so the entire
        DDL and additive migration run atomically — no partial-init half-state
        or stale missing-column decision is possible. CREATE IF NOT EXISTS
        makes re-invocation a no-op. Rule 2: the DDL inspects the actual SQLite
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
        _ensure_schema(conn)

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
