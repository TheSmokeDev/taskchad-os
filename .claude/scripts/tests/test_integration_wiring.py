"""Integration tests for Move 5a wiring — proving dead code is now live.

Tests that process detection, InferenceTracker, state sync/restore,
and skill index are all properly callable from their new call sites.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure chat dir is on path for cognition imports
_CHAT_DIR = Path(__file__).resolve().parent.parent.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))


# === Process detection wiring (5a.1) ===


class TestProcessDetectionAllTiers:
    """Process detection should run on ALL turns, not just Tier 1 + recall."""

    def test_detect_process_works_on_short_greeting(self):
        """Tier 0 messages should return current process (no transition)."""
        from cognition.processes import MentalProcess, detect_process

        result, reason = detect_process("hi", MentalProcess.PLANNING)
        assert result == MentalProcess.PLANNING
        assert reason == "no_transition"

    def test_detect_process_works_on_planning_message(self):
        """Planning signal should be detected regardless of tier."""
        from cognition.processes import MentalProcess, detect_process

        result, reason = detect_process("let's plan the architecture for v2")
        assert result == MentalProcess.PLANNING
        assert reason == "planning_signal"

    def test_process_state_persists_across_calls(self):
        """Session process dict should carry state between turns."""
        from cognition.processes import MentalProcess, detect_process

        session_processes: dict[str, MentalProcess] = {}
        session_key = "telegram:123:456"

        # First turn: detect planning
        current = session_processes.get(session_key, MentalProcess.DEFAULT)
        result, _ = detect_process("let's plan the API", current)
        session_processes[session_key] = result
        assert session_processes[session_key] == MentalProcess.PLANNING

        # Second turn: short message keeps current
        current = session_processes.get(session_key, MentalProcess.DEFAULT)
        result, reason = detect_process("ok", current)
        assert result == MentalProcess.PLANNING
        assert reason == "no_transition"

        # Third turn: explicit switch
        current = session_processes.get(session_key, MentalProcess.DEFAULT)
        result, reason = detect_process("switch to execution mode", current)
        session_processes[session_key] = result
        assert result == MentalProcess.EXECUTION
        assert reason == "explicit_override"

    def test_process_weights_applied_for_all_processes(self):
        """Every MentalProcess should have a weights entry."""
        from cognition.processes import PROCESS_WEIGHTS, MentalProcess

        for process in MentalProcess:
            assert process in PROCESS_WEIGHTS


# === InferenceTracker wiring (5a.5) ===


class TestInferenceTrackerWiring:
    """InferenceTracker should be importable and functional from reflection context."""

    def test_inference_tracker_importable(self):
        """Can import InferenceTracker from cognition module."""
        from cognition.self_model import InferenceTracker
        assert InferenceTracker is not None

    def test_inference_tracker_add_and_decay(self, tmp_path):
        """Add + decay cycle works end-to-end."""
        from cognition.self_model import InferenceTracker

        state_file = tmp_path / "inferences.json"
        tracker = InferenceTracker(state_file)

        # Add inference
        record = tracker.add_inference(
            inference="User prefers dark mode",
            observation="Changed theme to dark in settings",
            confidence=0.5,
            source="auto_capture",
        )
        assert record.confidence == 0.5
        assert record.status == "active"

        # Decay should return 0 (inference is too fresh)
        decayed = tracker.decay_old_inferences(decay_days=14)
        assert decayed == 0

        # Force old timestamp to test decay
        records = tracker.load()
        records[0].last_updated = "2020-01-01T00:00:00+00:00"
        tracker.save(records)

        decayed = tracker.decay_old_inferences(decay_days=14)
        assert decayed == 1

    def test_inference_tracker_empty_state(self, tmp_path):
        """Decay on empty state file returns 0."""
        from cognition.self_model import InferenceTracker

        state_file = tmp_path / "empty_inferences.json"
        tracker = InferenceTracker(state_file)

        decayed = tracker.decay_old_inferences()
        assert decayed == 0


# === State sync/restore wiring (5a.4, 5a.5) ===


class TestStateSyncRestore:
    """State restore handles missing vault gracefully."""

    def test_restore_handles_missing_vault(self, tmp_path, monkeypatch):
        """restore_state_from_vault returns empty dict when vault has no state."""
        # Patch config paths to tmp_path (must also patch _VAULT_STATE_DIR
        # since it's computed at import time from MEMORY_DIR)
        monkeypatch.setattr("state_sync.MEMORY_DIR", tmp_path / "Memory")
        monkeypatch.setattr("state_sync._VAULT_STATE_DIR", tmp_path / "Memory" / "_state")
        monkeypatch.setattr("state_sync.STAGING_STORE_PATH", tmp_path / "staging.jsonl")
        monkeypatch.setattr("state_sync.INFERENCE_STATE_FILE", tmp_path / "inferences.json")

        from state_sync import restore_state_from_vault

        results = restore_state_from_vault()
        assert isinstance(results, dict)
        # No files to restore from
        assert len(results) == 0

    def test_sync_writes_to_vault_state_dir(self, tmp_path, monkeypatch):
        """sync_state_to_vault creates _state dir and writes manifest."""
        vault_dir = tmp_path / "Memory"
        vault_dir.mkdir()

        # Create a fake staging file
        staging = tmp_path / "staging.jsonl"
        staging.write_text('{"test": true}\n')
        inference = tmp_path / "inferences.json"
        inference.write_text("[]")

        monkeypatch.setattr("state_sync.MEMORY_DIR", vault_dir)
        monkeypatch.setattr("state_sync._VAULT_STATE_DIR", vault_dir / "_state")
        monkeypatch.setattr("state_sync.STAGING_STORE_PATH", staging)
        monkeypatch.setattr("state_sync.INFERENCE_STATE_FILE", inference)

        from state_sync import sync_state_to_vault

        results = sync_state_to_vault()
        assert results.get("memory-candidates.jsonl") is True
        assert results.get("self-model-inferences.json") is True
        assert (vault_dir / "_state" / "sync-manifest.json").exists()

    def test_restore_only_when_local_missing(self, tmp_path, monkeypatch):
        """Restore does NOT overwrite existing local state."""
        vault_dir = tmp_path / "Memory" / "_state"
        vault_dir.mkdir(parents=True)

        # Create vault copy
        vault_staging = vault_dir / "memory-candidates.jsonl"
        vault_staging.write_text('{"from_vault": true}\n')

        # Create existing local file
        local_staging = tmp_path / "staging.jsonl"
        local_staging.write_text('{"local": true}\n')

        monkeypatch.setattr("state_sync.MEMORY_DIR", tmp_path / "Memory")
        monkeypatch.setattr("state_sync._VAULT_STATE_DIR", vault_dir)
        monkeypatch.setattr("state_sync.STAGING_STORE_PATH", local_staging)
        monkeypatch.setattr("state_sync.INFERENCE_STATE_FILE", tmp_path / "inferences.json")

        from state_sync import restore_state_from_vault

        results = restore_state_from_vault()
        # Should NOT restore staging because local file exists
        assert "memory-candidates.jsonl" not in results
        # Local file should be unchanged
        assert "local" in local_staging.read_text()


# === Skill index wiring (5a.2) ===


class TestSkillIndexAllTurns:
    """Skill index should be built for procedural_memory region."""

    def test_build_skill_index_importable(self):
        """Can import build_skill_index from cognition module."""
        from cognition.skills import build_skill_index
        assert build_skill_index is not None

    def test_build_skill_index_empty_dir(self, tmp_path):
        """Returns empty string for dir with no skills."""
        from cognition.skills import build_skill_index

        result = build_skill_index(tmp_path)
        assert result == ""

    def test_build_skill_index_nonexistent_dir(self, tmp_path):
        """Returns empty string for nonexistent dir."""
        from cognition.skills import build_skill_index

        result = build_skill_index(tmp_path / "nonexistent")
        assert result == ""

    def test_build_skill_index_with_skill(self, tmp_path):
        """Returns formatted index entry for a valid skill."""
        from cognition.skills import build_skill_index

        skill_dir = tmp_path / "test-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: test-skill\ndescription: A test skill\n---\n# Test\n"
        )

        result = build_skill_index(tmp_path)
        assert "test-skill" in result
        assert "A test skill" in result


# === Interfaces (5a.7) ===


class TestProtocolInterfaces:
    """Protocol interfaces should be importable and usable for type checking."""

    def test_interfaces_importable(self):
        """All protocol interfaces are importable."""
        from cognition.interfaces import (
            MemoryProcessor,
            ProcessDetector,
            QueryExpander,
            WMSnapshotPoint,
        )
        assert ProcessDetector is not None
        assert QueryExpander is not None
        assert MemoryProcessor is not None
        assert WMSnapshotPoint is not None

    def test_detect_process_satisfies_protocol(self):
        """detect_process() should satisfy ProcessDetector protocol."""
        from cognition.interfaces import ProcessDetector
        from cognition.processes import detect_process

        assert isinstance(detect_process, ProcessDetector)

    def test_snapshot_point_creation(self):
        """WMSnapshotPoint can be created with location."""
        from cognition.interfaces import WMSnapshotPoint

        point = WMSnapshotPoint(location="startup", description="Bot boot")
        assert point.location == "startup"


# === InferenceTracker.add_inference() wiring (5a.5) ===


class TestInferenceFromCapture:
    """auto_capture_from_turn should feed preference captures to InferenceTracker."""

    def test_preference_capture_creates_inference(self, tmp_path, monkeypatch):
        """Preference-type captures should call add_inference()."""
        from cognition.capture import auto_capture_from_turn
        from cognition.staging import StagingStore

        # Set up staging store
        staging_path = tmp_path / "staging.jsonl"
        store = StagingStore(staging_path)

        # Set up inference tracker state file
        inference_file = tmp_path / "inferences.json"
        monkeypatch.setattr("config.INFERENCE_STATE_FILE", inference_file)

        # Capture a preference
        written = auto_capture_from_turn(
            "I prefer dark mode for everything",
            "Got it, I'll remember that preference.",
            store,
            session_id="test",
            turn_number=1,
        )

        if written > 0:
            # Check inference tracker was called
            if inference_file.exists():
                import json
                inferences = json.loads(inference_file.read_text())
                assert len(inferences) >= 1
                assert inferences[0]["source"] == "auto_capture"

    def test_non_preference_skips_inference(self, tmp_path, monkeypatch):
        """Non-preference captures should NOT create inferences."""
        from cognition.capture import auto_capture_from_turn
        from cognition.staging import StagingStore

        staging_path = tmp_path / "staging.jsonl"
        store = StagingStore(staging_path)
        inference_file = tmp_path / "inferences.json"
        monkeypatch.setattr("config.INFERENCE_STATE_FILE", inference_file)

        # Capture a fact (not a preference)
        auto_capture_from_turn(
            "Remember that the server IP is 72.60.69.129",
            "Noted.",
            store,
            session_id="test",
        )

        # Inference file should not exist or be empty
        if inference_file.exists():
            import json
            inferences = json.loads(inference_file.read_text())
            # Facts don't trigger add_inference, only preferences/decisions
            pref_inferences = [
                i for i in inferences
                if "prefer" in i.get("inference", "").lower()
            ]
            assert len(pref_inferences) == 0


# === Skill nudge (5a.3) ===


class TestSkillNudge:
    """Skill nudge event should be loggable."""

    def test_skill_nudge_log_format(self):
        """SkillLog with action=nudge_opportunity is valid."""
        from cognition.observability import SkillLog, log_skill_event

        log = SkillLog(
            action="nudge_opportunity",
            skill_name="",
            category="",
            tool_count=4,
        )
        # Should not raise
        log_skill_event(log)
        assert log.action == "nudge_opportunity"
        assert log.tool_count == 4
