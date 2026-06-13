"""Tests for Living Self Act 1 — Make The Self Real (fix the self-model source).

Categories map to the PRP's Validation Loop. Every test is tmp_path-scoped with
injected ``embed`` / ``now`` / ``reasoning`` and monkeypatched imports so the
REAL branches run — NO live state (self-model-inferences.json, chat.db, SELF.md,
vault) is ever touched. Born-clean: all ids/text are synthetic.

  1. Rule-1 settings resolver — env-swept defaults, monkeypatch flips on next call.
  2. Embedding dedup (discriminating, FAILS pre-fix): paraphrase converges /
     distinct stays separate / threshold honored / B3 fail-open / M1 skip-decayed
     / one real-embedding integration test (guarded skip offline).
  3. Capture cut (discriminating, FAILS pre-fix): assistant text never captures;
     user text still captures; capture writes ZERO inferences.
  4b. Verbatim operator-word reader (B2 + NB1): naive SQLite AND aware Postgres
      created_at BOTH return in-window user texts (not []); window/role/source/
      slash filters all applied; raising store -> [].
  4c. Renderer source-filter (B1, FAILS pre-fix): auto_capture invisible,
      reflection visible.
  5. Reflection extraction wires the never-fired sources (FAILS pre-fix) +
     end-to-end read->extract->apply seam.
  6. Extractor provider-agnostic + fail-open + tolerant-parse (M2).
  7. Corpus migration reversible + audited + atomic + provenance criterion (B1+M5).
  8. Region-budget parity (M4): net-zero BASE delta + common-path bound + boundary.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for _p in (str(_SCRIPTS_DIR), str(_CHAT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cognition import operator_beliefs as ob  # noqa: E402
from cognition import self_model as sm  # noqa: E402
from cognition.capture import auto_capture_from_turn, extract_candidates  # noqa: E402
from cognition.self_model import (  # noqa: E402
    InferenceRecord,
    InferenceTracker,
    quarantine_auto_capture,
)

import config  # noqa: E402

# ===========================================================================
# Helpers — synthetic embed + fake store (born-clean)
# ===========================================================================

_CLUSTER_A = {"prefers concise answers", "likes short replies"}
_CLUSTER_B = {"prefers dark mode", "wants verbose explanations"}


def _fake_embed(text: str):
    """Deterministic fake embedder.

    Maps every string in cluster A to ONE shared unit axis (so cluster-A members
    cosine == 1.0 -> merge) and cluster B to a different shared axis. Any OTHER
    string gets a distinct hashed axis so two unrelated claims never accidentally
    cosine-match (cosine == 0.0 -> stay separate). Used so dedup tests run the
    REAL cosine path with zero network / no FastEmbed download.
    """
    import hashlib

    import numpy as np

    # All fake vectors share ONE dimensionality so any dot-product is valid.
    dim = 64
    key = text.strip().lower()
    vec = np.zeros(dim, dtype=np.float32)
    if key in _CLUSTER_A:
        vec[0] = 1.0  # cluster A shares axis 0 -> members cosine == 1.0
        return vec
    if key in _CLUSTER_B:
        vec[1] = 1.0  # cluster B shares axis 1
        return vec
    # Anything else -> a distinct hashed axis in [2, dim) so two DIFFERENT
    # unknown strings are orthogonal (never falsely merge) and never collide
    # with the reserved cluster axes.
    h = int(hashlib.sha1(key.encode("utf-8")).hexdigest(), 16)
    vec[2 + (h % (dim - 2))] = 1.0
    return vec


class _FakeStore:
    """Minimal session store exposing list_active + list_messages."""

    def __init__(self, sessions, messages, *, raise_on=None):
        self._sessions = sessions
        self._messages = messages
        self._raise_on = raise_on

    def list_active(self, source=None, sources=None, platform=None):
        if self._raise_on == "list_active":
            raise RuntimeError("boom")
        return list(self._sessions)

    def list_messages(self, session_id, limit=200):
        if self._raise_on == "list_messages":
            raise RuntimeError("boom")
        return list(self._messages.get(session_id, []))


def _msg(role, content, created_at):
    return SimpleNamespace(role=role, content=content, created_at=created_at)


def _sess(session_id, updated_at):
    return SimpleNamespace(session_id=session_id, updated_at=updated_at)


def _patch_fake_embed_batch(monkeypatch):
    """Patch embeddings.embed_batch to use the deterministic fake (no network)."""
    monkeypatch.setattr(
        "embeddings.embed_batch",
        lambda texts, **kw: [_fake_embed(t) for t in texts],
    )


_CONCISE = "operator prefers concise answers"  # shared extractor test phrase


def _run_extract(turns, reasoning, **kw):
    """Drive extract_operator_beliefs in a sync test (keeps call sites short)."""
    return asyncio.run(
        ob.extract_operator_beliefs(turns, Path("."), reasoning=reasoning, **kw)
    )


# ===========================================================================
# 1. Rule-1 settings resolver
# ===========================================================================


def test_settings_resolver_locked_defaults(monkeypatch):
    for var in (
        "INFERENCE_DEDUP_THRESHOLD",
        "INFERENCE_EXTRACTION_ENABLED",
        "INFERENCE_EXTRACTION_MAX_CLAIMS",
        "INFERENCE_EXTRACTION_MIN_CHARS",
    ):
        monkeypatch.delenv(var, raising=False)
    s = config.get_inference_extraction_settings()
    assert s.dedup_threshold == 0.72  # measured BGE gap default (see config docstring)
    assert s.extraction_enabled is True
    assert s.max_claims == 8
    assert s.min_chars == 12


def test_settings_resolver_env_flips_on_next_call(monkeypatch):
    monkeypatch.setenv("INFERENCE_DEDUP_THRESHOLD", "0.95")
    monkeypatch.setenv("INFERENCE_EXTRACTION_ENABLED", "false")
    monkeypatch.setenv("INFERENCE_EXTRACTION_MAX_CLAIMS", "3")
    monkeypatch.setenv("INFERENCE_EXTRACTION_MIN_CHARS", "20")
    s = config.get_inference_extraction_settings()
    assert s.dedup_threshold == 0.95
    assert s.extraction_enabled is False
    assert s.max_claims == 3
    assert s.min_chars == 20


def test_settings_resolver_explicit_args_passthrough():
    s = config.get_inference_extraction_settings(
        dedup_threshold=0.5, extraction_enabled=False, max_claims=1, min_chars=2
    )
    assert s == (0.5, False, 1, 2)


# ===========================================================================
# 2. Embedding dedup — discriminating, FAILS pre-fix (old _similar is exact)
# ===========================================================================


def test_dedup_paraphrase_converges(tmp_path, monkeypatch):
    """Two paraphrases of one preference -> ONE record, evidence_count == 2."""
    _patch_fake_embed_batch(monkeypatch)
    tracker = InferenceTracker(tmp_path / "inf.json")
    tracker.add_inference("prefers concise answers", "obs1", 0.7, source="reflection")
    r2 = tracker.add_inference("likes short replies", "obs2", 0.7, source="reflection")
    records = tracker.load()
    assert len(records) == 1
    assert r2.evidence_count == 2


def test_dedup_distinct_stays_separate(tmp_path, monkeypatch):
    """Two genuinely different beliefs -> TWO records, both evidence_count == 1."""
    _patch_fake_embed_batch(monkeypatch)
    tracker = InferenceTracker(tmp_path / "inf.json")
    tracker.add_inference("prefers concise answers", "obs1", 0.7, source="reflection")
    tracker.add_inference("prefers dark mode", "obs2", 0.7, source="reflection")
    records = tracker.load()
    assert len(records) == 2
    assert all(r.evidence_count == 1 for r in records)


def test_dedup_threshold_is_live_knob(tmp_path, monkeypatch):
    """Same convergent pair at threshold 0.99 -> stays separate (proves Rule-1 knob)."""
    monkeypatch.setenv("INFERENCE_DEDUP_THRESHOLD", "0.99")
    # cosine of the two cluster-A vectors is exactly 1.0, so to make them NOT
    # merge we give them slightly-different axes whose cosine < 0.99.
    import numpy as np

    def near_embed(text):
        key = text.strip().lower()
        if key == "prefers concise answers":
            return np.array([1.0, 0.0], dtype=np.float32)
        # cosine with [1,0] == 0.9 (< 0.99)
        return np.array([0.9, np.sqrt(1 - 0.81)], dtype=np.float32)

    monkeypatch.setattr(
        "embeddings.embed_batch",
        lambda texts, **kw: [near_embed(t) for t in texts],
    )
    tracker = InferenceTracker(tmp_path / "inf.json")
    tracker.add_inference("prefers concise answers", "obs1", 0.7, source="reflection")
    tracker.add_inference("likes short replies", "obs2", 0.7, source="reflection")
    assert len(tracker.load()) == 2


def test_dedup_fail_open_on_raising_embed(tmp_path, monkeypatch):
    """B3: a raising embed_batch -> per-pair fallback -> exact-match merge, no crash.

    This is the regression that keeps the offline standing suite green: identical
    normalized strings still merge to ONE record, distinct strings stay separate,
    and add_inference never raises.
    """
    def boom(*a, **k):
        raise RuntimeError("FastEmbed offline")

    monkeypatch.setattr("embeddings.embed_batch", boom)
    monkeypatch.setattr("embeddings.embed_text", boom)
    tracker = InferenceTracker(tmp_path / "inf.json")
    tracker.add_inference("PREFERS  Concise", "obs1", 0.7, source="reflection")
    # normalized-equal -> exact-match fallback merges:
    r2 = tracker.add_inference("prefers concise", "obs2", 0.7, source="reflection")
    assert len(tracker.load()) == 1
    assert r2.evidence_count == 2
    # A distinct string still inserts a new record under the fallback.
    tracker.add_inference("totally different belief", "obs3", 0.7, source="reflection")
    assert len(tracker.load()) == 2


def test_cosine_similar_fail_open_returns_exact_match(monkeypatch):
    """_cosine_similar with a raising embed returns the EXACT-match result, no propagation."""
    def boom(_text):
        raise RuntimeError("offline")

    assert sm._cosine_similar("Hello World", "hello   world", embed=boom) is True
    assert sm._cosine_similar("hello world", "goodbye world", embed=boom) is False


def test_dedup_skips_decayed_records(tmp_path, monkeypatch):
    """M1: a fresh belief cannot merge into / resurrect a decayed poisoned record."""
    _patch_fake_embed_batch(monkeypatch)
    path = tmp_path / "inf.json"
    # Seed a decayed record whose text is a cluster-A paraphrase of the incoming claim.
    decayed = InferenceRecord(
        id="decayed-1",
        inference="prefers concise answers",
        observation="poison",
        confidence=0.3,
        evidence_count=1,
        first_seen="2026-01-01T00:00:00+00:00",
        last_updated="2026-01-01T00:00:00+00:00",
        source="auto_capture",
        status="decayed",
    )
    InferenceTracker(path).save([decayed])
    tracker = InferenceTracker(path)
    new = tracker.add_inference("likes short replies", "obs", 0.7, source="reflection")
    records = tracker.load()
    assert len(records) == 2  # fresh record inserted, NOT merged into the decayed one
    assert new.evidence_count == 1
    decayed_after = next(r for r in records if r.id == "decayed-1")
    assert decayed_after.status == "decayed"
    assert decayed_after.evidence_count == 1  # untouched


def test_dedup_real_embeddings_integration(tmp_path, monkeypatch):
    """ONE real-model test (guarded skip offline) — asserts the measured 0.72 gap.

    Uses pairs measured against the live BGE-base-en-v1.5 this session:
    paraphrase "wants dark mode" / "prefers a dark theme" == 0.900 (>= 0.72 ->
    converge); distinct "prefers concise answers" / "prefers dark mode" == 0.614
    (< 0.72 -> separate). Proves the DEFAULT threshold actually converges a real
    paraphrase while keeping distinct beliefs apart on the real model.
    """
    threshold = config.get_inference_extraction_settings().dedup_threshold
    try:
        from embeddings import embed_text
    except Exception:
        pytest.skip("embeddings module unavailable")
    try:
        va = embed_text("operator wants dark mode")
        vb = embed_text("operator prefers a dark theme")
        vc = embed_text("operator prefers concise answers")
    except Exception:
        pytest.skip("FastEmbed model not available offline (130MB download)")
    sim_para = float(va @ vb)
    sim_dist = float(va @ vc)
    assert sim_para >= threshold, f"paraphrase cosine {sim_para} should be >= {threshold}"
    assert sim_dist < threshold, f"distinct cosine {sim_dist} should be < {threshold}"


# ===========================================================================
# 3. Capture cut — discriminating, FAILS pre-fix
# ===========================================================================


def test_capture_assistant_text_never_captures():
    """Assistant UX prose must NOT produce a candidate (pre-fix: a 'preference')."""
    cands = extract_candidates(
        "hi",
        "- End by asking whether the user wants edits, variants, or approval prep.",
    )
    assert cands == []


def test_capture_user_text_still_captures():
    """Operator words still produce a preference candidate."""
    cands = extract_candidates("I prefer concise answers", "ok")
    assert any(c.candidate_type == "preference" for c in cands)


def test_capture_writes_zero_inferences(tmp_path, monkeypatch):
    """auto_capture_from_turn never calls add_inference (pre-fix: >=1 auto_capture write)."""
    from cognition.staging import StagingStore

    calls = []
    monkeypatch.setattr(
        InferenceTracker,
        "add_inference",
        lambda self, *a, **k: calls.append((a, k)),
    )
    store = StagingStore(tmp_path / "staging.jsonl")
    auto_capture_from_turn(
        "I prefer concise answers",
        "- End by asking whether the user wants edits, variants, or approval prep.",
        store,
        session_id="syn",
        turn_number=1,
    )
    assert calls == []


# ===========================================================================
# 4b. Verbatim operator-word reader (B2 + NB1) — naive AND aware created_at
# ===========================================================================


def _patch_now_local(monkeypatch, dt):
    monkeypatch.setattr(config, "now_local", lambda: dt)


def test_reader_naive_created_at_returns_in_window(monkeypatch):
    """NB1: PRODUCTION-SHAPED NAIVE created_at (SQLite) must return in-window texts, not [].

    A raw naive>=aware comparison would raise TypeError, get swallowed, and
    return [] forever. The fixture deliberately uses tzinfo=None datetimes that
    mirror live chat.db ('2026-06-12T03:52:48.957105', tzinfo=None).
    """
    import session

    # window_start is tz-AWARE (now_local()-style).
    window_start = datetime(2026, 6, 12, 0, 0, 0, tzinfo=timezone(timedelta(hours=-7)))
    sessions = [_sess("s1", datetime(2026, 6, 12, 10, 0, 0))]  # naive updated_at
    messages = {
        "s1": [
            # in-window naive (kept)
            _msg("user", "I prefer concise answers", datetime(2026, 6, 12, 9, 0, 0)),
            # role excluded:
            _msg("assistant", "sure thing", datetime(2026, 6, 12, 9, 0, 1)),
            # out-of-window:
            _msg("user", "old belief", datetime(2026, 6, 1, 9, 0, 0)),
            # slash command excluded:
            _msg("user", "/status", datetime(2026, 6, 12, 9, 5, 0)),
        ]
    }
    store = _FakeStore(sessions, messages)
    turns = session.read_operator_user_turns(window_start, store=store)
    assert turns == ["I prefer concise answers"]


def test_reader_aware_created_at_returns_in_window(monkeypatch):
    """NB1: AWARE created_at (Postgres TIMESTAMPTZ) must also return in-window texts."""
    import session

    tz = timezone(timedelta(hours=-7))
    window_start = datetime(2026, 6, 12, 0, 0, 0, tzinfo=tz)
    sessions = [_sess("s1", datetime(2026, 6, 12, 10, 0, 0, tzinfo=tz))]
    messages = {
        "s1": [
            _msg("user", "aware in-window belief", datetime(2026, 6, 12, 9, 0, 0, tzinfo=tz)),
            _msg("user", "aware old belief", datetime(2026, 6, 1, 9, 0, 0, tzinfo=tz)),
        ]
    }
    store = _FakeStore(sessions, messages)
    turns = session.read_operator_user_turns(window_start, store=store)
    assert turns == ["aware in-window belief"]


def test_reader_session_window_prefilter(monkeypatch):
    """NM1: a session whose updated_at predates the window is not descended into (break)."""
    import session

    tz = timezone(timedelta(hours=-7))
    window_start = datetime(2026, 6, 12, 0, 0, 0, tzinfo=tz)
    # list_active is updated_at DESC; the second session is older than the window.
    sessions = [
        _sess("recent", datetime(2026, 6, 12, 10, 0, 0)),
        _sess("ancient", datetime(2026, 5, 1, 10, 0, 0)),
    ]
    messages = {
        "recent": [_msg("user", "recent belief", datetime(2026, 6, 12, 9, 0, 0))],
        "ancient": [_msg("user", "ancient belief", datetime(2026, 5, 1, 9, 0, 0))],
    }
    store = _FakeStore(sessions, messages)
    turns = session.read_operator_user_turns(window_start, store=store)
    assert turns == ["recent belief"]  # ancient session never read


def test_reader_raising_store_returns_empty():
    """Non-blocking: a store whose calls raise -> []."""
    import session

    window_start = datetime(2026, 6, 12, 0, 0, 0, tzinfo=timezone(timedelta(hours=-7)))
    store = _FakeStore([], {}, raise_on="list_active")
    assert session.read_operator_user_turns(window_start, store=store) == []


# ===========================================================================
# 4c. Renderer source-filter (B1) — discriminating, FAILS pre-fix
# ===========================================================================


def test_renderer_source_filter_hides_auto_capture(tmp_path, monkeypatch):
    """The live renderer injects reflection text but NOT auto_capture (pre-fix: both)."""
    import engine as engine_mod

    path = tmp_path / "inf.json"
    records = [
        InferenceRecord(
            id="a", inference="LEGACY AUTO CAPTURE LINE", observation="x",
            confidence=0.9, source="auto_capture", status="active",
            last_updated="2026-06-12T00:00:00+00:00",
        ),
        InferenceRecord(
            id="r", inference="OPERATOR PREFERS CONCISE", observation="x",
            confidence=0.9, source="reflection", status="active",
            last_updated="2026-06-12T00:00:00+00:00",
        ),
    ]
    InferenceTracker(path).save(records)
    monkeypatch.setattr("config.INFERENCE_STATE_FILE", path)
    monkeypatch.setattr("config.INFERENCE_PROMPT_MIN_CONFIDENCE", 0.3)
    monkeypatch.setattr("config.INFERENCE_PROMPT_CAP", 10)

    # Call the unbound method with a throwaway object — it only reads config + tracker.
    out = engine_mod.ConversationEngine._build_active_inference_region(SimpleNamespace())
    assert "OPERATOR PREFERS CONCISE" in out
    assert "LEGACY AUTO CAPTURE LINE" not in out


# ===========================================================================
# 5. Reflection extraction wires the never-fired sources + end-to-end seam
# ===========================================================================


def test_apply_operator_beliefs_writes_reflection_and_explicit(tmp_path, monkeypatch):
    """FAILS pre-fix (those sources had no writer): explicit+reflection records appear."""
    _patch_fake_embed_batch(monkeypatch)
    path = tmp_path / "inf.json"
    claims = [
        {"claim": "operator prefers concise", "confidence": 0.8, "kind": "inferred"},
        {"claim": "operator always tests before shipping", "confidence": 0.9, "kind": "explicit"},
    ]
    n = ob.apply_operator_beliefs(claims, path)
    assert n == 2
    records = InferenceTracker(path).load()
    sources = {r.source for r in records}
    assert sources == {"reflection", "explicit"}
    assert not any(r.source == "auto_capture" for r in records)


def test_end_to_end_read_extract_apply_seam(tmp_path, monkeypatch):
    """The reflection seam: read verbatim turns -> extract -> apply non-auto_capture records.

    Drives the exact composition the memory_reflect insertion runs (no real LLM,
    no real store): read_operator_user_turns returns verbatim turns, a stub
    extractor returns claims, apply writes them.
    """
    import session

    _patch_fake_embed_batch(monkeypatch)
    path = tmp_path / "inf.json"

    window_start = datetime(2026, 6, 12, 0, 0, 0, tzinfo=timezone(timedelta(hours=-7)))
    store = _FakeStore(
        [_sess("s1", datetime(2026, 6, 12, 10, 0, 0))],
        {"s1": [_msg("user", "I always test before shipping", datetime(2026, 6, 12, 9, 0, 0))]},
    )
    turns = session.read_operator_user_turns(window_start, store=store)
    assert turns == ["I always test before shipping"]

    async def fake_extract(user_turns, cwd, **kw):
        assert user_turns == turns  # verbatim turns flow through
        return [{"claim": "operator tests before shipping", "confidence": 0.9, "kind": "explicit"}]

    claims = asyncio.run(fake_extract(turns, cwd=tmp_path))
    written = ob.apply_operator_beliefs(claims, path)
    assert written == 1
    records = InferenceTracker(path).load()
    assert records[0].source == "explicit"
    assert not any(r.source == "auto_capture" for r in records)


# ===========================================================================
# 6. Extractor — provider-agnostic + fail-open + tolerant-parse (M2)
# ===========================================================================


def test_extractor_disabled_returns_empty_without_reasoning():
    settings = config.get_inference_extraction_settings(extraction_enabled=False)
    called = []

    async def reasoning(*a, **k):
        called.append(1)
        return SimpleNamespace(parsed=[], model="x")

    out = asyncio.run(
        ob.extract_operator_beliefs(
            ["hi"], Path("."), settings=settings, reasoning=reasoning
        )
    )
    assert out == []
    assert called == []


def test_extractor_empty_turns_returns_empty():
    out = asyncio.run(ob.extract_operator_beliefs([], Path(".")))
    assert out == []


def test_extractor_raising_reasoning_fails_open():
    async def reasoning(*a, **k):
        raise RuntimeError("provider down")

    assert _run_extract([_CONCISE], reasoning) == []


def test_extractor_tolerant_parse_dict_wrap():
    """M2: a {"claims":[...]} provider-wrap is STILL extracted (Codex/Gemini variance)."""
    async def reasoning(*a, **k):
        return SimpleNamespace(
            parsed={"claims": [{"claim": _CONCISE, "kind": "inferred"}]},
            model="codex",
        )

    out = _run_extract([_CONCISE], reasoning)
    assert len(out) == 1
    assert out[0]["claim"] == _CONCISE


def test_extractor_bare_dict_no_list_returns_empty():
    async def reasoning(*a, **k):
        return SimpleNamespace(parsed={"status": "ok"}, model="gemini")

    assert _run_extract([_CONCISE], reasoning) == []


def test_extractor_min_chars_floor():
    async def reasoning(*a, **k):
        return SimpleNamespace(parsed=[{"claim": "short", "kind": "inferred"}], model="x")

    # "short" (5 chars) < min_chars 12
    assert _run_extract([_CONCISE], reasoning) == []


def test_extractor_langfuse_none_no_crash(monkeypatch):
    """Rule 3 fail-open: get_observation_client -> None -> claims still returned."""
    from runtime import langfuse_setup

    monkeypatch.setattr(langfuse_setup, "get_observation_client", lambda: None)

    async def reasoning(*a, **k):
        return SimpleNamespace(
            parsed=[{"claim": _CONCISE, "kind": "inferred"}], model="x"
        )

    assert len(_run_extract([_CONCISE], reasoning)) == 1


def test_coerce_claim_list_branches():
    assert ob._coerce_claim_list([{"claim": "x"}]) == [{"claim": "x"}]
    assert ob._coerce_claim_list({"beliefs": [{"claim": "y"}]}) == [{"claim": "y"}]
    assert ob._coerce_claim_list({"only_list": [1, 2]}) == [1, 2]  # sole list value
    assert ob._coerce_claim_list({"a": [1], "b": [2]}) == []  # two unknown lists -> safe
    assert ob._coerce_claim_list(None) == []
    assert ob._coerce_claim_list("garbage") == []


# ===========================================================================
# 7. Corpus migration — reversible + audited + atomic + provenance criterion
# ===========================================================================


def _seed_corpus(path):
    # bot1: a bot UX/offer line (regex DOES match -> "bot UX/offer line" reason).
    bot_line = "- End by asking whether the user wants edits, variants, or approval prep."
    records = [
        InferenceRecord(
            id="bot1", inference=bot_line,
            observation="x", confidence=0.5, source="auto_capture", status="confirmed",
        ),
        # frag1: a raw fragment the regex does NOT match -> still quarantined by provenance.
        InferenceRecord(
            id="frag1", inference="com/opensouls kind of like this",
            observation="x", confidence=0.4, source="auto_capture", status="decayed",
        ),
        InferenceRecord(
            id="keep1", inference="operator prefers concise answers",
            observation="x", confidence=0.8, source="reflection", status="active",
        ),
    ]
    InferenceTracker(path).save(records)
    return records


def test_migration_dry_run_writes_nothing(tmp_path):
    path = tmp_path / "inf.json"
    _seed_corpus(path)
    before = path.read_text(encoding="utf-8")
    report = quarantine_auto_capture(path, dry_run=True)
    assert report.total == 3
    assert report.quarantined == 2
    assert report.kept == 1
    assert report.backup_path is None
    assert path.read_text(encoding="utf-8") == before  # untouched
    # no backup file created
    assert list(tmp_path.glob("*.bak.json")) == []


def test_migration_real_run_quarantines_by_provenance(tmp_path):
    """B1: EVERY auto_capture removed (incl. the non-bot raw fragment), reflection kept."""
    path = tmp_path / "inf.json"
    original = _seed_corpus(path)
    report = quarantine_auto_capture(path, dry_run=False)
    assert report.quarantined == 2
    assert report.kept == 1
    remaining = InferenceTracker(path).load()
    assert [r.id for r in remaining] == ["keep1"]
    assert all(r.source != "auto_capture" for r in remaining)
    # The raw fragment (regex does NOT match) was STILL quarantined -> provenance criterion.
    assert "frag1" not in {r.id for r in remaining}

    # Reversible: backup holds ALL originals byte-for-byte (on the records).
    assert report.backup_path is not None
    bak = json.loads(Path(report.backup_path).read_text(encoding="utf-8"))
    assert [r["id"] for r in bak] == [r.id for r in original]


def test_migration_decision_table_annotations(tmp_path):
    """The regex sets the reason LABEL only (bot UX line vs auto_capture provenance)."""
    path = tmp_path / "inf.json"
    _seed_corpus(path)
    report = quarantine_auto_capture(path, dry_run=True)
    reasons = dict(report.decisions)
    # bot1 text matches a _BOT_QUOTE_RES pattern -> "bot UX/offer line"
    bot_reason = next(v for k, v in report.decisions if k.startswith("- End by asking"))
    assert bot_reason == "bot UX/offer line"
    # the raw fragment does not match -> "auto_capture provenance"
    frag_reason = next(v for k, v in report.decisions if k.startswith("com/opensouls"))
    assert frag_reason == "auto_capture provenance"
    assert reasons  # table is populated


def test_save_is_atomic_no_tmp_sibling(tmp_path, monkeypatch):
    """M5: save uses os.replace and leaves no .tmp sibling."""
    path = tmp_path / "inf.json"
    replace_calls = []
    real_replace = sm.os.replace

    def spy_replace(src, dst):
        replace_calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(sm.os, "replace", spy_replace)
    tracker = InferenceTracker(path)
    tracker.save([InferenceRecord(id="1", inference="x", observation="y", confidence=0.5)])
    assert replace_calls, "save did not call os.replace"
    assert not (tmp_path / "inf.json.tmp").exists()
    assert path.exists()


# ===========================================================================
# 8. Region-budget parity (M4) — net-zero BASE delta + common path + boundary
# ===========================================================================


def test_region_budget_base_sum_unchanged():
    """Net-zero BASE delta: SELF_MODEL + USER_INFERENCES + PREFETCHED == 3700.

    Pre-change was 400 + 300 + 3000 == 3700; post-change is 700 + 500 + 2500 ==
    3700. With the unchanged final 27K clamp this guarantees NO new overflow.
    Deliberately NOT a byte-identity assertion (apply_process_weights multiplies
    by clamped weights -> a weighted assembly is not char-identical).
    """
    b = config.REGION_BUDGETS
    assert b["self_model"] == 700
    assert b["user_inferences"] == 500
    assert b["prefetched_context"] == 2500
    assert b["self_model"] + b["user_inferences"] + b["prefetched_context"] == 3700


def test_region_budget_common_path_under_cap():
    """A realistic common-path assembly (prefetched EMPTY) stays under 27000 chars.

    Token budgets convert to chars via *4. Fill identity + self_model + user_model
    + durable_memory + user_inferences to NEW budgets; prefetched_context EMPTY
    (no router prefetch on a normal turn); recall/continuity/recent empty.
    """
    b = config.REGION_BUDGETS
    common_tokens = (
        b["identity"]
        + b["self_model"]
        + b["user_model"]
        + b["durable_memory"]
        + b["user_inferences"]
        + b["working_memory"]
    )
    common_chars = common_tokens * 4
    assert common_chars < 27000, f"common-path {common_chars} chars must be < 27000"


def test_truncate_win32_boundary_unchanged():
    """Pure regression on the existing helper — boundary unchanged."""
    import engine as engine_mod

    over = "x" * 30000
    out = engine_mod._truncate_win32_append(over, 27000)
    assert len(out) == 27000 + len("\n[TRUNCATED]")
    assert out.endswith("\n[TRUNCATED]")
    # under-cap passes through untouched
    under = "y" * 100
    assert engine_mod._truncate_win32_append(under, 27000) == under
