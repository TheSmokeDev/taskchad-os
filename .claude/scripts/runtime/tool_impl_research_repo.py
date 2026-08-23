"""Bounded read tools for research, repositories, GitHub, and visible Chrome."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from typing import Any

_logger = logging.getLogger(__name__)
_MAX_RESULT_CHARS = 12_000
_DEFAULT_CDP_PORT = 18_222
_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SPREADSHEET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
_STATE_VALUES = frozenset({"open", "closed", "all"})


def _truncate(value: Any, limit: int = _MAX_RESULT_CHARS) -> str:
    text = value if isinstance(value, str) else json.dumps(
        value, indent=2, ensure_ascii=False, default=str
    )
    try:
        from security.redact import redact_sensitive_text

        text = redact_sensitive_text(text)
    except Exception:  # noqa: BLE001 — bounded output still returns if redactor is unavailable
        _logger.error("read-tool output redaction failed", exc_info=True)
        return "unavailable: output could not be safely redacted"
    return text if len(text) <= limit else text[:limit] + f"\n[TRUNCATED — {len(text) - limit} more chars]"


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def _cdp_port() -> int:
    import os

    raw = os.getenv("UPWORK_CDP_PORT") or os.getenv("HOMIE_CDP_PORT") or ""
    try:
        return int(raw) if raw.strip() else _DEFAULT_CDP_PORT
    except ValueError:
        return _DEFAULT_CDP_PORT


def _web_search(query: str = "", limit: int = 5, **_: Any) -> str:
    from runtime import tool_impl_seo_geo

    return tool_impl_seo_geo._seo_exa_search(query=query, limit=limit)


def _web_extract(url: str = "", max_characters: int = 4_000, **_: Any) -> str:
    from runtime import tool_impl_seo_geo

    return tool_impl_seo_geo._seo_exa_fetch(urls=[url], max_characters=max_characters)


def _firecrawl_scrape(url: str = "", only_main_content: bool = True, **_: Any) -> str:
    from runtime import tool_impl_seo_geo

    return tool_impl_seo_geo._firecrawl_scrape(
        url=url, only_main_content=only_main_content
    )


def _firecrawl_search(query: str = "", limit: int = 5, **_: Any) -> str:
    normalized = str(query or "").strip()
    if not normalized:
        return "error: query is required"
    from runtime import tool_impl_seo_geo

    result = tool_impl_seo_geo._firecrawl_request(
        "/search",
        {
            "query": normalized[:800],
            "limit": _bounded_int(limit, default=5, minimum=1, maximum=10),
            "scrapeOptions": {"formats": ["markdown"], "onlyMainContent": True},
        },
    )
    if isinstance(result, str):
        return result
    rows = result.get("data") or result.get("results") or []
    if not isinstance(rows, list):
        rows = []
    bounded = []
    for row in rows[:10]:
        if isinstance(row, dict):
            bounded.append({
                "title": str(row.get("title") or "")[:300],
                "url": str(row.get("url") or "")[:2_000],
                "description": str(row.get("description") or "")[:1_000],
                "markdown": str(row.get("markdown") or "")[:3_000],
            })
    return _truncate({"query": normalized, "results": bounded})


def _validated_repo(repo: str) -> str | None:
    normalized = str(repo or "").strip()
    return normalized if _GITHUB_REPO_RE.fullmatch(normalized) else None


def _gh_json(args: list[str]) -> str:
    binary = shutil.which("gh")
    if not binary:
        return "unavailable: GitHub CLI is not installed"
    try:
        completed = subprocess.run(
            [binary, *args], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30, shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unavailable: GitHub read failed ({type(exc).__name__})"
    if completed.returncode != 0:
        return f"unavailable: GitHub read returned exit {completed.returncode}"
    try:
        return _truncate(json.loads(completed.stdout or "null"))
    except ValueError:
        return "unavailable: GitHub returned invalid JSON"


def _gh_issue_view(repo: str = "", number: int = 0, **_: Any) -> str:
    target = _validated_repo(repo)
    number = _bounded_int(number, default=0, minimum=0, maximum=2_147_483_647)
    if not target or not number:
        return "error: repo must be owner/name and number must be positive"
    return _gh_json(["issue", "view", str(number), "--repo", target, "--json", "number,title,state,author,labels,body,url,updatedAt"])


def _gh_issue_list(repo: str = "", state: str = "open", limit: int = 20, **_: Any) -> str:
    target, state = _validated_repo(repo), str(state or "open").lower()
    if not target or state not in _STATE_VALUES:
        return "error: repo must be owner/name and state must be open, closed, or all"
    return _gh_json(["issue", "list", "--repo", target, "--state", state, "--limit", str(_bounded_int(limit, default=20, minimum=1, maximum=100)), "--json", "number,title,state,author,labels,url,updatedAt"])


def _gh_pr_view(repo: str = "", number: int = 0, **_: Any) -> str:
    target = _validated_repo(repo)
    number = _bounded_int(number, default=0, minimum=0, maximum=2_147_483_647)
    if not target or not number:
        return "error: repo must be owner/name and number must be positive"
    return _gh_json(["pr", "view", str(number), "--repo", target, "--json", "number,title,state,author,baseRefName,headRefName,mergeable,body,url,updatedAt,statusCheckRollup"])


def _gh_pr_list(repo: str = "", state: str = "open", limit: int = 20, **_: Any) -> str:
    target, state = _validated_repo(repo), str(state or "open").lower()
    if not target or state not in _STATE_VALUES:
        return "error: repo must be owner/name and state must be open, closed, or all"
    return _gh_json(["pr", "list", "--repo", target, "--state", state, "--limit", str(_bounded_int(limit, default=20, minimum=1, maximum=100)), "--json", "number,title,state,author,baseRefName,headRefName,url,updatedAt"])


def _gh_run_list(repo: str = "", limit: int = 20, **_: Any) -> str:
    target = _validated_repo(repo)
    if not target:
        return "error: repo must be owner/name"
    return _gh_json(["run", "list", "--repo", target, "--limit", str(_bounded_int(limit, default=20, minimum=1, maximum=100)), "--json", "databaseId,name,displayTitle,status,conclusion,event,headBranch,headSha,url,createdAt,updatedAt"])


def _repo_search(repo: str = "", query: str = "", limit: int = 50, **_: Any) -> str:
    query = str(query or "").strip()
    if not str(repo or "").strip() or not query:
        return "error: repo slug and query are required"
    try:
        from cofounder.repos import resolve_repo
        root = resolve_repo(str(repo).strip()).local_path
    except Exception as exc:  # noqa: BLE001
        return f"error: tracked repository could not be resolved ({type(exc).__name__})"
    if root is None:
        return "error: greenfield repositories have no searchable local path"
    try:
        root = root.expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return "error: tracked repository path is unavailable"
    if not root.is_dir():
        return "error: tracked repository path is not a directory"
    binary = shutil.which("rg")
    if not binary:
        return "unavailable: ripgrep is not installed"
    try:
        completed = subprocess.run(
            [
                binary,
                "-n",
                "--no-heading",
                "--color",
                "never",
                "--glob",
                "!.env*",
                "--glob",
                "!**/.env*",
                "--glob",
                "!*.pem",
                "--glob",
                "!*.key",
                "--glob",
                "!**/credentials/**",
                "--glob",
                "!**/tokens/**",
                "--",
                query,
                ".",
            ],
            cwd=str(root), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=20, shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unavailable: repository search failed ({type(exc).__name__})"
    if completed.returncode not in {0, 1}:
        return f"unavailable: repository search returned exit {completed.returncode}"
    rows = (completed.stdout or "").splitlines()
    cap = _bounded_int(limit, default=50, minimum=1, maximum=200)
    suffix = f"\n[TRUNCATED — {len(rows) - cap} more matches]" if len(rows) > cap else ""
    return _truncate("\n".join(rows[:cap]) + suffix) if rows else "No repository matches."


def _sheets_read(
    spreadsheet_id: str = "",
    range_notation: str = "",
    max_rows: int = 100,
    **_: Any,
) -> str:
    """Read one bounded spreadsheet range through the direct integration."""
    spreadsheet_id = str(spreadsheet_id or "").strip()
    range_notation = str(range_notation or "").strip()
    if not _SPREADSHEET_ID_RE.fullmatch(spreadsheet_id):
        return "error: spreadsheet_id is invalid"
    if len(range_notation) > 200 or "\n" in range_notation or "\r" in range_notation:
        return "error: range_notation is invalid"
    try:
        from integrations.sheets_api import (
            format_spreadsheet_for_context,
            read_spreadsheet,
        )

        data = read_spreadsheet(
            spreadsheet_id,
            range_notation=range_notation,
            max_rows=_bounded_int(max_rows, default=100, minimum=1, maximum=100),
        )
        return _truncate(format_spreadsheet_for_context(data, max_chars=8_000))
    except Exception as exc:  # noqa: BLE001
        return f"unavailable: Sheets read failed ({type(exc).__name__})"


def _browser_navigate(url: str = "", **_: Any) -> str:
    from runtime.tool_impl_seo_geo import _public_http_url

    target, error = _public_http_url(url)
    if error or target is None:
        return f"error: {error or 'invalid public URL'}"
    try:
        import browser_control
        opened = browser_control.run_agent_browser(["open", target], port=_cdp_port(), timeout=30, reap_on_timeout=True)
        if not opened.ok:
            return "error: visible-browser navigation failed"
        snapshot = browser_control.run_agent_browser(["snapshot"], port=_cdp_port(), timeout=30, reap_on_timeout=True)
        return _truncate(f"Opened {target}.\n\n{snapshot.stdout.strip()}") if snapshot.ok else f"Opened {target}, but the read-back snapshot failed."
    except Exception as exc:  # noqa: BLE001
        return f"error: visible-browser navigation failed ({type(exc).__name__})"


def _browser_console(**_: Any) -> str:
    try:
        import browser_control
        result = browser_control.run_agent_browser(["console"], port=_cdp_port(), timeout=20, reap_on_timeout=True)
        return _truncate(result.stdout.strip() or "No browser console messages.") if result.ok else "error: browser console read failed"
    except Exception as exc:  # noqa: BLE001
        return f"error: browser console read failed ({type(exc).__name__})"


_SPECS: tuple[tuple[str, str, str, dict[str, Any], Any], ...] = (
    ("web_search", "research_read", "Search current public web sources through the existing Exa read client.", {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}, _web_search),
    ("web_extract", "research_read", "Extract one public HTTP(S) source through the existing Exa read client.", {"type": "object", "properties": {"url": {"type": "string"}, "max_characters": {"type": "integer"}}, "required": ["url"]}, _web_extract),
    ("firecrawl_scrape", "research_read", "Extract one public page through the configured Firecrawl read client.", {"type": "object", "properties": {"url": {"type": "string"}, "only_main_content": {"type": "boolean"}}, "required": ["url"]}, _firecrawl_scrape),
    ("firecrawl_search", "research_read", "Search public web sources through the configured Firecrawl read client.", {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}, _firecrawl_search),
    ("exa_search", "research_read", "Search current public web sources through Exa.", {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}, _web_search),
    ("gh_issue_view", "repo_read", "Read one GitHub issue by repository and number.", {"type": "object", "properties": {"repo": {"type": "string"}, "number": {"type": "integer"}}, "required": ["repo", "number"]}, _gh_issue_view),
    ("gh_issue_list", "repo_read", "List bounded GitHub issues.", {"type": "object", "properties": {"repo": {"type": "string"}, "state": {"type": "string", "enum": ["open", "closed", "all"]}, "limit": {"type": "integer"}}, "required": ["repo"]}, _gh_issue_list),
    ("gh_pr_view", "repo_read", "Read one GitHub pull request and its checks.", {"type": "object", "properties": {"repo": {"type": "string"}, "number": {"type": "integer"}}, "required": ["repo", "number"]}, _gh_pr_view),
    ("gh_pr_list", "repo_read", "List bounded GitHub pull requests.", {"type": "object", "properties": {"repo": {"type": "string"}, "state": {"type": "string", "enum": ["open", "closed", "all"]}, "limit": {"type": "integer"}}, "required": ["repo"]}, _gh_pr_list),
    ("gh_run_list", "repo_read", "List bounded GitHub Actions runs.", {"type": "object", "properties": {"repo": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["repo"]}, _gh_run_list),
    ("repo_search", "repo_read", "Search a repository resolved from the tracked repository index.", {"type": "object", "properties": {"repo": {"type": "string"}, "query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["repo", "query"]}, _repo_search),
    ("browser_navigate", "browser_read", "Navigate the existing visible browser to one public URL and return a read-back snapshot. Never types, clicks, or submits.", {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}, _browser_navigate),
    ("browser_console", "browser_read", "Read console messages from the existing visible browser tab.", {"type": "object", "properties": {}}, _browser_console),
)


def register_tools() -> int:
    from runtime import tool_registry

    registered = 0
    try:
        tool_registry.register_tool(
            "sheets_read",
            "Read one bounded range from a Google Sheet using the configured direct integration.",
            toolset="business_read",
            parameters={
                "type": "object",
                "properties": {
                    "spreadsheet_id": {"type": "string"},
                    "range_notation": {"type": "string"},
                    "max_rows": {"type": "integer", "maximum": 100},
                },
                "required": ["spreadsheet_id"],
            },
            handler=_sheets_read,
            effect="read",
            integration_action="sheets.read",
            elevatable=True,
        )
        registered += 1
    except Exception:  # noqa: BLE001
        _logger.warning("failed to register Sheets read tool", exc_info=True)
    for name, toolset, description, parameters, handler in _SPECS:
        try:
            tool_registry.register_tool(name, description, toolset=toolset, parameters=parameters, handler=handler, effect="read", elevatable=True)
            registered += 1
        except Exception:  # noqa: BLE001
            _logger.warning("failed to register read tool %r", name, exc_info=True)
    return registered


__all__ = ["register_tools"]
