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

-- Tenant Isolation v0 (Phase A) -- per-tenant API token store.
-- Stores the SHA-256 HASH of each token, never the raw secret. Multi-tenant
-- mode engages iff at least one non-revoked is_admin=0 row exists. Admin rows
-- (is_admin=1) carry the global/operator token so MT mode does not strand it.
-- persona_scope is a JSON ARRAY of allowed persona ids (NULL = unscoped,
-- reserved for admin rows -- Phase B owns non-admin persona scoping).
-- Revocation is physical state (revoked_at non-NULL), read per request (Rule 2).
CREATE TABLE IF NOT EXISTS tenant_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_sha256 TEXT NOT NULL UNIQUE,
    workspace_id INTEGER NOT NULL,
    persona_scope TEXT,
    is_admin INTEGER NOT NULL DEFAULT 0,
    label TEXT,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    revoked_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tenant_tokens_hash ON tenant_tokens(token_sha256);
"""


class OrchestrationDB:
    """Thin SQLite wrapper for orchestration persistence."""

    def __init__(self, db_path: str | Path = ":memory:", check_same_thread: bool = True):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
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

    # ── Tenant token store (Tenant Isolation v0, Phase A) ───────────────────
    # All reads/writes operate on HASHES. The raw token never touches this
    # layer — the caller hashes it (orchestration.tenant_auth) before lookup.

    def insert_tenant_token(
        self,
        token_sha256: str,
        workspace_id: int,
        persona_scope: str | None,
        is_admin: bool,
        label: str | None,
    ) -> int:
        """Insert a tenant-token row; return its id. *token_sha256* is the HASH.

        ``persona_scope`` is a JSON-array string (or NULL). Raises
        ``sqlite3.IntegrityError`` on a duplicate hash (UNIQUE constraint).
        """
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO tenant_tokens "
                "(token_sha256, workspace_id, persona_scope, is_admin, label) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    token_sha256,
                    workspace_id,
                    persona_scope,
                    1 if is_admin else 0,
                    label,
                ),
            )
        return int(cur.lastrowid)

    def get_binding_by_hash(self, token_sha256: str) -> sqlite3.Row | None:
        """Return the ACTIVE (non-revoked) row for *token_sha256*, or None.

        Rule 2 — revocation is physical state: the ``revoked_at IS NULL`` filter
        lives in the read, so a revoked token stops resolving on the very next
        request with no cache to invalidate.
        """
        return self.conn.execute(
            "SELECT * FROM tenant_tokens "
            "WHERE token_sha256 = ? AND revoked_at IS NULL",
            (token_sha256,),
        ).fetchone()

    def has_active_tenant_token(self) -> bool:
        """True iff at least one ACTIVE non-admin tenant row exists.

        This is the multi-tenant mode flag (Rule 2 — read physical rows, never a
        module cache). Admin rows (``is_admin=1``) do NOT engage MT mode: they
        carry the global/operator token so it survives the first tenant onboard.
        """
        row = self.conn.execute(
            "SELECT 1 FROM tenant_tokens "
            "WHERE revoked_at IS NULL AND is_admin = 0 LIMIT 1"
        ).fetchone()
        return row is not None

    def revoke_token(self, token_id: int) -> bool:
        """Revoke the row with *token_id* (set ``revoked_at``). Idempotent.

        Returns True if a still-active row was revoked, False if the id was
        unknown or already revoked.
        """
        with self.conn:
            cur = self.conn.execute(
                "UPDATE tenant_tokens "
                "SET revoked_at = strftime('%s','now') "
                "WHERE id = ? AND revoked_at IS NULL",
                (token_id,),
            )
        return cur.rowcount > 0

    def list_tenant_tokens(self, include_revoked: bool = True) -> list[sqlite3.Row]:
        """List token rows for the operator CLI. NEVER exposes the raw token.

        The ``token_sha256`` column is present on each row but the CLI must not
        print it; the CLI prints id / workspace / label / is_admin / revoked.
        """
        if include_revoked:
            rows = self.conn.execute(
                "SELECT * FROM tenant_tokens ORDER BY id"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM tenant_tokens WHERE revoked_at IS NULL ORDER BY id"
            ).fetchall()
        return list(rows)

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
