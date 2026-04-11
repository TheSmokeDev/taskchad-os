"""Session stores for persistent chat conversations (SQLite + Postgres)."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from session_keys import build_session_key


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

    @property
    def runtime_session_id(self) -> str:
        """Runtime-neutral alias for the persisted session identifier."""

        return self.agent_session_id

    @runtime_session_id.setter
    def runtime_session_id(self, value: str) -> None:
        self.agent_session_id = value


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

    def _init_db(self) -> None:
        """Create the chat/session tables if they don't exist."""
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
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
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM chat_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_session(row)

    def create(self, session: Session) -> None:
        """Insert a new session."""
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO chat_sessions
                   (session_id, agent_session_id, runtime_session_id, runtime_lane, runtime_provider,
                    runtime_model, runtime_profile_key, platform, channel_id, thread_id, user_id,
                    created_at, updated_at, message_count, total_cost_usd,
                    status, mode, tool_call_count, runtime_tool_calls_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                ),
            )

    def update(self, session: Session) -> None:
        """Update an existing session's mutable fields."""
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
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
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            conn.execute(
                "DELETE FROM chat_messages WHERE session_id = ?",
                (session_id,),
            )
            cursor = conn.execute(
                "DELETE FROM chat_sessions WHERE session_id = ?",
                (session_id,),
            )
            return cursor.rowcount > 0

    def list_active(self, platform: str | None = None) -> list[Session]:
        """List active sessions, optionally filtered by platform."""
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            conn.row_factory = sqlite3.Row
            if platform:
                rows = conn.execute(
                    "SELECT * FROM chat_sessions WHERE status = 'active' AND platform = ? "
                    "ORDER BY updated_at DESC",
                    (platform,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM chat_sessions WHERE status = 'active' ORDER BY updated_at DESC"
                ).fetchall()
            return [self._row_to_session(row) for row in rows]

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
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            conn.execute(
                """INSERT INTO chat_messages (session_id, role, content, created_at, tool_calls_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, role, content, timestamp, _serialize_tool_calls(tool_calls)),
            )

    def list_messages(self, session_id: str, limit: int = 200) -> list[ChatMessage]:
        """List chat messages for a session in chronological order."""

        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
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

        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
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
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM chat_sessions WHERE user_id = ? ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()
            return [self._row_to_session(row) for row in rows]

    def save_heartbeat_thread(self, thread: HeartbeatThread) -> None:
        """Record a heartbeat notification so thread replies can be linked."""
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
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
        with sqlite3.connect(self.db_path, check_same_thread=False) as conn:
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
        """Insert a new session."""
        cur = self._conn.cursor()
        cur.execute(
            """INSERT INTO chat_sessions
               (session_id, agent_session_id, runtime_session_id, runtime_lane, runtime_provider,
                runtime_model, runtime_profile_key, platform, channel_id, thread_id, user_id,
                created_at, updated_at, message_count, total_cost_usd,
                status, mode, tool_call_count, runtime_tool_calls_json)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
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

    def list_active(self, platform: str | None = None) -> list[Session]:
        """List active sessions, optionally filtered by platform."""
        cur = self._conn.cursor()
        if platform:
            cur.execute(
                "SELECT * FROM chat_sessions WHERE status = 'active' AND platform = %s "
                "ORDER BY updated_at DESC",
                (platform,),
            )
        else:
            cur.execute(
                "SELECT * FROM chat_sessions WHERE status = 'active' ORDER BY updated_at DESC"
            )
        return [self._row_to_session(row) for row in cur.fetchall()]

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
