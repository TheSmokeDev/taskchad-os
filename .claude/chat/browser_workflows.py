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
    # M12 — phone-drive: OPERATOR-initiated remote control from the mobile
    # viewer. "interact" = the human pushes each button live (not agent
    # autonomy); agent-side writes keep their approval-phrase gates. Every
    # attempt still audits through the dashboard endpoints.
    "browser.viewer.elements": BrowserWorkflow(
        workflow_id="browser.viewer.elements",
        description="List the active tab's interactive elements (snapshot refs) for the mobile viewer.",
        classification="read",
        approval_level="none",
        router_command=None,
        default_url=None,
        audit_action="browser_viewer_elements",
    ),
    "browser.viewer.act": BrowserWorkflow(
        workflow_id="browser.viewer.act",
        description="Operator-driven action (click/fill/press/scroll/history) on the visible browser from the mobile viewer.",
        classification="interact",
        approval_level="operator",
        router_command=None,
        default_url=None,
        audit_action="browser_viewer_act",
    ),
    "browser.viewer.navigate": BrowserWorkflow(
        workflow_id="browser.viewer.navigate",
        description="Operator-driven navigation of the visible browser to an absolute http(s) URL from the mobile viewer.",
        classification="navigation",
        approval_level="operator",
        router_command=None,
        default_url=None,
        audit_action="browser_viewer_navigate",
    ),
    # P4.1 Phase B — the ghost DEVICE surface (screen / tap / app), distinct
    # from the browser-viewer above. These drive the ghost emulator's whole
    # device via raw adb; they are STRUCTURALLY ghost-only (ghost_capabilities
    # refuses any target != "ghost" before the gate) and default-ON for the
    # ghost. The dashboard /ghost page is the operator surface.
    "ghost.viewer.screen": BrowserWorkflow(
        workflow_id="ghost.viewer.screen",
        description="Capture the ghost device's live screen (adb screencap) for the dashboard viewer.",
        classification="read",
        approval_level="none",
        router_command=None,
        default_url=None,
        audit_action="ghost_viewer_screen",
    ),
    "ghost.viewer.tap": BrowserWorkflow(
        workflow_id="ghost.viewer.tap",
        description="Operator-driven tap on the ghost device from the dashboard viewer.",
        classification="interact",
        approval_level="operator",
        router_command=None,
        default_url=None,
        audit_action="ghost_viewer_tap",
    ),
    "ghost.viewer.text": BrowserWorkflow(
        workflow_id="ghost.viewer.text",
        description="Operator-driven text input on the ghost device from the dashboard viewer.",
        classification="interact",
        approval_level="operator",
        router_command=None,
        default_url=None,
        audit_action="ghost_viewer_text",
    ),
    "ghost.viewer.swipe": BrowserWorkflow(
        workflow_id="ghost.viewer.swipe",
        description="Operator-driven swipe on the ghost device from the dashboard viewer.",
        classification="interact",
        approval_level="operator",
        router_command=None,
        default_url=None,
        audit_action="ghost_viewer_swipe",
    ),
    "ghost.viewer.key": BrowserWorkflow(
        workflow_id="ghost.viewer.key",
        description="Operator-driven keyevent on the ghost device from the dashboard viewer.",
        classification="interact",
        approval_level="operator",
        router_command=None,
        default_url=None,
        audit_action="ghost_viewer_key",
    ),
    "ghost.viewer.app_launch": BrowserWorkflow(
        workflow_id="ghost.viewer.app_launch",
        description="Operator-driven app launch on the ghost device from the dashboard viewer.",
        classification="interact",
        approval_level="operator",
        router_command=None,
        default_url=None,
        audit_action="ghost_viewer_app_launch",
    ),
    "ghost.viewer.app_install": BrowserWorkflow(
        workflow_id="ghost.viewer.app_install",
        description="Operator-driven APK install on the ghost device from the dashboard viewer.",
        classification="interact",
        approval_level="operator",
        router_command=None,
        default_url=None,
        audit_action="ghost_viewer_app_install",
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
        router_command="/linkedin_post",
        default_url="https://www.linkedin.com/feed/",
        audit_action="linkedin_post_create",
        approval_examples=("post this to linkedin now",),
    ),
    "linkedin.connection.request": BrowserWorkflow(
        workflow_id="linkedin.connection.request",
        description="Send a LinkedIn connection request.",
        classification="write",
        approval_level="explicit",
        router_command="/linkedin_connect",
        default_url="https://www.linkedin.com/",
        audit_action="linkedin_connection_request",
        approval_examples=("send this linkedin connection request now",),
    ),
    "x.scout": BrowserWorkflow(
        workflow_id="x.scout",
        description="Scout the X timeline via the visible browser and draft a signal digest.",
        classification="read",
        approval_level="none",
        router_command="/x scout",
        default_url=None,
        audit_action="x_scout",
    ),
    "x.timeline": BrowserWorkflow(
        workflow_id="x.timeline",
        description="Read the X timeline via the visible browser (read-only).",
        classification="read",
        approval_level="none",
        router_command="/x timeline",
        default_url=None,
        audit_action="x_timeline",
    ),
    "x.search": BrowserWorkflow(
        workflow_id="x.search",
        description="Search X via the visible browser (read-only).",
        classification="read",
        approval_level="none",
        router_command="/x search",
        default_url=None,
        audit_action="x_search",
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
    "reddit.research": BrowserWorkflow(
        workflow_id="reddit.research",
        description="Search and read Reddit threads via the visible browser (read-only).",
        classification="read",
        approval_level="none",
        router_command="/reddit research",
        default_url=None,
        audit_action="reddit_research",
    ),
    "reddit.comment.create": BrowserWorkflow(
        workflow_id="reddit.comment.create",
        description="Post a comment reply on a Reddit thread.",
        classification="write",
        approval_level="explicit",
        router_command=None,
        default_url=None,
        audit_action="reddit_comment_create",
        approval_examples=("post this comment to reddit now",),
    ),
    "reddit.post.create": BrowserWorkflow(
        workflow_id="reddit.post.create",
        description="Create a Reddit self-post in a subreddit.",
        classification="write",
        approval_level="explicit",
        router_command=None,
        default_url=None,
        audit_action="reddit_post_create",
        approval_examples=("post this to reddit now",),
    ),
}

try:
    from local_extension_loader import apply_local_extension_hook

    apply_local_extension_hook(
        "register_browser_workflows",
        _WORKFLOWS,
        workflow_type=BrowserWorkflow,
    )
except ImportError:
    pass


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
    """True only when the operator's message ENDS with an approval phrase.

    Trailing-token check (not substring-anywhere): a post/comment BODY that
    happens to contain the approval phrase mid-text must NOT auto-approve. The
    operator confirms by appending the phrase at the very end of their message,
    mirroring the reddit write path ("<body> post this to reddit now").
    """

    normalized = _normalize_approval_text(user_text)
    if not normalized:
        return False
    return any(
        _normalize_approval_text(example) and normalized.endswith(_normalize_approval_text(example))
        for example in workflow.approval_examples
    )


def _normalize_approval_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _extract_http_url(text: str) -> str | None:
    match = re.search(r"https?://[^\s\"']+", text)
    return match.group(0) if match else None
