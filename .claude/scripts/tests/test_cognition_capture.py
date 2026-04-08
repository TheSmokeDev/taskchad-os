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
    """Same message twice -> second run deduped."""
    store = StagingStore(tmp_path / "staging.jsonl")
    auto_capture_from_turn("remember X", "OK", store, "s1", 1)
    count1 = store.count()

    auto_capture_from_turn("remember X", "OK", store, "s1", 2)
    count2 = store.count()

    # Second run should not add duplicates
    assert count2 == count1
