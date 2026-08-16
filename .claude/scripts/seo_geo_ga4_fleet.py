"""Read-only, registry-scoped GA4 analytics for the YourBusiness 27-brand fleet.

This collector reads one declared property per public brand from the local
measurement registry.  It never creates or edits GA4 resources and it treats
analytics events as instrumentation evidence, not as terminal CRM leads.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


PROFILE_ROOT = Path.home() / ".homie" / "profiles" / "seo_geo"
DEFAULT_REGISTRY = PROFILE_ROOT / "data" / "fleet-measurement" / "registry.json"
DEFAULT_OUT_DIR = PROFILE_ROOT / "data" / "fleet-ga4"
WINDOWS = (3, 7, 14, 28, 90)
HISTORY_DAYS = max(WINDOWS) * 2

FUNNEL_EVENT_GROUPS = {
    "quote_start": {"quote_start", "quote_started", "start_quote"},
    "quote_or_lead_submit": {
        "quote_submit",
        "quote_submitted",
        "quote_completed",
        "generate_lead",
        "lead_capture",
        "lead_captured",
        "lead_created",
        "lead_submitted",
    },
    "contact_submit": {"contact_submit", "contact_submitted", "form_submit"},
    "phone_click": {"phone_click", "click_to_call"},
}


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("registry must be a JSON object")
    return payload


def load_fleet(registry_path: Path) -> list[dict[str, str]]:
    registry = _read_json(registry_path)
    brands = registry.get("brands")
    if not isinstance(brands, list) or len(brands) != 27:
        raise ValueError("measurement registry must contain exactly 27 public brands")

    fleet: list[dict[str, str]] = []
    for brand in brands:
        if not isinstance(brand, dict):
            raise ValueError("measurement registry brand row must be an object")
        measurement = brand.get("measurement")
        ga4 = measurement.get("ga4") if isinstance(measurement, dict) else None
        prop = str(ga4.get("property_id_declared") or "") if isinstance(ga4, dict) else ""
        if not prop.startswith("properties/"):
            raise ValueError(f"brand {brand.get('id')} has no declared GA4 property")
        fleet.append(
            {
                "brand_id": str(brand.get("id") or ""),
                "display_name": str(brand.get("name") or brand.get("id") or ""),
                "domain": str(brand.get("domain") or ""),
                "property": prop,
            }
        )

    for label, values in (
        ("brand ids", [row["brand_id"] for row in fleet]),
        ("domains", [row["domain"] for row in fleet]),
        ("GA4 properties", [row["property"] for row in fleet]),
    ):
        if any(not value for value in values) or len(set(values)) != 27:
            raise ValueError(f"measurement registry does not have 27 unique {label}")
    return fleet


def _analytics_service() -> Any:
    from googleapiclient.discovery import build  # type: ignore[import-untyped]

    from integrations.auth import get_ga4_reporting_credentials

    return build("analyticsdata", "v1beta", credentials=get_ga4_reporting_credentials())


def _batch_body(start_date: date, end_date: date) -> dict[str, Any]:
    date_range = {"startDate": start_date.isoformat(), "endDate": end_date.isoformat()}
    return {
        "requests": [
            {
                "dateRanges": [date_range],
                "dimensions": [
                    {"name": "date"},
                    {"name": "sessionDefaultChannelGroup"},
                ],
                "metrics": [
                    {"name": "sessions"},
                    {"name": "screenPageViews"},
                ],
                "limit": "10000",
            },
            {
                "dateRanges": [date_range],
                "dimensions": [{"name": "date"}, {"name": "eventName"}],
                "metrics": [{"name": "eventCount"}],
                "limit": "10000",
            },
        ]
    }


def _number(values: list[dict[str, Any]], index: int) -> float:
    try:
        return float(values[index].get("value") or 0)
    except (IndexError, TypeError, ValueError):
        return 0.0


def _dimension(values: list[dict[str, Any]], index: int) -> str:
    try:
        return str(values[index].get("value") or "")
    except (IndexError, TypeError):
        return ""


def _parse_reports(response: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    reports = response.get("reports")
    if not isinstance(reports, list) or len(reports) < 2:
        raise ValueError("GA4 batch response did not contain both reports")

    traffic: list[dict[str, Any]] = []
    for row in reports[0].get("rows", []):
        dimensions = row.get("dimensionValues", [])
        metrics = row.get("metricValues", [])
        traffic.append(
            {
                "date": _dimension(dimensions, 0),
                "channel": _dimension(dimensions, 1),
                "sessions": _number(metrics, 0),
                "page_views": _number(metrics, 1),
            }
        )

    events: list[dict[str, Any]] = []
    for row in reports[1].get("rows", []):
        dimensions = row.get("dimensionValues", [])
        metrics = row.get("metricValues", [])
        events.append(
            {
                "date": _dimension(dimensions, 0),
                "event_name": _dimension(dimensions, 1),
                "event_count": _number(metrics, 0),
            }
        )
    return traffic, events


def _within(value: str, start_date: date, end_date: date) -> bool:
    try:
        observed = datetime.strptime(value, "%Y%m%d").date()
    except (TypeError, ValueError):
        return False
    return start_date <= observed <= end_date


def _period_metrics(
    traffic: Iterable[dict[str, Any]],
    events: Iterable[dict[str, Any]],
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    traffic_rows = [row for row in traffic if _within(str(row.get("date")), start_date, end_date)]
    event_rows = [row for row in events if _within(str(row.get("date")), start_date, end_date)]
    funnel: dict[str, float] = {}
    observed_names: set[str] = set()
    for group, names in FUNNEL_EVENT_GROUPS.items():
        total = sum(
            float(row.get("event_count") or 0)
            for row in event_rows
            if str(row.get("event_name")) in names
        )
        funnel[group] = round(total, 2)
        observed_names.update(
            str(row.get("event_name"))
            for row in event_rows
            if total and str(row.get("event_name")) in names
        )
    return {
        "sessions": round(sum(float(row.get("sessions") or 0) for row in traffic_rows), 2),
        "organic_sessions": round(
            sum(
                float(row.get("sessions") or 0)
                for row in traffic_rows
                if str(row.get("channel")) == "Organic Search"
            ),
            2,
        ),
        "page_views": round(sum(float(row.get("page_views") or 0) for row in traffic_rows), 2),
        "funnel_events": funnel,
        "observed_funnel_event_names": sorted(observed_names),
    }


def _metric_delta(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    funnel = {
        key: round(
            float(current.get("funnel_events", {}).get(key, 0))
            - float(previous.get("funnel_events", {}).get(key, 0)),
            2,
        )
        for key in FUNNEL_EVENT_GROUPS
    }
    return {
        "sessions": round(float(current["sessions"]) - float(previous["sessions"]), 2),
        "organic_sessions": round(
            float(current["organic_sessions"]) - float(previous["organic_sessions"]), 2
        ),
        "page_views": round(float(current["page_views"]) - float(previous["page_views"]), 2),
        "funnel_events": funnel,
    }


def build_comparisons(
    traffic: list[dict[str, Any]], events: list[dict[str, Any]], end_date: date
) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for days in WINDOWS:
        current_start = end_date - timedelta(days=days - 1)
        previous_end = current_start - timedelta(days=1)
        previous_start = previous_end - timedelta(days=days - 1)
        current = _period_metrics(traffic, events, current_start, end_date)
        previous = _period_metrics(traffic, events, previous_start, previous_end)
        output[f"{days}d"] = {
            "days": days,
            "freshness": "observed_through_yesterday",
            "current_range": {"start": current_start.isoformat(), "end": end_date.isoformat()},
            "previous_range": {
                "start": previous_start.isoformat(),
                "end": previous_end.isoformat(),
            },
            "current": current,
            "previous": previous,
            "delta": _metric_delta(current, previous),
        }
    return output


def _top_events(events: list[dict[str, Any]], end_date: date, days: int = 28) -> list[dict[str, Any]]:
    start = end_date - timedelta(days=days - 1)
    totals: Counter[str] = Counter()
    for row in events:
        if _within(str(row.get("date")), start, end_date):
            totals[str(row.get("event_name") or "unknown")] += float(row.get("event_count") or 0)
    return [
        {"event_name": name, "event_count": round(count, 2)}
        for name, count in totals.most_common(10)
    ]


def _rollup(brands: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    available = [brand for brand in brands if brand.get("status") == "ok"]
    for key in (f"{days}d" for days in WINDOWS):
        entries = [brand.get("window_comparisons", {}).get(key) for brand in available]
        entries = [entry for entry in entries if isinstance(entry, dict)]
        if not entries:
            continue
        current = {
            "sessions": sum(float(entry["current"]["sessions"]) for entry in entries),
            "organic_sessions": sum(float(entry["current"]["organic_sessions"]) for entry in entries),
            "page_views": sum(float(entry["current"]["page_views"]) for entry in entries),
            "funnel_events": {
                group: sum(float(entry["current"]["funnel_events"].get(group, 0)) for entry in entries)
                for group in FUNNEL_EVENT_GROUPS
            },
        }
        previous = {
            "sessions": sum(float(entry["previous"]["sessions"]) for entry in entries),
            "organic_sessions": sum(float(entry["previous"]["organic_sessions"]) for entry in entries),
            "page_views": sum(float(entry["previous"]["page_views"]) for entry in entries),
            "funnel_events": {
                group: sum(float(entry["previous"]["funnel_events"].get(group, 0)) for entry in entries)
                for group in FUNNEL_EVENT_GROUPS
            },
        }
        template = entries[0]
        output[key] = {
            "days": template["days"],
            "freshness": template["freshness"],
            "current_range": template["current_range"],
            "previous_range": template["previous_range"],
            "current": current,
            "previous": previous,
            "delta": _metric_delta(current, previous),
        }
    return output


def collect(*, registry_path: Path, service: Any | None = None, end_date: date | None = None) -> dict[str, Any]:
    fleet = load_fleet(registry_path)
    observed_end = end_date or (date.today() - timedelta(days=1))
    history_start = observed_end - timedelta(days=HISTORY_DAYS - 1)
    api = service or _analytics_service()
    brands: list[dict[str, Any]] = []
    for brand in fleet:
        row: dict[str, Any] = {**brand, "status": "unavailable"}
        try:
            response = (
                api.properties()
                .batchRunReports(
                    property=brand["property"], body=_batch_body(history_start, observed_end)
                )
                .execute()
            )
            traffic, events = _parse_reports(response)
            row.update(
                {
                    "status": "ok",
                    "window_comparisons": build_comparisons(traffic, events, observed_end),
                    "top_events_28d": _top_events(events, observed_end),
                }
            )
        except Exception as exc:  # noqa: BLE001
            row["reason"] = f"GA4 Data API read failed ({type(exc).__name__})"
        brands.append(row)

    ok_count = sum(brand["status"] == "ok" for brand in brands)
    status = "ok" if ok_count == 27 else ("partial" if ok_count else "error")
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": status,
        "read_only": True,
        "mutations": [],
        "registry_path": str(registry_path),
        "evidence_boundary": (
            "GA4 sessions and events are analytics receipts, not proof of a terminal CRM lead, "
            "a contacted customer, or revenue. GA4 data is observed through yesterday and may update."
        ),
        "history_range": {"start": history_start.isoformat(), "end": observed_end.isoformat()},
        "summary": {
            "expected_properties": 27,
            "properties_ok": ok_count,
            "properties_unavailable": 27 - ok_count,
        },
        "fleet_window_comparisons": _rollup(brands),
        "brands": brands,
    }


def write_receipt(receipt: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    dated = out_dir / f"{date.today().isoformat()}.json"
    latest = out_dir / "latest.json"
    content = json.dumps(receipt, indent=2, ensure_ascii=False)
    dated.write_text(content, encoding="utf-8")
    latest.write_text(content, encoding="utf-8")
    return dated, latest


def main() -> int:
    parser = argparse.ArgumentParser(description="Read the 27 declared GA4 properties without mutations.")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()
    try:
        receipt = collect(registry_path=args.registry)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: GA4 fleet preflight failed ({type(exc).__name__})")
        return 1
    dated, latest = write_receipt(receipt, args.out_dir)
    print(f"GA4_RECEIPT_JSON={dated}")
    print(f"GA4_LATEST_JSON={latest}")
    print(f"PROPERTIES_OK={receipt['summary']['properties_ok']}/27")
    print("MUTATIONS=0")
    return 0 if receipt["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
