"""Persona learning loop — persona_id column on session stores.

Tests cover:
A. Schema migration (SQLite fresh + existing DB)
B. Session dataclass persona_id field
C. Create INSERT carries persona_id
D. _row_to_session reads persona_id
E. Update does NOT touch persona_id (set-once)
F. Three-valued list_active persona_id filter

Sign off: YourAgent (US-001 executor).
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _TESTS_DIR.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for p in [str(_SCRIPTS_DIR), str(_CHAT_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from session import (  # noqa: E402
    Session,
    SQLiteSessionStore,
    _UNSET,
)


def _make_session(
    session_id: str = "test:ch:th",
    persona_id: str | None = None,
    source: str = "interactive",
) -> Session:
    now = datetime.now()
    return Session(
        session_id=session_id,
        agent_session_id="agent-1",
        platform="test",
        channel_id="ch",
        thread_id="th",
        user_id="user-1",
        created_at=now,
        updated_at=now,
        message_count=1,
        total_cost_usd=0.0,
        tool_call_count=0,
        status="active",
        source=source,
        persona_id=persona_id,
    )


# ============================================================================
# A. Schema migration
# ============================================================================


class TestSchemaMigration:
    """persona_id column is created on fresh + existing DBs."""

    def test_fresh_db_has_persona_id_column(self, tmp_path: Path) -> None:
        db_path = tmp_path / "chat.db"
        store = SQLiteSessionStore(db_path)
        with store._connect() as conn:
            cols = {
                r[1]: r
                for r in conn.execute("PRAGMA table_info(chat_sessions)").fetchall()
            }
            assert "persona_id" in cols
            col = cols["persona_id"]
            assert col[2].upper() == "TEXT"
            assert col[3] == 0  # NOT notnull — nullable

    def test_existing_db_gains_persona_id_column(self, tmp_path: Path) -> None:
        db_path = tmp_path / "chat.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE chat_sessions ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "  session_id TEXT NOT NULL UNIQUE, "
            "  agent_session_id TEXT NOT NULL, "
            "  runtime_session_id TEXT DEFAULT '', "
            "  runtime_provider TEXT DEFAULT 'claude', "
            "  runtime_model TEXT DEFAULT '', "
            "  runtime_profile_key TEXT DEFAULT '', "
            "  platform TEXT NOT NULL, "
            "  channel_id TEXT NOT NULL, "
            "  thread_id TEXT NOT NULL, "
            "  user_id TEXT NOT NULL, "
            "  created_at TEXT NOT NULL, "
            "  updated_at TEXT NOT NULL, "
            "  message_count INTEGER DEFAULT 0, "
            "  total_cost_usd REAL DEFAULT 0.0, "
            "  status TEXT DEFAULT 'active', "
            "  mode TEXT DEFAULT 'execute', "
            "  runtime_lane TEXT DEFAULT 'claude_native', "
            "  tool_call_count INTEGER DEFAULT 0, "
            "  runtime_tool_calls_json TEXT DEFAULT '[]', "
            "  source TEXT NOT NULL DEFAULT 'interactive'"
            ")"
        )
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO chat_sessions "
            "(session_id, agent_session_id, platform, channel_id, thread_id, "
            " user_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("s1", "a1", "test", "c1", "t1", "u1", now, now),
        )
        conn.commit()
        conn.close()

        store = SQLiteSessionStore(db_path)
        with store._connect() as conn2:
            cols = {
                r[1]: r
                for r in conn2.execute("PRAGMA table_info(chat_sessions)").fetchall()
            }
            assert "persona_id" in cols
            row = conn2.execute(
                "SELECT persona_id FROM chat_sessions WHERE session_id = 's1'"
            ).fetchone()
            assert row[0] is None

    def test_persona_id_index_created(self, tmp_path: Path) -> None:
        db_path = tmp_path / "chat.db"
        SQLiteSessionStore(db_path)
        conn = sqlite3.connect(db_path)
        indices = [
            r[1]
            for r in conn.execute(
                "SELECT * FROM sqlite_master WHERE type='index'"
            ).fetchall()
        ]
        assert "idx_chat_sessions_persona_source_updated" in indices
        conn.close()


# ============================================================================
# B. Session dataclass
# ============================================================================


class TestSessionDataclass:
    def test_default_persona_id_is_none(self) -> None:
        s = _make_session()
        assert s.persona_id is None

    def test_persona_id_set(self) -> None:
        s = _make_session(persona_id="sales")
        assert s.persona_id == "sales"


# ============================================================================
# C + D. Create INSERT + _row_to_session
# ============================================================================


class TestCreateAndRead:
    def test_create_with_persona_id(self, tmp_path: Path) -> None:
        store = SQLiteSessionStore(tmp_path / "chat.db")
        s = _make_session(persona_id="sales")
        store.create(s)
        got = store.get("test", "ch", "th")
        assert got is not None
        assert got.persona_id == "sales"

    def test_create_without_persona_id(self, tmp_path: Path) -> None:
        store = SQLiteSessionStore(tmp_path / "chat.db")
        s = _make_session()
        store.create(s)
        got = store.get("test", "ch", "th")
        assert got is not None
        assert got.persona_id is None


# ============================================================================
# E. Set-once semantics
# ============================================================================


class TestSetOnce:
    def test_update_does_not_change_persona_id(self, tmp_path: Path) -> None:
        store = SQLiteSessionStore(tmp_path / "chat.db")
        s = _make_session(persona_id="sales")
        store.create(s)

        s.message_count = 5
        store.update(s)

        got = store.get("test", "ch", "th")
        assert got is not None
        assert got.persona_id == "sales"
        assert got.message_count == 5

    def test_update_cannot_overwrite_persona_id_via_sql(self, tmp_path: Path) -> None:
        store = SQLiteSessionStore(tmp_path / "chat.db")
        s = _make_session(persona_id="sales")
        store.create(s)

        with store._connect() as conn:
            row = conn.execute(
                "SELECT persona_id FROM chat_sessions WHERE session_id = ?",
                (s.session_id,),
            ).fetchone()
            assert row[0] == "sales"

        s.persona_id = "marketing"
        store.update(s)

        with store._connect() as conn:
            row = conn.execute(
                "SELECT persona_id FROM chat_sessions WHERE session_id = ?",
                (s.session_id,),
            ).fetchone()
            assert row[0] == "sales"


# ============================================================================
# F. Three-valued list_active persona_id filter
# ============================================================================


class TestListActivePersonaFilter:
    def _seed(self, store: SQLiteSessionStore) -> None:
        store.create(_make_session(session_id="test:main:1", persona_id=None))
        store.create(_make_session(session_id="test:main:2", persona_id=None))
        store.create(_make_session(session_id="test:sales:1", persona_id="sales"))
        store.create(_make_session(session_id="test:mktg:1", persona_id="marketing"))

    def test_no_filter_returns_all(self, tmp_path: Path) -> None:
        store = SQLiteSessionStore(tmp_path / "chat.db")
        self._seed(store)
        result = store.list_active()
        assert len(result) == 4

    def test_persona_id_none_returns_only_null(self, tmp_path: Path) -> None:
        store = SQLiteSessionStore(tmp_path / "chat.db")
        self._seed(store)
        result = store.list_active(persona_id=None)
        assert len(result) == 2
        assert all(s.persona_id is None for s in result)

    def test_persona_id_string_returns_exact_match(self, tmp_path: Path) -> None:
        store = SQLiteSessionStore(tmp_path / "chat.db")
        self._seed(store)
        result = store.list_active(persona_id="sales")
        assert len(result) == 1
        assert result[0].persona_id == "sales"

    def test_persona_id_unset_default_no_filter(self, tmp_path: Path) -> None:
        store = SQLiteSessionStore(tmp_path / "chat.db")
        self._seed(store)
        result = store.list_active(persona_id=_UNSET)
        assert len(result) == 4

    def test_combined_source_and_persona_id(self, tmp_path: Path) -> None:
        store = SQLiteSessionStore(tmp_path / "chat.db")
        self._seed(store)
        result = store.list_active(source="interactive", persona_id=None)
        assert len(result) == 2
        assert all(s.persona_id is None for s in result)
        assert all(s.source == "interactive" for s in result)

    def test_persona_id_nonexistent_returns_empty(self, tmp_path: Path) -> None:
        store = SQLiteSessionStore(tmp_path / "chat.db")
        self._seed(store)
        result = store.list_active(persona_id="nonexistent")
        assert len(result) == 0


# ============================================================================
# Grep gate: session_keys.py must be unchanged
# ============================================================================


class TestGrepGates:
    def test_session_keys_has_no_persona_references(self) -> None:
        keys_file = _CHAT_DIR / "session_keys.py"
        content = keys_file.read_text()
        assert "persona" not in content.lower()

    def test_runtime_profile_key_not_reused_for_attribution(self) -> None:
        s = _make_session(persona_id="sales")
        assert s.runtime_profile_key == ""
        assert s.persona_id == "sales"
        assert s.runtime_profile_key != s.persona_id
