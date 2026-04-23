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
    from runtime import langfuse_setup
    from evolve.config_override import isolate_langfuse

    # Baseline depends on .env; we only assert it round-trips.
    baseline = langfuse_setup.is_langfuse_enabled()

    with isolate_langfuse():
        assert langfuse_setup.is_langfuse_enabled() is False

    assert langfuse_setup.is_langfuse_enabled() is baseline


def test_replay_context_composes_all_three():
    """`replay_context` must apply config overrides, recall-log isolation, and
    Langfuse disablement in one block. All three must restore on exit."""
    import config
    import recall_service
    from runtime import langfuse_setup
    from evolve.config_override import replay_context

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
    """Phase 2.4 needs to opt IN to tracing — confirm the flag is respected."""
    from runtime import langfuse_setup
    from evolve.config_override import replay_context

    baseline = langfuse_setup.is_langfuse_enabled()

    with replay_context({}, disable_tracing=False):
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
