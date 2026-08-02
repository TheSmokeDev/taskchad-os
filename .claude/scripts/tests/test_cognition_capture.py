"""Tests for cognition.capture — auto-capture with regex triggers."""

from __future__ import annotations

from pathlib import Path

from cognition.capture import auto_capture_from_turn, extract_candidates
from cognition.staging import StagingStore


def test_extract_fact_remember():
    """'remember to...' triggers fact capture."""
    candidates = extract_candidates(
        "remember to update the DNS records",
        "OK, I'll keep that in mind.",
    )
    assert len(candidates) >= 1
    assert candidates[0].candidate_type == "fact"
    assert "DNS" in candidates[0].observation


def test_extract_preference():
    """'I prefer...' triggers preference capture."""
    candidates = extract_candidates(
        "I prefer concise answers over long explanations",
        "Got it.",
    )
    assert len(candidates) >= 1
    assert candidates[0].candidate_type == "preference"


def test_extract_decision():
    """'decided' triggers decision capture."""
    candidates = extract_candidates(
        "we decided to use Supabase for the database",
        "Noted.",
    )
    assert len(candidates) >= 1
    assert candidates[0].candidate_type == "decision"


def test_extract_entity_email():
    """Email address triggers entity capture."""
    candidates = extract_candidates(
        "email me at test@example.com for details",
        "OK.",
    )
    assert len(candidates) >= 1
    entity_types = [c.candidate_type for c in candidates]
    assert "entity" in entity_types


def test_extract_entity_phone():
    """Phone number triggers entity capture."""
    candidates = extract_candidates(
        "call me at +18555994167",
        "OK.",
    )
    assert len(candidates) >= 1
    entity_types = [c.candidate_type for c in candidates]
    assert "entity" in entity_types


def test_max_captures():
    """Many triggers -> capped at 3."""
    text = (
        "remember the DNS, I prefer fast responses, "
        "we decided to use Redis, email test@example.com, "
        "also remember the port number, I always want concise answers"
    )
    candidates = extract_candidates(text, "OK.")
    assert len(candidates) <= 3


def test_length_filter_too_short():
    """Short text -> no candidates."""
    candidates = extract_candidates("ok", "yes")
    assert len(candidates) == 0


def test_no_system_markup():
    """System markup in content -> rejected."""
    candidates = extract_candidates(
        '<recalled-memory>remember this</recalled-memory>',
        "OK.",
    )
    # The matched observation containing system markup should be filtered
    for c in candidates:
        assert "<recalled-memory>" not in c.observation


def test_auto_capture_integration(tmp_path: Path):
    """Full auto_capture_from_turn writes to staging."""
    store = StagingStore(tmp_path / "staging.jsonl")
    written = auto_capture_from_turn(
        "remember to deploy on Friday",
        "Got it, I'll remind you.",
        store,
        session_id="test",
        turn_number=1,
    )
    assert written >= 1
    assert store.count() >= 1


def test_auto_capture_dedup(tmp_path: Path):
    """Same message twice -> second run merges evidence instead of duplicating (#166)."""
    store = StagingStore(tmp_path / "staging.jsonl")
    auto_capture_from_turn("remember X", "OK", store, "s1", 1)
    count1 = store.count()

    auto_capture_from_turn("remember X", "OK", store, "s1", 2)
    count2 = store.count()

    # Second run should not add duplicates
    assert count2 == count1

    unpromoted = store.read_unpromoted()
    assert len(unpromoted) == 1
    assert unpromoted[0].evidence_count == 2


def test_single_utterance_cannot_self_corroborate(tmp_path: Path):
    """Codex gate BLOCKER on PR #176: one sentence firing multiple trigger
    types (fact via 'remember' + decision via 'decided') must count as ONE
    evidence unit, not self-corroborate to evidence_count=2 and clear the
    promotion floor from a single message.
    """
    text = "remember we decided to use port 7888 for the relay server"
    candidates = extract_candidates(text, "Noted.")
    keys = [c.dedupe_key for c in candidates]
    # Precondition: the vector is real — >=2 candidates sharing a dedupe_key.
    assert len(keys) >= 2 and len(set(keys)) < len(keys), (
        f"expected same-key multi-trigger candidates, got {candidates!r}"
    )

    store = StagingStore(tmp_path / "staging.jsonl")
    auto_capture_from_turn(text, "Noted.", store, "s1", 1)
    rows = store.read_unpromoted()
    assert len(rows) == 1
    assert rows[0].evidence_count == 1


def test_cross_turn_reobservation_still_accumulates(tmp_path: Path):
    """Sibling guard: the batch dedupe must NOT block cross-turn accumulation —
    the same multi-trigger sentence in two separate turns reaches
    evidence_count=2 (the floor works as designed).
    """
    text = "remember we decided to use port 7888 for the relay server"
    store = StagingStore(tmp_path / "staging.jsonl")
    auto_capture_from_turn(text, "Noted.", store, "s1", 1)
    auto_capture_from_turn(text, "Noted again.", store, "s1", 2)
    rows = store.read_unpromoted()
    assert len(rows) == 1
    assert rows[0].evidence_count == 2
