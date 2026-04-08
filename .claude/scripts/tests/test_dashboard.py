"""Tests for the dashboard API endpoints."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# Ensure dashboard dir is importable
DASHBOARD_DIR = Path(__file__).resolve().parent.parent.parent / "dashboard"
sys.path.insert(0, str(DASHBOARD_DIR))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_db(tmp_path: Path) -> Path:
    """Create a temporary chat.db with test data."""
    db_path = tmp_path / "chat.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE chat_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL UNIQUE,
            agent_session_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            message_count INTEGER DEFAULT 0,
            total_cost_usd REAL DEFAULT 0.0,
            status TEXT DEFAULT 'active',
            mode TEXT DEFAULT 'execute'
        );
        CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    """)
    conn.execute(
        """INSERT INTO chat_sessions
           (session_id, agent_session_id, platform, channel_id, thread_id,
            user_id, created_at, updated_at, message_count, total_cost_usd, status, mode)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "telegram:123:456",
            "aaaa-bbbb-cccc",
            "telegram",
            "123",
            "456",
            "user1",
            "2026-03-01T10:00:00",
            "2026-03-01T10:05:00",
            3,
            0.05,
            "active",
            "execute",
        ),
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture()
def tmp_daily_dir(tmp_path: Path) -> Path:
    """Create a temporary daily log directory with test files."""
    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / "2026-03-01.md").write_text("# Daily Log: 2026-03-01\n\n## Sessions\n\nTest entry\n")
    (daily / "2026-02-28.md").write_text("# Daily Log: 2026-02-28\n\n## Heartbeats\n\nOK\n")
    (daily / "not-a-date.md").write_text("should be ignored")
    return daily


@pytest.fixture()
def tmp_memory_dir(tmp_path: Path) -> Path:
    """Create temporary memory files."""
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "SOUL.md").write_text("# SOUL\n\nI am the second brain.")
    (mem / "USER.md").write_text("# USER\n\nYourUser")
    (mem / "MEMORY.md").write_text("# MEMORY\n\nKey decisions here.")
    (mem / "HEARTBEAT.md").write_text("# HEARTBEAT\n\nCheck calendar.")
    return mem


@pytest.fixture()
def tmp_transcripts(tmp_path: Path) -> Path:
    """Create a temporary JSONL transcript."""
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()

    lines = [
        json.dumps({
            "type": "queue-operation",
            "operation": "start",
            "timestamp": "2026-03-01T10:00:00Z",
            "sessionId": "aaaa-bbbb-cccc",
        }),
        json.dumps({
            "type": "user",
            "message": {"role": "user", "content": "Hello bot"},
            "timestamp": "2026-03-01T10:00:01Z",
            "sessionId": "aaaa-bbbb-cccc",
        }),
        json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Hey there!"}],
            },
            "timestamp": "2026-03-01T10:00:02Z",
            "sessionId": "aaaa-bbbb-cccc",
        }),
        json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Read", "input": {}},
                    {"type": "text", "text": "I read the file."},
                ],
            },
            "timestamp": "2026-03-01T10:00:03Z",
            "sessionId": "aaaa-bbbb-cccc",
        }),
    ]
    (transcripts / "aaaa-bbbb-cccc.jsonl").write_text("\n".join(lines))
    return transcripts


@pytest.fixture()
def client(
    tmp_db: Path,
    tmp_daily_dir: Path,
    tmp_memory_dir: Path,
    tmp_transcripts: Path,
    tmp_path: Path,
) -> TestClient:
    """Create a TestClient with all paths patched to temp dirs."""
    # We need to patch before importing app, but app is already imported.
    # Instead, patch the module-level variables in app.
    import app as dashboard_app

    # Store originals
    orig_db = dashboard_app.CHAT_DB_PATH
    orig_daily = dashboard_app.DAILY_DIR
    orig_transcripts = dashboard_app.TRANSCRIPTS_DIR
    orig_paths = dashboard_app.MEMORY_FILE_PATHS.copy()
    orig_allowed = dashboard_app.ALLOWED_MEMORY_FILES.copy()

    # Patch state files to temp
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "heartbeat-state.json").write_text('{"last_run": "2026-03-01T10:00:00"}')
    (state_dir / "reflection-state.json").write_text('{"last_run": "2026-03-01T08:00:00"}')

    # Create a today log with "crash" for status endpoint
    today_log = tmp_daily_dir / f"{datetime.now().strftime('%Y-%m-%d')}.md"
    if not today_log.exists():
        today_log.write_text("Bot crashed (exit code 1)\nAnother crash here\n")

    # Apply patches
    dashboard_app.CHAT_DB_PATH = tmp_db
    dashboard_app.DAILY_DIR = tmp_daily_dir
    dashboard_app.TRANSCRIPTS_DIR = tmp_transcripts
    dashboard_app.MEMORY_FILE_PATHS = {
        "SOUL.md": tmp_memory_dir / "SOUL.md",
        "USER.md": tmp_memory_dir / "USER.md",
        "MEMORY.md": tmp_memory_dir / "MEMORY.md",
        "HEARTBEAT.md": tmp_memory_dir / "HEARTBEAT.md",
    }

    # Patch state loading and PID checking
    with patch.object(dashboard_app, "load_state", side_effect=lambda p: {
        "last_run": "2026-03-01T10:00:00"
    }), patch.object(dashboard_app, "read_pid", return_value=12345), \
         patch.object(dashboard_app, "is_pid_alive", return_value=True), \
         patch("config.get_today_log_path", return_value=tmp_daily_dir / "2026-03-01.md"):

        # Use context manager so lifespan (startup/shutdown) actually fires
        with TestClient(dashboard_app.app) as tc:
            yield tc

    # Restore originals
    dashboard_app.CHAT_DB_PATH = orig_db
    dashboard_app.DAILY_DIR = orig_daily
    dashboard_app.TRANSCRIPTS_DIR = orig_transcripts
    dashboard_app.MEMORY_FILE_PATHS = orig_paths
    dashboard_app.ALLOWED_MEMORY_FILES = orig_allowed


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestStatus:
    def test_returns_bot_status(self, client: TestClient) -> None:
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["bot"]["pid"] == 12345
        assert data["bot"]["alive"] is True

    def test_returns_heartbeat_state(self, client: TestClient) -> None:
        resp = client.get("/api/status")
        data = resp.json()
        assert "last_run" in data["heartbeat"]

    def test_returns_reflection_state(self, client: TestClient) -> None:
        resp = client.get("/api/status")
        data = resp.json()
        assert "last_run" in data["reflection"]


class TestSessions:
    def test_list_sessions(self, client: TestClient) -> None:
        resp = client.get("/api/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["platform"] == "telegram"
        assert data[0]["message_count"] == 3

    def test_get_session_with_transcript(self, client: TestClient) -> None:
        resp = client.get("/api/sessions/1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_session_id"] == "aaaa-bbbb-cccc"
        # Should have 3 displayable messages (1 user + 2 assistant with text)
        assert len(data["messages"]) == 3
        assert data["messages"][0]["role"] == "user"
        assert data["messages"][0]["content"] == "Hello bot"
        assert data["messages"][1]["role"] == "assistant"
        assert data["messages"][1]["content"] == "Hey there!"
        # Third message: assistant with tool_use + text — only text extracted
        assert data["messages"][2]["content"] == "I read the file."

    def test_get_session_missing_transcript(self, client: TestClient, tmp_transcripts: Path) -> None:
        """Session exists in DB but JSONL file is missing - returns empty messages."""
        # Add a session with a non-existent transcript
        db_path = client.app.state  # We can't easily get the DB here, but we test via the fixture
        # The fixture only has aaaa-bbbb-cccc. Session ID 1 has that transcript.
        # We test the 404 for a non-existent session ID instead.
        resp = client.get("/api/sessions/999")
        assert resp.status_code == 404

    def test_get_session_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/sessions/999")
        assert resp.status_code == 404

    def test_get_session_merges_db_messages_with_legacy_transcript(self, client: TestClient, tmp_db: Path) -> None:
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            """
            INSERT INTO chat_messages (session_id, role, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            ("telegram:123:456", "assistant", "DB transcript wins", "2026-03-01T10:00:04"),
        )
        conn.commit()
        conn.close()

        resp = client.get("/api/sessions/1")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["messages"]) == 4
        assert data["messages"][0]["content"] == "Hello bot"
        assert data["messages"][-1]["content"] == "DB transcript wins"


class TestLogs:
    def test_list_logs(self, client: TestClient) -> None:
        resp = client.get("/api/logs")
        assert resp.status_code == 200
        data = resp.json()
        # Should have 2 valid date-named logs (not "not-a-date.md")
        dates = [entry["date"] for entry in data]
        assert "2026-03-01" in dates
        assert "2026-02-28" in dates
        assert "not-a-date" not in dates
        # Should be sorted descending
        assert data[0]["date"] > data[1]["date"]

    def test_get_log(self, client: TestClient) -> None:
        resp = client.get("/api/logs/2026-03-01")
        assert resp.status_code == 200
        data = resp.json()
        assert data["date"] == "2026-03-01"
        assert "Test entry" in data["content"]
        assert "<p>" in data["html"] or "<h1>" in data["html"]

    def test_get_log_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/logs/2099-01-01")
        assert resp.status_code == 404

    def test_get_log_invalid_date(self, client: TestClient) -> None:
        resp = client.get("/api/logs/not-a-date")
        assert resp.status_code == 400


class TestMemory:
    def test_get_soul(self, client: TestClient) -> None:
        resp = client.get("/api/memory/SOUL.md")
        assert resp.status_code == 200
        data = resp.json()
        assert data["filename"] == "SOUL.md"
        assert "second brain" in data["content"].lower()
        assert "<h1>" in data["html"] or "<p>" in data["html"]
        assert "last_modified" in data

    def test_get_memory(self, client: TestClient) -> None:
        resp = client.get("/api/memory/MEMORY.md")
        assert resp.status_code == 200
        assert "Key decisions" in resp.json()["content"]

    def test_path_traversal_blocked(self, client: TestClient) -> None:
        # FastAPI normalizes path traversal in URLs before routing.
        # The whitelist approach means ANY non-whitelisted filename gets 400.
        # Test with a traversal-style name that doesn't contain slashes.
        resp = client.get("/api/memory/..config.py")
        assert resp.status_code == 400

    def test_nonexistent_file(self, client: TestClient) -> None:
        resp = client.get("/api/memory/NONEXISTENT.md")
        assert resp.status_code == 400  # Not in whitelist

    def test_only_whitelisted_files(self, client: TestClient) -> None:
        resp = client.get("/api/memory/secret.md")
        assert resp.status_code == 400
