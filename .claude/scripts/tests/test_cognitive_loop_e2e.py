"""Temp-state cognitive-loop E2E probes for chat prompt assembly."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

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
        # Living Self Act 1 (B1): the renderer now injects ONLY trustworthy
        # operator-belief sources {reflection, explicit}. Use "reflection" so
        # this loop test still proves inferences reach the system prompt.
        source="reflection",
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


@pytest.mark.asyncio
async def test_chat_turn_uses_working_memory_as_runtime_owner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A real chat turn renders and appends through WorkingMemory."""

    import config
    import engine as engine_module
    from cognition.self_model import InferenceTracker
    from engine import ConversationEngine
    from models import Channel, IncomingMessage, Platform, Thread, User
    from runtime.base import RUNTIME_LANE_CLAUDE_NATIVE, RuntimeResult
    from session import SQLiteSessionStore

    vault = seed_cognitive_loop_temp_vault(tmp_path / "TheHomie" / "Memory")
    inference_file = tmp_path / "state" / "self-model-inferences.json"
    tracker = InferenceTracker(inference_file)
    tracker.add_inference(
        "The user wants the full living mental loop proven.",
        "Seeded by cognitive-loop E2E harness.",
        confidence=0.95,
        # Living Self Act 1 (B1): the renderer now injects ONLY trustworthy
        # operator-belief sources {reflection, explicit}. Use "reflection" so
        # this loop test still proves inferences reach the system prompt.
        source="reflection",
    )

    monkeypatch.setattr(config, "MEMORY_DIR", vault)
    monkeypatch.setattr(config, "INFERENCE_STATE_FILE", inference_file)

    captured: dict[str, str] = {}

    async def fake_run(request):
        captured["system_prompt"] = request.system_prompt["append"]
        return RuntimeResult(
            text="E2E assistant response",
            runtime_lane=RUNTIME_LANE_CLAUDE_NATIVE,
            provider="claude",
            model="claude-sonnet-4-6",
            session_id="runtime-e2e",
        )

    class FakeRecallLog:
        tier = "tier_0"

    class FakeRecallResponse:
        formatted_text = ""
        log = FakeRecallLog()

    async def fake_recall(**_kwargs):
        return FakeRecallResponse()

    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)
    monkeypatch.setattr(engine_module, "recall_memory_service", fake_recall)

    engine = ConversationEngine(
        session_store=SQLiteSessionStore(tmp_path / "chat.db"),
        project_root=tmp_path,
        max_turns=1,
        max_budget_usd=0.01,
    )
    message = IncomingMessage(
        text="Prove the loop.",
        user=User(platform=Platform.TELEGRAM, platform_id="user-1", display_name="YourUser"),
        channel=Channel(platform=Platform.TELEGRAM, platform_id="chat-1", is_dm=True),
        platform=Platform.TELEGRAM,
        thread=Thread(thread_id="thread-1"),
    )

    outputs = [out async for out in engine.handle_message(message)]

    assert outputs[0].text == "E2E assistant response"
    assert IDENTITY_SENTINELS["SOUL"] in captured["system_prompt"]
    assert IDENTITY_SENTINELS["WORKING"] in captured["system_prompt"]
    assert "full living mental loop" in captured["system_prompt"]

    wm = engine._last_turn_working_memory
    assert wm is not None
    assert wm.memories[-2].role == "user"
    assert wm.memories[-2].content == "Prove the loop."
    assert wm.memories[-1].role == "assistant"
    assert wm.memories[-1].content == "E2E assistant response"
