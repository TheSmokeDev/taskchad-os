"""Called-Shots ledger — the receipts spine (epic #186, T1 #187).

SQLite-backed disagreement ledger: when the Homie challenges an operator
position, the bet is RECORDED — both sides, the reasoning, the receipts —
then reconciled when the outcome lands. Accuracy ("2 of 3 on pricing") is
always DERIVED BY QUERY over resolved rows (Rule 2 — no stored counter can
drift from the rows that prove it).

T2 obligation: check ``record_shot``'s return BEFORE weaving a challenge
into a reply — ``None`` means the bet was NOT staked (fail-open path); never
claim a bet the ledger doesn't hold.

Contract (consumed by T2 challenge surface + T3 reconcile/callback):
  - Every entrypoint is hard-gated by the operator kill-switch
    ``HOMIE_KILLSWITCH_CALLED_SHOTS`` (default-ON — absent env = enabled; the
    switch only turns the feature OFF). A refusal RAISES ``KillSwitchDisabled``
    per the kill-switch contract — callers catch and degrade.
  - Contract errors (empty persona_id, unknown decided_by/outcome) raise
    ``ValueError`` — those are caller bugs, not runtime conditions.
  - Runtime failures (DB/IO) NEVER escape: record/reconcile return ``None``,
    ``list_open`` returns ``[]``, ``track_record`` returns zeros — each with a
    visible stdout receipt (fail-open, matching promotion/staging).
  - ``persona_id`` is NOT NULL at the SCHEMA level (the owner_id seam — per
    the architecture doc it makes per-persona scorecards and the multi-tenant
    path free later).
  - The vault mirror note is DERIVED state: written best-effort after a
    successful row write; a mirror failure never fails the write.

Pattern sources: social/db.py (per-call WAL connection, CHECK constraints),
self_model.py (boot-shim, atomic tmp+os.replace), config resolver Rule 1.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# Boot-shim: resolve the active persona's paths BEFORE any framework import
# (mirrors self_model.py — a standalone run picks up the right profile).
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from personas import apply_persona_override  # noqa: E402

apply_persona_override()

from security import kill_switches  # noqa: E402

DECIDED_BY_VALUES = ("operator", "homie", "open")
# "void" is the RETRACT path: the bet should never have existed (a
# false-positive challenge struck from the record). Void rows resolve the
# shot's lifecycle but are EXCLUDED from the accuracy math — they are not an
# outcome anyone was right about.
OUTCOME_VALUES = ("operator_right", "homie_right", "push", "void")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS called_shots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_id TEXT NOT NULL CHECK (length(persona_id) > 0),
    created_at TEXT NOT NULL,
    domain TEXT NOT NULL DEFAULT '',
    operator_position TEXT NOT NULL DEFAULT '',
    homie_position TEXT NOT NULL DEFAULT '',
    homie_reasoning TEXT NOT NULL DEFAULT '',
    receipts TEXT NOT NULL DEFAULT '[]',
    decided_by TEXT NOT NULL DEFAULT 'open'
        CHECK (decided_by IN ('operator', 'homie', 'open')),
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'resolved')),
    outcome TEXT
        CHECK (outcome IS NULL
               OR outcome IN ('operator_right', 'homie_right', 'push', 'void')),
    resolved_at TEXT,
    -- Kimi re-gate LOW: couple the state pair at the SCHEMA level so a future
    -- writer (T3 sweep, repair script) can never mint a resolved-with-NULL-
    -- outcome row that track_record's fold would silently drop from every
    -- denominator. One-way door — cheapest before the first production DB.
    CHECK ((status = 'open' AND outcome IS NULL)
           OR (status = 'resolved' AND outcome IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_called_shots_persona_status
    ON called_shots(persona_id, status);
CREATE INDEX IF NOT EXISTS idx_called_shots_persona_domain
    ON called_shots(persona_id, domain);
"""


@dataclass
class CalledShot:
    """One staked disagreement — a row of the ledger."""

    id: int
    persona_id: str
    created_at: str
    domain: str
    operator_position: str
    homie_position: str
    homie_reasoning: str
    receipts: list[str] = field(default_factory=list)
    decided_by: str = "open"
    status: str = "open"
    outcome: str | None = None
    resolved_at: str | None = None


@dataclass
class TrackRecord:
    """Per-(persona, domain) accuracy — ALWAYS derived by query (Rule 2).

    ``resolved`` EXCLUDES void rows: a voided shot was struck from the record
    (false-positive challenge), so it never enters an accuracy denominator.
    ``ok=False`` marks a RUNTIME-FAILURE return — zeros then mean "the ledger
    could not be read", NOT "no history"; callers (T3's callback) must check
    ``ok`` before rendering any track-record claim.
    """

    persona_id: str
    domain: str | None
    resolved: int = 0
    operator_right: int = 0
    homie_right: int = 0
    push: int = 0
    void: int = 0
    open: int = 0
    ok: bool = True


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def shot_age_days(created_at: object, now: datetime | None = None) -> float | None:
    """Age of an ISO ``created_at`` in days, or ``None`` on unparseable input.

    The ONE age parser for the called-shots surface (LOW-4): the T3 stale
    sweep and the ``/shots`` list renderer kept divergent copies of this
    ISO-parse / naive-as-UTC / age-in-days logic — hoisted here so both read
    one truth and only their RENDERS differ. ``now`` is a None-sentinel clock
    (Rule 1 — resolved at call time, never frozen as a default): the sweep
    passes one shared ``now`` for a whole batch, the renderer lets it default
    per row. Fail-open — any parse/arithmetic error returns ``None`` and the
    caller's renderer decides how to show a missing age (skip the row / '?').
    """
    try:
        stamp = datetime.fromisoformat(str(created_at))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        if now is None:
            now = datetime.now(UTC)
        return (now - stamp).total_seconds() / 86400.0
    except Exception:
        return None


def _normalize_domain(domain: object) -> str:
    """One domain normalizer for the write AND the read (the persona grain
    discipline applied to domains): strip + casefold so an LLM-produced
    "Pricing " and an operator-typed "pricing" land in ONE bucket. The empty
    string is preserved — it is the valid "no domain" bucket, not an error.
    """
    return str(domain or "").strip().casefold()


def _normalize_persona(
    persona_id: object,
    *,
    allow_none: bool = False,
) -> str | None:
    """One normalizer for EVERY entrypoint (Rule 4 — the write and the reads
    must key at the same grain, so " sales " can never record as "sales" but
    query as a different persona).

    ``allow_none=True`` (list_open): ``None`` means "all personas" — but an
    EXPLICIT empty/whitespace-only string is a caller bug, not a wildcard, and
    raises rather than silently widening to a cross-persona read.
    """
    if persona_id is None:
        if allow_none:
            return None
        raise ValueError("persona_id is required (owner_id seam)")
    text = str(persona_id).strip()
    if not text:
        raise ValueError("persona_id must be a non-empty string (owner_id seam)")
    return text


def _resolve_db_path(db_path: str | Path | None) -> Path:
    """None sentinel -> settings at CALL TIME (Rule 1)."""
    if db_path is not None:
        return Path(db_path)
    from config import get_called_shots_settings

    return Path(get_called_shots_settings().db_path)


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA_SQL)
    return conn


def _row_to_shot(row: sqlite3.Row) -> CalledShot:
    try:
        receipts = json.loads(row["receipts"] or "[]")
        if not isinstance(receipts, list):
            receipts = []
    except (json.JSONDecodeError, TypeError):
        receipts = []
    return CalledShot(
        id=row["id"],
        persona_id=row["persona_id"],
        created_at=row["created_at"],
        domain=row["domain"],
        operator_position=row["operator_position"],
        homie_position=row["homie_position"],
        homie_reasoning=row["homie_reasoning"],
        receipts=[str(r) for r in receipts],
        decided_by=row["decided_by"],
        status=row["status"],
        outcome=row["outcome"],
        resolved_at=row["resolved_at"],
    )


def record_shot(
    persona_id: str,
    domain: str,
    operator_position: str,
    homie_position: str,
    homie_reasoning: str = "",
    receipts: list[str] | None = None,
    decided_by: str = "open",
    *,
    db_path: str | Path | None = None,
) -> CalledShot | None:
    """Stake a bet. Returns the recorded shot, or None on a runtime failure.

    Raises ``KillSwitchDisabled`` when the operator turned the feature off,
    ``ValueError`` on contract errors (empty persona_id / bad decided_by).
    """
    kill_switches.requireEnabled("called_shots", caller="record_shot")
    persona_id = _normalize_persona(persona_id)
    if decided_by not in DECIDED_BY_VALUES:
        raise ValueError(
            f"record_shot: decided_by must be one of {DECIDED_BY_VALUES}, "
            f"got {decided_by!r}"
        )
    try:
        path = _resolve_db_path(db_path)
        conn = _connect(path)
        try:
            cur = conn.execute(
                """INSERT INTO called_shots
                   (persona_id, created_at, domain, operator_position,
                    homie_position, homie_reasoning, receipts, decided_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    persona_id,
                    _now_iso(),
                    _normalize_domain(domain),
                    operator_position or "",
                    homie_position or "",
                    homie_reasoning or "",
                    json.dumps([str(r) for r in (receipts or [])], ensure_ascii=False),
                    decided_by,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM called_shots WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        finally:
            conn.close()
        shot = _row_to_shot(row)
        # Derived state — best-effort, never fails the write.
        _maybe_write_mirror(shot, db_path=path)
        return shot
    except Exception as exc:
        # Broad by design: the gate + contract ValueErrors sit BEFORE this try,
        # so KillSwitchDisabled and caller bugs still propagate — everything at
        # runtime (settings parse, Path(), serialization, sqlite, IO) fails open.
        print(f"[called_shots] record_shot failed (fail-open): {exc!r}", flush=True)
        return None


def list_open(
    persona_id: str | None = None,
    *,
    db_path: str | Path | None = None,
) -> list[CalledShot]:
    """Open (unreconciled) shots, newest first. [] on runtime failure.

    Raises ``KillSwitchDisabled`` when the operator turned the feature off.
    """
    kill_switches.requireEnabled("called_shots", caller="list_open")
    persona_id = _normalize_persona(persona_id, allow_none=True)
    try:
        conn = _connect(_resolve_db_path(db_path))
        try:
            if persona_id:
                rows = conn.execute(
                    "SELECT * FROM called_shots WHERE status = 'open' "
                    "AND persona_id = ? ORDER BY id DESC",
                    (persona_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM called_shots WHERE status = 'open' "
                    "ORDER BY id DESC"
                ).fetchall()
        finally:
            conn.close()
        return [_row_to_shot(r) for r in rows]
    except Exception as exc:
        # Broad by design — gate + contract errors sit before the try.
        print(f"[called_shots] list_open failed (fail-open): {exc!r}", flush=True)
        return []


def reconcile(
    shot_id: int,
    outcome: str,
    *,
    persona_id: str | None = None,
    db_path: str | Path | None = None,
) -> CalledShot | None:
    """Settle an OPEN shot with an outcome. Returns the resolved shot.

    ``persona_id`` (additive, Rule 4): when provided, the UPDATE is keyed at
    the authorizing grain — a caller acting for one persona cannot settle
    another persona's bet. ``None`` keeps the legacy global behavior.
    ``outcome="void"`` is the retract path: the shot's lifecycle closes but
    the row is excluded from all accuracy math (see ``TrackRecord``).

    Returns None when the id is unknown, the persona doesn't match, the shot
    is already resolved, or on a runtime failure (each with a DISTINCT visible
    receipt). Only open->resolved transitions ever write — a resolved row is
    immutable history.

    Raises ``KillSwitchDisabled`` when the operator turned the feature off,
    ``ValueError`` on an unknown outcome or an explicit-but-empty persona_id.
    """
    kill_switches.requireEnabled("called_shots", caller="reconcile")
    if outcome not in OUTCOME_VALUES:
        raise ValueError(
            f"reconcile: outcome must be one of {OUTCOME_VALUES}, got {outcome!r}"
        )
    persona_id = _normalize_persona(persona_id, allow_none=True)
    try:
        path = _resolve_db_path(db_path)
        conn = _connect(path)
        try:
            if persona_id:
                cur = conn.execute(
                    "UPDATE called_shots SET status = 'resolved', outcome = ?, "
                    "resolved_at = ? WHERE id = ? AND status = 'open' "
                    "AND persona_id = ?",
                    (outcome, _now_iso(), shot_id, persona_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE called_shots SET status = 'resolved', outcome = ?, "
                    "resolved_at = ? WHERE id = ? AND status = 'open'",
                    (outcome, _now_iso(), shot_id),
                )
            conn.commit()
            if cur.rowcount == 0:
                row = conn.execute(
                    "SELECT status, persona_id FROM called_shots WHERE id = ?",
                    (shot_id,),
                ).fetchone()
                if row is None:
                    why = "unknown id"
                elif persona_id and row["persona_id"] != persona_id:
                    why = "persona mismatch"
                else:
                    why = "already resolved"
                print(
                    f"[called_shots] reconcile skipped shot {shot_id}: {why}",
                    flush=True,
                )
                return None
            row = conn.execute(
                "SELECT * FROM called_shots WHERE id = ?", (shot_id,)
            ).fetchone()
        finally:
            conn.close()
        shot = _row_to_shot(row)
        _maybe_write_mirror(shot, db_path=path)  # refresh the derived mirror note
        return shot
    except Exception as exc:
        # Broad by design — gate + contract errors sit before the try.
        print(f"[called_shots] reconcile failed (fail-open): {exc!r}", flush=True)
        return None


def set_decided_by(
    shot_id: int,
    decided_by: str,
    *,
    persona_id: str | None = None,
    db_path: str | Path | None = None,
) -> CalledShot | None:
    """Log WHO made the final call on an OPEN shot — a one-way ratchet.

    Kimi K3 design-gate adjudication (PR #192 review, applied via #191):
    ``decided_by`` is a DECISION-TIME fact and needs a decision-time writer —
    folding it into reconcile loses overrides on never-settled shots, and a
    stale open decided shot is a feature (the nag says "you overrode me here
    and we never settled it"). This is NOT a general mutator: valid ONLY on
    ``status='open'`` rows, ONLY transitioning FROM ``decided_by='open'``,
    target ONLY ``operator`` or ``homie`` — a single-field state transition
    that preserves T1's insert-only character. No outcome coupling.

    Returns the updated shot, or None when the id is unknown, the shot is not
    open, the call was already logged, the persona doesn't match, or on a
    runtime failure (each with a DISTINCT visible receipt).

    Raises ``KillSwitchDisabled`` when the operator turned the feature off,
    ``ValueError`` on a target outside {operator, homie} or an
    explicit-but-empty persona_id.
    """
    kill_switches.requireEnabled("called_shots", caller="set_decided_by")
    if decided_by not in ("operator", "homie"):
        raise ValueError(
            "set_decided_by: decided_by must be 'operator' or 'homie' "
            f"(one-way ratchet from 'open'), got {decided_by!r}"
        )
    persona_id = _normalize_persona(persona_id, allow_none=True)
    try:
        path = _resolve_db_path(db_path)
        conn = _connect(path)
        try:
            if persona_id:
                cur = conn.execute(
                    "UPDATE called_shots SET decided_by = ? "
                    "WHERE id = ? AND status = 'open' AND decided_by = 'open' "
                    "AND persona_id = ?",
                    (decided_by, shot_id, persona_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE called_shots SET decided_by = ? "
                    "WHERE id = ? AND status = 'open' AND decided_by = 'open'",
                    (decided_by, shot_id),
                )
            conn.commit()
            if cur.rowcount == 0:
                row = conn.execute(
                    "SELECT status, decided_by, persona_id FROM called_shots "
                    "WHERE id = ?",
                    (shot_id,),
                ).fetchone()
                if row is None:
                    why = "unknown id"
                elif row["status"] != "open":
                    why = "not open"
                elif row["decided_by"] != "open":
                    why = "already decided"
                elif persona_id and row["persona_id"] != persona_id:
                    why = "persona mismatch"
                else:  # pragma: no cover — unreachable if CAS terms are complete
                    why = "unmatched"
                print(
                    f"[called_shots] set_decided_by skipped shot {shot_id}: {why}",
                    flush=True,
                )
                return None
            row = conn.execute(
                "SELECT * FROM called_shots WHERE id = ?", (shot_id,)
            ).fetchone()
        finally:
            conn.close()
        shot = _row_to_shot(row)
        _maybe_write_mirror(shot, db_path=path)  # refresh the derived mirror note
        return shot
    except Exception as exc:
        # Broad by design — gate + contract errors sit before the try.
        print(f"[called_shots] set_decided_by failed (fail-open): {exc!r}", flush=True)
        return None


def track_record(
    persona_id: str,
    domain: str | None = None,
    *,
    db_path: str | Path | None = None,
) -> TrackRecord:
    """Who-was-right counts for (persona[, domain]) — DERIVED BY QUERY (Rule 2).

    Void rows count into ``.void`` ONLY — never into ``resolved`` or any
    accuracy denominator (a voided shot was struck, not decided). On a runtime
    failure returns zeros with ``ok=False`` — callers MUST check ``ok`` before
    claiming "no track record". Raises ``KillSwitchDisabled`` when the
    operator turned the feature off.
    """
    kill_switches.requireEnabled("called_shots", caller="track_record")
    persona_id = _normalize_persona(persona_id)
    if domain is not None:
        domain = _normalize_domain(domain)  # the write-side grain, symmetric
    record = TrackRecord(persona_id=persona_id, domain=domain)
    try:
        conn = _connect(_resolve_db_path(db_path))
        try:
            where = "persona_id = ?"
            params: list[object] = [persona_id]
            if domain is not None:
                where += " AND domain = ?"
                params.append(domain)
            for outcome, count in conn.execute(
                f"SELECT outcome, COUNT(*) FROM called_shots "
                f"WHERE {where} AND status = 'resolved' GROUP BY outcome",
                params,
            ).fetchall():
                if outcome == "void":
                    record.void = int(count)  # struck bets — NOT resolved
                elif outcome in OUTCOME_VALUES:
                    setattr(record, outcome, int(count))
                    record.resolved += int(count)
            row = conn.execute(
                f"SELECT COUNT(*) FROM called_shots WHERE {where} AND status = 'open'",
                params,
            ).fetchone()
            record.open = int(row[0])
        finally:
            conn.close()
        return record
    except Exception as exc:
        # Broad by design — gate + contract errors sit before the try. ok=False
        # marks these zeros as "ledger unreadable", never "no history".
        print(f"[called_shots] track_record failed (fail-open): {exc!r}", flush=True)
        return TrackRecord(persona_id=persona_id, domain=domain, ok=False)


# ---------------------------------------------------------------------------
# Additive READ surface (T3 R1 gate — honest-failure probes for consumers).
# Read-only URI connects (mode=ro can NEVER create/initialize a DB — these are
# probes, not the schema-creating ``_connect`` path). Additive only: nothing
# above this block changed.
# ---------------------------------------------------------------------------


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    """Read-only probe connection. Raises if the file can't be opened ro."""
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=250")
    return conn


def get_shot_checked(
    shot_id: int,
    *,
    db_path: str | Path | None = None,
) -> tuple[CalledShot | None, bool]:
    """One row by id, with an HONEST failure channel: ``(shot, ok)``.

    ``(None, True)`` = the ledger is readable and no such row exists (a
    missing DB file counts — no ledger means genuinely no row).
    ``(None, False)`` = the ledger could NOT be read — callers must render
    "unreadable", never "unknown id". Raises ``KillSwitchDisabled`` when the
    operator turned the feature off.
    """
    kill_switches.requireEnabled("called_shots", caller="get_shot_checked")
    try:
        path = Path(_resolve_db_path(db_path))
        if not path.exists():
            return None, True
        conn = _connect_ro(path)
        try:
            row = conn.execute(
                "SELECT * FROM called_shots WHERE id = ?", (shot_id,)
            ).fetchone()
        finally:
            conn.close()
        return (_row_to_shot(row) if row is not None else None), True
    except Exception as exc:
        print(f"[called_shots] get_shot_checked failed: {exc!r}", flush=True)
        return None, False


def list_open_checked(
    persona_id: str | None = None,
    *,
    db_path: str | Path | None = None,
) -> tuple[list[CalledShot], bool]:
    """Open shots with an HONEST failure channel: ``(shots, ok)``.

    ``([], True)`` = readable-and-empty (a missing DB file counts);
    ``([], False)`` = the ledger could not be read — callers must render
    "unreadable", never "no open bets". Same persona grain as ``list_open``
    (``None`` = all personas, empty string raises). Raises
    ``KillSwitchDisabled`` when the operator turned the feature off.
    """
    kill_switches.requireEnabled("called_shots", caller="list_open_checked")
    persona_id = _normalize_persona(persona_id, allow_none=True)
    try:
        path = Path(_resolve_db_path(db_path))
        if not path.exists():
            return [], True
        conn = _connect_ro(path)
        try:
            if persona_id:
                rows = conn.execute(
                    "SELECT * FROM called_shots WHERE status = 'open' "
                    "AND persona_id = ? ORDER BY id DESC",
                    (persona_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM called_shots WHERE status = 'open' "
                    "ORDER BY id DESC"
                ).fetchall()
        finally:
            conn.close()
        return [_row_to_shot(r) for r in rows], True
    except Exception as exc:
        print(f"[called_shots] list_open_checked failed: {exc!r}", flush=True)
        return [], False


def list_resolved_domains(
    persona_id: str,
    *,
    db_path: str | Path | None = None,
) -> list[str] | None:
    """Distinct non-empty domains with resolved history for one persona.

    ``[]`` = readable, no resolved domains (a missing DB file counts);
    ``None`` = the ledger could not be read. Raises ``KillSwitchDisabled``
    when the operator turned the feature off, ``ValueError`` on an empty
    persona_id.
    """
    kill_switches.requireEnabled("called_shots", caller="list_resolved_domains")
    persona_id = _normalize_persona(persona_id)
    try:
        path = Path(_resolve_db_path(db_path))
        if not path.exists():
            return []
        conn = _connect_ro(path)
        try:
            rows = conn.execute(
                "SELECT DISTINCT domain FROM called_shots "
                "WHERE persona_id = ? AND status = 'resolved' AND domain != ''",
                (persona_id,),
            ).fetchall()
        finally:
            conn.close()
        # strip() filter is defensive (LOW-2): _normalize_domain already
        # strips+casefolds at write, but rows written by any future path
        # must not surface a whitespace-only "domain".
        return [str(r[0]) for r in rows if str(r[0]).strip()]
    except Exception as exc:
        print(f"[called_shots] list_resolved_domains failed: {exc!r}", flush=True)
        return None


# ---------------------------------------------------------------------------
# Vault mirror — derived state, best-effort, never source of truth (Rule 2)
# ---------------------------------------------------------------------------

_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_component(text: str) -> str:
    return _SAFE_COMPONENT_RE.sub("-", text).strip("-") or "unknown"


def _maybe_write_mirror(
    shot: CalledShot,
    *,
    db_path: str | Path | None = None,
) -> None:
    """Write/refresh the human-readable vault note for one shot.

    Own try/except — a mirror failure NEVER fails the ledger write. The note
    is regenerated whole from a FRESH re-read of the row immediately before
    rendering (falling back to the passed ``shot`` if the re-read fails), so
    the record-vs-reconcile interleave collapses to the re-read->replace gap.
    A write landing inside that gap can still leave a briefly stale note —
    and because resolved rows are immutable there is no later ledger write to
    regenerate it — accepted: the table is the source of truth, the mirror is
    a convenience view.
    """
    try:
        from config import get_called_shots_settings

        settings = get_called_shots_settings()
        if not settings.mirror_enabled:
            return
        # Freshness re-read (best-effort): render the CURRENT row, not the
        # possibly-stale snapshot the caller carried across the mirror race.
        try:
            conn = _connect(_resolve_db_path(db_path))
            try:
                row = conn.execute(
                    "SELECT * FROM called_shots WHERE id = ?", (shot.id,)
                ).fetchone()
            finally:
                conn.close()
            if row is not None:
                shot = _row_to_shot(row)
        except Exception:
            pass  # fall back to the passed shot — still fail-open
        mirror_dir = Path(settings.mirror_dir)
        mirror_dir.mkdir(parents=True, exist_ok=True)
        date_part = (shot.created_at or "")[:10] or "undated"
        name = (
            f"{date_part}-{_safe_component(shot.persona_id)}-shot-{shot.id:04d}.md"
        )
        target = mirror_dir / name
        receipts_md = "\n".join(f"- {r}" for r in shot.receipts) or "- (none)"
        outcome_line = shot.outcome or "unresolved"
        # Frontmatter value must be newline-free — a persona_id carrying a
        # newline would otherwise spoof arbitrary frontmatter keys.
        persona_fm = re.sub(r"[\r\n]+", " ", shot.persona_id)
        body = (
            "---\n"
            "tags: [system, memory, called-shots]\n"
            f"status: {shot.status}\n"
            f"date: {date_part}\n"
            f"persona: {persona_fm}\n"
            "---\n\n"
            f"# Called Shot #{shot.id} — {shot.domain or 'general'}\n\n"
            f"**Persona:** {shot.persona_id}\n"
            f"**Staked:** {shot.created_at}\n"
            f"**Decided by:** {shot.decided_by}\n"
            f"**Status:** {shot.status}\n"
            f"**Outcome:** {outcome_line}\n"
            + (f"**Resolved:** {shot.resolved_at}\n" if shot.resolved_at else "")
            + "\n## Operator position\n\n"
            f"{shot.operator_position or '(none)'}\n\n"
            "## Homie position\n\n"
            f"{shot.homie_position or '(none)'}\n\n"
            "## Reasoning\n\n"
            f"{shot.homie_reasoning or '(none)'}\n\n"
            "## Receipts\n\n"
            f"{receipts_md}\n"
        )
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, target)  # atomic on win32 + posix
    except Exception as exc:
        print(f"[called_shots] mirror write failed (non-fatal): {exc!r}", flush=True)
