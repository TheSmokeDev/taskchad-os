"""Social-write browser executor — Phase 1, ban-safe core.

Turns an operator-approved-PER-ACTION social-write request (already gated by
the chat HANDLER) into ONE audited agent-browser action on the existing visible
Chrome (CDP 9222), with a screenshot receipt and a redacted audit row.

Approval is NOT this executor's job. The chat handler is the sole approval
authority (it gates on the operator's verbatim message via
`require_browser_workflow_permission` and dispatches a SocialWriteTask ONLY when
`decision.allowed`). The executor receives an already-allowed task and only:
  - confirms the visible Chrome is ready (refuses + audits "failed" otherwise),
  - drives the write through an injected SocialWriteDriver (agent-browser --cdp),
  - persists a screenshot (path only, never bytes/URL/page text),
  - audits every attempt (failed/succeeded).

Slice boundary: this module lives in orchestration/ and must NOT import the
chat slice (browser_control / browser_workflows / browser_audit). The concrete
SocialWriteDriver is injected by the chat slice — keeping orchestration free of
agent-browser imports and provider-agnostic at import time.
"""

from __future__ import annotations

import json
import time
from typing import Any, Protocol

from orchestration.contract import SOCIAL_WRITE_FIELDS
from orchestration.executor import ExecutorAdapter
from orchestration.models import ExecutorReceipt, SocialWriteTask, Subtask
from orchestration.observability import orchestration_span

# ── Driver Protocol (chat slice implements this) ──────────────────────────


class SocialWriteDriver(Protocol):
    """Injected by the chat slice. Keeps orchestration free of agent-browser imports.

    Every method is keyword-only where it takes a port so a positional/keyword
    drift in the concrete impl cannot bite. Each maps to a real, differently
    named helper in the chat slice (annotated below).

    NOTE: there is NO `gate` method on this surface. Approval is the HANDLER's
    job — the executor receives an already-allowed task and never re-evaluates
    approval, and never feeds the post body into any gate.

    Backing helpers (concrete impl in the chat slice):
      resolve_port -> browser_control.resolve_cdp_port(env_names=..., default=...)
      readiness    -> browser_control.browser_readiness(port=...) -> {enabled: bool, ...}
      drive        -> sequential run_agent_browser(--cdp) steps -> (ok, detail)
      screenshot   -> persists capture_browser_screenshot_png BYTES -> file, returns PATH
      audit        -> browser_audit.append_browser_audit_record (redacts internally)
    """

    def resolve_port(self) -> int:
        ...

    def readiness(self, *, port: int) -> dict:
        ...

    def drive(self, task: SocialWriteTask, *, port: int) -> tuple[bool, str]:
        ...

    def screenshot(self, *, port: int, workflow_id: str) -> str | None:
        ...

    def audit(self, **kwargs: Any) -> None:
        ...


# ── Task parsing ──────────────────────────────────────────────────────────


def parse_social_write_task(metadata: str | None) -> SocialWriteTask:
    """Parse a SocialWriteTask from Subtask.metadata JSON.

    Allowlist-filters the decoded dict to SOCIAL_WRITE_FIELDS so a tampered or
    over-broad metadata blob cannot smuggle an approval claim or any other
    field into the task. Raises ValueError on malformed/absent metadata.
    """

    if not metadata:
        raise ValueError("SocialWriteTask metadata is missing")
    try:
        decoded = json.loads(metadata)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"SocialWriteTask metadata is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ValueError("SocialWriteTask metadata must be a JSON object")
    filtered = {k: v for k, v in decoded.items() if k in SOCIAL_WRITE_FIELDS}
    required = ("workflow_id", "target_url", "payload_text")
    if any(key not in filtered for key in required):
        raise ValueError("SocialWriteTask metadata is missing required fields")
    return SocialWriteTask(**filtered)


# ── Executor ──────────────────────────────────────────────────────────────


class BrowserExecutor(ExecutorAdapter):
    """Operator-in-the-loop social-write executor over the visible Chrome CDP.

    Constructed IN-HANDLER as a local variable — NEVER registered into the
    shared ExecutorRegistry.default(). A stray resolve("browser") falls back to
    LocalExecutor and silently no-ops a write, which is the intended fail-safe.
    """

    def __init__(self, driver: SocialWriteDriver) -> None:
        self._driver = driver

    @property
    def name(self) -> str:
        return "browser"

    def dispatch(self, subtask: Subtask) -> ExecutorReceipt:
        with orchestration_span(
            "browser_executor_dispatch",
            metadata={"subtask_id": subtask.id},
            trace_metadata={"feature_phase": "social_write_phase_1"},
        ):
            try:
                task = parse_social_write_task(subtask.metadata)
            except ValueError as exc:
                return ExecutorReceipt(
                    status="failed",
                    executor_name=self.name,
                    error=f"invalid social-write task: {exc}",
                    timestamp=int(time.time()),
                )

            port = self._driver.resolve_port()

            # Rule 2: trust the physical readiness guard, not a cached "up" flag.
            # The task is ALREADY approved (handler gated it) — the executor does
            # NOT call the gate and does NOT inspect any token.
            readiness = self._driver.readiness(port=port)
            if not readiness.get("enabled"):
                self._driver.audit(
                    command=f"executor:{task.workflow_id}",
                    workflow_id=task.workflow_id,
                    outcome="failed",
                    reason="visible chrome not ready",
                    cdp_port=readiness.get("cdp_port"),
                    cdp_reachable=readiness.get("cdp_reachable"),
                    target_url=task.target_url,
                    action=task.action,
                    subtask_id=subtask.id,
                    executor_name=self.name,
                )
                return ExecutorReceipt(
                    status="failed",
                    executor_name=self.name,
                    error="visible-chrome not ready",
                    timestamp=int(time.time()),
                )

            try:
                ok, detail = self._driver.drive(task, port=port)
            except Exception as exc:  # noqa: BLE001 - drive failures must audit, never crash dispatch
                self._driver.audit(
                    command=f"executor:{task.workflow_id}",
                    workflow_id=task.workflow_id,
                    outcome="failed",
                    reason=str(exc),
                    cdp_port=readiness.get("cdp_port"),
                    cdp_reachable=readiness.get("cdp_reachable"),
                    target_url=task.target_url,
                    action=task.action,
                    subtask_id=subtask.id,
                    executor_name=self.name,
                )
                return ExecutorReceipt(
                    status="failed",
                    executor_name=self.name,
                    error=str(exc),
                    timestamp=int(time.time()),
                )

            shot: str | None = None
            if ok and task.post_action_snapshot:
                try:
                    shot = self._driver.screenshot(port=port, workflow_id=task.workflow_id)
                except Exception:  # noqa: BLE001 - a screenshot failure must not fail a landed write
                    shot = None

            self._driver.audit(
                command=f"executor:{task.workflow_id}",
                workflow_id=task.workflow_id,
                outcome="succeeded" if ok else "failed",
                reason=detail,
                cdp_port=readiness.get("cdp_port"),
                cdp_reachable=readiness.get("cdp_reachable"),
                target_url=task.target_url,
                action=task.action,
                subtask_id=subtask.id,
                executor_name=self.name,
            )

            return ExecutorReceipt(
                status="completed" if ok else "failed",
                executor_name=self.name,
                error=None if ok else detail,
                metadata={
                    "screenshot_path": shot,  # path only — never bytes/URL/page text
                    "workflow_id": task.workflow_id,
                    "subtask_id": subtask.id,
                },
                timestamp=int(time.time()),
            )

    def cancel(self, subtask: Subtask) -> ExecutorReceipt:
        # Social writes are one-shot synchronous attempts — nothing to cancel.
        return ExecutorReceipt(
            status="rejected",
            executor_name=self.name,
            error="browser social-write dispatch is synchronous — nothing to cancel",
            timestamp=int(time.time()),
        )

    def check_status(self, subtask: Subtask) -> ExecutorReceipt:
        # No external state to poll — the receipt from dispatch is authoritative.
        return ExecutorReceipt(
            status="rejected",
            executor_name=self.name,
            error="browser social-write executor has no pollable external state",
            timestamp=int(time.time()),
        )

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "async_dispatch": False,
            "progress_polling": False,
            "description": (
                "Operator-approved social-write executor over visible Chrome CDP (Phase 1)"
            ),
        }
