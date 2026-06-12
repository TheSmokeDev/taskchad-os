"""Tests for policy-gated amendment ledger and auto-apply."""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

_CHAT_DIR = Path(__file__).resolve().parent.parent.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import cognition.amendments as amendments_module  # noqa: E402
from cognition.amendments import (  # noqa: E402
    AmendmentProposal,
    ProposalLedger,
    _ledger_lock,
    _ledger_lock_file,
    _split_amendment_section,
    apply_policy_approved_amendments,
    build_amendment_gate_section,
    collapse_autonomous_amendments,
    parse_amendment_records,
    process_amendment_output,
)


def _raw_llm_record(
    content: str,
    *,
    target: str = "MEMORY.md",
    summary: str = "Raw lesson",
) -> dict[str, Any]:
    """A ledger row exactly as the LLM wrote it: ONLY the 8 prompt-documented keys."""

    return {
        "source": "memory_reflect",
        "target_file": target,
        "summary": summary,
        "rationale": "Seen repeatedly in daily logs.",
        "evidence_paths": ["daily/2026-06-09.md"],
        "proposed_content": content,
        "confidence_score": 0.9,
        "status": "pending",
    }


def _write_raw_ledger(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def _read_raw_rows(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


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
    assert "Do NOT create or edit the ledger file yourself." in section
    assert "the runtime appends them to the ledger" in section
    assert "Recently Proposed" not in section

    ledger = ProposalLedger(tmp_path / "amendments.jsonl")
    assert ledger.append(
        AmendmentProposal(
            source="memory_weekly",
            target_file="MEMORY.md",
            summary="Pending digest entry",
            proposed_content="Digest content body.",
        )
    ) is True
    section_with_digest = build_amendment_gate_section(
        tmp_path / "amendments.jsonl",
        source="memory_weekly",
        ledger=ledger,
    )

    assert "### Recently Proposed (do NOT re-propose these)" in section_with_digest
    assert "- [pending] MEMORY.md: Pending digest entry" in section_with_digest


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


def test_read_all_heals_idless_records_with_stable_ids(tmp_path: Path) -> None:
    ledger_path = tmp_path / "amendments.jsonl"
    _write_raw_ledger(ledger_path, [_raw_llm_record("Prefer ledgers over direct edits.")])
    ledger = ProposalLedger(ledger_path)

    first = ledger.read_all()
    second = ledger.read_all()

    assert len(first) == len(second) == 1
    assert first[0].id
    assert first[0].id == second[0].id
    on_disk = _read_raw_rows(ledger_path)[0]
    assert on_disk["id"] == first[0].id
    assert on_disk["created_at"]


def test_apply_persists_applied_status_for_idless_records(tmp_path: Path) -> None:
    memory_dir = tmp_path / "Memory"
    memory_dir.mkdir()
    target = memory_dir / "MEMORY.md"
    target.write_text("# MEMORY\n", encoding="utf-8")
    ledger_path = tmp_path / "state" / "amendments.jsonl"
    _write_raw_ledger(
        ledger_path,
        [_raw_llm_record("Prefers explicit runtime control over silent fallback.")],
    )
    ledger = ProposalLedger(ledger_path)

    results = apply_policy_approved_amendments(ledger, memory_dir)

    assert len(results) == 1
    assert results[0].status == "applied"
    assert results[0].policy_reason == "policy_allowed"
    assert _read_raw_rows(ledger_path)[0]["status"] == "applied"
    text_after_first = target.read_text(encoding="utf-8")
    assert text_after_first.count("HOMIE_AUTO_AMENDMENT:") == 1

    second_results = apply_policy_approved_amendments(ledger, memory_dir)

    assert second_results == []
    assert target.read_text(encoding="utf-8") == text_after_first


def test_apply_skips_when_marker_already_present(tmp_path: Path) -> None:
    memory_dir = tmp_path / "Memory"
    memory_dir.mkdir()
    target = memory_dir / "MEMORY.md"
    ledger = ProposalLedger(tmp_path / "amendments.jsonl")
    proposal = AmendmentProposal(
        source="test",
        target_file="MEMORY.md",
        summary="Marker already merged",
        rationale="Hand-merged earlier.",
        evidence_paths=["daily/2026-06-09.md"],
        proposed_content="Some amendment text already merged by hand.",
        confidence_score=0.9,
    )
    assert ledger.append(proposal) is True
    seeded = (
        "# MEMORY\n\n## Autonomous Amendments\n\n"
        f"<!-- HOMIE_AUTO_AMENDMENT:{proposal.id} -->\n- hand merged variant\n"
    )
    target.write_text(seeded, encoding="utf-8")

    results = apply_policy_approved_amendments(ledger, memory_dir)

    assert results[0].status == "applied"
    assert results[0].policy_decision == "reconcile"
    assert results[0].policy_reason == "already_present_in_target"
    assert target.read_text(encoding="utf-8") == seeded
    assert ledger.read_all()[0].status == "applied"


def test_apply_skips_when_content_already_present(tmp_path: Path) -> None:
    memory_dir = tmp_path / "Memory"
    memory_dir.mkdir()
    target = memory_dir / "MEMORY.md"
    content = "Prefer explicit provider pinning for cron lanes."
    seeded = (
        "# MEMORY\n\n## Autonomous Amendments\n\n"
        f"<!-- HOMIE_AUTO_AMENDMENT:{uuid.uuid4()} -->\n- {content}\n"
    )
    target.write_text(seeded, encoding="utf-8")
    ledger = ProposalLedger(tmp_path / "amendments.jsonl")
    proposal = AmendmentProposal(
        source="test",
        target_file="MEMORY.md",
        summary="Content already merged",
        rationale="Shipped under a different marker.",
        evidence_paths=["daily/2026-06-09.md"],
        proposed_content=content,
        confidence_score=0.9,
    )
    assert ledger.append(proposal) is True

    results = apply_policy_approved_amendments(ledger, memory_dir)

    assert results[0].status == "applied"
    assert results[0].policy_decision == "reconcile"
    assert results[0].policy_reason == "already_present_in_target"
    assert target.read_text(encoding="utf-8") == seeded
    reconciled = ledger.read_all()[0]
    assert reconciled.status == "applied"
    assert reconciled.policy_reason == "already_present_reconciled"


def test_dedupe_ignores_summary_and_stored_key(tmp_path: Path) -> None:
    ledger_path = tmp_path / "amendments.jsonl"
    ledger = ProposalLedger(ledger_path)
    first = AmendmentProposal(
        source="memory_reflect",
        target_file="MEMORY.md",
        summary="Summary one",
        proposed_content="Shared content body.",
    )
    same_content_new_summary = AmendmentProposal(
        source="memory_weekly",
        target_file="MEMORY.md",
        summary="A totally different summary",
        proposed_content="Shared content body.",
    )

    assert ledger.append(first) is True
    assert ledger.append(same_content_new_summary) is False

    rows = _read_raw_rows(ledger_path)
    rows[0]["dedupe_key"] = "tampered-stored-key"
    _write_raw_ledger(ledger_path, rows)
    same_content_third = AmendmentProposal(
        source="memory_dream",
        target_file="MEMORY.md",
        summary="Third summary",
        proposed_content="Shared content body.",
    )

    assert ledger.append(same_content_third) is False
    assert ledger.count_pending() == 1


def test_apply_limit_counts_only_physical_writes(tmp_path: Path) -> None:
    memory_dir = tmp_path / "Memory"
    memory_dir.mkdir()
    target = memory_dir / "MEMORY.md"
    contents = [f"Distinct lesson number {index} for limit tests." for index in range(5)]
    target.write_text(
        "# MEMORY\n\n## Autonomous Amendments\n\n"
        f"<!-- HOMIE_AUTO_AMENDMENT:{uuid.uuid4()} -->\n- {contents[0]}\n",
        encoding="utf-8",
    )
    ledger = ProposalLedger(tmp_path / "amendments.jsonl")
    for index, content in enumerate(contents):
        assert ledger.append(
            AmendmentProposal(
                source="test",
                target_file="MEMORY.md",
                summary=f"Lesson {index}",
                rationale="Repeated evidence.",
                evidence_paths=["daily/2026-06-09.md"],
                proposed_content=content,
                confidence_score=0.9,
            )
        ) is True

    results = apply_policy_approved_amendments(ledger, memory_dir, limit=2)

    # The first candidate reconciles (free); two physical writes consume the limit.
    assert [result.policy_decision for result in results] == [
        "reconcile",
        "apply",
        "apply",
    ]
    text = target.read_text(encoding="utf-8")
    assert text.count("HOMIE_AUTO_AMENDMENT:") == 3  # pre-seeded + 2 physical writes
    statuses = [proposal.status for proposal in ledger.read_all()]
    assert statuses.count("applied") == 3
    assert statuses.count("pending") == 2


def test_autonomous_section_capped_on_append(tmp_path: Path) -> None:
    memory_dir = tmp_path / "Memory"
    memory_dir.mkdir()
    target = memory_dir / "MEMORY.md"
    head = "# MEMORY\n\n- durable head fact\n\n## Autonomous Amendments\n"
    seeded_blocks = "".join(
        f"\n<!-- HOMIE_AUTO_AMENDMENT:{uuid.uuid4()} -->\n"
        f"- seeded amendment {index:02d}\n"
        "  - source: test\n"
        "  - evidence: daily/2026-06-09.md\n"
        for index in range(20)
    )
    target.write_text(head + seeded_blocks, encoding="utf-8")
    ledger = ProposalLedger(tmp_path / "amendments.jsonl")
    assert ledger.append(
        AmendmentProposal(
            source="test",
            target_file="MEMORY.md",
            summary="Beyond the cap",
            rationale="Cap behavior.",
            evidence_paths=["daily/2026-06-09.md"],
            proposed_content="Brand new amendment beyond the cap.",
            confidence_score=0.9,
        )
    ) is True

    results = apply_policy_approved_amendments(ledger, memory_dir, section_cap=20)

    assert results[0].status == "applied"
    assert results[0].policy_decision == "apply"
    text = target.read_text(encoding="utf-8")
    head_after, blocks_after = _split_amendment_section(text)
    assert head_after + "".join(blocks_after) == text  # split round-trip invariant
    assert len(blocks_after) == 20
    assert "seeded amendment 00" not in text  # oldest trimmed
    assert "seeded amendment 01" in text
    assert "Brand new amendment beyond the cap." in text
    assert text.startswith(head)  # head intact


def test_collapse_dedupes_reconciles_and_preserves_head(tmp_path: Path) -> None:
    memory_dir = tmp_path / "Memory"
    memory_dir.mkdir()
    target = memory_dir / "MEMORY.md"
    content_a = "Prefer the ledger gate for identity changes."
    content_b = "Heartbeat checks calendar before email."
    content_c = "Never shipped to the target file."
    head = "# MEMORY\n\nDurable head facts stay.\n\n## Autonomous Amendments\n"
    duplicated_blocks = "".join(
        f"\n<!-- HOMIE_AUTO_AMENDMENT:{uuid.uuid4()} -->\n"
        f"- {content}\n"
        "  - source: memory_reflect\n"
        f"  - evidence: daily/2026-06-0{index + 1}.md\n"
        for index, content in enumerate([content_a] * 3 + [content_b] * 3)
    )
    target.write_text(head + duplicated_blocks, encoding="utf-8")
    ledger_path = tmp_path / "state" / "amendments.jsonl"
    _write_raw_ledger(
        ledger_path,
        [_raw_llm_record(content) for content in (content_a, content_b, content_c)],
    )
    ledger = ProposalLedger(ledger_path)
    original_head, original_blocks = _split_amendment_section(
        target.read_text(encoding="utf-8")
    )
    assert len(original_blocks) == 6

    report = collapse_autonomous_amendments(target, ledger, section_cap=20)

    assert report.target_file == str(target)
    assert report.blocks_before == 6
    assert report.blocks_kept == 2
    assert report.ledger_applied == 2
    assert report.ledger_superseded == 1
    text_after = target.read_text(encoding="utf-8")
    head_after, blocks_after = _split_amendment_section(text_after)
    assert head_after == original_head  # head bytes identical
    assert len(blocks_after) == 2
    proposals = ledger.read_all()
    statuses = sorted(proposal.status for proposal in proposals)
    assert statuses == ["applied", "applied", "superseded"]
    superseded = next(p for p in proposals if p.status == "superseded")
    assert superseded.proposed_content == content_c
    assert superseded.policy_reason == "stale_backlog_collapse"
    for proposal in proposals:
        if proposal.status == "applied":
            # Content-based reconciliation: the kept block carries the content;
            # markers keep their ORIGINAL ids (no rewrite — the ledger is only
            # touched after the target write).
            assert proposal.proposed_content in text_after

    second_report = collapse_autonomous_amendments(target, ledger, section_cap=20)

    assert second_report.blocks_before == 2
    assert second_report.blocks_kept == 2
    assert second_report.ledger_applied == 0
    assert second_report.ledger_superseded == 0
    assert target.read_text(encoding="utf-8") == text_after


def test_collapse_target_write_failure_leaves_ledger_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIX-1: a failed TARGET write must leave the ledger byte-identical.

    Collapse plans in memory, writes snapshot + target, and only then updates
    the ledger — so a target-write crash cannot leave the ledger claiming
    blocks were collapsed while the target stays flooded.
    """

    memory_dir = tmp_path / "Memory"
    memory_dir.mkdir()
    target = memory_dir / "MEMORY.md"
    content = "Collapse ordering survives a failed target write."
    head = "# MEMORY\n\nDurable head facts stay.\n\n## Autonomous Amendments\n"
    duplicated_blocks = "".join(
        f"\n<!-- HOMIE_AUTO_AMENDMENT:{uuid.uuid4()} -->\n"
        f"- {content}\n"
        "  - source: memory_reflect\n"
        "  - evidence: daily/2026-06-09.md\n"
        for _ in range(3)
    )
    target.write_text(head + duplicated_blocks, encoding="utf-8")
    ledger_path = tmp_path / "state" / "amendments.jsonl"
    # Seed a RAW id-less LLM-written row: even read_all's id-heal must not
    # touch the ledger before the target write succeeds.
    _write_raw_ledger(ledger_path, [_raw_llm_record(content)])
    ledger = ProposalLedger(ledger_path)
    ledger_before = ledger.path.read_bytes()
    target_before = target.read_bytes()

    real_write = amendments_module._atomic_write_text

    def fail_target_write_only(path: Path, text: str) -> None:
        if Path(path) == target:
            raise OSError("simulated target write failure")
        real_write(path, text)

    with monkeypatch.context() as patched:
        patched.setattr(
            amendments_module, "_atomic_write_text", fail_target_write_only
        )
        with pytest.raises(OSError, match="simulated target write failure"):
            collapse_autonomous_amendments(target, ledger, section_cap=20)

    assert ledger.path.read_bytes() == ledger_before  # ledger byte-identical
    assert target.read_bytes() == target_before  # target untouched
    assert ledger.read_all()[0].status == "pending"  # still claimable

    report = collapse_autonomous_amendments(target, ledger, section_cap=20)

    assert report.blocks_before == 3
    assert report.blocks_kept == 1
    assert report.ledger_applied == 1
    assert report.ledger_superseded == 0
    assert ledger.read_all()[0].status == "applied"
    text_after = target.read_text(encoding="utf-8")
    assert text_after.count("HOMIE_AUTO_AMENDMENT:") == 1
    # Content-based reconciliation: the kept block keeps its original marker id.
    assert content in text_after


def test_heal_preserves_malformed_lines_verbatim(tmp_path: Path) -> None:
    """FIX-2: the heal rewrite must never delete unparseable ledger lines."""

    ledger_path = tmp_path / "amendments.jsonl"
    idless_line = json.dumps(
        _raw_llm_record("Heal keeps malformed neighbors."), ensure_ascii=False
    )
    malformed_line = (
        '{"source": "memory_reflect", "target_file": "MEMORY.md", "proposed'
    )
    complete = _raw_llm_record("Complete row stays untouched.")
    complete["id"] = str(uuid.uuid4())
    complete["created_at"] = "2026-06-09T00:00:00+00:00"
    complete_line = json.dumps(complete, ensure_ascii=False)
    ledger_path.write_text(
        idless_line + "\n" + malformed_line + "\n" + complete_line + "\n",
        encoding="utf-8",
    )
    ledger = ProposalLedger(ledger_path)

    proposals = ledger.read_all()  # triggers the line-based heal

    assert len(proposals) == 2  # read semantics unchanged: parseable only
    lines_after = ledger_path.read_text(encoding="utf-8").splitlines()
    assert len(lines_after) == 3  # nothing deleted
    assert lines_after[1] == malformed_line  # byte-identical, original position
    assert lines_after[2] == complete_line  # complete line untouched
    healed = json.loads(lines_after[0])
    assert healed["id"]
    assert healed["created_at"]
    assert healed["id"] == proposals[0].id  # returned id matches disk


def test_ledger_lock_nests_without_deadlock(tmp_path: Path) -> None:
    """FIX-3: ledger mutations inside a held _ledger_lock must not deadlock."""

    ledger = ProposalLedger(tmp_path / "amendments.jsonl")
    proposal = AmendmentProposal(
        source="test",
        target_file="MEMORY.md",
        summary="Nested append",
        proposed_content="Nested lock content.",
    )
    start = time.monotonic()
    with _ledger_lock(ledger.path):
        assert ledger.append(proposal) is True  # append acquires internally
        with _ledger_lock(ledger.path):  # explicit double-nesting is a no-op
            assert ledger.count_pending() == 1
        assert ledger.mark_reviewed(
            proposal.id, status="approved", reviewer="operator"
        ) is True  # _update_record acquires internally
    assert time.monotonic() - start < 4.0  # never hit the 5s timeout spin
    assert ledger.read_all()[0].status == "approved"


def test_ledger_lock_file_matches_shared_file_lock_convention(
    tmp_path: Path,
) -> None:
    """FIX-5: _ledger_lock and shared.file_lock contend on the SAME lockfile."""

    from shared import file_lock as shared_file_lock

    ledger_path = tmp_path / "amendments.jsonl"
    expected_lock_file = _ledger_lock_file(ledger_path)
    assert expected_lock_file == Path(str(ledger_path) + ".lock")

    with shared_file_lock(ledger_path, timeout=1.0):
        # shared.file_lock created exactly the lockfile our helper computes.
        assert expected_lock_file.exists()
        # Both helpers lock the same byte of the same file, so a second
        # acquisition from a different handle is denied — real interop.
        with pytest.raises(TimeoutError):
            with _ledger_lock(ledger_path, timeout=0.3):
                pass


def test_prose_substring_does_not_reconcile(tmp_path: Path) -> None:
    """FIX-4: proposal text inside ordinary prose must NOT count as present."""

    memory_dir = tmp_path / "Memory"
    memory_dir.mkdir()
    target = memory_dir / "MEMORY.md"
    content = "Prefer direct integrations for everything."
    target.write_text(
        f"# MEMORY\n\n- Note to self: {content} That was a good call.\n",
        encoding="utf-8",
    )
    ledger = ProposalLedger(tmp_path / "amendments.jsonl")
    proposal = AmendmentProposal(
        source="test",
        target_file="MEMORY.md",
        summary="Prose lookalike",
        rationale="A real amendment block is still required.",
        evidence_paths=["daily/2026-06-09.md"],
        proposed_content=content,
        confidence_score=0.9,
    )
    assert ledger.append(proposal) is True

    results = apply_policy_approved_amendments(ledger, memory_dir)

    assert results[0].policy_decision == "apply"  # NOT reconcile
    assert results[0].status == "applied"
    text = target.read_text(encoding="utf-8")
    assert "## Autonomous Amendments" in text  # a real block got written
    assert f"HOMIE_AUTO_AMENDMENT:{proposal.id}" in text
    # The equal-content-block case (different marker id → reconcile) is
    # covered by test_apply_skips_when_content_already_present above.


def test_superseded_status_round_trips(tmp_path: Path) -> None:
    proposal = AmendmentProposal(
        source="collapse_reconcile",
        target_file="MEMORY.md",
        summary="Old backlog entry",
        proposed_content="Stale content kept for audit.",
        status="superseded",
    )

    assert proposal.status == "superseded"

    ledger = ProposalLedger(tmp_path / "amendments.jsonl")
    assert ledger.append(proposal) is True
    assert ledger.read_all()[0].status == "superseded"
