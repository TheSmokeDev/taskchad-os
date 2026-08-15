"""Build a zero-spend evidence registry for the YourBusiness SEO/GEO fleet.

This generator reads local repository configuration only.  It deliberately
separates *declared source configuration* from proof that analytics tags,
conversion events, lead delivery, or public visibility work in production.
No provider APIs, crawlers, browsers, forms, sitemap submissions, or deploys
are used by this file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path("~/YourBusiness")
PROFILE_ROOT = Path.home() / ".homie" / "profiles" / "seo_geo"
DEFAULT_OUT_DIR = PROFILE_ROOT / "data" / "fleet-measurement"
EXPECTED_PUBLIC_BRAND_COUNT = 27
WORKSPACE_FILE = "pnpm-workspace.yaml"
YourBusiness_CONFIG = Path("packages/insurance-kit/src/config/brand.ts")


class RegistryError(RuntimeError):
    """Raised when the local fleet source cannot support an honest registry."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _file_metadata(path: Path, repo_root: Path) -> dict[str, str]:
    try:
        relative = path.relative_to(repo_root).as_posix()
    except ValueError:
        relative = str(path)
    content = path.read_bytes()
    return {
        "path": relative,
        "modified_at": _iso(datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _workspace_public_apps(workspace_path: Path) -> list[str]:
    """Return configured public app paths without executing or parsing YAML broadly."""

    text = workspace_path.read_text(encoding="utf-8")
    app_paths = re.findall(
        r"^\s*-\s*['\"]?(apps/[A-Za-z0-9._-]+)['\"]?\s*(?:#.*)?$",
        text,
        flags=re.MULTILINE,
    )
    if not app_paths:
        raise RegistryError(f"No app paths found in {workspace_path}")

    duplicates = sorted({path for path in app_paths if app_paths.count(path) > 1})
    if duplicates:
        raise RegistryError(f"Duplicate app paths in workspace: {', '.join(duplicates)}")

    public_apps = [path for path in app_paths if path != "apps/admin"]
    if "apps/web" not in public_apps:
        raise RegistryError("Workspace does not declare apps/web as a public brand")
    if len(public_apps) != EXPECTED_PUBLIC_BRAND_COUNT:
        raise RegistryError(
            "Expected exactly "
            f"{EXPECTED_PUBLIC_BRAND_COUNT} public brands (apps/web plus 26 satellites), "
            f"found {len(public_apps)}"
        )
    return public_apps


def _object_body(source: str, constant_name: str) -> str:
    """Extract a TypeScript object literal body without importing or executing it."""

    declaration = re.search(
        rf"export\s+const\s+{re.escape(constant_name)}(?:\s*:\s*[^=]+)?\s*=\s*\{{",
        source,
    )
    if declaration is None:
        raise RegistryError(f"Could not find exported object {constant_name}")

    opening_index = source.find("{", declaration.start())
    depth = 0
    quote: str | None = None
    escaped = False
    index = opening_index
    while index < len(source):
        char = source[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in {"'", '"', "`"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[opening_index + 1 : index]
        index += 1
    raise RegistryError(f"Unclosed object literal for {constant_name}")


def _string_field(body: str, field: str) -> str | None:
    match = re.search(
        rf"(?:^|\n)\s*{re.escape(field)}\s*:\s*(['\"])(?P<value>.*?)\1\s*,?",
        body,
    )
    return match.group("value") if match else None


def _boolean_field(body: str, field: str) -> bool | None:
    match = re.search(rf"(?:^|\n)\s*{re.escape(field)}\s*:\s*(true|false)\s*,?", body)
    if match is None:
        return None
    return match.group(1) == "true"


def _string_array_field(body: str, field: str) -> list[str]:
    match = re.search(
        rf"(?:^|\n)\s*{re.escape(field)}\s*:\s*\[(?P<values>[^\]]*)\]\s*,?",
        body,
        flags=re.DOTALL,
    )
    if match is None:
        return []
    return re.findall(r"['\"]([^'\"]+)['\"]", match.group("values"))


def _parse_brand_config(
    *,
    repo_root: Path,
    app_path: str,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Read only literal BrandConfig values needed for the measurement registry."""

    config_path = repo_root / app_path / "brand.config.ts"
    if not config_path.is_file():
        raise RegistryError(f"Brand config is missing: {config_path}")

    config_source = config_path.read_text(encoding="utf-8")
    source_files = [_file_metadata(config_path, repo_root)]
    if app_path == "apps/web":
        defaults_path = repo_root / YourBusiness_CONFIG
        if not defaults_path.is_file():
            raise RegistryError(f"YourBusiness default BrandConfig is missing: {defaults_path}")
        body = _object_body(defaults_path.read_text(encoding="utf-8"), "YourBusiness_BRAND")
        source_files.append(_file_metadata(defaults_path, repo_root))
    else:
        body = _object_body(config_source, "brandConfig")

    required = {
        "id": _string_field(body, "id"),
        "name": _string_field(body, "name"),
        "domain": _string_field(body, "domain"),
        "default_locale": _string_field(body, "defaultLocale"),
        "tier": _string_field(body, "tier"),
        "niche": _string_field(body, "niche"),
        "seo_ready": _boolean_field(body, "seoReady"),
    }
    missing = [key for key, value in required.items() if value is None]
    if missing:
        raise RegistryError(f"Brand config {config_path} is missing literal fields: {', '.join(missing)}")

    return (
        {
            **required,
            "supported_locales": _string_array_field(body, "supportedLocales"),
            "insurance_types": _string_array_field(body, "insuranceTypes"),
            "config_source": f"{app_path}/brand.config.ts",
        },
        source_files,
    )


def _domain_property(domain: str) -> str:
    return f"sc-domain:{domain.removeprefix('www.')}"


def _load_ga4_inventory(repo_root: Path) -> dict[str, Any]:
    """Read the declared fleet GA4 inventory from the current production ref.

    The inventory is useful mapping evidence, but it deliberately never counts
    as a live tag, event, or conversion receipt.  It is read through Git rather
    than checking out or modifying the dirty working tree.
    """
    ref_path = "origin/main:config/ga4-fleet-live.json"
    try:
        resolved = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "origin/main"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        result = subprocess.run(
            ["git", "-C", str(repo_root), "show", ref_path],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode != 0:
            return {"status": "UNAVAILABLE", "reference": ref_path, "by_brand": {}}
        payload = json.loads(result.stdout)
        entries = payload.get("brands", []) if isinstance(payload, dict) else []
        by_brand = {
            str(entry.get("brand_id")): entry
            for entry in entries
            if isinstance(entry, dict) and str(entry.get("brand_id", "")).strip()
        }
        return {
            "status": "DECLARED_INVENTORY",
            "reference": ref_path,
            "commit": resolved.stdout.strip() if resolved.returncode == 0 else None,
            "by_brand": by_brand,
        }
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {"status": "UNAVAILABLE", "reference": ref_path, "by_brand": {}}


def _load_gsc_snapshot(snapshot_path: Path | None) -> dict[str, Any]:
    """Turn one saved finalized fleet snapshot into per-brand receipt evidence."""
    if snapshot_path is None or not snapshot_path.is_file():
        return {"status": "UNAVAILABLE", "path": None, "by_brand": {}}
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"status": "UNAVAILABLE", "path": str(snapshot_path), "by_brand": {}}
    rows = payload.get("brands", []) if isinstance(payload, dict) else []
    by_brand = {
        str(row.get("brand_id")): row
        for row in rows
        if isinstance(row, dict) and str(row.get("brand_id", "")).strip()
    }
    return {
        "status": "RECEIPT_AVAILABLE",
        "path": str(snapshot_path),
        "generated_at": payload.get("generated_at") if isinstance(payload, dict) else None,
        "finalized_window": payload.get("ranges", {}).get("primary") if isinstance(payload, dict) else None,
        "by_brand": by_brand,
    }


def _brand_record(
    *,
    repo_root: Path,
    app_path: str,
    ga4_inventory: dict[str, Any],
    gsc_snapshot: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    config, source_files = _parse_brand_config(repo_root=repo_root, app_path=app_path)
    route_path = repo_root / app_path / "app" / "api" / "submit-quote" / "route.ts"
    route_relative = route_path.relative_to(repo_root).as_posix()
    route_present = route_path.is_file()
    if route_present:
        source_files.append(_file_metadata(route_path, repo_root))

    quote_source_status = "SOURCE_PRESENT_UNVERIFIED" if route_present else "SOURCE_NOT_FOUND_UNVERIFIED"
    source_reference = route_relative if route_present else None
    domain = str(config["domain"])
    brand_id = str(config["id"])
    ga4 = ga4_inventory.get("by_brand", {}).get(brand_id, {})
    gsc = gsc_snapshot.get("by_brand", {}).get(brand_id, {})
    gsc_receipt_ok = str(gsc.get("status", "")).lower() == "ok"
    return (
        {
            "id": brand_id,
            "name": config["name"],
            "app_path": app_path,
            "domain": domain,
            "homepage_from_declared_domain": f"https://{domain}",
            "production_canonical_proof": "UNVERIFIED",
            "configuration": {
                "source_state": "DECLARED_CONFIG_ONLY",
                "brand_config": config["config_source"],
                "seo_ready_declared": config["seo_ready"],
                "tier_declared": config["tier"],
                "niche_declared": config["niche"],
                "default_locale_declared": config["default_locale"],
                "supported_locales_declared": config["supported_locales"],
                "insurance_types_declared": config["insurance_types"],
            },
            "measurement": {
                "gsc": {
                    "expected_property": _domain_property(domain),
                    "mapping_status": "RECEIPT_VERIFIED" if gsc_receipt_ok else "EXPECTED_NOT_VERIFIED",
                    "access_or_fresh_data_receipt": (
                        {
                            "state": "RECEIPT_VERIFIED",
                            "snapshot_status": gsc.get("status"),
                            "snapshot_generated_at": gsc_snapshot.get("generated_at"),
                            "finalized_window": gsc_snapshot.get("finalized_window"),
                        }
                        if gsc_receipt_ok
                        else "UNVERIFIED"
                    ),
                    "note": (
                        "A saved finalized GSC receipt confirmed property access/data; it is not URL-level index or ranking proof."
                        if gsc_receipt_ok
                        else "Expected domain-property mapping only; no matching saved GSC receipt was supplied."
                    ),
                },
                "ga4": {
                    "account_declared": ga4.get("account"),
                    "property_id_declared": ga4.get("property"),
                    "stream_declared": ga4.get("stream"),
                    "measurement_id_declared": ga4.get("measurement_id"),
                    "vercel_project_declared": ga4.get("vercel_project"),
                    "declaration_status": (
                        "DECLARED_FROM_ORIGIN_MAIN_INVENTORY" if ga4 else "NOT_DECLARED_IN_INVENTORY"
                    ),
                    "deployed_tag_proof": "UNVERIFIED",
                    "event_receipt_proof": "UNVERIFIED",
                    "note": "A configured or declared property is not proof that a tag is deployed or an event was received.",
                },
                "quote": {
                    "source_status": quote_source_status,
                    "source_reference": source_reference,
                    "live_quote_start_receipt": "UNVERIFIED",
                    "live_quote_submission_receipt": "UNVERIFIED",
                    "note": "Source presence is not a live form test or a customer quote receipt.",
                },
                "lead": {
                    "source_status": quote_source_status,
                    "source_reference": source_reference,
                    "terminal_lead_receipt": "UNVERIFIED",
                    "contact_sla_receipt": "UNVERIFIED",
                    "note": "Source presence is not proof that a lead reached the admin/CRM or was contacted.",
                },
                "ai_visibility": {
                    "instrumentation_status": "UNINSTRUMENTED",
                    "prompt_cohort_receipt": "UNVERIFIED",
                    "note": "No paid AI-visibility provider is called by this registry.",
                },
            },
        },
        source_files,
    )


def _unique_source_files(source_files: list[dict[str, str]]) -> list[dict[str, str]]:
    by_path = {item["path"]: item for item in source_files}
    return [by_path[path] for path in sorted(by_path)]


def build_registry(
    repo_root: Path = REPO_ROOT,
    *,
    observed_at: datetime | None = None,
    gsc_snapshot_path: Path | None = None,
) -> dict[str, Any]:
    """Build an in-memory registry from local configuration only."""

    repo_root = repo_root.resolve()
    workspace_path = repo_root / WORKSPACE_FILE
    if not workspace_path.is_file():
        raise RegistryError(f"Workspace file is missing: {workspace_path}")
    apps = _workspace_public_apps(workspace_path)
    generated_at = observed_at or _utc_now()
    ga4_inventory = _load_ga4_inventory(repo_root)
    gsc_snapshot = _load_gsc_snapshot(gsc_snapshot_path)

    all_source_files = [_file_metadata(workspace_path, repo_root)]
    brands: list[dict[str, Any]] = []
    for app_path in apps:
        record, source_files = _brand_record(
            repo_root=repo_root,
            app_path=app_path,
            ga4_inventory=ga4_inventory,
            gsc_snapshot=gsc_snapshot,
        )
        brands.append(record)
        all_source_files.extend(source_files)

    ids = [str(brand["id"]) for brand in brands]
    domains = [str(brand["domain"]) for brand in brands]
    for label, values in (("brand ids", ids), ("domains", domains)):
        duplicates = sorted({value for value in values if values.count(value) > 1})
        if duplicates:
            raise RegistryError(f"Duplicate {label}: {', '.join(duplicates)}")

    quote_sources = sum(
        brand["measurement"]["quote"]["source_status"] == "SOURCE_PRESENT_UNVERIFIED" for brand in brands
    )
    gsc_receipts = sum(
        brand["measurement"]["gsc"]["mapping_status"] == "RECEIPT_VERIFIED" for brand in brands
    )
    ga4_declarations = sum(
        brand["measurement"]["ga4"]["declaration_status"] == "DECLARED_FROM_ORIGIN_MAIN_INVENTORY" for brand in brands
    )
    seo_ready = sum(bool(brand["configuration"]["seo_ready_declared"]) for brand in brands)
    generated_iso = _iso(generated_at)
    return {
        "schema_version": 1,
        "registry": "YourBusiness-seo-geo-measurement",
        "generated_at": generated_iso,
        "observed_at": generated_iso,
        "read_only": True,
        "mutations": [],
        "evidence_boundary": (
            "Configuration and source presence are not live analytics, quote, lead, ranking, indexing, "
            "or contact proof."
        ),
        "source": {
            "repo_root": str(repo_root),
            "workspace": _file_metadata(workspace_path, repo_root),
            "source_files": _unique_source_files(all_source_files),
            "observed_at": generated_iso,
            "ga4_inventory": {
                key: value for key, value in ga4_inventory.items() if key != "by_brand"
            },
            "gsc_snapshot": {
                key: value for key, value in gsc_snapshot.items() if key != "by_brand"
            },
        },
        "summary": {
            "expected_public_brands": EXPECTED_PUBLIC_BRAND_COUNT,
            "public_brand_count": len(brands),
            "satellite_brand_count": len(brands) - 1,
            "seo_ready_declared": seo_ready,
            "seo_not_ready_declared": len(brands) - seo_ready,
            "gsc_expected_mappings": len(brands),
            "gsc_verified_access_or_fresh_data_receipts": gsc_receipts,
            "ga4_properties_declared_in_origin_main_inventory": ga4_declarations,
            "ga4_deployed_tag_proofs": 0,
            "ga4_event_receipt_proofs": 0,
            "quote_source_present_unverified": quote_sources,
            "quote_live_submission_receipts": 0,
            "lead_source_present_unverified": quote_sources,
            "terminal_lead_receipts": 0,
            "contact_sla_receipts": 0,
            "ai_visibility_instrumented": 0,
        },
        "brands": brands,
    }


def _render_markdown(registry: dict[str, Any]) -> str:
    summary = registry["summary"]
    lines = [
        "# YourBusiness SEO/GEO Measurement Registry",
        "",
        f"- Generated / observed: {registry['generated_at']}",
        "- Mode: read-only local configuration scan. No provider API, crawl, form submission, sitemap submission, deploy, social post, or browser action occurred.",
        "- Evidence boundary: configuration and source presence are not production analytics, quote, lead, ranking, indexing, or contact proof.",
        "",
        "## Fleet summary",
        "",
        "| Signal | Count | Meaning |",
        "| --- | ---: | --- |",
        f"| Public brands | {summary['public_brand_count']} | Exact roster invariant: YourBusiness plus 26 satellite apps |",
        f"| SEO-ready declared | {summary['seo_ready_declared']} | BrandConfig declaration only |",
        f"| GSC expected mappings | {summary['gsc_expected_mappings']} | Expected `sc-domain:` mapping; inspect receipt status separately |",
        f"| GSC finalized receipts | {summary['gsc_verified_access_or_fresh_data_receipts']} | Saved snapshot access/data proof; not URL-level index proof |",
        f"| GA4 declared properties | {summary['ga4_properties_declared_in_origin_main_inventory']} | Origin/main inventory only; not a deployed-tag proof |",
        f"| GA4 deployed-tag proofs | {summary['ga4_deployed_tag_proofs']} | No per-brand deployed-tag receipt in this registry |",
        f"| GA4 event receipts | {summary['ga4_event_receipt_proofs']} | No per-brand event receipt in this registry |",
        f"| Quote sources present | {summary['quote_source_present_unverified']} | Source route present; no form submission proof |",
        f"| Terminal lead receipts | {summary['terminal_lead_receipts']} | No admin/CRM receipt asserted |",
        f"| AI visibility instrumented | {summary['ai_visibility_instrumented']} | No paid AI-visibility check was run |",
        "",
        "## Brand registry",
        "",
        "| Brand | Domain | SEO-ready | GSC mapping | GA4 | Quote / lead evidence | AI visibility |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for brand in registry["brands"]:
        measurement = brand["measurement"]
        lines.append(
            "| {name} | `{domain}` | `{seo_ready}` | `{gsc}` | `{ga4}` | `{quote}` / `{lead}` | `{ai}` |".format(
                name=str(brand["name"]).replace("|", "\\|"),
                domain=brand["domain"],
                seo_ready=brand["configuration"]["seo_ready_declared"],
                gsc=measurement["gsc"]["mapping_status"],
                ga4=measurement["ga4"]["deployed_tag_proof"],
                quote=measurement["quote"]["source_status"],
                lead=measurement["lead"]["terminal_lead_receipt"],
                ai=measurement["ai_visibility"]["instrumentation_status"],
            )
        )
    lines.extend(
        [
            "",
            "## Required proof before counting a lead",
            "",
            "1. Verify the expected Search Console property has access and a fresh receipt.",
            "2. Map each brand to a GA4 property and verify its deployed tag and conversion-event receipt.",
            "3. Verify quote start and submission separately, then capture the terminal admin/CRM lead receipt and contact SLA receipt.",
            "4. Add a bounded AI-visibility prompt cohort only after its provider and spend cap are approved.",
            "",
        ]
    )
    return "\n".join(lines)


def write_registry(registry: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    """Write deterministic JSON and Markdown receipts to the persona-local data area."""

    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "registry.json"
    markdown_path = out_dir / "registry.md"
    json_path.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    markdown_path.write_text(_render_markdown(registry), encoding="utf-8")
    return json_path, markdown_path


def run(*, repo_root: Path = REPO_ROOT, out_dir: Path = DEFAULT_OUT_DIR) -> tuple[Path, Path]:
    registry = build_registry(repo_root)
    return write_registry(registry, out_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the zero-spend YourBusiness fleet measurement registry.")
    parser.add_argument("--repo", type=Path, default=REPO_ROOT, help="YourBusiness repository root")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Persona-local receipt directory")
    args = parser.parse_args()
    try:
        json_path, markdown_path = run(repo_root=args.repo, out_dir=args.out_dir)
    except RegistryError as exc:
        print(f"REGISTRY_ERROR={exc}", file=__import__("sys").stderr)
        return 1
    print(f"REGISTRY_JSON={json_path}")
    print(f"REGISTRY_MD={markdown_path}")
    print("MUTATIONS=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
