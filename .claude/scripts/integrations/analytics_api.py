"""
Google Analytics (GA4) Direct Integration for The Homie.

Read-only access to GA4 data via the Analytics Data API. Shares OAuth token with Gmail.

Usage:
    uv run python -m integrations.analytics_api overview
    uv run python -m integrations.analytics_api top-pages --days 28
    uv run python -m integrations.analytics_api traffic-sources
    uv run python -m integrations.analytics_api realtime
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Add parent dir for config imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import GA4_PROPERTY_ID  # noqa: E402
from shared import with_retry  # noqa: E402


@dataclass
class PageMetrics:
    """A page with traffic metrics."""

    page_path: str
    sessions: int
    page_views: int
    avg_engagement_time: float


@dataclass
class TrafficSource:
    """A traffic source with metrics."""

    source: str
    medium: str
    sessions: int
    users: int


def _get_analytics_service() -> Any:
    """Build authenticated Analytics Data API service."""
    from googleapiclient.discovery import build  # type: ignore[import-untyped]

    from integrations.auth import get_google_credentials

    creds = get_google_credentials()
    service: Any = build("analyticsdata", "v1beta", credentials=creds)
    return service


def _property_id() -> str:
    """Return the GA4 property ID (e.g. 'properties/123456789')."""
    pid = GA4_PROPERTY_ID
    if pid and not pid.startswith("properties/"):
        pid = f"properties/{pid}"
    return pid


def get_overview(days: int = 28) -> dict[str, Any]:
    """Get overall site metrics: sessions, users, page views, bounce rate."""
    service = _get_analytics_service()
    prop = _property_id()

    body = {
        "dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "yesterday"}],
        "metrics": [
            {"name": "sessions"},
            {"name": "totalUsers"},
            {"name": "screenPageViews"},
            {"name": "averageSessionDuration"},
            {"name": "bounceRate"},
            {"name": "newUsers"},
        ],
    }

    result: dict[str, Any] = with_retry(
        lambda: service.properties().runReport(property=prop, body=body).execute()
    )

    rows = result.get("rows", [])
    if rows:
        values = rows[0].get("metricValues", [])
        return {
            "sessions": int(values[0]["value"]) if len(values) > 0 else 0,
            "users": int(values[1]["value"]) if len(values) > 1 else 0,
            "page_views": int(values[2]["value"]) if len(values) > 2 else 0,
            "avg_session_duration": float(values[3]["value"]) if len(values) > 3 else 0.0,
            "bounce_rate": float(values[4]["value"]) if len(values) > 4 else 0.0,
            "new_users": int(values[5]["value"]) if len(values) > 5 else 0,
            "days": days,
        }
    return {"sessions": 0, "users": 0, "page_views": 0, "avg_session_duration": 0.0,
            "bounce_rate": 0.0, "new_users": 0, "days": days}


def get_top_pages(days: int = 28, max_results: int = 10) -> list[PageMetrics]:
    """Get top pages by page views."""
    service = _get_analytics_service()
    prop = _property_id()

    body = {
        "dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "yesterday"}],
        "dimensions": [{"name": "pagePath"}],
        "metrics": [
            {"name": "sessions"},
            {"name": "screenPageViews"},
            {"name": "averageSessionDuration"},
        ],
        "orderBys": [{"metric": {"metricName": "screenPageViews"}, "desc": True}],
        "limit": max_results,
    }

    result: dict[str, Any] = with_retry(
        lambda: service.properties().runReport(property=prop, body=body).execute()
    )

    pages: list[PageMetrics] = []
    for row in result.get("rows", []):
        dims = row.get("dimensionValues", [])
        vals = row.get("metricValues", [])
        pages.append(
            PageMetrics(
                page_path=dims[0]["value"] if dims else "",
                sessions=int(vals[0]["value"]) if len(vals) > 0 else 0,
                page_views=int(vals[1]["value"]) if len(vals) > 1 else 0,
                avg_engagement_time=float(vals[2]["value"]) if len(vals) > 2 else 0.0,
            )
        )
    return pages


def get_traffic_sources(days: int = 28, max_results: int = 10) -> list[TrafficSource]:
    """Get top traffic sources by sessions."""
    service = _get_analytics_service()
    prop = _property_id()

    body = {
        "dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "yesterday"}],
        "dimensions": [{"name": "sessionSource"}, {"name": "sessionMedium"}],
        "metrics": [
            {"name": "sessions"},
            {"name": "totalUsers"},
        ],
        "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
        "limit": max_results,
    }

    result: dict[str, Any] = with_retry(
        lambda: service.properties().runReport(property=prop, body=body).execute()
    )

    sources: list[TrafficSource] = []
    for row in result.get("rows", []):
        dims = row.get("dimensionValues", [])
        vals = row.get("metricValues", [])
        sources.append(
            TrafficSource(
                source=dims[0]["value"] if len(dims) > 0 else "",
                medium=dims[1]["value"] if len(dims) > 1 else "",
                sessions=int(vals[0]["value"]) if len(vals) > 0 else 0,
                users=int(vals[1]["value"]) if len(vals) > 1 else 0,
            )
        )
    return sources


def get_realtime() -> dict[str, Any]:
    """Get realtime active users."""
    service = _get_analytics_service()
    prop = _property_id()

    body = {
        "metrics": [{"name": "activeUsers"}],
    }

    result: dict[str, Any] = with_retry(
        lambda: service.properties().runRealtimeReport(property=prop, body=body).execute()
    )

    rows = result.get("rows", [])
    active = int(rows[0]["metricValues"][0]["value"]) if rows else 0
    return {"active_users": active}


def format_overview_for_context(data: dict[str, Any]) -> str:
    """Format overview metrics for display."""
    duration_min = data["avg_session_duration"] / 60
    bounce_pct = data["bounce_rate"] * 100
    return (
        f"**Site Overview** (last {data['days']} days)\n"
        f"  Sessions: {data['sessions']:,}\n"
        f"  Users: {data['users']:,} ({data['new_users']:,} new)\n"
        f"  Page views: {data['page_views']:,}\n"
        f"  Avg session: {duration_min:.1f} min\n"
        f"  Bounce rate: {bounce_pct:.1f}%"
    )


def format_pages_for_context(pages: list[PageMetrics]) -> str:
    """Format top pages for display."""
    if not pages:
        return "No page data available."

    lines = ["**Top Pages**\n"]
    for i, p in enumerate(pages, 1):
        lines.append(
            f"{i}. **{p.page_path}** — {p.page_views} views, "
            f"{p.sessions} sessions"
        )
    return "\n".join(lines)


def format_sources_for_context(sources: list[TrafficSource]) -> str:
    """Format traffic sources for display."""
    if not sources:
        return "No traffic source data available."

    lines = ["**Traffic Sources**\n"]
    for i, s in enumerate(sources, 1):
        lines.append(
            f"{i}. **{s.source}** / {s.medium} — {s.sessions} sessions, "
            f"{s.users} users"
        )
    return "\n".join(lines)


def format_realtime_for_context(data: dict[str, Any]) -> str:
    """Format realtime data for display."""
    return f"**Realtime** — {data['active_users']} active users right now"


# CLI for testing
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Google Analytics (GA4) integration")
    parser.add_argument("command", choices=["overview", "top-pages", "traffic-sources", "realtime"])
    parser.add_argument("--days", type=int, default=28)
    parser.add_argument("--max", type=int, default=10)

    args = parser.parse_args()

    if args.command == "overview":
        d = get_overview(days=args.days)
        print(format_overview_for_context(d))
    elif args.command == "top-pages":
        ps = get_top_pages(days=args.days, max_results=args.max)
        print(format_pages_for_context(ps))
    elif args.command == "traffic-sources":
        ss = get_traffic_sources(days=args.days, max_results=args.max)
        print(format_sources_for_context(ss))
    elif args.command == "realtime":
        r = get_realtime()
        print(format_realtime_for_context(r))
