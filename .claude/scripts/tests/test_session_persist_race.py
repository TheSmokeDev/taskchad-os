"""Issue #131 — off-loop session persists must stay race-safe.

PR #144's naive per-call ``asyncio.to_thread`` removed the implicit
serialization the single-threaded event loop provided, so two persists for the
SAME conversation could run concurrently on worker threads and lose a
``message_count`` increment (read-modify-write). The gate required a
per-conversation lock SHARED by the router + engine + persona persist paths,
plus an engine persist that RE-READS the row inside the worker thread (so the
timeout-shield snapshot can't clobber an interleaved router bump).

These tests prove: (1) the persist runs off the event-loop thread; (2)
router-vs-router keeps the count; (3) the engine's fresh re-read merges onto an
interleaved router bump instead of a stale snapshot; (4) the create-vs-update
collision no longer raises; (5) all paths share one lock keyed by the canonical
session key.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime
from types import SimpleNamespace

import pytest
import router as router_module
from engine import ConversationEngine
from models import Channel, IncomingMessage, Platform, User
from router import ChatRouter
from session import Session, SQLiteSessionStore, get_persist_lock
from session_keys import build_session_key, resolve_thread_id


class _FakeEngine:
    """Minimal engine exposing only what ``_persist_router_turn`` reads."""

    def __init__(self, store: SQLiteSessionStore) -> None:
        self.session_store = store


class _StubManager:
    """ChatRouter only stores the manager at construction; nothing is called."""


def _cli_incoming(text: str, *, source: str = "interactive") -> IncomingMessage:
    return IncomingMessage(
        text=text,
        user=User(Platform.CLI, "cli-user", "User"),
        channel=Channel(Platform.CLI, "cli-test", is_dm=True),
        platform=Platform.CLI,
        timestamp=datetime.now(),
        source=source,
    )


def _engine_with_store(store: SQLiteSessionStore) -> ConversationEngine:
    """A bare ConversationEngine — ``_persist_engine_turn`` only needs the store,
    so skip the heavy identity-context construction."""
    convo = ConversationEngine.__new__(ConversationEngine)
    convo.session_store = store
    return convo


def _fake_result() -> SimpleNamespace:
    return SimpleNamespace(
        runtime_lane="claude_native",
        provider="claude",
        model="claude-opus-4-8",
        profile_key="primary-claude",
        tool_call_count=0,
    )


_KEY = build_session_key("cli", "cli-test", "cli-test")


@pytest.mark.asyncio
async def test_persist_offload_keeps_loop_free(tmp_path) -> None:
    """The router persist body executes on a worker thread, not the loop."""
    main_thread = threading.current_thread()
    store = SQLiteSessionStore(tmp_path / "chat.db")
    seen: dict[str, threading.Thread] = {}
    original_create = store.create

    def spy_create(session):
        seen["thread"] = threading.current_thread()
        return original_create(session)

    store.create = spy_create  # type: ignore[method-assign]
    router = ChatRouter(_FakeEngine(store), _StubManager())

    await router._persist_router_turn_off_loop(_cli_incoming("hi"), "reply")

    assert seen["thread"] is not main_thread, "persist must run off the event loop"
    persisted = store.get("cli", "cli-test", "cli-test")
    assert persisted is not None and persisted.message_count == 1


@pytest.mark.asyncio
async def test_router_vs_router_count_preserved(tmp_path) -> None:
    """Two concurrent router persists for one conversation keep both turns."""
    store = SQLiteSessionStore(tmp_path / "chat.db")
    original_create = store.create
    original_update = store.update

    def slow_create(session):
        time.sleep(0.05)  # widen the overlap window an unlocked race would lose
        return original_create(session)

    def slow_update(session):
        time.sleep(0.05)
        return original_update(session)

    store.create = slow_create  # type: ignore[method-assign]
    store.update = slow_update  # type: ignore[method-assign]
    router = ChatRouter(_FakeEngine(store), _StubManager())

    await asyncio.gather(
        router._persist_router_turn_off_loop(_cli_incoming("first"), "reply-1"),
        router._persist_router_turn_off_loop(_cli_incoming("second"), "reply-2"),
    )

    session = store.get("cli", "cli-test", "cli-test")
    assert session is not None
    assert session.message_count == 2  # neither increment lost
    assert len(store.list_messages(_KEY)) == 4  # 2 user + 2 assistant rows


@pytest.mark.asyncio
async def test_engine_vs_router_shield_race_update_path(tmp_path) -> None:
    """Engine persist merges onto an interleaved router bump (fresh re-read),
    not the stale turn-start snapshot — the timeout-shield update path."""
    store = SQLiteSessionStore(tmp_path / "chat.db")
    now = datetime.now()
    # Engine turn-start snapshot: the row exists with count == 1.
    store.create(
        Session(
            session_id=_KEY,
            agent_session_id="",
            platform="cli",
            channel_id="cli-test",
            thread_id="cli-test",
            user_id="cli-user",
            created_at=now,
            updated_at=now,
            message_count=1,
        )
    )
    # While the LLM ran, a router persist bumped the row to count == 2.
    bumped = store.get("cli", "cli-test", "cli-test")
    bumped.message_count += 1
    store.update(bumped)

    convo = _engine_with_store(store)
    action = convo._persist_engine_turn(
        session_key=_KEY,
        platform_str="cli",
        channel_id="cli-test",
        thread_id="cli-test",
        message=_cli_incoming("shielded prompt"),
        response_text="engine reply",
        persisted_runtime_session_id="sdk-1",
        normalized_tool_calls=[],
        result=_fake_result(),
        cost_usd=0.0,
        mode="execute",
        now=datetime.now(),
    )

    assert action == "update"
    # Stale snapshot (count 1) would have written 2 and LOST the router bump;
    # the fresh re-read (count 2) writes 3.
    assert store.get("cli", "cli-test", "cli-test").message_count == 3


@pytest.mark.asyncio
async def test_engine_vs_router_shield_race_create_path(tmp_path) -> None:
    """No row at engine turn start; a router persist created it meanwhile. The
    fresh re-read updates instead of colliding on the UNIQUE session_id."""
    store = SQLiteSessionStore(tmp_path / "chat.db")
    now = datetime.now()
    # Router created the row while the engine's snapshot was still None.
    store.create(
        Session(
            session_id=_KEY,
            agent_session_id="",
            platform="cli",
            channel_id="cli-test",
            thread_id="cli-test",
            user_id="cli-user",
            created_at=now,
            updated_at=now,
            message_count=1,
        )
    )

    convo = _engine_with_store(store)
    # 605a93fe's shape (existing=None → create) would raise UNIQUE IntegrityError.
    action = convo._persist_engine_turn(
        session_key=_KEY,
        platform_str="cli",
        channel_id="cli-test",
        thread_id="cli-test",
        message=_cli_incoming("shielded prompt"),
        response_text="engine reply",
        persisted_runtime_session_id="sdk-1",
        normalized_tool_calls=[],
        result=_fake_result(),
        cost_usd=0.0,
        mode="execute",
        now=datetime.now(),
    )

    assert action == "update"
    assert store.get("cli", "cli-test", "cli-test").message_count == 2


def test_get_persist_lock_is_stable_per_key() -> None:
    assert get_persist_lock(_KEY) is get_persist_lock(_KEY)
    assert get_persist_lock("cli:a:a") is not get_persist_lock("cli:b:b")


@pytest.mark.asyncio
async def test_router_wrapper_locks_on_canonical_session_key(
    tmp_path, monkeypatch
) -> None:
    """The router wrapper acquires the lock keyed by exactly the engine's
    canonical session key — proving one key space across paths (gate req 2)."""
    requested: list[str] = []
    shared = asyncio.Lock()

    def spy_get_persist_lock(key: str) -> asyncio.Lock:
        requested.append(key)
        return shared

    monkeypatch.setattr(router_module, "get_persist_lock", spy_get_persist_lock)
    store = SQLiteSessionStore(tmp_path / "chat.db")
    router = ChatRouter(_FakeEngine(store), _StubManager())

    await router._persist_router_turn_off_loop(_cli_incoming("hi"), "reply")

    engine_key = build_session_key("cli", "cli-test", resolve_thread_id("cli-test", None))
    assert requested == [engine_key]
