"""Tests for the canonical direct-integration capability policy."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

from integrations.capabilities import (
    IntegrationPolicyError,
    get_integration_action,
    get_integration_actions,
    require_integration_action,
)


def _load_query_module():
    query_path = (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "direct-integrations"
        / "scripts"
        / "query.py"
    )
    spec = importlib.util.spec_from_file_location(
        "direct_integrations_query_under_test",
        query_path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_policy_declares_core_mutating_actions() -> None:
    actions = {action.id: action for action in get_integration_actions()}

    assert actions["slack.send"].effect == "external_post"
    assert set(actions["slack.send"].exposures) == {"operator_confirmed", "internal"}
    assert actions["sheets.write"].effect == "write"
    assert actions["gmail.archive"].effect == "archive"
    assert actions["outlook.send_email"].effect == "send"


def test_policy_allows_reads_and_blocks_wrong_surface() -> None:
    action = require_integration_action("gmail", "list", surface="model", caller="test")

    assert action.id == "gmail.list"

    with pytest.raises(IntegrationPolicyError, match="not exposed"):
        require_integration_action("sheets", "write", surface="model", caller="test")


def test_policy_blocks_disabled_override() -> None:
    with pytest.raises(IntegrationPolicyError, match="disabled by policy"):
        require_integration_action(
            "slack",
            "send",
            surface="internal",
            caller="test",
            policy_overrides={"slack.send": False},
        )


def test_registry_reports_declared_actions() -> None:
    from integrations.registry import get_declared_actions

    gmail_actions = get_declared_actions("gmail")
    all_actions = get_declared_actions()

    assert any(action.id == "gmail.archive" for action in gmail_actions)
    assert "gmail" in all_actions
    assert "slack" in all_actions


def test_normalizes_cli_slugs_to_canonical_actions() -> None:
    assert get_integration_action("search-console", "top-queries").id == (
        "search_console.top_queries"
    )
    assert get_integration_action("personal-gmail", "unread").id == (
        "personal_gmail.unread"
    )


def test_query_wrapper_maps_mutators_to_operator_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    query = _load_query_module()
    calls: list[tuple[str, str, str | None]] = []

    def fake_require(integration: str, action: str, **kwargs):
        calls.append((integration, action, kwargs.get("surface")))

    monkeypatch.setattr(query, "require_integration_action", fake_require)

    query._require_cli_action("slack", "send")
    query._require_cli_action("sheets", "append")
    query._require_cli_action("gmail", "list")

    assert calls == [
        ("slack", "send", "operator_confirmed"),
        ("sheets", "append", "operator_confirmed"),
        ("gmail", "list", "model"),
    ]


def test_query_wrapper_blocks_before_sheets_write_imports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = _load_query_module()

    def deny(*args, **kwargs):
        raise IntegrationPolicyError("blocked")

    monkeypatch.setattr(query, "require_integration_action", deny)
    args = argparse.Namespace(
        action="write",
        target_id="sheet-id",
        range="Sheet1!A1",
        values='[["value"]]',
        max_rows=500,
    )

    with pytest.raises(IntegrationPolicyError, match="blocked"):
        query.cmd_sheets(args)


def test_slack_send_entrypoint_checks_policy_before_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import integrations.slack_api as slack_api

    def deny(*args, **kwargs):
        raise IntegrationPolicyError("blocked")

    def fail_client():
        pytest.fail("Slack client should not be built when policy denies")

    monkeypatch.setattr(slack_api, "require_integration_action", deny)
    monkeypatch.setattr(slack_api, "get_slack_client", fail_client)

    with pytest.raises(IntegrationPolicyError, match="blocked"):
        slack_api.send_notification("#general", "hello")


def test_sheets_write_entrypoint_checks_policy_before_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import integrations.sheets_api as sheets_api

    def deny(*args, **kwargs):
        raise IntegrationPolicyError("blocked")

    def fail_service():
        pytest.fail("Sheets service should not be built when policy denies")

    monkeypatch.setattr(sheets_api, "require_integration_action", deny)
    monkeypatch.setattr(sheets_api, "get_sheets_service", fail_service)

    with pytest.raises(IntegrationPolicyError, match="blocked"):
        sheets_api.write_spreadsheet("sheet-id", "Sheet1!A1", [["value"]])


def test_gmail_archive_entrypoint_checks_policy_before_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import integrations.gmail as gmail

    def deny(*args, **kwargs):
        raise IntegrationPolicyError("blocked")

    def fail_service():
        pytest.fail("Gmail service should not be built when policy denies")

    monkeypatch.setattr(gmail, "require_integration_action", deny)
    monkeypatch.setattr(gmail, "get_gmail_service", fail_service)

    with pytest.raises(IntegrationPolicyError, match="blocked"):
        gmail.archive_emails(["msg-1"])


def test_outlook_send_entrypoint_checks_policy_before_graph_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import integrations.outlook as outlook

    def deny(*args, **kwargs):
        raise IntegrationPolicyError("blocked")

    def fail_post(*args, **kwargs):
        pytest.fail("Graph POST should not run when policy denies")

    monkeypatch.setattr(outlook, "require_integration_action", deny)
    monkeypatch.setattr(outlook, "_graph_post", fail_post)

    with pytest.raises(IntegrationPolicyError, match="blocked"):
        outlook.send_email("user@example.com", "Subject", "Body")


def test_notifications_block_slack_policy_without_raising(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import notifications

    def deny(*args, **kwargs):
        raise IntegrationPolicyError("blocked")

    monkeypatch.setattr(notifications, "require_integration_action", deny)

    result = notifications.send_slack_notification("Title", "Message")

    assert result is None
    assert "blocked by policy" in capsys.readouterr().out
