"""
Google Search Console Direct Integration for The Homie.

Read-only access to Search Console performance data. Shares OAuth token with Gmail.

Usage:
    uv run python -m integrations.search_console_api top-queries
    uv run python -m integrations.search_console_api top-pages --days 28
    uv run python -m integrations.search_console_api overview
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# Add parent dir for config imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Boot-shim: must run BEFORE any framework imports (config, runtime, etc.)
from personas import apply_persona_override  # noqa: E402

apply_persona_override()

from config import GSC_SITE_URL  # noqa: E402
from shared import with_retry  # noqa: E402


@dataclass
class SearchQuery:
    """A search query with performance metrics."""

    query: str
    clicks: int
    impressions: int
    ctr: float
    position: float


@dataclass
class SearchPage:
    """A page with search performance metrics."""

    page: str
    clicks: int
    impressions: int
    ctr: float
    position: float


@dataclass
class SearchQueryPage:
    """One Search Analytics query/page row with no implied exhaustiveness."""

    query: str
    page: str
    clicks: int
    impressions: int
    ctr: float
    position: float


def get_search_console_service() -> Any:
    """Build authenticated Search Console API service."""
    from googleapiclient.discovery import build  # type: ignore[import-untyped]

    from integrations.auth import get_google_credentials

    creds = get_google_credentials()
    service: Any = build("searchconsole", "v1", credentials=creds)
    return service


def _date_range(days: int) -> tuple[str, str]:
    """Return (start_date, end_date) strings for the last N days."""
    end = datetime.now() - timedelta(days=3)  # GSC data has ~3 day lag
    start = end - timedelta(days=max(1, days) - 1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def get_top_queries(
    site_url: str | None = None,
    days: int = 28,
    max_results: int = 10,
) -> list[SearchQuery]:
    """Get top search queries by clicks."""
    service = get_search_console_service()
    url = site_url or GSC_SITE_URL
    start_date, end_date = _date_range(days)

    body = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": ["query"],
        "rowLimit": max_results,
        "dataState": "final",
    }

    result: dict[str, Any] = with_retry(
        lambda: service.searchanalytics().query(siteUrl=url, body=body).execute()
    )

    queries: list[SearchQuery] = []
    for row in result.get("rows", []):
        queries.append(
            SearchQuery(
                query=row["keys"][0],
                clicks=int(row["clicks"]),
                impressions=int(row["impressions"]),
                ctr=row["ctr"],
                position=row["position"],
            )
        )
    return queries


def get_top_pages(
    site_url: str | None = None,
    days: int = 28,
    max_results: int = 10,
) -> list[SearchPage]:
    """Get top pages by clicks."""
    service = get_search_console_service()
    url = site_url or GSC_SITE_URL
    start_date, end_date = _date_range(days)

    body = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": ["page"],
        "rowLimit": max_results,
        "dataState": "final",
    }

    result: dict[str, Any] = with_retry(
        lambda: service.searchanalytics().query(siteUrl=url, body=body).execute()
    )

    pages: list[SearchPage] = []
    for row in result.get("rows", []):
        pages.append(
            SearchPage(
                page=row["keys"][0],
                clicks=int(row["clicks"]),
                impressions=int(row["impressions"]),
                ctr=row["ctr"],
                position=row["position"],
            )
        )
    return pages


def get_query_page_slice(
    site_url: str | None = None,
    days: int = 28,
    max_results: int = 100,
    start_row: int = 0,
    query: str | None = None,
    page_url: str | None = None,
) -> dict[str, Any]:
    """Return a bounded, finalized GSC query/page evidence slice.

    Search Analytics returns top rows subject to Google-side limits.  The
    response therefore carries explicit pagination and ``at_limit`` metadata
    instead of presenting any slice as a complete query inventory.

    ``page_url`` must be an explicit canonical URL when a page is filtered.
    This intentionally avoids converting a YourBusiness-relative path onto the
    apex host, which would make URL-inspection and cannibalization evidence
    misleading for the canonical ``www`` site.
    """
    service = get_search_console_service()
    url = site_url or GSC_SITE_URL
    start_date, end_date = _date_range(days)
    row_limit = max(1, min(int(max_results), 250))
    row_start = max(0, int(start_row))

    filters: list[dict[str, str]] = []
    normalized_query = str(query or "").strip()
    normalized_page = str(page_url or "").strip()
    if normalized_query:
        filters.append({
            "dimension": "query",
            "operator": "equals",
            "expression": normalized_query[:800],
        })
    if normalized_page:
        filters.append({
            "dimension": "page",
            "operator": "equals",
            "expression": normalized_page[:2_000],
        })

    body: dict[str, Any] = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": ["query", "page"],
        "rowLimit": row_limit,
        "startRow": row_start,
        "dataState": "final",
    }
    if filters:
        body["dimensionFilterGroups"] = [{"filters": filters}]

    result: dict[str, Any] = with_retry(
        lambda: service.searchanalytics().query(siteUrl=url, body=body).execute()
    )
    rows: list[dict[str, Any]] = []
    for row in result.get("rows", []):
        keys = row.get("keys", [])
        rows.append({
            "query": str(keys[0]) if len(keys) > 0 else "",
            "page": str(keys[1]) if len(keys) > 1 else "",
            "clicks": int(row.get("clicks", 0)),
            "impressions": int(row.get("impressions", 0)),
            "ctr": float(row.get("ctr", 0.0)),
            "position": float(row.get("position", 0.0)),
        })

    return {
        "site_url": url,
        "data_state": "final",
        "requested_period": {"start_date": start_date, "end_date": end_date, "days": days},
        "filters": {"query": normalized_query or None, "page_url": normalized_page or None},
        "row_limit": row_limit,
        "start_row": row_start,
        "returned_rows": len(rows),
        "at_limit": len(rows) >= row_limit,
        "rows": rows,
        "note": "Search Analytics responses are bounded top rows and are not an exhaustive inventory.",
    }


def get_overall_stats(
    site_url: str | None = None,
    days: int = 28,
) -> dict[str, Any]:
    """Get overall search performance stats (totals)."""
    service = get_search_console_service()
    url = site_url or GSC_SITE_URL
    start_date, end_date = _date_range(days)

    body = {
        "startDate": start_date,
        "endDate": end_date,
        "dataState": "final",
    }

    result: dict[str, Any] = with_retry(
        lambda: service.searchanalytics().query(siteUrl=url, body=body).execute()
    )

    rows = result.get("rows", [])
    if rows:
        row = rows[0]
        return {
            "clicks": int(row["clicks"]),
            "impressions": int(row["impressions"]),
            "ctr": row["ctr"],
            "position": row["position"],
            "period": f"{start_date} to {end_date}",
        }
    return {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0, "period": f"{start_date} to {end_date}"}


def format_queries_for_context(queries: list[SearchQuery]) -> str:
    """Format search queries for display."""
    if not queries:
        return "No search query data available."

    lines = ["**Top Search Queries**\n"]
    for i, q in enumerate(queries, 1):
        lines.append(
            f"{i}. **{q.query}** — {q.clicks} clicks, "
            f"{q.impressions} impressions, "
            f"CTR {q.ctr:.1%}, pos {q.position:.1f}"
        )
    return "\n".join(lines)


def format_pages_for_context(pages: list[SearchPage]) -> str:
    """Format search pages for display."""
    if not pages:
        return "No page data available."

    lines = ["**Top Pages**\n"]
    for i, p in enumerate(pages, 1):
        # Shorten the URL for readability
        short_url = p.page.replace("https://", "").replace("http://", "")
        lines.append(
            f"{i}. **{short_url}** — {p.clicks} clicks, "
            f"{p.impressions} impressions, "
            f"CTR {p.ctr:.1%}, pos {p.position:.1f}"
        )
    return "\n".join(lines)


def format_stats_for_context(stats: dict[str, Any]) -> str:
    """Format overall stats for display."""
    return (
        f"**Search Performance** ({stats['period']})\n"
        f"  Total clicks: {stats['clicks']:,}\n"
        f"  Total impressions: {stats['impressions']:,}\n"
        f"  Average CTR: {stats['ctr']:.1%}\n"
        f"  Average position: {stats['position']:.1f}"
    )


# CLI for testing
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Search Console integration")
    parser.add_argument("command", choices=["top-queries", "top-pages", "overview"])
    parser.add_argument("--days", type=int, default=28)
    parser.add_argument("--max", type=int, default=10)

    args = parser.parse_args()

    if args.command == "top-queries":
        qs = get_top_queries(days=args.days, max_results=args.max)
        print(format_queries_for_context(qs))
    elif args.command == "top-pages":
        ps = get_top_pages(days=args.days, max_results=args.max)
        print(format_pages_for_context(ps))
    elif args.command == "overview":
        st = get_overall_stats(days=args.days)
        print(format_stats_for_context(st))
