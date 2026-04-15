"""Tests for cognition.self_model — inference tracking, decay, strengthen."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from cognition.self_model import InferenceRecord, InferenceTracker


# === InferenceRecord tests ===


def test_inference_record_defaults():
    r = InferenceRecord(id="1", inference="test", observation="obs", confidence=0.7)
    assert r.evidence_count == 1
    assert r.contradiction_count == 0
    assert r.status == "active"
    assert r.source == "auto_capture"


# === Add inference tests ===


def test_add_inference(tmp_path):
    tracker = InferenceTracker(tmp_path / "inferences.json")
    record = tracker.add_inference("User prefers concise", "rejected verbose", 0.7)
    assert record.confidence == 0.7
    assert record.status == "active"
    assert record.evidence_count == 1
    assert record.id  # UUID assigned


def test_add_inference_persists(tmp_path):
    path = tmp_path / "inferences.json"
    tracker = InferenceTracker(path)
    tracker.add_inference("test inference", "obs", 0.5)
    assert path.exists()

    # Reload from file
    tracker2 = InferenceTracker(path)
    records = tracker2.load()
    assert len(records) == 1
    assert records[0].inference == "test inference"


def test_add_multiple_inferences(tmp_path):
    tracker = InferenceTracker(tmp_path / "inf.json")
    tracker.add_inference("pref A", "obs A", 0.6)
    tracker.add_inference("pref B", "obs B", 0.7)
    records = tracker.load()
    assert len(records) == 2


# === Strengthen tests ===


def test_strengthen_existing(tmp_path):
    tracker = InferenceTracker(tmp_path / "inf.json")
    tracker.add_inference("prefers concise", "obs1", 0.7)
    r2 = tracker.add_inference("prefers concise", "obs2", 0.7)
    assert r2.evidence_count == 2
    assert r2.confidence > 0.7  # Boosted by INFERENCE_CONFIRM_BOOST


def test_strengthen_three_times_confirms(tmp_path):
    tracker = InferenceTracker(tmp_path / "inf.json")
    tracker.add_inference("likes dark mode", "obs1", 0.7)
    tracker.add_inference("likes dark mode", "obs2", 0.7)
    r3 = tracker.add_inference("likes dark mode", "obs3", 0.7)
    assert r3.evidence_count == 3
    assert r3.status == "confirmed"


def test_strengthen_does_not_exceed_max(tmp_path):
    tracker = InferenceTracker(tmp_path / "inf.json")
    for _ in range(20):
        tracker.add_inference("always true", "obs", 0.95)
    records = tracker.load()
    assert records[0].confidence <= 1.0


# === Decay tests ===


def test_decay_old_inference(tmp_path):
    tracker = InferenceTracker(tmp_path / "inf.json")
    record = tracker.add_inference("old pref", "obs", 0.7)
    # Manually set last_updated to 30 days ago
    records = tracker.load()
    records[0].last_updated = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    tracker.save(records)

    decayed_count = tracker.decay_old_inferences(decay_days=14, decay_rate=0.05)
    assert decayed_count == 1

    records = tracker.load()
    assert records[0].confidence < 0.7


def test_decay_does_not_affect_recent(tmp_path):
    tracker = InferenceTracker(tmp_path / "inf.json")
    tracker.add_inference("recent pref", "obs", 0.7)
    decayed_count = tracker.decay_old_inferences(decay_days=14)
    assert decayed_count == 0


def test_decayed_status_below_threshold(tmp_path):
    tracker = InferenceTracker(tmp_path / "inf.json")
    tracker.add_inference("weak pref", "obs", 0.35)  # Just above threshold
    records = tracker.load()
    records[0].last_updated = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    tracker.save(records)

    tracker.decay_old_inferences(decay_days=14, decay_rate=0.05, min_confidence=0.3)
    records = tracker.load()
    assert records[0].status == "decayed"
    assert records[0].confidence == 0.3


def test_decay_confirmed_skipped(tmp_path):
    """Confirmed inferences are not subject to decay (status != 'active')."""
    tracker = InferenceTracker(tmp_path / "inf.json")
    tracker.add_inference("solid pref", "obs1", 0.7)
    tracker.add_inference("solid pref", "obs2", 0.7)
    tracker.add_inference("solid pref", "obs3", 0.7)  # Now confirmed

    records = tracker.load()
    records[0].last_updated = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    tracker.save(records)

    decayed = tracker.decay_old_inferences(decay_days=14)
    assert decayed == 0


# === Contradict tests ===


def test_contradict(tmp_path):
    tracker = InferenceTracker(tmp_path / "inf.json")
    r = tracker.add_inference("user likes X", "obs", 0.7)
    ok = tracker.contradict(r.id)
    assert ok is True

    records = tracker.load()
    assert records[0].contradiction_count == 1
    assert records[0].confidence < 0.7


def test_contradict_nonexistent(tmp_path):
    tracker = InferenceTracker(tmp_path / "inf.json")
    ok = tracker.contradict("fake-id")
    assert ok is False


def test_contradict_does_not_go_below_min(tmp_path):
    tracker = InferenceTracker(tmp_path / "inf.json")
    r = tracker.add_inference("fragile pref", "obs", 0.2)
    tracker.contradict(r.id)
    records = tracker.load()
    assert records[0].confidence >= 0.1


def test_contradict_demotes_confirmed_when_confidence_drops_below_threshold(tmp_path):
    """Confirmed at 0.8 → contradict → 0.65 → status becomes 'active'."""
    tracker = InferenceTracker(tmp_path / "inf.json")
    r = tracker.add_inference("user-belief", "obs", 0.8)
    records = tracker.load()
    records[0].status = "confirmed"
    tracker.save(records)

    assert tracker.contradict(r.id) is True
    updated = tracker.load()[0]
    assert abs(updated.confidence - 0.65) < 1e-9
    assert updated.status == "active"


def test_contradict_preserves_confirmed_when_confidence_stays_high(tmp_path):
    """Confirmed at 1.0 → contradict → 0.85 → stays 'confirmed' (>= 0.7)."""
    tracker = InferenceTracker(tmp_path / "inf.json")
    r = tracker.add_inference("strong-belief", "obs", 1.0)
    records = tracker.load()
    records[0].status = "confirmed"
    tracker.save(records)

    assert tracker.contradict(r.id) is True
    updated = tracker.load()[0]
    assert abs(updated.confidence - 0.85) < 1e-9
    assert updated.status == "confirmed"


def test_contradict_does_not_promote_active_records(tmp_path):
    """Active records stay active — contradict must never promote to confirmed."""
    tracker = InferenceTracker(tmp_path / "inf.json")
    r = tracker.add_inference("fresh-guess", "obs", 0.9)
    # add_inference creates with status="active"; verify precondition
    assert tracker.load()[0].status == "active"

    assert tracker.contradict(r.id) is True
    updated = tracker.load()[0]
    assert updated.status == "active"
    assert abs(updated.confidence - 0.75) < 1e-9


# === get_active tests ===


def test_get_active_filters_decayed(tmp_path):
    tracker = InferenceTracker(tmp_path / "inf.json")
    tracker.add_inference("active one", "obs", 0.8)
    tracker.add_inference("weak one", "obs", 0.35)

    # Decay the weak one
    records = tracker.load()
    records[1].last_updated = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    tracker.save(records)
    tracker.decay_old_inferences(decay_days=14, decay_rate=0.05, min_confidence=0.3)

    active = tracker.get_active(min_confidence=0.3)
    # Decayed record should be filtered out
    assert all(r.status != "decayed" for r in active)
    assert len(active) >= 1


def test_get_active_respects_confidence(tmp_path):
    tracker = InferenceTracker(tmp_path / "inf.json")
    tracker.add_inference("low conf", "obs", 0.2)
    tracker.add_inference("high conf", "obs", 0.9)

    active = tracker.get_active(min_confidence=0.5)
    assert len(active) == 1
    assert active[0].confidence == 0.9


# === Empty/corrupt state tests ===


def test_empty_state_file(tmp_path):
    tracker = InferenceTracker(tmp_path / "empty.json")
    assert tracker.load() == []
    assert tracker.get_active() == []


def test_corrupt_state_file(tmp_path):
    path = tmp_path / "corrupt.json"
    path.write_text("not json at all", encoding="utf-8")
    tracker = InferenceTracker(path)
    assert tracker.load() == []
