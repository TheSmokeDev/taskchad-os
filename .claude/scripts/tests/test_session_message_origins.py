"""Ingress identity survives persistence and later transcript representations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from engine import ConversationEngine
from models import Channel, IncomingMessage, Platform, User
from session import PostgresSessionStore, Session, SQLiteSessionStore, read_operator_user_turns
from session_lifecycle_hooks import write_clear_transcript

from personas.learning.hooks import incoming_origin


def _session(now):
    return Session("cli:test:test", "runtime", "cli", "test", "test", "operator", now, now)


def test_sqlite_origin_survives_restart_and_all_message_readers(tmp_path):
    path = tmp_path / "chat.db"
    store = SQLiteSessionStore(path)
    now = datetime.now()
    store.create(_session(now))
    store.add_message(
        "cli:test:test",
        "user",
        "identical wording",
        now,
        source_ref="chat-message:ingress-one:user",
    )
    store.add_message(
        "cli:test:test",
        "user",
        "identical wording",
        now,
        source_ref="chat-message:ingress-two:user",
    )
    restarted = SQLiteSessionStore(path)
    expected = {"chat-message:ingress-one:user", "chat-message:ingress-two:user"}
    for rows in (
        restarted.list_messages("cli:test:test"),
        restarted.list_recent_messages("cli:test:test"),
        restarted.search_messages("wording"),
    ):
        assert {row.source_ref for row in rows} == expected
        assert len({row.id for row in rows}) == 2
        assert len({row.source_revision for row in rows}) == 1
    turns = read_operator_user_turns(
        now - timedelta(days=1), store=restarted, include_provenance=True
    )
    assert {row["source_ref"] for row in turns} == expected


def test_sqlite_additive_upgrade_leaves_old_rows_without_inferred_origin(tmp_path):
    path = tmp_path / "legacy.db"
    now = datetime.now().isoformat()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE chat_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, "
            "created_at TEXT NOT NULL, tool_calls_json TEXT DEFAULT '[]')"
        )
        conn.execute(
            "INSERT INTO chat_messages VALUES(73, 's', 'user', 'old text', ?, '[]')", (now,)
        )
    for _ in range(2):
        store = SQLiteSessionStore(path)
        row = store.list_messages("s")[0]
        assert row.id == 73
        assert row.source_origin_ref is None
        assert row.source_ref == "chat-message:s:73"
        with store._connect() as conn:
            columns = {row[1]: row for row in conn.execute("PRAGMA table_info(chat_messages)")}
        assert columns["source_origin_ref"][2:4] == ("TEXT", 0)


class _PostgresProtocol:
    """SQL-boundary protocol fixture; no server, credentials, or inferred IDs."""

    def __init__(self):
        self.statements = []
        self.rows = []
        self.query = ""

    def execute(self, query, params=None):
        self.query = " ".join(query.split())
        self.statements.append((self.query, params))
        if self.query.startswith("INSERT INTO chat_messages"):
            assert "source_origin_ref" in self.query
            assert "%s" in self.query
            self.rows.append((len(self.rows) + 201, *params))

    def fetchone(self):
        if "information_schema.columns" in self.query:
            if "source_origin_ref" in self.query:
                return ("text", "YES")
            return ("text", "NO", "'interactive'")
        if "COUNT(*)" in self.query:
            return (0,)
        raise AssertionError(self.query)

    def fetchall(self):
        assert "source_origin_ref" in self.query
        return list(self.rows)


def test_postgres_origin_migration_parameter_binding_and_row_decoding(monkeypatch):
    cursor = _PostgresProtocol()
    conn = SimpleNamespace(cursor=lambda: cursor)
    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        SimpleNamespace(
            connect=lambda *_a, **_k: conn,
            errors=SimpleNamespace(DuplicateColumn=RuntimeError),
        ),
    )
    store = PostgresSessionStore("synthetic-protocol-only")
    assert any("ADD COLUMN IF NOT EXISTS source_origin_ref TEXT" in q for q, _ in cursor.statements)
    now = datetime.now(UTC)
    store.add_message("s", "user", "same wording", now, source_ref="chat-message:event:user")
    restarted = PostgresSessionStore("synthetic-protocol-only")
    for rows in (
        restarted.list_messages("s"),
        restarted.list_recent_messages("s"),
        restarted.search_messages("wording"),
    ):
        assert rows[0].id == 201
        assert rows[0].source_origin_ref == "chat-message:event:user"
        assert rows[0].source_ref == "chat-message:event:user"
    store.add_message("s", "assistant", "legacy caller", now)
    assert store.list_messages("s")[-1].source_ref == "chat-message:s:202"


def test_engine_ingress_identity_matches_persisted_rows_and_clear_envelope(tmp_path, monkeypatch):
    monkeypatch.delenv("HOMIE_LEARNING_ORIGIN_KEY", raising=False)
    now = datetime.now()
    message = IncomingMessage(
        "internal rewritten instructions",
        User(Platform.CLI, "operator", "Operator"),
        Channel(Platform.CLI, "test", is_dm=True),
        Platform.CLI,
        platform_message_id="physical-ingress-7",
        timestamp=now,
        raw_event={"display_text": "real operator wording"},
    )
    session_key = "cli:test:test"
    origin_before_persist = incoming_origin(message, session_key)
    expected_user = f"chat-message:{origin_before_persist}:user"
    expected_revision = hashlib.sha256(
        json.dumps(["user", "real operator wording"], ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    path = tmp_path / "chat.db"
    engine = ConversationEngine.__new__(ConversationEngine)
    engine.session_store = SQLiteSessionStore(path)
    engine._persist_engine_turn(
        session_key=session_key,
        platform_str="cli",
        channel_id="test",
        thread_id="test",
        message=message,
        response_text="actual assistant response",
        persisted_runtime_session_id="r",
        normalized_tool_calls=[],
        result=SimpleNamespace(
            runtime_lane="generic_runtime",
            provider="test",
            model="test",
            profile_key="test",
            tool_call_count=0,
        ),
        cost_usd=0.0,
        mode="execute",
        now=now,
    )
    restarted = SQLiteSessionStore(path)
    user, assistant = restarted.list_messages(session_key)
    assert user.source_ref == expected_user
    assert user.source_revision == expected_revision
    assert assistant.source_ref == f"chat-message:{origin_before_persist}:assistant"
    transcript = write_clear_transcript(
        store=restarted,
        session=restarted.get("cli", "test", "test"),
        platform="cli",
        channel_id="test",
        thread_id="test",
        state_dir=tmp_path / "state",
    )
    rows = [json.loads(line) for line in transcript.read_text(encoding="utf-8").splitlines()]
    assert rows[1]["source_ref"] == expected_user
    assert rows[1]["source_revision"] == expected_revision
    assert rows[1]["source_message_id"] == user.id


@pytest.mark.parametrize("bad_ref", ["", "  ", 7, "x" * 2049])
def test_malformed_origin_is_rejected_without_message_write(tmp_path, bad_ref):
    store = SQLiteSessionStore(tmp_path / "chat.db")
    with pytest.raises(ValueError, match="source_ref"):
        store.add_message("s", "user", "message", source_ref=bad_ref)
    assert store.list_messages("s") == []
