from __future__ import annotations

import json
from datetime import datetime

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
