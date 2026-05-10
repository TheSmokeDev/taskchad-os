"""SQLite persistence for orchestration — schema, CRUD, row mapping.

Uses stdlib sqlite3 only. No external dependencies.
DB path default: .claude/data/orchestration.db (from config.ORCHESTRATION_DB_PATH).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from orchestration.models import (
    AgentDelivery,
    AgentMessage,
    Attempt,
    Convoy,
    DependencyEdge,
    Subtask,
    TeamMember,
    TeamSession,
)

# ── Schema DDL ─────────────────────────────────────────────────────────────
# Parity: mission-control/src/lib/migrations.ts migration 050_convoy_mode

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS convoys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL DEFAULT 1,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'active', 'paused', 'completed', 'failed', 'cancelled')),
    decomposition_mode TEXT NOT NULL DEFAULT 'manual'
        CHECK (decomposition_mode IN ('manual', 'ai_assisted')),
    created_by TEXT NOT NULL,
    base_branch TEXT NOT NULL DEFAULT 'main',
    repo_path TEXT,
    merge_strategy TEXT NOT NULL DEFAULT 'squash'
        CHECK (merge_strategy IN ('squash', 'merge', 'rebase')),
    total_subtasks INTEGER DEFAULT 0,
    completed_subtasks INTEGER DEFAULT 0,
    failed_subtasks INTEGER DEFAULT 0,
    started_at INTEGER,
    completed_at INTEGER,
    metadata TEXT,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_convoys_status ON convoys(workspace_id, status);

CREATE TABLE IF NOT EXISTS subtasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    convoy_id INTEGER NOT NULL REFERENCES convoys(id) ON DELETE CASCADE,
    workspace_id INTEGER NOT NULL DEFAULT 1,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'ready', 'dispatched', 'running',
                          'completed', 'failed', 'cancelled', 'stalled')),
    assigned_agent_id TEXT,
    assigned_agent_name TEXT,
    paperclip_issue_id TEXT,
    remaining_dependencies INTEGER NOT NULL DEFAULT 0,
    port_allocated INTEGER,
    worktree_path TEXT,
    worktree_branch TEXT,
    merge_commit TEXT,
    error_message TEXT,
    stall_detected_at INTEGER,
    dispatched_at INTEGER,
    started_at INTEGER,
    completed_at INTEGER,
    seq INTEGER NOT NULL DEFAULT 0,
    metadata TEXT,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_subtasks_convoy ON subtasks(convoy_id);
CREATE INDEX IF NOT EXISTS idx_subtasks_status ON subtasks(convoy_id, status);
CREATE INDEX IF NOT EXISTS idx_subtasks_paperclip ON subtasks(paperclip_issue_id);

CREATE TABLE IF NOT EXISTS dependency_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL DEFAULT 1,
    convoy_id INTEGER NOT NULL REFERENCES convoys(id) ON DELETE CASCADE,
    from_subtask_id INTEGER NOT NULL REFERENCES subtasks(id) ON DELETE CASCADE,
    to_subtask_id INTEGER NOT NULL REFERENCES subtasks(id) ON DELETE CASCADE,
    UNIQUE (from_subtask_id, to_subtask_id)
);
CREATE INDEX IF NOT EXISTS idx_edges_convoy ON dependency_edges(workspace_id, convoy_id);
CREATE INDEX IF NOT EXISTS idx_edges_to ON dependency_edges(to_subtask_id);

CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL DEFAULT 1,
    convoy_id INTEGER NOT NULL REFERENCES convoys(id) ON DELETE CASCADE,
    subtask_id INTEGER NOT NULL REFERENCES subtasks(id) ON DELETE CASCADE,
    attempt_key TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL CHECK (action IN ('dispatch', 'cancel', 'nudge')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sent', 'acked', 'failed', 'expired')),
    paperclip_issue_id TEXT,
    error_message TEXT,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_attempts_key ON attempts(workspace_id, attempt_key);

CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL DEFAULT 1,
    convoy_id INTEGER REFERENCES convoys(id) ON DELETE CASCADE,
    thread_id INTEGER,
    correlation_id TEXT,
    causation_id TEXT,
    reply_to_message_id INTEGER,
    from_agent TEXT NOT NULL,
    message_type TEXT NOT NULL DEFAULT 'message'
        CHECK (message_type IN ('command', 'approval_request', 'clarification',
                                'exception', 'handoff', 'interrupt', 'cancel',
                                'result', 'status', 'message')),
    subject TEXT,
    body TEXT NOT NULL,
    artifact_refs TEXT,
    dedupe_key TEXT UNIQUE,
    msg_type TEXT,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_agent_messages_convoy ON agent_messages(workspace_id, convoy_id, created_at DESC);
-- idx_agent_messages_msg_type is created AFTER `_ensure_column` runs in
-- `_migrate()` so older DBs that pre-date the msg_type column don't crash
-- during initial migration. CREATE INDEX must follow column-add ordering.

CREATE TABLE IF NOT EXISTS agent_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL DEFAULT 1,
    message_id INTEGER NOT NULL REFERENCES agent_messages(id) ON DELETE CASCADE,
    recipient_agent TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'seen', 'claimed', 'acked', 'nacked', 'expired', 'dead_lettered')),
    claim_token TEXT,
    claimed_at INTEGER,
    acked_at INTEGER,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_agent_deliveries_recipient ON agent_deliveries(workspace_id, recipient_agent, status);
CREATE INDEX IF NOT EXISTS idx_agent_deliveries_message ON agent_deliveries(message_id);

CREATE TABLE IF NOT EXISTS callback_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    workspace_id INTEGER NOT NULL DEFAULT 1,
    convoy_id INTEGER NOT NULL,
    subtask_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    processed_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_callback_receipts_subtask
    ON callback_receipts(convoy_id, subtask_id);

CREATE TABLE IF NOT EXISTS team_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL DEFAULT 1,
    convoy_id INTEGER REFERENCES convoys(id) ON DELETE SET NULL,
    team_name TEXT NOT NULL,
    lead_agent_id TEXT NOT NULL,
    lead_agent_name TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'idle', 'shutdown_requested', 'closed')),
    backend_type TEXT NOT NULL DEFAULT 'local'
        CHECK (backend_type IN ('local', 'paperclip', 'workflow', 'auto')),
    last_activity_at INTEGER,
    shutdown_requested_at INTEGER,
    closed_at INTEGER,
    metadata TEXT,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_team_sessions_status ON team_sessions(workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_team_sessions_convoy ON team_sessions(convoy_id);

CREATE TABLE IF NOT EXISTS team_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL DEFAULT 1,
    team_session_id INTEGER NOT NULL REFERENCES team_sessions(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    agent_name TEXT,
    role TEXT NOT NULL DEFAULT 'worker'
        CHECK (role IN ('leader', 'worker')),
    subtask_id INTEGER REFERENCES subtasks(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'idle', 'closed')),
    joined_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    last_activity_at INTEGER,
    UNIQUE (team_session_id, agent_id)
);
CREATE INDEX IF NOT EXISTS idx_team_members_session ON team_members(team_session_id);
CREATE INDEX IF NOT EXISTS idx_team_members_agent ON team_members(agent_id);
"""


class OrchestrationDB:
    """Thin SQLite wrapper for orchestration persistence."""

    def __init__(self, db_path: str | Path = ":memory:", check_same_thread: bool = True):
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=check_same_thread)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        for statement in _SCHEMA_SQL.split(";"):
            stmt = statement.strip()
            if stmt:
                self.conn.execute(stmt)
        # Backwards-compat ALTERs for DBs created before a column was added.
        # SQLite < 3.37 lacks `ADD COLUMN IF NOT EXISTS`, so we inspect
        # table_info first and only ALTER when the column is missing.
        self._ensure_column("agent_messages", "msg_type", "TEXT")
        # Indexes that depend on backwards-compat-added columns must run
        # AFTER the column-ensure step (older DBs would crash with
        # "no such column" otherwise).
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_messages_msg_type "
            "ON agent_messages(convoy_id, msg_type)"
        )
        self.conn.commit()

    def _ensure_column(self, table: str, column: str, decl: str) -> None:
        """Add `column` to `table` if it does not already exist."""
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {r[1] for r in rows}
        if column not in existing:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def close(self) -> None:
        self.conn.close()

    # ── Row mappers ────────────────────────────────────────────────────────

    @staticmethod
    def row_to_convoy(row: sqlite3.Row) -> Convoy:
        return Convoy(**{k: row[k] for k in row.keys()})

    @staticmethod
    def row_to_subtask(row: sqlite3.Row) -> Subtask:
        return Subtask(**{k: row[k] for k in row.keys()})

    @staticmethod
    def row_to_edge(row: sqlite3.Row) -> DependencyEdge:
        return DependencyEdge(**{k: row[k] for k in row.keys()})

    @staticmethod
    def row_to_attempt(row: sqlite3.Row) -> Attempt:
        return Attempt(**{k: row[k] for k in row.keys()})

    @staticmethod
    def row_to_message(row: sqlite3.Row) -> AgentMessage:
        return AgentMessage(**{k: row[k] for k in row.keys()})

    @staticmethod
    def row_to_delivery(row: sqlite3.Row) -> AgentDelivery:
        return AgentDelivery(**{k: row[k] for k in row.keys()})

    @staticmethod
    def row_to_team_session(row: sqlite3.Row) -> TeamSession:
        return TeamSession(**{k: row[k] for k in row.keys()})

    @staticmethod
    def row_to_team_member(row: sqlite3.Row) -> TeamMember:
        return TeamMember(**{k: row[k] for k in row.keys()})
