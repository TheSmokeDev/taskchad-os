"""Dedicated keystone tests for ``cognition.belief_conflicts`` (Living Self Act 2).

Fills the gaps NOT already covered by ``test_living_self_act2.py`` (which proves
the cosine pre-filter band, max_eligible bound, the judge fail-open/tolerant
parse, every ``_decide_loser`` provenance pairing, and B2 cross-run idempotency).
This file pins three remaining behaviors:

  - ``judge_contradictions`` prompt hardening — the untrusted belief text is
    whitespace-collapsed AND length-capped (the ``_safe`` defense) before it goes
    into the numbered one-pair-per-line judge prompt, and the judge's own
    self-conflict output (a_id == b_id) is filtered out of the returned conflicts.
  - ``apply_contradictions`` B1 HELD path — explicit-vs-explicit records tension
    on BOTH records (held=True, neither confidence drops), and the winner's audit
    points at the loser (symmetric).
  - ``apply_contradictions`` empty-conflicts no-op.

The LLM JUDGE is mocked at its documented seam (the injected ``reasoning``
callable that stands in for ``reasoning_step`` -> ``run_with_runtime_lanes``).
Embeddings are injected deterministically. Born-clean, tmp_path-scoped.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for _p in (str(_SCRIPTS_DIR), str(_CHAT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cognition import belief_conflicts as bc  # noqa: E402
from cognition.self_model import InferenceRecord, InferenceTracker  # noqa: E402

import config  # noqa: E402


def _rec(rid, text, *, source="reflection", confidence=0.8, evidence_count=1,
         status="active", last_updated="2026-06-13T00:00:00+00:00",
         contradicted_by=None):
    return InferenceRecord(
        id=rid,
        inference=text,
        observation=text,
        confidence=confidence,
        evidence_count=evidence_count,
        contradiction_count=len(contradicted_by or []),
        contradicted_by=list(contradicted_by or []),
        first_seen=last_updated,
        last_updated=last_updated,
        source=source,
        status=status,
    )


def _settings(**kw):
    base = dict(
        enabled=True,
        pair_min_cosine=0.45,
        pair_max_cosine=0.72,
        max_pairs=20,
        max_eligible=100,
        min_records=2,
        allow_explicit_vs_explicit=False,
    )
    base.update(kw)
    return config.ContradictionSettings(**base)


def _seed(path, records):
    InferenceTracker(path).save(records)


def _run(coro):
    return asyncio.run(coro)


# ===========================================================================
# judge_contradictions — prompt hardening (the _safe defense) + self-conflict filter
# ===========================================================================


def test_judge_collapses_and_caps_untrusted_belief_text():
    """The belief text fed to the judge is newline-collapsed AND length-capped.

    A multi-line belief could otherwise break the numbered one-pair-per-line
    format; a wall-of-text belief could crowd the instruction. The gate's _safe
    collapses whitespace and caps each belief to 300 chars BEFORE building the
    prompt — proven by capturing the context the judge actually receives.
    """
    captured = {}

    async def reasoning(context, instruction, **_k):
        captured["context"] = context
        captured["instruction"] = instruction
        return SimpleNamespace(parsed=[], model="x")

    multiline = "line one\nline two\n\tindented three"
    wall = "Z" * 1000  # exceeds the 300-char cap
    a = _rec("x", multiline)
    b = _rec("y", wall)
    _run(bc.judge_contradictions([(a, b)], cwd=Path("."), settings=_settings(),
                                 reasoning=reasoning))
    ctx = captured["context"]
    # newlines inside the belief are gone (collapsed to single spaces) — the
    # pair stays on ONE prompt line. The only newlines are the line separators
    # the builder itself emits between the header and the single pair line.
    assert "line one line two indented three" in ctx
    # the wall-of-text belief is capped to 300 chars — the full 1000 Z run is NOT
    # present, but a 300-Z run is.
    assert "Z" * 300 in ctx
    assert "Z" * 301 not in ctx


def test_judge_filters_self_conflict_a_equals_b():
    """A judge that returns a_id == b_id (a self-conflict) is filtered out."""
    a = _rec("x", "alpha")
    b = _rec("y", "beta")
    parsed = [
        {"a_id": "x", "b_id": "x", "reason": "self"},   # a==b -> dropped
        {"a_id": "x", "b_id": "y", "reason": "real"},   # valid -> kept
    ]

    async def reasoning(*_a, **_k):
        return SimpleNamespace(parsed=parsed, model="x")

    out = _run(bc.judge_contradictions([(a, b)], cwd=Path("."), settings=_settings(),
                                       reasoning=reasoning))
    assert len(out) == 1
    assert out[0]["a_id"] == "x" and out[0]["b_id"] == "y"


def test_judge_drops_non_dict_conflict_items():
    """Junk (non-dict) items in the judge array never reach the conflict list."""
    a = _rec("x", "alpha")
    b = _rec("y", "beta")
    parsed = ["a bare string", 7, None, {"a_id": "x", "b_id": "y", "reason": "ok"}]

    async def reasoning(*_a, **_k):
        return SimpleNamespace(parsed=parsed, model="x")

    out = _run(bc.judge_contradictions([(a, b)], cwd=Path("."), settings=_settings(),
                                       reasoning=reasoning))
    assert out == [{"a_id": "x", "b_id": "y", "reason": "ok"}]


# ===========================================================================
# apply_contradictions — B1 HELD path records tension on BOTH records
# ===========================================================================


def test_apply_held_records_tension_on_both_records(tmp_path):
    """Explicit-vs-explicit: BOTH records get a contradicted_by audit, NEITHER drops."""
    path = tmp_path / "inf.json"
    _seed(path, [
        _rec("e1", "alpha", source="explicit", confidence=0.8),
        _rec("e2", "beta", source="explicit", confidence=0.8),
    ])
    conflicts = [{"a_id": "e1", "b_id": "e2", "reason": "opposed"}]
    n = bc.apply_contradictions(conflicts, path, settings=_settings())
    assert n == 2  # both records moved (tension recorded on each)
    by_id = {r.id: r for r in InferenceTracker(path).load()}
    # confidence UNCHANGED on both (held=True path never applies -0.15)
    assert abs(by_id["e1"].confidence - 0.8) < 1e-9
    assert abs(by_id["e2"].confidence - 0.8) < 1e-9
    # symmetric audit: each record's contradicted_by points at the OTHER's id
    assert by_id["e1"].contradicted_by and by_id["e1"].contradicted_by[0].startswith("e2:")
    assert by_id["e2"].contradicted_by and by_id["e2"].contradicted_by[0].startswith("e1:")
    assert by_id["e1"].contradiction_count == 1
    assert by_id["e2"].contradiction_count == 1


def test_apply_held_path_idempotent_on_rerun(tmp_path):
    """A second identical run over an already-held pair is a no-op (B2 via the audit key)."""
    path = tmp_path / "inf.json"
    _seed(path, [
        _rec("e1", "alpha", source="explicit", confidence=0.8),
        _rec("e2", "beta", source="explicit", confidence=0.8),
    ])
    conflicts = [{"a_id": "e1", "b_id": "e2", "reason": "opposed"}]
    first = bc.apply_contradictions(conflicts, path, settings=_settings())
    second = bc.apply_contradictions(conflicts, path, settings=_settings())
    assert first == 2
    assert second == 0  # already held vs this winner -> skipped, never flaps
    by_id = {r.id: r for r in InferenceTracker(path).load()}
    # exactly ONE audit entry each (no duplicate appended on the rerun)
    assert len(by_id["e1"].contradicted_by) == 1
    assert len(by_id["e2"].contradicted_by) == 1


def test_apply_empty_conflicts_is_noop(tmp_path):
    path = tmp_path / "inf.json"
    _seed(path, [_rec("r", "alpha", source="reflection", confidence=0.8)])
    assert bc.apply_contradictions([], path, settings=_settings()) == 0
    assert InferenceTracker(path).load()[0].confidence == 0.8


def test_apply_explicit_vs_reflection_only_reflection_drops(tmp_path):
    """Cross-provenance: only the reflection's confidence drops; the explicit is untouched."""
    path = tmp_path / "inf.json"
    _seed(path, [
        _rec("expl", "alpha", source="explicit", confidence=0.8),
        _rec("refl", "beta", source="reflection", confidence=0.8),
    ])
    n = bc.apply_contradictions(
        [{"a_id": "expl", "b_id": "refl", "reason": "opposed"}], path, settings=_settings()
    )
    assert n == 1  # only the reflection moved
    by_id = {r.id: r for r in InferenceTracker(path).load()}
    assert abs(by_id["expl"].confidence - 0.8) < 1e-9   # explicit sacrosanct
    assert abs(by_id["refl"].confidence - 0.65) < 1e-9  # reflection took -0.15
    assert by_id["expl"].contradicted_by == []          # explicit not even audited
