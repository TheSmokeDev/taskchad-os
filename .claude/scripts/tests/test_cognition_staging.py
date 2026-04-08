"""Tests for cognition.staging — JSONL staging store."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from cognition.staging import StagingCandidate, StagingStore


def test_append_and_read(tmp_path: Path):
    """Write candidate -> read it back."""
    store = StagingStore(tmp_path / "staging.jsonl")
    candidate = StagingCandidate(
        source_turn="test:1",
        candidate_type="fact",
        observation="The server is running on port 7888",
        dedupe_key="server running port 7888",
        promotion_target="MEMORY.md",
    )

    assert store.append(candidate) is True
    results = store.read_recent(hours=1)
    assert len(results) == 1
    assert results[0].observation == "The server is running on port 7888"
    assert results[0].candidate_type == "fact"


def test_dedup_exact_key(tmp_path: Path):
    """Same dedupe_key -> rejected."""
    store = StagingStore(tmp_path / "staging.jsonl")
    c1 = StagingCandidate(
        source_turn="test:1",
        candidate_type="fact",
        observation="Fact A",
        dedupe_key="fact-a",
        promotion_target="MEMORY.md",
    )
    c2 = StagingCandidate(
        source_turn="test:2",
        candidate_type="fact",
        observation="Fact A again",
        dedupe_key="fact-a",  # Same key
        promotion_target="MEMORY.md",
    )

    assert store.append(c1) is True
    assert store.append(c2) is False  # Rejected
    assert store.count() == 1


def test_empty_dedupe_key_rejected(tmp_path: Path):
    """Empty dedupe_key -> rejected."""
    store = StagingStore(tmp_path / "staging.jsonl")
    c = StagingCandidate(
        source_turn="test:1",
        candidate_type="fact",
        observation="Something",
        dedupe_key="",
        promotion_target="MEMORY.md",
    )
    assert store.append(c) is False


def test_count(tmp_path: Path):
    store = StagingStore(tmp_path / "staging.jsonl")
    assert store.count() == 0

    for i in range(3):
        store.append(StagingCandidate(
            source_turn=f"test:{i}",
            candidate_type="fact",
            observation=f"Fact {i}",
            dedupe_key=f"fact-{i}",
            promotion_target="MEMORY.md",
        ))
    assert store.count() == 3


def test_cleanup_expired(tmp_path: Path):
    """Old entries removed."""
    store = StagingStore(tmp_path / "staging.jsonl")

    # Write an already-expired entry
    expired = StagingCandidate(
        source_turn="test:1",
        candidate_type="fact",
        observation="Old fact",
        dedupe_key="old-fact",
        promotion_target="MEMORY.md",
    )
    expired.decay_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    # Write directly since append would set decay_at fresh
    import json
    from dataclasses import asdict

    with open(tmp_path / "staging.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(expired)) + "\n")

    # Write a fresh entry
    store.append(StagingCandidate(
        source_turn="test:2",
        candidate_type="fact",
        observation="Fresh fact",
        dedupe_key="fresh-fact",
        promotion_target="MEMORY.md",
    ))

    assert store.count() == 2
    removed = store.cleanup_expired()
    assert removed == 1
    assert store.count() == 1


def test_read_recent_filter(tmp_path: Path):
    """Only returns candidates within time window."""
    store = StagingStore(tmp_path / "staging.jsonl")
    store.append(StagingCandidate(
        source_turn="test:1",
        candidate_type="fact",
        observation="Recent fact",
        dedupe_key="recent",
        promotion_target="MEMORY.md",
    ))

    # Should find it within 1 hour
    assert len(store.read_recent(hours=1)) == 1


def test_nonexistent_file(tmp_path: Path):
    """Store handles missing file gracefully."""
    store = StagingStore(tmp_path / "nonexistent.jsonl")
    assert store.count() == 0
    assert store.read_recent() == []
