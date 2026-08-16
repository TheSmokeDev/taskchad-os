"""Read-only daily evidence loop for the YourBusiness 27-site SEO/GEO fleet.

The job deliberately produces receipts for the ``seo_geo`` Homie instead of
calling a model or changing websites.  It never submits sitemaps, requests
indexing, deploys content, posts to social media, or sends messages.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = Path("~/YourBusiness")
GSC_FLEET_SNAPSHOT = Path.home() / ".codex" / "skills" / "gsc-ops" / "scripts" / "fleet_snapshot.py"
GA4_FLEET_REPORTER = SCRIPTS_DIR / "seo_geo_ga4_fleet.py"
PROFILE_ROOT = Path.home() / ".homie" / "profiles" / "seo_geo"
DEFAULT_OUT_DIR = PROFILE_ROOT / "data" / "fleet-pulse"
GA4_OUT_DIR = PROFILE_ROOT / "data" / "fleet-ga4"
PAID_RESEARCH_ROOT = PROFILE_ROOT / "data" / "fleet-paid-research"
AI_FEATURE_CLASSIFIER = "ai_overview only; featured snippets and answer boxes are separate fields"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _run_gsc_snapshot(out_dir: Path, *, days: int, momentum_days: int, limit: int) -> dict[str, Any]:
    if not GSC_FLEET_SNAPSHOT.is_file():
        return {"status": "unavailable", "reason": "gsc-ops fleet_snapshot.py is not installed"}
    command = [
        sys.executable,
        str(GSC_FLEET_SNAPSHOT),
        "--repo", str(REPO_ROOT),
        "--out-dir", str(out_dir),
        "--days", str(days),
        "--momentum-days", str(momentum_days),
        "--limit", str(limit),
    ]
    try:
        result = subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True, timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "unavailable", "reason": f"fleet snapshot failed ({type(exc).__name__})"}
    stdout = result.stdout[-4000:]
    output: dict[str, Any] = {"status": "ok" if result.returncode == 0 else "error", "exit_code": result.returncode, "stdout": stdout}
    if result.stderr:
        output["stderr_tail"] = result.stderr[-1000:]
    return output


def _run_ga4_fleet(registry: dict[str, Any]) -> dict[str, Any]:
    """Run the exact 27-property, read-only GA4 collector and embed only its rollup."""

    registry_path = registry.get("receipt_json") if isinstance(registry, dict) else None
    if not registry_path or not Path(str(registry_path)).is_file():
        return {"status": "unavailable", "reason": "fresh measurement registry is unavailable"}
    if not GA4_FLEET_REPORTER.is_file():
        return {"status": "unavailable", "reason": "GA4 fleet reporter is not installed"}
    command = [
        sys.executable,
        str(GA4_FLEET_REPORTER),
        "--registry",
        str(registry_path),
        "--out-dir",
        str(GA4_OUT_DIR),
    ]
    try:
        result = subprocess.run(
            command,
            cwd=SCRIPTS_DIR,
            text=True,
            capture_output=True,
            timeout=480,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "unavailable", "reason": f"GA4 fleet read failed ({type(exc).__name__})"}

    latest = GA4_OUT_DIR / "latest.json"
    try:
        payload = json.loads(latest.read_text(encoding="utf-8")) if latest.is_file() else {}
    except (OSError, TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    status = str(payload.get("status") or ("error" if result.returncode else "ok"))
    output: dict[str, Any] = {
        "status": status,
        "scope": "27 registry-mapped GA4 properties; Analytics Data API read only",
        "exit_code": result.returncode,
        "receipt_json": str(latest) if latest.is_file() else None,
        "summary": payload.get("summary", {}),
        "fleet_window_comparisons": payload.get("fleet_window_comparisons", {}),
        "evidence_boundary": payload.get("evidence_boundary"),
        "stdout": result.stdout[-2000:],
    }
    if result.stderr:
        output["stderr_tail"] = result.stderr[-1000:]
    return output


def _openseo_projects() -> dict[str, Any]:
    try:
        from runtime.tool_impl_seo_geo import _openseo_read

        value = _openseo_read("projects")
        return {"status": "ok" if not value.startswith("unavailable:") else "unavailable", "result": value}
    except Exception as exc:  # noqa: BLE001
        return {"status": "unavailable", "reason": f"OpenSEO read failed ({type(exc).__name__})"}


def _firecrawl_state() -> dict[str, Any]:
    from runtime.tool_impl_seo_geo import firecrawl_configured

    return {
        "status": "configured" if firecrawl_configured() else "not_configured",
        "scheduled_use": False,
        "reason": (
            "The daily loop never spends Firecrawl credits. Firecrawl is not part of the brokered "
            "DataForSEO lane and remains disabled for scheduled paid GEO work until it has its own unit-to-dollar cap."
        ),
    }


def _budget_broker_state() -> dict[str, Any]:
    """Read local broker state only after a broker policy already exists.

    The daily pulse must not initialize a policy or make a provider request.
    The paid runner owns broker initialization and all reservations; this
    source is only a compact observer of saved, local control state.
    """
    try:
        import seo_geo_budget_broker as broker
    except ImportError:
        return {"status": "unavailable", "reason": "SEO/GEO budget broker is not installed"}

    broker_root = getattr(broker, "DEFAULT_ROOT", None)
    if not isinstance(broker_root, Path):
        return {"status": "unavailable", "reason": "SEO/GEO budget broker has no local root contract"}
    if not (broker_root / "policy.json").is_file():
        return {
            "status": "not_initialized",
            "reason": "The paid runner has not initialized its local broker policy yet",
            "root": str(broker_root),
        }
    try:
        status = broker.budget_status()
    except Exception as exc:  # noqa: BLE001
        return {"status": "unavailable", "reason": f"broker status read failed ({type(exc).__name__})"}
    if not isinstance(status, dict):
        return {"status": "unavailable", "reason": "broker status was not an object"}
    return {
        "status": "ok",
        "scope": "local broker policy and monthly ledger only; no provider call",
        "root": str(broker_root),
        "summary": status,
    }


def _paid_research_receipt_state() -> dict[str, Any]:
    """Read a compact summary of the last brokered paid-research receipt."""
    path = PAID_RESEARCH_ROOT / "latest.json"
    if not path.is_file():
        return {"status": "missing", "reason": "no brokered paid-research receipt exists yet"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        return {"status": "unavailable", "reason": f"paid-research receipt read failed ({type(exc).__name__})"}
    if not isinstance(payload, dict):
        return {"status": "unavailable", "reason": "paid-research receipt was not an object"}
    provider = payload.get("provider", {})
    cohort = payload.get("cohort", {})
    budget = payload.get("budget", {})
    provider = provider if isinstance(provider, dict) else {}
    cohort = cohort if isinstance(cohort, dict) else {}
    budget = budget if isinstance(budget, dict) else {}
    accepted_count = len(cohort.get("accepted", [])) if isinstance(cohort.get("accepted"), list) else 0
    rejected_count = len(cohort.get("rejected", [])) if isinstance(cohort.get("rejected"), list) else 0
    candidate_count = len(cohort.get("candidates", [])) if isinstance(cohort.get("candidates"), list) else 0
    if not candidate_count and (accepted_count or rejected_count):
        candidate_count = accepted_count + rejected_count
    return {
        "status": "ok",
        "path": str(path),
        "generated_at": payload.get("generated_at"),
        "mode": payload.get("mode"),
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
        "budget": budget,
    }


def _ai_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    results = payload.get("results")
    rows = results if isinstance(results, list) else []
    prompt_count = len(rows)
    aio_present = sum(bool(row.get("ai_overview_present")) for row in rows if isinstance(row, dict))
    owner_cited = sum(bool(row.get("owner_domain_cited")) for row in rows if isinstance(row, dict))
    fleet_cited = sum(bool(row.get("fleet_cited_domains")) for row in rows if isinstance(row, dict))
    return {
        "prompt_count": prompt_count,
        "ai_overview_present": aio_present,
        "ai_overview_rate": round(aio_present / prompt_count, 4) if prompt_count else None,
        "owner_domain_cited": owner_cited,
        "owner_citation_rate": round(owner_cited / prompt_count, 4) if prompt_count else None,
        "fleet_domain_cited": fleet_cited,
        "fleet_citation_rate": round(fleet_cited / prompt_count, 4) if prompt_count else None,
    }


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _ai_visibility_state() -> dict[str, Any]:
    """Summarize saved AI visibility evidence without making a paid provider call."""

    latest_path = PAID_RESEARCH_ROOT / "latest.json"
    if not latest_path.is_file():
        return {
            "status": "missing",
            "provider_calls": 0,
            "platform_coverage": {
                "google_ai_overview": "not_observed",
                "chatgpt": "unmeasured",
                "gemini": "unmeasured",
                "claude": "unmeasured",
                "perplexity": "unmeasured",
            },
        }
    try:
        current = json.loads(latest_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        return {"status": "unavailable", "reason": f"AI receipt read failed ({type(exc).__name__})"}
    if not isinstance(current, dict):
        return {"status": "unavailable", "reason": "AI receipt was not an object"}

    provider = current.get("provider") if isinstance(current.get("provider"), dict) else {}
    classifier = str(provider.get("feature_classifier") or "")
    comparable = classifier == AI_FEATURE_CLASSIFIER
    generated = _parse_time(current.get("generated_at"))
    age_days = round((datetime.now(UTC) - generated.astimezone(UTC)).total_seconds() / 86400, 2) if generated else None
    metrics = _ai_metrics(current)

    previous: dict[str, Any] | None = None
    if comparable:
        candidates: list[tuple[datetime, dict[str, Any]]] = []
        for path in PAID_RESEARCH_ROOT.glob("runs/*/receipt.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            when = _parse_time(payload.get("generated_at"))
            item_provider = payload.get("provider") if isinstance(payload.get("provider"), dict) else {}
            if (
                when
                and generated
                and when < generated
                and item_provider.get("feature_classifier") == AI_FEATURE_CLASSIFIER
            ):
                candidates.append((when, payload))
        if candidates:
            previous = sorted(candidates, key=lambda item: item[0])[-1][1]

    comparison: dict[str, Any]
    if not comparable:
        comparison = {
            "status": "preliminary_not_comparable",
            "reason": "latest receipt predates the corrected Google AI Overview classifier",
        }
    elif not previous:
        comparison = {
            "status": "baseline_only",
            "reason": "one corrected-classifier receipt exists; a second is required for trend",
        }
    else:
        prior_metrics = _ai_metrics(previous)
        comparison = {
            "status": "comparable",
            "previous_generated_at": previous.get("generated_at"),
            "delta": {
                key: metrics[key] - prior_metrics[key]
                for key in ("ai_overview_present", "owner_domain_cited", "fleet_domain_cited")
            },
        }

    return {
        "status": "ok",
        "generated_at": current.get("generated_at"),
        "age_days": age_days,
        "provider_calls": 0,
        "scope": "saved Google AI Overview receipt only; daily pulse spent $0",
        "metrics": metrics,
        "comparison": comparison,
        "platform_coverage": {
            "google_ai_overview": "observed_preliminary" if not comparable else "observed_comparable",
            "chatgpt": "unmeasured",
            "gemini": "unmeasured",
            "claude": "unmeasured",
            "perplexity": "unmeasured",
        },
    }


def _snapshot_path_from_result(result: dict[str, Any]) -> Path | None:
    """Find the JSON receipt emitted by the local GSC fleet snapshot."""
    import re

    match = re.search(r"^SNAPSHOT_JSON=(.+)$", str(result.get("stdout", "")), flags=re.MULTILINE)
    if not match:
        return None
    candidate = Path(match.group(1).strip())
    return candidate if candidate.is_file() else None


def _measurement_registry(gsc_snapshot_path: Path | None = None) -> dict[str, Any]:
    """Refresh the local 27-brand evidence registry without provider calls."""
    try:
        from seo_geo_measurement_registry import build_registry, write_registry

        registry = build_registry(REPO_ROOT, gsc_snapshot_path=gsc_snapshot_path)
        json_path, markdown_path = write_registry(
            registry,
            PROFILE_ROOT / "data" / "fleet-measurement",
        )
        return {
            "status": "ok",
            "scope": "local source configuration only",
            "summary": registry.get("summary", {}),
            "receipt_json": str(json_path),
            "receipt_markdown": str(markdown_path),
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "unavailable", "reason": f"measurement registry failed ({type(exc).__name__})"}


def _render_report(receipt: dict[str, Any]) -> str:
    gsc = receipt["sources"]["gsc"]
    ga4 = receipt["sources"]["ga4"]
    openseo = receipt["sources"]["openseo"]
    firecrawl = receipt["sources"]["firecrawl"]
    registry = receipt["sources"]["measurement_registry"]
    broker = receipt["sources"]["budget_broker"]
    paid_research = receipt["sources"]["paid_research"]
    ai_visibility = receipt["sources"]["ai_visibility"]
    return "\n".join([
        "# SEO/GEO Fleet Pulse",
        "",
        f"- Generated: {receipt['generated_at']}",
        "- Mode: read-only. No sitemap submission, indexing request, deploy, social post, or form action occurred.",
        f"- GSC fleet snapshot: `{gsc['status']}`",
        f"- GA4 exact 27-property fleet receipt: `{ga4['status']}`",
        f"- OpenSEO free-read availability: `{openseo['status']}`",
        f"- Firecrawl availability: `{firecrawl['status']}` (not used by the daily job)",
        f"- DataForSEO budget broker: `{broker['status']}` (local ledger read only)",
        f"- Brokered paid-research receipt: `{paid_research['status']}` (not invoked by the daily job)",
        f"- Saved AI-visibility analytics: `{ai_visibility['status']}` (Google AI Overview only; no daily spend)",
        f"- Per-brand measurement registry: `{registry['status']}` (configuration/source evidence only)",
        "",
        "Ask SEO/GEO Homie to read this receipt and turn it into a ranked, approval-only action queue.",
    ]) + "\n"


def run(*, out_dir: Path, days: int, momentum_days: int, limit: int) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    gsc = _run_gsc_snapshot(out_dir, days=days, momentum_days=momentum_days, limit=limit)
    registry = _measurement_registry(_snapshot_path_from_result(gsc))
    ga4 = _run_ga4_fleet(registry)
    receipt = {
        "schema_version": 4,
        "persona": "seo_geo",
        "generated_at": datetime.now(UTC).isoformat(),
        "read_only": True,
        "mutations": [],
        "sources": {
            "gsc": gsc,
            "ga4": ga4,
            "openseo": _openseo_projects(),
            "firecrawl": _firecrawl_state(),
            "budget_broker": _budget_broker_state(),
            "paid_research": _paid_research_receipt_state(),
            "ai_visibility": _ai_visibility_state(),
            "measurement_registry": registry,
        },
    }
    _write_json(out_dir / "latest.json", receipt)
    (out_dir / "latest.md").write_text(_render_report(receipt), encoding="utf-8")
    print(f"RECEIPT_JSON={out_dir / 'latest.json'}")
    print(f"RECEIPT_MD={out_dir / 'latest.md'}")
    print("MUTATIONS=0")
    return 0 if receipt["sources"]["gsc"]["status"] == "ok" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the read-only SEO/GEO fleet evidence loop.")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--days", type=int, default=28)
    parser.add_argument("--momentum-days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    if args.days < 1 or args.momentum_days < 1 or args.limit < 1:
        parser.error("days, momentum-days, and limit must be positive")
    return run(out_dir=args.out_dir, days=args.days, momentum_days=args.momentum_days, limit=args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
