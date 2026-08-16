"""Outbound Discord receipts for scheduled SEO/GEO control jobs.

This module deliberately has one narrow authority: post a deterministic summary
of a *local* SEO/GEO receipt to the single channel bound to the ``seo_geo``
persona, and mention the one configured operator.  It never reads public sites,
calls research providers, or changes a website.

The scheduled-job wrapper owns when this is called.  Direct runs of the source
jobs stay silent, which prevents an exploratory local run from unexpectedly
pinging the operator.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping


DISCORD_API_BASE = "https://discord.com/api/v10"
PERSONA_ID = "seo_geo"
BINDING_NAME = "seo_geo"
TEXT_LIMIT = 1_850
DEFAULT_NOTIFICATION_DIR = (
    Path.home() / ".homie" / "profiles" / PERSONA_ID / "data" / "fleet-notifications"
)


@dataclass(frozen=True)
class DiscordTarget:
    token: str
    guild_id: str
    channel_id: str
    operator_id: str


def _clip(value: object, limit: int = TEXT_LIMIT) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else f"{text[: limit - 1].rstrip()}…"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _bindings_path() -> Path:
    import config

    return Path(config.DATA_DIR) / "discord-channel-bindings.json"


def resolve_target(*, bindings_path: Path | None = None) -> DiscordTarget | None:
    """Resolve exactly one configured SEO/GEO channel and one operator.

    Ambiguity is a no-send condition.  In particular, adding another allowed
    Discord user must not silently turn a job receipt into a multi-user ping.
    """

    import config

    raw = _read_json(bindings_path or _bindings_path())
    if not isinstance(raw, dict) or not isinstance(raw.get("channels"), dict):
        return None
    root_guild_id = str(raw.get("guild_id") or "").strip()
    matches: list[tuple[str, str]] = []
    for channel_id, row in raw["channels"].items():
        if not isinstance(row, dict):
            continue
        guild_id = str(row.get("guild_id") or root_guild_id).strip()
        if (
            str(row.get("name") or "").strip() == BINDING_NAME
            and str(row.get("persona") or "").strip() == PERSONA_ID
            and str(row.get("kind") or "persona").strip() == "persona"
            and guild_id
            and str(channel_id).strip()
        ):
            matches.append((guild_id, str(channel_id).strip()))
    if len(matches) != 1:
        return None

    token = str(getattr(config, "DISCORD_BOT_TOKEN", "") or "").strip()
    allowed_guilds = {
        str(value).strip()
        for value in getattr(config, "DISCORD_ALLOWED_GUILDS", ())
        if str(value).strip()
    }
    operator_ids = tuple(
        dict.fromkeys(
            str(value).strip()
            for value in getattr(config, "DISCORD_ALLOWED_USERS", ())
            if str(value).strip()
        )
    )
    guild_id, channel_id = matches[0]
    if not token or guild_id not in allowed_guilds or len(operator_ids) != 1:
        return None
    return DiscordTarget(token, guild_id, channel_id, operator_ids[0])


def _source_states(receipt: Mapping[str, Any]) -> str:
    sources = receipt.get("sources")
    if not isinstance(sources, Mapping):
        return "Receipt source detail unavailable."
    labels = (
        ("gsc", "GSC"),
        ("ga4", "GA4"),
        ("ai_visibility", "AI"),
        ("measurement_registry", "measurement"),
        ("budget_broker", "budget"),
    )
    states: list[str] = []
    for key, label in labels:
        source = sources.get(key)
        status = source.get("status") if isinstance(source, Mapping) else None
        if status:
            states.append(f"{label}={status}")
    return " · ".join(states) if states else "Receipt source detail unavailable."


def _gsc_snapshot_path(receipt: Mapping[str, Any] | None) -> Path | None:
    """Resolve the saved source snapshot without calling Google again."""

    if not isinstance(receipt, Mapping):
        return None
    sources = receipt.get("sources")
    source = sources.get("gsc") if isinstance(sources, Mapping) else None
    stdout = source.get("stdout") if isinstance(source, Mapping) else None
    match = re.search(r"^SNAPSHOT_JSON=(.+)$", str(stdout or ""), flags=re.MULTILINE)
    if not match:
        return None
    candidate = Path(match.group(1).strip())
    return candidate if candidate.is_file() else None


def _analytics_totals(brand: Mapping[str, Any]) -> dict[str, float]:
    analytics = brand.get("analytics")
    totals = analytics.get("totals") if isinstance(analytics, Mapping) else None
    if not isinstance(totals, Mapping):
        return {"impressions": 0.0, "clicks": 0.0}
    return {
        "impressions": float(totals.get("impressions") or 0),
        "clicks": float(totals.get("clicks") or 0),
    }


def _fleet_totals(snapshot: Mapping[str, Any] | None) -> dict[str, float]:
    brands = snapshot.get("brands") if isinstance(snapshot, Mapping) else None
    if not isinstance(brands, list):
        return {"impressions": 0.0, "clicks": 0.0, "brands_with_clicks": 0.0}
    totals = [_analytics_totals(brand) for brand in brands if isinstance(brand, Mapping)]
    return {
        "impressions": sum(item["impressions"] for item in totals),
        "clicks": sum(item["clicks"] for item in totals),
        "brands_with_clicks": float(sum(item["clicks"] > 0 for item in totals)),
    }


def _format_change(current: float, previous: float) -> str:
    delta = current - previous
    sign = "+" if delta >= 0 else ""
    if previous:
        percent = delta / previous * 100
        return f"{sign}{delta:,.0f}, {sign}{percent:.1f}%"
    return f"{sign}{delta:,.0f}, new" if current else "0"


def _window_line(prefix: str, key: str, comparison: Mapping[str, Any]) -> str:
    current = comparison.get("current")
    previous = comparison.get("previous")
    if not isinstance(current, Mapping) or not isinstance(previous, Mapping):
        return f"{prefix} {key}: comparison unavailable."
    impressions = float(current.get("impressions") or 0)
    prior_impressions = float(previous.get("impressions") or 0)
    clicks = float(current.get("clicks") or 0)
    prior_clicks = float(previous.get("clicks") or 0)
    ctr = float(current.get("ctr") or 0) * 100
    prior_ctr = float(previous.get("ctr") or 0) * 100
    return (
        f"{prefix} {key}: {impressions:,.0f} imp ({_format_change(impressions, prior_impressions)}) · "
        f"{clicks:,.0f} clicks ({_format_change(clicks, prior_clicks)}) · "
        f"CTR {ctr:.2f}% ({ctr - prior_ctr:+.2f} pp)"
    )


def _brand_window_movers(snapshot: Mapping[str, Any] | None, key: str = "7d") -> list[str]:
    brands = snapshot.get("brands") if isinstance(snapshot, Mapping) else None
    if not isinstance(brands, list):
        return []
    rows: list[tuple[float, str]] = []
    for brand in brands:
        if not isinstance(brand, Mapping):
            continue
        analytics = brand.get("analytics")
        comparisons = analytics.get("window_comparisons") if isinstance(analytics, Mapping) else None
        comparison = comparisons.get(key) if isinstance(comparisons, Mapping) else None
        delta = comparison.get("delta") if isinstance(comparison, Mapping) else None
        if not isinstance(delta, Mapping):
            continue
        impressions = float(delta.get("impressions") or 0)
        if impressions:
            name = str(brand.get("display_name") or brand.get("brand_id") or "Unknown")
            rows.append((impressions, name))
    if not rows:
        return []
    rows.sort()
    output = [f"best {rows[-1][1]} {rows[-1][0]:+,.0f} imp"]
    if rows[0][0] < 0:
        output.append(f"watch {rows[0][1]} {rows[0][0]:+,.0f} imp")
    return output


def _sitemap_alert_line(snapshot: Mapping[str, Any] | None) -> str | None:
    brands = snapshot.get("brands") if isinstance(snapshot, Mapping) else None
    if not isinstance(brands, list):
        return None
    error_rows: list[tuple[int, str]] = []
    warning_brands = 0
    warning_total = 0
    for brand in brands:
        if not isinstance(brand, Mapping):
            continue
        sitemaps = brand.get("sitemaps")
        if not isinstance(sitemaps, list):
            continue
        errors = sum(
            int(float(row.get("errors") or 0)) for row in sitemaps if isinstance(row, Mapping)
        )
        warnings = sum(
            int(float(row.get("warnings") or 0)) for row in sitemaps if isinstance(row, Mapping)
        )
        name = str(brand.get("display_name") or brand.get("brand_id") or "Unknown")
        if errors:
            error_rows.append((errors, name))
        if warnings:
            warning_brands += 1
            warning_total += warnings
    if not error_rows and not warning_total:
        return None
    parts: list[str] = []
    if error_rows:
        errors, name = sorted(error_rows, reverse=True)[0]
        parts.append(f"{name} {errors} errors")
    if warning_total:
        parts.append(f"warnings on {warning_brands} brands ({warning_total} total)")
    return "Sitemap alert: " + " · ".join(parts)


def _brand_movers(
    current_snapshot: Mapping[str, Any] | None,
    previous_snapshot: Mapping[str, Any] | None,
) -> list[str]:
    current_brands = current_snapshot.get("brands") if isinstance(current_snapshot, Mapping) else None
    previous_brands = previous_snapshot.get("brands") if isinstance(previous_snapshot, Mapping) else None
    if not isinstance(current_brands, list) or not isinstance(previous_brands, list):
        return []
    previous = {
        str(item.get("brand_id") or ""): item
        for item in previous_brands
        if isinstance(item, Mapping)
    }
    rows: list[tuple[float, float, str, Mapping[str, Any]]] = []
    for brand in current_brands:
        if not isinstance(brand, Mapping):
            continue
        brand_id = str(brand.get("brand_id") or "")
        prior = previous.get(brand_id)
        if not prior:
            continue
        current = _analytics_totals(brand)
        old = _analytics_totals(prior)
        delta_impressions = current["impressions"] - old["impressions"]
        delta_clicks = current["clicks"] - old["clicks"]
        if delta_impressions:
            rows.append((delta_impressions, delta_clicks, brand_id, brand))
    if not rows:
        return []
    lines: list[str] = []
    for delta_impressions, delta_clicks, _brand_id, brand in sorted(rows, reverse=True)[:3]:
        name = str(brand.get("display_name") or brand.get("brand_id") or "Unknown")
        click_sign = "+" if delta_clicks >= 0 else ""
        lines.append(f"{name}: {delta_impressions:+,.0f} impressions · {click_sign}{delta_clicks:,.0f} clicks")
    return lines


def _query_categories(brand: Mapping[str, Any]) -> dict[str, set[str]]:
    analytics = brand.get("analytics")
    rows = analytics.get("top_queries") if isinstance(analytics, Mapping) else None
    categories: dict[str, set[str]] = {}
    if not isinstance(rows, list):
        return categories
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        keys = row.get("keys")
        if not isinstance(keys, list) or not keys:
            continue
        categories[str(keys[0])] = {
            str(value) for value in row.get("categories", []) if str(value).strip()
        }
    return categories


def _route_opportunities(snapshot: Mapping[str, Any] | None) -> list[str]:
    """Return sampled query/page opportunities, not a claim of full GSC coverage."""

    brands = snapshot.get("brands") if isinstance(snapshot, Mapping) else None
    if not isinstance(brands, list):
        return []
    candidates: list[tuple[float, float, float, str, str, str]] = []
    for brand in brands:
        if not isinstance(brand, Mapping):
            continue
        analytics = brand.get("analytics")
        rows = analytics.get("top_query_pages") if isinstance(analytics, Mapping) else None
        categories = _query_categories(brand)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            keys = row.get("keys")
            if not isinstance(keys, list) or len(keys) < 2:
                continue
            query, url = str(keys[0]), str(keys[1])
            if "brand" in categories.get(query, set()):
                continue
            impressions = float(row.get("impressions") or 0)
            clicks = float(row.get("clicks") or 0)
            position = float(row.get("position") or 0)
            if impressions < 5 or not 4 <= position <= 30:
                continue
            candidates.append((impressions, clicks, position, str(brand.get("display_name") or brand.get("brand_id")), query, url))
    lines: list[str] = []
    for impressions, clicks, position, name, query, url in sorted(candidates, reverse=True)[:1]:
        action = "CTR/title candidate" if position <= 10 else "on-page/internal-link candidate"
        parsed = urllib.parse.urlparse(url)
        route = parsed.path or "/"
        lines.append(
            f"{name} — `{query}` → {route} | {impressions:,.0f} impressions, "
            f"{clicks:,.0f} clicks, position {position:.1f}; {action}."
        )
    return lines


def _fragmented_intent(snapshot: Mapping[str, Any] | None) -> str | None:
    brands = snapshot.get("brands") if isinstance(snapshot, Mapping) else None
    if not isinstance(brands, list):
        return None
    candidates: list[tuple[int, float, str, str]] = []
    for brand in brands:
        if not isinstance(brand, Mapping):
            continue
        analytics = brand.get("analytics")
        rows = analytics.get("top_query_pages") if isinstance(analytics, Mapping) else None
        if not isinstance(rows, list):
            continue
        grouped: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            keys = row.get("keys")
            if not isinstance(keys, list) or len(keys) < 2:
                continue
            grouped.setdefault(str(keys[0]), []).append(row)
        for query, query_rows in grouped.items():
            urls = {str(row.get("keys", [None, ""])[1]) for row in query_rows}
            impressions = sum(float(row.get("impressions") or 0) for row in query_rows)
            if len(urls) >= 3 and impressions >= 10:
                candidates.append((len(urls), impressions, str(brand.get("display_name") or brand.get("brand_id")), query))
    if not candidates:
        return None
    urls, impressions, name, query = sorted(candidates, reverse=True)[0]
    return f"Intent-ownership watch: {name} has `{query}` across {urls} URLs ({impressions:,.0f} sampled impressions). Inspect before editing; this may be cannibalization."


def _daily_context(receipt: Mapping[str, Any] | None) -> dict[str, Any]:
    current_path = _gsc_snapshot_path(receipt)
    current = _read_json(current_path) if current_path else None
    previous: dict[str, Any] | None = None
    if current_path:
        dated = sorted(
            path for path in current_path.parent.glob("20??-??-??.json") if path != current_path
        )
        if dated:
            previous = _read_json(dated[-1])
    return {"current": current, "previous": previous}


def _daily_body(
    receipt: Mapping[str, Any] | None,
    *,
    status: str,
    context: Mapping[str, Any] | None = None,
) -> list[str]:
    if not receipt:
        return ["No fresh receipt was produced. Check the scheduled job log."]
    current = context.get("current") if isinstance(context, Mapping) else None
    lines: list[str] = []
    action_lines: list[str] = []
    comparisons = current.get("fleet_window_comparisons") if isinstance(current, Mapping) else None
    if isinstance(comparisons, Mapping) and comparisons:
        finalized = comparisons.get("3d")
        current_range = finalized.get("current_range") if isinstance(finalized, Mapping) else None
        if isinstance(current_range, Mapping):
            lines.append(f"GSC finalized through {current_range.get('end')}; equal non-overlapping comparisons:")
        for key in ("3d", "7d", "14d", "28d", "90d"):
            comparison = comparisons.get(key)
            if isinstance(comparison, Mapping):
                lines.append(_window_line("GSC", key, comparison))
        movers = _brand_window_movers(current)
        if movers:
            lines.append("7d drivers: " + " · ".join(movers))
        sitemap_alert = _sitemap_alert_line(current)
        if sitemap_alert:
            lines.append(sitemap_alert)
        opportunities = _route_opportunities(current)
        if opportunities:
            action_lines.append("Best sampled update candidate (approval required; no page changed):")
            action_lines.extend(opportunities)
        fragmented = _fragmented_intent(current)
        if fragmented:
            action_lines.append(fragmented)
    else:
        lines.append(_source_states(receipt))
    sources = receipt.get("sources")
    if isinstance(sources, Mapping):
        ga4 = sources.get("ga4")
        ga4_summary = ga4.get("summary") if isinstance(ga4, Mapping) else None
        ga4_windows = ga4.get("fleet_window_comparisons") if isinstance(ga4, Mapping) else None
        if isinstance(ga4_summary, Mapping):
            lines.append(
                f"GA4 fleet: {ga4_summary.get('properties_ok', 0)}/{ga4_summary.get('expected_properties', 27)} properties readable."
            )
        if isinstance(ga4_windows, Mapping):
            organic_parts: list[str] = []
            submit_parts: list[str] = []
            for key in ("3d", "7d", "14d", "28d", "90d"):
                comparison = ga4_windows.get(key)
                current_ga4 = comparison.get("current") if isinstance(comparison, Mapping) else None
                previous_ga4 = comparison.get("previous") if isinstance(comparison, Mapping) else None
                if not isinstance(current_ga4, Mapping) or not isinstance(previous_ga4, Mapping):
                    continue
                current_funnel = current_ga4.get("funnel_events")
                previous_funnel = previous_ga4.get("funnel_events")
                current_funnel = current_funnel if isinstance(current_funnel, Mapping) else {}
                previous_funnel = previous_funnel if isinstance(previous_funnel, Mapping) else {}
                organic = float(current_ga4.get("organic_sessions") or 0)
                prior_organic = float(previous_ga4.get("organic_sessions") or 0)
                submits = float(current_funnel.get("quote_or_lead_submit") or 0)
                prior_submits = float(previous_funnel.get("quote_or_lead_submit") or 0)
                organic_parts.append(
                    f"{key} {organic:,.0f} ({_format_change(organic, prior_organic)})"
                )
                submit_parts.append(
                    f"{key} {submits:,.0f} ({_format_change(submits, prior_submits)})"
                )
            if organic_parts:
                lines.append("GA4 organic sessions: " + " · ".join(organic_parts))
                lines.append("GA4 quote/lead submit events: " + " · ".join(submit_parts))
        ai = sources.get("ai_visibility")
        ai_metrics = ai.get("metrics") if isinstance(ai, Mapping) else None
        ai_comparison = ai.get("comparison") if isinstance(ai, Mapping) else None
        if isinstance(ai_metrics, Mapping):
            lines.append(
                "AI visibility (saved Google AIO receipt): "
                f"AIO {ai_metrics.get('ai_overview_present', 0)}/{ai_metrics.get('prompt_count', 0)} · "
                f"owner citations {ai_metrics.get('owner_domain_cited', 0)} · "
                f"fleet citations {ai_metrics.get('fleet_domain_cited', 0)} · "
                f"trend {ai_comparison.get('status') if isinstance(ai_comparison, Mapping) else 'unknown'}. "
                "ChatGPT/Gemini/Claude/Perplexity unmeasured."
            )
        registry = sources.get("measurement_registry")
        summary = registry.get("summary") if isinstance(registry, Mapping) else None
        if isinstance(summary, Mapping):
            expected = summary.get("expected_public_brands")
            fresh = summary.get("gsc_verified_access_or_fresh_data_receipts")
            leads = summary.get("terminal_lead_receipts")
            if any(value is not None for value in (expected, fresh, leads)):
                lines.append(
                    "Proof boundary: GA4 events are not terminal leads; "
                    f"terminal lead receipts={leads if leads is not None else '?'}."
                )
        paid = sources.get("paid_research")
        if isinstance(paid, Mapping):
            provider = paid.get("provider")
            provider_status = provider.get("status") if isinstance(provider, Mapping) else None
            if provider_status:
                lines.append(f"Last paid research: {provider_status}; this daily job spent $0.")
    lines.extend(action_lines)
    return lines


def _control_body(receipt: Mapping[str, Any] | None, *, status: str) -> list[str]:
    if not receipt:
        return ["No fresh control-review receipt was produced. Check the scheduled job log."]
    source_status = receipt.get("source_status")
    if isinstance(source_status, Mapping):
        states = " · ".join(f"{key}={value}" for key, value in source_status.items())
    else:
        states = "Receipt source detail unavailable."
    queue = receipt.get("gsc")
    candidates = queue.get("queue") if isinstance(queue, Mapping) else None
    candidate_count = len(candidates) if isinstance(candidates, list) else 0
    lines = [states]
    mode = str(receipt.get("mode") or "weekly")
    keys = ("7d", "14d", "90d") if mode == "weekly" else ("28d", "90d")
    gsc_windows = queue.get("window_comparisons") if isinstance(queue, Mapping) else None
    if isinstance(gsc_windows, Mapping):
        for key in keys:
            comparison = gsc_windows.get(key)
            if isinstance(comparison, Mapping):
                lines.append(_window_line("GSC", key, comparison))
    ga4 = receipt.get("ga4")
    ga4_summary = ga4.get("summary") if isinstance(ga4, Mapping) else None
    ga4_windows = ga4.get("window_comparisons") if isinstance(ga4, Mapping) else None
    if isinstance(ga4_summary, Mapping):
        lines.append(
            f"GA4 fleet: {ga4_summary.get('properties_ok', 0)}/{ga4_summary.get('expected_properties', 27)} properties readable."
        )
    if isinstance(ga4_windows, Mapping):
        parts: list[str] = []
        for key in keys:
            comparison = ga4_windows.get(key)
            current = comparison.get("current") if isinstance(comparison, Mapping) else None
            previous = comparison.get("previous") if isinstance(comparison, Mapping) else None
            if isinstance(current, Mapping) and isinstance(previous, Mapping):
                value = float(current.get("organic_sessions") or 0)
                prior = float(previous.get("organic_sessions") or 0)
                parts.append(f"{key} {value:,.0f} ({_format_change(value, prior)})")
        if parts:
            lines.append("GA4 organic sessions: " + " · ".join(parts))
    ai = receipt.get("ai_visibility")
    metrics = ai.get("metrics") if isinstance(ai, Mapping) else None
    comparison = ai.get("comparison") if isinstance(ai, Mapping) else None
    if isinstance(metrics, Mapping):
        lines.append(
            f"Google AIO: {metrics.get('ai_overview_present', 0)}/{metrics.get('prompt_count', 0)} · "
            f"owner citations {metrics.get('owner_domain_cited', 0)} · "
            f"trend {comparison.get('status') if isinstance(comparison, Mapping) else 'unknown'}."
        )
    lines.append(
        f"Evidence queue candidates: {candidate_count}. This control review spent $0 and made no site changes."
    )
    return lines


def _paid_body(receipt: Mapping[str, Any] | None, *, status: str) -> list[str]:
    if not receipt:
        return ["No fresh paid-research receipt was produced. Check the scheduled job log before retrying."]
    provider = receipt.get("provider")
    provider = provider if isinstance(provider, Mapping) else {}
    cohort = receipt.get("cohort")
    cohort = cohort if isinstance(cohort, Mapping) else {}
    budget = receipt.get("budget")
    budget = budget if isinstance(budget, Mapping) else {}
    accepted = cohort.get("accepted")
    rejected = cohort.get("rejected")
    results = receipt.get("results")
    results = results if isinstance(results, list) else []
    aio_present = sum(
        bool(row.get("ai_overview_present")) for row in results if isinstance(row, Mapping)
    )
    owner_cited = sum(
        bool(row.get("owner_domain_cited")) for row in results if isinstance(row, Mapping)
    )
    fleet_cited = sum(
        bool(row.get("fleet_cited_domains")) for row in results if isinstance(row, Mapping)
    )
    classifier = provider.get("feature_classifier")
    trend_state = "corrected-classifier baseline" if classifier else "preliminary classifier; not trendable"
    return [
        f"Google AI Overview research: {provider.get('status') or 'unknown'} ({provider.get('operation') or 'unknown operation'}).",
        f"Cohort: accepted={len(accepted) if isinstance(accepted, list) else 0} · rejected={len(rejected) if isinstance(rejected, list) else 0} · charged=${budget.get('charged_usd', 0)}.",
        f"Results: AIO {aio_present}/{len(results)} · owner citations {owner_cited} · fleet citations {fleet_cited} · {trend_state}.",
        "Google AIO only; ChatGPT, Gemini, Claude, and Perplexity are unmeasured. No site, GSC, or social changes were made.",
    ]


def render_message(
    *,
    job: str,
    status: str,
    receipt: Mapping[str, Any] | None,
    exit_code: int | None,
    failure_reason: str | None = None,
    daily_context: Mapping[str, Any] | None = None,
) -> str:
    """Render concise deterministic text. Receipt data is treated as local facts."""

    label = {
        "daily": "Daily fleet pulse",
        "weekly": "Weekly control review",
        "monthly": "Monthly control review",
        "paid": "Weekly paid GEO research",
    }.get(job, job)
    marker = "✅" if status == "completed" else "⚠️"
    generated = receipt.get("generated_at") if isinstance(receipt, Mapping) else None
    lines = [f"{marker} SEO/GEO {label}: {status.upper()}"]
    if generated:
        lines.append(f"Receipt generated: {generated}")
    if job == "daily":
        lines.extend(_daily_body(receipt, status=status, context=daily_context))
    elif job in {"weekly", "monthly"}:
        lines.extend(_control_body(receipt, status=status))
    elif job == "paid":
        lines.extend(_paid_body(receipt, status=status))
    else:
        lines.append("No job-specific summary is configured.")
    if failure_reason:
        lines.append(f"Attention: {failure_reason}")
    if exit_code is not None:
        lines.append(f"Job exit code: {exit_code}.")
    return _clip("\n".join(lines))


def _post(target: DiscordTarget, *, content: str) -> str | None:
    payload = {
        "content": f"<@{target.operator_id}> {content}",
        "allowed_mentions": {
            "parse": [],
            "users": [target.operator_id],
            "roles": [],
            "replied_user": False,
        },
    }
    request = urllib.request.Request(
        f"{DISCORD_API_BASE}/channels/{urllib.parse.quote(target.channel_id, safe='')}/messages",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bot {target.token}",
            "Content-Type": "application/json",
            "User-Agent": "TheHomie-SEO-GEO-Receipt/1.0",
        },
        method="POST",
    )
    for attempt in range(2):
        response = None
        try:
            response = urllib.request.urlopen(request, timeout=12)
            raw = json.loads(response.read().decode("utf-8"))
            message_id = str(raw.get("id") or "").strip() if isinstance(raw, Mapping) else ""
            channel_id = str(raw.get("channel_id") or "").strip() if isinstance(raw, Mapping) else ""
            if message_id and channel_id == target.channel_id:
                return message_id
            return None
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt == 0:
                retry_after = 1.0
                try:
                    retry_after = float(exc.headers.get("Retry-After", "1"))
                except (TypeError, ValueError):
                    pass
                time.sleep(min(max(retry_after, 0.1), 5.0))
                continue
            return None
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        finally:
            if response is not None:
                response.close()
    return None


def _write_receipt(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def notify(
    *,
    job: str,
    status: str,
    receipt_path: Path | None,
    exit_code: int | None,
    failure_reason: str | None = None,
    out_dir: Path = DEFAULT_NOTIFICATION_DIR,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Post one auditable receipt; delivery failure never overwrites job evidence."""

    receipt = _read_json(receipt_path) if receipt_path else None
    daily_context = _daily_context(receipt) if job == "daily" else None
    content = render_message(
        job=job,
        status=status,
        receipt=receipt,
        exit_code=exit_code,
        failure_reason=failure_reason,
        daily_context=daily_context,
    )
    fingerprint = hashlib.sha256(content.encode("utf-8")).hexdigest()
    target = resolve_target()
    outcome = "dry_run" if dry_run else "target_unavailable"
    message_id: str | None = None
    if target is not None:
        if dry_run:
            outcome = "dry_run"
        else:
            message_id = _post(target, content=content)
            outcome = "delivered" if message_id else "delivery_failed"
    payload = {
        "schema_version": 1,
        "persona": PERSONA_ID,
        "job": job,
        "job_status": status,
        "job_exit_code": exit_code,
        "failure_reason": failure_reason,
        "generated_at": datetime.now(UTC).isoformat(),
        "receipt_path": str(receipt_path) if receipt_path else None,
        "receipt_generated_at": receipt.get("generated_at") if receipt else None,
        "delivery": {
            "status": outcome,
            "channel_id": target.channel_id if target else None,
            "message_id": message_id,
            "operator_mention": bool(target),
            "content_sha256": fingerprint,
        },
    }
    _write_receipt(out_dir / f"{job}-latest.json", payload)
    return payload
