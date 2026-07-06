"""Per-skill recurrence telemetry sidecar (Skill-From-Experience Loop, WS2 / Rail 3).

A JSON-map sidecar at ``DATA_DIR/skill_usage.json`` keyed by skill name. It turns
the existing conflict-detection signal (a GENERATED draft re-appearing) into a
recurrence counter, and graduates a draft to ``state="eligible"`` once it recurs
``SKILL_PROMOTE_REUSE_THRESHOLD`` times. Promotion itself (the operator gate +
scan + physical move) lives in ``cognition.skill_promotion`` (WS3); this module
is pure state.

Design invariants (PRP §Known Gotchas):

- Rule 1 — the sidecar path AND the config knobs are resolved at CALL TIME inside
  each function body. NOTHING is bound at import (do NOT repeat
  ``browser_audit.py:18``'s import-time ``BROWSER_AUDIT_LOG = DATA_DIR / ...``).
- Rule 2 — state derives ONLY from the physical sidecar on disk, never a cache.
- M4 (locking) — every read-modify-write acquires ``shared.file_lock`` DIRECTLY
  around the RMW (the ``proactive_actions._append_lock`` idiom). We do NOT
  "mirror StagingStore", whose ``_update_record`` relies on the CALLER holding
  the lock and does not acquire it itself.
- NM1 (Windows-safe atomic write) — write a sibling temp file, flush, CLOSE it,
  THEN ``os.replace``. Never ``os.replace`` while the temp handle is open.
- NM2 (audit ownership) — ``prune_stale`` flips state ONLY; it writes NO audit
  row. WS3's ``archive_stale`` owns the audit emission for stale archival.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Iterator
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    # Cross-process file lock (M4): the read-then-write of the recurrence counter
    # must be atomic across concurrent cross-session turns (the router serializes
    # per conversation key only; two threads/channels can record recurrence for
    # the same skill name concurrently). Without the lock both read the map
    # before either writes -> a lost update (recurrence_count under-counts).
    from shared import file_lock as _file_lock
except Exception:  # pragma: no cover - optional when imported outside scripts env
    _file_lock = None  # type: ignore[assignment]


SIDECAR_FILE_NAME = "skill_usage.json"

USAGE_STATES = frozenset(
    {
        "staged",
        "eligible",
        "promoted",
        "stale",
        "archived",
    }
)

_DEFAULT_PROMOTE_REUSE_THRESHOLD = 3
_DEFAULT_STALE_DAYS = 30


@dataclass
class SkillUsage:
    """Recurrence telemetry for one self-authored (generated) skill draft."""

    name: str
    created_at: str
    recurrence_count: int = 0
    last_seen_at: str = ""
    state: str = "staged"  # staged | eligible | promoted | stale | archived
    source_session: str = ""
    scan_verdict: str = ""
    promoted_at: str | None = None
    path: str = ""  # disambiguates duplicate draft names (PRP minor / ConflictMatch.path)


# --------------------------------------------------------------------------- #
# Call-time resolvers (Rule 1) — never bind these at import.
# --------------------------------------------------------------------------- #


def _resolve_sidecar_path(path: Path | str | None = None) -> Path:
    """Resolve the sidecar path at CALL TIME (Rule 1).

    Explicit ``path`` wins (tests). Otherwise ``DATA_DIR/skill_usage.json`` is
    resolved by importing ``config`` inside the body so ``HOMIE_HOME`` / test
    path overrides and ``monkeypatch.setattr(config, "DATA_DIR", ...)`` take
    effect on the next call with no module reload.
    """
    if path is not None:
        return Path(path)
    try:
        import config

        # Attribute access (not ``from config import DATA_DIR``) so a test's
        # ``monkeypatch.setattr(config, "DATA_DIR", ...)`` is honored on the
        # next call with no module reload (Rule 1 call-time resolution).
        base = Path(config.DATA_DIR)
    except Exception:  # pragma: no cover - import path fallback for direct scripts
        from personas import get_default_paths

        base = get_default_paths()["data"]
    return base / SIDECAR_FILE_NAME


def _resolve_threshold(threshold: int | None = None) -> int:
    """Resolve the reuse threshold at CALL TIME (Rule 1, None-sentinel).

    WS4 adds the real ``SKILL_PROMOTE_REUSE_THRESHOLD`` config knob; until then
    (and whenever config lacks it) fall back to ``3``.
    """
    if threshold is not None:
        return int(threshold)
    try:
        from config import SKILL_PROMOTE_REUSE_THRESHOLD

        return int(SKILL_PROMOTE_REUSE_THRESHOLD)
    except Exception:
        return _DEFAULT_PROMOTE_REUSE_THRESHOLD


def _resolve_stale_days(stale_days: int | None = None) -> int:
    """Resolve the stale-archive horizon at CALL TIME (Rule 1, None-sentinel).

    WS4 adds the real ``SKILL_STALE_DAYS`` config knob; until then (and whenever
    config lacks it) fall back to ``30``.
    """
    if stale_days is not None:
        return int(stale_days)
    try:
        from config import SKILL_STALE_DAYS

        return int(SKILL_STALE_DAYS)
    except Exception:
        return _DEFAULT_STALE_DAYS


@contextlib.contextmanager
def _sidecar_lock(path: Path) -> Iterator[None]:
    """Guard the sidecar RMW with the shared cross-process lock (M4).

    Fail-open: if ``shared.file_lock`` is unavailable (module imported outside
    the scripts env) the RMW proceeds unlocked — telemetry must never hard-fail
    a turn. The lock is the load-bearing serializer for the concurrent
    cross-session case (proven by the N-thread test).
    """
    if _file_lock is None:
        yield
        return
    with _file_lock(path):
        yield


# --------------------------------------------------------------------------- #
# Physical sidecar IO (Rule 2) — the JSON map is the only source of truth.
# --------------------------------------------------------------------------- #


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _coerce_usage(record: dict[str, Any]) -> SkillUsage | None:
    """Build a SkillUsage from a raw record, tolerating unknown/missing keys."""
    if not isinstance(record, dict):
        return None
    names = {f.name for f in fields(SkillUsage)}
    payload = {k: v for k, v in record.items() if k in names}
    try:
        return SkillUsage(**payload)
    except (TypeError, ValueError):
        return None


def _read_map(path: Path) -> dict[str, SkillUsage]:
    """Read the physical sidecar into a name -> SkillUsage map (Rule 2).

    A missing file is an empty map; a corrupt/torn file degrades to an empty
    map rather than crashing a turn (the lock makes torn reads vanishingly rare,
    but telemetry stays fail-open regardless).
    """
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, SkillUsage] = {}
    for name, record in raw.items():
        usage = _coerce_usage(record)
        if usage is not None:
            usage.name = name  # the map key is authoritative
            out[name] = usage
    return out


def _write_map_atomic(path: Path, data: dict[str, SkillUsage]) -> None:
    """Atomically replace the sidecar (NM1 — Windows-safe).

    Write a sibling temp file in the SAME directory, flush, CLOSE the handle,
    THEN ``os.replace``. ``os.replace`` is atomic only on the same filesystem,
    so the temp must be a sibling; and on Windows it fails if the temp handle is
    still open, so the ``with`` block is fully exited (file closed) before
    replace.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {name: asdict(usage) for name, usage in data.items()}
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            handle.flush()
        # handle is now CLOSED (with-block exited) -> safe to replace on win32.
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            if tmp_path.exists():
                tmp_path.unlink()
        raise


# --------------------------------------------------------------------------- #
# Public API (consumed by WS3 promote()/archive_stale and WS4 wiring).
# --------------------------------------------------------------------------- #


def record_recurrence(
    name: str,
    *,
    source_session: str = "",
    path: str = "",
    threshold: int | None = None,
    sidecar_path: Path | str | None = None,
) -> SkillUsage:
    """Record one recurrence of a GENERATED draft and return its usage row.

    Increments ``recurrence_count`` and stamps ``last_seen_at``. When the count
    reaches the reuse threshold AND the row is still ``staged``, the state flips
    to ``eligible`` (the promotion gate then reads this physical state, Rule 2).

    Creates the row on first sight. The whole read-modify-write runs under ONE
    ``shared.file_lock`` so concurrent cross-session turns cannot lose an
    increment (M3/M4). ``path`` disambiguates duplicate draft names.

    Args:
        name: the MATCHED draft's name (B2 — recurrence is keyed on the matched
            generated draft, not the new proposal's name).
        source_session: best-effort provenance tag.
        path: filesystem path of the matched draft (stored to disambiguate).
        threshold: None-sentinel override for the reuse threshold (tests/tuning);
            resolves ``SKILL_PROMOTE_REUSE_THRESHOLD`` when None.
        sidecar_path: None-sentinel override for the sidecar path (tests).
    """
    sidecar = _resolve_sidecar_path(sidecar_path)
    limit = _resolve_threshold(threshold)
    with _sidecar_lock(sidecar):
        data = _read_map(sidecar)
        usage = data.get(name)
        if usage is None:
            usage = SkillUsage(name=name, created_at=_now_iso())
            data[name] = usage
        usage.recurrence_count += 1
        usage.last_seen_at = _now_iso()
        if source_session:
            usage.source_session = source_session
        if path:
            usage.path = path
        if usage.state == "staged" and usage.recurrence_count >= limit:
            usage.state = "eligible"
        _write_map_atomic(sidecar, data)
        return usage


def get_usage(
    name: str,
    *,
    sidecar_path: Path | str | None = None,
) -> SkillUsage | None:
    """Return the physical usage row for ``name`` (Rule 2), or None if absent."""
    sidecar = _resolve_sidecar_path(sidecar_path)
    with _sidecar_lock(sidecar):
        return _read_map(sidecar).get(name)


def mark_state(
    name: str,
    state: str,
    *,
    sidecar_path: Path | str | None = None,
) -> None:
    """Set the lifecycle state for ``name`` (no-op if the row is absent).

    Stamps ``promoted_at`` when transitioning to ``promoted``. RMW under the
    shared lock (M4). Raises ``ValueError`` on an unknown state (a contract bug,
    not a runtime condition).
    """
    if state not in USAGE_STATES:
        raise ValueError(f"unknown skill usage state: {state!r}")
    sidecar = _resolve_sidecar_path(sidecar_path)
    with _sidecar_lock(sidecar):
        data = _read_map(sidecar)
        usage = data.get(name)
        if usage is None:
            return
        usage.state = state
        if state == "promoted" and not usage.promoted_at:
            usage.promoted_at = _now_iso()
        _write_map_atomic(sidecar, data)


def list_eligible(
    threshold: int | None = None,
    *,
    sidecar_path: Path | str | None = None,
) -> list[SkillUsage]:
    """Return rows that are ``state=="eligible"`` AND meet the reuse threshold.

    The threshold gate is re-applied here (not just trusted from the stored
    state) so a config change to the threshold is honored against the physical
    counter (Rule 2).
    """
    sidecar = _resolve_sidecar_path(sidecar_path)
    limit = _resolve_threshold(threshold)
    with _sidecar_lock(sidecar):
        data = _read_map(sidecar)
    return [
        usage
        for usage in data.values()
        if usage.state == "eligible" and usage.recurrence_count >= limit
    ]


def prune_stale(
    stale_days: int | None = None,
    *,
    sidecar_path: Path | str | None = None,
) -> list[str]:
    """Flip ``staged`` rows untouched for > ``stale_days`` to ``archived``.

    Returns the names that were archived. Only ``staged`` rows are pruned —
    ``eligible`` / ``promoted`` rows are never auto-archived. A row with no
    ``last_seen_at`` falls back to ``created_at`` for the age check.

    NM2: this writes NO audit row. WS3's ``skill_promotion.archive_stale`` calls
    this and then emits one audit row per archived name. Keeping audit ownership
    in WS3 means WS2 has no audit dependency.
    """
    sidecar = _resolve_sidecar_path(sidecar_path)
    days = _resolve_stale_days(stale_days)
    cutoff = datetime.now(UTC) - timedelta(days=days)
    cutoff_iso = cutoff.isoformat()
    archived: list[str] = []
    with _sidecar_lock(sidecar):
        data = _read_map(sidecar)
        for name, usage in data.items():
            if usage.state != "staged":
                continue
            seen = usage.last_seen_at or usage.created_at
            if seen and seen < cutoff_iso:
                usage.state = "archived"
                archived.append(name)
        if archived:
            _write_map_atomic(sidecar, data)
    return archived


__all__ = (
    "SIDECAR_FILE_NAME",
    "USAGE_STATES",
    "SkillUsage",
    "record_recurrence",
    "get_usage",
    "mark_state",
    "list_eligible",
    "prune_stale",
)
