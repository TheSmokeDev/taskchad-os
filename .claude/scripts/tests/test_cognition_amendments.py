"""Tests for human-gated amendment proposal ledger."""

from __future__ import annotations

import sys
from pathlib import Path

_CHAT_DIR = Path(__file__).resolve().parent.parent.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))

from cognition.amendments import (  # noqa: E402
    AmendmentProposal,
    ProposalLedger,
    build_amendment_gate_section,
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


def test_proposal_ledger_does_not_apply_on_review(tmp_path: Path) -> None:
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


def test_amendment_gate_section_names_human_gate(tmp_path: Path) -> None:
    section = build_amendment_gate_section(
        tmp_path / "amendments.jsonl",
        source="memory_weekly",
    )

    assert "Human-Gated Durable Memory Amendments" in section
    assert "Do not directly edit `SELF.md`, `SOUL.md`, `USER.md`, or `MEMORY.md`" in section
    assert "`source`: `memory_weekly`" in section
    assert "`status`: `pending`" in section
