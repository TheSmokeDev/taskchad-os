"""Executor adapter boundary — dispatch/cancel/status with normalized receipts.

The framework dispatches subtasks through an executor adapter. Every adapter
returns an ExecutorReceipt so the orchestration layer handles all backends
uniformly.

Available executors:
    - LocalExecutor:          default, logs dispatch, operator completes manually
    - PaperclipExecutor:      optional, dispatches to Paperclip governance system
    - WorkflowRunnerExecutor: optional, dispatches to deterministic workflow engine

The ExecutorRegistry resolves executors by name. The framework never depends
on any specific executor being available — LocalExecutor is always the fallback.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any

from orchestration.models import ExecutorReceipt, Subtask
from orchestration.observability import orchestration_span, update_observation

logger = logging.getLogger(__name__)


# ── Abstract Interface ────────────────────────────────────────────────────


class ExecutorAdapter(ABC):
    """Abstract interface for subtask execution backends.

    All methods return ExecutorReceipt for uniform handling by the
    orchestration layer. Executors are downstream of framework state —
    they do not write to the orchestration DB directly.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this executor (e.g. 'local', 'paperclip')."""

    @abstractmethod
    def dispatch(self, subtask: Subtask) -> ExecutorReceipt:
        """Dispatch a subtask for execution.

        Returns a receipt with status 'accepted' or 'rejected'.
        The external_ref field may contain a backend-specific reference
        (e.g. Paperclip issue ID, workflow run ID).
        """

    @abstractmethod
    def cancel(self, subtask: Subtask) -> ExecutorReceipt:
        """Request cancellation of a dispatched subtask.

        Returns a receipt with status 'cancelled' or 'rejected'.
        """

    @abstractmethod
    def check_status(self, subtask: Subtask) -> ExecutorReceipt:
        """Poll current status from the execution backend.

        Returns a receipt with status reflecting the backend's view
        (e.g. 'completed', 'failed', 'progress') or 'rejected' if unknown.
        """

    def get_capabilities(self) -> dict[str, Any]:
        """Return executor capabilities for introspection.

        Override to advertise what this executor supports.
        """
        return {"name": self.name, "async_dispatch": False, "progress_polling": False}


# ── Local Executor (Default) ─────────────────────────────────────────────


class LocalExecutor(ExecutorAdapter):
    """Default executor — logs dispatch, operator completes manually via CLI.

    This is the always-available fallback. The framework works fully with
    just this executor — no external backends required.
    """

    @property
    def name(self) -> str:
        return "local"

    def dispatch(self, subtask: Subtask) -> ExecutorReceipt:
        logger.info("LocalExecutor: dispatch subtask %d (%s)", subtask.id, subtask.title)
        return ExecutorReceipt(
            status="accepted",
            executor_name=self.name,
            timestamp=int(time.time()),
            metadata={"note": "Operator completes manually via CLI"},
        )

    def cancel(self, subtask: Subtask) -> ExecutorReceipt:
        logger.info("LocalExecutor: cancel subtask %d (%s)", subtask.id, subtask.title)
        return ExecutorReceipt(
            status="cancelled",
            executor_name=self.name,
            timestamp=int(time.time()),
        )

    def check_status(self, subtask: Subtask) -> ExecutorReceipt:
        # Local executor has no external state to poll — framework DB is truth
        return ExecutorReceipt(
            status="accepted",
            executor_name=self.name,
            timestamp=int(time.time()),
            metadata={"note": "No external state — framework DB is authoritative"},
        )

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "async_dispatch": False,
            "progress_polling": False,
            "description": "Local/manual executor — operator completes via CLI",
        }


# ── Paperclip Executor (Optional Adapter) ────────────────────────────────


class PaperclipExecutor(ExecutorAdapter):
    """Optional Paperclip governance executor — stub for Phase 4 seam.

    When fully wired, this adapter:
    - Creates Paperclip issues for dispatched subtasks
    - Polls Paperclip for status updates
    - Receives webhook callbacks for completion/failure

    The framework remains the source of truth. Paperclip is a downstream
    execution/governance backend — it never writes to the orchestration DB.

    Configuration:
        PAPERCLIP_API_URL:  Paperclip API endpoint
        PAPERCLIP_API_KEY:  Authentication key
        PAPERCLIP_COMPANY_ID: Company context for issue creation
    """

    def __init__(
        self,
        api_url: str | None = None,
        api_key: str | None = None,
        company_id: str | None = None,
    ):
        self._api_url = api_url
        self._api_key = api_key
        self._company_id = company_id

    @property
    def name(self) -> str:
        return "paperclip"

    @property
    def is_configured(self) -> bool:
        return bool(self._api_url and self._api_key)

    def dispatch(self, subtask: Subtask) -> ExecutorReceipt:
        if not self.is_configured:
            return ExecutorReceipt(
                status="rejected",
                executor_name=self.name,
                error="Paperclip executor not configured (missing API URL or key)",
                timestamp=int(time.time()),
            )
        # STUB: When implemented, this creates a Paperclip issue and returns
        # the issue ID as external_ref.
        logger.info(
            "PaperclipExecutor: would dispatch subtask %d (%s) to %s",
            subtask.id,
            subtask.title,
            self._api_url,
        )
        return ExecutorReceipt(
            status="rejected",
            executor_name=self.name,
            error="Paperclip dispatch not yet implemented",
            timestamp=int(time.time()),
        )

    def cancel(self, subtask: Subtask) -> ExecutorReceipt:
        if not self.is_configured:
            return ExecutorReceipt(
                status="rejected",
                executor_name=self.name,
                error="Paperclip executor not configured",
                timestamp=int(time.time()),
            )
        logger.info(
            "PaperclipExecutor: would cancel subtask %d (%s)",
            subtask.id,
            subtask.title,
        )
        return ExecutorReceipt(
            status="rejected",
            executor_name=self.name,
            error="Paperclip cancel not yet implemented",
            timestamp=int(time.time()),
        )

    def check_status(self, subtask: Subtask) -> ExecutorReceipt:
        if not self.is_configured:
            return ExecutorReceipt(
                status="rejected",
                executor_name=self.name,
                error="Paperclip executor not configured",
                timestamp=int(time.time()),
            )
        return ExecutorReceipt(
            status="rejected",
            executor_name=self.name,
            error="Paperclip status check not yet implemented",
            timestamp=int(time.time()),
        )

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "async_dispatch": True,
            "progress_polling": True,
            "configured": self.is_configured,
            "description": "Paperclip governance/task executor (optional)",
        }


# ── Workflow Runner Executor (Optional Adapter) ──────────────────────────


class WorkflowRunnerExecutor(ExecutorAdapter):
    """Optional deterministic workflow-runner executor — stub for Phase 4b seam.

    Maps framework convoys/subtasks to workflow-engine nodes/edges for
    Archon / remote-coding-agent style deterministic execution.

    When fully wired, this adapter:
    - Translates subtask DAGs to workflow definitions
    - Dispatches workflow runs to the engine
    - Polls or receives callbacks for step completion
    - Reports per-step progress back to the framework

    The framework remains the source of truth. The workflow engine is a
    downstream execution backend — it never writes to the orchestration DB.

    Configuration:
        WORKFLOW_ENGINE_URL:  Workflow engine API endpoint
        WORKFLOW_ENGINE_KEY:  Authentication key
    """

    def __init__(
        self,
        engine_url: str | None = None,
        engine_key: str | None = None,
    ):
        self._engine_url = engine_url
        self._engine_key = engine_key

    @property
    def name(self) -> str:
        return "workflow"

    @property
    def is_configured(self) -> bool:
        return bool(self._engine_url)

    def dispatch(self, subtask: Subtask) -> ExecutorReceipt:
        if not self.is_configured:
            return ExecutorReceipt(
                status="rejected",
                executor_name=self.name,
                error="Workflow runner not configured (missing engine URL)",
                timestamp=int(time.time()),
            )
        # STUB: When implemented, this translates the subtask to a workflow
        # node and triggers execution, returning the run ID as external_ref.
        logger.info(
            "WorkflowRunnerExecutor: would dispatch subtask %d (%s) to %s",
            subtask.id,
            subtask.title,
            self._engine_url,
        )
        return ExecutorReceipt(
            status="rejected",
            executor_name=self.name,
            error="Workflow dispatch not yet implemented",
            timestamp=int(time.time()),
        )

    def cancel(self, subtask: Subtask) -> ExecutorReceipt:
        if not self.is_configured:
            return ExecutorReceipt(
                status="rejected",
                executor_name=self.name,
                error="Workflow runner not configured",
                timestamp=int(time.time()),
            )
        logger.info(
            "WorkflowRunnerExecutor: would cancel subtask %d (%s)",
            subtask.id,
            subtask.title,
        )
        return ExecutorReceipt(
            status="rejected",
            executor_name=self.name,
            error="Workflow cancel not yet implemented",
            timestamp=int(time.time()),
        )

    def check_status(self, subtask: Subtask) -> ExecutorReceipt:
        if not self.is_configured:
            return ExecutorReceipt(
                status="rejected",
                executor_name=self.name,
                error="Workflow runner not configured",
                timestamp=int(time.time()),
            )
        return ExecutorReceipt(
            status="rejected",
            executor_name=self.name,
            error="Workflow status check not yet implemented",
            timestamp=int(time.time()),
        )

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "async_dispatch": True,
            "progress_polling": True,
            "configured": self.is_configured,
            "description": "Deterministic workflow-runner executor (Archon-style, optional)",
        }


# ── Executor Registry ────────────────────────────────────────────────────


class ExecutorRegistry:
    """Resolves executor adapters by name.

    The registry always contains a LocalExecutor as the default fallback.
    Optional executors (Paperclip, workflow runner) are registered on startup
    based on environment configuration.
    """

    def __init__(self) -> None:
        self._executors: dict[str, ExecutorAdapter] = {}
        # Local executor is always available
        self.register(LocalExecutor())

    def register(self, executor: ExecutorAdapter) -> None:
        self._executors[executor.name] = executor
        logger.debug("ExecutorRegistry: registered '%s'", executor.name)

    def get(self, name: str) -> ExecutorAdapter | None:
        return self._executors.get(name)

    def has(self, name: str) -> bool:
        return name in self._executors

    def resolve(self, name: str | None = None) -> ExecutorAdapter:
        """Resolve an executor by name, falling back to 'local'.

        If name is None or not found, returns the local executor.
        """
        if name and name in self._executors:
            return self._executors[name]
        return self._executors["local"]

    @property
    def available(self) -> list[str]:
        return list(self._executors.keys())

    def list_capabilities(self) -> list[dict[str, Any]]:
        return [ex.get_capabilities() for ex in self._executors.values()]

    @property
    def backend_selector(self) -> "BackendSelector":
        if not hasattr(self, "_selector"):
            self._selector = BackendSelector(self)
        return self._selector

    @classmethod
    def default(cls) -> "ExecutorRegistry":
        """Return the process-wide default registry built from env config."""
        global _DEFAULT_REGISTRY
        if _DEFAULT_REGISTRY is None:
            _DEFAULT_REGISTRY = create_default_registry()
        return _DEFAULT_REGISTRY


_DEFAULT_REGISTRY: "ExecutorRegistry | None" = None


# ── Backend Selector (Phase 6) ────────────────────────────────────────────


class BackendSelector:
    """Resolves a concrete executor for a team session's backend_type.

    Backend strategy:
    - 'local'      → LocalExecutor always
    - 'paperclip'  → PaperclipExecutor; fallback LocalExecutor if unavailable
    - 'workflow'   → WorkflowRunnerExecutor; fallback LocalExecutor if unavailable
    - 'auto'       → detect best available; always has local as final fallback

    Availability is checked cheaply (env/config only, no network calls).
    When fallback occurs, a WARNING is logged and the caller receives the
    actually-selected backend name so audit trails remain accurate.
    """

    def __init__(self, registry: ExecutorRegistry):
        self._registry = registry

    def is_available(self, backend_name: str) -> bool:
        """Check if a specific backend is configured (no network calls)."""
        if backend_name == "local":
            return True
        ex = self._registry.get(backend_name)
        if ex is None:
            return False
        # Paperclip and WorkflowRunner expose is_configured; unknown adapters
        # are considered available only if registered.
        is_configured = getattr(ex, "is_configured", None)
        if is_configured is None:
            return True
        return bool(is_configured)

    def select(
        self,
        backend_type: str,
        *,
        workspace_id: int = 1,  # noqa: ARG002 - reserved for future per-workspace config
    ) -> tuple[ExecutorAdapter, str]:
        """Return (executor, actual_backend_name) for the given backend_type.

        Walks BACKEND_FALLBACK_CHAIN for the requested strategy. If the
        preferred backend is unavailable, logs a WARNING and returns the
        next entry in the chain. 'local' is guaranteed to be reachable.
        """
        from orchestration.contract import BACKEND_FALLBACK_CHAIN

        with orchestration_span(
            "backend_selection",
            metadata={"requested_backend": backend_type},
            trace_metadata={"feature_phase": 6},
        ):
            chain = BACKEND_FALLBACK_CHAIN.get(backend_type)
            if chain is None:
                logger.warning(
                    "BackendSelector: unknown backend_type '%s', defaulting to local",
                    backend_type,
                )
                update_observation(
                    metadata={
                        "requested_backend": backend_type,
                        "actual_backend": "local",
                        "fallback_used": True,
                        "fallback_reason": "unknown_backend_type",
                    },
                    level="WARNING",
                    status_message=f"Unknown backend_type '{backend_type}'",
                )
                return self._registry.resolve("local"), "local"

            preferred = chain[0]
            for idx, name in enumerate(chain):
                if self.is_available(name):
                    executor = self._registry.get(name)
                    if executor is None:
                        continue
                    if idx > 0:
                        logger.warning(
                            "BackendSelector: '%s' unavailable, falling back to '%s'",
                            preferred,
                            name,
                        )
                    update_observation(
                        metadata={
                            "requested_backend": preferred,
                            "actual_backend": name,
                            "fallback_used": idx > 0,
                            "fallback_reason": None if idx == 0 else f"{preferred}_unavailable",
                        }
                    )
                    return executor, name

            logger.warning(
                "BackendSelector: no backend in chain for '%s' was resolvable, using local",
                backend_type,
            )
            update_observation(
                metadata={
                    "requested_backend": backend_type,
                    "actual_backend": "local",
                    "fallback_used": True,
                    "fallback_reason": "no_backend_resolved",
                },
                level="WARNING",
                status_message=f"No backend resolved for '{backend_type}'",
            )
            return self._registry.resolve("local"), "local"


def create_default_registry() -> ExecutorRegistry:
    """Create a registry with all adapters configured from environment.

    Reads PAPERCLIP_API_URL/KEY and WORKFLOW_ENGINE_URL/KEY from env.
    Unconfigured adapters are still registered (they return 'rejected'
    receipts cleanly) so the framework can report their availability.
    """
    import os

    registry = ExecutorRegistry()

    # Paperclip — optional
    paperclip = PaperclipExecutor(
        api_url=os.getenv("PAPERCLIP_API_URL"),
        api_key=os.getenv("PAPERCLIP_API_KEY"),
        company_id=os.getenv("PAPERCLIP_COMPANY_ID"),
    )
    registry.register(paperclip)

    # Workflow runner — optional
    workflow = WorkflowRunnerExecutor(
        engine_url=os.getenv("WORKFLOW_ENGINE_URL"),
        engine_key=os.getenv("WORKFLOW_ENGINE_KEY"),
    )
    registry.register(workflow)

    return registry
