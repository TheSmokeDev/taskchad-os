"""Frozen orchestration data models — dataclasses matching MC donor schema.

Field names and types are 1:1 with the Mission Control SQLite schema
(migration 050_convoy_mode) to maintain parity during and after migration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orchestration.contract import (
    BackendType,
    ConvoyStatus,
    DecompositionMode,
    DeliveryStatus,
    MergeStrategy,
    MessageType,
    SocialWriteAction,
    SubtaskStatus,
    TeamMemberRole,
    TeamMemberStatus,
    TeamSessionStatus,
)

# ── Convoy ─────────────────────────────────────────────────────────────────
# Parity: convoy.ts Convoy interface + mc_convoys table


@dataclass
class Convoy:
    id: int = 0
    workspace_id: int = 1
    title: str = ""
    description: str | None = None
    status: ConvoyStatus = "draft"
    decomposition_mode: DecompositionMode = "manual"
    created_by: str = ""
    base_branch: str = "main"
    repo_path: str | None = None
    merge_strategy: MergeStrategy = "squash"
    total_subtasks: int = 0
    completed_subtasks: int = 0
    failed_subtasks: int = 0
    started_at: int | None = None
    completed_at: int | None = None
    metadata: str | None = None
    created_at: int = 0
    updated_at: int = 0


# ── Subtask ────────────────────────────────────────────────────────────────
# Parity: convoy.ts ConvoySubtask interface + mc_convoy_subtasks table


@dataclass
class Subtask:
    id: int = 0
    convoy_id: int = 0
    workspace_id: int = 1
    title: str = ""
    description: str | None = None
    status: SubtaskStatus = "pending"
    assigned_agent_id: str | None = None
    assigned_agent_name: str | None = None
    paperclip_issue_id: str | None = None
    remaining_dependencies: int = 0
    port_allocated: int | None = None
    worktree_path: str | None = None
    worktree_branch: str | None = None
    merge_commit: str | None = None
    error_message: str | None = None
    stall_detected_at: int | None = None
    dispatched_at: int | None = None
    started_at: int | None = None
    completed_at: int | None = None
    seq: int = 0
    metadata: str | None = None
    created_at: int = 0
    updated_at: int = 0


# ── Dependency Edge ────────────────────────────────────────────────────────
# Parity: convoy.ts DependencyEdge interface + mc_convoy_dependency_edges


@dataclass
class DependencyEdge:
    id: int = 0
    workspace_id: int = 1
    convoy_id: int = 0
    from_subtask_id: int = 0
    to_subtask_id: int = 0


# ── Attempt ────────────────────────────────────────────────────────────────
# Parity: mc_convoy_attempts table


@dataclass
class Attempt:
    id: int = 0
    workspace_id: int = 1
    convoy_id: int = 0
    subtask_id: int = 0
    attempt_key: str = ""
    action: str = "dispatch"  # dispatch | cancel | nudge
    status: str = "pending"  # pending | sent | acked | failed | expired
    paperclip_issue_id: str | None = None
    error_message: str | None = None
    created_at: int = 0
    updated_at: int = 0


# ── Agent Message ──────────────────────────────────────────────────────────
# Parity: mailbox.ts AgentMessage interface + agent_messages table


@dataclass
class AgentMessage:
    id: int = 0
    workspace_id: int = 1
    convoy_id: int | None = None
    thread_id: int | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    reply_to_message_id: int | None = None
    from_agent: str = ""
    message_type: MessageType = "message"
    subject: str | None = None
    body: str = ""
    artifact_refs: str | None = None
    dedupe_key: str | None = None
    msg_type: str | None = None
    created_at: int = 0


# ── Agent Delivery ─────────────────────────────────────────────────────────
# Parity: mailbox.ts AgentDelivery interface + agent_deliveries table


@dataclass
class AgentDelivery:
    id: int = 0
    workspace_id: int = 1
    message_id: int = 0
    recipient_agent: str = ""
    status: DeliveryStatus = "pending"
    claim_token: str | None = None
    claimed_at: int | None = None
    acked_at: int | None = None
    created_at: int = 0


# ── Composite types ────────────────────────────────────────────────────────


@dataclass
class ConvoyWithSubtasks:
    convoy: Convoy = field(default_factory=Convoy)
    subtasks: list[Subtask] = field(default_factory=list)
    edges: list[DependencyEdge] = field(default_factory=list)


@dataclass
class MessageWithDeliveries:
    message: AgentMessage = field(default_factory=AgentMessage)
    deliveries: list[AgentDelivery] = field(default_factory=list)


# ── Input DTOs ─────────────────────────────────────────────────────────────
# Parity: convoy.ts CreateConvoyInput, CreateSubtaskInput
# Parity: mailbox.ts SendMessageInput


@dataclass
class CreateSubtaskInput:
    title: str = ""
    description: str | None = None
    assigned_agent_id: str | None = None
    assigned_agent_name: str | None = None
    depends_on_subtask_indexes: list[int] = field(default_factory=list)
    metadata: str | None = None


@dataclass
class AddSubtaskInput:
    title: str = ""
    description: str | None = None
    assigned_agent_id: str | None = None
    assigned_agent_name: str | None = None
    depends_on_subtask_ids: list[int] = field(default_factory=list)
    metadata: str | None = None


@dataclass
class CreateConvoyInput:
    title: str = ""
    description: str | None = None
    created_by: str = ""
    base_branch: str = "main"
    repo_path: str | None = None
    merge_strategy: MergeStrategy = "squash"
    decomposition_mode: DecompositionMode = "manual"
    subtasks: list[CreateSubtaskInput] = field(default_factory=list)


@dataclass
class SendMessageInput:
    from_agent: str = ""
    recipients: list[str] = field(default_factory=list)
    body: str = ""
    convoy_id: int | None = None
    thread_id: int | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    reply_to_message_id: int | None = None
    message_type: MessageType = "message"
    subject: str | None = None
    artifact_refs: dict[str, Any] | None = None
    dedupe_key: str | None = None
    msg_type: str | None = None


# ── Executor Receipt ──────────────────────────────────────────────────────
# Normalized receipt returned by all executor adapters on dispatch/cancel/status.

ExecutorReceiptStatus = (
    str  # "accepted" | "rejected" | "completed" | "failed" | "cancelled" | "progress"
)


@dataclass
class ExecutorReceipt:
    """Normalized receipt from an executor adapter.

    Every executor operation (dispatch, cancel, check_status) returns one of
    these so the framework can handle all backends uniformly.
    """

    status: ExecutorReceiptStatus = "accepted"
    external_ref: str | None = None  # e.g. Paperclip issue ID, workflow run ID
    executor_name: str = "local"  # which executor produced this
    error: str | None = None  # human-readable error if status is "rejected"/"failed"
    progress_pct: float | None = None  # 0.0-1.0 for progress reports
    progress_message: str | None = None  # optional message for progress updates
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: int = 0  # epoch seconds, filled by executor


@dataclass
class TeamSession:
    id: int = 0
    workspace_id: int = 1
    convoy_id: int | None = None
    team_name: str = ""
    lead_agent_id: str = ""
    lead_agent_name: str | None = None
    status: TeamSessionStatus = "active"
    backend_type: BackendType = "local"
    last_activity_at: int | None = None
    shutdown_requested_at: int | None = None
    closed_at: int | None = None
    metadata: str | None = None
    created_at: int = 0
    updated_at: int = 0


@dataclass
class TeamMember:
    id: int = 0
    workspace_id: int = 1
    team_session_id: int = 0
    agent_id: str = ""
    agent_name: str | None = None
    role: TeamMemberRole = "worker"
    subtask_id: int | None = None
    status: TeamMemberStatus = "active"
    joined_at: int = 0
    last_activity_at: int | None = None


@dataclass
class TeamSessionWithMembers:
    session: TeamSession = field(default_factory=TeamSession)
    members: list[TeamMember] = field(default_factory=list)


@dataclass
class CreateTeamSessionInput:
    team_name: str = ""
    lead_agent_id: str = ""
    lead_agent_name: str | None = None
    convoy_id: int | None = None
    backend_type: BackendType = "local"
    metadata: str | None = None


@dataclass
class AddTeamMemberInput:
    agent_id: str = ""
    agent_name: str | None = None
    role: TeamMemberRole = "worker"
    subtask_id: int | None = None


# ── Typed Mailbox Payloads (Phase 3) ──────────────────────────────────────
# Structured bodies for team-coordination messages. JSON-serialized into
# `agent_messages.body` by the typed send helpers in mailbox_service.


@dataclass
class TaskAssignmentPayload:
    subtask_id: int
    title: str
    description: str | None = None
    depends_on: list[int] = field(default_factory=list)


@dataclass
class WorkHandoffPayload:
    subtask_id: int
    summary: str
    artifacts: list[str] = field(default_factory=list)


@dataclass
class BlockedRequestPayload:
    subtask_id: int | None
    reason: str
    needs: str


@dataclass
class VerifierFeedbackPayload:
    subtask_id: int | None
    verdict: str  # "pass" | "fail" | "needs_revision"
    findings: list[str] = field(default_factory=list)
    score: float | None = None


@dataclass
class CofounderAssignmentPayload:
    """Cofounder v2 WS3 — one delegated agenda line (cofounder -> persona).

    ``agenda_ref`` anchors the assignment back to its artifact
    (``AGENDA-YYYY-MM-DD.md#<line>``); ``repo`` is a REPOSITORIES.md slug or
    None for non-repo work (research, outreach, content). ``mode`` is the
    OPERATOR-APPROVED execution mode (WS4): ``draft`` = direct no-tools
    runtime run producing a vault deliverable; ``code`` = detached Archon
    worktree dispatch (repo required, PR-for-review).
    """

    subtask_id: int
    task: str
    repo: str | None = None
    why: str = ""
    priority: int = 2
    agenda_ref: str = ""
    due: str | None = None
    mode: str = "draft"


@dataclass
class CofounderResultPayload:
    """Cofounder v2 WS4 — one work-loop outcome (persona -> cofounder).

    ``status``: ``done`` (draft deliverable written) | ``dispatched``
    (Archon run started; completion tracking is WS5's) | ``failed`` |
    ``refused`` (scope re-check at claim denied — Rule 4's second half).
    """

    subtask_id: int
    agenda_ref: str = ""
    status: str = "done"
    summary: str = ""
    deliverable_path: str | None = None
    run_id: str | None = None
    branch: str | None = None


@dataclass
class ShutdownRequestPayload:
    reason: str = "coordinator requested graceful shutdown"


@dataclass
class IdleReadyPayload:
    subtask_id: int | None = None
    message: str = "worker idle and ready for next task"


@dataclass
class ProgressReport:
    """In-flight progress update from an executor for a running subtask."""

    subtask_id: int = 0
    convoy_id: int = 0
    executor_name: str = "local"
    progress_pct: float = 0.0  # 0.0-1.0
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: int = 0


# ── Social-Write Task (Phase 1) ────────────────────────────────────────────
# Carried via Subtask.metadata = json.dumps(asdict(task)) into BrowserExecutor.
# A SocialWriteTask only EXISTS because the chat HANDLER already gated on the
# operator's verbatim text and got decision.allowed — it carries NO approval
# claim. There is deliberately NO approval_token field (default-deny invariant).


@dataclass
class SocialWriteTask:
    workflow_id: str  # a registered write workflow, e.g. "linkedin.post.create"
    target_url: str  # absolute http(s); the gate redacts it on the way out
    payload_text: str  # final approved text (post body or connection note)
    action: SocialWriteAction = "post"  # "post" | "connect"
    # literal default (Rule-1 safe) — capture a screenshot after the write
    post_action_snapshot: bool = True
    # Optional operator-reviewed local asset attached to the exact queue row.
    # This is content, not an approval claim; the handler/button remains the
    # only approval authority.
    media_path: str | None = None
