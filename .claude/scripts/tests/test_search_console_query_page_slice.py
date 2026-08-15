"""Unit coverage for bounded, finalized GSC query/page evidence slices."""

from __future__ import annotations

import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from integrations import search_console_api  # noqa: E402


class _Request:
    def execute(self):
        return {
            "rows": [
                {
                    "keys": ["non owner sr22 california", "https://www.your-business.example.com/en/california/sr22-without-car"],
                    "clicks": 2,
                    "impressions": 52,
                    "ctr": 2 / 52,
                    "position": 11.9,
                }
            ]
        }


class _Analytics:
    def __init__(self):
        self.site_url = ""
        self.body: dict = {}

    def query(self, *, siteUrl, body):  # noqa: N803 - mirrors Google client
        self.site_url = siteUrl
        self.body = body
        return _Request()


class _Service:
    def __init__(self):
        self.analytics = _Analytics()

    def searchanalytics(self):
        return self.analytics


def test_query_page_slice_preserves_explicit_canonical_filter(monkeypatch):
    service = _Service()
    monkeypatch.setattr(search_console_api, "get_search_console_service", lambda: service)
    monkeypatch.setattr(search_console_api, "_date_range", lambda days: ("2026-07-12", "2026-08-08"))

    result = search_console_api.get_query_page_slice(
        site_url="sc-domain:your-business.example.com",
        page_url="https://www.your-business.example.com/en/california/sr22-without-car",
        query="non owner sr22 california",
        max_results=250,
        start_row=3,
    )

    assert service.analytics.site_url == "sc-domain:your-business.example.com"
    assert service.analytics.body["dataState"] == "final"
    assert service.analytics.body["startRow"] == 3
    assert service.analytics.body["rowLimit"] == 250
    assert service.analytics.body["dimensionFilterGroups"] == [{"filters": [
        {"dimension": "query", "operator": "equals", "expression": "non owner sr22 california"},
        {"dimension": "page", "operator": "equals", "expression": "https://www.your-business.example.com/en/california/sr22-without-car"},
    ]}]
    assert result["returned_rows"] == 1
    assert result["at_limit"] is False
    assert result["rows"][0]["page"].startswith("https://www.your-business.example.com/")
