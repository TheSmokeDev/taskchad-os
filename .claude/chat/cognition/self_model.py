"""Inference confidence tracking for user-model self-awareness.

Tracks inferences about the user with confidence scores that decay
over time if not reinforced and strengthen when confirmed. Runs
during daily reflection (same schedule as promotion).

Pattern: continuity.py — dataclass + JSON persistence with load/save.
Pattern: staging.py — file rewrite for updates.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4


@dataclass
class InferenceRecord:
    """A single user-model inference with confidence tracking."""

    id: str
    inference: str
    observation: str
    confidence: float
    evidence_count: int = 1
    contradiction_count: int = 0
    first_seen: str = ""
    last_updated: str = ""
    source: str = "auto_capture"  # auto_capture | explicit | reflection
    status: str = "active"  # active | decayed | confirmed


def _similar(a: str, b: str) -> bool:
    """Check if two inference strings are semantically similar (normalized match)."""
    norm_a = re.sub(r"\s+", " ", a.strip().lower())
    norm_b = re.sub(r"\s+", " ", b.strip().lower())
    return norm_a == norm_b


class InferenceTracker:
    """JSON-backed inference confidence tracker."""

    def __init__(self, state_file: Path) -> None:
        self._path = state_file

    def load(self) -> list[InferenceRecord]:
        """Load all inferences from state file."""
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [InferenceRecord(**r) for r in data]
        except (json.JSONDecodeError, TypeError, KeyError):
            pass
        return []

    def save(self, records: list[InferenceRecord]) -> None:
        """Write all inferences to state file."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps([asdict(r) for r in records], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def add_inference(
        self,
        inference: str,
        observation: str,
        confidence: float,
        source: str = "auto_capture",
    ) -> InferenceRecord:
        """Add new inference or strengthen existing one if similar."""
        confirm_boost = 0.1
        try:
            from config import INFERENCE_CONFIRM_BOOST

            confirm_boost = INFERENCE_CONFIRM_BOOST
        except ImportError:
            pass

        records = self.load()
        now_iso = datetime.now(UTC).isoformat()

        # Check for similar existing inference
        for r in records:
            if _similar(r.inference, inference):
                r.confidence = min(1.0, r.confidence + confirm_boost)
                r.evidence_count += 1
                r.last_updated = now_iso
                if r.evidence_count >= 3:
                    r.status = "confirmed"
                self.save(records)
                return r

        # New inference
        record = InferenceRecord(
            id=str(uuid4()),
            inference=inference,
            observation=observation,
            confidence=confidence,
            evidence_count=1,
            first_seen=now_iso,
            last_updated=now_iso,
            source=source,
            status="active",
        )
        records.append(record)
        self.save(records)
        return record

    def decay_old_inferences(
        self,
        decay_days: int = 14,
        decay_rate: float = 0.05,
        min_confidence: float = 0.3,
    ) -> int:
        """Decay inferences not updated in decay_days. Returns count decayed."""
        records = self.load()
        cutoff = datetime.now(UTC) - timedelta(days=decay_days)
        cutoff_iso = cutoff.isoformat()
        decayed = 0

        for r in records:
            if r.status == "active" and r.last_updated and r.last_updated < cutoff_iso:
                old_confidence = r.confidence
                r.confidence = max(min_confidence, r.confidence - decay_rate)
                if r.confidence <= min_confidence:
                    r.status = "decayed"
                if r.confidence != old_confidence:
                    decayed += 1

        if decayed > 0:
            self.save(records)
        return decayed

    def contradict(self, inference_id: str) -> bool:
        """Record a contradiction. Lowers confidence; demotes confirmed beliefs."""
        records = self.load()
        for r in records:
            if r.id == inference_id:
                r.contradiction_count += 1
                r.confidence = max(0.1, r.confidence - 0.15)
                if r.status == "confirmed" and r.confidence < 0.7:
                    r.status = "active"
                r.last_updated = datetime.now(UTC).isoformat()
                self.save(records)
                return True
        return False

    def get_active(self, min_confidence: float = 0.3) -> list[InferenceRecord]:
        """Return active inferences above min_confidence threshold."""
        return [
            r for r in self.load()
            if r.status != "decayed" and r.confidence >= min_confidence
        ]
