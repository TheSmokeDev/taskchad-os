"""Frozen orchestration contract — status enums, valid transitions, rules.

This module defines the canonical types and rules that govern convoy and
mailbox behavior across all adapters (CLI, local API, Mission Control GUI).

NO-DUAL-WRITE RULE:
    During migration from Mission Control ownership to framework ownership,
    there must be zero period where both systems write orchestration state.
    The cutover is atomic per entity type: either MC owns writes or the
    framework does. Never both.

LOCAL API TRUST BOUNDARY:
    The local control API (Phase 3) binds to loopback only by default.
    Authentication is opt-in via ORCHESTRATION_API_TOKEN — when set, all
    requests must present it as a bearer token. Non-loopback access requires
    ORCHESTRATION_API_ALLOW_NON_LOOPBACK=true AND a token to be configured.
"""

from typing import Literal

# ── Convoy Status ──────────────────────────────────────────────────────────

ConvoyStatus = Literal["draft", "active", "paused", "completed", "failed", "cancelled"]

# ── Subtask Status ─────────────────────────────────────────────────────────

SubtaskStatus = Literal[
    "pending",
    "ready",
    "dispatched",
    "running",
    "completed",
    "failed",
    "cancelled",
    "stalled",
]

# ── Message Type ───────────────────────────────────────────────────────────

MessageType = Literal[
    "command",
    "approval_request",
    "clarification",
    "exception",
    "handoff",
    "interrupt",
    "cancel",
    "result",
    "status",
    "message",
]

# ── Typed Mailbox Message Kinds (Phase 3) ─────────────────────────────────
# Team-coordination protocol on top of the DB-backed mailbox. Distinct from
# MessageType above — this is the typed control taxonomy for leader/worker
# conversations. Nullable on the row; legacy rows remain untyped.

MailboxMessageType = Literal[
    "direct",            # plain agent-to-agent message (default)
    "task_assignment",   # leader assigns a subtask to a worker
    "work_handoff",      # worker passes completed output to another agent
    "blocked_request",   # worker signals it is blocked, needs leader input
    "idle_ready",        # worker signals it is idle and ready for new work
    "shutdown_request",  # leader requests graceful worker shutdown
    "shutdown_ack",      # worker acknowledges shutdown request
    "verifier_feedback", # verifier agent sends structured review feedback
    "progress_update",   # worker sends unsolicited progress status
]

TERMINAL_MESSAGE_TYPES: frozenset[str] = frozenset(["shutdown_ack"])

# ── Delivery Status ────────────────────────────────────────────────────────

DeliveryStatus = Literal[
    "pending", "seen", "claimed", "acked", "nacked", "expired", "dead_lettered"
]

# ── Decomposition & Merge ──────────────────────────────────────────────────

DecompositionMode = Literal["manual", "ai_assisted"]
MergeStrategy = Literal["squash", "merge", "rebase"]

# ── Terminal statuses ──────────────────────────────────────────────────────

TERMINAL_SUBTASK_STATUSES: frozenset[SubtaskStatus] = frozenset(
    ["completed", "failed", "cancelled"]
)

TERMINAL_CONVOY_STATUSES: frozenset[ConvoyStatus] = frozenset(["completed", "failed", "cancelled"])

# ── Valid convoy state transitions ─────────────────────────────────────────
# Parity: convoy.ts:updateConvoyStatus() validTransitions

CONVOY_TRANSITIONS: dict[ConvoyStatus, list[ConvoyStatus]] = {
    "draft": ["active", "cancelled"],
    "active": ["paused", "cancelled"],
    "paused": ["active", "cancelled"],
    # Terminal states have no outbound transitions
}

# ── Valid subtask state transitions ───────────────────────────────────────
# Explicit transitions for transition_subtask(). This map covers mechanical
# state changes only. completed and failed are NOT here — those MUST go
# through handle_subtask_completion() / handle_subtask_failure() which
# handle downstream dependency release. pending→ready and ready→dispatched
# are handled by add_subtasks / dispatch_subtask respectively.

SUBTASK_TRANSITIONS: dict[SubtaskStatus, list[SubtaskStatus]] = {
    "pending": ["cancelled"],
    "ready": ["cancelled"],
    "dispatched": ["running", "cancelled"],
    "running": ["stalled", "cancelled"],
    "stalled": ["running", "cancelled"],
    # Terminal states have no outbound transitions
}

# ── Updatable subtask fields ──────────────────────────────────────────────
# Fields that can be patched via update_subtask_fields(). Keeps the allowed
# set explicit so callers can't write arbitrary columns.

UPDATABLE_SUBTASK_FIELDS: frozenset[str] = frozenset(
    [
        "assigned_agent_id",
        "assigned_agent_name",
        "error_message",
        "worktree_branch",
        "merge_commit",
    ]
)

# Fields that can be written after a subtask reaches terminal status.
# These are "seal" fields whose values are only known after completion
# (e.g. merge SHA after merge, final error after failure).
POST_TERMINAL_FIELDS: frozenset[str] = frozenset(
    [
        "merge_commit",
        "error_message",
    ]
)

# ── Executor callback events ───────────────────────────────────────────────
# Valid event_type values for POST /api/executor/callback.
# Parity: mission-control/src/app/api/webhooks/convoy/route.ts event handling.

CALLBACK_EVENT_TYPES: frozenset[str] = frozenset(
    ["subtask.completed", "subtask.failed", "subtask.started", "subtask.stalled"]
)

# ── Team session contract ─────────────────────────────────────────────────

TeamSessionStatus = Literal["active", "idle", "shutdown_requested", "closed"]
TeamMemberRole = Literal["leader", "worker"]
TeamMemberStatus = Literal["active", "idle", "closed"]
BackendType = Literal["local", "paperclip", "workflow", "auto"]

TERMINAL_TEAM_STATUSES: frozenset[str] = frozenset(["closed"])

# ── Backend fallback chain (Phase 6) ──────────────────────────────────────
# Ordered executor-name preference per backend strategy. Framework tries each
# entry in order until one is available. 'local' is always the final fallback.

BACKEND_FALLBACK_CHAIN: dict[str, list[str]] = {
    "auto":      ["paperclip", "workflow", "local"],
    "paperclip": ["paperclip", "local"],
    "workflow":  ["workflow", "local"],
    "local":     ["local"],
}

# ── Defaults ───────────────────────────────────────────────────────────────

DEFAULT_WORKSPACE_ID: int = 1
