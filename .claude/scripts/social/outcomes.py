"""Manual, append-only outcome evidence for the Socials authority lane.

These records describe observed movement after a post, article, or repository
event.  They never claim causal attribution and never grant the Socials persona
new tools, capabilities, autonomy, or publication authority.  GitHub metrics
are deliberately stored as correlated deltas, not conversions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCHEMA = "social-outcome/v1"
_PERSONA_ID = "socials"
_SUBJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#-]{0,199}$")
_INTEGER_RE = re.compile(r"^[+-]?\d+$")
_SECRET_RE = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+/=-]{12,}|"
    r"(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|secret)\s*[:=]\s*\S+)"
)

ENGAGEMENT_METRICS = frozenset(
    {
        "post_saves",
        "substantive_comments",
        "profile_views",
        "qualified_dms",
        "article_sessions",
        "gsc_impressions",
        "ai_citations",
    }
)
GITHUB_DELTA_METRICS = frozenset(
    {
        "repo_views_delta",
        "repo_clones_delta",
        "stars_delta",
        "forks_delta",
        "installs_delta",
        "issues_delta",
        "contributors_delta",
    }
)
ALLOWED_METRICS = ENGAGEMENT_METRICS | GITHUB_DELTA_METRICS


@dataclass(frozen=True, slots=True)
class SocialOutcome:
    schema_version: str
    outcome_id: str
    subject_id: str
    observed_at: str
    recorded_at: str
    metrics: dict[str, int]
    note: str
    conversion_attribution: bool
    github_attribution: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _resolve_store_path() -> Path:
    import config

    return Path(config.DATA_DIR) / "social_outcomes.jsonl"


def _aware(value: datetime | None, *, name: str) -> datetime:
    resolved = value or datetime.now(UTC)
    if resolved.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return resolved.astimezone(UTC)


def _clean_subject(value: str) -> str:
    subject = str(value or "").strip()
    if not _SUBJECT_RE.fullmatch(subject):
        raise ValueError("subject_id must be one safe post, article, or repository identifier")
    return subject


def _clean_note(value: str) -> str:
    note = " ".join(str(value or "").split())
    if len(note) > 1_000:
        raise ValueError("note must be at most 1000 characters")
    if _SECRET_RE.search(note):
        raise ValueError("note contains credential-like text")
    return note


def validate_metrics(metrics: Mapping[str, Any]) -> dict[str, int]:
    if not metrics:
        raise ValueError("at least one outcome metric is required")
    unknown = sorted(set(metrics) - ALLOWED_METRICS)
    if unknown:
        raise ValueError(f"unknown outcome metrics: {', '.join(unknown)}")
    validated: dict[str, int] = {}
    for name in sorted(metrics):
        raw = metrics[name]
        if isinstance(raw, bool):
            raise ValueError(f"{name} must be an integer, not boolean")
        if not isinstance(raw, int) and not _INTEGER_RE.fullmatch(str(raw).strip()):
            raise ValueError(f"{name} must be an exact integer")
        value = int(raw)
        if name in ENGAGEMENT_METRICS and value < 0:
            raise ValueError(f"{name} cannot be negative")
        if abs(value) > 1_000_000_000:
            raise ValueError(f"{name} is outside the supported range")
        validated[name] = value
    return validated


def _outcome_id(
    *,
    subject_id: str,
    observed_at: datetime,
    metrics: Mapping[str, int],
    note: str,
) -> str:
    canonical = json.dumps(
        {
            "schema_version": _SCHEMA,
            "subject_id": subject_id,
            "observed_at": observed_at.isoformat(timespec="seconds"),
            "metrics": dict(sorted(metrics.items())),
            "note": note,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"so_{observed_at:%Y%m%dT%H%M%SZ}_{digest}"


def build_outcome(
    subject_id: str,
    metrics: Mapping[str, Any],
    *,
    observed_at: datetime | None = None,
    recorded_at: datetime | None = None,
    note: str = "",
) -> SocialOutcome:
    observed = _aware(observed_at, name="observed_at")
    recorded = _aware(recorded_at, name="recorded_at")
    subject = _clean_subject(subject_id)
    clean_metrics = validate_metrics(metrics)
    clean_note = _clean_note(note)
    has_github = bool(set(clean_metrics) & GITHUB_DELTA_METRICS)
    return SocialOutcome(
        schema_version=_SCHEMA,
        outcome_id=_outcome_id(
            subject_id=subject,
            observed_at=observed,
            metrics=clean_metrics,
            note=clean_note,
        ),
        subject_id=subject,
        observed_at=observed.isoformat(timespec="seconds"),
        recorded_at=recorded.isoformat(timespec="seconds"),
        metrics=clean_metrics,
        note=clean_note,
        conversion_attribution=False,
        github_attribution=(
            "correlated_movement_not_conversion" if has_github else None
        ),
    )


def _append_once(path: Path, outcome: SocialOutcome) -> str:
    from shared import file_lock

    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path, timeout=10.0):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        for line in lines:
            try:
                existing = json.loads(line)
            except json.JSONDecodeError:
                continue
            if existing.get("outcome_id") == outcome.outcome_id:
                return "duplicate"
        payload = json.dumps(outcome.as_dict(), ensure_ascii=False, sort_keys=True)
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(payload + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    return "written"


def _write_experience_note(
    outcome: SocialOutcome,
    *,
    persona_root: Path | None,
    reindex: bool,
) -> dict[str, Any]:
    """Append a deterministic evidence note with an explicit no-grant label."""

    try:
        from personas import experience

        content = json.dumps(
            {
                "outcome_id": outcome.outcome_id,
                "subject_id": outcome.subject_id,
                "observed_at": outcome.observed_at,
                "metrics": outcome.metrics,
                "note": outcome.note,
                "attribution": outcome.github_attribution or "observed_not_attributed",
                "capability_effect": "none",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        label = f"social-outcome:{outcome.outcome_id}"
        section = experience.render_ingest_section(
            label=label,
            content=content,
            source="operator-recorded authority outcome",
            note=(
                "Evidence receipt only. Grants no tools, capabilities, autonomy, "
                "publication authority, or causal conversion credit."
            ),
            local_time=datetime.fromisoformat(outcome.recorded_at),
        )
        return experience.append_experience_section(
            persona_id=_PERSONA_ID,
            section=section,
            dedup_key=experience.ingest_key(label, content),
            local_time=datetime.fromisoformat(outcome.recorded_at),
            root=persona_root,
            reindex=reindex,
        )
    except Exception as exc:  # noqa: BLE001 - ledger success must survive note failure
        return {"status": "error", "detail": type(exc).__name__}


def record_outcome(
    subject_id: str,
    metrics: Mapping[str, Any],
    *,
    observed_at: datetime | None = None,
    recorded_at: datetime | None = None,
    note: str = "",
    store_path: Path | None = None,
    persona_root: Path | None = None,
    reindex: bool = True,
) -> dict[str, Any]:
    """Persist one idempotent outcome and mirror it to Socials experience."""

    outcome = build_outcome(
        subject_id,
        metrics,
        observed_at=observed_at,
        recorded_at=recorded_at,
        note=note,
    )
    path = Path(store_path) if store_path is not None else _resolve_store_path()
    ledger_status = _append_once(path, outcome)
    note_receipt = _write_experience_note(
        outcome,
        persona_root=persona_root,
        reindex=reindex,
    )
    return {
        "status": ledger_status,
        "outcome": outcome.as_dict(),
        "store_path": str(path),
        "experience_note": note_receipt,
    }


def list_outcomes(
    *,
    subject_id: str | None = None,
    store_path: Path | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    path = Path(store_path) if store_path is not None else _resolve_store_path()
    wanted = _clean_subject(subject_id) if subject_id is not None else None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return []
    rows: list[dict[str, Any]] = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("schema_version") != _SCHEMA:
            continue
        if wanted is not None and row.get("subject_id") != wanted:
            continue
        rows.append(row)
        if len(rows) >= max(1, min(int(limit), 500)):
            break
    return rows


def parse_outcome_arguments(arguments: str | Sequence[str]) -> tuple[str, dict[str, int], str]:
    """Parse ``<subject> metric=value ... [note='...']`` for `/social`."""

    tokens = shlex.split(arguments) if isinstance(arguments, str) else list(arguments)
    if not tokens:
        raise ValueError("usage: /social outcome <id> metric=value ... [note='...']")
    subject = _clean_subject(tokens[0])
    metrics: dict[str, Any] = {}
    note = ""
    for token in tokens[1:]:
        if "=" not in token:
            raise ValueError(f"expected name=value, got {token!r}")
        name, raw = token.split("=", 1)
        normalized = name.strip().casefold().replace("-", "_")
        if normalized == "note":
            note = raw
            continue
        if normalized in metrics:
            raise ValueError(f"duplicate metric {normalized}")
        metrics[normalized] = raw
    return subject, validate_metrics(metrics), _clean_note(note)


def main() -> None:
    parser = argparse.ArgumentParser(description="Record a manual Socials outcome")
    parser.add_argument("subject_id")
    parser.add_argument("metrics", nargs="+", help="metric=value pairs")
    parser.add_argument("--note", default="")
    args = parser.parse_args()
    subject, metrics, _parsed_note = parse_outcome_arguments(
        [args.subject_id, *args.metrics]
    )
    receipt = record_outcome(subject, metrics, note=args.note)
    print(json.dumps(receipt, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()


__all__ = [
    "ALLOWED_METRICS",
    "ENGAGEMENT_METRICS",
    "GITHUB_DELTA_METRICS",
    "SocialOutcome",
    "build_outcome",
    "list_outcomes",
    "parse_outcome_arguments",
    "record_outcome",
    "validate_metrics",
]
