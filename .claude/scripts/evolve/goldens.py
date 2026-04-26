"""Golden + regression query loaders — Phase 2.2 + 2.6.

Phase 2.2 (`load_golden_queries`, `load_goldens_metadata`) returns minimal
data suitable for the replay flow: query strings only, ordered. Phase 2.6
adds:

- `load_golden_queries_full()` — returns full per-query dicts with
  stratification metadata (`tier_expected`, `domain`, `length_bucket`,
  `path_flavor`). Used by `evolve audit-goldens` to detect drift.
- `validate_stratification()` — warns (does NOT raise) when bucket
  distributions drift outside the PRD's ±10% guidelines. PRD Risk #7:
  targets are guidelines, not hard errors.
- `load_regression_queries()` — loads the regression corpus (separate
  file). Each entry has `query`, `fixed_in`, `expected_top_path`,
  `min_top_score` for hard-veto enforcement in `evolve propose`.

Schema versioning: v2 of golden_queries.json carries full metadata; the
loader fails LOUDLY on missing fields rather than silently filling
defaults — PRD line 350 ("breaking changes bump the version and
invalidate old replays"). v1 (15 minimal queries) is no longer
supported.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

_GOLDENS_PATH = Path(__file__).resolve().parent / "golden_queries.json"
_REGRESSION_PATH = Path(__file__).resolve().parent / "regression_queries.json"

_logger = logging.getLogger(__name__)


# ── Required metadata per golden entry (v2) ────────────────────────────────


GOLDEN_REQUIRED_FIELDS: tuple[str, ...] = (
    "query",
    "tier_expected",
    "domain",
    "length_bucket",
    "path_flavor",
)

VALID_TIERS: tuple[str, ...] = ("tier_1", "tier_2", "tier_3")
VALID_LENGTH_BUCKETS: tuple[str, ...] = ("short", "medium", "long")
VALID_PATH_FLAVORS: tuple[str, ...] = (
    "happy",
    "zero-result",
    "error-inducing",
    "rare-path",
)

# PRD stratification targets (Phase 2.6 lines 281-289). Tolerance is the
# absolute percentage deviation from the target before a warning fires
# (PRD Risk #7: targets are guidelines, not hard errors — warn, don't raise).
STRATIFICATION_TARGETS: dict[str, dict[str, float]] = {
    "tier_expected": {"tier_1": 40.0, "tier_2": 40.0, "tier_3": 20.0},
    "length_bucket": {"short": 30.0, "medium": 50.0, "long": 20.0},
    "path_flavor": {
        "happy": 70.0,
        "zero-result": 10.0,
        "error-inducing": 5.0,
        "rare-path": 15.0,
    },
}
STRATIFICATION_TOLERANCE_PCT: float = 10.0


# ── Required metadata per regression entry ─────────────────────────────────


REGRESSION_REQUIRED_FIELDS: tuple[str, ...] = (
    "query",
    "fixed_in",
    "expected_top_path",
    "min_top_score",
)


# ── Loaders ────────────────────────────────────────────────────────────────


def load_golden_queries(path: Path | str | None = None) -> list[str]:
    """Return the ordered list of golden query strings.

    Backward-compatible Phase 2.2 surface — replay uses positional alignment
    in compare_reports, so preserving ordering here is load-bearing.
    """
    p = Path(path) if path else _GOLDENS_PATH
    data = json.loads(p.read_text(encoding="utf-8"))
    queries = data.get("queries", [])
    return [q["query"] for q in queries if isinstance(q, dict) and q.get("query")]


def load_goldens_metadata(path: Path | str | None = None) -> dict[str, Any]:
    """Return the full JSON — used by CLI to display version + description."""
    p = Path(path) if path else _GOLDENS_PATH
    return json.loads(p.read_text(encoding="utf-8"))


def load_golden_queries_full(
    path: Path | str | None = None,
    *,
    validate: bool = True,
) -> list[dict[str, Any]]:
    """Return the ordered list of golden query entries with full metadata.

    Validates schema (v2 required + required fields per entry) and, when
    ``validate=True`` (default), warns on stratification drift outside the
    PRD's ±10% guidelines. Stratification warnings are logged at WARNING
    level; the function does not raise — PRD Risk #7.

    Raises ``ValueError`` on hard schema violations (missing version 2,
    missing required field, invalid enum value).
    """
    p = Path(path) if path else _GOLDENS_PATH
    data = json.loads(p.read_text(encoding="utf-8"))

    version = data.get("version")
    if version != 2:
        raise ValueError(
            f"golden_queries.json version must be 2, got {version!r}. "
            f"v1 (Phase 2.2 minimal schema) is no longer supported — "
            f"each entry needs full stratification metadata "
            f"({', '.join(GOLDEN_REQUIRED_FIELDS)})"
        )

    raw_queries = data.get("queries", [])
    if not isinstance(raw_queries, list):
        raise ValueError(
            f"golden_queries.json 'queries' must be a list, got "
            f"{type(raw_queries).__name__}"
        )

    entries: list[dict[str, Any]] = []
    for i, raw in enumerate(raw_queries):
        if not isinstance(raw, dict):
            raise ValueError(
                f"queries[{i}] must be an object, got {type(raw).__name__}"
            )
        for field in GOLDEN_REQUIRED_FIELDS:
            if field not in raw:
                raise ValueError(
                    f"queries[{i}] missing required field {field!r}"
                )
        if raw["tier_expected"] not in VALID_TIERS:
            raise ValueError(
                f"queries[{i}].tier_expected={raw['tier_expected']!r} "
                f"must be one of {VALID_TIERS}"
            )
        if raw["length_bucket"] not in VALID_LENGTH_BUCKETS:
            raise ValueError(
                f"queries[{i}].length_bucket={raw['length_bucket']!r} "
                f"must be one of {VALID_LENGTH_BUCKETS}"
            )
        if raw["path_flavor"] not in VALID_PATH_FLAVORS:
            raise ValueError(
                f"queries[{i}].path_flavor={raw['path_flavor']!r} "
                f"must be one of {VALID_PATH_FLAVORS}"
            )
        entries.append(dict(raw))

    if validate:
        validate_stratification(entries)

    return entries


def load_regression_queries(
    path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Load the regression corpus. Each entry exercises a code path that
    a shipped hardening PR fixed; falling below ``min_top_score`` becomes
    a hard veto in ``evolve propose`` (Phase 2.6 integration).

    Raises ``ValueError`` on schema violations — fail-loud is intentional;
    a malformed regression file would silently disable the hard-veto
    enforcement.
    """
    p = Path(path) if path else _REGRESSION_PATH
    if not p.exists():
        raise FileNotFoundError(
            f"regression_queries.json not found at {p}. "
            f"Phase 2.6 expects this file to exist alongside golden_queries.json."
        )
    data = json.loads(p.read_text(encoding="utf-8"))

    raw_queries = data.get("queries", [])
    if not isinstance(raw_queries, list):
        raise ValueError(
            f"regression_queries.json 'queries' must be a list, got "
            f"{type(raw_queries).__name__}"
        )
    if not raw_queries:
        # Empty regression corpus would silently fail-open the hard-veto
        # gate; follow the same posture as evolve/veto.py rejecting empty
        # rulesets (Codex review 2026-04-25 Finding 3).
        raise ValueError(
            "regression_queries.json must contain at least one entry; "
            "an empty corpus would silently disable regression enforcement"
        )

    entries: list[dict[str, Any]] = []
    for i, raw in enumerate(raw_queries):
        if not isinstance(raw, dict):
            raise ValueError(
                f"regression queries[{i}] must be an object, got {type(raw).__name__}"
            )
        for field in REGRESSION_REQUIRED_FIELDS:
            if field not in raw:
                raise ValueError(
                    f"regression queries[{i}] missing required field {field!r}"
                )
        try:
            min_score = float(raw["min_top_score"])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"regression queries[{i}].min_top_score must be numeric, "
                f"got {raw['min_top_score']!r}"
            ) from exc
        if not (0.0 <= min_score <= 1.0):
            raise ValueError(
                f"regression queries[{i}].min_top_score={min_score} "
                f"must be in [0.0, 1.0]"
            )
        entries.append(dict(raw))

    return entries


# ── Stratification validator ───────────────────────────────────────────────


def _bucket_distribution(
    entries: list[dict[str, Any]], field: str
) -> dict[str, float]:
    """Compute observed bucket percentages for ``field`` across ``entries``."""
    counts: dict[str, int] = {}
    for entry in entries:
        bucket = entry.get(field)
        if bucket is None:
            continue
        counts[bucket] = counts.get(bucket, 0) + 1
    total = sum(counts.values())
    if total == 0:
        return {}
    return {k: 100.0 * v / total for k, v in counts.items()}


def validate_stratification(
    entries: list[dict[str, Any]],
    *,
    targets: dict[str, dict[str, float]] | None = None,
    tolerance_pct: float = STRATIFICATION_TOLERANCE_PCT,
) -> list[str]:
    """Check stratification distribution and emit warnings on drift.

    Returns the list of warning messages emitted (also logged at WARNING).
    Does NOT raise — PRD Risk #7: targets are guidelines, not hard errors.
    """
    targets = targets if targets is not None else STRATIFICATION_TARGETS
    warnings_emitted: list[str] = []

    for field, target_buckets in targets.items():
        observed = _bucket_distribution(entries, field)

        for bucket, target_pct in target_buckets.items():
            actual_pct = observed.get(bucket, 0.0)
            drift = abs(actual_pct - target_pct)
            if drift > tolerance_pct:
                msg = (
                    f"stratification drift on {field}={bucket!r}: "
                    f"observed {actual_pct:.1f}% vs target {target_pct:.1f}% "
                    f"(±{tolerance_pct:.0f}% tolerance); "
                    f"observed {sum(1 for e in entries if e.get(field) == bucket)} of {len(entries)} entries"
                )
                _logger.warning(msg)
                warnings_emitted.append(msg)

        # Flag buckets that exist in observed but aren't in targets — could
        # be a typo or a new bucket the targets table missed.
        unknown_buckets = set(observed) - set(target_buckets)
        for bucket in unknown_buckets:
            msg = (
                f"unknown {field}={bucket!r} not in target table "
                f"(observed {observed[bucket]:.1f}%)"
            )
            _logger.warning(msg)
            warnings_emitted.append(msg)

    return warnings_emitted


# ── Audit-goldens drift detection (Phase 2.6) ──────────────────────────────


def audit_goldens_drift(
    per_query: list[Any],
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compare observed replay results against the golden metadata and
    return a list of drift records.

    A drift record is emitted when the observed runtime tier does not match
    ``tier_expected`` OR when the query returned zero results despite a
    non-``zero-result`` ``path_flavor`` in the metadata. Pure function —
    the actual replay runs upstream (typically via ``run_replay_sync``);
    this layer only diffs observed vs expected.

    Returns an empty list when the corpus is in agreement.

    Raises ``ValueError`` on length mismatch — silent index drift would
    misattribute drifts across queries.
    """
    if len(per_query) != len(entries):
        raise ValueError(
            f"audit length mismatch: per_query has {len(per_query)} entries, "
            f"goldens has {len(entries)}. Run replay over the same query "
            f"set in the same order before auditing."
        )

    drifts: list[dict[str, Any]] = []
    for entry, observed in zip(entries, per_query):
        observed_tier = getattr(observed, "tier", "") or ""
        observed_count = getattr(observed, "results_count", 0) or 0
        observed_error = getattr(observed, "error", "") or ""
        expected_tier = entry.get("tier_expected", "")
        flavor = entry.get("path_flavor", "")

        reasons: list[str] = []
        if expected_tier and observed_tier and observed_tier != expected_tier:
            reasons.append(
                f"tier_drift (expected {expected_tier!r}, observed {observed_tier!r})"
            )
        if (
            flavor not in ("zero-result", "error-inducing")
            and observed_count == 0
            and not observed_error
        ):
            reasons.append("unexpected_zero_results")
        if flavor == "happy" and observed_error:
            reasons.append(f"unexpected_error: {observed_error[:80]}")

        if reasons:
            drifts.append({
                "query": entry.get("query", ""),
                "tier_expected": expected_tier,
                "tier_observed": observed_tier,
                "path_flavor": flavor,
                "results_count": observed_count,
                "error": observed_error,
                "reasons": reasons,
            })
    return drifts
