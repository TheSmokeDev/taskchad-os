"""Tests for bounded cognitive-loop drift detection."""

from __future__ import annotations

import sys
from pathlib import Path

_CHAT_DIR = Path(__file__).resolve().parent.parent.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))

from cognition.contradictions import (  # noqa: E402
    DriftFinding,
    DriftLedger,
    build_drift_detection_section,
    detect_cognitive_loop_drift,
)


def _status(**states: str) -> dict:
    return {
        "subsystems": {
            name: {"state": state}
            for name, state in states.items()
        }
    }


def test_detect_cognitive_loop_drift_reports_source_path(tmp_path: Path) -> None:
    (tmp_path / "PRDs" / "active").mkdir(parents=True)
    doc = tmp_path / "PRDs" / "active" / "PRD-test.md"
    doc.write_text(
        "# Test\n\nSelf-amendment proposal ledger is shipped and green.\n",
        encoding="utf-8",
    )

    findings = detect_cognitive_loop_drift(
        tmp_path,
        _status(self_amendment="planned"),
    )

    assert len(findings) == 1
    assert findings[0].subsystem == "self_amendment"
    assert findings[0].current_state == "planned"
    assert findings[0].source_paths == ["PRDs/active/PRD-test.md:3"]


def test_detect_cognitive_loop_drift_ignores_honest_non_live_line(tmp_path: Path) -> None:
    (tmp_path / "WORKBOARD.md").write_text(
        "Self-amendment proposal ledger remains planned, not shipped.\n",
        encoding="utf-8",
    )

    findings = detect_cognitive_loop_drift(
        tmp_path,
        _status(self_amendment="planned"),
    )

    assert findings == []


def test_detect_cognitive_loop_drift_is_bounded(tmp_path: Path) -> None:
    active = tmp_path / "PRPs" / "active"
    active.mkdir(parents=True)
    for index in range(5):
        (active / f"PRP-{index}.md").write_text(
            "WorkingMemory production owner is complete and ready.\n",
            encoding="utf-8",
        )

    findings = detect_cognitive_loop_drift(
        tmp_path,
        _status(working_memory="shadow_only"),
        max_findings=2,
    )

    assert len(findings) == 2


def test_drift_detection_section_renders_findings(tmp_path: Path) -> None:
    (tmp_path / "WORKBOARD.md").write_text(
        "Contradiction detector is complete and green.\n",
        encoding="utf-8",
    )

    section = build_drift_detection_section(
        tmp_path,
        _status(contradiction_detection="planned"),
    )

    assert "Cognitive Loop Drift Findings" in section
    assert "WORKBOARD.md:1" in section


def test_drift_ledger_appends_and_dedupes_open_findings(tmp_path: Path) -> None:
    ledger = DriftLedger(tmp_path / "drift.jsonl")
    finding = DriftFinding(
        subsystem="working_memory",
        current_state="shadow_only",
        summary="WorkingMemory overclaim",
        source_paths=["WORKBOARD.md:1"],
        evidence=["WorkingMemory is complete."],
    )
    duplicate = DriftFinding(
        subsystem="working_memory",
        current_state="shadow_only",
        summary="WorkingMemory overclaim",
        source_paths=["WORKBOARD.md:1"],
        evidence=["WorkingMemory is complete."],
    )

    assert ledger.append(finding) is True
    assert ledger.append(duplicate) is False
    assert ledger.count_open() == 1
