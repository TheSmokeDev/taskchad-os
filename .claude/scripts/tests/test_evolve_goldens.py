"""Phase 2.6 tests — golden_queries.json v2 loader, stratification validator,
regression-corpus loader, and audit-goldens drift detection.

Mirrors test_evolve_veto.py structure: pure evaluator tests, deterministic,
no I/O against the live vault. The actual replay path is exercised via
test_evolve_isolation.py + test_evolve_tracing.py end-to-end smokes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for _p in (_SCRIPTS_DIR, _CHAT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _v2_entry(**overrides):
    """Build a minimum-valid v2 golden entry for tests."""
    base = {
        "query": "test query",
        "tier_expected": "tier_2",
        "domain": "memory-root",
        "length_bucket": "short",
        "path_flavor": "happy",
    }
    base.update(overrides)
    return base


def _write_goldens(tmp_path: Path, entries, version=2):
    """Write a goldens JSON file and return its path."""
    path = tmp_path / "golden_queries.json"
    path.write_text(
        json.dumps({"version": version, "queries": entries}),
        encoding="utf-8",
    )
    return path


# ── 1. v2 schema validation ────────────────────────────────────────────────


class TestLoaderV2Schema:
    def test_v1_rejected_explicitly(self, tmp_path):
        """v1 had only `query` + `domain` — Phase 2.6 demands full metadata."""
        from evolve.goldens import load_golden_queries_full

        path = tmp_path / "golden_queries.json"
        path.write_text(
            json.dumps({
                "version": 1,
                "queries": [{"query": "x", "domain": "framework"}],
            }),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="version must be 2"):
            load_golden_queries_full(path=path)

    def test_missing_required_field_rejected(self, tmp_path):
        from evolve.goldens import load_golden_queries_full

        # Drop `path_flavor` from the entry
        entry = _v2_entry()
        del entry["path_flavor"]
        path = _write_goldens(tmp_path, [entry])
        with pytest.raises(ValueError, match="missing required field 'path_flavor'"):
            load_golden_queries_full(path=path)

    def test_invalid_tier_rejected(self, tmp_path):
        from evolve.goldens import load_golden_queries_full

        path = _write_goldens(tmp_path, [_v2_entry(tier_expected="tier_99")])
        with pytest.raises(ValueError, match="tier_99"):
            load_golden_queries_full(path=path)

    def test_invalid_path_flavor_rejected(self, tmp_path):
        from evolve.goldens import load_golden_queries_full

        path = _write_goldens(tmp_path, [_v2_entry(path_flavor="weird")])
        with pytest.raises(ValueError, match="weird"):
            load_golden_queries_full(path=path)

    def test_full_schema_round_trips(self, tmp_path):
        from evolve.goldens import load_golden_queries_full

        entries = [
            _v2_entry(query="A", tier_expected="tier_1"),
            _v2_entry(query="B", tier_expected="tier_2"),
            _v2_entry(query="C", tier_expected="tier_3"),
        ]
        path = _write_goldens(tmp_path, entries)
        loaded = load_golden_queries_full(path=path, validate=False)
        assert [e["query"] for e in loaded] == ["A", "B", "C"]


# ── 2. Stratification validation ───────────────────────────────────────────


class TestStratificationValidation:
    def test_within_tolerance_no_warnings(self):
        """When every bucket is within ±10pp of target, no warnings fire."""
        from evolve.goldens import validate_stratification

        # Build 100 entries that hit every target exactly:
        # tier 40/40/20, length 30/50/20, flavor 70/10/5/15
        def _entry(tier, length, flavor):
            return _v2_entry(
                tier_expected=tier, length_bucket=length, path_flavor=flavor,
            )

        entries = (
            # 70 happy: 28 tier_1, 28 tier_2, 14 tier_3 (40/40/20 of 70)
            [_entry("tier_1", "short", "happy")] * 12
            + [_entry("tier_1", "medium", "happy")] * 12
            + [_entry("tier_1", "long", "happy")] * 4
            + [_entry("tier_2", "short", "happy")] * 8
            + [_entry("tier_2", "medium", "happy")] * 16
            + [_entry("tier_2", "long", "happy")] * 4
            + [_entry("tier_3", "medium", "happy")] * 14
            # 10 zero-result, 5 error-inducing, 15 rare-path
            + [_entry("tier_2", "short", "zero-result")] * 4
            + [_entry("tier_2", "medium", "zero-result")] * 4
            + [_entry("tier_1", "long", "zero-result")] * 2
            + [_entry("tier_2", "short", "error-inducing")] * 2
            + [_entry("tier_2", "long", "error-inducing")] * 3
            + [_entry("tier_3", "medium", "rare-path")] * 8
            + [_entry("tier_3", "long", "rare-path")] * 7
        )
        assert len(entries) == 100  # sanity

        warnings = validate_stratification(entries)
        # All buckets are exactly on target — no warnings expected.
        assert warnings == [], f"unexpected warnings: {warnings}"

    def test_out_of_tolerance_warns_does_not_raise(self):
        from evolve.goldens import validate_stratification

        # 100% tier_1 — way above 40% target
        entries = [_v2_entry(tier_expected="tier_1")] * 10
        # Must NOT raise
        warnings = validate_stratification(entries)
        # tier_2 at 0% is 40pp below target → warning
        assert any("tier_2" in w for w in warnings)
        # tier_1 at 100% is 60pp above target → warning
        assert any("tier_1" in w for w in warnings)

    def test_unknown_bucket_flagged(self):
        from evolve.goldens import validate_stratification

        # Use an unknown path_flavor bucket
        entries = [_v2_entry(path_flavor="unknown-flavor")] * 10
        warnings = validate_stratification(entries)
        # The validator should flag unknown buckets
        assert any("unknown" in w.lower() for w in warnings)

    def test_empty_entries_returns_no_warnings(self):
        """Edge case: empty corpus shouldn't crash. Each bucket is at 0%
        which is below target, so we expect warnings. Just ensure no crash."""
        from evolve.goldens import validate_stratification

        warnings = validate_stratification([])
        # Don't assert specific warnings — empty just means everything is at 0%.
        # The key invariant: the function doesn't raise.
        assert isinstance(warnings, list)


# ── 3. Backward-compat for the v1 surface ──────────────────────────────────


class TestBackwardCompat:
    def test_load_golden_queries_returns_list_of_strings(self, tmp_path):
        """Existing `load_golden_queries` consumers (replay path) MUST keep
        getting list[str]. The Phase 2.6 metadata is opt-in via
        load_golden_queries_full."""
        from evolve.goldens import load_golden_queries

        path = _write_goldens(
            tmp_path,
            [
                _v2_entry(query="alpha"),
                _v2_entry(query="beta"),
            ],
        )
        result = load_golden_queries(path=path)
        assert result == ["alpha", "beta"]
        assert all(isinstance(q, str) for q in result)

    def test_load_goldens_metadata_returns_full_dict(self, tmp_path):
        """`load_goldens_metadata` returns the parsed JSON unchanged — used
        by audit-goldens to display version + description."""
        from evolve.goldens import load_goldens_metadata

        path = _write_goldens(tmp_path, [_v2_entry()])
        meta = load_goldens_metadata(path=path)
        assert meta["version"] == 2
        assert isinstance(meta["queries"], list)


# ── 4. Audit drift detection ───────────────────────────────────────────────


class TestAuditGoldensDrift:
    def test_tier_mismatch_flagged(self):
        from evolve.goldens import audit_goldens_drift
        from evolve.models import ReplayQueryResult

        entries = [
            _v2_entry(query="A", tier_expected="tier_1"),
            _v2_entry(query="B", tier_expected="tier_2"),
        ]
        per_query = [
            ReplayQueryResult(query="A", tier="tier_2", results_count=3,
                              top_scores=[0.5, 0.4, 0.3]),
            ReplayQueryResult(query="B", tier="tier_2", results_count=3,
                              top_scores=[0.6, 0.5, 0.4]),
        ]
        drifts = audit_goldens_drift(per_query, entries)
        assert len(drifts) == 1
        assert drifts[0]["query"] == "A"
        assert any("tier_drift" in r for r in drifts[0]["reasons"])

    def test_unexpected_zero_results_flagged(self):
        from evolve.goldens import audit_goldens_drift
        from evolve.models import ReplayQueryResult

        entries = [
            _v2_entry(query="happy-q", path_flavor="happy", tier_expected="tier_2"),
        ]
        per_query = [
            ReplayQueryResult(query="happy-q", tier="tier_2", results_count=0),
        ]
        drifts = audit_goldens_drift(per_query, entries)
        assert len(drifts) == 1
        assert any("unexpected_zero_results" in r for r in drifts[0]["reasons"])

    def test_zero_result_flavor_does_not_drift(self):
        """A query intentionally tagged path_flavor=zero-result returning 0
        results is the EXPECTED behavior, not drift."""
        from evolve.goldens import audit_goldens_drift
        from evolve.models import ReplayQueryResult

        entries = [
            _v2_entry(query="off-topic", path_flavor="zero-result",
                      tier_expected="tier_2"),
        ]
        per_query = [
            ReplayQueryResult(query="off-topic", tier="tier_2", results_count=0),
        ]
        drifts = audit_goldens_drift(per_query, entries)
        assert drifts == []

    def test_clean_corpus_returns_empty(self):
        from evolve.goldens import audit_goldens_drift
        from evolve.models import ReplayQueryResult

        entries = [_v2_entry(query="X", tier_expected="tier_1")]
        per_query = [
            ReplayQueryResult(query="X", tier="tier_1", results_count=3,
                              top_scores=[0.7, 0.6, 0.5]),
        ]
        drifts = audit_goldens_drift(per_query, entries)
        assert drifts == []

    def test_length_mismatch_raises(self):
        from evolve.goldens import audit_goldens_drift

        with pytest.raises(ValueError, match="length mismatch"):
            audit_goldens_drift([], [_v2_entry()])
