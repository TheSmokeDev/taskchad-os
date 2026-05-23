"""Tests for policy-gated amendment ledger and auto-apply."""

from __future__ import annotations

import sys
from pathlib import Path

_CHAT_DIR = Path(__file__).resolve().parent.parent.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))

from cognition.amendments import (  # noqa: E402
    AmendmentProposal,
    ProposalLedger,
    apply_policy_approved_amendments,
    build_amendment_gate_section,
    parse_amendment_records,
    process_amendment_output,
)


def test_proposal_ledger_appends_pending_proposal(tmp_path: Path) -> None:
    ledger = ProposalLedger(tmp_path / "amendments.jsonl")
    proposal = AmendmentProposal(
        source="test",
        target_file="SELF.md",
        summary="Codify a verified behavior",
        rationale="Repeated evidence in test logs.",
        evidence_paths=["daily/2026-05-22.md"],
        proposed_content="- Prefer proposal ledgers for identity changes.",
    )

    assert ledger.append(proposal) is True
    pending = ledger.read_pending()

    assert len(pending) == 1
    assert pending[0].target_file == "SELF.md"
    assert pending[0].status == "pending"


def test_proposal_ledger_rejects_duplicate_active_key(tmp_path: Path) -> None:
    ledger = ProposalLedger(tmp_path / "amendments.jsonl")
    first = AmendmentProposal(
        source="test",
        target_file="MEMORY.md",
        summary="Same",
        proposed_content="- Same content",
    )
    duplicate = AmendmentProposal(
        source="test",
        target_file="MEMORY.md",
        summary="Same",
        proposed_content="- Same content",
    )

    assert ledger.append(first) is True
    assert ledger.append(duplicate) is False
    assert ledger.count_pending() == 1


def test_proposal_ledger_does_not_apply_on_manual_review(tmp_path: Path) -> None:
    ledger = ProposalLedger(tmp_path / "amendments.jsonl")
    target = tmp_path / "SELF.md"
    target.write_text("# SELF\n\nunchanged\n", encoding="utf-8")
    proposal = AmendmentProposal(
        source="test",
        target_file="SELF.md",
        summary="Change self",
        proposed_content="changed",
    )
    ledger.append(proposal)

    assert ledger.mark_reviewed(
        proposal.id,
        status="approved",
        reviewer="operator",
    ) is True

    assert target.read_text(encoding="utf-8") == "# SELF\n\nunchanged\n"
    assert ledger.read_all()[0].status == "approved"


def test_amendment_gate_section_names_policy_gate(tmp_path: Path) -> None:
    section = build_amendment_gate_section(
        tmp_path / "amendments.jsonl",
        source="memory_weekly",
    )

    assert "Policy-Gated Durable Memory Amendments" in section
    assert "machine policy gate" in section
    assert "`source`: `memory_weekly`" in section
    assert "`confidence_score`: 0.0-1.0" in section
    assert "`status`: `pending`" in section


def test_policy_approved_amendment_applies_with_rollback(tmp_path: Path) -> None:
    memory_dir = tmp_path / "Memory"
    memory_dir.mkdir()
    target = memory_dir / "SELF.md"
    target.write_text("# SELF\n\n- original\n", encoding="utf-8")
    ledger = ProposalLedger(tmp_path / "state" / "amendments.jsonl")
    proposal = AmendmentProposal(
        source="test",
        target_file="SELF.md",
        summary="Add autonomous behavior",
        rationale="Repeated evidence.",
        evidence_paths=["daily/2026-05-23.md"],
        proposed_content="Remember the deterministic future-behavior proof.",
        confidence_score=0.92,
    )
    assert ledger.append(proposal) is True

    results = apply_policy_approved_amendments(ledger, memory_dir)

    assert len(results) == 1
    assert results[0].status == "applied"
    assert results[0].before_hash
    assert results[0].after_hash
    assert Path(results[0].rollback_snapshot_path).exists()
    text = target.read_text(encoding="utf-8")
    assert "## Autonomous Amendments" in text
    assert "deterministic future-behavior proof" in text
    assert ledger.read_all()[0].status == "applied"


def test_policy_rejects_secret_like_content(tmp_path: Path) -> None:
    memory_dir = tmp_path / "Memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text("# MEMORY\n", encoding="utf-8")
    ledger = ProposalLedger(tmp_path / "amendments.jsonl")
    proposal = AmendmentProposal(
        source="test",
        target_file="MEMORY.md",
        summary="Bad secret",
        rationale="Nope.",
        evidence_paths=["daily/2026-05-23.md"],
        proposed_content="Store API_TOKEN=<REDACTED-openai> for later.",
        confidence_score=0.99,
    )
    ledger.append(proposal)

    results = apply_policy_approved_amendments(ledger, memory_dir)

    assert results[0].status == "policy_rejected"
    assert results[0].policy_reason == "secret_like_content"
    assert "sk-testsecret" not in (memory_dir / "MEMORY.md").read_text(encoding="utf-8")


def test_parse_and_process_amendment_output(tmp_path: Path) -> None:
    memory_dir = tmp_path / "Memory"
    memory_dir.mkdir()
    (memory_dir / "USER.md").write_text("# USER\n", encoding="utf-8")
    ledger = ProposalLedger(tmp_path / "amendments.jsonl")
    output = """
{"target_file":"USER.md","summary":"Preference","rationale":"Explicit ask","evidence_paths":["daily/2026-05-23.md"],"proposed_content":"Prefers explicit model control.","confidence_score":0.95,"status":"pending"}
"""

    parsed = parse_amendment_records(output, default_source="memory_reflect")
    results = process_amendment_output(output, ledger, memory_dir, default_source="memory_reflect")

    assert parsed[0].source == "memory_reflect"
    assert results[0].status == "applied"
    assert "Prefers explicit model control" in (memory_dir / "USER.md").read_text(
        encoding="utf-8"
    )
