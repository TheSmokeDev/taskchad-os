from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

import core_handlers
import session_lifecycle_hooks as lifecycle
from models import Channel, IncomingMessage, Platform, User
from session import Session, SQLiteSessionStore
from session_keys import build_session_key


def _seed_session(store: SQLiteSessionStore) -> Session:
    now = datetime.now()
    session_id = build_session_key("cli", "chan-1", "chan-1")
    session = Session(
        session_id=session_id,
        agent_session_id="runtime-1",
        platform="cli",
        channel_id="chan-1",
        thread_id="chan-1",
        user_id="user-1",
        created_at=now,
        updated_at=now,
        message_count=2,
        runtime_lane="generic",
        runtime_provider="codex",
        runtime_model="gpt-5",
    )
    store.create(session)
    store.add_message(session_id, "user", "keep this before clear", now)
    store.add_message(session_id, "assistant", "saved before clear", now)
    return session


def test_clear_lifecycle_order_persists_hooks_delete_then_identity_reload(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    session = _seed_session(store)
    order: list[str] = []

    monkeypatch.setattr(lifecycle, "get_state_dir", lambda: tmp_path / "state")
    original_write = lifecycle.write_clear_transcript

    def recording_write(**kwargs):
        order.append("persist_transcript")
        return original_write(**kwargs)

    def fake_hook(hook_name, payload, *, timeout_seconds=15.0):
        order.append(hook_name)
        assert payload["source"] == "clear"
        assert payload["session_id"] == session.session_id
        assert payload["transcript_path"]
        return lifecycle.HookInvocation(
            hook_name=hook_name,
            returncode=0,
            stdout_chars=42 if hook_name == "session-start-context.py" else 0,
        )

    original_delete = store.delete

    def recording_delete(platform, channel_id, thread_id):
        order.append("session_delete")
        return original_delete(platform, channel_id, thread_id)

    class Engine:
        def reload_soul_context(self) -> None:
            order.append("identity_reload")

    monkeypatch.setattr(lifecycle, "write_clear_transcript", recording_write)
    monkeypatch.setattr(lifecycle, "run_hook_script", fake_hook)
    monkeypatch.setattr(store, "delete", recording_delete)

    result = lifecycle.clear_session_with_lifecycle(
        store=store,
        session=session,
        platform="cli",
        channel_id="chan-1",
        thread_id="chan-1",
        engine=Engine(),
    )

    assert order == [
        "persist_transcript",
        "session-end-flush.py",
        "session-start-context.py",
        "session_delete",
        "identity_reload",
    ]
    assert store.get("cli", "chan-1", "chan-1") is None
    assert result.transcript_path is not None
    rows = [
        json.loads(line)
        for line in result.transcript_path.read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["type"] == "session_signal"
    assert rows[0]["event"] == "clear"
    assert [row["message"]["role"] for row in rows[1:]] == ["user", "assistant"]
    assert [row["message"]["content"] for row in rows[1:]] == [
        "keep this before clear",
        "saved before clear",
    ]


def test_clear_lifecycle_hook_failure_still_deletes_and_reports_warning(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    session = _seed_session(store)
    hooks_seen: list[str] = []

    monkeypatch.setattr(lifecycle, "get_state_dir", lambda: tmp_path / "state")

    def fake_hook(hook_name, payload, *, timeout_seconds=15.0):
        hooks_seen.append(hook_name)
        if hook_name == "session-end-flush.py":
            raise RuntimeError("flush hook failed")
        return lifecycle.HookInvocation(hook_name=hook_name, returncode=0)

    monkeypatch.setattr(lifecycle, "run_hook_script", fake_hook)

    result = lifecycle.clear_session_with_lifecycle(
        store=store,
        session=session,
        platform="cli",
        channel_id="chan-1",
        thread_id="chan-1",
        engine=None,
    )

    assert hooks_seen == ["session-end-flush.py", "session-start-context.py"]
    assert store.get("cli", "chan-1", "chan-1") is None
    assert "session-end-flush.py" in result.warning_summary()
    assert "flush hook failed" in result.warning_summary()


@pytest.mark.asyncio
async def test_handle_clear_surfaces_lifecycle_warning(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    _seed_session(store)
    monkeypatch.setattr(lifecycle, "get_state_dir", lambda: tmp_path / "state")

    def fake_hook(hook_name, payload, *, timeout_seconds=15.0):
        if hook_name == "session-end-flush.py":
            raise RuntimeError("flush hook failed")
        return lifecycle.HookInvocation(hook_name=hook_name, returncode=0)

    class Engine:
        session_store = store

        def reload_soul_context(self) -> None:
            return None

    monkeypatch.setattr(lifecycle, "run_hook_script", fake_hook)
    core_handlers.set_context(engine=Engine(), adapters={}, bot_start_time=datetime.now())
    incoming = IncomingMessage(
        text="/clear",
        user=User(Platform.CLI, "user-1", "User"),
        channel=Channel(Platform.CLI, "chan-1", is_dm=True),
        platform=Platform.CLI,
        timestamp=datetime.now(),
    )

    response = await core_handlers.handle_clear(None, incoming, "")

    assert response.startswith("Session cleared. Next message starts fresh.")
    assert "Lifecycle warning:" in response
    assert "session-end-flush.py" in response
    assert store.get("cli", "chan-1", "chan-1") is None


# =============================================================================
# Living Mind Act 4 — trigger_source plumbing (R1 B1) + clear-seam brief-owed
# marker capture (R1 B4). All state lands in tmp via config.STATE_DIR
# monkeypatch (call-time resolution — Rule 1 in action).
# =============================================================================


def _seed_old_session(store: SQLiteSessionStore, hours_ago: float = 10.0) -> Session:
    old = datetime.now() - timedelta(hours=hours_ago)
    session_id = build_session_key("cli", "chan-1", "chan-1")
    session = Session(
        session_id=session_id,
        agent_session_id="",
        platform="cli",
        channel_id="chan-1",
        thread_id="chan-1",
        user_id="1111111111",
        created_at=old,
        updated_at=old,
        message_count=2,
    )
    store.create(session)
    store.add_message(session_id, "user", "before clear", old)
    store.add_message(session_id, "assistant", "saved", old)
    return session


def _act4_clear_setup(tmp_path, monkeypatch):
    """tmp STATE_DIR + hook stubs + a real-engine-shaped context object."""
    import config
    import engine as engine_module

    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    monkeypatch.setattr(lifecycle, "get_state_dir", lambda: state_dir)
    for var in ("SESSION_BRIEF_ENABLED", "SESSION_BRIEF_AWAY_HOURS"):
        monkeypatch.delenv(var, raising=False)

    def fake_hook(hook_name, payload, *, timeout_seconds=15.0):
        return lifecycle.HookInvocation(hook_name=hook_name, returncode=0)

    monkeypatch.setattr(lifecycle, "run_hook_script", fake_hook)

    store = SQLiteSessionStore(tmp_path / "chat.db")
    # ConversationEngine without __init__: handle_clear only needs
    # session_store + note_router_activity + reload_soul_context — no live
    # identity reads in tests.
    engine = engine_module.ConversationEngine.__new__(
        engine_module.ConversationEngine
    )
    engine.session_store = store
    engine._session_brief_fired_at = None
    engine.reload_soul_context = lambda: None  # type: ignore[method-assign]
    core_handlers.set_context(
        engine=engine, adapters={}, bot_start_time=datetime.now()
    )
    return store, state_dir


def _clear_incoming(source: str = "interactive") -> IncomingMessage:
    return IncomingMessage(
        text="/clear",
        user=User(Platform.CLI, "1111111111", "User"),
        channel=Channel(Platform.CLI, "chan-1", is_dm=True),
        platform=Platform.CLI,
        timestamp=datetime.now(),
        source=source,
    )


def _read_event_rows(state_dir) -> list[dict]:
    path = state_dir / "clear-lifecycle-events.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.mark.asyncio
async def test_interactive_clear_writes_trigger_source_and_captures_marker(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator /clear after an away gap: the brief-owed marker is
    captured BEFORE the clear event closes the gap, and the event row
    carries trigger_source=interactive."""
    store, state_dir = _act4_clear_setup(tmp_path, monkeypatch)
    session = _seed_old_session(store, hours_ago=10)
    seeded_updated_at = session.updated_at

    response = await core_handlers.handle_clear(None, _clear_incoming(), "")

    assert response.startswith("Session cleared.")
    rows = _read_event_rows(state_dir)
    assert len(rows) == 1
    assert rows[0]["trigger_source"] == "interactive"
    # Marker captured pre-lifecycle, carrying the PRE-clear boundary.
    marker = state_dir / "session-brief-owed.json"
    assert marker.exists()
    boundary = datetime.fromisoformat(
        json.loads(marker.read_text(encoding="utf-8"))["last_activity"]
    )
    assert boundary == seeded_updated_at
    # The clear event itself is NEWER than the marker boundary — exactly the
    # gap-closing write the marker had to beat.
    event_ts = datetime.fromisoformat(rows[0]["timestamp"])
    assert event_ts > boundary


@pytest.mark.asyncio
async def test_cron_clear_writes_cron_trigger_source_and_no_marker(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cron-sourced /clear must not pretend to be operator presence: the
    row records trigger_source=cron and no marker is written."""
    store, state_dir = _act4_clear_setup(tmp_path, monkeypatch)
    _seed_old_session(store, hours_ago=10)

    response = await core_handlers.handle_clear(
        None, _clear_incoming(source="cron"), ""
    )

    assert response.startswith("Session cleared.")
    rows = _read_event_rows(state_dir)
    assert len(rows) == 1
    assert rows[0]["trigger_source"] == "cron"
    assert not (state_dir / "session-brief-owed.json").exists()


def test_clear_lifecycle_default_trigger_source_is_interactive(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy callers that never pass trigger_source keep working and write
    the interactive default (additive contract)."""
    monkeypatch.setattr(lifecycle, "get_state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(
        lifecycle,
        "run_hook_script",
        lambda name, payload, *, timeout_seconds=15.0: lifecycle.HookInvocation(
            hook_name=name, returncode=0
        ),
    )
    store = SQLiteSessionStore(tmp_path / "chat.db")
    session = _seed_session(store)

    result = lifecycle.clear_session_with_lifecycle(
        store=store,
        session=session,
        platform="cli",
        channel_id="chan-1",
        thread_id="chan-1",
        engine=None,
    )

    assert result.trigger_source == "interactive"
    rows = _read_event_rows(tmp_path / "state")
    assert rows[0]["trigger_source"] == "interactive"
    # Existing keys untouched (additive contract).
    assert {"timestamp", "session_id", "transcript_path", "events"} <= set(
        rows[0].keys()
    )


@pytest.mark.asyncio
async def test_reload_still_refreshes_identity_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Store:
        def delete(self, platform, channel_id, thread_id):
            return True

    class Existing:
        pass

    class Engine:
        max_turns = 0
        max_budget_usd = 0.0

        def __init__(self) -> None:
            self.reloaded = False

        def reload_soul_context(self) -> None:
            self.reloaded = True

    import config

    engine = Engine()
    monkeypatch.setattr(config, "reload_config", lambda: {})
    monkeypatch.setattr(
        core_handlers,
        "_get_session",
        lambda incoming: (Store(), Existing(), "cli", "chan-1", "chan-1"),
    )
    core_handlers.set_context(engine=engine, adapters={}, bot_start_time=datetime.now())

    response = await core_handlers.handle_reload(None, object(), "")

    assert engine.reloaded is True
    assert "Soul context reloaded" in response
    assert "Session cleared" in response
