"""Tests for the cognitive-loop status collector."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_CHAT_DIR = Path(__file__).resolve().parent.parent.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))

from cognition.status import collect_cognitive_loop_status  # noqa: E402


def _seed_status_sources(tmp_path: Path, *, heartbeat_source: str) -> tuple[Path, Path]:
    chat_dir = tmp_path / "chat"
    scripts_dir = tmp_path / "scripts"
    cognition_dir = chat_dir / "cognition"
    runtime_dir = scripts_dir / "runtime"
    cognition_dir.mkdir(parents=True)
    runtime_dir.mkdir(parents=True)

    (chat_dir / "engine.py").write_text(
        "\n".join([
            "from cognition.identity_payload import build_identity_payload",
            "from cognition.processes import detect_process, get_process_weights",
            "from cognition.regions import apply_process_weights, PromptRegion",
            "from cognition.self_model import InferenceTracker",
            "payload = {}",
            'working = payload.get("WORKING", "")',
            "PromptRegion('working_memory', working, 1)",
            "InferenceTracker(None).get_active()",
            "user_inferences = []",
        ]),
        encoding="utf-8",
    )
    (cognition_dir / "working_memory.py").write_text("", encoding="utf-8")
    (cognition_dir / "steps.py").write_text("", encoding="utf-8")
    (cognition_dir / "processes.py").write_text("", encoding="utf-8")
    (cognition_dir / "self_model.py").write_text(
        "def contradict(): pass\n",
        encoding="utf-8",
    )
    (cognition_dir / "identity_payload.py").write_text("", encoding="utf-8")
    (scripts_dir / "memory_reflect.py").write_text(
        "from cognition.identity_payload import build_identity_payload\nSELF.md\n",
        encoding="utf-8",
    )
    (scripts_dir / "memory_weekly.py").write_text(
        "from cognition.identity_payload import build_identity_payload\nSELF.md\n",
        encoding="utf-8",
    )
    (scripts_dir / "memory_dream.py").write_text(
        "from cognition.identity_payload import build_identity_payload\n"
        "SELF.md\nResolve contradictions\n",
        encoding="utf-8",
    )
    (scripts_dir / "heartbeat.py").write_text(heartbeat_source, encoding="utf-8")
    (runtime_dir / "bootstrap.py").write_text(
        "def build_session_briefing(): pass\n"
        "def _extract_working_memory(): pass\n",
        encoding="utf-8",
    )
    return chat_dir, scripts_dir


def test_cognitive_loop_status_is_json_serializable() -> None:
    status = collect_cognitive_loop_status()

    payload = json.dumps(status)
    assert isinstance(payload, str)
    assert status["overall"] == "partial"
    assert isinstance(status["subsystems"], dict)
    assert isinstance(status["next_actions"], list)


def test_cognitive_loop_reports_live_identity_and_inferences() -> None:
    status = collect_cognitive_loop_status()
    subsystems = status["subsystems"]

    assert subsystems["identity_payload"]["state"] == "live"
    assert subsystems["active_inferences"]["state"] == "live"
    assert "user_inferences" in subsystems["active_inferences"]["evidence"]


def test_cognitive_loop_reports_scheduled_identity_truthfully() -> None:
    status = collect_cognitive_loop_status()
    subsystems = status["subsystems"]

    assert subsystems["scheduled_cognition_payload"]["state"] == "live"
    assert subsystems["reflection_identity"]["state"] == "live"
    assert subsystems["weekly_identity"]["state"] == "live"
    assert subsystems["dream_identity"]["state"] == "live"
    assert subsystems["heartbeat_identity"]["state"] == "live"
    assert "build_scheduled_cognition_payload" in (
        subsystems["heartbeat_identity"]["evidence"]
    )


def test_cognitive_loop_does_not_overclaim_planned_features() -> None:
    status = collect_cognitive_loop_status()
    subsystems = status["subsystems"]

    assert subsystems["working_memory"]["state"] == "shadow_only"
    assert subsystems["self_amendment"]["state"] == "planned"
    assert subsystems["contradiction_detection"]["state"] == "planned"
    assert subsystems["self_amendment"]["details"]["proposal_ledger"] is False
    assert subsystems["contradiction_detection"]["details"]["detector"] is False


def test_heartbeat_identity_flips_live_when_helper_is_wired(tmp_path: Path) -> None:
    chat_dir, scripts_dir = _seed_status_sources(
        tmp_path,
        heartbeat_source=(
            "from cognition.scheduled_payload import build_scheduled_cognition_payload\n"
            "build_scheduled_cognition_payload(memory_dir)\n"
            'caller="heartbeat"\n'
        ),
    )

    status = collect_cognitive_loop_status(chat_dir=chat_dir, scripts_dir=scripts_dir)

    assert status["subsystems"]["heartbeat_identity"]["state"] == "live"


def test_heartbeat_identity_reports_drift_when_helper_is_absent(tmp_path: Path) -> None:
    chat_dir, scripts_dir = _seed_status_sources(
        tmp_path,
        heartbeat_source='caller="heartbeat"\n',
    )

    status = collect_cognitive_loop_status(chat_dir=chat_dir, scripts_dir=scripts_dir)

    assert status["subsystems"]["heartbeat_identity"]["state"] == "drift"
