"""Capability substance and safety tests for generic persona read tools."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from runtime import persona_tools, tool_impl, tool_impl_research_repo, tool_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    saved = dict(tool_registry._REGISTRY)
    tool_registry._REGISTRY.clear()
    yield
    tool_registry._REGISTRY.clear()
    tool_registry._REGISTRY.update(saved)


def test_registers_every_declared_gap_under_its_structural_owner():
    assert tool_impl_research_repo.register_tools() == 14
    expected = {
        "web_search": "research_read",
        "web_extract": "research_read",
        "firecrawl_scrape": "research_read",
        "firecrawl_search": "research_read",
        "exa_search": "research_read",
        "gh_issue_view": "repo_read",
        "gh_issue_list": "repo_read",
        "gh_pr_view": "repo_read",
        "gh_pr_list": "repo_read",
        "gh_run_list": "repo_read",
        "repo_search": "repo_read",
        "browser_navigate": "browser_read",
        "browser_console": "browser_read",
        "sheets_read": "business_read",
    }
    for name, owner in expected.items():
        entry = tool_registry.get_entry(name)
        assert entry is not None
        assert entry.toolset == owner
        assert entry.effect == "read"
        assert entry.handler is not None


def test_ai_engineering_payload_carries_exact_definitions_and_real_dispatch(monkeypatch):
    monkeypatch.setattr(
        tool_impl_research_repo,
        "_gh_json",
        lambda args: json.dumps({"called": args[:2]}),
    )
    tool_impl.register_tools()
    payload = persona_tools.build_persona_tool_payload(
        "ai-engineer",
        {"toolsets": ["safe_core", "ai_engineering"], "tools": []},
    )
    assert payload is not None
    definitions, dispatch = payload
    names = {(row.get("function") or {}).get("name") for row in definitions}
    assert {
        "web_search", "firecrawl_scrape", "gh_issue_view", "repo_search",
        "browser_navigate", "browser_console",
        "sheets_read",
    }.issubset(names)
    result = json.loads(dispatch("gh_issue_list", {"repo": "thehomie-framework/thehomie"}))
    assert result["called"] == ["issue", "list"]


def test_github_uses_argument_array_read_verb_and_never_shell(monkeypatch):
    observed = {}

    def fake_run(argv, **kwargs):
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, "[]", "")

    monkeypatch.setattr(tool_impl_research_repo.shutil, "which", lambda _: "gh")
    monkeypatch.setattr(tool_impl_research_repo.subprocess, "run", fake_run)
    assert tool_impl_research_repo._gh_issue_list("owner/repo") == "[]"
    assert observed["argv"][:3] == ["gh", "issue", "list"]
    assert observed["kwargs"]["shell"] is False


def test_github_failure_does_not_echo_provider_stderr(monkeypatch):
    monkeypatch.setattr(tool_impl_research_repo.shutil, "which", lambda _: "gh")
    monkeypatch.setattr(
        tool_impl_research_repo.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "token=secret"),
    )
    result = tool_impl_research_repo._gh_pr_list("owner/repo")
    assert "secret" not in result
    assert "exit 1" in result


def test_github_body_is_redacted_at_output_boundary(monkeypatch):
    synthetic = "sk-" + "test-secret-value"
    monkeypatch.setattr(tool_impl_research_repo.shutil, "which", lambda _: "gh")
    monkeypatch.setattr(
        tool_impl_research_repo.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], 0, json.dumps({"body": f"OPENAI_API_KEY={synthetic}"}), ""
        ),
    )
    result = tool_impl_research_repo._gh_issue_view("owner/repo", 1)
    assert synthetic not in result
    assert "***" in result


def test_browser_navigation_rejects_private_target_before_browser_import():
    assert "local/private" in tool_impl_research_repo._browser_navigate(
        "http://127.0.0.1/private"
    )


def test_repo_search_is_confined_to_resolved_tracked_repo(tmp_path, monkeypatch):
    target = tmp_path / "repo"
    target.mkdir()
    (target / "a.txt").write_text("needle", encoding="utf-8")

    class Resolution:
        local_path = target

    from cofounder import repos

    monkeypatch.setattr(repos, "resolve_repo", lambda slug: Resolution())
    monkeypatch.setattr(tool_impl_research_repo.shutil, "which", lambda _: "rg")
    result = tool_impl_research_repo._repo_search("tracked", "needle")
    assert "a.txt" in result


def test_repo_search_excludes_credential_paths_and_redacts_matches(tmp_path, monkeypatch):
    synthetic = "sk-" + "test-secret-value"
    target = tmp_path / "repo"
    target.mkdir()

    class Resolution:
        local_path = target

    from cofounder import repos

    observed = {}

    def fake_run(argv, **kwargs):
        observed["argv"] = argv
        return subprocess.CompletedProcess(
            argv, 0, f"safe.py:1:OPENAI_API_KEY={synthetic}", ""
        )

    monkeypatch.setattr(repos, "resolve_repo", lambda slug: Resolution())
    monkeypatch.setattr(tool_impl_research_repo.shutil, "which", lambda _: "rg")
    monkeypatch.setattr(tool_impl_research_repo.subprocess, "run", fake_run)
    result = tool_impl_research_repo._repo_search("tracked", "OPENAI_API_KEY")
    command = " ".join(observed["argv"])
    assert "!.env*" in command
    assert "!**/credentials/**" in command
    assert synthetic not in result
    assert "***" in result


def test_web_extract_reuses_public_url_guard():
    assert "local/private" in tool_impl_research_repo._web_extract(
        "http://127.0.0.1/private"
    )


def test_sheets_read_is_bounded_and_maps_to_integration_action(monkeypatch):
    tool_impl_research_repo.register_tools()
    entry = tool_registry.get_entry("sheets_read")
    assert entry is not None
    assert entry.integration_action == "sheets.read"
    assert entry.toolset == "business_read"
    assert tool_impl_research_repo._sheets_read("bad") == "error: spreadsheet_id is invalid"
