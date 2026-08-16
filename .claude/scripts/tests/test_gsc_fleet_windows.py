"""Deterministic tests for finalized GSC fleet comparison windows."""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path


SCRIPT = Path.home() / ".codex" / "skills" / "gsc-ops" / "scripts" / "fleet_snapshot.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("gsc_fleet_snapshot_windows", SCRIPT)
assert SPEC and SPEC.loader
gsc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gsc)


def _row(day: date, *, clicks: float, impressions: float, position: float) -> dict:
    return {
        "keys": [day.isoformat()],
        "clicks": clicks,
        "impressions": impressions,
        "ctr": clicks / impressions if impressions else 0,
        "position": position,
    }


def test_comparisons_use_equal_non_overlapping_final_windows():
    end = date(2026, 8, 12)
    rows = []
    for offset in range(3):
        rows.append(_row(end - timedelta(days=offset), clicks=2, impressions=10, position=20))
    for offset in range(3, 6):
        rows.append(_row(end - timedelta(days=offset), clicks=1, impressions=20, position=5))

    comparison = gsc.build_window_comparisons(rows, end, windows=(3,))["3d"]

    assert comparison["current_range"] == {"start": "2026-08-10", "end": "2026-08-12"}
    assert comparison["previous_range"] == {"start": "2026-08-07", "end": "2026-08-09"}
    assert comparison["current"] == {
        "clicks": 6.0,
        "impressions": 30.0,
        "ctr": 0.2,
        "position": 20.0,
    }
    assert comparison["previous"] == {
        "clicks": 3.0,
        "impressions": 60.0,
        "ctr": 0.05,
        "position": 5.0,
    }
    assert comparison["delta"]["ctr_percentage_points"] == 15.0


def test_fleet_rollup_weights_position_by_impressions():
    template = gsc.build_window_comparisons([], date(2026, 8, 12), windows=(3,))["3d"]
    first = dict(template)
    first["current"] = {"clicks": 1, "impressions": 10, "ctr": 0.1, "position": 10}
    first["previous"] = {"clicks": 0, "impressions": 10, "ctr": 0, "position": 8}
    second = dict(template)
    second["current"] = {"clicks": 9, "impressions": 90, "ctr": 0.1, "position": 20}
    second["previous"] = {"clicks": 5, "impressions": 90, "ctr": 5 / 90, "position": 18}

    brands = [
        {"analytics": {"window_comparisons": {"3d": first}}},
        {"analytics": {"window_comparisons": {"3d": second}}},
    ]
    rollup = gsc.aggregate_fleet_window_comparisons(brands)["3d"]

    assert rollup["current"]["impressions"] == 100.0
    assert rollup["current"]["clicks"] == 10.0
    assert rollup["current"]["position"] == 19.0
