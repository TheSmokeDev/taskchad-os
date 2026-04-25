"""Unit tests for evolve.config_override — the three isolation primitives.

These guard the invariants that make the replay harness safe to run against
the live vault: no attribute leaks, exception-safe restores, typo detection,
recall-log ring buffer unpolluted, and Langfuse `@observe` neutered for the
block's duration.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for _p in (_SCRIPTS_DIR, _CHAT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def test_override_config_patches_and_restores():
    import config
    from evolve.config_override import override_config

    original = config.RECALL_MIN_SCORE
    assert original == 0.3

    with override_config(RECALL_MIN_SCORE=0.9) as applied:
        assert applied == {"RECALL_MIN_SCORE": 0.9}
        assert config.RECALL_MIN_SCORE == 0.9

    assert config.RECALL_MIN_SCORE == original


def test_override_config_restores_on_exception():
    import config
    from evolve.config_override import override_config

    original = config.TIER1_MAX_RESULTS
    assert original == 5

    with pytest.raises(RuntimeError):
        with override_config(TIER1_MAX_RESULTS=99):
            assert config.TIER1_MAX_RESULTS == 99
            raise RuntimeError("boom")

    assert config.TIER1_MAX_RESULTS == original


def test_override_config_rejects_unknown_attrs():
    """Typo detection — refusing silent attr creation prevents the 'I set the
    flag and nothing happened' class of bug."""
    from evolve.config_override import override_config

    with pytest.raises(AttributeError, match="does not exist"):
        with override_config(RECALL_DOES_NOT_EXIST=1):
            pass


def test_isolate_recall_side_effects_stubs_persist_log():
    """Ensures the replay harness can't pollute the production recall ring buffer."""
    import recall_service

    from evolve.config_override import isolate_recall_side_effects

    original = recall_service._persist_log

    with isolate_recall_side_effects():
        assert recall_service._persist_log is not original
        # The stub must still be callable without raising so caller code
        # that invokes it unconditionally doesn't break.
        recall_service._persist_log("anything")

    assert recall_service._persist_log is original


def test_isolate_langfuse_forces_disabled():
    """With .env LANGFUSE_ENABLED=true, is_langfuse_enabled() should return
    True baseline and False inside the isolation block. This guard keeps
    the replay harness from emitting spans to the live Langfuse project."""
    from evolve.config_override import isolate_langfuse
    from runtime import langfuse_setup

    # Baseline depends on .env; we only assert it round-trips.
    baseline = langfuse_setup.is_langfuse_enabled()

    with isolate_langfuse():
        assert langfuse_setup.is_langfuse_enabled() is False

    assert langfuse_setup.is_langfuse_enabled() is baseline


def test_replay_context_composes_all_three():
    """`replay_context` must apply config overrides, recall-log isolation, and
    Langfuse disablement in one block. All three must restore on exit."""
    import recall_service

    import config
    from evolve.config_override import replay_context
    from runtime import langfuse_setup

    orig_score = config.RECALL_MIN_SCORE
    orig_persist = recall_service._persist_log
    orig_enabled = langfuse_setup.is_langfuse_enabled

    with replay_context({"RECALL_MIN_SCORE": 0.5}):
        assert config.RECALL_MIN_SCORE == 0.5
        assert recall_service._persist_log is not orig_persist
        assert langfuse_setup.is_langfuse_enabled() is False

    assert config.RECALL_MIN_SCORE == orig_score
    assert recall_service._persist_log is orig_persist
    assert langfuse_setup.is_langfuse_enabled is orig_enabled


def test_replay_context_disable_tracing_false_keeps_langfuse_live():
    """Phase 2.4 needs to opt IN to tracing — confirm the flag is respected.

    Note: when this assertion holds AND Langfuse is configured, a real
    `replay_run` span gets emitted under user_id="evolve-replay" — that's
    the production behavior Phase 2.4 enables. The new tag-required guard
    is asserted separately in test_evolve_tracing.py.
    """
    from evolve.config_override import replay_context
    from evolve.replay_tracing import build_experiment_tag
    from runtime import langfuse_setup

    baseline = langfuse_setup.is_langfuse_enabled()
    tag = build_experiment_tag("exp-test-isolation", {}, None)

    with replay_context({}, disable_tracing=False, experiment_tag=tag):
        # Pass-through — should equal the baseline, not be forced False.
        assert langfuse_setup.is_langfuse_enabled() is baseline


def test_snapshot_config_returns_all_requested_keys():
    """Report provenance must not silently drop keys that don't exist — unknown
    keys come back as None so typos surface instead of hiding."""
    from evolve.config_override import RECALL_CONFIG_KEYS, snapshot_config

    snap = snapshot_config(RECALL_CONFIG_KEYS)
    assert set(snap.keys()) == set(RECALL_CONFIG_KEYS)
    # At minimum these are booleans/floats/ints — not None.
    assert snap["RECALL_ENABLED"] is not None
    assert snap["RECALL_MIN_SCORE"] is not None

    # Unknown key path
    weird = snapshot_config(["NOT_A_REAL_KEY"])
    assert weird == {"NOT_A_REAL_KEY": None}


def test_classify_tier_does_not_write_span_when_tracing_disabled():
    """Regression for #15: classify_tier() must not call get_client().update_current_span()
    when is_langfuse_enabled() returns False.

    The old code called get_client() directly (no guard). Under a traced caller
    with an ambient root span, this leaked tier metadata onto the live trace even
    inside isolate_langfuse().

    Patch target is `langfuse.get_client` (the real source) because classify_tier
    does a lazy local `from langfuse import get_client` that binds langfuse.get_client,
    not a module-level attribute of cognition.recall. Mocking the wrong target would
    make this test pass trivially (Codex adversarial review finding on PR #16).
    """
    from unittest.mock import MagicMock, patch

    from evolve.config_override import isolate_langfuse
    from cognition.recall import RecallTier, classify_tier

    mock_client = MagicMock()

    with isolate_langfuse():
        with patch("langfuse.get_client", return_value=mock_client) as mock_get_client:
            result = classify_tier("What happened with my leads yesterday?")

    # Tier classification still returns correctly
    assert result == RecallTier.TIER_1
    # The critical assertions: get_client itself must not have been reached,
    # and no span write can have been dispatched.
    mock_get_client.assert_not_called()
    mock_client.update_current_span.assert_not_called()

def test_classify_tier_writes_span_when_tracing_enabled():
    """Symmetric counterpart to test_classify_tier_does_not_write_span_when_tracing_disabled.

    Verifies that update_current_span IS called when is_langfuse_enabled() returns True.
    Guards against a future over-aggressive guard that silences all tracing without
    any test catching the regression.

    Patch target is langfuse.get_client because classify_tier() does a lazy local
    import (`from langfuse import get_client`) which binds langfuse.get_client, not
    a module-level attribute of cognition.recall.
    """
    from unittest.mock import MagicMock, patch

    from cognition.recall import RecallTier, classify_tier

    mock_client = MagicMock()

    with patch("runtime.langfuse_setup.is_langfuse_enabled", return_value=True):
        with patch("langfuse.get_client", return_value=mock_client):
            result = classify_tier("What happened with my leads yesterday?")

    # Tier classification returns correctly
    assert result == RecallTier.TIER_1
    # The critical assertion: update_current_span MUST have been called with metadata=
    mock_client.update_current_span.assert_called_once_with(
        metadata={"tier": RecallTier.TIER_1.value}
    )


def test_search_hybrid_weights_affect_scoring_at_call_time():
    """Regression for #11: weights must be read from config at call time so
    override_config changes change actual search behavior.

    Protocol:
      - Import memory_search BEFORE override fires (warm-process scenario).
      - Mock keyword_search + vector_search to return a single row each with
        known scores. The row shares a chunk key so scores merge in one entry.
      - Known baseline: keyword=0.8, semantic=0.2. Default weights: vec=0.7,
        kw=0.3 -> combined = 0.7*0.2 + 0.3*0.8 = 0.38.
      - Override vector_weight=0.0 -> combined = 0.0*0.2 + 0.3*0.8 = 0.24.
      - Set min_score=0.3 under the override -> 0.24 < 0.3 -> row filtered out.
      - Same setup WITHOUT the override -> combined=0.38 > 0.3 -> row survives.

    Outcome-based: if the fix regresses to baked defaults, override is ignored,
    row passes filtering, test fails.
    """
    import memory_search
    from evolve.config_override import override_config
    from unittest.mock import patch, MagicMock

    def make_mock_db():
        mock_db = MagicMock()
        row_tmpl = {
            "file_path": "/alpha", "start_line": 1, "end_line": 10,
            "content": "keyword-heavy row", "section_title": "",
        }
        mock_db.keyword_search.return_value = [{**row_tmpl, "score": 0.8}]
        mock_db.vector_search.return_value = [{**row_tmpl, "score": 0.2}]
        return mock_db

    with patch("embeddings.embed_text", return_value=[0.0] * 384):
        with patch("memory_search.get_memory_db", return_value=make_mock_db()):
            baseline = memory_search.search_hybrid("any", min_score=0.3)
        assert len(baseline) == 1
        assert baseline[0].score == pytest.approx(0.38, abs=0.001)

        with override_config(SEARCH_VECTOR_WEIGHT=0.0):
            with patch("memory_search.get_memory_db", return_value=make_mock_db()):
                candidate = memory_search.search_hybrid("any", min_score=0.3)
        assert len(candidate) == 0


def test_search_hybrid_limit_respects_config_override():
    """Regression for #11 (Codex finding F2): SEARCH_DEFAULT_LIMIT must also
    read at call time."""
    import memory_search
    from evolve.config_override import override_config
    from unittest.mock import patch, MagicMock

    def make_db_with_n_rows(n):
        mock_db = MagicMock()
        rows = [
            {"file_path": f"/path{i}", "start_line": 1, "end_line": 10,
             "content": "row", "score": 1.0 - i * 0.01, "section_title": ""}
            for i in range(n)
        ]
        mock_db.keyword_search.return_value = rows
        mock_db.vector_search.return_value = rows
        return mock_db

    with patch("embeddings.embed_text", return_value=[0.0] * 384):
        with override_config(SEARCH_DEFAULT_LIMIT=3):
            with patch("memory_search.get_memory_db", return_value=make_db_with_n_rows(10)):
                results = memory_search.search_hybrid("any", min_score=0.0)
        assert len(results) == 3


def test_search_functions_use_none_sentinel_for_limit():
    """Structural guard for #11: all 4 search entry points must default
    limit to None so runtime config reads flow through."""
    import memory_search
    import inspect

    for fn_name in ("search_keyword", "search_semantic", "search_hybrid", "search"):
        fn = getattr(memory_search, fn_name)
        sig = inspect.signature(fn)
        assert sig.parameters["limit"].default is None, (
            f"{fn_name} must use None sentinel for limit"
        )


def test_search_semantic_sentinel_in_defaults():
    """Structural guard for #11: search_semantic.min_score must default to None."""
    import memory_search
    import inspect
    sig = inspect.signature(memory_search.search_semantic)
    assert sig.parameters["min_score"].default is None


def test_search_hybrid_sentinel_in_defaults():
    """Structural guard for #11: search_hybrid weights must default to None."""
    import memory_search
    import inspect
    sig = inspect.signature(memory_search.search_hybrid)
    assert sig.parameters["vector_weight"].default is None
    assert sig.parameters["keyword_weight"].default is None
    assert sig.parameters["min_score"].default is None
