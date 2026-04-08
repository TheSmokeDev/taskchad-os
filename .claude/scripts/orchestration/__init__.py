"""Framework-owned orchestration: convoy, mailbox, and multi-agent dispatch.

This package is the canonical source of truth for orchestration primitives.
Mission Control and Paperclip are downstream consumers.
"""

from orchestration.contract import (
    CALLBACK_EVENT_TYPES,
    CONVOY_TRANSITIONS,
    DEFAULT_WORKSPACE_ID,
    POST_TERMINAL_FIELDS,
    SUBTASK_TRANSITIONS,
    TERMINAL_SUBTASK_STATUSES,
    UPDATABLE_SUBTASK_FIELDS,
    ConvoyStatus,
    DecompositionMode,
    DeliveryStatus,
    MergeStrategy,
    MessageType,
    SubtaskStatus,
)
from orchestration.executor import (
    ExecutorAdapter,
    ExecutorRegistry,
    LocalExecutor,
    PaperclipExecutor,
    WorkflowRunnerExecutor,
    create_default_registry,
)
from orchestration.models import (
    AddSubtaskInput,
    AgentDelivery,
    AgentMessage,
    Attempt,
    Convoy,
    ConvoyWithSubtasks,
    CreateConvoyInput,
    CreateSubtaskInput,
    DependencyEdge,
    ExecutorReceipt,
    MessageWithDeliveries,
    ProgressReport,
    SendMessageInput,
    Subtask,
)

__all__ = [
    # Contract
    "CALLBACK_EVENT_TYPES",
    "ConvoyStatus",
    "SubtaskStatus",
    "MessageType",
    "DeliveryStatus",
    "DecompositionMode",
    "MergeStrategy",
    "CONVOY_TRANSITIONS",
    "POST_TERMINAL_FIELDS",
    "SUBTASK_TRANSITIONS",
    "TERMINAL_SUBTASK_STATUSES",
    "UPDATABLE_SUBTASK_FIELDS",
    "DEFAULT_WORKSPACE_ID",
    # Models
    "Convoy",
    "Subtask",
    "DependencyEdge",
    "Attempt",
    "AgentMessage",
    "AgentDelivery",
    "ConvoyWithSubtasks",
    "MessageWithDeliveries",
    "AddSubtaskInput",
    "CreateConvoyInput",
    "CreateSubtaskInput",
    "SendMessageInput",
    # Executor boundary (Phase 4)
    "ExecutorReceipt",
    "ProgressReport",
    "ExecutorAdapter",
    "ExecutorRegistry",
    "LocalExecutor",
    "PaperclipExecutor",
    "WorkflowRunnerExecutor",
    "create_default_registry",
]
