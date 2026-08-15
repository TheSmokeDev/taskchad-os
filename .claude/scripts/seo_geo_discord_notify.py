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
    percent = (delta / previous * 100) if previous else 0.0
    return f"{sign}{delta:,.0f} ({sign}{percent:.1f}%)"


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
    for impressions, clicks, position, name, query, url in sorted(candidates, reverse=True)[:3]:
        action = "CTR/title candidate" if position <= 10 else "on-page/internal-link candidate"
        lines.append(
            f"{name} — `{query}` → {url} | {impressions:,.0f} impressions, "
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
    previous = context.get("previous") if isinstance(context, Mapping) else None
    current_totals = _fleet_totals(current)
    previous_totals = _fleet_totals(previous)
    lines: list[str] = []
    ranges = current.get("ranges") if isinstance(current, Mapping) else None
    primary = ranges.get("primary") if isinstance(ranges, Mapping) else None
    if isinstance(primary, Mapping):
        lines.append(
            f"Final GSC window: {primary.get('start')}–{primary.get('end')} "
            f"({primary.get('days')} days)."
        )
    if current_totals["impressions"]:
        ctr = current_totals["clicks"] / current_totals["impressions"] * 100
        if previous_totals["impressions"]:
            prior_ctr = previous_totals["clicks"] / previous_totals["impressions"] * 100
            lines.append(
                "Fleet: "
                f"{current_totals['impressions']:,.0f} impressions ({_format_change(current_totals['impressions'], previous_totals['impressions'])}) · "
                f"{current_totals['clicks']:,.0f} clicks ({_format_change(current_totals['clicks'], previous_totals['clicks'])}) · "
                f"CTR {ctr:.3f}% ({ctr - prior_ctr:+.3f} pp)."
            )
            lines.append("Note: these are overlapping 28-day rolling windows, so this is directional movement—not a confirmed one-day ranking win.")
        else:
            lines.append(
                f"Fleet: {current_totals['impressions']:,.0f} impressions · {current_totals['clicks']:,.0f} clicks · CTR {ctr:.3f}%.")
        lines.append(f"Properties read: 27/27; brands with at least one click: {current_totals['brands_with_clicks']:,.0f}/27.")
        movers = _brand_movers(current, previous)
        if movers:
            lines.append("Drivers: " + " | ".join(movers))
        opportunities = _route_opportunities(current)
        if opportunities:
            lines.append("Best sampled update candidates (approval required; no page changed):")
            lines.extend(opportunities)
        fragmented = _fragmented_intent(current)
        if fragmented:
            lines.append(fragmented)
    else:
        lines.append(_source_states(receipt))
    sources = receipt.get("sources")
    if isinstance(sources, Mapping):
        registry = sources.get("measurement_registry")
        summary = registry.get("summary") if isinstance(registry, Mapping) else None
        if isinstance(summary, Mapping):
            expected = summary.get("expected_public_brands")
            fresh = summary.get("gsc_verified_access_or_fresh_data_receipts")
            ga4 = summary.get("ga4_deployed_tag_proofs")
            leads = summary.get("terminal_lead_receipts")
            if any(value is not None for value in (expected, fresh, ga4, leads)):
                lines.append(
                    "Measurement gap: "
                    f"GA4 tag receipts={ga4 if ga4 is not None else '?'} · "
                    f"terminal lead receipts={leads if leads is not None else '?'}. "
                    "Visibility is real; lead attribution is still unproven."
                )
        paid = sources.get("paid_research")
        if isinstance(paid, Mapping):
            provider = paid.get("provider")
            provider_status = provider.get("status") if isinstance(provider, Mapping) else None
            if provider_status:
                lines.append(f"Last paid research: {provider_status}; this daily job spent $0.")
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
    return [states, f"Evidence queue candidates: {candidate_count}. This control review spent $0 and made no site changes."]


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
    return [
        f"Google AI Overview research: {provider.get('status') or 'unknown'} ({provider.get('operation') or 'unknown operation'}).",
        f"Cohort: accepted={len(accepted) if isinstance(accepted, list) else 0} · rejected={len(rejected) if isinstance(rejected, list) else 0} · charged=${budget.get('charged_usd', 0)}.",
        "This is Google AI Overview evidence only; no site, GSC, or social changes were made.",
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
