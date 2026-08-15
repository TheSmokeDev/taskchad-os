"""Render no-spend weekly/monthly SEO/GEO control receipts from saved evidence.

This program never calls a provider.  It reads the deterministic daily
fleet-pulse, its finalized GSC snapshot, and the per-brand measurement
registry, then writes a compact action queue for the SEO/GEO Homie to review.
It does not invoke a model, spend Firecrawl/OpenSEO/DataForSEO credits, submit
anything to search engines, deploy, post, or touch lead records.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


PROFILE_ROOT = Path.home() / ".homie" / "profiles" / "seo_geo"
DEFAULT_PULSE_DIR = PROFILE_ROOT / "data" / "fleet-pulse"
DEFAULT_REGISTRY_DIR = PROFILE_ROOT / "data" / "fleet-measurement"
DEFAULT_PAID_RESEARCH_DIR = PROFILE_ROOT / "data" / "fleet-paid-research"
DEFAULT_OUT_DIR = PROFILE_ROOT / "data" / "fleet-control"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _path_from_gsc_stdout(pulse: dict[str, Any]) -> Path | None:
    stdout = str(pulse.get("sources", {}).get("gsc", {}).get("stdout", ""))
    match = re.search(r"^SNAPSHOT_JSON=(.+)$", stdout, flags=re.MULTILINE)
    if not match:
        return None
    candidate = Path(match.group(1).strip())
    return candidate if candidate.is_file() else None


def _registry_path(registry_dir: Path) -> Path | None:
    for name in ("latest.json", "registry-latest.json"):
        candidate = registry_dir / name
        if candidate.is_file():
            return candidate
    candidates = sorted(registry_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _paid_research_path(paid_research_dir: Path) -> Path | None:
    """Return the deterministic latest paid-research receipt, if one exists.

    The control review does not invoke the paid runner.  This is deliberately a
    local file read so a historical provider receipt can inform the queue
    without spending money again.
    """
    candidate = paid_research_dir / "latest.json"
    return candidate if candidate.is_file() else None


def _count(value: Any) -> int:
    if isinstance(value, (list, tuple, set, dict)):
        return len(value)
    return _to_int(value)


def _paid_research_summary(receipt: dict[str, Any]) -> dict[str, Any]:
    """Keep only receipt-level, non-sensitive paid-research evidence."""
    provider = receipt.get("provider", {})
    cohort = receipt.get("cohort", {})
    budget = receipt.get("budget", {})
    provider = provider if isinstance(provider, dict) else {}
    cohort = cohort if isinstance(cohort, dict) else {}
    budget = budget if isinstance(budget, dict) else {}
    accepted_count = _count(cohort.get("accepted"))
    rejected_count = _count(cohort.get("rejected"))
    candidate_count = _count(cohort.get("candidates"))
    if not candidate_count and (accepted_count or rejected_count):
        candidate_count = accepted_count + rejected_count
    return {
        "state": "present" if receipt else "missing",
        "generated_at": receipt.get("generated_at"),
        "mode": receipt.get("mode"),
        "provider": {
            "name": provider.get("name"),
            "operation": provider.get("operation"),
            "scope": provider.get("scope"),
            "status": provider.get("status"),
        },
        "cohort": {
            "id": cohort.get("id"),
            "candidate_count": candidate_count,
            "accepted_count": accepted_count,
            "rejected_count": rejected_count,
        },
        # The receipt's budget is evidence from a previous brokered run. It is
        # not spend by this control-review process.
        "budget": budget,
    }


def _to_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _alerts(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    for brand in snapshot.get("brands", []):
        if not isinstance(brand, dict):
            continue
        brand_id = str(brand.get("brand_id", "unknown"))
        status = str(brand.get("status", "unknown"))
        if status != "ok":
            alerts.append({"severity": "warning", "brand_id": brand_id, "type": "gsc_status", "detail": status})
        for sitemap in brand.get("sitemaps", []):
            if not isinstance(sitemap, dict):
                continue
            errors = _to_int(sitemap.get("errors"))
            warnings = _to_int(sitemap.get("warnings"))
            if errors:
                alerts.append({"severity": "high", "brand_id": brand_id, "type": "sitemap_errors", "detail": errors})
            if warnings:
                alerts.append({"severity": "warning", "brand_id": brand_id, "type": "sitemap_warnings", "detail": warnings})
    return alerts


def _queue(snapshot: dict[str, Any], max_candidates: int) -> list[dict[str, Any]]:
    entries = snapshot.get("recommendations", [])
    if not isinstance(entries, list):
        return []
    queue: list[dict[str, Any]] = []
    for entry in entries[:max_candidates]:
        if not isinstance(entry, dict):
            continue
        queue.append({
            "brand_id": entry.get("brand_id"),
            "domain": entry.get("domain"),
            "score": entry.get("score"),
            "top_nonbrand_query": entry.get("top_nonbrand_query"),
            "reasons": entry.get("reasons", []),
            "next_evidence": "Use gsc_query_page_slice before proposing any content, consolidation, or metadata change.",
            "change_state": "approval_required",
        })
    return queue


def build_review(
    *,
    mode: str,
    pulse_dir: Path,
    registry_dir: Path,
    paid_research_dir: Path = DEFAULT_PAID_RESEARCH_DIR,
    max_candidates: int,
) -> dict[str, Any]:
    pulse_path = pulse_dir / "latest.json"
    pulse = _read_json(pulse_path)
    snapshot_path = _path_from_gsc_stdout(pulse)
    snapshot = _read_json(snapshot_path) if snapshot_path else {}
    registry_path = _registry_path(registry_dir)
    registry = _read_json(registry_path) if registry_path else {}
    paid_research_path = _paid_research_path(paid_research_dir)
    paid_research = _read_json(paid_research_path) if paid_research_path else {}

    sources = {
        "daily_pulse": str(pulse_path) if pulse_path.is_file() else None,
        "gsc_snapshot": str(snapshot_path) if snapshot_path else None,
        "measurement_registry": str(registry_path) if registry_path else None,
        "paid_research_receipt": str(paid_research_path) if paid_research_path else None,
    }
    source_status = {
        name: "ok" if path else "unavailable"
        for name, path in sources.items()
    }
    ranges = snapshot.get("ranges", {}) if isinstance(snapshot, dict) else {}
    return {
        "schema_version": 2,
        "mode": mode,
        "generated_at": datetime.now(UTC).isoformat(),
        "read_only": True,
        "mutations": [],
        "spend": {"firecrawl": 0, "openseo": 0, "dataforseo": 0, "model": 0},
        "sources": sources,
        "source_status": source_status,
        "gsc": {
            "finalized_window": ranges.get("primary"),
            "brand_count": len(snapshot.get("brands", [])) if isinstance(snapshot.get("brands"), list) else 0,
            "queue": _queue(snapshot, max_candidates),
            "alerts": _alerts(snapshot),
            "limitations": [
                "GSC query/page rows are bounded samples, not a complete inventory.",
                "Sitemap submitted/indexed fields are reports, not URL-level index proof.",
            ],
        },
        "measurement": {
            "summary": registry.get("summary", {}) if registry else {},
            "state": "present" if registry else "missing",
            "limitations": [
                "Declared GA4 properties are not production-tag or event proof.",
                "A persisted lead is not a contacted, qualified, or sold lead without terminal receipts.",
            ],
        },
        "paid_research": {
            "receipt": _paid_research_summary(paid_research),
            "limitations": [
                "The control review only reads a saved receipt; it does not call a paid provider.",
                "DataForSEO Google AI Overview evidence is not evidence of ChatGPT, Gemini, Claude, or Perplexity visibility.",
            ],
        },
        "approval_gates": [
            "No SEO/GEO website change is applied from this receipt.",
            "No GSC sitemap, property, or indexing mutation is performed.",
            "DataForSEO research is permitted only through the approved budget broker, its monthly ledger, and a saved receipt; this review never invokes it.",
            "Firecrawl, paid OpenSEO SERP, and other paid AI-visibility runs remain disabled until each has its own approved unit-to-dollar cap.",
            "No social post, outreach, email, or lead action is performed.",
        ],
    }


def render_markdown(review: dict[str, Any]) -> str:
    lines = [
        f"# SEO/GEO {str(review['mode']).title()} Control Review",
        "",
        f"- Generated: {review['generated_at']}",
        "- Mode: deterministic, read-only, zero-spend receipt; it did not call a model or a paid provider.",
        f"- GSC sources: `{review['source_status']['gsc_snapshot']}`; registry: `{review['source_status']['measurement_registry']}`.",
        f"- Paid-research receipt: `{review['source_status']['paid_research_receipt']}`; this review made no paid-provider call.",
        "",
        "## Evidence Queue",
        "",
    ]
    queue = review["gsc"]["queue"]
    if queue:
        for item in queue:
            lines.append(f"- **{item.get('brand_id')}** — score `{item.get('score')}`, query `{item.get('top_nonbrand_query') or '-'}`")
            lines.append(f"  - {item['next_evidence']}")
    else:
        lines.append("- No GSC queue is available yet. Run the daily fleet pulse first.")

    lines.extend(["", "## Data-quality Alerts", ""])
    alerts = review["gsc"]["alerts"]
    if alerts:
        for alert in alerts:
            lines.append(f"- `{alert['severity']}` {alert['brand_id']}: {alert['type']} = {alert['detail']}")
    else:
        lines.append("- No alert data is available.")

    lines.extend(["", "## Brokered Paid-research Evidence", ""])
    paid = review["paid_research"]["receipt"]
    if paid["state"] == "present":
        provider = paid["provider"]
        cohort = paid["cohort"]
        lines.append(
            "- `{name}` / `{operation}`: `{status}`; cohort `{cohort_id}` accepted "
            "`{accepted}` of `{candidates}` candidates.".format(
                name=provider.get("name") or "unknown",
                operation=provider.get("operation") or "unknown",
                status=provider.get("status") or "unknown",
                cohort_id=cohort.get("id") or "unknown",
                accepted=cohort.get("accepted_count", 0),
                candidates=cohort.get("candidate_count", 0),
            )
        )
        lines.append("- This is saved Google AI Overview research evidence only; it does not prove other AI-platform visibility.")
    else:
        lines.append("- No brokered paid-research receipt is available yet.")

    lines.extend(["", "## Gates", ""])
    lines.extend(f"- {gate}" for gate in review["approval_gates"])
    lines.append("")
    return "\n".join(lines)


def run(
    *,
    mode: str,
    out_dir: Path,
    pulse_dir: Path,
    registry_dir: Path,
    paid_research_dir: Path = DEFAULT_PAID_RESEARCH_DIR,
    max_candidates: int,
) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    review = build_review(
        mode=mode,
        pulse_dir=pulse_dir,
        registry_dir=registry_dir,
        paid_research_dir=paid_research_dir,
        max_candidates=max_candidates,
    )
    prefix = "weekly" if mode == "weekly" else "monthly"
    stamp = datetime.now().date().isoformat()
    json_path = out_dir / f"{stamp}-{prefix}.json"
    md_path = out_dir / f"{stamp}-{prefix}.md"
    _write_json(json_path, review)
    md_path.write_text(render_markdown(review), encoding="utf-8")
    _write_json(out_dir / f"{prefix}-latest.json", review)
    (out_dir / f"{prefix}-latest.md").write_text(render_markdown(review), encoding="utf-8")
    print(f"REVIEW_JSON={json_path}")
    print(f"REVIEW_MD={md_path}")
    print("MUTATIONS=0")
    print("SPEND=0")
    return 0 if review["source_status"]["daily_pulse"] == "ok" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Render zero-spend SEO/GEO control reviews from existing receipts.")
    parser.add_argument("--mode", choices=("weekly", "monthly"), default="weekly")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--pulse-dir", type=Path, default=DEFAULT_PULSE_DIR)
    parser.add_argument("--registry-dir", type=Path, default=DEFAULT_REGISTRY_DIR)
    parser.add_argument("--paid-research-dir", type=Path, default=DEFAULT_PAID_RESEARCH_DIR)
    parser.add_argument("--max-candidates", type=int, default=5)
    args = parser.parse_args()
    if args.max_candidates < 1 or args.max_candidates > 27:
        parser.error("max-candidates must be between 1 and 27")
    return run(
        mode=args.mode,
        out_dir=args.out_dir,
        pulse_dir=args.pulse_dir,
        registry_dir=args.registry_dir,
        paid_research_dir=args.paid_research_dir,
        max_candidates=args.max_candidates,
    )


if __name__ == "__main__":
    raise SystemExit(main())
