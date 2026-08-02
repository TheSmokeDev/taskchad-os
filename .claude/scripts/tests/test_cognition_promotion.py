"""Tests for cognition.promotion — promotion pipeline."""

from __future__ import annotations

from pathlib import Path

import pytest
from cognition import steps
from cognition.promotion import (
    _batch_distill,
    _is_duplicate,
    _normalize_line,
    _passes_quality_gate,
    _rejection_reason,
    run_promotion_pipeline,
)
from cognition.staging import StagingCandidate, StagingStore


def _make_candidate(**kwargs) -> StagingCandidate:
    """Helper to create a StagingCandidate with defaults."""
    defaults = {
        "source_turn": "test:1",
        "candidate_type": "fact",
        "observation": "Test observation",
        "dedupe_key": "test-key",
        "promotion_target": "MEMORY.md",
        "confidence": 0.8,
        "evidence_count": 3,
    }
    defaults.update(kwargs)
    return StagingCandidate(**defaults)


def test_quality_gate_passes():
    c = _make_candidate(confidence=0.8, evidence_count=3)
    assert _passes_quality_gate(c) is True


def test_quality_gate_low_confidence():
    c = _make_candidate(confidence=0.5, evidence_count=3)
    assert _passes_quality_gate(c) is False


def test_quality_gate_low_evidence():
    c = _make_candidate(confidence=0.9, evidence_count=1)
    assert _passes_quality_gate(c) is False


def test_quality_gate_both_low():
    c = _make_candidate(confidence=0.3, evidence_count=0)
    assert _passes_quality_gate(c) is False


def test_quality_gate_threshold_exact():
    """Exactly at threshold should pass."""
    c = _make_candidate(confidence=0.7, evidence_count=2)
    assert _passes_quality_gate(c) is True


def test_rejection_reason_confidence():
    c = _make_candidate(confidence=0.3, evidence_count=5)
    assert "low_confidence" in _rejection_reason(c)


def test_rejection_reason_evidence():
    c = _make_candidate(confidence=0.9, evidence_count=1)
    assert "low_evidence" in _rejection_reason(c)


def test_is_duplicate_exact():
    assert _is_duplicate("Server runs on port 7888", "- Server runs on port 7888\n") is True


def test_is_duplicate_case_insensitive():
    assert _is_duplicate("HELLO WORLD", "- hello world\n") is True


def test_is_not_duplicate():
    assert _is_duplicate("New unique fact", "Existing content here") is False


def test_is_duplicate_empty_text():
    """Empty text is considered duplicate (no-op)."""
    assert _is_duplicate("", "any content") is True
    assert _is_duplicate("   ", "any content") is True


def test_is_duplicate_substring_false_positive_prevented():
    """A short new fact that is a substring of an unrelated, longer existing
    sentence must NOT be flagged as a duplicate (issue #167 Finding 2)."""
    existing = "- The operator prefers concise fixes over long explanations\n"
    assert _is_duplicate("operator prefers concise fixes", existing) is False


def test_is_duplicate_identical_bullet_detected():
    """A genuinely identical bullet line is still detected as a duplicate."""
    existing = "- The operator prefers concise fixes over long explanations\n"
    assert _is_duplicate(
        "The operator prefers concise fixes over long explanations", existing
    ) is True


def test_staging_mark_promoted(tmp_path: Path):
    store = StagingStore(tmp_path / "staging.jsonl")
    c = _make_candidate(dedupe_key="promo-test")
    store.append(c)

    unpromoted = store.read_unpromoted()
    assert len(unpromoted) == 1

    cid = unpromoted[0].id
    assert store.mark_promoted(cid, "MEMORY.md") is True
    assert len(store.read_unpromoted()) == 0


def test_staging_mark_rejected(tmp_path: Path):
    store = StagingStore(tmp_path / "staging.jsonl")
    c = _make_candidate(dedupe_key="reject-test")
    store.append(c)

    unpromoted = store.read_unpromoted()
    cid = unpromoted[0].id
    assert store.mark_rejected(cid, "low_confidence") is True
    assert len(store.read_unpromoted()) == 0


def test_staging_read_unpromoted(tmp_path: Path):
    store = StagingStore(tmp_path / "staging.jsonl")
    for i in range(3):
        store.append(_make_candidate(
            dedupe_key=f"fact-{i}",
            observation=f"Fact {i}",
        ))
    assert len(store.read_unpromoted()) == 3

    cid = store.read_unpromoted()[0].id
    store.mark_rejected(cid, "test rejection")
    assert len(store.read_unpromoted()) == 2


def test_mark_nonexistent_id(tmp_path: Path):
    store = StagingStore(tmp_path / "staging.jsonl")
    store.append(_make_candidate(dedupe_key="exists"))
    assert store.mark_promoted("nonexistent-id", "MEMORY.md") is False


def test_empty_staging_read_unpromoted(tmp_path: Path):
    store = StagingStore(tmp_path / "staging.jsonl")
    assert store.read_unpromoted() == []


def test_self_model_floor_one_unchanged():
    """self_model candidates still promote at evidence_count=1 (unchanged)."""
    c = _make_candidate(candidate_type="self_model", confidence=0.8, evidence_count=1)
    assert _passes_quality_gate(c) is True


@pytest.mark.asyncio
async def test_low_confidence_still_rejected(tmp_path: Path):
    store = StagingStore(tmp_path / "staging.jsonl")
    c = _make_candidate(confidence=0.2, evidence_count=5, dedupe_key="bad-conf")
    store.append(c)

    results = await run_promotion_pipeline(store, tmp_path, tmp_path)

    assert len(results) == 1
    assert results[0].action == "rejected"
    assert "low_confidence" in results[0].reason
    assert store.read_unpromoted() == []  # permanently rejected, not pending


@pytest.mark.asyncio
async def test_low_evidence_deferred_not_rejected(tmp_path: Path):
    store = StagingStore(tmp_path / "staging.jsonl")
    c = _make_candidate(confidence=0.9, evidence_count=1, dedupe_key="one-obs")
    store.append(c)

    results = await run_promotion_pipeline(store, tmp_path, tmp_path)

    assert len(results) == 1
    assert results[0].action == "deferred"
    assert "low_evidence" in results[0].reason
    # Still pending — NOT marked rejected, so it can re-qualify later.
    assert len(store.read_unpromoted()) == 1


@pytest.mark.asyncio
async def test_evidence_accumulates_then_promotes(tmp_path: Path, monkeypatch):
    """Same candidate observed in 2 sessions -> evidence_count=2 -> promotes."""
    from cognition.steps import ReasoningStepResult

    async def _fake_reasoning_step(context, instruction, output_schema=None, cwd=None):
        return ReasoningStepResult(
            output_text="[]", parsed=[{"i": 0, "text": "The bot's port is 7888."}],
            model="fake", cost_usd=0.0, latency_ms=0.0,
        )

    monkeypatch.setattr(steps, "reasoning_step", _fake_reasoning_step)

    store = StagingStore(tmp_path / "staging.jsonl")
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    # Session 1: first observation — evidence_count=1, not yet eligible.
    store.append(StagingCandidate(
        source_turn="s1:1", candidate_type="fact", observation="The bot's port is 7888",
        dedupe_key="bot-port-7888", promotion_target="MEMORY.md", confidence=0.8,
    ))
    first_run = await run_promotion_pipeline(store, memory_dir, tmp_path)
    assert first_run[0].action == "deferred"

    # Session 2: same observation repeated -> merges to evidence_count=2.
    store.append(StagingCandidate(
        source_turn="s2:1", candidate_type="fact", observation="The bot's port is 7888",
        dedupe_key="bot-port-7888", promotion_target="MEMORY.md", confidence=0.8,
    ))
    assert store.read_unpromoted()[0].evidence_count == 2

    second_run = await run_promotion_pipeline(store, memory_dir, tmp_path)
    assert len(second_run) == 1
    assert second_run[0].action == "promoted"
    assert (memory_dir / "MEMORY.md").exists()
    assert "The bot's port is 7888" in (memory_dir / "MEMORY.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_migration_runs_before_load(tmp_path: Path, monkeypatch):
    import json
    from dataclasses import asdict

    from cognition.steps import ReasoningStepResult

    async def _fake_reasoning_step(context, instruction, output_schema=None, cwd=None):
        return ReasoningStepResult(
            output_text="[]", parsed=[{"i": 0, "text": "distilled"}],
            model="fake", cost_usd=0.0, latency_ms=0.0,
        )

    monkeypatch.setattr(steps, "reasoning_step", _fake_reasoning_step)

    path = tmp_path / "staging.jsonl"
    legacy = StagingCandidate(
        source_turn="test:1", candidate_type="fact", observation="legacy stuck fact",
        dedupe_key="legacy-stuck", promotion_target="MEMORY.md",
        confidence=0.9, evidence_count=2,  # now above floor once un-rejected
        rejected=True, rejected_reason="low_evidence (1 < 2)",
    )
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(legacy)) + "\n")

    store = StagingStore(path)
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    results = await run_promotion_pipeline(store, memory_dir, tmp_path)

    assert len(results) == 1
    assert results[0].action == "promoted"


@pytest.mark.asyncio
async def test_mixed_batch_three_way_split(tmp_path: Path, monkeypatch):
    """One eligible, one low_evidence, one low_confidence in the SAME run ->
    exactly one promoted / deferred / rejected, correctly attributed."""
    from cognition.steps import ReasoningStepResult

    async def _fake_reasoning_step(context, instruction, output_schema=None, cwd=None):
        return ReasoningStepResult(
            output_text="[]", parsed=[{"i": 0, "text": "distilled A"}],
            model="fake", cost_usd=0.0, latency_ms=0.0,
        )

    monkeypatch.setattr(steps, "reasoning_step", _fake_reasoning_step)

    store = StagingStore(tmp_path / "staging.jsonl")
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    ok = _make_candidate(confidence=0.9, evidence_count=3, dedupe_key="ok")
    low_ev = _make_candidate(confidence=0.9, evidence_count=1, dedupe_key="low-ev")
    low_conf = _make_candidate(confidence=0.1, evidence_count=5, dedupe_key="low-conf")
    store.append(ok)
    store.append(low_ev)
    store.append(low_conf)

    results = await run_promotion_pipeline(store, memory_dir, tmp_path)
    by_id = {r.candidate_id: r for r in results}

    assert by_id[ok.id].action == "promoted"
    assert by_id[low_ev.id].action == "deferred"
    assert by_id[low_conf.id].action == "rejected"


@pytest.mark.asyncio
async def test_dry_run_never_mutates_staging(tmp_path: Path, monkeypatch):
    """Codex gate MAJOR + Kimi gate MAJOR on PR #176: a dry_run pipeline
    (reflection --test) must leave the staging file byte-identical across
    EVERY write path — the unreject_low_evidence migration, both
    mark_rejected sites, AND the mark_promoted flip (Kimi: the fixtures must
    actually reach the distill/promote path, or the invariant ships unproven
    for the write that matters most)."""
    from cognition.steps import ReasoningStepResult

    async def _fake_reasoning_step(context, instruction, output_schema=None, cwd=None):
        return ReasoningStepResult(
            output_text="[]", parsed=[{"i": 0, "text": "An eligible distilled claim."}],
            model="fake", cost_usd=0.0, latency_ms=0.0,
        )

    monkeypatch.setattr(steps, "reasoning_step", _fake_reasoning_step)

    staging_path = tmp_path / "staging.jsonl"
    store = StagingStore(staging_path)
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    legacy = StagingCandidate(
        source_turn="t:1", candidate_type="fact", observation="legacy stuck row",
        confidence=0.9, promotion_target="MEMORY.md", dedupe_key="legacy-key",
    )
    store.append(legacy)
    store.mark_rejected(legacy.id, "low_evidence (1 < 2)")

    low_conf = StagingCandidate(
        source_turn="t:2", candidate_type="fact", observation="shaky claim",
        confidence=0.1, evidence_count=5, promotion_target="MEMORY.md",
        dedupe_key="shaky-key",
    )
    store.append(low_conf)

    # ELIGIBLE candidate — passes the quality gate, reaches distill, and
    # would hit mark_promoted on a real run.
    eligible = StagingCandidate(
        source_turn="t:3", candidate_type="fact", observation="an eligible claim",
        confidence=0.9, evidence_count=2, promotion_target="MEMORY.md",
        dedupe_key="eligible-key",
    )
    store.append(eligible)

    before = staging_path.read_bytes()
    results = await run_promotion_pipeline(store, memory_dir, tmp_path, dry_run=True)
    after = staging_path.read_bytes()

    assert after == before
    # The eligible candidate DID traverse the promote path in the report...
    assert any(r.action == "promoted" for r in results)
    # ...but nothing physical happened: no target write, no promoted flip.
    assert not (memory_dir / "MEMORY.md").exists()
    assert any(c.id == eligible.id for c in store.read_unpromoted())


@pytest.mark.asyncio
async def test_batch_distill_middle_item_dropped_no_cross_contamination(tmp_path: Path, monkeypatch):
    """5-candidate batch, LLM returns 4 items with the middle one (i=2)
    missing -> 4 correct placements + 1 fallback, no cross-contamination
    (issue #167 Finding 1)."""
    from cognition.steps import ReasoningStepResult

    candidates = [
        _make_candidate(observation=f"Observation {i}", dedupe_key=f"mid-drop-{i}")
        for i in range(5)
    ]

    async def _fake_reasoning_step(context, instruction, output_schema=None, cwd=None):
        # Middle item (i=2) is dropped by the LLM.
        return ReasoningStepResult(
            output_text="[]",
            parsed=[
                {"i": 0, "text": "distilled-0"},
                {"i": 1, "text": "distilled-1"},
                {"i": 3, "text": "distilled-3"},
                {"i": 4, "text": "distilled-4"},
            ],
            model="fake", cost_usd=0.0, latency_ms=0.0,
        )

    monkeypatch.setattr(steps, "reasoning_step", _fake_reasoning_step)

    distilled = await _batch_distill(candidates, tmp_path)

    assert distilled[candidates[0].id] == "distilled-0"
    assert distilled[candidates[1].id] == "distilled-1"
    # Dropped middle item falls back to its own raw observation, never a
    # neighbor's text.
    assert distilled[candidates[2].id] == "Observation 2"
    assert distilled[candidates[3].id] == "distilled-3"
    assert distilled[candidates[4].id] == "distilled-4"


@pytest.mark.asyncio
async def test_pipeline_middle_item_dropped_no_cross_contamination(tmp_path: Path, monkeypatch):
    """End-to-end: same middle-drop scenario through run_promotion_pipeline —
    each candidate's promoted content must match its OWN distilled text (or
    fallback), never a neighbor's, and each lands under its own id."""
    from cognition.steps import ReasoningStepResult

    async def _fake_reasoning_step(context, instruction, output_schema=None, cwd=None):
        return ReasoningStepResult(
            output_text="[]",
            parsed=[
                {"i": 0, "text": "distilled-0"},
                {"i": 1, "text": "distilled-1"},
                {"i": 3, "text": "distilled-3"},
                {"i": 4, "text": "distilled-4"},
            ],
            model="fake", cost_usd=0.0, latency_ms=0.0,
        )

    monkeypatch.setattr(steps, "reasoning_step", _fake_reasoning_step)

    store = StagingStore(tmp_path / "staging.jsonl")
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    candidates = [
        _make_candidate(observation=f"Observation {i}", dedupe_key=f"pipe-mid-drop-{i}")
        for i in range(5)
    ]
    for c in candidates:
        store.append(c)

    results = await run_promotion_pipeline(store, memory_dir, tmp_path)
    by_id = {r.candidate_id: r for r in results}

    assert len(results) == 5
    assert by_id[candidates[0].id].distilled_text == "distilled-0"
    assert by_id[candidates[1].id].distilled_text == "distilled-1"
    assert by_id[candidates[2].id].distilled_text == "Observation 2"  # fallback
    assert by_id[candidates[3].id].distilled_text == "distilled-3"
    assert by_id[candidates[4].id].distilled_text == "distilled-4"
    assert all(r.action == "promoted" for r in results)


@pytest.mark.asyncio
async def test_batch_distill_reasoning_step_raises_falls_back_to_raw(tmp_path: Path, monkeypatch):
    """If reasoning_step raises, every candidate falls back to its own raw
    observation — a distillation outage must never crash the pipeline."""

    async def _fake_reasoning_step(context, instruction, output_schema=None, cwd=None):
        raise RuntimeError("provider timeout")

    monkeypatch.setattr(steps, "reasoning_step", _fake_reasoning_step)

    candidates = [
        _make_candidate(observation=f"Observation {i}", dedupe_key=f"raise-{i}")
        for i in range(3)
    ]

    distilled = await _batch_distill(candidates, tmp_path)

    for c in candidates:
        assert distilled[c.id] == c.observation


@pytest.mark.asyncio
async def test_batch_distill_non_list_parsed_falls_back_to_raw(tmp_path: Path, monkeypatch):
    """A response that isn't a JSON array (e.g. parsed=None or a dict) must
    fall back to raw observations, not raise or silently drop candidates."""
    from cognition.steps import ReasoningStepResult

    async def _fake_reasoning_step(context, instruction, output_schema=None, cwd=None):
        return ReasoningStepResult(
            output_text="{}", parsed=None, model="fake", cost_usd=0.0, latency_ms=0.0,
        )

    monkeypatch.setattr(steps, "reasoning_step", _fake_reasoning_step)

    candidates = [_make_candidate(observation="Solo observation", dedupe_key="non-list")]

    distilled = await _batch_distill(candidates, tmp_path)

    assert distilled[candidates[0].id] == "Solo observation"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_item",
    [
        pytest.param({"i": True, "text": "distilled-1-bool"}, id="bool_index"),
        pytest.param({"i": -1, "text": "distilled-neg"}, id="negative_index"),
        pytest.param({"i": 99, "text": "distilled-oob"}, id="out_of_range_index"),
        pytest.param({"i": 1, "text": None}, id="none_text"),
        pytest.param("distilled-1-legacy-string", id="non_dict_item"),
        # Codex gate #180: empty/whitespace/non-string text must keep the raw
        # fallback — NOT persist "" (which _is_duplicate treats as a dup and
        # permanently drops the candidate) and NOT str()-ify a list/dict/number
        # into durable memory.
        pytest.param({"i": 1, "text": ""}, id="empty_text"),
        pytest.param({"i": 1, "text": "   "}, id="whitespace_text"),
        pytest.param({"i": 1, "text": ["B1", "B2"]}, id="list_text"),
        pytest.param({"i": 1, "text": {"claim": "B"}}, id="dict_text"),
        pytest.param({"i": 1, "text": 42}, id="number_text"),
    ],
)
async def test_batch_distill_malformed_item_falls_back_not_corrupts(
    tmp_path: Path, monkeypatch, bad_item
):
    """A malformed item (bool index, out-of-range index, None/empty/whitespace
    text, non-string text, or a non-dict entry) is skipped — its candidate
    keeps its RAW observation (never "", never a stringified structure) and no
    OTHER candidate's slot is touched or corrupted."""
    from cognition.steps import ReasoningStepResult

    candidates = [
        _make_candidate(observation=f"Observation {i}", dedupe_key=f"malformed-{i}")
        for i in range(3)
    ]

    async def _fake_reasoning_step(context, instruction, output_schema=None, cwd=None):
        return ReasoningStepResult(
            output_text="[]",
            parsed=[
                {"i": 0, "text": "distilled-0"},
                bad_item,
                {"i": 2, "text": "distilled-2"},
            ],
            model="fake", cost_usd=0.0, latency_ms=0.0,
        )

    monkeypatch.setattr(steps, "reasoning_step", _fake_reasoning_step)

    distilled = await _batch_distill(candidates, tmp_path)

    assert distilled[candidates[0].id] == "distilled-0"
    assert distilled[candidates[1].id] == "Observation 1"  # fallback, untouched
    assert distilled[candidates[2].id] == "distilled-2"


@pytest.mark.asyncio
async def test_batch_distill_duplicate_index_drops_both_to_raw(tmp_path: Path, monkeypatch):
    """Codex gate BLOCKER on PR #180: two response items claiming the same 'i'
    must DISTRUST BOTH (the LLM mislabeled at least one), so the collided
    candidate reverts to its RAW observation instead of last-write-wins
    silently attributing 'second' onto candidate 0. A well-formed candidate in
    the same batch is unaffected."""
    from cognition.steps import ReasoningStepResult

    candidates = [
        _make_candidate(observation=f"Observation {i}", dedupe_key=f"dup-{i}")
        for i in range(2)
    ]

    async def _fake_reasoning_step(context, instruction, output_schema=None, cwd=None):
        return ReasoningStepResult(
            output_text="[]",
            parsed=[
                {"i": 0, "text": "first"},
                {"i": 0, "text": "second"},
                {"i": 1, "text": "distilled-1"},
            ],
            model="fake", cost_usd=0.0, latency_ms=0.0,
        )

    monkeypatch.setattr(steps, "reasoning_step", _fake_reasoning_step)

    distilled = await _batch_distill(candidates, tmp_path)

    # Collided candidate reverts to raw — NOT "second".
    assert distilled[candidates[0].id] == "Observation 0"
    # A third occurrence of the same index stays at raw (idempotent).
    assert distilled[candidates[1].id] == "distilled-1"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("- Server runs on port 7888", "server runs on port 7888"),
        ("* Server runs on port 7888", "server runs on port 7888"),
        ("+ Server runs on port 7888", "server runs on port 7888"),
        ("Server runs on port 7888...", "server runs on port 7888"),
        ("Server  runs\ton   port 7888!?", "server runs on port 7888"),
        # Codex gate #180 MINOR: ordered + task-box bullet forms normalize too.
        ("1. Server runs on port 7888", "server runs on port 7888"),
        ("2) Server runs on port 7888", "server runs on port 7888"),
        ("- [x] Server runs on port 7888", "server runs on port 7888"),
        ("- [ ] Server runs on port 7888", "server runs on port 7888"),
    ],
)
def test_normalize_line_variants(raw, expected):
    assert _normalize_line(raw) == expected


def test_is_duplicate_multiline_unit_consistency():
    """Codex gate MAJOR on PR #180: both sides split into physical lines and
    normalize identically. A genuine multi-line duplicate is caught; a
    distinct two-fact unit vs a one-line combined fact is NOT falsely
    rejected."""
    # Genuine duplicate — every line already present (was a FALSE NEGATIVE).
    assert _is_duplicate("Alpha\nBeta", "- Alpha\n- Beta\n") is True
    # Two separate facts vs one combined line — NOT a duplicate
    # (was a FALSE POSITIVE under whole-text collapse).
    assert _is_duplicate("Alpha\nBeta", "- Alpha Beta\n") is False
    # Single-line unit still works both ways.
    assert _is_duplicate("Alpha", "- Alpha\n") is True
    assert _is_duplicate("Gamma", "- Alpha\n- Beta\n") is False


def test_is_duplicate_substring_is_not_a_duplicate():
    """The headline #167 fix: a short new fact that is a substring of an
    unrelated longer existing line is NOT a duplicate (line-level, not
    whole-file substring)."""
    existing = "- prefers concise fixes over long explanations when debugging\n"
    assert _is_duplicate("operator prefers concise fixes", existing) is False
