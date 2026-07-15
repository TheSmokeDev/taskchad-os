"""Tests for github_signal.fetch + github_signal.trending — no network.

urllib is monkeypatched at module level; trending parses inline HTML fixtures.
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from github_signal import fetch as fetch_mod  # noqa: E402
from github_signal import trending as trending_mod  # noqa: E402
from github_signal.fetch import FetchError, fetch_starred  # noqa: E402
from github_signal.trending import (  # noqa: E402
    filter_by_keywords,
    parse_trending_html,
)


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _star_item(i: int) -> dict:
    return {
        "starred_at": f"2026-07-{(i % 28) + 1:02d}T00:00:00Z",
        "repo": {
            "full_name": f"owner/repo{i}",
            "description": f"desc {i}",
            "language": "Python",
            "topics": ["ai"],
            "html_url": f"https://github.com/owner/repo{i}",
            "pushed_at": "2026-07-01T00:00:00Z",
            "stargazers_count": i,
        },
    }


# ── fetch_starred ──────────────────────────────────────────


def test_pagination_merges_full_and_partial_pages(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")
    seen_requests: list[urllib.request.Request] = []

    def fake_urlopen(req, timeout=None):
        seen_requests.append(req)
        page = int(req.full_url.split("page=")[-1])
        count = 100 if page == 1 else 40
        offset = 0 if page == 1 else 100
        payload = json.dumps([_star_item(offset + i) for i in range(count)])
        return _FakeResponse(payload.encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    items = fetch_starred()

    assert len(items) == 140
    assert len(seen_requests) == 2  # partial page 2 stops the loop
    assert items[0]["full_name"] == "owner/repo0"
    assert items[0]["starred_at"].endswith("Z")
    # star+json Accept variant + Bearer auth on every request
    assert seen_requests[0].get_header("Accept") == "application/vnd.github.star+json"
    assert seen_requests[0].get_header("Authorization") == "Bearer ghp_test_token"


def test_http_401_raises_fetcherror_with_token_hint(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 401, "Unauthorized", None, io.BytesIO(b"")
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(FetchError, match="GITHUB_TOKEN"):
        fetch_starred()


def test_http_500_and_network_errors_raise_fetcherror(monkeypatch):
    def fake_500(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 500, "Server Error", None, io.BytesIO(b"")
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_500)
    with pytest.raises(FetchError, match="HTTP 500"):
        fetch_starred()

    def fake_urlerror(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlerror)
    with pytest.raises(FetchError, match="network error"):
        fetch_starred()


def test_invalid_json_raises_fetcherror(monkeypatch):
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResponse(b"<html>rate limited</html>"),
    )
    with pytest.raises(FetchError, match="invalid JSON"):
        fetch_starred()


def test_empty_star_list_returns_empty(monkeypatch):
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(b"[]")
    )
    assert fetch_starred() == []


def test_flatten_handles_plain_repo_shape_without_wrapper():
    plain = {
        "full_name": "owner/plain",
        "description": None,
        "html_url": "https://github.com/owner/plain",
    }
    flat = fetch_mod._flatten(plain)
    assert flat is not None
    assert flat["full_name"] == "owner/plain"
    assert flat["description"] == ""
    assert flat["starred_at"] == ""
    assert fetch_mod._flatten({"repo": {}}) is None  # no full_name → dropped


# ── trending parser ────────────────────────────────────────

_GOLDEN_HTML = """
<html><body>
<article class="Box-row">
  <h2 class="h3 lh-condensed"><a href="/openai/superagent">openai / superagent</a></h2>
  <p class="col-9 color-fg-muted my-1 pr-4">An LLM agent framework for everything.</p>
  <div>
    <a href="/openai/superagent/stargazers">12,345</a>
    <span class="d-inline-block ml-0 mr-3"><span itemprop="programmingLanguage">Python</span></span>
    <span class="d-inline-block float-sm-right">1,234 stars today</span>
  </div>
</article>
<article class="Box-row">
  <h2 class="h3"><a href="/rails/rails">rails / rails</a></h2>
  <p class="col-9 color-fg-muted my-1 pr-4">A web-application framework.</p>
  <div><a href="/rails/rails/stargazers">55,000</a></div>
</article>
<article class="Box-row">
  <div>broken card with no h2 repo link</div>
</article>
</body></html>
"""


def test_trending_parser_extracts_repo_cards():
    repos = parse_trending_html(_GOLDEN_HTML)
    assert len(repos) == 2  # broken card dropped
    first = repos[0]
    assert first["full_name"] == "openai/superagent"
    assert "LLM agent framework" in first["description"]
    assert first["stars"] == "12345"
    assert first["language"] == "Python"
    assert first["stars_today"] == "1234"
    assert repos[1]["full_name"] == "rails/rails"


def test_trending_parser_malformed_html_returns_empty():
    assert parse_trending_html("") == []
    assert parse_trending_html("<div><p>not a trending page</p></div>") == []
    assert parse_trending_html("<article class='Box-row'><h2>") == []


def test_fetch_trending_network_failure_returns_empty(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert trending_mod.fetch_trending() == []


def test_keyword_filter_case_insensitive_over_name_and_description():
    items = [
        {"full_name": "openai/superagent", "description": "An LLM framework"},
        {"full_name": "rails/rails", "description": "A web framework"},
        {"full_name": "foo/bar", "description": "Voice AGENT toolkit"},
    ]
    hits = filter_by_keywords(items, ["llm", "agent"])
    assert [i["full_name"] for i in hits] == ["openai/superagent", "foo/bar"]
    # empty keyword list = no filtering
    assert filter_by_keywords(items, []) == items
