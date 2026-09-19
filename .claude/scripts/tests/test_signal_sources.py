"""Tests for the shared, read-only authority research transports."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from integrations.research_sources import (  # noqa: E402
    ResearchSourcesClient,
    classify_public_source,
)


class _Response:
    def __init__(self, payload, *, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"unexpected HTTP status {self.status_code}")

    def json(self):
        return self._payload


def test_exa_results_are_normalized_and_bounded():
    client = ResearchSourcesClient()
    raw = json.dumps(
        {
            "results": [
                {
                    "title": "Official AI search update",
                    "url": "https://developers.google.com/search/docs/update",
                    "description": "AI search citation documentation",
                    "publishedDate": "2026-09-02T00:00:00Z",
                },
                {
                    "title": "Unsafe",
                    "url": "http://127.0.0.1/private",
                    "description": "must be dropped",
                },
            ]
        }
    )
    with patch.object(client, "_exa_mcp_call", return_value=raw):
        documents = client.exa_search("AI search updates", lane="platform_changes", limit=2)
    assert len(documents) == 1
    assert documents[0].source_class == "official_documentation"
    assert documents[0].primary_source is True


def test_github_read_combines_metadata_and_latest_release_without_exposing_token(
    monkeypatch,
):
    monkeypatch.setenv("GITHUB_TOKEN", "do-not-return-this-token")
    calls: list[tuple[str, dict]] = []

    def fake_get(url: str, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/releases/latest"):
            return _Response(
                {
                    "name": "v0.1.1",
                    "tag_name": "v0.1.1",
                    "published_at": "2026-09-02T00:00:00Z",
                }
            )
        return _Response(
            {
                "html_url": "https://github.com/your-github-user/geo-skills",
                "description": "Evidence-first GEO tools",
                "topics": ["geo", "ai-search"],
                "updated_at": "2026-09-02T00:00:00Z",
            }
        )

    document = ResearchSourcesClient(http_get=fake_get).github_repository(
        "your-github-user/geo-skills"
    )
    assert document.verified_repository is True
    assert document.repository == "your-github-user/geo-skills"
    assert "v0.1.1" in document.snippet
    assert "do-not-return-this-token" not in repr(document)
    assert calls[0][1]["headers"]["Authorization"].startswith("Bearer ")


def test_source_classification_keeps_vendor_and_practitioner_non_primary():
    assert classify_public_source("https://ahrefs.com/blog/study") == (
        "vendor_research",
        False,
    )
    assert classify_public_source("https://example.substack.com/p/report") == (
        "practitioner_self_report",
        False,
    )
