"""Integration idempotence tests for the amendment pipeline (issue #58).

WS-B half of the #58 fix: the three scheduled producers (memory_reflect,
memory_dream, memory_weekly) must pass explicit ``apply_limit`` /
``section_cap`` bounds into ``process_amendment_output`` at CALL time
(Rule 1 — no config-bound function defaults), serialize ledger access through
``cognition.amendments.ledger_file_lock`` (reentrant; same lockfile + OS lock
as ``shared.file_lock``, but plain ``shared.file_lock`` would deadlock against
the ledger mutations nested inside), and hand the gate builder a
``ProposalLedger`` so the pending-proposal digest renders into the prompt.

The two double-run tests prove the flood failure is dead: running the same
pipeline twice produces ZERO growth in MEMORY.md — including the live-failure
shape where a raw id-less JSONL row was written directly into the ledger and
re-applied on every scheduled run.

Deterministic: zero LLM calls, zero network, tmp_path-isolated — never touches
the live ledger (``.claude/data/state/amendment-proposals.jsonl``) or the live
vault (``vault/memory/``).
"""

from __future__ import annotations

import inspect
import json
import re
import sys
from pathlib import Path

import pytest

# Ensure scripts dir is on path (defensive — conftest.py also injects it).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_CHAT_DIR = Path(__file__).resolve().parent.parent.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))

from cognition.amendments import (  # noqa: E402
    ProposalLedger,
    process_amendment_output,
)

import memory_dream  # noqa: E402
import memory_reflect  # noqa: E402
import memory_weekly  # noqa: E402

_PRODUCER_MODULES = (memory_reflect, memory_dream, memory_weekly)

_AMENDMENT_MARKER = "HOMIE_AUTO_AMENDMENT"


def _make_memory_dir(tmp_path: Path) -> tuple[Path, Path]:
    """Build a minimal tmp memory dir with a small MEMORY.md."""
    memory_dir = tmp_path / "Memory"
    memory_dir.mkdir()
    memory_file = memory_dir / "MEMORY.md"
    memory_file.write_text(
        "# MEMORY\n\n## Decisions\n\n- existing decision A\n",
        encoding="utf-8",
    )
    return memory_dir, memory_file


# =============================================================================
# DOUBLE-RUN IDEMPOTENCE — engine called with producer-style bounds
# =============================================================================


def test_parsed_output_double_run_zero_growth(tmp_path: Path) -> None:
    """Same amendment JSON processed twice must not grow MEMORY.md."""
    memory_dir, memory_file = _make_memory_dir(tmp_path)
    ledger = ProposalLedger(tmp_path / "state" / "amendment-proposals.jsonl")
    json_text = json.dumps(
        {
            "target_file": "MEMORY.md",
            "summary": "Idempotence lesson",
            "rationale": "Repeated evidence across daily logs.",
            "evidence_paths": ["daily/2026-06-09.md"],
            "proposed_content": "Lesson: double runs must keep MEMORY.md byte-identical.",
            "confidence_score": 0.9,
            "status": "pending",
        }
    )

    process_amendment_output(
        json_text,
        ledger,
        memory_dir,
        default_source="memory_reflect",
        apply_limit=3,
        section_cap=20,
    )
    after_run_1 = memory_file.read_bytes()

    process_amendment_output(
        json_text,
        ledger,
        memory_dir,
        default_source="memory_reflect",
        apply_limit=3,
        section_cap=20,
    )
    after_run_2 = memory_file.read_bytes()

    assert len(after_run_2) == len(after_run_1)
    assert after_run_2 == after_run_1
    assert ledger.count_pending() == 0


def test_direct_written_idless_record_double_run_zero_growth(
    tmp_path: Path,
) -> None:
    """Replicate the live #58 failure: a raw id-less JSONL row applies once.

    The flood bug: rows written directly into the ledger (no ``id`` /
    ``created_at`` / ``dedupe_key`` keys) got a FRESH uuid on every read, so
    the post-apply status update never matched a stored row — the row stayed
    "pending" forever and re-applied on every scheduled run.
    """
    memory_dir, memory_file = _make_memory_dir(tmp_path)
    ledger_path = tmp_path / "state" / "amendment-proposals.jsonl"
    ledger_path.parent.mkdir(parents=True)
    raw_record = {
        "source": "memory_reflect",
        "target_file": "MEMORY.md",
        "summary": "Direct-written id-less row",
        "rationale": "Replicates the live ledger flood shape.",
        "evidence_paths": ["daily/2026-06-09.md"],
        "proposed_content": "Lesson: id-less ledger rows apply exactly once.",
        "confidence_score": 0.9,
        "status": "pending",
    }
    ledger_path.write_text(
        json.dumps(raw_record, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    ledger = ProposalLedger(ledger_path)

    process_amendment_output(
        "",
        ledger,
        memory_dir,
        default_source="memory_reflect",
        apply_limit=3,
        section_cap=20,
    )
    after_run_1 = memory_file.read_bytes()
    assert after_run_1.decode("utf-8").count(_AMENDMENT_MARKER) == 1

    process_amendment_output(
        "",
        ledger,
        memory_dir,
        default_source="memory_reflect",
        apply_limit=3,
        section_cap=20,
    )
    after_run_2 = memory_file.read_bytes()

    assert after_run_2 == after_run_1
    statuses = [proposal.status for proposal in ledger.read_all()]
    assert "pending" not in statuses
    assert "applied" in statuses


# =============================================================================
# PRODUCER SOURCE CONTRACTS — call sites pass bounds + lock + gate ledger
# =============================================================================


def test_producer_call_sites_pass_limits() -> None:
    """Every process_amendment_output call passes apply_limit= under ledger_file_lock."""
    total_call_sites = 0
    for module in _PRODUCER_MODULES:
        source = inspect.getsource(module)
        positions = [
            match.start()
            for match in re.finditer(r"process_amendment_output\(", source)
        ]
        assert positions, (
            f"{module.__name__}: no process_amendment_output call found"
        )
        for pos in positions:
            call_window = source[pos : pos + 600]
            assert "apply_limit=" in call_window, (
                f"{module.__name__}: process_amendment_output call at offset "
                f"{pos} does not pass apply_limit="
            )
            assert "section_cap=" in call_window, (
                f"{module.__name__}: process_amendment_output call at offset "
                f"{pos} does not pass section_cap="
            )
            lock_window = source[max(0, pos - 300) : pos + 600]
            assert "ledger_file_lock(" in lock_window, (
                f"{module.__name__}: process_amendment_output call at offset "
                f"{pos} is not wrapped in the reentrant ledger_file_lock "
                "(plain shared.file_lock deadlocks against the nested "
                "ledger mutations)"
            )
        total_call_sites += len(positions)
    assert total_call_sites == 4


def test_gate_sections_pass_ledger() -> None:
    """Every build_amendment_gate_section call passes ledger= for the digest."""
    for module in _PRODUCER_MODULES:
        source = inspect.getsource(module)
        positions = [
            match.start()
            for match in re.finditer(r"build_amendment_gate_section\(", source)
        ]
        assert positions, (
            f"{module.__name__}: no build_amendment_gate_section call found"
        )
        for pos in positions:
            call_window = source[pos : pos + 400]
            assert "ledger=" in call_window, (
                f"{module.__name__}: build_amendment_gate_section call at "
                f"offset {pos} does not pass ledger="
            )


# =============================================================================
# RULE 1 — gate helpers use the None sentinel, resolved at call time
# =============================================================================

_GATE_HELPERS = (
    (memory_reflect, "_assemble_reflect_amendment_section"),
    (memory_weekly, "_assemble_weekly_amendment_section"),
    (memory_dream, "_assemble_dream_amendment_section"),
)


def test_amendment_section_helpers_use_none_sentinel() -> None:
    """FIX-6: ledger_file must not bind AMENDMENT_LEDGER_FILE at def time."""
    for module, helper_name in _GATE_HELPERS:
        helper = getattr(module, helper_name)
        parameter = inspect.signature(helper).parameters["ledger_file"]
        assert parameter.default is None, (
            f"{module.__name__}.{helper_name}: ledger_file default must be the "
            f"None sentinel (Rule 1), got {parameter.default!r}"
        )


def test_amendment_section_helpers_resolve_sentinel_at_call_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runtime overrides of AMENDMENT_LEDGER_FILE must reach the gate prompt.

    With the old def-time default this fails: the section would still embed
    the path bound at import. The tmp ledger file never exists, so the gate
    builder's digest read is a no-op — nothing touches the live ledger.
    """
    ledger_file = tmp_path / "amendment-proposals.jsonl"
    for module, helper_name in _GATE_HELPERS:
        monkeypatch.setattr(module, "AMENDMENT_LEDGER_FILE", ledger_file)
        section = getattr(module, helper_name)()
        assert str(ledger_file) in section, (
            f"{module.__name__}.{helper_name}: monkeypatched ledger path did "
            "not reach the rendered gate section (def-time binding regression)"
        )
