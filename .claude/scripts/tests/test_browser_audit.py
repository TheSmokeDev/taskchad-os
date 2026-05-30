from __future__ import annotations

import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS.parent / "chat"))

from browser_audit import (  # type: ignore[import-not-found]  # noqa: E402
    append_browser_audit_record,
    normalize_surface,
)


def test_audit_record_redacts_target_url_query_and_fragment(tmp_path: Path) -> None:
    log_path = tmp_path / "browser_actions.jsonl"

    record = append_browser_audit_record(
        command="/browser open",
        workflow_id="browser.open",
        action="browser_open",
        outcome="allowed",
        reason="opening https://example.com/path?token=secret#frag",
        cdp_port=9222,
        cdp_reachable=True,
        surface="web",
        session_id="cli:local",
        target_url="https://example.com/path?token=secret#frag",
        path=log_path,
    )

    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0] == record
    assert rows[0]["target_url"] == "https://example.com/path"
    assert rows[0]["reason"] == "opening https://example.com/path"
    assert rows[0]["surface"] == "mission_control"
    assert "secret" not in json.dumps(rows[0])
    assert "#frag" not in json.dumps(rows[0])


def test_audit_record_redacts_urls_in_command(tmp_path: Path) -> None:
    log_path = tmp_path / "browser_actions.jsonl"

    append_browser_audit_record(
        command="/browser https://example.com/path?token=secret#frag",
        workflow_id=None,
        outcome="failed",
        reason="unknown command",
        path=log_path,
    )

    row = json.loads(log_path.read_text(encoding="utf-8"))
    assert row["command"] == "/browser https://example.com/path"
    assert "secret" not in json.dumps(row)
    assert "#frag" not in json.dumps(row)


def test_audit_log_appends_allowed_and_blocked_decisions(tmp_path: Path) -> None:
    log_path = tmp_path / "browser_actions.jsonl"

    append_browser_audit_record(
        command="/browser status",
        workflow_id="browser.status",
        outcome="allowed",
        reason="Browser workflow allowed.",
        cdp_port=9222,
        cdp_reachable=True,
        surface="cli",
        path=log_path,
    )
    append_browser_audit_record(
        command="/linkedin_profile edit",
        workflow_id="linkedin.profile.edit",
        outcome="blocked",
        reason="write workflow requires explicit approval",
        cdp_port=9222,
        cdp_reachable=True,
        surface="telegram",
        path=log_path,
    )

    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [row["outcome"] for row in rows] == ["allowed", "blocked"]
    assert rows[1]["workflow_id"] == "linkedin.profile.edit"


def test_surface_normalization_defaults_unknown() -> None:
    assert normalize_surface("WEB") == "mission_control"
    assert normalize_surface("telegram") == "telegram"
    assert normalize_surface("mastodon") == "unknown"
