"""Tests for first-class self-model state rendering."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_CHAT_DIR = Path(__file__).resolve().parent.parent.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))

from cognition.self_model import (  # noqa: E402
    build_self_model_state,
    render_self_model_state_section,
)


def test_self_model_state_groups_active_inferences(tmp_path: Path) -> None:
    state_file = tmp_path / "self-model-inferences.json"
    state_file.write_text(
        json.dumps([
            {
                "id": "drive",
                "inference": "Jarvis autonomy is the current P0 focus lane.",
                "observation": "Operator said to focus the next lane on autonomy.",
                "confidence": 0.95,
                "evidence_count": 3,
                "contradiction_count": 0,
                "first_seen": "2026-05-23T00:00:00+00:00",
                "last_updated": "2026-05-23T00:00:00+00:00",
                "source": "test",
                "status": "confirmed",
            },
            {
                "id": "mistake",
                "inference": "A recurring mistake is over-claiming live autonomy from source wiring.",
                "observation": "Status needed a truth split.",
                "confidence": 0.9,
                "evidence_count": 2,
                "contradiction_count": 0,
                "first_seen": "2026-05-23T00:00:00+00:00",
                "last_updated": "2026-05-23T00:00:00+00:00",
                "source": "test",
                "status": "active",
            },
        ]),
        encoding="utf-8",
    )

    state = build_self_model_state(state_file)
    section = render_self_model_state_section(state)

    assert state.drives == ["Jarvis autonomy is the current P0 focus lane."]
    assert state.recurring_mistakes == [
        "A recurring mistake is over-claiming live autonomy from source wiring."
    ]
    assert "Active Self-Model / Psyche State" in section
    assert "Current Drives" in section
    assert "Recurring Mistakes" in section
