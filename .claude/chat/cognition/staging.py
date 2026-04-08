"""Typed staging store for auto-captured memory candidates.

Append-only JSONL file with exact-key dedup. Candidates sit here until
the promotion pipeline (Move 2) reviews and graduates them to MEMORY.md,
USER.md, or SELF.md.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path


@dataclass
class StagingCandidate:
    """A single auto-captured memory candidate."""

    id: str = ""
    source_turn: str = ""
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

    def append(self, candidate: StagingCandidate) -> bool:
        """Append candidate to JSONL. Returns False if dedup rejects it."""
        if not candidate.dedupe_key:
            return False

        # Check dedup against recent entries
        existing_keys = self._load_dedupe_keys()
        if candidate.dedupe_key in existing_keys:
            return False

        # Ensure parent directory exists
        self._path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic append
        line = json.dumps(asdict(candidate), ensure_ascii=False) + "\n"
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()

        return True

    def read_recent(self, hours: int = 24) -> list[StagingCandidate]:
        """Read candidates from the last N hours."""
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        cutoff_iso = cutoff.isoformat()

        candidates = []
        for record in self._iter_records():
            if record.get("timestamp", "") >= cutoff_iso:
                candidates.append(StagingCandidate(**record))

        return candidates

    def count(self) -> int:
        """Total candidates in store."""
        return sum(1 for _ in self._iter_records())

    def cleanup_expired(self) -> int:
        """Remove candidates past decay_at. Returns count removed."""
        now_iso = datetime.now(UTC).isoformat()
        kept: list[dict] = []
        removed = 0

        for record in self._iter_records():
            decay = record.get("decay_at", "")
            if decay and decay < now_iso:
                removed += 1
            else:
                kept.append(record)

        if removed > 0:
            # Rewrite file with non-expired entries
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                for record in kept:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")

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
        """Read candidates not yet promoted or rejected."""
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

    def _update_record(self, candidate_id: str, updates: dict) -> bool:
        """Update a record by ID. Rewrites file. Returns True if found.

        Pattern: cleanup_expired() — read all, modify, rewrite.
        Safe because callers hold file_lock() from shared.py.
        """
        records = self._iter_records()
        found = False
        for record in records:
            if record.get("id") == candidate_id:
                record.update(updates)
                found = True
        if found:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                for record in records:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return found

    def _load_dedupe_keys(self) -> set[str]:
        """Load all dedupe_key values from the store."""
        keys: set[str] = set()
        for record in self._iter_records():
            key = record.get("dedupe_key", "")
            if key:
                keys.add(key)
        return keys
