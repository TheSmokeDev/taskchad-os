"""Tests for the no-provider SEO/GEO review renderer."""

from __future__ import annotations

import json
import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import seo_geo_control_review as review  # noqa: E402


def test_weekly_review_reads_saved_receipts_without_provider_calls(tmp_path):
    pulse_dir = tmp_path / "pulse"
    registry_dir = tmp_path / "registry"
    paid_research_dir = tmp_path / "paid-research"
    snapshot = tmp_path / "snapshot.json"
    pulse_dir.mkdir()
    registry_dir.mkdir()
    paid_research_dir.mkdir()
    snapshot.write_text(json.dumps({
        "ranges": {"primary": {"start": "2026-07-12", "end": "2026-08-08", "data_state": "final"}},
        "fleet_window_comparisons": {"7d": {"current": {"impressions": 10}}},
        "brands": [{"brand_id": "YourBusiness", "status": "ok", "sitemaps": [{"errors": 0, "warnings": 4}]}],
        "recommendations": [{"brand_id": "YourBusiness", "domain": "your-business.example.com", "score": 75, "top_nonbrand_query": "sr22", "reasons": ["demand"]}],
    }), encoding="utf-8")
    ga4_receipt = tmp_path / "ga4.json"
    ga4_receipt.write_text("{}", encoding="utf-8")
    (pulse_dir / "latest.json").write_text(json.dumps({
        "sources": {
            "gsc": {"stdout": f"SNAPSHOT_JSON={snapshot}\n"},
            "ga4": {
                "status": "ok",
                "receipt_json": str(ga4_receipt),
                "summary": {"expected_properties": 27, "properties_ok": 27},
                "fleet_window_comparisons": {"7d": {"current": {"organic_sessions": 9}}},
            },
            "ai_visibility": {
                "status": "ok",
                "metrics": {"prompt_count": 4, "ai_overview_present": 2},
            },
        },
    }), encoding="utf-8")
    (registry_dir / "latest.json").write_text(json.dumps({"summary": {"brand_count": 27}}), encoding="utf-8")
    (paid_research_dir / "latest.json").write_text(json.dumps({
        "generated_at": "2026-08-12T00:00:00+00:00",
        "mode": "production",
        "provider": {
            "name": "dataforseo",
            "operation": "geo-mentions",
            "scope": "Google AI Overview only",
            "status": "settled",
        },
        "cohort": {"id": "five-brand-geo-v1", "accepted": [1], "rejected": [2]},
        "budget": {"monthly_cap_usd": 25, "remaining_usd": 24.99},
    }), encoding="utf-8")

    result = review.build_review(
        mode="weekly",
        pulse_dir=pulse_dir,
        registry_dir=registry_dir,
        paid_research_dir=paid_research_dir,
        max_candidates=5,
    )

    assert result["read_only"] is True
    assert result["spend"] == {"firecrawl": 0, "openseo": 0, "dataforseo": 0, "model": 0}
    assert result["gsc"]["queue"][0]["change_state"] == "approval_required"
    assert result["gsc"]["alerts"] == [{"severity": "warning", "brand_id": "YourBusiness", "type": "sitemap_warnings", "detail": 4}]
    assert result["paid_research"]["receipt"]["provider"]["status"] == "settled"
    assert result["paid_research"]["receipt"]["cohort"]["accepted_count"] == 1
    assert result["paid_research"]["receipt"]["cohort"]["candidate_count"] == 2
    assert result["gsc"]["window_comparisons"]["7d"]["current"]["impressions"] == 10
    assert result["ga4"]["summary"]["properties_ok"] == 27
    assert result["ai_visibility"]["metrics"]["ai_overview_present"] == 2
    assert "approved budget broker" in result["approval_gates"][2]
