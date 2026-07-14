from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS.parent / "chat"))

from browser_workflows import (  # type: ignore[import-not-found]  # noqa: E402
    get_browser_workflow,
    list_browser_workflows,
    require_browser_workflow_permission,
)


def test_registry_contains_initial_phase_2_workflows() -> None:
    workflow_ids = {workflow.workflow_id for workflow in list_browser_workflows()}

    assert {
        "browser.status",
        "browser.tabs",
        "browser.open",
        "browser.snapshot",
        "browserops.capabilities",
        "browserops.guide",
        "browserops.context",
        "browser.viewer.status",
        "browser.viewer.screenshot",
        "browser.viewer.stream_enable",
        "browser.viewer.stream_disable",
        "linkedin.profile.open",
        "linkedin.profile.edit",
        "linkedin.post.create",
        "linkedin.connection.request",
        "x.post.create",
    }.issubset(workflow_ids)
    edit_workflow = get_browser_workflow("linkedin.profile.edit")
    assert edit_workflow is not None
    assert edit_workflow.classification == "write"


def test_read_workflows_pass_without_approval() -> None:
    for workflow_id in (
        "browser.status",
        "browserops.capabilities",
        "browserops.guide",
        "browserops.context",
        "browser.viewer.status",
        "browser.viewer.screenshot",
        "browser.viewer.stream_enable",
        "browser.viewer.stream_disable",
    ):
        decision = require_browser_workflow_permission(workflow_id, "show browser status")
        assert decision.allowed is True
        assert decision.outcome == "allowed"


def test_navigation_requires_absolute_http_url() -> None:
    blocked = require_browser_workflow_permission(
        "browser.open",
        "open this",
        target_url="file:///~/secrets.html",
    )
    allowed = require_browser_workflow_permission(
        "browser.open",
        "open this",
        target_url="https://example.com/path?secret=1#top",
    )

    assert blocked.allowed is False
    assert blocked.outcome == "blocked"
    assert allowed.allowed is True
    assert allowed.target_url == "https://example.com/path"


def test_navigation_can_extract_http_url_from_user_text() -> None:
    decision = require_browser_workflow_permission(
        "browser.open",
        "open https://example.com/path?secret=1#top",
    )

    assert decision.allowed is True
    assert decision.target_url == "https://example.com/path"


def test_write_workflows_block_without_explicit_approval() -> None:
    for text in (
        "can we update my profile?",
        "draft a post",
        "see what my profile looks like",
    ):
        decision = require_browser_workflow_permission("linkedin.profile.edit", text)
        assert decision.allowed is False
        assert decision.outcome == "blocked"
        assert "requires explicit approval" in decision.reason


def test_write_workflow_passes_with_explicit_approval() -> None:
    decision = require_browser_workflow_permission(
        "linkedin.profile.edit",
        "approve LinkedIn profile edit",
    )

    assert decision.allowed is True
    assert decision.outcome == "allowed"


def test_unknown_workflow_is_default_denied() -> None:
    decision = require_browser_workflow_permission("browser.cookie.dump", "do it")

    assert decision.allowed is False
    assert decision.outcome == "blocked"
    assert "Unknown browser workflow" in decision.reason


def test_registry_contains_reddit_workflows() -> None:
    workflow_ids = {workflow.workflow_id for workflow in list_browser_workflows()}
    assert {
        "reddit.research",
        "reddit.comment.create",
        "reddit.post.create",
    }.issubset(workflow_ids)
    research = get_browser_workflow("reddit.research")
    assert research is not None and research.classification == "read"
    comment = get_browser_workflow("reddit.comment.create")
    assert comment is not None and comment.classification == "write"
    post = get_browser_workflow("reddit.post.create")
    assert post is not None and post.classification == "write"


def test_reddit_research_passes_without_approval() -> None:
    decision = require_browser_workflow_permission(
        "reddit.research", "research california sr-22 insurance"
    )
    assert decision.allowed is True
    assert decision.outcome == "allowed"


def test_reddit_writes_block_without_explicit_approval() -> None:
    for workflow_id, text in (
        ("reddit.comment.create", "comment https://reddit.com/r/x/comments/1/ | helpful reply"),
        ("reddit.post.create", "post Insurance | Title | body text"),
    ):
        decision = require_browser_workflow_permission(workflow_id, text)
        assert decision.allowed is False
        assert decision.outcome == "blocked"
        assert "requires explicit approval" in decision.reason


def test_reddit_writes_pass_with_explicit_approval() -> None:
    comment = require_browser_workflow_permission(
        "reddit.comment.create",
        "comment https://reddit.com/r/x/comments/1/ | reply post this comment to reddit now",
    )
    assert comment.allowed is True
    assert comment.outcome == "allowed"
    post = require_browser_workflow_permission(
        "reddit.post.create",
        "post Insurance | Title | body post this to reddit now",
    )
    assert post.allowed is True
    assert post.outcome == "allowed"


# ── Social-write (Phase 1) — router_command wiring + NM1 trailing-token gate ──


def test_linkedin_write_workflows_have_router_commands() -> None:
    post = get_browser_workflow("linkedin.post.create")
    connect = get_browser_workflow("linkedin.connection.request")
    assert post is not None and post.router_command == "/linkedin_post"
    assert connect is not None and connect.router_command == "/linkedin_connect"
    assert post.classification == "write" and post.approval_level == "explicit"
    assert connect.classification == "write" and connect.approval_level == "explicit"


def test_linkedin_writes_block_without_explicit_approval() -> None:
    for workflow_id, text in (
        ("linkedin.post.create", "https://www.linkedin.com/feed/ | here is my body"),
        ("linkedin.connection.request", "https://www.linkedin.com/in/x | nice to connect"),
    ):
        decision = require_browser_workflow_permission(workflow_id, text)
        assert decision.allowed is False
        assert decision.outcome == "blocked"
        assert "requires explicit approval" in decision.reason


def test_linkedin_writes_pass_with_trailing_approval() -> None:
    post = require_browser_workflow_permission(
        "linkedin.post.create",
        "https://www.linkedin.com/feed/ | here is my body post this to linkedin now",
    )
    assert post.allowed is True
    connect = require_browser_workflow_permission(
        "linkedin.connection.request",
        "https://www.linkedin.com/in/x | hi send this linkedin connection request now",
    )
    assert connect.allowed is True


def test_x_write_requires_explicit_approval() -> None:
    blocked = require_browser_workflow_permission(
        "x.post.create", "a Primo crypto and AI post"
    )
    assert blocked.allowed is False
    assert blocked.outcome == "blocked"

    allowed = require_browser_workflow_permission(
        "x.post.create", "a Primo crypto and AI post post this to x now"
    )
    assert allowed.allowed is True
    assert allowed.outcome == "allowed"


def test_approval_phrase_embedded_in_body_does_not_auto_approve() -> None:
    """NM1: the phrase only counts as a TRAILING confirmation token — a post body
    that contains it mid-text (with no trailing confirmation) stays BLOCKED."""
    decision = require_browser_workflow_permission(
        "linkedin.post.create",
        "https://www.linkedin.com/feed/ | I love how 'post this to linkedin now' reads as copy",
    )
    assert decision.allowed is False
    assert decision.outcome == "blocked"


def test_gate_with_isolated_approved_flag_and_empty_user_text() -> None:
    """THE FIX contract at the gate level: handlers now pass user_text="" plus an
    `approved` flag computed from the STRUCTURALLY-ISOLATED confirmation segment.
    The gate's own `.endswith` scan never sees the body. Both write workflows are
    allowed only when `approved=True`."""
    for workflow_id in ("linkedin.post.create", "reddit.comment.create", "reddit.post.create"):
        blocked = require_browser_workflow_permission(workflow_id, "", approved=False)
        assert blocked.allowed is False
        assert blocked.outcome == "blocked"
        allowed = require_browser_workflow_permission(workflow_id, "", approved=True)
        assert allowed.allowed is True
        assert allowed.outcome == "allowed"


def test_gate_empty_user_text_with_body_phrase_can_never_approve_via_scan() -> None:
    """A body that ends with the phrase can NEVER reach the gate as user_text under
    the new handler contract — the handler passes "" and decides approval on the
    isolated segment. Proven here: empty user_text + approved=False stays BLOCKED
    regardless of what the (now-unseen) body contained."""
    decision = require_browser_workflow_permission(
        "reddit.comment.create", "", approved=False
    )
    assert decision.allowed is False
    assert decision.outcome == "blocked"
