"""Homie Mobile M8 — sessions browser endpoints (read-only, admin-classified).

GET /api/sessions          — recent sessions, hidden tool/hook sources excluded
GET /api/sessions/search   — FTS5 content search across all conversations
GET /api/sessions/messages — transcript for ANY session id
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime

import pytest


@pytest.fixture
def dash_client(tmp_path, monkeypatch):
    """Isolated app + a seeded chat.db (three sessions, one hidden-source)."""
    from fastapi.testclient import TestClient

    import config

    chat_db = tmp_path / "chat.db"
    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", tmp_path / "dashboard.db")
    monkeypatch.setattr(config, "CHAT_DB_PATH", chat_db)
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(config, "ORCHESTRATION_DB_PATH", tmp_path / "orch.db")
    monkeypatch.setenv("ORCHESTRATION_API_TOKEN", "")

    # Seed through the real store so schema + FTS triggers apply.
    from session import Session, SQLiteSessionStore

    store = SQLiteSessionStore(chat_db)
    now = datetime.now()

    def _mk(session_id: str, platform: str, source: str, persona_id: str | None = None) -> None:
        parts = session_id.split(":")
        store.create(
            Session(
                session_id=session_id,
                agent_session_id="",
                platform=platform,
                channel_id=parts[1],
                thread_id=parts[2] if len(parts) > 2 else parts[1],
                user_id="user-1",
                created_at=now,
                updated_at=now,
                message_count=2,
                source=source,
                persona_id=persona_id,
            )
        )

    _mk("web:dashboard-main:dashboard-main", "web", "interactive")
    _mk("web:dashboard-sales:dashboard-sales", "web", "interactive", persona_id="sales")
    _mk("telegram:chat-9:chat-9", "telegram", "interactive")
    _mk("cli:hook-run:hook-run", "cli", "hook")  # hidden source — must not list

    store.add_message("web:dashboard-main:dashboard-main", "user", "find the tenant isolation notes")
    store.add_message(
        "web:dashboard-main:dashboard-main",
        "assistant",
        "The zanzibar-flamingo keyword lives here for FTS.",
        tool_calls=[{"id": "t1", "name": "Read", "arguments": {"path": "a.md"}}],
    )
    store.add_message("telegram:chat-9:chat-9", "user", "totally unrelated telegram turn")

    import orchestration.api as oa

    importlib.reload(oa)
    db, cs, ms, reg, ts = oa._get_services()
    oa._db = db
    oa._convoy_svc = cs
    oa._mailbox_svc = ms
    oa._executor_registry = reg
    oa._team_svc = ts
    yield TestClient(oa.app)
    db.close()


def test_sessions_list_returns_rows_with_preview_and_persona(dash_client) -> None:
    r = dash_client.get("/api/sessions")
    assert r.status_code == 200
    sessions = r.json()["sessions"]
    ids = [s["session_id"] for s in sessions]
    assert "web:dashboard-main:dashboard-main" in ids
    assert "web:dashboard-sales:dashboard-sales" in ids
    assert "telegram:chat-9:chat-9" in ids
    main = next(s for s in sessions if s["session_id"] == "web:dashboard-main:dashboard-main")
    assert "zanzibar-flamingo" in main["preview"]
    sales = next(s for s in sessions if s["session_id"] == "web:dashboard-sales:dashboard-sales")
    assert sales["persona_id"] == "sales"


def test_sessions_list_excludes_hidden_sources(dash_client) -> None:
    r = dash_client.get("/api/sessions")
    ids = [s["session_id"] for s in r.json()["sessions"]]
    assert "cli:hook-run:hook-run" not in ids


def test_sessions_list_platform_filter(dash_client) -> None:
    r = dash_client.get("/api/sessions?platform=telegram")
    sessions = r.json()["sessions"]
    assert sessions and all(s["platform"] == "telegram" for s in sessions)


def test_sessions_search_finds_content_across_sessions(dash_client) -> None:
    r = dash_client.get("/api/sessions/search?q=zanzibar-flamingo")
    assert r.status_code == 200
    hits = r.json()["hits"]
    assert len(hits) == 1
    assert hits[0]["session_id"] == "web:dashboard-main:dashboard-main"
    assert "zanzibar-flamingo" in hits[0]["snippet"]
    assert hits[0]["role"] == "assistant"


def test_sessions_search_no_hits(dash_client) -> None:
    r = dash_client.get("/api/sessions/search?q=nonexistentwordxyz")
    assert r.status_code == 200
    assert r.json()["hits"] == []


def test_sessions_messages_returns_transcript_with_tool_calls(dash_client) -> None:
    r = dash_client.get(
        "/api/sessions/messages?session_id=web:dashboard-main:dashboard-main"
    )
    assert r.status_code == 200
    messages = r.json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    tool_calls = json.loads(messages[1]["tool_calls_json"])
    assert tool_calls and tool_calls[0]["name"] == "Read"


def test_sessions_messages_unknown_session_is_empty(dash_client) -> None:
    r = dash_client.get("/api/sessions/messages?session_id=web:nope:nope")
    assert r.status_code == 200
    assert r.json()["messages"] == []
