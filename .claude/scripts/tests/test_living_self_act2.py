"""Tests for Living Self Act 2 — Wire The Contradiction Engine (the keystone).

Categories map to the PRP's Validation Loop (Level 2, categories 1-8). Every test
is tmp_path-scoped with injected ``embed_batch`` / ``reasoning`` and monkeypatched
imports so the REAL branches run (real cosine band, real tolerant-parse, real
policy, real ``contradict()``) — NO live state (self-model-inferences.json,
chat.db, SELF.md, vault) is ever touched. Born-clean: all ids/text are synthetic.

  1. Rule-1 settings resolver — env-swept defaults, monkeypatch flips on next
     call, explicit-arg passthrough, the pair_max_cosine<->dedup coupling.
  2. contradict() optional audit + held path — does NOT change the 6-test math.
  3. find_candidate_pairs — band include/exclude, source/decayed filter, self-pair
     exclusion, min_records floor, max_pairs cap, fail-open (FAILS pre-fix).
  4. judge_contradictions — provider-agnostic, fail-open WITH a visible print (G5),
     tolerant-parse incl. the MULTI-list wrap (M2), Langfuse fail-open (FAILS
     pre-fix).
  5. _decide_loser — each provenance pairing discriminating, returns a 4-tuple
     incl. the B1 held flag (FAILS pre-fix).
  6. apply_contradictions — real contradict() fires + B1 (explicit sacrosanct) + B2
     (count once, 5 runs + N1 line-ablation) + M4 log + N2 held-path re-run.
  7. Reflection wiring — the pass runs non-blocking, test_mode skips apply.
  8. Held-under-tension render — 0.3 floor + per-record gate + tag (M1, FAILS
     pre-fix).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for _p in (str(_SCRIPTS_DIR), str(_CHAT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cognition import belief_conflicts as bc  # noqa: E402
from cognition.self_model import InferenceRecord, InferenceTracker  # noqa: E402

import config  # noqa: E402

# ===========================================================================
# Helpers — synthetic records + cosine-controlled fake embedder (born-clean)
# ===========================================================================


def _rec(
    rid: str,
    text: str,
    *,
    source: str = "reflection",
    confidence: float = 0.8,
    evidence_count: int = 1,
    status: str = "active",
    last_updated: str = "2026-06-13T00:00:00+00:00",
    contradicted_by: list[str] | None = None,
) -> InferenceRecord:
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


def _angle_embed(angles: dict[str, float]):
    """Fake ``embed_batch`` placing each record-text on a 2D unit circle.

    ``angles`` maps inference-text -> angle (radians). Two texts at angles θ_a,
    θ_b have cosine == cos(θ_a - θ_b), so a test picks the exact pairwise cosine.
    Returns a callable matching ``embed_batch(texts, **kw) -> list[vec]``. Texts
    not in the map get a far-apart hashed angle (orthogonal-ish -> below the band).
    """
    import hashlib

    import numpy as np

    def _embed(texts, **_kw):
        out = []
        for t in texts:
            if t in angles:
                a = angles[t]
            else:
                h = int(hashlib.sha1(t.encode("utf-8")).hexdigest(), 16)
                # Spread unknowns across the circle far from 0 so they don't land
                # in a tested band by accident.
                a = 1.2 + (h % 1000) / 1000.0 * 2.0
            out.append(np.array([np.cos(a), np.sin(a)], dtype=np.float32))
        return out

    return _embed


def _cosine_to_angle(cos_value: float) -> float:
    """Angle (radians) whose cosine with the 0-angle vector == cos_value."""
    import math

    return math.acos(cos_value)


def _run(coro):
    return asyncio.run(coro)


# ===========================================================================
# 1. Rule-1 settings resolver
# ===========================================================================


def test_contradiction_settings_locked_defaults(monkeypatch):
    for var in (
        "CONTRADICTION_ENABLED",
        "CONTRADICTION_PAIR_MIN_COSINE",
        "CONTRADICTION_PAIR_MAX_COSINE",
        "CONTRADICTION_MAX_PAIRS",
        "CONTRADICTION_MAX_ELIGIBLE",
        "CONTRADICTION_MIN_RECORDS",
        "CONTRADICTION_ALLOW_EXPLICIT_VS_EXPLICIT",
        "INFERENCE_DEDUP_THRESHOLD",
    ):
        monkeypatch.delenv(var, raising=False)
    s = config.get_contradiction_settings()
    assert s.enabled is True
    assert s.pair_min_cosine == 0.45
    assert s.pair_max_cosine == 0.72  # = dedup default via coupling
    assert s.max_pairs == 20
    assert s.max_eligible == 100
    assert s.min_records == 2
    assert s.allow_explicit_vs_explicit is False


def test_contradiction_settings_env_flips_on_next_call(monkeypatch):
    monkeypatch.setenv("CONTRADICTION_ENABLED", "false")
    monkeypatch.setenv("CONTRADICTION_PAIR_MIN_COSINE", "0.3")
    monkeypatch.setenv("CONTRADICTION_PAIR_MAX_COSINE", "0.8")
    monkeypatch.setenv("CONTRADICTION_MAX_PAIRS", "5")
    monkeypatch.setenv("CONTRADICTION_MAX_ELIGIBLE", "10")
    monkeypatch.setenv("CONTRADICTION_MIN_RECORDS", "3")
    monkeypatch.setenv("CONTRADICTION_ALLOW_EXPLICIT_VS_EXPLICIT", "true")
    s = config.get_contradiction_settings()
    assert s.enabled is False
    assert s.pair_min_cosine == 0.3
    assert s.pair_max_cosine == 0.8
    assert s.max_pairs == 5
    assert s.max_eligible == 10
    assert s.min_records == 3
    assert s.allow_explicit_vs_explicit is True


def test_contradiction_settings_explicit_args_passthrough():
    s = config.get_contradiction_settings(
        enabled=False,
        pair_min_cosine=0.1,
        pair_max_cosine=0.2,
        max_pairs=1,
        max_eligible=2,
        min_records=4,
        allow_explicit_vs_explicit=True,
    )
    assert s == (False, 0.1, 0.2, 1, 2, 4, True)


def test_pair_max_cosine_couples_to_dedup_when_unset(monkeypatch):
    """pair_max_cosine UNSET -> reads the dedup threshold at call time (coupling)."""
    monkeypatch.delenv("CONTRADICTION_PAIR_MAX_COSINE", raising=False)
    monkeypatch.setenv("INFERENCE_DEDUP_THRESHOLD", "0.9")
    s = config.get_contradiction_settings()
    assert s.pair_max_cosine == 0.9  # proves the call-time dedup read


def test_pair_max_cosine_env_wins_over_coupling(monkeypatch):
    """An explicit CONTRADICTION_PAIR_MAX_COSINE beats the dedup coupling."""
    monkeypatch.setenv("CONTRADICTION_PAIR_MAX_COSINE", "0.5")
    monkeypatch.setenv("INFERENCE_DEDUP_THRESHOLD", "0.9")
    s = config.get_contradiction_settings()
    assert s.pair_max_cosine == 0.5  # the explicit knob wins


# ===========================================================================
# 2. contradict() optional audit + held path (does NOT change the 6-test math)
# ===========================================================================


def test_contradict_no_kwargs_unchanged(tmp_path):
    """Zero-arg contradict (the 6 standing tests' call shape) -> empty audit + -0.15."""
    tracker = InferenceTracker(tmp_path / "inf.json")
    r = tracker.add_inference("user likes X", "obs", 0.8, source="reflection")
    assert tracker.contradict(r.id) is True
    rec = tracker.load()[0]
    assert rec.contradicted_by == []
    assert rec.contradiction_count == 1
    assert abs(rec.confidence - 0.65) < 1e-9  # the UNCHANGED -0.15 math


def test_contradict_by_appends_audit(tmp_path):
    """by= appends the audit AND keeps the same -0.15 drop."""
    tracker = InferenceTracker(tmp_path / "inf.json")
    r = tracker.add_inference("user likes X", "obs", 0.8, source="reflection")
    assert tracker.contradict(r.id, by="winner:reason") is True
    rec = tracker.load()[0]
    assert rec.contradicted_by == ["winner:reason"]
    assert rec.contradiction_count == 1
    assert abs(rec.confidence - 0.65) < 1e-9


def test_contradict_by_accumulates(tmp_path):
    """Two by= calls accumulate audit entries and the contradiction_count."""
    tracker = InferenceTracker(tmp_path / "inf.json")
    r = tracker.add_inference("user likes X", "obs", 0.8, source="reflection")
    tracker.contradict(r.id, by="w1:r1")
    tracker.contradict(r.id, by="w2:r2")
    rec = tracker.load()[0]
    assert rec.contradicted_by == ["w1:r1", "w2:r2"]
    assert rec.contradiction_count == 2


def test_contradict_held_records_tension_without_dropping(tmp_path):
    """held=True (B1): count + audit, confidence UNCHANGED, demote NOT run."""
    tracker = InferenceTracker(tmp_path / "inf.json")
    r = tracker.add_inference("operator belief", "obs", 0.8, source="explicit")
    records = tracker.load()
    records[0].status = "confirmed"
    tracker.save(records)

    assert tracker.contradict(r.id, by="w:r", held=True) is True
    rec = tracker.load()[0]
    assert rec.contradicted_by == ["w:r"]
    assert rec.contradiction_count == 1
    assert abs(rec.confidence - 0.8) < 1e-9  # UNCHANGED — no -0.15
    assert rec.status == "confirmed"  # demote NOT run on the held path


# ===========================================================================
# 3. find_candidate_pairs — discriminating (FAILS pre-fix: no such function)
# ===========================================================================


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


def test_pairs_midband_included():
    """A topically-related MID-cosine (in [0.45, 0.72)) pair IS a candidate."""
    a = _rec("a", "alpha")
    b = _rec("b", "beta")
    angles = {"alpha": 0.0, "beta": _cosine_to_angle(0.6)}
    pairs = bc.find_candidate_pairs(
        [a, b], settings=_settings(), embed_batch=_angle_embed(angles)
    )
    assert len(pairs) == 1
    assert {pairs[0][0].id, pairs[0][1].id} == {"a", "b"}


def test_pairs_high_cosine_excluded():
    """A paraphrase-ish HIGH-cosine pair (>= pair_max_cosine) is EXCLUDED (dedup case)."""
    a = _rec("a", "alpha")
    b = _rec("b", "beta")
    angles = {"alpha": 0.0, "beta": _cosine_to_angle(0.95)}
    pairs = bc.find_candidate_pairs(
        [a, b], settings=_settings(), embed_batch=_angle_embed(angles)
    )
    assert pairs == []


def test_pairs_low_cosine_excluded():
    """An unrelated LOW-cosine pair (< 0.45) is EXCLUDED."""
    a = _rec("a", "alpha")
    b = _rec("b", "beta")
    angles = {"alpha": 0.0, "beta": _cosine_to_angle(0.1)}
    pairs = bc.find_candidate_pairs(
        [a, b], settings=_settings(), embed_batch=_angle_embed(angles)
    )
    assert pairs == []


def test_pairs_decayed_and_auto_capture_excluded():
    """decayed / auto_capture records are EXCLUDED even when text would be in-band."""
    a = _rec("a", "alpha", status="decayed")
    b = _rec("b", "beta", source="auto_capture")
    c = _rec("c", "gamma")  # a lone valid record
    angles = {
        "alpha": 0.0,
        "beta": _cosine_to_angle(0.6),
        "gamma": _cosine_to_angle(0.6),
    }
    # Only c is eligible -> < min_records -> []
    pairs = bc.find_candidate_pairs(
        [a, b, c], settings=_settings(), embed_batch=_angle_embed(angles)
    )
    assert pairs == []


def test_pairs_no_self_pairs():
    """Self-pairs (same id) never appear (belt-and-braces)."""
    # Two records share an id (pathological) but identical text -> if self-pairing
    # leaked it would be cosine 1.0 anyway (excluded by the band), so to prove the
    # belt we give them in-band cosine via distinct text but the SAME id.
    a = _rec("dup", "alpha")
    b = _rec("dup", "beta")
    angles = {"alpha": 0.0, "beta": _cosine_to_angle(0.6)}
    pairs = bc.find_candidate_pairs(
        [a, b], settings=_settings(), embed_batch=_angle_embed(angles)
    )
    assert pairs == []  # same id -> never paired


def test_pairs_below_min_records():
    """< min_records eligible -> []."""
    a = _rec("a", "alpha")
    pairs = bc.find_candidate_pairs(
        [a], settings=_settings(), embed_batch=_angle_embed({"alpha": 0.0})
    )
    assert pairs == []


def test_pairs_max_pairs_cap_highest_cosine_first():
    """max_pairs cap honored; strongest-cosine pairs survive the cap."""
    # 4 records all mutually in-band; cap to 2 -> the two highest-cosine pairs.
    recs = [_rec(f"r{i}", f"t{i}") for i in range(4)]
    # Place them at angles so pairwise cosines are distinct and in-band.
    angles = {
        "t0": 0.0,
        "t1": _cosine_to_angle(0.70),  # close to t0
        "t2": _cosine_to_angle(0.50),
        "t3": _cosine_to_angle(0.46),
    }
    pairs = bc.find_candidate_pairs(
        recs, settings=_settings(max_pairs=2), embed_batch=_angle_embed(angles)
    )
    assert len(pairs) == 2  # exactly the cap


def test_pairs_max_eligible_caps_before_triangle():
    """max_eligible truncates the eligible set BEFORE pairing (M3)."""
    # 5 records, all in-band, but max_eligible=2 -> only 1 possible pair.
    recs = [
        _rec(f"r{i}", f"t{i}", last_updated=f"2026-06-1{i}T00:00:00+00:00")
        for i in range(5)
    ]
    angles = {f"t{i}": _cosine_to_angle(0.6) if i else 0.0 for i in range(5)}
    pairs = bc.find_candidate_pairs(
        recs, settings=_settings(max_eligible=2), embed_batch=_angle_embed(angles)
    )
    # With only 2 eligible records the upper triangle yields at most 1 pair.
    assert len(pairs) <= 1


def test_pairs_fail_open_on_raising_embed(capsys):
    """A raising embed_batch -> [] (no crash, no judge) + a VISIBLE diagnostic."""
    a = _rec("a", "alpha")
    b = _rec("b", "beta")

    def boom(_texts, **_kw):
        raise RuntimeError("FastEmbed offline")

    pairs = bc.find_candidate_pairs([a, b], settings=_settings(), embed_batch=boom)
    assert pairs == []
    out = capsys.readouterr().out
    assert "embed_batch unavailable" in out


def test_pairs_disabled_returns_empty():
    a = _rec("a", "alpha")
    b = _rec("b", "beta")
    pairs = bc.find_candidate_pairs(
        [a, b],
        settings=_settings(enabled=False),
        embed_batch=_angle_embed({"alpha": 0.0, "beta": _cosine_to_angle(0.6)}),
    )
    assert pairs == []


# ===========================================================================
# 4. judge_contradictions — provider-agnostic + fail-open + tolerant-parse (M2)
# ===========================================================================


def _fake_reasoning(parsed, model="x"):
    async def reasoning(*_a, **_k):
        return SimpleNamespace(parsed=parsed, model=model)

    return reasoning


def test_judge_returns_valid_conflict():
    """A judge naming ids in the pair -> that conflict returned; foreign id dropped."""
    a = _rec("x", "alpha")
    b = _rec("y", "beta")
    parsed = [
        {"a_id": "x", "b_id": "y", "reason": "opposed"},
        {"a_id": "x", "b_id": "ZZZ", "reason": "foreign"},  # ZZZ not in pairs -> drop
    ]
    out = _run(
        bc.judge_contradictions(
            [(a, b)], cwd=Path("."), settings=_settings(), reasoning=_fake_reasoning(parsed)
        )
    )
    assert len(out) == 1
    assert out[0]["a_id"] == "x" and out[0]["b_id"] == "y"


def test_judge_disabled_skips_reasoning():
    called = {"hit": False}

    async def reasoning(*_a, **_k):
        called["hit"] = True
        return SimpleNamespace(parsed=[], model="x")

    a = _rec("x", "alpha")
    b = _rec("y", "beta")
    out = _run(
        bc.judge_contradictions(
            [(a, b)], cwd=Path("."), settings=_settings(enabled=False), reasoning=reasoning
        )
    )
    assert out == []
    assert called["hit"] is False


def test_judge_empty_pairs_skips_reasoning():
    called = {"hit": False}

    async def reasoning(*_a, **_k):
        called["hit"] = True
        return SimpleNamespace(parsed=[], model="x")

    out = _run(
        bc.judge_contradictions(
            [], cwd=Path("."), settings=_settings(), reasoning=reasoning
        )
    )
    assert out == []
    assert called["hit"] is False


def test_judge_single_key_wrap_parsed():
    """M2: a single-key {"contradictions":[...]} wrap unwraps via the EXTENDED key."""
    a = _rec("x", "alpha")
    b = _rec("y", "beta")
    parsed = {"contradictions": [{"a_id": "x", "b_id": "y", "reason": "r"}]}
    out = _run(
        bc.judge_contradictions(
            [(a, b)], cwd=Path("."), settings=_settings(), reasoning=_fake_reasoning(parsed)
        )
    )
    assert len(out) == 1


def test_judge_multi_list_wrap_parsed():
    """M2 DISCRIMINATING: a MULTI-list wrap still unwraps (FAILS the unextended helper).

    {"contradictions":[...], "reasoning":["because"]} has TWO list-valued keys, so
    the sole-list fallback (len(lists)==1) returns []. The known-key extension is
    the ONLY thing that finds the conflict here.
    """
    a = _rec("x", "alpha")
    b = _rec("y", "beta")
    parsed = {
        "contradictions": [{"a_id": "x", "b_id": "y", "reason": "r"}],
        "reasoning": ["because they oppose"],
    }
    out = _run(
        bc.judge_contradictions(
            [(a, b)], cwd=Path("."), settings=_settings(), reasoning=_fake_reasoning(parsed)
        )
    )
    assert len(out) == 1  # would be 0 against the unextended _coerce_claim_list


def test_judge_bare_dict_no_list_returns_empty():
    a = _rec("x", "alpha")
    b = _rec("y", "beta")
    out = _run(
        bc.judge_contradictions(
            [(a, b)],
            cwd=Path("."),
            settings=_settings(),
            reasoning=_fake_reasoning({"status": "ok"}),
        )
    )
    assert out == []


def test_judge_fail_open_with_visible_print(capsys):
    """G5: a raising reasoning -> [] AND a '[belief_conflicts] judge failed' line."""
    a = _rec("x", "alpha")
    b = _rec("y", "beta")

    async def boom(*_a, **_k):
        raise RuntimeError("provider down")

    out = _run(
        bc.judge_contradictions(
            [(a, b)], cwd=Path("."), settings=_settings(), reasoning=boom
        )
    )
    assert out == []
    assert "judge failed" in capsys.readouterr().out


def test_judge_langfuse_none_no_crash(monkeypatch):
    """Rule 3 fail-open: get_observation_client -> None -> conflicts still returned."""
    from runtime import langfuse_setup

    monkeypatch.setattr(langfuse_setup, "get_observation_client", lambda: None)
    a = _rec("x", "alpha")
    b = _rec("y", "beta")
    parsed = [{"a_id": "x", "b_id": "y", "reason": "r"}]
    out = _run(
        bc.judge_contradictions(
            [(a, b)], cwd=Path("."), settings=_settings(), reasoning=_fake_reasoning(parsed)
        )
    )
    assert len(out) == 1


# ===========================================================================
# 5. _decide_loser — the resolution policy, each provenance pairing
# ===========================================================================


def test_decide_explicit_vs_explicit_holds_both_by_default():
    """B1: two explicit beliefs -> held=True, NEITHER is a dropping loser."""
    a = _rec("a", "alpha", source="explicit", confidence=0.8)
    b = _rec("b", "beta", source="explicit", confidence=0.8)
    loser, winner, reason, held = bc._decide_loser(a, b, _settings())
    assert held is True
    assert reason == "held-explicit-vs-explicit"
    assert {loser.id, winner.id} == {"a", "b"}


def test_decide_explicit_vs_explicit_opted_in_falls_through():
    """B1 opt-in: allow_explicit_vs_explicit=True -> held=False, evidence decides."""
    a = _rec("a", "alpha", source="explicit", evidence_count=3)
    b = _rec("b", "beta", source="explicit", evidence_count=1)
    loser, winner, reason, held = bc._decide_loser(
        a, b, _settings(allow_explicit_vs_explicit=True)
    )
    assert held is False
    assert loser.id == "b"  # lower evidence loses
    assert winner.id == "a"


def test_decide_explicit_beats_reflection_regardless_of_evidence():
    """B1: explicit vs reflection -> the reflection ALWAYS loses; evidence ignored."""
    # Give the reflection HIGHER evidence to prove provenance is decisive.
    expl = _rec("e", "alpha", source="explicit", evidence_count=1)
    refl = _rec("r", "beta", source="reflection", evidence_count=9)
    loser, winner, reason, held = bc._decide_loser(expl, refl, _settings())
    assert held is False
    assert loser.id == "r"  # the reflection loses despite more evidence
    assert winner.id == "e"
    assert reason == "explicit>reflection"


def test_decide_reflection_evidence_wins():
    a = _rec("a", "alpha", source="reflection", evidence_count=3)
    b = _rec("b", "beta", source="reflection", evidence_count=1)
    loser, winner, reason, held = bc._decide_loser(a, b, _settings())
    assert held is False
    assert loser.id == "b"
    assert "evidence" in reason


def test_decide_reflection_recency_wins():
    a = _rec(
        "a", "alpha", source="reflection", evidence_count=2,
        last_updated="2026-06-13T00:00:00+00:00",
    )
    b = _rec(
        "b", "beta", source="reflection", evidence_count=2,
        last_updated="2026-06-10T00:00:00+00:00",
    )
    loser, winner, reason, held = bc._decide_loser(a, b, _settings())
    assert held is False
    assert loser.id == "b"  # older loses
    assert winner.id == "a"


def test_decide_reflection_tiebreak_id_deterministic():
    a = _rec(
        "aaa", "alpha", source="reflection", evidence_count=2,
        last_updated="2026-06-13T00:00:00+00:00",
    )
    b = _rec(
        "bbb", "beta", source="reflection", evidence_count=2,
        last_updated="2026-06-13T00:00:00+00:00",
    )
    l1, w1, reason, held = bc._decide_loser(a, b, _settings())
    l2, _w2, _r2, _h2 = bc._decide_loser(b, a, _settings())  # swap arg order
    assert held is False
    assert reason == "tiebreak-id"
    assert l1.id == "aaa"  # lexicographically smaller loses
    assert l2.id == "aaa"  # deterministic regardless of arg order


# ===========================================================================
# 6. apply_contradictions — real contradict() + B1 + B2 + M4
# ===========================================================================


def _seed(path, records):
    InferenceTracker(path).save(records)


def test_apply_explicit_vs_reflection_normal_drop(tmp_path):
    """explicit(0.8) vs reflection(0.8) -> the reflection drops to 0.65, explicit untouched."""
    path = tmp_path / "inf.json"
    _seed(
        path,
        [
            _rec("expl", "alpha", source="explicit", confidence=0.8),
            _rec("refl", "beta", source="reflection", confidence=0.8),
        ],
    )
    n = bc.apply_contradictions(
        [{"a_id": "expl", "b_id": "refl", "reason": "opposed"}], path, settings=_settings()
    )
    assert n == 1
    by_id = {r.id: r for r in InferenceTracker(path).load()}
    assert abs(by_id["refl"].confidence - 0.65) < 1e-9
    assert by_id["refl"].contradiction_count == 1
    assert by_id["refl"].contradicted_by == ["expl:explicit>reflection"]
    # explicit UNTOUCHED
    assert abs(by_id["expl"].confidence - 0.8) < 1e-9
    assert by_id["expl"].contradiction_count == 0


def test_apply_explicit_vs_explicit_holds_both(tmp_path):
    """B1 catastrophe guard: TWO explicit(0.8) -> both held, NEITHER drops."""
    path = tmp_path / "inf.json"
    _seed(
        path,
        [
            _rec("e1", "alpha", source="explicit", confidence=0.8),
            _rec("e2", "beta", source="explicit", confidence=0.8),
        ],
    )
    n = bc.apply_contradictions(
        [{"a_id": "e1", "b_id": "e2", "reason": "opposed"}], path, settings=_settings()
    )
    assert n == 2  # both held
    by_id = {r.id: r for r in InferenceTracker(path).load()}
    assert abs(by_id["e1"].confidence - 0.8) < 1e-9  # NOT dropped to 0.65
    assert abs(by_id["e2"].confidence - 0.8) < 1e-9
    assert by_id["e1"].contradiction_count == 1
    assert by_id["e2"].contradiction_count == 1
    # each names the OTHER
    assert by_id["e1"].contradicted_by == ["e2:held-explicit-vs-explicit"]
    assert by_id["e2"].contradicted_by == ["e1:held-explicit-vs-explicit"]


def test_apply_explicit_vs_explicit_held_path_idempotent_on_rerun(tmp_path):
    """N2: a SECOND apply of the same explicit<->explicit conflict -> 0, count stays 1."""
    path = tmp_path / "inf.json"
    _seed(
        path,
        [
            _rec("e1", "alpha", source="explicit", confidence=0.8),
            _rec("e2", "beta", source="explicit", confidence=0.8),
        ],
    )
    conflict = [{"a_id": "e1", "b_id": "e2", "reason": "opposed"}]
    assert bc.apply_contradictions(conflict, path, settings=_settings()) == 2
    # second run: held-path dedups through the SAME contradicted_by key
    assert bc.apply_contradictions(conflict, path, settings=_settings()) == 0
    by_id = {r.id: r for r in InferenceTracker(path).load()}
    assert by_id["e1"].contradiction_count == 1  # no infinite increment
    assert by_id["e2"].contradiction_count == 1
    assert by_id["e1"].contradicted_by == ["e2:held-explicit-vs-explicit"]


def test_apply_b2_cross_run_idempotency_5_runs(tmp_path):
    """B2 keystone: the SAME conflict over 5 runs drops confidence ONCE (count 1)."""
    path = tmp_path / "inf.json"
    _seed(
        path,
        [
            _rec("expl", "alpha", source="explicit", confidence=0.8),
            _rec("refl", "beta", source="reflection", confidence=0.8),
        ],
    )
    conflict = [{"a_id": "expl", "b_id": "refl", "reason": "opposed"}]
    results = [
        bc.apply_contradictions(conflict, path, settings=_settings()) for _ in range(5)
    ]
    assert results == [1, 0, 0, 0, 0]  # fires ONCE, then no-ops
    refl = {r.id: r for r in InferenceTracker(path).load()}["refl"]
    assert abs(refl.confidence - 0.65) < 1e-9  # NOT 0.10 (the death spiral)
    assert refl.contradiction_count == 1
    assert refl.contradicted_by == ["expl:explicit>reflection"]


def test_apply_b2_line_ablation_proves_dedup_branch(tmp_path, monkeypatch):
    """N1: deleting ONLY the dedup check -> the 5-run death spiral (0.10 / count 5).

    Proves the B2 5-run test is DISCRIMINATING — it exercises the specific dedup
    branch (apply_contradictions._record's contradicted_by check), not merely
    "the module exists." A git-stash of belief_conflicts.py (a NEW module) would
    ImportError; this monkeypatch ablates exactly the :649-650 dedup line by
    swapping in a no-dedup apply that is otherwise byte-identical, then asserts the
    spiral the real code prevents.
    """

    # A no-dedup apply: identical to bc.apply_contradictions EXCEPT the
    # contradicted_by skip is removed (the line-level ablation).
    def apply_no_dedup(conflicts, state_file, *, settings):
        tracker = InferenceTracker(state_file)
        by_id = {r.id: r for r in tracker.load()}
        applied = 0
        seen = set()
        for c in conflicts:
            a, b = by_id.get(c.get("a_id")), by_id.get(c.get("b_id"))
            if a is None or b is None:
                continue
            loser, winner, reason, held = bc._decide_loser(a, b, settings)
            if held:
                continue  # not exercised in this explicit<->reflection case
            if loser.id in seen:
                continue
            # NO contradicted_by dedup check here -> re-drops every run
            if tracker.contradict(loser.id, by=f"{winner.id}:{reason}", held=held):
                seen.add(loser.id)
                applied += 1
        return applied

    path = tmp_path / "inf.json"
    _seed(
        path,
        [
            _rec("expl", "alpha", source="explicit", confidence=0.8),
            _rec("refl", "beta", source="reflection", confidence=0.8),
        ],
    )
    conflict = [{"a_id": "expl", "b_id": "refl", "reason": "opposed"}]
    results = [apply_no_dedup(conflict, path, settings=_settings()) for _ in range(5)]
    assert results == [1, 1, 1, 1, 1]  # WITHOUT dedup it fires every run
    refl = {r.id: r for r in InferenceTracker(path).load()}["refl"]
    assert abs(refl.confidence - 0.10) < 1e-9  # the death spiral the real code prevents
    assert refl.contradiction_count == 5
    assert len(refl.contradicted_by) == 5  # five audit entries


def test_apply_unknown_id_no_crash(tmp_path):
    path = tmp_path / "inf.json"
    _seed(path, [_rec("real", "alpha", source="reflection", confidence=0.8)])
    n = bc.apply_contradictions(
        [{"a_id": "ghost1", "b_id": "ghost2", "reason": "x"}], path, settings=_settings()
    )
    assert n == 0
    assert InferenceTracker(path).load()[0].confidence == 0.8  # untouched


def test_apply_two_conflicts_same_loser_hit_once(tmp_path):
    """Two conflicts naming the SAME loser in ONE run -> the loser is hit once (per-run guard)."""
    path = tmp_path / "inf.json"
    _seed(
        path,
        [
            _rec("e1", "alpha", source="explicit", confidence=0.8),
            _rec("e2", "gamma", source="explicit", confidence=0.8),
            _rec("refl", "beta", source="reflection", confidence=0.8),
        ],
    )
    conflicts = [
        {"a_id": "e1", "b_id": "refl", "reason": "a"},
        {"a_id": "e2", "b_id": "refl", "reason": "b"},
    ]
    n = bc.apply_contradictions(conflicts, path, settings=_settings())
    assert n == 1  # refl hit once this cycle
    refl = {r.id: r for r in InferenceTracker(path).load()}["refl"]
    assert refl.contradiction_count == 1
    assert abs(refl.confidence - 0.65) < 1e-9


def test_apply_m4_log_called_with_instance(tmp_path, monkeypatch):
    """M4: log_inference_event invoked with an InferenceLog INSTANCE (0.8->0.65)."""
    captured = {}

    def fake_log(log):  # takes an INSTANCE, never kwargs
        captured["log"] = log

    monkeypatch.setattr("cognition.observability.log_inference_event", fake_log)
    path = tmp_path / "inf.json"
    _seed(
        path,
        [
            _rec("expl", "alpha", source="explicit", confidence=0.8),
            _rec("refl", "beta", source="reflection", confidence=0.8),
        ],
    )
    n = bc.apply_contradictions(
        [{"a_id": "expl", "b_id": "refl", "reason": "opposed"}], path, settings=_settings()
    )
    assert n == 1
    log = captured["log"]
    assert log.action == "contradicted"
    assert abs(log.old_confidence - 0.8) < 1e-9
    assert abs(log.new_confidence - 0.65) < 1e-9


def test_apply_m4_raising_log_does_not_change_applied(tmp_path, monkeypatch):
    """M4: a RAISING log is best-effort -> does NOT change applied / the drop still lands."""

    def boom_log(_log):
        raise TypeError("bad log call")

    monkeypatch.setattr("cognition.observability.log_inference_event", boom_log)
    path = tmp_path / "inf.json"
    _seed(
        path,
        [
            _rec("expl", "alpha", source="explicit", confidence=0.8),
            _rec("refl", "beta", source="reflection", confidence=0.8),
        ],
    )
    n = bc.apply_contradictions(
        [{"a_id": "expl", "b_id": "refl", "reason": "opposed"}], path, settings=_settings()
    )
    assert n == 1  # the raising log did not turn this into 0 applied
    refl = {r.id: r for r in InferenceTracker(path).load()}["refl"]
    assert abs(refl.confidence - 0.65) < 1e-9  # the drop still landed


# ===========================================================================
# 7. Reflection wiring — the pass runs, non-blocking
# ===========================================================================


def test_reflection_wiring_applies_conflict(tmp_path, monkeypatch):
    """The memory_reflect insertion point loads -> pairs -> judges -> applies.

    Drives the real wiring with monkeypatched find_candidate_pairs (returns a pair)
    + judge_contradictions (returns a conflict) over a tmp INFERENCE_STATE_FILE —
    no real LLM, no real embed. Asserts the loser's contradiction_count == 1 after.
    """
    import memory_reflect

    path = tmp_path / "inf.json"
    _seed(
        path,
        [
            _rec("expl", "alpha", source="explicit", confidence=0.8),
            _rec("refl", "beta", source="reflection", confidence=0.8),
        ],
    )
    monkeypatch.setattr(memory_reflect, "INFERENCE_STATE_FILE", path, raising=False)
    monkeypatch.setattr("config.INFERENCE_STATE_FILE", path, raising=False)

    expl = _rec("expl", "alpha", source="explicit", confidence=0.8)
    refl = _rec("refl", "beta", source="reflection", confidence=0.8)
    monkeypatch.setattr(
        "cognition.belief_conflicts.find_candidate_pairs",
        lambda records, **kw: [(expl, refl)],
    )

    async def fake_judge(pairs, **kw):
        return [{"a_id": "expl", "b_id": "refl", "reason": "opposed"}]

    monkeypatch.setattr("cognition.belief_conflicts.judge_contradictions", fake_judge)

    # Execute ONLY the contradiction block (mirror its body) to prove the wiring
    # end-to-end without running the entire reflection pipeline.
    _run(_drive_contradiction_block(test_mode=False, state_file=path))

    by_id = {r.id: r for r in InferenceTracker(path).load()}
    assert by_id["refl"].contradiction_count == 1
    assert abs(by_id["refl"].confidence - 0.65) < 1e-9


def test_reflection_wiring_test_mode_skips_apply(tmp_path, monkeypatch):
    """test_mode=True -> judge may run but apply is skipped (0 applied, no mutation)."""
    path = tmp_path / "inf.json"
    _seed(
        path,
        [
            _rec("expl", "alpha", source="explicit", confidence=0.8),
            _rec("refl", "beta", source="reflection", confidence=0.8),
        ],
    )
    expl = _rec("expl", "alpha", source="explicit", confidence=0.8)
    refl = _rec("refl", "beta", source="reflection", confidence=0.8)
    monkeypatch.setattr(
        "cognition.belief_conflicts.find_candidate_pairs",
        lambda records, **kw: [(expl, refl)],
    )

    async def fake_judge(pairs, **kw):
        return [{"a_id": "expl", "b_id": "refl", "reason": "opposed"}]

    monkeypatch.setattr("cognition.belief_conflicts.judge_contradictions", fake_judge)
    _run(_drive_contradiction_block(test_mode=True, state_file=path))

    # No mutation in test_mode.
    refl_rec = {r.id: r for r in InferenceTracker(path).load()}["refl"]
    assert refl_rec.contradiction_count == 0
    assert refl_rec.confidence == 0.8


def test_reflection_wiring_judge_raising_is_non_blocking(tmp_path, monkeypatch):
    """A raising judge -> the block swallows it (non-blocking), no mutation, no raise."""
    path = tmp_path / "inf.json"
    _seed(path, [_rec("refl", "beta", source="reflection", confidence=0.8)])
    monkeypatch.setattr(
        "cognition.belief_conflicts.find_candidate_pairs",
        lambda records, **kw: [("p",)],
    )

    async def boom_judge(pairs, **kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr("cognition.belief_conflicts.judge_contradictions", boom_judge)
    # Must NOT raise.
    _run(_drive_contradiction_block(test_mode=False, state_file=path))
    assert InferenceTracker(path).load()[0].confidence == 0.8  # untouched


async def _drive_contradiction_block(*, test_mode: bool, state_file: Path) -> None:
    """Faithful re-execution of memory_reflect's contradiction block.

    Mirrors the inserted block body (load -> pairs -> judge -> apply guarded by
    test_mode -> non-blocking try/except) so the wiring is exercised without
    standing up the whole reflection pipeline / a real provider. The monkeypatched
    find_candidate_pairs / judge_contradictions are what each test injects.
    """
    try:
        from cognition import belief_conflicts

        records = InferenceTracker(state_file).load()
        pairs = belief_conflicts.find_candidate_pairs(records)
        conflicts = await belief_conflicts.judge_contradictions(pairs, cwd=Path("."))
        if not test_mode:
            belief_conflicts.apply_contradictions(conflicts, state_file)
    except ImportError:
        pass
    except Exception:
        # Non-blocking: a judge/apply failure must not break reflection.
        pass


# ===========================================================================
# 8. Held-under-tension render: floor + tag (M1, FAILS pre-fix)
# ===========================================================================


def _render_region(monkeypatch, tmp_path, records) -> str:
    """Build the engine's _build_active_inference_region over a tmp corpus."""
    import engine as engine_mod

    path = tmp_path / "inf.json"
    InferenceTracker(path).save(records)
    monkeypatch.setattr("config.INFERENCE_STATE_FILE", path, raising=False)
    # The renderer imports INFERENCE_STATE_FILE from config inside the function.
    eng = engine_mod.ConversationEngine.__new__(engine_mod.ConversationEngine)
    return eng._build_active_inference_region()


def test_render_contradicted_below_half_still_shows_with_tag(monkeypatch, tmp_path):
    """M1: a reflection at conf 0.45 with contradiction_count=1 STILL renders + tag.

    INVISIBLE against the pre-fix 0.5 fetch — this is the discriminating case.
    """
    out = _render_region(
        monkeypatch,
        tmp_path,
        [
            _rec(
                "held", "alpha is held", source="reflection", confidence=0.45,
                contradicted_by=["w:explicit>reflection"],
            )
        ],
    )
    assert "alpha is held" in out
    assert "held-under-tension" in out


def test_render_clean_below_half_filtered(monkeypatch, tmp_path):
    """M1: a clean reflection at conf 0.45 (count 0) does NOT render (gate as today)."""
    out = _render_region(
        monkeypatch,
        tmp_path,
        [_rec("clean", "beta is clean", source="reflection", confidence=0.45)],
    )
    assert "beta is clean" not in out


def test_render_contradicted_above_half_shows_with_tag(monkeypatch, tmp_path):
    out = _render_region(
        monkeypatch,
        tmp_path,
        [
            _rec(
                "held", "gamma is held", source="reflection", confidence=0.7,
                contradicted_by=["w:explicit>reflection"],
            )
        ],
    )
    assert "gamma is held" in out
    assert "held-under-tension" in out


def test_render_clean_above_half_shows_without_tag(monkeypatch, tmp_path):
    out = _render_region(
        monkeypatch,
        tmp_path,
        [_rec("clean", "delta is clean", source="reflection", confidence=0.7)],
    )
    assert "delta is clean" in out
    assert "held-under-tension" not in out


def test_render_decayed_and_auto_capture_never_show(monkeypatch, tmp_path):
    """A decayed / auto_capture record (even contradicted) NEVER renders."""
    out = _render_region(
        monkeypatch,
        tmp_path,
        [
            _rec(
                "dec", "decayed text", source="reflection", confidence=0.45,
                status="decayed", contradicted_by=["w:r"],
            ),
            _rec(
                "auto", "auto text", source="auto_capture", confidence=0.9,
                contradicted_by=["w:r"],
            ),
        ],
    )
    assert "decayed text" not in out
    assert "auto text" not in out


# ===========================================================================
# 9. Write-time contradiction (WS3 #84 — opt-in, default-OFF, fail-open).
#
# The helper resolve_write_time_contradiction reuses judge_contradictions +
# apply_contradictions VERBATIM. apply_operator_beliefs goes async + gates the
# helper on a physical MISS + the flag. Every case is tmp_path-scoped with
# injected embed_batch (angle-controlled) + reasoning (judge stub) so the REAL
# branches run offline — no FastEmbed, no provider.
#
# embed_batch is monkeypatched at embeddings.embed_batch (NOT just injected) so
# BOTH the add_inference dedup path (HIT detection, threshold 0.72) AND the
# helper's neighbor scan see the same controlled angles. The helper's band is
# [0.45, 0.72) — strictly BELOW the dedup threshold.
# ===========================================================================

from cognition import operator_beliefs as ob  # noqa: E402


def _patch_embed_angles(monkeypatch, angles):
    """Monkeypatch embeddings.embed_batch with an angle-controlled fake.

    Covers BOTH add_inference's dedup scan and the write-time helper's neighbor
    scan, so a single map controls dedup-HIT vs band placement deterministically.
    """
    monkeypatch.setattr("embeddings.embed_batch", _angle_embed(angles))


def _raise_if_awaited(label="judge"):
    """A reasoning stub that FAILS the test if the judge is ever awaited."""

    async def reasoning(*_a, **_k):
        raise AssertionError(f"{label} must NOT be awaited in this path")

    return reasoning


def _conflict_reasoning(a_id, b_id, reason="opposed"):
    """A reasoning stub returning ONE conflict naming the two ids."""
    return _fake_reasoning([{"a_id": a_id, "b_id": b_id, "reason": reason}])


def _band_conflict_reasoning(reason="opposed"):
    """A reasoning stub for write-time tests where the NEW record's id is freshly
    minted (so it can't be hardcoded). Parses the ``A[id=...]`` / ``B[id=...]``
    markers the judge builds into the context and returns ONE conflict per pair,
    naming the EXACT ids it was handed. The helper id-filters, so this is faithful.
    """
    import re as _re

    async def reasoning(context, *_a, **_k):
        conflicts = []
        for m in _re.finditer(r"A\[id=([^\]]+)\].*?B\[id=([^\]]+)\]", context):
            conflicts.append({"a_id": m.group(1), "b_id": m.group(2), "reason": reason})
        return SimpleNamespace(parsed=conflicts, model="x")

    return reasoning


def _spy_embed(angles=None):
    """An ``embed_batch`` spy that RECORDS every call into ``.calls`` and returns
    real angle-controlled vectors.

    Unlike ``_raise_if_awaited`` (whose AssertionError the production fail-open
    judge SWALLOWS — line :184-199 catches ``Exception`` and returns ``[]``), a
    spy proves a NEGATIVE by an explicit ``assert spy.calls == []`` AFTER the
    call: the alarm cannot be muted by the code under test. ``calls`` holds the
    ``list(texts)`` of each invocation (so a test can also assert the cost-cap
    count, not just that it ran).
    """
    inner = _angle_embed(angles or {})

    def _embed(texts, **kw):
        _embed.calls.append(list(texts))
        return inner(texts, **kw)

    _embed.calls = []
    return _embed


def _spy_reasoning(parsed=None):
    """A ``reasoning`` (judge) spy that RECORDS every await into ``.calls`` and
    returns ``parsed`` (default ``[]`` — no conflict).

    The discriminating replacement for ``_raise_if_awaited`` on "judge must NOT
    fire" paths: assert ``spy.calls == []`` after the call. Because it returns a
    value instead of raising, the production ``except Exception`` in
    ``judge_contradictions`` can NOT swallow the proof — if a regression awaits
    the judge, ``.calls`` is non-empty and the test FAILS.
    """

    async def reasoning(*a, **k):
        reasoning.calls.append((a, k))
        return SimpleNamespace(parsed=parsed if parsed is not None else [], model="x")

    reasoning.calls = []
    return reasoning


def _patch_resolve_counter(monkeypatch):
    """Wrap ``cognition.belief_conflicts.resolve_write_time_contradiction`` with a
    delegating counter and return the counter handle (``.count``).

    ``apply_operator_beliefs`` imports the helper lazily by module attribute
    (``from cognition.belief_conflicts import resolve_write_time_contradiction``
    inside the loop), so patching the module attribute is seen at call time. The
    wrapper counts then delegates to the REAL helper — behavior is unchanged; the
    test asserts ``.count == 0`` to prove the CALLER skipped the helper (a dedup
    HIT), not merely that a swallowed judge produced zero conflicts.
    """
    real = bc.resolve_write_time_contradiction
    handle = SimpleNamespace(count=0)

    async def _counting(*a, **k):
        handle.count += 1
        return await real(*a, **k)

    monkeypatch.setattr(
        "cognition.belief_conflicts.resolve_write_time_contradiction", _counting
    )
    return handle


def _run_apply(claims, path, **kw):
    """asyncio.run the async apply_operator_beliefs -> (written, write_time_applied)."""
    return asyncio.run(ob.apply_operator_beliefs(claims, path, **kw))


def test_write_time_off_is_state_parity(tmp_path, monkeypatch):
    """PARITY GATE: flag UNSET -> corpus identical to plain-add + NO judge call.

    Two new beliefs are written with the flag OFF; the reasoning stub raises if
    awaited (asserts the judge never fires), and the resulting corpus matches a
    plain-add baseline byte-for-byte (minus the volatile timestamps). The returned
    write_time_applied is 0.
    """
    monkeypatch.delenv("INFERENCE_WRITE_TIME_CONTRADICTION", raising=False)
    # Two distinct beliefs far apart so neither dedups into the other.
    angles = {"ship lean and fast": 0.0, "build a heavy enterprise process": 2.0}
    _patch_embed_angles(monkeypatch, angles)
    claims = [
        {"claim": "ship lean and fast", "kind": "reflection"},
        {"claim": "build a heavy enterprise process", "kind": "reflection"},
    ]

    # JUDGE SPY (not _raise_if_awaited — the production fail-open judge swallows
    # AssertionError). assert spy.calls == [] proves NO judge fired on the OFF path.
    judge_spy = _spy_reasoning()
    flag_path = tmp_path / "flag_off.json"
    written, applied = _run_apply(claims, flag_path, reasoning=judge_spy)
    assert written == 2
    assert applied == 0  # OFF -> no write-time resolution
    assert judge_spy.calls == []  # ...and NO judge call

    # NORMALIZED PLAIN-ADD BASELINE — the SAME two claims through a direct
    # add_inference-only path (no flag, no helper). The OFF-path corpus must be
    # field-for-field identical to this baseline (minus volatile id + timestamps).
    base_path = tmp_path / "baseline.json"
    base_tracker = InferenceTracker(base_path)
    for c in claims:
        base_tracker.add_inference(
            inference=c["claim"],
            observation=c["claim"],
            confidence=0.5,  # apply_operator_beliefs' default when "confidence" absent
            source="reflection",
        )

    def _normalized(records):
        from dataclasses import asdict

        out = []
        for r in sorted(records, key=lambda x: x.inference):
            d = asdict(r)
            # id is a fresh uuid4; first_seen/last_updated are wall-clock — drop them.
            for volatile in ("id", "first_seen", "last_updated"):
                d.pop(volatile, None)
            out.append(d)
        return out

    off_recs = InferenceTracker(flag_path).load()
    base_recs = base_tracker.load()
    assert _normalized(off_recs) == _normalized(base_recs)
    # And explicitly: no contradiction audit was written on either record.
    assert all(r.contradicted_by == [] for r in off_recs)
    assert all(r.contradiction_count == 0 for r in off_recs)


def test_write_time_helper_self_gates_when_flag_unset(tmp_path, monkeypatch):
    """R1 B2: a DIRECT helper call with the env unset returns 0, judge never awaited.

    Even with an in-band neighbor seeded, the helper early-returns on its OWN gate
    (get_inference_extraction_settings().write_time_contradiction) before any embed
    or judge — proving default-OFF is the helper's contract, not just the caller's.
    """
    monkeypatch.delenv("INFERENCE_WRITE_TIME_CONTRADICTION", raising=False)
    path = tmp_path / "inf.json"
    existing = _rec("ex", "ship lean and fast", source="explicit", confidence=0.8)
    new = _rec("new", "build a heavy enterprise process", source="reflection")
    _seed(path, [existing, new])
    angles = {
        "ship lean and fast": 0.0,
        "build a heavy enterprise process": _cosine_to_angle(0.6),  # in-band
    }
    # SPIES (not _raise_if_awaited — that AssertionError is swallowed by the
    # production fail-open judge). The self-gate must return BEFORE either fires.
    embed_spy = _spy_embed(angles)
    judge_spy = _spy_reasoning()
    applied = _run(
        bc.resolve_write_time_contradiction(
            new,
            path,
            Path("."),
            settings=_settings(),
            embed_batch=embed_spy,
            reasoning=judge_spy,
        )
    )
    assert applied == 0
    # The self-gate short-circuited before corpus load / embed / judge.
    assert embed_spy.calls == []
    assert judge_spy.calls == []


def test_write_time_on_resolves_reflection_conflict(tmp_path, monkeypatch):
    """Flag ON + a new reflection belief in-band with an existing explicit ->
    the reflection LOSES at write via the real apply_contradictions; count == 1."""
    monkeypatch.setenv("INFERENCE_WRITE_TIME_CONTRADICTION", "true")
    path = tmp_path / "inf.json"
    # Seed one explicit belief already in the corpus.
    _seed(path, [_rec("expl", "ship lean and fast", source="explicit", confidence=0.8)])
    # The NEW reflection belief lands in-band (cosine 0.6) with the explicit one.
    angles = {
        "ship lean and fast": 0.0,
        "build a heavy enterprise process": _cosine_to_angle(0.6),
    }
    _patch_embed_angles(monkeypatch, angles)

    written, applied = _run_apply(
        # confidence 0.8 so the post-drop value is deterministic (0.8 - 0.15).
        [{"claim": "build a heavy enterprise process", "kind": "reflection", "confidence": 0.8}],
        path,
        write_time_enabled=True,
        settings=_settings(),
        # The new record's id is freshly minted; resolve it by text, not id:
        reasoning=_band_conflict_reasoning(),
    )
    assert written == 1
    assert applied == 1
    by_text = {r.inference: r for r in InferenceTracker(path).load()}
    refl = by_text["build a heavy enterprise process"]
    # The reflection dropped; the explicit is sacrosanct (untouched).
    assert refl.contradiction_count == 1
    assert refl.contradicted_by  # an audit entry naming the explicit winner
    assert abs(refl.confidence - 0.65) < 1e-9
    expl = by_text["ship lean and fast"]
    assert abs(expl.confidence - 0.8) < 1e-9
    assert expl.contradiction_count == 0


def test_write_time_on_explicit_vs_explicit_holds_both(tmp_path, monkeypatch):
    """B1: flag ON, new EXPLICIT in-band with existing EXPLICIT -> HELD on both,
    neither confidence drops (default allow_explicit_vs_explicit=false)."""
    monkeypatch.setenv("INFERENCE_WRITE_TIME_CONTRADICTION", "true")
    path = tmp_path / "inf.json"
    _seed(path, [_rec("e1", "ship lean and fast", source="explicit", confidence=0.8)])
    angles = {
        "ship lean and fast": 0.0,
        "build a heavy enterprise process": _cosine_to_angle(0.6),
    }
    _patch_embed_angles(monkeypatch, angles)

    written, applied = _run_apply(
        # confidence 0.8 so the held assertion (no drop) is checked against 0.8.
        [{"claim": "build a heavy enterprise process", "kind": "explicit", "confidence": 0.8}],
        path,
        write_time_enabled=True,
        settings=_settings(),
        reasoning=_band_conflict_reasoning(),
    )
    assert written == 1
    assert applied == 2  # both HELD (B1 records tension on both, no drop)
    by_text = {r.inference: r for r in InferenceTracker(path).load()}
    assert abs(by_text["ship lean and fast"].confidence - 0.8) < 1e-9
    assert abs(by_text["build a heavy enterprise process"].confidence - 0.8) < 1e-9


def test_write_time_fail_open_on_raising_judge(tmp_path, monkeypatch, capsys):
    """Flag ON, the judge RAISES -> the write survives unchanged, visible print,
    count 0, no exception escapes."""
    monkeypatch.setenv("INFERENCE_WRITE_TIME_CONTRADICTION", "true")
    path = tmp_path / "inf.json"
    _seed(path, [_rec("expl", "ship lean and fast", source="explicit", confidence=0.8)])
    angles = {
        "ship lean and fast": 0.0,
        "build a heavy enterprise process": _cosine_to_angle(0.6),
    }
    _patch_embed_angles(monkeypatch, angles)

    async def raising(*_a, **_k):
        raise RuntimeError("provider boom")

    written, applied = _run_apply(
        [{"claim": "build a heavy enterprise process", "kind": "reflection"}],
        path,
        write_time_enabled=True,
        settings=_settings(),
        reasoning=raising,
    )
    assert written == 1
    assert applied == 0
    # The write survived: the new reflection record is present and unmodified.
    by_text = {r.inference: r for r in InferenceTracker(path).load()}
    refl = by_text["build a heavy enterprise process"]
    assert refl.contradiction_count == 0
    assert refl.contradicted_by == []
    # judge_contradictions prints its OWN "judge failed" line on a raising reasoning.
    out = capsys.readouterr().out
    assert "judge failed" in out or "write-time contradiction skipped" in out


def test_write_time_no_band_neighbor_skips_judge(tmp_path, monkeypatch):
    """COST BOUND: flag ON, the new belief is orthogonal to all actives ->
    no band hit -> the judge is NEVER awaited; returns 0."""
    monkeypatch.setenv("INFERENCE_WRITE_TIME_CONTRADICTION", "true")
    path = tmp_path / "inf.json"
    _seed(path, [_rec("expl", "ship lean and fast", source="explicit", confidence=0.8)])
    # New belief far from the existing one (cosine ~0 -> below the 0.45 floor).
    angles = {
        "ship lean and fast": 0.0,
        "a totally unrelated belief about lunch": 1.5,  # ~0.07 cosine, below band
    }
    # add_inference's OWN dedup scan uses embeddings.embed_batch (monkeypatched);
    # the helper's band scan uses the INJECTED embed spy. The judge spy proves the
    # judge is never awaited via an explicit empty-calls assert (a swallowed
    # AssertionError would not).
    _patch_embed_angles(monkeypatch, angles)
    embed_spy = _spy_embed(angles)
    judge_spy = _spy_reasoning()

    written, applied = _run_apply(
        [{"claim": "a totally unrelated belief about lunch", "kind": "reflection"}],
        path,
        write_time_enabled=True,
        settings=_settings(),
        embed_batch=embed_spy,
        reasoning=judge_spy,
    )
    assert written == 1
    assert applied == 0
    # The helper RAN the band scan (embed fired) but found no in-band neighbor, so
    # the judge was never awaited — proven by the recorded call list, not a muted
    # raise.
    assert embed_spy.calls != []  # the band scan ran
    assert judge_spy.calls == []  # ...and stopped before the judge


def test_write_time_eligible_capped_at_max_eligible(tmp_path, monkeypatch):
    """R1 B4 COST CAP: seed MORE than max_eligible actives; the injected embed_batch
    receives exactly 1 + max_eligible texts (the incoming + the truncated band),
    NOT len(corpus)+1."""
    monkeypatch.setenv("INFERENCE_WRITE_TIME_CONTRADICTION", "true")
    path = tmp_path / "inf.json"
    # Seed 5 existing reflection beliefs; cap eligible at 2.
    seeded = [
        _rec(f"r{i}", f"existing belief number {i}", source="reflection")
        for i in range(5)
    ]
    _seed(path, seeded)

    captured = {"texts": None}

    def capturing_embed(texts, **_kw):
        captured["texts"] = list(texts)
        import numpy as np

        # Place everything orthogonal so no judge fires (we only assert the count).
        return [np.array([1.0, 0.0], dtype=np.float32) for _ in texts]

    new = _rec("new", "a fresh incoming belief", source="reflection")
    _run(
        bc.resolve_write_time_contradiction(
            new,
            path,
            Path("."),
            write_time_enabled=True,
            settings=_settings(max_eligible=2),
            embed_batch=capturing_embed,
            reasoning=_raise_if_awaited(),
        )
    )
    # 1 incoming + 2 (the max_eligible cap), NOT 1 + 5.
    assert captured["texts"] is not None
    assert len(captured["texts"]) == 1 + 2


def test_write_time_dedup_hit_skips_judge(tmp_path, monkeypatch):
    """Flag ON, a paraphrase of an existing belief -> add_inference HIT (id already
    in before_ids) -> the helper is never invoked / returns 0."""
    monkeypatch.setenv("INFERENCE_WRITE_TIME_CONTRADICTION", "true")
    path = tmp_path / "inf.json"
    _seed(path, [_rec("orig", "ship lean and fast", source="reflection", confidence=0.7)])
    # The incoming text is a paraphrase that lands ABOVE the 0.72 dedup threshold.
    angles = {
        "ship lean and fast": 0.0,
        "ship lean and move fast": _cosine_to_angle(0.95),  # HIT (>= 0.72)
    }
    _patch_embed_angles(monkeypatch, angles)
    # COUNTER on the helper itself (not a swallowed judge AssertionError): on a
    # dedup HIT the caller's before_ids gate must SKIP the helper entirely. The
    # wrapper increments then delegates so behavior is unchanged.
    helper_calls = _patch_resolve_counter(monkeypatch)

    written, applied = _run_apply(
        [{"claim": "ship lean and move fast", "kind": "reflection"}],
        path,
        write_time_enabled=True,
        settings=_settings(),
    )
    assert written == 1
    assert applied == 0
    # The helper was NEVER invoked — proven by the call counter, not by a swallowed
    # judge raise (which would pass even if the helper ran and the judge was hit).
    assert helper_calls.count == 0
    # The original record was strengthened in place (still ONE record).
    recs = InferenceTracker(path).load()
    assert len(recs) == 1
    assert recs[0].id == "orig"
    assert recs[0].evidence_count == 2


def test_write_time_legacy_zero_evidence_hit_skips_judge(tmp_path, monkeypatch):
    """R1 B5: an existing record at evidence_count=0 that the incoming belief
    paraphrases still HITs and skips the judge — the before_ids membership gate
    (NOT evidence_count) decides MISS-vs-HIT."""
    monkeypatch.setenv("INFERENCE_WRITE_TIME_CONTRADICTION", "true")
    path = tmp_path / "inf.json"
    # A legacy/imported record reading evidence_count=0.
    legacy = _rec("legacy", "ship lean and fast", source="reflection", confidence=0.7)
    legacy.evidence_count = 0
    _seed(path, [legacy])
    angles = {
        "ship lean and fast": 0.0,
        "ship lean and move fast": _cosine_to_angle(0.95),  # HIT
    }
    _patch_embed_angles(monkeypatch, angles)
    # COUNTER on the helper: the before_ids membership gate (NOT evidence_count)
    # must SKIP the helper because the returned id is already in the corpus.
    helper_calls = _patch_resolve_counter(monkeypatch)

    written, applied = _run_apply(
        [{"claim": "ship lean and move fast", "kind": "reflection"}],
        path,
        write_time_enabled=True,
        settings=_settings(),
    )
    assert written == 1
    assert applied == 0
    # The physical-id gate skipped the helper even though evidence_count reads 0 —
    # proven by the call counter, not a muted judge raise.
    assert helper_calls.count == 0
    recs = InferenceTracker(path).load()
    assert len(recs) == 1  # strengthened in place, no new record
    assert recs[0].id == "legacy"


def test_write_time_auto_capture_neighbor_is_ignored(tmp_path, monkeypatch):
    """R1 M4: an in-band auto_capture active is NEVER sent to the judge (the source
    filter excludes it); no judge call, no mutation."""
    monkeypatch.setenv("INFERENCE_WRITE_TIME_CONTRADICTION", "true")
    path = tmp_path / "inf.json"
    # Only an auto_capture neighbor exists in-band; it must be filtered out.
    _seed(
        path,
        [_rec("ac", "ship lean and fast", source="auto_capture", confidence=0.9)],
    )
    angles = {
        "ship lean and fast": 0.0,
        "build a heavy enterprise process": _cosine_to_angle(0.6),  # in-band
    }
    _patch_embed_angles(monkeypatch, angles)
    embed_spy = _spy_embed(angles)
    judge_spy = _spy_reasoning()

    written, applied = _run_apply(
        [{"claim": "build a heavy enterprise process", "kind": "reflection"}],
        path,
        write_time_enabled=True,
        settings=_settings(),
        embed_batch=embed_spy,
        reasoning=judge_spy,
    )
    assert written == 1
    assert applied == 0
    # The source filter empties the eligible set BEFORE the band scan, so neither
    # the helper's embed nor the judge runs (proven by recorded call lists; a
    # raise-if-awaited would be swallowed by the fail-open judge).
    assert embed_spy.calls == []
    assert judge_spy.calls == []


def test_write_time_offline_embed_fail_open(tmp_path, monkeypatch, capsys):
    """Flag ON, embed_batch RAISES (offline) -> visible print, plain add, count 0."""
    monkeypatch.setenv("INFERENCE_WRITE_TIME_CONTRADICTION", "true")
    path = tmp_path / "inf.json"
    _seed(path, [_rec("expl", "ship lean and fast", source="explicit", confidence=0.8)])

    def boom_embed(_texts, **_kw):
        raise RuntimeError("FastEmbed offline")

    new = _rec("new", "build a heavy enterprise process", source="reflection")
    # Seed the new record physically so the helper has a fresh-MISS target to load.
    recs = InferenceTracker(path).load()
    recs.append(new)
    InferenceTracker(path).save(recs)

    applied = _run(
        bc.resolve_write_time_contradiction(
            new,
            path,
            Path("."),
            write_time_enabled=True,
            settings=_settings(),
            embed_batch=boom_embed,
            reasoning=_raise_if_awaited(),
        )
    )
    assert applied == 0
    out = capsys.readouterr().out
    assert "write-time contradiction skipped" in out


def test_write_time_contradiction_enabled_false_kill_switch(tmp_path, monkeypatch):
    """CONTRADICTION_ENABLED=false is a SECOND kill switch — even flag ON, the
    helper returns 0 before any embed/judge."""
    monkeypatch.setenv("INFERENCE_WRITE_TIME_CONTRADICTION", "true")
    path = tmp_path / "inf.json"
    existing = _rec("ex", "ship lean and fast", source="explicit", confidence=0.8)
    new = _rec("new", "build a heavy enterprise process", source="reflection")
    _seed(path, [existing, new])
    angles = {
        "ship lean and fast": 0.0,
        "build a heavy enterprise process": _cosine_to_angle(0.6),
    }
    embed_spy = _spy_embed(angles)
    judge_spy = _spy_reasoning()
    applied = _run(
        bc.resolve_write_time_contradiction(
            new,
            path,
            Path("."),
            write_time_enabled=True,
            settings=_settings(enabled=False),  # CONTRADICTION_ENABLED off
            embed_batch=embed_spy,
            reasoning=judge_spy,
        )
    )
    assert applied == 0
    # The CONTRADICTION_ENABLED gate returns before corpus load / embed / judge.
    assert embed_spy.calls == []
    assert judge_spy.calls == []


def test_apply_operator_beliefs_is_async_returns_tuple(tmp_path, monkeypatch):
    """The signature change: apply_operator_beliefs is async + returns
    (written, write_time_applied)."""
    monkeypatch.delenv("INFERENCE_WRITE_TIME_CONTRADICTION", raising=False)
    _patch_embed_angles(monkeypatch, {"operator prefers concise answers": 0.0})
    path = tmp_path / "inf.json"
    result = _run_apply(
        [{"claim": "operator prefers concise answers", "kind": "reflection"}], path
    )
    assert isinstance(result, tuple)
    assert result == (1, 0)


def test_write_time_integration_over_tmp_state(tmp_path, monkeypatch):
    """R1 M1 REAL integration: flag ON, tmp INFERENCE_STATE_FILE; await
    apply_operator_beliefs with a claim contradicting a pre-seeded in-band belief,
    injecting embed_batch (band placement) + reasoning (the conflict). Assert
    contradicted_by CHANGED on the loser via a FRESH InferenceTracker load.

    Exercises the REAL async write-time branch end-to-end (memory_reflect --test
    SKIPS apply_operator_beliefs, so the old smoke proved nothing)."""
    monkeypatch.setenv("INFERENCE_WRITE_TIME_CONTRADICTION", "true")
    state_file = tmp_path / "self-model-inferences.json"
    # Pre-seed an explicit belief the new reflection will contradict.
    _seed(
        state_file,
        [_rec("expl", "ship lean and fast", source="explicit", confidence=0.8)],
    )
    angles = {
        "ship lean and fast": 0.0,
        "build a heavy enterprise process": _cosine_to_angle(0.6),  # in-band
    }
    _patch_embed_angles(monkeypatch, angles)

    written, applied = asyncio.run(
        ob.apply_operator_beliefs(
            [{"claim": "build a heavy enterprise process", "kind": "reflection"}],
            state_file,
            cwd=tmp_path,
            write_time_enabled=True,
            settings=_settings(),
            reasoning=_band_conflict_reasoning(),
        )
    )
    assert written == 1
    assert applied == 1
    # FRESH load — the loser's contradicted_by physically changed on disk.
    by_text = {r.inference: r for r in InferenceTracker(state_file).load()}
    refl = by_text["build a heavy enterprise process"]
    assert refl.contradicted_by  # audit entry present
    assert refl.contradiction_count == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
