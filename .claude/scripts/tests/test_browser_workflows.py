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
