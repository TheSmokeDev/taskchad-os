"""Session stores for persistent chat conversations (SQLite + Postgres)."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from session_keys import build_session_key

# === PRD-7 §7.10 / Phase 4 (PRP-7d) — source tagging ===
# Single source of truth for session-source values + default-hidden set.
# Click `Choice`, `session list` filter, tests, and any future writer all
# import from here.
SOURCE_VALUES: tuple[str, ...] = ("interactive", "tool", "cron", "hook")
SOURCE_HIDDEN_BY_DEFAULT: tuple[str, ...] = ("tool", "hook")


def normalize_source(value: str | None) -> str:
    """Coerce arbitrary value to a known SOURCE_VALUES entry.

    Fail-OPEN: unknown or None values become "interactive". Non-Click writers
    (relay, internal callers) cannot bypass the enum, but a relay frame with a
    typo also won't crash the engine.
    """

    if not isinstance(value, str):
        return "interactive"
    v = value.strip()
    if v in SOURCE_VALUES:
        return v
    return "interactive"


def _assert_source_column_shape(
    cursor: Any,
    *,
    backend: Literal["sqlite", "postgres"],
) -> None:
    """Assert chat_sessions.source matches PRD §7.10 + §14.23 physical shape.

    SQLite PRAGMA row layout: name=r[1], type=r[2], notnull=r[3], dflt_value=r[4].
        Required: type.upper() == "TEXT", notnull == 1,
                  str(dflt_value) contains 'interactive'.
    Postgres information_schema row layout: data_type=r[0], is_nullable=r[1],
                  column_default=r[2]. Required: data_type == 'text',
                  is_nullable == 'NO', column_default contains 'interactive'.
        MUST filter by table_schema = current_schema() (multi-schema search_path
        drift defense).
    Both backends: SELECT COUNT(*) WHERE source IS NULL OR source = '' must be 0.
    On any mismatch, raise RuntimeError with manual-repair guidance. Do NOT
    auto-update existing data — operator repairs, then re-runs.
    """

    repair = (
        " — manual repair required (see PRD §7.10 + §14.23); then re-run migration"
    )
    if backend == "sqlite":
        rows = cursor.execute("PRAGMA table_info(chat_sessions)").fetchall()
        cols = {r[1]: r for r in rows}
        if "source" not in cols:
            raise RuntimeError("source column missing" + repair)
        r = cols["source"]
        col_type = (r[2] or "")
        if col_type.upper() != "TEXT":
            raise RuntimeError(
                f"source type expected 'TEXT', got {col_type!r}" + repair
            )
        if r[3] != 1:
            raise RuntimeError("source is not NOT NULL" + repair)
        if "interactive" not in str(r[4] or ""):
            raise RuntimeError(
                f"source default missing 'interactive', got {r[4]!r}" + repair
            )
        bad = cursor.execute(
            "SELECT COUNT(*) FROM chat_sessions WHERE source IS NULL OR source = ''"
        ).fetchone()[0]
        if bad:
            raise RuntimeError(
                f"chat_sessions has {bad} NULL/empty source rows" + repair
            )
    elif backend == "postgres":
        cursor.execute(
            "SELECT data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_name = 'chat_sessions' "
            "  AND column_name = 'source' "
            "  AND table_schema = current_schema()"
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("source column missing in current_schema()" + repair)
        if row[0] != "text":
            raise RuntimeError(
                f"source data_type expected 'text', got {row[0]!r}" + repair
            )
        if row[1] != "NO":
            raise RuntimeError("source is_nullable expected 'NO'" + repair)
        if "interactive" not in (row[2] or ""):
            raise RuntimeError(
                f"source column_default missing 'interactive', got {row[2]!r}" + repair
            )
        cursor.execute(
            "SELECT COUNT(*) FROM chat_sessions WHERE source IS NULL OR source = ''"
        )
        bad = cursor.fetchone()[0]
        if bad:
            raise RuntimeError(
                f"chat_sessions has {bad} NULL/empty source rows" + repair
            )
    else:
        raise ValueError(f"unsupported backend: {backend!r}")


def _is_duplicate_source_column_error(exc: sqlite3.OperationalError) -> bool:
    """Classify an ``OperationalError`` as a benign duplicate-column race.

    SQLite versions / forks emit several different wordings for the same
    "another connection won the ALTER" race condition:

      * ``"duplicate column name: source"``        (mainline)
      * ``"DUPLICATE COLUMN: source"``             (case-insensitive)
      * ``"column already exists: source"``        (alternate phrasing)
      * ``"column 'source' already exists"``       (variant phrasing)

    All four indicate the same benign race: the loser process saw the column
    was added by the winner between our PRE-CHECK and our ALTER. The
    unconditional post-check ``_assert_source_column_shape`` then validates
    the final shape regardless of which path took us there — so the catch
    here only needs to filter out genuinely-benign race wordings.

    Anything else (``"database is locked"``, ``"syntax error"``,
    ``"no such table"``, ``"unable to open database file"``) MUST propagate —
    those are real failures, NOT races.
    """

    msg = str(exc).lower()
    if "duplicate column" in msg:
        return True
    if "already exists" in msg and ("source" in msg or "column" in msg):
        return True
    return False


def _run_source_migration(
    cursor: Any,
    *,
    _alter_executor: Callable[[str], None] | None = None,
) -> None:
    """Run the SQLite chat_sessions.source ALTER with race tolerance.

    Pure-Python seam (R3 NNM2): production callers pass `_alter_executor=None`,
    which resolves to `cursor.execute`. Tests inject a callable that raises
    `sqlite3.OperationalError("duplicate column name: source")` to simulate the
    concurrent-first-boot race without monkeypatching `sqlite3.Connection.execute`
    (which became an immutable type attribute in Python 3.14).

    Race semantics:
        - PRE-CHECK reads PRAGMA table_info(chat_sessions).
        - If `source` is missing, run the ALTER. Catch ONLY OperationalErrors
          whose message matches ``_is_duplicate_source_column_error`` — which
          covers ``"duplicate column"`` plus the ``"column already exists"``
          variants emitted by some SQLite forks for the same benign race.
          Any other sqlite3.Error MUST propagate (locked DB, malformed
          statement, wrong table, permission denied).
        - The shape post-check (`_assert_source_column_shape`) is the caller's
          responsibility — this helper is the ALTER seam only.
    """

    alter = _alter_executor if _alter_executor is not None else cursor.execute
    cols = {r[1]: r for r in cursor.execute("PRAGMA table_info(chat_sessions)").fetchall()}
    if "source" in cols:
        return
    try:
        alter(
            "ALTER TABLE chat_sessions ADD COLUMN source TEXT NOT NULL DEFAULT 'interactive'"
        )
    except sqlite3.OperationalError as exc:
        if not _is_duplicate_source_column_error(exc):
            raise
        # Race accepted: another process won the ALTER between our pre-check
        # and our ALTER call. Caller's post-check will validate the shape.
        return


@dataclass
class Session:
    """Represents a chat session tied to a platform thread."""

    session_id: str  # Composite: {platform}:{channel_id}:{thread_id}
    agent_session_id: str  # Back-compat alias for runtime_session_id
    platform: str
    channel_id: str
    thread_id: str
    user_id: str
    created_at: datetime
    updated_at: datetime
    message_count: int = 0
    total_cost_usd: float = 0.0
    tool_call_count: int = 0
    status: str = "active"
    mode: str = "execute"  # "plan" or "execute"
    runtime_lane: str = "claude_native"
    runtime_provider: str = "claude"
    runtime_model: str = ""
    runtime_profile_key: str = ""
    runtime_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    source: str = "interactive"  # PRD-7 §7.10 / Phase 4 (PRP-7d)

    @property
    def runtime_session_id(self) -> str:
        """Runtime-neutral alias for the persisted session identifier."""

        return self.agent_session_id

    @runtime_session_id.setter
    def runtime_session_id(self, value: str) -> None:
        self.agent_session_id = value


@dataclass
class SessionSummary:
    """Lightweight row summary for `thehomie session list` (PRP-7d R1 B3).

    Separates ergonomics (`internal_id` for table display) from PRD contract
    (`session_id` is the stable composite/runtime identifier `show`/`resume`
    accept). `runtime_session_id` is the exact value `session resume` re-execs
    with — kept distinct from `session_id` so a future split between the
    composite key and the runtime UUID is non-breaking.
    """

    internal_id: int
    session_id: str
    platform: str
    source: str
    message_count: int
    updated_at: datetime
    runtime_session_id: str


@dataclass
class HeartbeatThread:
    """Tracks a heartbeat notification posted to Slack so thread replies can start conversations."""

    channel_id: str
    thread_ts: str  # The Slack message ts — becomes the thread_ts for replies
    alert_text: str
    created_at: datetime


@dataclass
class ChatMessage:
    """Represents one persisted chat message within a session."""

    id: int | None
    session_id: str
    role: str
    content: str
    created_at: datetime
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


def _serialize_tool_calls(tool_calls: Any) -> str:
    """Serialize normalized tool call records for storage."""

    if not tool_calls:
        return "[]"
    try:
        return json.dumps(tool_calls)
    except TypeError:
        return "[]"


def _parse_tool_calls(raw: Any) -> list[dict[str, Any]]:
    """Parse stored tool call JSON from the database."""

    if not raw:
        return []
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
    return []


def _quote_fts_query(query: str) -> str:
    """Quote each term for a basic FTS5 AND search."""

    terms = query.strip().split()
    if not terms:
        return ""
    return " AND ".join(f'"{term}"' for term in terms)


class SQLiteSessionStore:
    """Persistent session storage backed by SQLite."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _open(self) -> sqlite3.Connection:
        """Raw connection with no schema check.

        Used by ``_init_db`` (which would otherwise recurse through
        ``_connect``) and by ``_connect`` after the schema is ensured.
        """
        return sqlite3.connect(self.db_path, check_same_thread=False)

    def _connect(self) -> sqlite3.Connection:
        """Open a per-op connection, re-initialising the schema if the DB file
        was wiped mid-run.

        The store opens a fresh connection per operation and ``_init_db`` only
        runs at construction. If the gitignored DB file is deleted/truncated
        while the bot is running (e.g. ``git clean -x``), SQLite silently
        recreates it empty on the next connect and every query then fails with
        ``no such table``. Re-initialise when the file is missing or
        zero-length before handing back the connection. Cheap on the happy
        path (one ``stat``); idempotent (``CREATE TABLE IF NOT EXISTS``).
        """
        try:
            wiped = (not self.db_path.exists()) or self.db_path.stat().st_size == 0
        except OSError:
            wiped = True
        if wiped:
            self._init_db()
        return self._open()

    def _init_db(self) -> None:
        """Create the chat/session tables if they don't exist."""
        with self._open() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL UNIQUE,
                    agent_session_id TEXT NOT NULL,
                    runtime_session_id TEXT DEFAULT '',
                    runtime_provider TEXT DEFAULT 'claude',
                    runtime_model TEXT DEFAULT '',
                    runtime_profile_key TEXT DEFAULT '',
                    platform TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    message_count INTEGER DEFAULT 0,
                    total_cost_usd REAL DEFAULT 0.0,
                    status TEXT DEFAULT 'active',
                    mode TEXT DEFAULT 'execute',
                    runtime_lane TEXT DEFAULT 'claude_native',
                    tool_call_count INTEGER DEFAULT 0,
                    runtime_tool_calls_json TEXT DEFAULT '[]'
                );
                CREATE INDEX IF NOT EXISTS idx_platform_thread
                    ON chat_sessions(platform, channel_id, thread_id);
                CREATE TABLE IF NOT EXISTS heartbeat_threads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id TEXT NOT NULL,
                    thread_ts TEXT NOT NULL,
                    alert_text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_hb_channel_thread
                    ON heartbeat_threads(channel_id, thread_ts);
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    tool_calls_json TEXT DEFAULT '[]'
                );
                CREATE INDEX IF NOT EXISTS idx_chat_messages_session_created
                    ON chat_messages(session_id, created_at);
                CREATE VIRTUAL TABLE IF NOT EXISTS chat_messages_fts USING fts5(
                    content,
                    role UNINDEXED,
                    session_id UNINDEXED,
                    content='chat_messages',
                    content_rowid='id'
                );
                CREATE TRIGGER IF NOT EXISTS chat_messages_ai
                    AFTER INSERT ON chat_messages BEGIN
                    INSERT INTO chat_messages_fts(rowid, content, role, session_id)
                    VALUES (new.id, new.content, new.role, new.session_id);
                END;
                CREATE TRIGGER IF NOT EXISTS chat_messages_ad
                    AFTER DELETE ON chat_messages BEGIN
                    INSERT INTO chat_messages_fts(chat_messages_fts, rowid, content, role, session_id)
                    VALUES ('delete', old.id, old.content, old.role, old.session_id);
                END;
                CREATE TRIGGER IF NOT EXISTS chat_messages_au
                    AFTER UPDATE ON chat_messages BEGIN
                    INSERT INTO chat_messages_fts(chat_messages_fts, rowid, content, role, session_id)
                    VALUES ('delete', old.id, old.content, old.role, old.session_id);
                    INSERT INTO chat_messages_fts(rowid, content, role, session_id)
                    VALUES (new.id, new.content, new.role, new.session_id);
                END;
            """)
            # Migration: add mode column to existing databases
            try:
                conn.execute("ALTER TABLE chat_sessions ADD COLUMN mode TEXT DEFAULT 'execute'")
            except sqlite3.OperationalError:
                pass  # Column already exists
            for statement in (
                "ALTER TABLE chat_sessions ADD COLUMN runtime_session_id TEXT DEFAULT ''",
                "ALTER TABLE chat_sessions ADD COLUMN runtime_provider TEXT DEFAULT 'claude'",
                "ALTER TABLE chat_sessions ADD COLUMN runtime_model TEXT DEFAULT ''",
                "ALTER TABLE chat_sessions ADD COLUMN runtime_profile_key TEXT DEFAULT ''",
                "ALTER TABLE chat_sessions ADD COLUMN runtime_lane TEXT DEFAULT 'claude_native'",
                "ALTER TABLE chat_sessions ADD COLUMN tool_call_count INTEGER DEFAULT 0",
                "ALTER TABLE chat_sessions ADD COLUMN runtime_tool_calls_json TEXT DEFAULT '[]'",
                "ALTER TABLE chat_messages ADD COLUMN tool_calls_json TEXT DEFAULT '[]'",
            ):
                try:
                    conn.execute(statement)
                except sqlite3.OperationalError:
                    pass
            conn.execute(
                """
                UPDATE chat_sessions
                SET runtime_session_id = agent_session_id
                WHERE COALESCE(runtime_session_id, '') = ''
                """
            )
            try:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_user_id ON chat_sessions(user_id)")
            except sqlite3.OperationalError:
                pass

            # === PRD-7 §7.10 / Phase 4 (PRP-7d) — chat_sessions.source migration ===
            # Dedicated, physical-state-checked block. Do NOT append to the broad
            # OperationalError-swallow loop above — that pattern would mask
            # wrong-table / locked-DB / malformed-statement errors as
            # "column already exists" (Rule 2 + R1 B1 + R2 NB2 + R3 NNB1).
            #
            # Shape:
            #   1. PRE-CHECK + race-tolerant ALTER via _run_source_migration
            #      (pure-Python seam — production passes _alter_executor=None).
            #   2. Unconditional shared post-check via _assert_source_column_shape
            #      (runs whether the column was missing, just-added, or hit the
            #      duplicate-column race). Any shape mismatch raises with a
            #      manual-repair message — never auto-update existing data.
            #   3. Composite index on (source, updated_at DESC) to back
            #      `thehomie session list` (PRP-7d R2 NM1).
            _run_source_migration(conn)
            _assert_source_column_shape(conn, backend="sqlite")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_sessions_source_updated "
                "ON chat_sessions(source, updated_at DESC)"
            )

    def _row_to_session(self, row: sqlite3.Row) -> Session:
        """Convert a database row to a Session object."""
        runtime_session_id = (
            row["runtime_session_id"]
            if "runtime_session_id" in row.keys() and row["runtime_session_id"]
            else row["agent_session_id"]
        )
        return Session(
            session_id=row["session_id"],
            agent_session_id=runtime_session_id,
            platform=row["platform"],
            channel_id=row["channel_id"],
            thread_id=row["thread_id"],
            user_id=row["user_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            message_count=row["message_count"],
            total_cost_usd=row["total_cost_usd"],
            tool_call_count=(
                row["tool_call_count"]
                if "tool_call_count" in row.keys()
                else 0
            ),
            status=row["status"],
            mode=row["mode"] if "mode" in row.keys() else "execute",
            runtime_lane=(row["runtime_lane"] if "runtime_lane" in row.keys() and row["runtime_lane"] else "claude_native"),
            runtime_provider=(
                row["runtime_provider"]
                if "runtime_provider" in row.keys() and row["runtime_provider"]
                else "claude"
            ),
            runtime_model=(
                row["runtime_model"]
                if "runtime_model" in row.keys() and row["runtime_model"]
                else ""
            ),
            runtime_profile_key=(
                row["runtime_profile_key"]
                if "runtime_profile_key" in row.keys() and row["runtime_profile_key"]
                else ""
            ),
            runtime_tool_calls=(
                _parse_tool_calls(row["runtime_tool_calls_json"])
                if "runtime_tool_calls_json" in row.keys()
                else []
            ),
            source=(
                normalize_source(row["source"])
                if "source" in row.keys()
                else "interactive"
            ),
        )

    def _row_to_chat_message(self, row: sqlite3.Row) -> ChatMessage:
        """Convert a database row to a ChatMessage."""

        return ChatMessage(
            id=row["id"] if "id" in row.keys() else None,
            session_id=row["session_id"],
            role=row["role"],
            content=row["content"],
            created_at=datetime.fromisoformat(row["created_at"]),
            tool_calls=(
                _parse_tool_calls(row["tool_calls_json"])
                if "tool_calls_json" in row.keys()
                else []
            ),
        )

    def get(self, platform: str, channel_id: str, thread_id: str) -> Session | None:
        """Look up a session by platform, channel, and thread."""
        session_id = build_session_key(platform, channel_id, thread_id)
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM chat_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_session(row)

    def create(self, session: Session) -> None:
        """Insert a new session.

        Writes ``normalize_source(session.source)`` so non-Click writers cannot
        bypass the four-value enum (PRP-7d R1 M4). Set-once invariant: ``update``
        deliberately does NOT touch ``source``.
        """
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO chat_sessions
                   (session_id, agent_session_id, runtime_session_id, runtime_lane, runtime_provider,
                    runtime_model, runtime_profile_key, platform, channel_id, thread_id, user_id,
                    created_at, updated_at, message_count, total_cost_usd,
                    status, mode, tool_call_count, runtime_tool_calls_json, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session.session_id,
                    session.agent_session_id,
                    session.runtime_session_id,
                    session.runtime_lane,
                    session.runtime_provider,
                    session.runtime_model,
                    session.runtime_profile_key,
                    session.platform,
                    session.channel_id,
                    session.thread_id,
                    session.user_id,
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                    session.message_count,
                    session.total_cost_usd,
                    session.status,
                    session.mode,
                    session.tool_call_count,
                    _serialize_tool_calls(session.runtime_tool_calls),
                    normalize_source(session.source),
                ),
            )

    def update(self, session: Session) -> None:
        """Update an existing session's mutable fields."""
        with self._connect() as conn:
            conn.execute(
                """UPDATE chat_sessions
                   SET agent_session_id = ?, runtime_session_id = ?, runtime_lane = ?, runtime_provider = ?,
                       runtime_model = ?, runtime_profile_key = ?, updated_at = ?, message_count = ?,
                       total_cost_usd = ?, tool_call_count = ?, status = ?, mode = ?, runtime_tool_calls_json = ?
                   WHERE session_id = ?""",
                (
                    session.agent_session_id,
                    session.runtime_session_id,
                    session.runtime_lane,
                    session.runtime_provider,
                    session.runtime_model,
                    session.runtime_profile_key,
                    datetime.now().isoformat(),
                    session.message_count,
                    session.total_cost_usd,
                    session.tool_call_count,
                    session.status,
                    session.mode,
                    _serialize_tool_calls(session.runtime_tool_calls),
                    session.session_id,
                ),
            )

    def delete(self, platform: str, channel_id: str, thread_id: str) -> bool:
        """Delete a session by platform, channel, and thread. Returns True if a row was deleted."""
        session_id = build_session_key(platform, channel_id, thread_id)
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM chat_messages WHERE session_id = ?",
                (session_id,),
            )
            cursor = conn.execute(
                "DELETE FROM chat_sessions WHERE session_id = ?",
                (session_id,),
            )
            return cursor.rowcount > 0

    def list_active(
        self,
        platform: str | None = None,
        source: str | None = None,
        sources: list[str] | None = None,
    ) -> list[Session]:
        """List active sessions, optionally filtered by platform and/or source.

        ``source`` is exact-match; ``sources`` is an ``IN (...)`` filter. Both
        are bound via ``?`` placeholders — never f-string interpolated. Existing
        callers that pass only ``platform`` (or nothing) keep working unchanged.
        """
        where_clauses: list[str] = ["status = 'active'"]
        params: list[Any] = []
        if platform:
            where_clauses.append("platform = ?")
            params.append(platform)
        if source:
            where_clauses.append("source = ?")
            params.append(source)
        elif sources:
            placeholders = ",".join("?" * len(sources))
            where_clauses.append(f"source IN ({placeholders})")
            params.extend(sources)
        sql = (
            "SELECT * FROM chat_sessions WHERE "
            + " AND ".join(where_clauses)
            + " ORDER BY updated_at DESC"
        )
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [self._row_to_session(row) for row in rows]

    def list_recent(
        self,
        *,
        platform: str | None = None,
        source: str | None = None,
        sources: list[str] | None = None,
        hidden: tuple[str, ...] | None = None,
        limit: int = 20,
        all_sources: bool = False,
    ) -> list[SessionSummary]:
        """List recent sessions for ``thehomie session list`` (PRP-7d R2 NM4).

        Always returns ``list[SessionSummary]`` — the lightweight DTO carrying
        ``internal_id`` (table display) plus ``session_id`` (PRD §7.10
        identifier) plus ``runtime_session_id`` (resume target). Operators that
        need the full ``Session`` call ``get_by_session_id`` instead.

        ``hidden`` uses the **None sentinel** pattern (Rule 1 / R1 B5): the
        default is resolved from ``SOURCE_HIDDEN_BY_DEFAULT`` inside the body so
        monkeypatching the module-level constant in tests is observed at
        runtime.

        When ``all_sources=True`` the ``hidden`` set is ignored (operator
        explicitly asked for everything). Filter precedence: ``source`` (exact)
        > ``sources`` (IN) > ``hidden`` exclusion. All values bind via ``?``;
        f-strings are used only to assemble placeholder slots.
        """

        if hidden is None:
            hidden = SOURCE_HIDDEN_BY_DEFAULT
        where_clauses: list[str] = []
        params: list[Any] = []
        if platform:
            where_clauses.append("platform = ?")
            params.append(platform)
        if source:
            where_clauses.append("source = ?")
            params.append(source)
        elif sources:
            placeholders = ",".join("?" * len(sources))
            where_clauses.append(f"source IN ({placeholders})")
            params.extend(sources)
        elif not all_sources and hidden:
            placeholders = ",".join("?" * len(hidden))
            where_clauses.append(f"source NOT IN ({placeholders})")
            params.extend(hidden)
        where_sql = (
            (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        )
        sql = (
            "SELECT id, session_id, platform, source, message_count, "
            "updated_at, runtime_session_id, agent_session_id "
            "FROM chat_sessions"
            + where_sql
            + " ORDER BY updated_at DESC LIMIT ?"
        )
        params.append(limit)
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, tuple(params)).fetchall()
            summaries: list[SessionSummary] = []
            for row in rows:
                runtime_id = (
                    row["runtime_session_id"]
                    if "runtime_session_id" in row.keys() and row["runtime_session_id"]
                    else (row["agent_session_id"] or row["session_id"])
                )
                summaries.append(
                    SessionSummary(
                        internal_id=row["id"],
                        session_id=row["session_id"],
                        platform=row["platform"],
                        source=normalize_source(row["source"]),
                        message_count=row["message_count"],
                        updated_at=datetime.fromisoformat(row["updated_at"]),
                        runtime_session_id=runtime_id,
                    )
                )
            return summaries

    def get_by_session_id(self, session_id: str) -> Session | None:
        """Look up a session by composite session_id OR runtime_session_id.

        PRD §7.10 contract (PRP-7d R1 B3): the argument is the stable string
        identifier (composite ``platform:channel:thread`` OR runtime UUID), NOT
        the SQLite autoincrement primary key. Tries ``session_id`` first, then
        falls back to ``runtime_session_id`` so operators can paste either form
        from quiet-JSON output.
        """

        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM chat_sessions "
                "WHERE session_id = ? OR runtime_session_id = ? "
                "LIMIT 1",
                (session_id, session_id),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_session(row)

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        created_at: datetime | None = None,
        tool_calls: Any = None,
    ) -> None:
        """Persist one chat message for transcript replay/search."""

        timestamp = (created_at or datetime.now()).isoformat()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO chat_messages (session_id, role, content, created_at, tool_calls_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, role, content, timestamp, _serialize_tool_calls(tool_calls)),
            )

    def list_messages(self, session_id: str, limit: int = 200) -> list[ChatMessage]:
        """List chat messages for a session in chronological order."""

        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT id, session_id, role, content, created_at, tool_calls_json
                   FROM chat_messages
                   WHERE session_id = ?
                   ORDER BY created_at ASC, id ASC
                   LIMIT ?""",
                (session_id, limit),
            ).fetchall()
            return [self._row_to_chat_message(row) for row in rows]

    def search_messages(
        self,
        query: str,
        limit: int = 20,
        session_id: str | None = None,
    ) -> list[ChatMessage]:
        """Search persisted chat messages with FTS5."""

        fts_query = _quote_fts_query(query)
        if not fts_query:
            return []

        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            if session_id:
                rows = conn.execute(
                    """
                    SELECT m.id, m.session_id, m.role, m.content, m.created_at, m.tool_calls_json
                    FROM chat_messages_fts
                    JOIN chat_messages m ON m.id = chat_messages_fts.rowid
                    WHERE chat_messages_fts MATCH ? AND m.session_id = ?
                    ORDER BY m.created_at DESC, m.id DESC
                    LIMIT ?
                    """,
                    (fts_query, session_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT m.id, m.session_id, m.role, m.content, m.created_at, m.tool_calls_json
                    FROM chat_messages_fts
                    JOIN chat_messages m ON m.id = chat_messages_fts.rowid
                    WHERE chat_messages_fts MATCH ?
                    ORDER BY m.created_at DESC, m.id DESC
                    LIMIT ?
                    """,
                    (fts_query, limit),
                ).fetchall()
            return [self._row_to_chat_message(row) for row in rows]

    def get_by_user(self, user_id: str) -> list[Session]:
        """Look up all sessions for a user across all platforms."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM chat_sessions WHERE user_id = ? ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()
            return [self._row_to_session(row) for row in rows]

    def save_heartbeat_thread(self, thread: HeartbeatThread) -> None:
        """Record a heartbeat notification so thread replies can be linked."""
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO heartbeat_threads
                   (channel_id, thread_ts, alert_text, created_at)
                   VALUES (?, ?, ?, ?)""",
                (
                    thread.channel_id,
                    thread.thread_ts,
                    thread.alert_text,
                    thread.created_at.isoformat(),
                ),
            )

    def get_heartbeat_thread(self, channel_id: str, thread_ts: str) -> HeartbeatThread | None:
        """Look up a heartbeat thread by channel and ts."""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM heartbeat_threads WHERE channel_id = ? AND thread_ts = ?",
                (channel_id, thread_ts),
            ).fetchone()
            if row is None:
                return None
            return HeartbeatThread(
                channel_id=row["channel_id"],
                thread_ts=row["thread_ts"],
                alert_text=row["alert_text"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )


class PostgresSessionStore:
    """Persistent session storage backed by PostgreSQL."""

    def __init__(self, database_url: str) -> None:
        import psycopg

        self._url = database_url
        self._conn = psycopg.connect(database_url, autocommit=True)
        self._init_db()

    def _init_db(self) -> None:
        """Create the chat/session tables if they don't exist."""
        cur = self._conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id SERIAL PRIMARY KEY,
                session_id TEXT NOT NULL UNIQUE,
                agent_session_id TEXT NOT NULL,
                runtime_session_id TEXT DEFAULT '',
                runtime_provider TEXT DEFAULT 'claude',
                runtime_model TEXT DEFAULT '',
                runtime_profile_key TEXT DEFAULT '',
                platform TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                message_count INTEGER DEFAULT 0,
                total_cost_usd DOUBLE PRECISION DEFAULT 0.0,
                status TEXT DEFAULT 'active',
                mode TEXT DEFAULT 'execute',
                runtime_lane TEXT DEFAULT 'claude_native',
                tool_call_count INTEGER DEFAULT 0,
                runtime_tool_calls_json TEXT DEFAULT '[]'
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_platform_thread
                ON chat_sessions(platform, channel_id, thread_id)
        """)
        # Migration: add mode column to existing databases
        try:
            cur.execute("ALTER TABLE chat_sessions ADD COLUMN mode TEXT DEFAULT 'execute'")
        except Exception:
            pass  # Column already exists
        for statement in (
            "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS runtime_session_id TEXT DEFAULT ''",
            (
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS "
                "runtime_provider TEXT DEFAULT 'claude'"
            ),
            (
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS "
                "runtime_model TEXT DEFAULT ''"
            ),
            (
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS "
                "runtime_profile_key TEXT DEFAULT ''"
            ),
            (
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS "
                "runtime_lane TEXT DEFAULT 'claude_native'"
            ),
            (
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS "
                "tool_call_count INTEGER DEFAULT 0"
            ),
            (
                "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS "
                "runtime_tool_calls_json TEXT DEFAULT '[]'"
            ),
        ):
            cur.execute(statement)
        cur.execute(
            """
            UPDATE chat_sessions
            SET runtime_session_id = agent_session_id
            WHERE COALESCE(runtime_session_id, '') = ''
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_id ON chat_sessions(user_id)")

        # === PRD-7 §7.10 / Phase 4 (PRP-7d) — chat_sessions.source migration ===
        # Postgres mirror of the SQLite block. Do NOT extend the for-loop above
        # (that loop uses ADD COLUMN IF NOT EXISTS which silently masks a
        # malformed pre-existing column). Strict pattern below per Rule 2 +
        # R1 B1 + R2 NB2 + R2 NB3 + R3 NNB1.
        #
        # Shape (mirrors SQLite):
        #   1. PRE-CHECK information_schema.columns scoped to current_schema()
        #      so a same-named column in another schema cannot silently match.
        #   2. ALTER wrapped to catch ONLY psycopg.errors.DuplicateColumn (race
        #      window between the pre-check and the ALTER). Any other psycopg
        #      error (OperationalError, InsufficientPrivilege, SyntaxError, etc.)
        #      MUST propagate.
        #   3. Unconditional shared post-check via _assert_source_column_shape.
        #   4. Composite index (source, updated_at DESC) backing
        #      `thehomie session list` (PRP-7d R2 NM1).
        import psycopg  # local import — same lazy pattern as __init__

        cur.execute(
            "SELECT data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_name = 'chat_sessions' "
            "  AND column_name = 'source' "
            "  AND table_schema = current_schema()"
        )
        existing_source = cur.fetchone()
        if existing_source is None:
            try:
                cur.execute(
                    "ALTER TABLE chat_sessions "
                    "ADD COLUMN source TEXT NOT NULL DEFAULT 'interactive'"
                )
            except psycopg.errors.DuplicateColumn:
                # Concurrent first-boot race: another connection added the
                # column between our pre-check and our ALTER. Accept the race
                # and fall through to the unconditional post-check.
                pass
        _assert_source_column_shape(cur, backend="postgres")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_sessions_source_updated "
            "ON chat_sessions(source, updated_at DESC)"
        )

        cur.execute("""
            CREATE TABLE IF NOT EXISTS heartbeat_threads (
                id SERIAL PRIMARY KEY,
                channel_id TEXT NOT NULL,
                thread_ts TEXT NOT NULL,
                alert_text TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            )
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_hb_channel_thread
                ON heartbeat_threads(channel_id, thread_ts)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id BIGSERIAL PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                tool_calls_json TEXT DEFAULT '[]'
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_chat_messages_session_created
                ON chat_messages(session_id, created_at)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_chat_messages_fts
                ON chat_messages USING GIN (to_tsvector('simple', content))
        """)

    def _row_to_session(self, row: tuple) -> Session:
        """Convert a database row to a Session object."""
        runtime_session_id = row[3] if len(row) > 3 and row[3] else row[2]
        runtime_provider = row[4] if len(row) > 4 and row[4] else "claude"
        runtime_model = row[5] if len(row) > 5 and row[5] else ""
        runtime_profile_key = row[6] if len(row) > 6 and row[6] else ""
        return Session(
            session_id=row[1],
            agent_session_id=runtime_session_id,
            platform=row[7],
            channel_id=row[8],
            thread_id=row[9],
            user_id=row[10],
            created_at=(
                row[11]
                if isinstance(row[11], datetime)
                else datetime.fromisoformat(str(row[11]))
            ),
            updated_at=(
                row[12]
                if isinstance(row[12], datetime)
                else datetime.fromisoformat(str(row[12]))
            ),
            message_count=row[13],
            total_cost_usd=float(row[14]),
            tool_call_count=row[18] if len(row) > 18 else 0,
            status=row[15],
            mode=row[16] if len(row) > 16 and row[16] else "execute",
            runtime_lane=row[17] if len(row) > 17 and row[17] else "claude_native",
            runtime_provider=runtime_provider,
            runtime_model=runtime_model,
            runtime_profile_key=runtime_profile_key,
            runtime_tool_calls=_parse_tool_calls(row[19] if len(row) > 19 else None),
            # PRP-7d Postgres positional row index — source is column 20
            # (after id, session_id, agent_session_id, runtime_session_id,
            # runtime_provider, runtime_model, runtime_profile_key, platform,
            # channel_id, thread_id, user_id, created_at, updated_at,
            # message_count, total_cost_usd, status, mode, runtime_lane,
            # tool_call_count, runtime_tool_calls_json — count = 20, so source
            # is at index 20 = row[20]).
            source=normalize_source(row[20]) if len(row) > 20 else "interactive",
        )

    def get(self, platform: str, channel_id: str, thread_id: str) -> Session | None:
        """Look up a session by platform, channel, and thread."""
        session_id = build_session_key(platform, channel_id, thread_id)
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM chat_sessions WHERE session_id = %s",
            (session_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_session(row)

    def create(self, session: Session) -> None:
        """Insert a new session.

        Writes ``normalize_source(session.source)`` so non-Click writers cannot
        bypass the four-value enum (PRP-7d R1 M4). Set-once invariant: ``update``
        deliberately does NOT touch ``source``.
        """
        cur = self._conn.cursor()
        cur.execute(
            """INSERT INTO chat_sessions
               (session_id, agent_session_id, runtime_session_id, runtime_lane, runtime_provider,
                runtime_model, runtime_profile_key, platform, channel_id, thread_id, user_id,
                created_at, updated_at, message_count, total_cost_usd,
                status, mode, tool_call_count, runtime_tool_calls_json, source)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                session.session_id,
                session.agent_session_id,
                session.runtime_session_id,
                session.runtime_lane,
                session.runtime_provider,
                session.runtime_model,
                session.runtime_profile_key,
                session.platform,
                session.channel_id,
                session.thread_id,
                session.user_id,
                session.created_at,
                session.updated_at,
                session.message_count,
                session.total_cost_usd,
                session.status,
                session.mode,
                session.tool_call_count,
                _serialize_tool_calls(session.runtime_tool_calls),
                normalize_source(session.source),
            ),
        )

    def update(self, session: Session) -> None:
        """Update an existing session's mutable fields."""
        cur = self._conn.cursor()
        cur.execute(
            """UPDATE chat_sessions
               SET agent_session_id = %s, runtime_session_id = %s, runtime_lane = %s, runtime_provider = %s,
                   runtime_model = %s, runtime_profile_key = %s, updated_at = %s, message_count = %s,
                   total_cost_usd = %s, tool_call_count = %s, status = %s, mode = %s, runtime_tool_calls_json = %s
               WHERE session_id = %s""",
            (
                session.agent_session_id,
                session.runtime_session_id,
                session.runtime_lane,
                session.runtime_provider,
                session.runtime_model,
                session.runtime_profile_key,
                datetime.now(),
                session.message_count,
                session.total_cost_usd,
                session.tool_call_count,
                session.status,
                session.mode,
                _serialize_tool_calls(session.runtime_tool_calls),
                session.session_id,
            ),
        )

    def delete(self, platform: str, channel_id: str, thread_id: str) -> bool:
        """Delete a session by platform, channel, and thread. Returns True if a row was deleted."""
        session_id = build_session_key(platform, channel_id, thread_id)
        cur = self._conn.cursor()
        cur.execute(
            "DELETE FROM chat_messages WHERE session_id = %s",
            (session_id,),
        )
        cur.execute(
            "DELETE FROM chat_sessions WHERE session_id = %s",
            (session_id,),
        )
        return cur.rowcount > 0

    def list_active(
        self,
        platform: str | None = None,
        source: str | None = None,
        sources: list[str] | None = None,
    ) -> list[Session]:
        """List active sessions, optionally filtered by platform and/or source.

        ``source`` is exact-match; ``sources`` is an ``IN (...)`` filter. Both
        bind via ``%s`` placeholders. Existing callers that pass only
        ``platform`` (or nothing) keep working unchanged.
        """
        where_clauses: list[str] = ["status = 'active'"]
        params: list[Any] = []
        if platform:
            where_clauses.append("platform = %s")
            params.append(platform)
        if source:
            where_clauses.append("source = %s")
            params.append(source)
        elif sources:
            placeholders = ",".join(["%s"] * len(sources))
            where_clauses.append(f"source IN ({placeholders})")
            params.extend(sources)
        sql = (
            "SELECT * FROM chat_sessions WHERE "
            + " AND ".join(where_clauses)
            + " ORDER BY updated_at DESC"
        )
        cur = self._conn.cursor()
        cur.execute(sql, tuple(params))
        return [self._row_to_session(row) for row in cur.fetchall()]

    def list_recent(
        self,
        *,
        platform: str | None = None,
        source: str | None = None,
        sources: list[str] | None = None,
        hidden: tuple[str, ...] | None = None,
        limit: int = 20,
        all_sources: bool = False,
    ) -> list[SessionSummary]:
        """List recent sessions for ``thehomie session list`` (PRP-7d R2 NM4).

        Postgres mirror of ``SQLiteSessionStore.list_recent`` — see that
        method's docstring for the full contract. Always returns
        ``list[SessionSummary]``; ``hidden`` uses the None-sentinel pattern.
        """

        if hidden is None:
            hidden = SOURCE_HIDDEN_BY_DEFAULT
        where_clauses: list[str] = []
        params: list[Any] = []
        if platform:
            where_clauses.append("platform = %s")
            params.append(platform)
        if source:
            where_clauses.append("source = %s")
            params.append(source)
        elif sources:
            placeholders = ",".join(["%s"] * len(sources))
            where_clauses.append(f"source IN ({placeholders})")
            params.extend(sources)
        elif not all_sources and hidden:
            placeholders = ",".join(["%s"] * len(hidden))
            where_clauses.append(f"source NOT IN ({placeholders})")
            params.extend(hidden)
        where_sql = (
            (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        )
        sql = (
            "SELECT id, session_id, platform, source, message_count, "
            "updated_at, runtime_session_id, agent_session_id "
            "FROM chat_sessions"
            + where_sql
            + " ORDER BY updated_at DESC LIMIT %s"
        )
        params.append(limit)
        cur = self._conn.cursor()
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
        summaries: list[SessionSummary] = []
        for row in rows:
            updated_at = (
                row[5]
                if isinstance(row[5], datetime)
                else datetime.fromisoformat(str(row[5]))
            )
            runtime_id = row[6] if len(row) > 6 and row[6] else (
                row[7] if len(row) > 7 and row[7] else row[1]
            )
            summaries.append(
                SessionSummary(
                    internal_id=row[0],
                    session_id=row[1],
                    platform=row[2],
                    source=normalize_source(row[3]),
                    message_count=row[4],
                    updated_at=updated_at,
                    runtime_session_id=runtime_id,
                )
            )
        return summaries

    def get_by_session_id(self, session_id: str) -> Session | None:
        """Look up a session by composite session_id OR runtime_session_id.

        PRD §7.10 contract (PRP-7d R1 B3): the argument is the stable string
        identifier (composite ``platform:channel:thread`` OR runtime UUID), NOT
        the SERIAL primary key.
        """

        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM chat_sessions "
            "WHERE session_id = %s OR runtime_session_id = %s "
            "LIMIT 1",
            (session_id, session_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_session(row)

    def get_by_user(self, user_id: str) -> list[Session]:
        """Look up all sessions for a user across all platforms."""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM chat_sessions WHERE user_id = %s ORDER BY updated_at DESC",
            (user_id,),
        )
        return [self._row_to_session(row) for row in cur.fetchall()]

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        created_at: datetime | None = None,
        tool_calls: Any = None,
    ) -> None:
        """Persist one chat message for transcript replay/search."""

        cur = self._conn.cursor()
        cur.execute(
            """INSERT INTO chat_messages (session_id, role, content, created_at, tool_calls_json)
               VALUES (%s, %s, %s, %s, %s)""",
            (session_id, role, content, created_at or datetime.now(), _serialize_tool_calls(tool_calls)),
        )

    def list_messages(self, session_id: str, limit: int = 200) -> list[ChatMessage]:
        """List chat messages for a session in chronological order."""

        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT id, session_id, role, content, created_at, tool_calls_json
            FROM chat_messages
            WHERE session_id = %s
            ORDER BY created_at ASC, id ASC
            LIMIT %s
            """,
            (session_id, limit),
        )
        rows = cur.fetchall()
        return [
            ChatMessage(
                id=row[0],
                session_id=row[1],
                role=row[2],
                content=row[3],
                created_at=(row[4] if isinstance(row[4], datetime) else datetime.fromisoformat(str(row[4]))),
                tool_calls=_parse_tool_calls(row[5] if len(row) > 5 else None),
            )
            for row in rows
        ]

    def search_messages(
        self,
        query: str,
        limit: int = 20,
        session_id: str | None = None,
    ) -> list[ChatMessage]:
        """Search persisted chat messages."""

        if not query.strip():
            return []

        cur = self._conn.cursor()
        if session_id:
            cur.execute(
                """
                SELECT id, session_id, role, content, created_at, tool_calls_json
                FROM chat_messages
                WHERE session_id = %s
                  AND to_tsvector('simple', content) @@ plainto_tsquery('simple', %s)
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (session_id, query, limit),
            )
        else:
            cur.execute(
                """
                SELECT id, session_id, role, content, created_at, tool_calls_json
                FROM chat_messages
                WHERE to_tsvector('simple', content) @@ plainto_tsquery('simple', %s)
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (query, limit),
            )
        rows = cur.fetchall()
        return [
            ChatMessage(
                id=row[0],
                session_id=row[1],
                role=row[2],
                content=row[3],
                created_at=(row[4] if isinstance(row[4], datetime) else datetime.fromisoformat(str(row[4]))),
                tool_calls=_parse_tool_calls(row[5] if len(row) > 5 else None),
            )
            for row in rows
        ]

    def save_heartbeat_thread(self, thread: HeartbeatThread) -> None:
        """Record a heartbeat notification so thread replies can be linked."""
        cur = self._conn.cursor()
        cur.execute(
            """INSERT INTO heartbeat_threads (channel_id, thread_ts, alert_text, created_at)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (channel_id, thread_ts) DO UPDATE SET
                   alert_text = EXCLUDED.alert_text,
                   created_at = EXCLUDED.created_at""",
            (thread.channel_id, thread.thread_ts, thread.alert_text, thread.created_at),
        )

    def get_heartbeat_thread(self, channel_id: str, thread_ts: str) -> HeartbeatThread | None:
        """Look up a heartbeat thread by channel and ts."""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT channel_id, thread_ts, alert_text, created_at FROM heartbeat_threads "
            "WHERE channel_id = %s AND thread_ts = %s",
            (channel_id, thread_ts),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return HeartbeatThread(
            channel_id=row[0],
            thread_ts=row[1],
            alert_text=row[2],
            created_at=(
                row[3]
                if isinstance(row[3], datetime)
                else datetime.fromisoformat(str(row[3]))
            ),
        )

    def close(self) -> None:
        """Close the database connection."""
        if self._conn and not self._conn.closed:
            self._conn.close()


def read_operator_user_turns(
    window_start: datetime,
    *,
    store: SQLiteSessionStore | PostgresSessionStore | None = None,
) -> list[str]:
    """Read the operator's VERBATIM ``role == "user"`` turns over a window (Living Self Act 1, B2).

    This is the ONLY place the operator's exact words live — the engine persists
    every turn here, so the reflection belief-extractor reads chat.db (NOT the
    daily-log paraphrase, which is the bot's third-person restatement, and NOT
    staging, which carries no role marker). Reads the SAME store recall uses; no
    schema change. Works for SQLite AND Postgres because ``list_active`` /
    ``list_messages`` are on both stores.

    NB1 (tz no-op fix — the load-bearing correctness fix): ``window_start`` may
    be tz-aware (``now_local()``) while SQLite ``created_at`` is NAIVE and
    Postgres ``created_at`` is AWARE. A raw ``naive >= aware`` comparison raises
    ``TypeError``, gets swallowed by the fail-open, and returns ``[]`` forever —
    the self never forms a belief. We normalize BOTH sides to naive-local via the
    canonical ``normalize_physical_timestamp`` owner before comparing.

    NM1 (window prefilter): ``list_active`` orders by ``updated_at DESC``, so we
    descend ONLY into sessions whose ``updated_at`` is within the window and break
    once a session is older than the window — instead of reading the full message
    history of every interactive session ever. KNOWN LIMITATION: ``list_messages``
    has a hard ``limit=200`` and returns oldest-first, so a single session with
    >200 messages drops its oldest turns; the ``updated_at`` prefilter keeps the
    read bounded and this is an accepted, documented bound for Act 1.

    NM2 (slash-command drop): rows whose stripped text starts with ``/`` are
    commands (``/help``, ``/status``, ``/video`` …), not stated beliefs — they are
    dropped so they don't dilute the extractor's claim budget or tempt a weak
    provider to hallucinate a belief from a command verb.

    Whole-body try/except -> ``[]`` on ANY error (non-blocking — reflection
    survives an empty/locked store).
    """
    try:
        from cognition.proactive_brief import normalize_physical_timestamp

        if store is None:
            store = get_session_store()

        window_naive = normalize_physical_timestamp(window_start)
        turns: list[str] = []
        for sess in store.list_active(source="interactive"):
            # NM1: list_active is updated_at DESC — once a session's last
            # activity predates the window, every later session is older too.
            sess_updated = normalize_physical_timestamp(sess.updated_at)
            if (
                window_naive is not None
                and sess_updated is not None
                and sess_updated < window_naive
            ):
                break
            for m in store.list_messages(sess.session_id):
                if m.role != "user":
                    continue
                created = normalize_physical_timestamp(m.created_at)
                if (
                    window_naive is not None
                    and created is not None
                    and created < window_naive
                ):
                    continue
                text = (m.content or "").strip()
                if not text:
                    continue
                # NM2: drop slash commands — not stated beliefs.
                if text.startswith("/"):
                    continue
                turns.append(text)
        return turns
    except Exception as exc:
        # non-blocking — reflection survives an empty/locked store, but the
        # failure MUST be visible: a silent [] is indistinguishable from a
        # legitimately quiet window (the project's recurring silent-failure
        # class — cf. the R2 timestamp no-op + Act-3 episode-flip swallow).
        print(f"[read_operator_user_turns] read failed (non-fatal): {exc!r}", flush=True)
        return []


def get_session_store(
    chat_db_path: Path | None = None,
) -> SQLiteSessionStore | PostgresSessionStore:
    """Factory: returns Postgres if DATABASE_URL is set, else SQLite."""
    url = os.getenv("DATABASE_URL", "")
    if url:
        return PostgresSessionStore(url)
    if chat_db_path is None:
        from config import CHAT_DB_PATH

        chat_db_path = CHAT_DB_PATH
    return SQLiteSessionStore(chat_db_path)
