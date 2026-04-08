"""Tests for cognition.promotion — promotion pipeline."""

from __future__ import annotations

from pathlib import Path

from cognition.promotion import _is_duplicate, _passes_quality_gate, _rejection_reason
from cognition.staging import StagingCandidate, StagingStore


def _make_candidate(**kwargs) -> StagingCandidate:
    """Helper to create a StagingCandidate with defaults."""
    defaults = {
        "source_turn": "test:1",
        "candidate_type": "fact",
        "observation": "Test observation",
        "dedupe_key": "test-key",
        "promotion_target": "MEMORY.md",
        "confidence": 0.8,
        "evidence_count": 3,
    }
    defaults.update(kwargs)
    return StagingCandidate(**defaults)


def test_quality_gate_passes():
    c = _make_candidate(confidence=0.8, evidence_count=3)
    assert _passes_quality_gate(c) is True


def test_quality_gate_low_confidence():
    c = _make_candidate(confidence=0.5, evidence_count=3)
    assert _passes_quality_gate(c) is False


def test_quality_gate_low_evidence():
    c = _make_candidate(confidence=0.9, evidence_count=1)
    assert _passes_quality_gate(c) is False


def test_quality_gate_both_low():
    c = _make_candidate(confidence=0.3, evidence_count=0)
    assert _passes_quality_gate(c) is False


def test_quality_gate_threshold_exact():
    """Exactly at threshold should pass."""
    c = _make_candidate(confidence=0.7, evidence_count=2)
    assert _passes_quality_gate(c) is True


def test_rejection_reason_confidence():
    c = _make_candidate(confidence=0.3, evidence_count=5)
    assert "low_confidence" in _rejection_reason(c)


def test_rejection_reason_evidence():
    c = _make_candidate(confidence=0.9, evidence_count=1)
    assert "low_evidence" in _rejection_reason(c)


def test_is_duplicate_exact():
    assert _is_duplicate("Server runs on port 7888", "...Server runs on port 7888...") is True


def test_is_duplicate_case_insensitive():
    assert _is_duplicate("HELLO WORLD", "hello world is here") is True


def test_is_not_duplicate():
    assert _is_duplicate("New unique fact", "Existing content here") is False


def test_is_duplicate_empty_text():
    """Empty text is considered duplicate (no-op)."""
    assert _is_duplicate("", "any content") is True
    assert _is_duplicate("   ", "any content") is True


def test_staging_mark_promoted(tmp_path: Path):
    store = StagingStore(tmp_path / "staging.jsonl")
    c = _make_candidate(dedupe_key="promo-test")
    store.append(c)

    unpromoted = store.read_unpromoted()
    assert len(unpromoted) == 1

    cid = unpromoted[0].id
    assert store.mark_promoted(cid, "MEMORY.md") is True
    assert len(store.read_unpromoted()) == 0


def test_staging_mark_rejected(tmp_path: Path):
    store = StagingStore(tmp_path / "staging.jsonl")
    c = _make_candidate(dedupe_key="reject-test")
    store.append(c)

    unpromoted = store.read_unpromoted()
    cid = unpromoted[0].id
    assert store.mark_rejected(cid, "low_confidence") is True
    assert len(store.read_unpromoted()) == 0


def test_staging_read_unpromoted(tmp_path: Path):
    store = StagingStore(tmp_path / "staging.jsonl")
    for i in range(3):
        store.append(_make_candidate(
            dedupe_key=f"fact-{i}",
            observation=f"Fact {i}",
        ))
    assert len(store.read_unpromoted()) == 3

    cid = store.read_unpromoted()[0].id
    store.mark_rejected(cid, "test rejection")
    assert len(store.read_unpromoted()) == 2


def test_mark_nonexistent_id(tmp_path: Path):
    store = StagingStore(tmp_path / "staging.jsonl")
    store.append(_make_candidate(dedupe_key="exists"))
    assert store.mark_promoted("nonexistent-id", "MEMORY.md") is False


def test_empty_staging_read_unpromoted(tmp_path: Path):
    store = StagingStore(tmp_path / "staging.jsonl")
    assert store.read_unpromoted() == []
