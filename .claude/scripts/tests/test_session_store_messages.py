from __future__ import annotations

from datetime import datetime

import session as session_module
from session import Session, SQLiteSessionStore
from session_keys import (
    build_session_key,
    build_web_channel_id,
    build_web_conversation_id,
    resolve_thread_id,
)


def test_session_key_helpers_preserve_current_contract() -> None:
    assert resolve_thread_id("chan", None) == "chan"
    assert resolve_thread_id("chan", "thread") == "thread"
    assert build_session_key("web", "chan", "thread") == "web:chan:thread"
    assert build_session_key("web", "chan", None) == "web:chan:chan"
    assert build_web_channel_id("web:user:thread", "user") == "web:user:thread"
    assert build_web_channel_id("", "user1") == "web:user1"
    assert build_web_conversation_id("web:user:thread", "user1") == "web:user:thread"
    assert build_web_conversation_id("", "user1", "thehomie") == "web:thehomie:user1"


def test_sqlite_session_store_persists_and_searches_chat_messages(tmp_path) -> None:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    session_id = build_session_key("web", "channel-1", "thread-1")
    now = datetime.now()

    store.create(
        Session(
            session_id=session_id,
            agent_session_id="agent-1",
            platform="web",
            channel_id="channel-1",
            thread_id="thread-1",
            user_id="user-1",
            created_at=now,
            updated_at=now,
        )
    )

    store.add_message(session_id, "user", "Tell me about convoy retries", now)
    store.add_message(
        session_id,
        "assistant",
        "Convoy retries need jitter",
        now,
        tool_calls=[{"id": "tc-1", "name": "Read", "arguments": {"path": "convoy.py"}}],
    )

    messages = store.list_messages(session_id)
    assert [msg.role for msg in messages] == ["user", "assistant"]
    assert messages[0].content == "Tell me about convoy retries"
    assert messages[1].content == "Convoy retries need jitter"
    assert messages[1].tool_calls == [{"id": "tc-1", "name": "Read", "arguments": {"path": "convoy.py"}}]

    search_results = store.search_messages("jitter", session_id=session_id)
    assert len(search_results) == 1
    assert search_results[0].role == "assistant"
    assert "jitter" in search_results[0].content.lower()


def test_sqlite_session_store_persists_runtime_tool_calls_on_session(tmp_path) -> None:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    session_id = build_session_key("web", "channel-1", "thread-1")
    now = datetime.now()

    store.create(
        Session(
            session_id=session_id,
            agent_session_id="agent-1",
            platform="web",
            channel_id="channel-1",
            thread_id="thread-1",
            user_id="user-1",
            created_at=now,
            updated_at=now,
            runtime_tool_calls=[{"id": "tc-1", "name": "Read", "arguments": {"path": "foo.py"}}],
        )
    )

    persisted = store.get("web", "channel-1", "thread-1")
    assert persisted is not None
    assert persisted.runtime_tool_calls == [{"id": "tc-1", "name": "Read", "arguments": {"path": "foo.py"}}]


def test_session_delete_cascades_chat_messages(tmp_path) -> None:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    session_id = build_session_key("web", "channel-2", "thread-2")
    now = datetime.now()

    store.create(
        Session(
            session_id=session_id,
            agent_session_id="agent-2",
            platform="web",
            channel_id="channel-2",
            thread_id="thread-2",
            user_id="user-2",
            created_at=now,
            updated_at=now,
        )
    )
    store.add_message(session_id, "user", "old message", now)
    assert len(store.list_messages(session_id)) == 1

    assert store.delete("web", "channel-2", "thread-2") is True
    assert store.list_messages(session_id) == []
    assert store.search_messages("old", session_id=session_id) == []


# =============================================================================
# Issue #131 — WAL + busy_timeout pragmas on every connection, with a fail-open
# warning when the WAL conversion cannot be applied.
# =============================================================================


def test_fresh_store_uses_wal_and_busy_timeout(tmp_path) -> None:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    with store._connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_self_heal_reinit_keeps_wal(tmp_path) -> None:
    import gc

    db = tmp_path / "chat.db"
    store = SQLiteSessionStore(db)
    # Simulate a `git clean -x` wipe mid-run: it removes the DB and its WAL
    # sidecars. gc.collect() drops the lingering _init_db connection so Windows
    # releases the file handle (on the Linux box, open fds never block unlink).
    gc.collect()
    for name in ("chat.db", "chat.db-wal", "chat.db-shm"):
        sidecar = tmp_path / name
        if sidecar.exists():
            sidecar.unlink()
    with store._connect() as conn:  # re-init path (session.py _connect)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_wal_fallback_warns_not_crashes(tmp_path, monkeypatch, capsys) -> None:
    # An in-memory DB can never be WAL — journal_mode returns "memory", which
    # exercises the validation branch without mocking sqlite internals. Capture
    # the real connect FIRST so the replacement doesn't recurse into itself.
    real_connect = session_module.sqlite3.connect

    def fake_connect(*_args, **_kwargs):
        return real_connect(":memory:", check_same_thread=False)

    monkeypatch.setattr(session_module.sqlite3, "connect", fake_connect)
    monkeypatch.setattr(SQLiteSessionStore, "_wal_warned", False)

    SQLiteSessionStore(tmp_path / "chat.db")  # must not raise

    assert "journal_mode=WAL not applied" in capsys.readouterr().out
