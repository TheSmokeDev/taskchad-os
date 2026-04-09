from __future__ import annotations

from datetime import datetime

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
