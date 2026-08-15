"""Bounded, brokered Google AI Overview research for the YourBusiness fleet.

This is intentionally a *research-only* runner.  It performs a fresh,
finalized GSC query/page ownership check before it sends a single paid request,
and it never edits a site, submits a sitemap, requests indexing, posts to a
social account, or touches a lead form.

Only the ``geo-mentions`` DataForSEO command is allowed here.  Its output is
Google AI Overview evidence, not evidence of citations in ChatGPT, Claude,
Gemini, Perplexity, or any other AI product.
"""

import argparse
import hashlib
import importlib
import json
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PROFILE_ROOT = Path.home() / ".homie" / "profiles" / "seo_geo"
DEFAULT_OUT_DIR = PROFILE_ROOT / "data" / "fleet-paid-research"
FLEET_REGISTRY_PATH = PROFILE_ROOT / "data" / "fleet-measurement" / "registry.json"
DATAFORSEO_CLI = Path.home() / ".claude" / "skills" / "dataforseo" / "scripts" / "dataforseo.py"

COHORT_ID = "five-brand-candidate-google-aio-v1"
PER_TERM_ESTIMATE_USD = 0.002
# The upstream CLI rounds each invocation independently.  A one-cent floor
# keeps the broker conservative while still making the five eligible-term
# baseline exactly $0.0100 under its local price table.
MIN_PRODUCTION_RESERVATION_USD = 0.01
DEFAULT_TIMEOUT_SECONDS = 180


# This list deliberately keeps all ten original candidates.  The current
# GSC preflight decides which ones are eligible.  A candidate with multiple
# current page owners (including apex/www splits) cannot spend money here.
COHORT_CANDIDATES: tuple[dict[str, str], ...] = (
    {
        "brand_id": "YourBusiness",
        "brand_name": "YourBusiness",
        "domain": "your-business.example.com",
        "site_url": "sc-domain:your-business.example.com",
        "query": "dmv sr22 form",
    },
    {
        "brand_id": "YourBusiness",
        "brand_name": "YourBusiness",
        "domain": "your-business.example.com",
        "site_url": "sc-domain:your-business.example.com",
        "query": "car insurance no license",
    },
    {
        "brand_id": "high-risk-auto-ca",
        "brand_name": "High Risk Auto CA",
        "domain": "highriskautoca.com",
        "site_url": "sc-domain:highriskautoca.com",
        "query": "high risk auto insurance fresno",
    },
    {
        "brand_id": "high-risk-auto-ca",
        "brand_name": "High Risk Auto CA",
        "domain": "highriskautoca.com",
        "site_url": "sc-domain:highriskautoca.com",
        "query": "high risk car insurance california",
    },
    {
        "brand_id": "ie-auto-insurance",
        "brand_name": "IE Auto Insurance",
        "domain": "ieautoinsurance.com",
        "site_url": "sc-domain:ieautoinsurance.com",
        "query": "car insurance quotes ontario ca",
        "blocked_reason": "url_inspection_noindex_2026-08-12",
    },
    {
        "brand_id": "ie-auto-insurance",
        "brand_name": "IE Auto Insurance",
        "domain": "ieautoinsurance.com",
        "site_url": "sc-domain:ieautoinsurance.com",
        "query": "auto insurance inland empire",
    },
    {
        "brand_id": "cheap-sr22-california",
        "brand_name": "Cheap SR-22 California",
        "domain": "cheapsr22california.com",
        "site_url": "sc-domain:cheapsr22california.com",
        "query": "cheap sr22 insurance california",
    },
    {
        "brand_id": "cheap-sr22-california",
        "brand_name": "Cheap SR-22 California",
        "domain": "cheapsr22california.com",
        "site_url": "sc-domain:cheapsr22california.com",
        "query": "cheap sr22 california",
    },
    {
        "brand_id": "sac-auto-insurance",
        "brand_name": "SAC Auto Insurance",
        "domain": "sacautoinsurance.com",
        "site_url": "sc-domain:sacautoinsurance.com",
        "query": "car insurance quotes sacramento",
    },
    {
        "brand_id": "sac-auto-insurance",
        "brand_name": "SAC Auto Insurance",
        "domain": "sacautoinsurance.com",
        "site_url": "sc-domain:sacautoinsurance.com",
        "query": "car insurance sacramento",
    },
)


class PaidResearchError(RuntimeError):
    """Expected safe failure: no paid provider request should follow."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _safe_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def _safe_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(value, encoding="utf-8")
    temp.replace(path)


def _query_page_slice(*, site_url: str, query: str, days: int) -> dict[str, Any]:
    """Read only one bounded, finalized GSC query/page slice."""
    from integrations.search_console_api import get_query_page_slice

    return get_query_page_slice(
        site_url=site_url,
        query=query,
        days=days,
        max_results=20,
        start_row=0,
    )


def _normalized_host(url: str) -> str:
    raw = str(url or "").strip()
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    host = (parsed.hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _fleet_domains(path: Path = FLEET_REGISTRY_PATH) -> set[str]:
    """Load the exact 27-brand local registry before paid research.

    The registry is already produced by the zero-spend daily evidence loop.  A
    missing or malformed roster is a no-go: this runner must not claim fleet
    citation evidence from a hand-maintained or stale 28-domain provider list.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise PaidResearchError(f"fleet_registry_unavailable:{type(exc).__name__}") from exc
    brands = payload.get("brands") if isinstance(payload, Mapping) else None
    if not isinstance(brands, list):
        raise PaidResearchError("fleet_registry_malformed")
    domains = {
        _normalized_host(str(item.get("domain", "")))
        for item in brands
        if isinstance(item, Mapping) and str(item.get("domain", "")).strip()
    }
    if len(domains) != 27:
        raise PaidResearchError(f"fleet_registry_expected_27_domains_got_{len(domains)}")
    return domains


def _preflight_candidate(candidate: Mapping[str, str], *, days: int) -> dict[str, Any]:
    """Require exactly one current GSC page owner for a candidate query."""
    base = dict(candidate)
    if base.get("blocked_reason"):
        return {
            **base,
            "eligible": False,
            "reason": str(base["blocked_reason"]),
        }
    try:
        evidence = _query_page_slice(
            site_url=base["site_url"], query=base["query"], days=days
        )
    except Exception as exc:  # noqa: BLE001 - fail closed before paid work
        return {
            **base,
            "eligible": False,
            "reason": "gsc_unavailable",
            "error_class": type(exc).__name__,
        }

    rows = evidence.get("rows", [])
    if not isinstance(rows, list):
        return {**base, "eligible": False, "reason": "gsc_malformed_response"}
    if bool(evidence.get("at_limit")):
        return {
            **base,
            "eligible": False,
            "reason": "gsc_slice_at_limit",
            "gsc_evidence": evidence,
        }

    owners = sorted(
        {
            str(row.get("page", "")).strip()
            for row in rows
            if isinstance(row, Mapping) and str(row.get("page", "")).strip()
        }
    )
    if not owners:
        return {
            **base,
            "eligible": False,
            "reason": "no_current_gsc_page_owner",
            "gsc_evidence": evidence,
        }
    if len(owners) != 1:
        return {
            **base,
            "eligible": False,
            "reason": "multiple_current_gsc_page_owners",
            "owner_pages": owners,
            "gsc_evidence": evidence,
        }

    owner_url = owners[0]
    if _normalized_host(owner_url) != base["domain"].lower():
        return {
            **base,
            "eligible": False,
            "reason": "gsc_owner_host_mismatch",
            "owner_pages": owners,
            "gsc_evidence": evidence,
        }
    return {
        **base,
        "eligible": True,
        "canonical_owner_url": owner_url,
        "gsc_evidence": evidence,
    }


def preflight_candidates(
    candidates: Sequence[Mapping[str, str]] = COHORT_CANDIDATES,
    *,
    days: int = 28,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return eligible and rejected candidates without any paid request."""
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for candidate in candidates:
        result = _preflight_candidate(candidate, days=days)
        (accepted if result["eligible"] else rejected).append(result)
    return accepted, rejected


def _estimate_usd(accepted_count: int) -> float:
    if accepted_count < 1:
        return 0.0
    return round(
        max(MIN_PRODUCTION_RESERVATION_USD, accepted_count * PER_TERM_ESTIMATE_USD),
        4,
    )


def _manifest_hash(accepted: Sequence[Mapping[str, Any]]) -> str:
    terms = [
        {
            "brand_id": item["brand_id"],
            "domain": item["domain"],
            "query": item["query"],
            "canonical_owner_url": item["canonical_owner_url"],
        }
        for item in accepted
    ]
    encoded = json.dumps(terms, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _weekly_idempotency_key(*, manifest_hash: str, now: datetime) -> str:
    iso_year, iso_week, _ = now.isocalendar()
    encoded = json.dumps(
        {
            "cohort": COHORT_ID,
            "operation": "geo-mentions",
            "week": f"{iso_year}-W{iso_week:02d}",
            "manifest_hash": manifest_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _broker_module() -> Any:
    try:
        return importlib.import_module("seo_geo_budget_broker")
    except Exception as exc:  # noqa: BLE001
        raise PaidResearchError(f"budget_broker_unavailable:{type(exc).__name__}") from exc


def _broker_call(name: str, **kwargs: Any) -> dict[str, Any]:
    """Call the intentionally narrow broker API, validating its receipt."""
    module = _broker_module()
    callable_value: Callable[..., Any] | None = getattr(module, name, None)
    if not callable(callable_value):
        raise PaidResearchError(f"budget_broker_missing_{name}")
    result = callable_value(**kwargs)
    if not isinstance(result, Mapping):
        raise PaidResearchError(f"budget_broker_malformed_{name}")
    return dict(result)


def _reserve_production(
    *,
    idempotency_key: str,
    estimated_usd: float,
    manifest_hash: str,
    units: int,
) -> dict[str, Any]:
    return _broker_call(
        "reserve",
        provider="dataforseo",
        operation="geo-mentions",
        cohort=COHORT_ID,
        idempotency_key=idempotency_key,
        estimated_usd=estimated_usd,
        metadata={
            "manifest_hash": manifest_hash,
            "scope": "google_ai_overview_only",
            "units": units,
        },
    )


def _settle_production(
    *,
    broker_run_id: str,
    estimated_usd: float,
    artifact_paths: Sequence[str],
    provider_status: str,
    details: Mapping[str, Any],
) -> dict[str, Any]:
    return _broker_call(
        "settle",
        run_id=broker_run_id,
        actual_usd=estimated_usd,
        artifact_paths=list(artifact_paths),
        provider_status=provider_status,
        details=dict(details),
    )


def _mark_unknown(
    *,
    broker_run_id: str,
    reason: str,
    artifact_paths: Sequence[str],
    details: Mapping[str, Any],
) -> dict[str, Any]:
    return _broker_call(
        "mark_unknown",
        run_id=broker_run_id,
        reason=reason,
        artifact_paths=list(artifact_paths),
        details=dict(details),
    )


def _broker_status() -> dict[str, Any]:
    return _broker_call("budget_status")


def _dataforseo_command(
    *,
    keywords: Sequence[str],
    output_path: Path,
    mode: str,
    estimated_usd: float,
) -> list[str]:
    """Build the only allowed paid command; global flags stay after command."""
    command = [
        sys.executable,
        str(DATAFORSEO_CLI),
        "geo-mentions",
    ]
    if mode == "sandbox":
        command.append("--sandbox")
    command.extend([
        "--max-cost",
        f"{0.0 if mode == 'sandbox' else estimated_usd:.4f}",
        "--format",
        "json",
        "--output",
        str(output_path),
        "--location",
        "2840",
    ])
    for keyword in keywords:
        command.extend(["--keyword", keyword])
    return command


def _load_provider_output(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PaidResearchError(f"provider_receipt_malformed:{type(exc).__name__}") from exc
    if not isinstance(payload, list) or not all(isinstance(item, Mapping) for item in payload):
        raise PaidResearchError("provider_receipt_malformed:unexpected_shape")
    return [dict(item) for item in payload]


def _annotate_results(
    results: Sequence[Mapping[str, Any]],
    *,
    accepted: Sequence[Mapping[str, Any]],
    fleet_domains: set[str],
) -> list[dict[str, Any]]:
    """Attach owner and fleet citation facts without trusting one target domain.

    ``geo-mentions`` can accept one domain flag, but this cohort spans several
    brands.  We therefore request raw cited domains and calculate both the
    owning brand's citation and all observed 27-brand fleet citations locally.
    """
    owners = {str(item["query"]): item for item in accepted}
    annotated: list[dict[str, Any]] = []
    for raw in results:
        item = dict(raw)
        owner = owners.get(str(item.get("keyword", "")))
        cited_values = item.get("cited_domains", [])
        cited = {
            _normalized_host(str(value))
            for value in cited_values
            if str(value).strip()
        } if isinstance(cited_values, list) else set()
        if owner:
            owner_domain = _normalized_host(str(owner["domain"]))
            item["owner_brand_id"] = owner["brand_id"]
            item["owner_domain"] = owner_domain
            item["owner_domain_cited"] = owner_domain in cited
            item["fleet_cited_domains"] = sorted(cited & fleet_domains)
        else:
            item["owner_domain_cited"] = False
            item["fleet_cited_domains"] = sorted(cited & fleet_domains)
        annotated.append(item)
    return annotated


def _render_markdown(receipt: Mapping[str, Any]) -> str:
    cohort = receipt["cohort"]
    provider = receipt["provider"]
    budget = receipt["budget"]
    lines = [
        "# Brokered SEO/GEO Paid Research",
        "",
        f"- Generated: {receipt['generated_at']}",
        f"- Mode: `{receipt['mode']}`",
        "- Scope: Google AI Overview evidence only; this does not measure other AI products.",
        "- Site mutations: `0` (no content, deploy, sitemap, indexing, social, or lead action).",
        f"- Provider status: `{provider['status']}`",
        f"- Eligible terms: `{len(cohort['accepted'])}`; rejected by strict GSC owner gate: "
        f"`{len(cohort['rejected'])}`.",
        f"- Reserved/estimated spend: `${budget['estimated_usd']:.4f}`.",
        "",
        "## GSC ownership gate",
        "",
    ]
    for item in cohort["accepted"]:
        lines.append(f"- PASS `{item['query']}` -> `{item['canonical_owner_url']}`")
    for item in cohort["rejected"]:
        lines.append(f"- NO-GO `{item['query']}`: `{item['reason']}`")
    lines.extend(["", "## Provider results", ""])
    for item in receipt.get("results", []):
        if "error" in item:
            lines.append(f"- `{item.get('keyword', 'unknown')}`: provider error `{item['error']}`")
        else:
            cited = "yes" if item.get("owner_domain_cited") else "no"
            lines.append(
                f"- `{item.get('keyword', 'unknown')}`: "
                f"AIO present `{item.get('ai_overview_present')}`, "
                f"owner `{item.get('owner_domain', 'unknown')}` cited `{cited}`, "
                f"fleet citations `{len(item.get('fleet_cited_domains', []))}`."
            )
    return "\n".join(lines) + "\n"


def _artifact_paths(*, run_dir: Path, out_dir: Path) -> dict[str, str]:
    return {
        "run_dir": str(run_dir),
        "manifest_json": str(run_dir / "manifest.json"),
        "provider_json": str(run_dir / "geo-mentions.json"),
        "receipt_json": str(run_dir / "receipt.json"),
        "receipt_markdown": str(run_dir / "receipt.md"),
        "latest_json": str(out_dir / "latest.json"),
        "latest_markdown": str(out_dir / "latest.md"),
    }


def _broker_artifact_paths(artifacts: Mapping[str, str]) -> list[str]:
    """Broker stores a flat, immutable artifact path list in its ledger."""
    return [str(path) for path in artifacts.values()]


def run(
    *,
    mode: str,
    out_dir: Path = DEFAULT_OUT_DIR,
    days: int = 28,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    candidates: Sequence[Mapping[str, str]] = COHORT_CANDIDATES,
) -> dict[str, Any]:
    """Run the bounded cohort and return its local receipt.

    In production mode the broker reservation happens before the child process.
    Any child timeout, nonzero exit, or malformed output is marked ``unknown``
    and intentionally remains charged; this runner never retries automatically.
    """
    if mode not in {"sandbox", "production"}:
        raise ValueError("mode must be sandbox or production")
    if days < 1 or timeout_seconds < 1:
        raise ValueError("days and timeout_seconds must be positive")

    now = _utc_now()
    local_run_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:10]}"
    run_dir = out_dir / "runs" / local_run_id
    artifacts = _artifact_paths(run_dir=run_dir, out_dir=out_dir)
    accepted, rejected = preflight_candidates(candidates, days=days)
    manifest_hash = _manifest_hash(accepted) if accepted else None
    estimated_usd = _estimate_usd(len(accepted))

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "cohort_id": COHORT_ID,
        "mode": mode,
        "research_scope": "Google AI Overview evidence only",
        "gsc_data_state": "final",
        "site_mutations": [],
        "accepted": accepted,
        "rejected": rejected,
        "manifest_hash": manifest_hash,
        "estimate": {
            "per_term_usd": PER_TERM_ESTIMATE_USD,
            "minimum_production_reservation_usd": MIN_PRODUCTION_RESERVATION_USD,
            "estimated_usd": estimated_usd,
        },
    }
    _safe_write_json(Path(artifacts["manifest_json"]), manifest)

    receipt: dict[str, Any] = {
        "schema_version": 1,
        "persona": "seo_geo",
        "generated_at": now.isoformat(),
        "mode": mode,
        "research_only": True,
        "site_mutations": [],
        "provider": {
            "name": "dataforseo",
            "operation": "geo-mentions",
            "scope": "Google AI Overview evidence only",
            "feature_classifier": (
                "ai_overview only; featured snippets and answer boxes are separate fields"
            ),
            "status": "not_started",
        },
        "cohort": {
            "id": COHORT_ID,
            "accepted": accepted,
            "rejected": rejected,
            "manifest_hash": manifest_hash,
        },
        "budget": {
            "estimated_usd": estimated_usd,
            "charged_usd": 0.0,
            "reservation_status": "not_needed" if mode == "sandbox" else "not_started",
        },
        "artifacts": artifacts,
        "results": [],
    }

    if not accepted:
        receipt["provider"]["status"] = "no_eligible_terms"
        receipt["budget"]["reservation_status"] = "not_needed"
        _finalize_receipt(receipt, run_dir=run_dir, out_dir=out_dir)
        return receipt

    try:
        fleet_domains = _fleet_domains()
    except Exception as exc:  # noqa: BLE001 - fleet scope must be proven before spending
        receipt["provider"]["status"] = "blocked_by_fleet_registry"
        receipt["provider"]["error_class"] = type(exc).__name__
        receipt["budget"]["reservation_status"] = "not_needed"
        _finalize_receipt(receipt, run_dir=run_dir, out_dir=out_dir)
        return receipt

    broker_reservation: dict[str, Any] | None = None
    broker_run_id: str | None = None
    if mode == "production":
        try:
            broker_reservation = _reserve_production(
                idempotency_key=_weekly_idempotency_key(manifest_hash=manifest_hash or "", now=now),
                estimated_usd=estimated_usd,
                manifest_hash=manifest_hash or "",
                units=len(accepted),
            )
            broker_run_id = str(broker_reservation.get("run_id", "")).strip() or None
            if not broker_run_id:
                raise PaidResearchError("budget_broker_reservation_missing_run_id")
            receipt["budget"].update({
                "reservation_status": "reserved",
                "broker_run_id": broker_run_id,
                "broker": broker_reservation,
            })
        except Exception as exc:  # noqa: BLE001 - a provider call must not follow
            receipt["provider"]["status"] = "blocked_by_budget_broker"
            receipt["budget"].update({
                "reservation_status": "blocked",
                "error_class": type(exc).__name__,
            })
            _finalize_receipt(receipt, run_dir=run_dir, out_dir=out_dir)
            return receipt
    else:
        receipt["budget"].update({
            "reservation_status": "sandbox_no_charge",
            "broker": {"status": "not_called"},
        })

    provider_path = Path(artifacts["provider_json"])
    command = _dataforseo_command(
        keywords=[str(item["query"]) for item in accepted],
        output_path=provider_path,
        mode=mode,
        estimated_usd=estimated_usd,
    )
    # Store the immutable (credential-free) command shape for diagnosis.  The
    # DataForSEO credential lives in its own local skill env file and is never
    # passed through argv or copied into an artifact.
    receipt["provider"]["command"] = command
    try:
        child = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        if child.returncode != 0:
            raise PaidResearchError(f"provider_nonzero_exit:{child.returncode}")
        results = _load_provider_output(provider_path)
    except subprocess.TimeoutExpired as exc:
        _record_unknown(
            receipt,
            broker_run_id=broker_run_id,
            reason="provider_timeout",
            artifacts=_broker_artifact_paths(artifacts),
            details={"error_class": type(exc).__name__},
        )
        _finalize_receipt(receipt, run_dir=run_dir, out_dir=out_dir)
        return receipt
    except Exception as exc:  # noqa: BLE001 - keep reservation charged if uncertain
        _record_unknown(
            receipt,
            broker_run_id=broker_run_id,
            reason="provider_receipt_uncertain",
            artifacts=_broker_artifact_paths(artifacts),
            details={"error_class": type(exc).__name__},
        )
        _finalize_receipt(receipt, run_dir=run_dir, out_dir=out_dir)
        return receipt

    receipt["results"] = _annotate_results(results, accepted=accepted, fleet_domains=fleet_domains)
    provider_status = "completed"
    if any("error" in item for item in results):
        provider_status = "completed_with_provider_errors"
    receipt["provider"]["status"] = provider_status
    if mode == "production":
        try:
            settlement = _settle_production(
                broker_run_id=broker_run_id or "",
                estimated_usd=estimated_usd,
                artifact_paths=_broker_artifact_paths(artifacts),
                provider_status=provider_status,
                details={"result_count": len(results), "manifest_hash": manifest_hash},
            )
            receipt["budget"].update({
                "reservation_status": "settled",
                "charged_usd": estimated_usd,
                "broker": settlement,
            })
        except Exception as exc:  # provider did run: conservative unknown must remain charged
            _record_unknown(
                receipt,
                broker_run_id=broker_run_id,
                reason="broker_settlement_uncertain",
                artifacts=_broker_artifact_paths(artifacts),
                details={"error_class": type(exc).__name__},
            )
    _finalize_receipt(receipt, run_dir=run_dir, out_dir=out_dir)
    return receipt


def _record_unknown(
    receipt: dict[str, Any],
    *,
    broker_run_id: str | None,
    reason: str,
    artifacts: Sequence[str],
    details: Mapping[str, Any],
) -> None:
    receipt["provider"]["status"] = "unknown"
    receipt["provider"]["unknown_reason"] = reason
    receipt["budget"]["reservation_status"] = "unknown"
    if broker_run_id is None:
        receipt["budget"]["charged_usd"] = 0.0
        return
    # An unknown reservation is never released automatically.  If the broker
    # itself cannot record the state we keep that fact in this local receipt.
    try:
        unknown = _mark_unknown(
            broker_run_id=broker_run_id,
            reason=reason,
            artifact_paths=artifacts,
            details=details,
        )
        receipt["budget"].update({
            "charged_usd": receipt["budget"]["estimated_usd"],
            "broker": unknown,
        })
    except Exception as exc:  # noqa: BLE001
        receipt["budget"].update({
            "charged_usd": receipt["budget"]["estimated_usd"],
            "broker_mark_unknown_error_class": type(exc).__name__,
        })


def _finalize_receipt(receipt: dict[str, Any], *, run_dir: Path, out_dir: Path) -> None:
    """Write immutable per-run evidence and the latest local read surface."""
    receipt_path = run_dir / "receipt.json"
    markdown_path = run_dir / "receipt.md"
    _safe_write_json(receipt_path, receipt)
    _safe_write_text(markdown_path, _render_markdown(receipt))
    _safe_write_json(out_dir / "latest.json", receipt)
    _safe_write_text(out_dir / "latest.md", _render_markdown(receipt))


def _exit_code(receipt: Mapping[str, Any]) -> int:
    status = str(receipt.get("provider", {}).get("status", ""))
    if status in {"completed", "completed_with_provider_errors", "no_eligible_terms"}:
        return 0
    if status == "blocked_by_budget_broker":
        return 2
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run bounded brokered GEO research without site mutations."
    )
    parser.add_argument("--mode", choices=("sandbox", "production"), required=True)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--days", type=int, default=28)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args()
    receipt = run(
        mode=args.mode,
        out_dir=args.out_dir,
        days=args.days,
        timeout_seconds=args.timeout_seconds,
    )
    print(f"PAID_RESEARCH_JSON={args.out_dir / 'latest.json'}")
    print(f"PAID_RESEARCH_MD={args.out_dir / 'latest.md'}")
    print("SITE_MUTATIONS=0")
    print(f"PROVIDER_STATUS={receipt['provider']['status']}")
    return _exit_code(receipt)


if __name__ == "__main__":
    raise SystemExit(main())
