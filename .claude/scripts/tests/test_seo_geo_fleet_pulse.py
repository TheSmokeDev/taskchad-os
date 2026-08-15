"""Focused receipt-only tests for the SEO/GEO daily fleet pulse."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import seo_geo_fleet_pulse as pulse  # noqa: E402


def test_paid_research_receipt_summary_never_runs_a_provider(tmp_path, monkeypatch):
    (tmp_path / "latest.json").write_text(json.dumps({
        "generated_at": "2026-08-12T00:00:00+00:00",
        "mode": "production",
        "provider": {"name": "dataforseo", "operation": "geo-mentions", "scope": "Google AI Overview only", "status": "settled"},
        "cohort": {"id": "five-brand-geo-v1", "accepted": [1], "rejected": [2]},
        "budget": {"monthly_cap_usd": 25},
    }), encoding="utf-8")
    monkeypatch.setattr(pulse, "PAID_RESEARCH_ROOT", tmp_path)

    result = pulse._paid_research_receipt_state()

    assert result["status"] == "ok"
    assert result["provider"]["operation"] == "geo-mentions"
    assert result["cohort"]["accepted_count"] == 1
    assert result["cohort"]["candidate_count"] == 2


def test_budget_status_is_not_called_before_policy_exists(tmp_path, monkeypatch):
    calls: list[str] = []
    module = types.SimpleNamespace(
        DEFAULT_ROOT=tmp_path,
        budget_status=lambda: calls.append("called") or {"remaining_usd": 25},
    )
    monkeypatch.setitem(sys.modules, "seo_geo_budget_broker", module)

    result = pulse._budget_broker_state()

    assert result["status"] == "not_initialized"
    assert calls == []


def test_budget_status_reads_existing_local_policy_only(tmp_path, monkeypatch):
    (tmp_path / "policy.json").write_text("{}", encoding="utf-8")
    module = types.SimpleNamespace(DEFAULT_ROOT=tmp_path, budget_status=lambda: {"remaining_usd": 24.99})
    monkeypatch.setitem(sys.modules, "seo_geo_budget_broker", module)

    result = pulse._budget_broker_state()

    assert result == {
        "status": "ok",
        "scope": "local broker policy and monthly ledger only; no provider call",
        "root": str(tmp_path),
        "summary": {"remaining_usd": 24.99},
    }
