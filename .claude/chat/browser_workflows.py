"""Browser workflow registry and permission gates."""

from __future__ import annotations

import re
from dataclasses import dataclass

from browser_control import redact_url, validate_web_url


@dataclass(frozen=True)
class BrowserWorkflow:
    workflow_id: str
    description: str
    classification: str
    approval_level: str
    router_command: str | None
    default_url: str | None
    audit_action: str
    approval_examples: tuple[str, ...] = ()

    @property
    def is_write(self) -> bool:
        return self.classification == "write"

    @property
    def is_navigation(self) -> bool:
        return self.classification == "navigation"


@dataclass(frozen=True)
class BrowserWorkflowDecision:
    workflow_id: str
    allowed: bool
    outcome: str
    reason: str
    next_action: str
    target_url: str | None = None


_WORKFLOWS: dict[str, BrowserWorkflow] = {
    "browser.status": BrowserWorkflow(
        workflow_id="browser.status",
        description="Check visible browser and CDP readiness.",
        classification="read",
        approval_level="none",
        router_command="/browser status",
        default_url=None,
        audit_action="browser_status",
    ),
    "browser.tabs": BrowserWorkflow(
        workflow_id="browser.tabs",
        description="List visible browser tabs with URL redaction.",
        classification="read",
        approval_level="none",
        router_command="/browser tabs",
        default_url=None,
        audit_action="browser_tabs",
    ),
    "browser.open": BrowserWorkflow(
        workflow_id="browser.open",
        description="Navigate the visible browser to an absolute http(s) URL.",
        classification="navigation",
        approval_level="none",
        router_command="/browser open",
        default_url=None,
        audit_action="browser_open",
    ),
    "browser.snapshot": BrowserWorkflow(
        workflow_id="browser.snapshot",
        description="Capture a text snapshot from the visible browser.",
        classification="read",
        approval_level="none",
        router_command="/browser snapshot",
        default_url=None,
        audit_action="browser_snapshot",
    ),
    "browserops.capabilities": BrowserWorkflow(
        workflow_id="browserops.capabilities",
        description="Show Browser Homie readiness, policy, and registered workflow capabilities.",
        classification="read",
        approval_level="none",
        router_command="/browserops capabilities",
        default_url=None,
        audit_action="browserops_capabilities",
    ),
    "browserops.guide": BrowserWorkflow(
        workflow_id="browserops.guide",
        description="Load the current agent-browser core guide for browser work.",
        classification="read",
        approval_level="none",
        router_command="/browserops guide",
        default_url=None,
        audit_action="browserops_guide",
    ),
    "browserops.context": BrowserWorkflow(
        workflow_id="browserops.context",
        description="Prefetch Browser Homie context for engine-side browser tasks.",
        classification="read",
        approval_level="none",
        router_command="/browserops context",
        default_url=None,
        audit_action="browserops_context",
    ),
    "browser.viewer.status": BrowserWorkflow(
        workflow_id="browser.viewer.status",
        description="Read the Homie Dashboard browser viewer status.",
        classification="read",
        approval_level="none",
        router_command=None,
        default_url=None,
        audit_action="browser_viewer_status",
    ),
    "browser.viewer.screenshot": BrowserWorkflow(
        workflow_id="browser.viewer.screenshot",
        description="Capture a transient read-only screenshot for the Homie Dashboard viewer.",
        classification="read",
        approval_level="none",
        router_command=None,
        default_url=None,
        audit_action="browser_viewer_screenshot",
    ),
    "browser.viewer.stream_enable": BrowserWorkflow(
        workflow_id="browser.viewer.stream_enable",
        description="Enable the read-only browser viewport stream.",
        classification="read",
        approval_level="none",
        router_command=None,
        default_url=None,
        audit_action="browser_viewer_stream_enable",
    ),
    "browser.viewer.stream_disable": BrowserWorkflow(
        workflow_id="browser.viewer.stream_disable",
        description="Disable the read-only browser viewport stream.",
        classification="read",
        approval_level="none",
        router_command=None,
        default_url=None,
        audit_action="browser_viewer_stream_disable",
    ),
    "linkedin.profile.open": BrowserWorkflow(
        workflow_id="linkedin.profile.open",
        description="Open the configured LinkedIn profile in the visible browser.",
        classification="navigation",
        approval_level="none",
        router_command="/linkedin_profile open",
        default_url="https://www.linkedin.com/in/",
        audit_action="linkedin_profile_open",
    ),
    "linkedin.profile.edit": BrowserWorkflow(
        workflow_id="linkedin.profile.edit",
        description="Edit the configured LinkedIn profile.",
        classification="write",
        approval_level="explicit",
        router_command=None,
        default_url="https://www.linkedin.com/in/",
        audit_action="linkedin_profile_edit",
        approval_examples=("approve linkedin profile edit",),
    ),
    "linkedin.post.create": BrowserWorkflow(
        workflow_id="linkedin.post.create",
        description="Create a LinkedIn post.",
        classification="write",
        approval_level="explicit",
        router_command=None,
        default_url="https://www.linkedin.com/feed/",
        audit_action="linkedin_post_create",
        approval_examples=("post this to linkedin now",),
    ),
    "linkedin.connection.request": BrowserWorkflow(
        workflow_id="linkedin.connection.request",
        description="Send a LinkedIn connection request.",
        classification="write",
        approval_level="explicit",
        router_command=None,
        default_url="https://www.linkedin.com/",
        audit_action="linkedin_connection_request",
        approval_examples=("send the connection request",),
    ),
    "x.post.create": BrowserWorkflow(
        workflow_id="x.post.create",
        description="Create an X post.",
        classification="write",
        approval_level="explicit",
        router_command=None,
        default_url="https://x.com/",
        audit_action="x_post_create",
        approval_examples=("post this to x now",),
    ),
}


def list_browser_workflows() -> list[BrowserWorkflow]:
    return list(_WORKFLOWS.values())


def get_browser_workflow(workflow_id: str) -> BrowserWorkflow | None:
    return _WORKFLOWS.get(workflow_id)


def require_browser_workflow_permission(
    workflow_id: str,
    user_text: str,
    *,
    approved: bool = False,
    target_url: str | None = None,
) -> BrowserWorkflowDecision:
    """Default-deny gate for browser workflows."""

    workflow = get_browser_workflow(workflow_id)
    redacted_target = redact_url(target_url) if target_url else None
    if workflow is None:
        return BrowserWorkflowDecision(
            workflow_id=workflow_id,
            allowed=False,
            outcome="blocked",
            reason=f"Unknown browser workflow: {workflow_id}",
            next_action="Use a registered browser workflow.",
            target_url=redacted_target,
        )

    if workflow.is_navigation:
        url = target_url or _extract_http_url(user_text) or workflow.default_url
        if not url:
            return BrowserWorkflowDecision(
                workflow_id=workflow_id,
                allowed=False,
                outcome="blocked",
                reason="Navigation workflows require a target URL.",
                next_action="Use an absolute http(s) URL.",
                target_url=redacted_target,
            )
        try:
            validate_web_url(url)
        except ValueError as exc:
            return BrowserWorkflowDecision(
                workflow_id=workflow_id,
                allowed=False,
                outcome="blocked",
                reason=str(exc),
                next_action="Use an absolute http(s) URL.",
                target_url=redact_url(url),
            )
        return BrowserWorkflowDecision(
            workflow_id=workflow_id,
            allowed=True,
            outcome="allowed",
            reason="Read/navigation browser workflow allowed.",
            next_action="Proceed.",
            target_url=redact_url(url),
        )

    if workflow.is_write and not (approved or _has_explicit_approval(workflow, user_text)):
        example = workflow.approval_examples[0] if workflow.approval_examples else "approve this browser workflow"
        return BrowserWorkflowDecision(
            workflow_id=workflow_id,
            allowed=False,
            outcome="blocked",
            reason=f"{workflow_id} is write-capable and requires explicit approval.",
            next_action=f"Reply with explicit approval, for example: \"{example}\".",
            target_url=redacted_target,
        )

    return BrowserWorkflowDecision(
        workflow_id=workflow_id,
        allowed=True,
        outcome="allowed",
        reason="Browser workflow allowed.",
        next_action="Proceed.",
        target_url=redacted_target,
    )


def _has_explicit_approval(workflow: BrowserWorkflow, user_text: str) -> bool:
    normalized = _normalize_approval_text(user_text)
    if not normalized:
        return False
    return any(example in normalized for example in workflow.approval_examples)


def _normalize_approval_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _extract_http_url(text: str) -> str | None:
    match = re.search(r"https?://[^\s\"']+", text)
    return match.group(0) if match else None
