"""Tests for the read-only, registry-scoped GA4 fleet collector."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import seo_geo_ga4_fleet as ga4  # noqa: E402


def _registry(path: Path, *, duplicate_property: bool = False) -> Path:
    brands = []
    for index in range(27):
        property_id = 1000 if duplicate_property else 1000 + index
        brands.append(
            {
                "id": f"brand-{index}",
                "name": f"Brand {index}",
                "domain": f"brand-{index}.example",
                "measurement": {
                    "ga4": {"property_id_declared": f"properties/{property_id}"}
                },
            }
        )
    path.write_text(json.dumps({"brands": brands}), encoding="utf-8")
    return path


class _Execute:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _Properties:
    def __init__(self, payload, calls):
        self.payload = payload
        self.calls = calls

    def batchRunReports(self, *, property, body):  # noqa: N802, A002
        self.calls.append((property, body))
        return _Execute(self.payload)


class _Service:
    def __init__(self, payload):
        self.calls = []
        self._properties = _Properties(payload, self.calls)

    def properties(self):
        return self._properties


def _payload():
    return {
        "reports": [
            {
                "rows": [
                    {
                        "dimensionValues": [
                            {"value": "20260812"},
                            {"value": "Organic Search"},
                        ],
                        "metricValues": [{"value": "4"}, {"value": "8"}],
                    },
                    {
                        "dimensionValues": [
                            {"value": "20260812"},
                            {"value": "Direct"},
                        ],
                        "metricValues": [{"value": "1"}, {"value": "2"}],
                    },
                    {
                        "dimensionValues": [
                            {"value": "20260809"},
                            {"value": "Organic Search"},
                        ],
                        "metricValues": [{"value": "2"}, {"value": "3"}],
                    },
                ]
            },
            {
                "rows": [
                    {
                        "dimensionValues": [
                            {"value": "20260812"},
                            {"value": "quote_started"},
                        ],
                        "metricValues": [{"value": "3"}],
                    },
                    {
                        "dimensionValues": [
                            {"value": "20260812"},
                            {"value": "lead_submitted"},
                        ],
                        "metricValues": [{"value": "1"}],
                    },
                ]
            },
        ]
    }


def test_collect_reads_exactly_one_declared_property_per_brand(tmp_path):
    service = _Service(_payload())
    receipt = ga4.collect(
        registry_path=_registry(tmp_path / "registry.json"),
        service=service,
        end_date=date(2026, 8, 12),
    )

    assert receipt["status"] == "ok"
    assert receipt["summary"] == {
        "expected_properties": 27,
        "properties_ok": 27,
        "properties_unavailable": 0,
    }
    assert len(service.calls) == 27
    assert {call[0] for call in service.calls} == {
        f"properties/{1000 + index}" for index in range(27)
    }
    fleet = receipt["fleet_window_comparisons"]["3d"]
    assert fleet["current"]["sessions"] == 135
    assert fleet["current"]["organic_sessions"] == 108
    assert fleet["current"]["funnel_events"]["quote_start"] == 81
    assert fleet["current"]["funnel_events"]["quote_or_lead_submit"] == 27
    assert fleet["previous"]["organic_sessions"] == 54


def test_registry_rejects_duplicate_property_mapping(tmp_path):
    with pytest.raises(ValueError, match="27 unique GA4 properties"):
        ga4.load_fleet(_registry(tmp_path / "registry.json", duplicate_property=True))
