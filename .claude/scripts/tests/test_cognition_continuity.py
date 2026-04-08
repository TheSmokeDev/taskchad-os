"""Tests for cognition.continuity — continuity state tracking."""

from __future__ import annotations

from pathlib import Path

from cognition.continuity import (
    ContinuityState,
    cleanup_old_continuity,
    load_continuity,
    save_continuity,
    update_continuity_from_turn,
)


def test_empty_state_region():
    state = ContinuityState()
    assert state.to_region_text() == ""


def test_focus_extraction():
    state = ContinuityState()
    state = update_continuity_from_turn(
        state,
        "Let's work on the outreach system refactor",
        "Sure, I'll start by reading the code.",
    )
    assert "outreach system" in state.current_focus


def test_short_message_no_focus():
    """Messages under 30 chars don't update focus."""
    state = ContinuityState(current_focus="previous focus")
    state = update_continuity_from_turn(state, "yes", "OK")
    assert state.current_focus == "previous focus"


def test_question_detection():
    state = ContinuityState()
    state = update_continuity_from_turn(
        state,
        "What's the status of the cancellation pipeline?",
        "The pipeline is processing 51 leads.",
    )
    assert any("cancellation pipeline" in q for q in state.open_loops)


def test_commitment_detection():
    state = ContinuityState()
    state = update_continuity_from_turn(
        state,
        "Check the leads",
        "I'll check the leads database and report back with the numbers",
    )
    assert len(state.pending_commitments) >= 1


def test_decision_detection():
    state = ContinuityState()
    state = update_continuity_from_turn(
        state,
        "Let's go with the JSONL approach for staging data",
        "Sounds good, implementing JSONL now.",
    )
    assert any("JSONL" in d for d in state.recent_decisions)


def test_save_and_load(tmp_path: Path):
    state = ContinuityState(session_id="test:1", current_focus="testing continuity")
    save_continuity(state, tmp_path)
    loaded = load_continuity("test:1", tmp_path)
    assert loaded.current_focus == "testing continuity"
    assert loaded.session_id == "test:1"


def test_load_nonexistent(tmp_path: Path):
    loaded = load_continuity("nonexistent", tmp_path)
    assert loaded.current_focus == ""
    assert loaded.session_id == "nonexistent"


def test_load_corrupted_file(tmp_path: Path):
    """Corrupted JSON returns empty state."""
    filepath = tmp_path / "test_1.json"
    filepath.write_text("not valid json!!!", encoding="utf-8")
    loaded = load_continuity("test:1", tmp_path)
    assert loaded.current_focus == ""


def test_region_text_formatting():
    state = ContinuityState(
        current_focus="Building finance dashboard",
        open_loops=["What about the loan tracker?"],
        pending_commitments=["check the Teller API status"],
        recent_decisions=["using Recharts for charts"],
    )
    text = state.to_region_text()
    assert "Building finance dashboard" in text
    assert "loan tracker" in text
    assert "Teller API" in text
    assert "Recharts" in text


def test_list_capping():
    state = ContinuityState()
    for i in range(20):
        state = update_continuity_from_turn(
            state,
            f"What is question number {i} about something?",
            f"Answer {i} with some text here",
        )
    # Should be capped at 5 (default), not 20
    assert len(state.open_loops) <= 5


def test_turn_count_increments():
    state = ContinuityState()
    assert state.turn_count == 0
    state = update_continuity_from_turn(state, "hello there friend", "hi")
    assert state.turn_count == 1
    state = update_continuity_from_turn(state, "another message here", "response")
    assert state.turn_count == 2


def test_updated_at_set():
    state = ContinuityState()
    assert state.updated_at == ""
    state = update_continuity_from_turn(state, "test message content here", "response")
    assert state.updated_at != ""


def test_cleanup_old_continuity(tmp_path: Path):
    import os
    import time

    # Create a recent file
    recent = tmp_path / "recent.json"
    recent.write_text('{"session_id": "recent"}')

    # Create an old file (fake old mtime)
    old = tmp_path / "old.json"
    old.write_text('{"session_id": "old"}')
    old_time = time.time() - (8 * 86400)  # 8 days ago
    os.utime(old, (old_time, old_time))

    removed = cleanup_old_continuity(tmp_path, max_age_days=7)
    assert removed == 1
    assert recent.exists()
    assert not old.exists()


def test_cleanup_nonexistent_dir(tmp_path: Path):
    """Cleanup of nonexistent dir returns 0."""
    assert cleanup_old_continuity(tmp_path / "nope") == 0


def test_no_duplicate_entries():
    """Same question shouldn't appear twice."""
    state = ContinuityState()
    state = update_continuity_from_turn(
        state,
        "What's the status of the pipeline?",
        "Let me check.",
    )
    state = update_continuity_from_turn(
        state,
        "What's the status of the pipeline?",
        "Still processing.",
    )
    # Count occurrences of the question
    count = sum(1 for q in state.open_loops if "pipeline" in q)
    assert count == 1
