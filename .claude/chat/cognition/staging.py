"""Typed staging store for auto-captured memory candidates.

Append-only JSONL file with exact-key dedup. Candidates sit here until
the promotion pipeline (Move 2) reviews and graduates them to MEMORY.md,
USER.md, or SELF.md.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

try:
    # Cross-process file lock (fixes #166 Finding 2): every read-modify-write
    # against the JSONL file must be atomic across the live-bot hot path
    # (engine.py append) and the cron promotion/cleanup pass (memory_reflect.py)
    # — they run in different processes. Same self-locking idiom as
    # cognition.proactive_actions._append_lock and cognition.skill_usage's M4
    # RMW lock.
    from shared import atomic_write_text as _atomic_write_text
    from shared import file_lock as _file_lock
except Exception as _lock_import_exc:  # pragma: no cover - optional outside scripts env
    _file_lock = None  # type: ignore[assignment]
    _atomic_write_text = None  # type: ignore[assignment]
    print(f"[staging] shared.file_lock unavailable, operating unlocked: {_lock_import_exc!r}")


# Single owner of the low_evidence reason shape — the defer-vs-reject branch
# (promotion) and the legacy unreject migration (below) both key off it; a
# reworded reason string must not silently flip candidates to permanent
# rejection or no-op the migration.
LOW_EVIDENCE_REASON_PREFIX = "low_evidence"


def is_low_evidence_reason(reason: str | None) -> bool:
    """True when a rejected_reason marks a (deferrable) low-evidence case."""
    return str(reason or "").startswith(LOW_EVIDENCE_REASON_PREFIX)


@dataclass
class StagingCandidate:
    """A single auto-captured memory candidate.

    Merge provenance semantics (deliberate): on a ``dedupe_key`` merge the
    record keeps FIRST-seen provenance (``id``, ``source_turn``, ``source``,
    ``observation``, ``candidate_type``, ``promotion_target``) while
    ``timestamp``/``decay_at`` refresh to LAST-seen and ``evidence_count``/
    ``confidence`` accumulate — so ``read_recent()`` treats an old,
    recently-re-observed row as recent by design.
    """

    id: str = ""
    source_turn: str = ""
    source: str = ""  # explicit | reflection | other producer-owned provenance
    candidate_type: str = ""  # fact | preference | decision | self_model | procedural | entity
    observation: str = ""
    inference: str = ""
    confidence: float = 0.0
    evidence_count: int = 1
    dedupe_key: str = ""
    promotion_target: str = ""  # USER.md | MEMORY.md | SELF.md | skills/generated/
    promoted: bool = False
    promoted_at: str | None = None
    rejected: bool = False
    rejected_reason: str | None = None
    timestamp: str = ""
    decay_at: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = str(uuid.uuid4())
        if not self.timestamp:
            self.timestamp = datetime.now(UTC).isoformat()
        if not self.decay_at:
            decay = datetime.now(UTC) + timedelta(days=30)
            self.decay_at = decay.isoformat()


class StagingStore:
    """JSONL-backed staging store with exact-key dedup."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Guard a read-modify-write against this file with the shared
        cross-process lock. Fail-open: if ``shared.file_lock`` is unavailable
        the operation proceeds unlocked (single-process test contexts).
        """
        if _file_lock is None:
            yield
            return
        with _file_lock(self._path):
            yield

    def _write_all(self, records: list[dict]) -> None:
        """Rewrite the JSONL file with the given records atomically. Caller holds the lock.

        tmp + os.replace so a crash mid-write never leaves a truncated store
        (mirrors cognition.skill_usage's NM1 / shared.atomic_write_text).
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        content = "".join(
            json.dumps(record, ensure_ascii=False) + "\n" for record in records
        )
        if _atomic_write_text is not None:
            _atomic_write_text(self._path, content)
        else:  # pragma: no cover - optional when imported outside scripts env
            self._path.write_text(content, encoding="utf-8")

    def append(self, candidate: StagingCandidate) -> bool:
        """Append candidate to JSONL.

        On a ``dedupe_key`` hit, merges into the existing record instead of
        dropping the observation (fixes #166 Finding 1 — evidence_count could
        never accumulate past 1): increments ``evidence_count``, refreshes
        ``timestamp``, and keeps the higher of the two ``confidence`` values.
        Returns False when merged (no new row written) or when the candidate
        is invalid (empty dedupe_key); True only when a new row was appended.
        """
        if not candidate.dedupe_key:
            return False

        with self._locked():
            records = self._iter_records()
            for record in records:
                if record.get("dedupe_key") == candidate.dedupe_key:
                    record["evidence_count"] = int(record.get("evidence_count", 1)) + 1
                    record["timestamp"] = candidate.timestamp
                    # An actively re-observed candidate earns a fresh expiry —
                    # otherwise slow accumulators keep first-observation+30d
                    # decay and cleanup_expired() can purge them mid-climb.
                    record["decay_at"] = candidate.decay_at
                    record["confidence"] = max(
                        float(record.get("confidence", 0.0)), candidate.confidence
                    )
                    self._write_all(records)
                    return False

            # No existing match — append as a new row
            self._path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(asdict(candidate), ensure_ascii=False) + "\n"
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
            return True

    def read_recent(self, hours: int = 24) -> list[StagingCandidate]:
        """Read candidates from the last N hours.

        Locked so this can never observe a torn intermediate state from a
        concurrent ``_write_all()`` rewrite in another process/thread.
        """
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        cutoff_iso = cutoff.isoformat()

        with self._locked():
            candidates = []
            for record in self._iter_records():
                if record.get("timestamp", "") >= cutoff_iso:
                    candidates.append(StagingCandidate(**record))

            return candidates

    def count(self) -> int:
        """Total candidates in store.

        Locked so this can never observe a torn intermediate state from a
        concurrent ``_write_all()`` rewrite in another process/thread.
        """
        with self._locked():
            return sum(1 for _ in self._iter_records())

    def cleanup_expired(self) -> int:
        """Remove candidates past decay_at. Returns count removed."""
        now_iso = datetime.now(UTC).isoformat()

        with self._locked():
            kept: list[dict] = []
            removed = 0

            for record in self._iter_records():
                decay = record.get("decay_at", "")
                if decay and decay < now_iso:
                    removed += 1
                else:
                    kept.append(record)

            if removed > 0:
                self._write_all(kept)

            return removed

    def _iter_records(self) -> list[dict]:
        """Read all JSONL records."""
        if not self._path.exists():
            return []

        records = []
        try:
            with open(self._path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except Exception:
            return []

        return records

    def read_unpromoted(self) -> list[StagingCandidate]:
        """Read candidates not yet promoted or rejected.

        Locked so this can never observe a torn intermediate state from a
        concurrent ``_write_all()`` rewrite in another process/thread (e.g.
        the cron promotion pass racing the live-bot ``append()`` hot path).
        """
        with self._locked():
            candidates = []
            for record in self._iter_records():
                if not record.get("promoted") and not record.get("rejected"):
                    candidates.append(StagingCandidate(**record))
            return candidates

    def mark_promoted(self, candidate_id: str, target: str) -> bool:
        """Mark candidate as promoted. Rewrites JSONL."""
        return self._update_record(candidate_id, {
            "promoted": True,
            "promoted_at": datetime.now(UTC).isoformat(),
            "promotion_target": target,
        })

    def mark_rejected(self, candidate_id: str, reason: str) -> bool:
        """Mark candidate as rejected. Rewrites JSONL."""
        return self._update_record(candidate_id, {
            "rejected": True,
            "rejected_reason": reason,
        })

    def unreject_low_evidence(self) -> int:
        """Flip legacy ``low_evidence`` rejections back to pending.

        Before the evidence-merge fix (#166), append() silently dropped
        repeat observations instead of incrementing evidence_count, so every
        fact/preference/decision/entity candidate failed the promotion floor
        and got permanently mark_rejected(reason="low_evidence ..."). Those
        rows are stuck; flip them back to unpromoted so they re-enter
        run_promotion_pipeline and can accumulate evidence going forward.
        Idempotent — a second run finds nothing left to flip.
        """
        with self._locked():
            records = self._iter_records()
            flipped = 0
            for record in records:
                reason = str(record.get("rejected_reason") or "")
                if record.get("rejected") and is_low_evidence_reason(reason):
                    record["rejected"] = False
                    record["rejected_reason"] = None
                    flipped += 1
            if flipped > 0:
                self._write_all(records)
            return flipped

    def _update_record(self, candidate_id: str, updates: dict) -> bool:
        """Update a record by ID. Rewrites file. Returns True if found.

        Acquires this store's own cross-process lock (see ``_locked``) around
        the read-modify-write — callers do not need to hold one themselves.
        """
        with self._locked():
            records = self._iter_records()
            found = False
            for record in records:
                if record.get("id") == candidate_id:
                    record.update(updates)
                    found = True
            if found:
                self._write_all(records)
            return found
