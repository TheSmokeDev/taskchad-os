"""Focused tests for the zero-spend SEO/GEO measurement registry."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import seo_geo_measurement_registry as registry  # noqa: E402


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _brand_config(*, brand_id: str, domain: str, seo_ready: bool = False) -> str:
    return f"""export const brandConfig = {{
  id: '{brand_id}',
  name: '{brand_id.title()}',
  domain: '{domain}',
  defaultLocale: 'en',
  supportedLocales: ['en'],
  tier: 'niche',
  seoReady: {str(seo_ready).lower()},
  niche: 'general',
  insuranceTypes: ['car', 'sr22'],
}};
"""


def _make_fleet_repo(tmp_path: Path) -> Path:
    public_apps = ["apps/web", *[f"apps/satellite-{index:02d}" for index in range(1, 27)]]
    workspace = "packages:\n  - 'apps/admin'\n" + "".join(f"  - '{app}'\n" for app in public_apps)
    _write(tmp_path / "pnpm-workspace.yaml", workspace)
    _write(
        tmp_path / "apps/web/brand.config.ts",
        "import { YourBusiness_BRAND } from '@YourBusiness/insurance-kit';\nexport const brandConfig = YourBusiness_BRAND;\n",
    )
    _write(
        tmp_path / "packages/insurance-kit/src/config/brand.ts",
        """export const YourBusiness_BRAND = {
  id: 'YourBusiness',
  name: 'YourBusiness',
  domain: 'your-business.example.com',
  defaultLocale: 'en',
  supportedLocales: ['en', 'es'],
  tier: 'flagship',
  seoReady: true,
  niche: 'general',
  insuranceTypes: ['car', 'sr22'],
};
""",
    )
    for index in range(1, 27):
        app = f"apps/satellite-{index:02d}"
        _write(
            tmp_path / app / "brand.config.ts",
            _brand_config(brand_id=f"satellite-{index:02d}", domain=f"satellite-{index:02d}.test", seo_ready=index == 1),
        )
        _write(tmp_path / app / "app/api/submit-quote/route.ts", "export async function POST() { return Response.json({}); }\n")
    _write(tmp_path / "apps/web/app/api/submit-quote/route.ts", "export async function POST() { return Response.json({}); }\n")
    return tmp_path


def test_builds_exact_27_brand_registry_and_marks_only_declared_evidence(tmp_path):
    repo = _make_fleet_repo(tmp_path)
    observed = datetime(2026, 8, 11, 22, 0, tzinfo=UTC)

    result = registry.build_registry(repo, observed_at=observed)

    assert result["summary"]["public_brand_count"] == 27
    assert result["summary"]["satellite_brand_count"] == 26
    assert result["summary"]["quote_source_present_unverified"] == 27
    assert result["summary"]["gsc_verified_access_or_fresh_data_receipts"] == 0
    assert result["summary"]["ga4_deployed_tag_proofs"] == 0
    assert result["summary"]["terminal_lead_receipts"] == 0
    assert result["generated_at"] == "2026-08-11T22:00:00+00:00"

    YourBusiness = result["brands"][0]
    assert YourBusiness["id"] == "YourBusiness"
    assert YourBusiness["measurement"]["gsc"]["expected_property"] == "sc-domain:your-business.example.com"
    assert YourBusiness["measurement"]["gsc"]["mapping_status"] == "EXPECTED_NOT_VERIFIED"
    assert YourBusiness["measurement"]["ga4"]["deployed_tag_proof"] == "UNVERIFIED"
    assert YourBusiness["measurement"]["quote"]["source_status"] == "SOURCE_PRESENT_UNVERIFIED"
    assert YourBusiness["measurement"]["lead"]["terminal_lead_receipt"] == "UNVERIFIED"


def test_writes_json_and_markdown_receipts_without_provider_state(tmp_path):
    repo = _make_fleet_repo(tmp_path / "repo")
    out_dir = tmp_path / "out"

    json_path, markdown_path = registry.run(repo_root=repo, out_dir=out_dir)

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")
    assert payload["read_only"] is True
    assert payload["mutations"] == []
    assert payload["summary"]["public_brand_count"] == 27
    assert "No provider API, crawl, form submission, sitemap submission, deploy" in markdown
    assert "SOURCE_PRESENT_UNVERIFIED" in markdown
    assert "UNVERIFIED" in markdown


def test_attaches_saved_gsc_receipts_without_calling_a_provider(tmp_path, monkeypatch):
    repo = _make_fleet_repo(tmp_path / "repo")
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({
        "generated_at": "2026-08-11T22:00:00+00:00",
        "ranges": {"primary": {"start": "2026-07-12", "end": "2026-08-08", "data_state": "final"}},
        "brands": [{"brand_id": "YourBusiness", "status": "ok"}],
    }), encoding="utf-8")
    monkeypatch.setattr(registry, "_load_ga4_inventory", lambda _: {"status": "UNAVAILABLE", "by_brand": {}})

    result = registry.build_registry(repo, gsc_snapshot_path=snapshot)

    YourBusiness = result["brands"][0]
    assert YourBusiness["measurement"]["gsc"]["mapping_status"] == "RECEIPT_VERIFIED"
    assert result["summary"]["gsc_verified_access_or_fresh_data_receipts"] == 1


def test_fails_closed_when_workspace_is_not_the_exact_public_roster(tmp_path):
    _write(tmp_path / "pnpm-workspace.yaml", "packages:\n  - 'apps/admin'\n  - 'apps/web'\n")

    with pytest.raises(registry.RegistryError, match="Expected exactly 27 public brands"):
        registry.build_registry(tmp_path)
