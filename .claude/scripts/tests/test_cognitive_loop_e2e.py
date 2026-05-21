"""Temp-state cognitive-loop E2E probes for chat prompt assembly."""

from __future__ import annotations

from pathlib import Path
import sys

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for path in (str(_SCRIPTS_DIR), str(_CHAT_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from cognitive_loop_test_harness import (  # noqa: E402
    IDENTITY_SENTINELS,
    seed_cognitive_loop_temp_vault,
)


def test_chat_frozen_regions_use_temp_identity_inferences_and_working_memory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Chat prompt assembly consumes temp identity, inference, and WORKING state."""

    import config
    from cognition.self_model import InferenceTracker
    from engine import ConversationEngine

    vault = seed_cognitive_loop_temp_vault(tmp_path / "TheHomie" / "Memory")
    state_dir = tmp_path / "state"
    inference_file = state_dir / "self-model-inferences.json"
    tracker = InferenceTracker(inference_file)
    tracker.add_inference(
        "The user requires file-line evidence for validation claims.",
        "Seeded by cognitive-loop E2E harness.",
        confidence=0.95,
        source="validation_harness",
    )

    monkeypatch.setattr(config, "MEMORY_DIR", vault)
    monkeypatch.setattr(config, "INFERENCE_STATE_FILE", inference_file)

    engine = ConversationEngine(
        session_store=object(),
        project_root=tmp_path,
        max_turns=1,
        max_budget_usd=0.01,
    )
    regions = {region.name: region.content for region in engine._build_frozen_regions()}

    assert IDENTITY_SENTINELS["SOUL"] in regions["identity"]
    assert IDENTITY_SENTINELS["SELF"] in regions["self_model"]
    assert IDENTITY_SENTINELS["USER"] in regions["user_model"]
    assert IDENTITY_SENTINELS["MEMORY"] in regions["durable_memory"]
    assert IDENTITY_SENTINELS["WORKING"] in regions["working_memory"]
    assert "The user requires file-line evidence" in regions["user_inferences"]
