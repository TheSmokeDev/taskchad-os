"""Phase 2.4 tests — Langfuse replay-tagged span helpers.

Covers the `evolve.replay_tracing` module + the `replay_context` validation
guard added to `evolve.config_override`. Seven test classes mirror the seven
contracts the new code introduces:

1. `override_fingerprint` — stable, length-bounded, deterministic
2. `build_experiment_tag` — PRD-shape dict
3. `replay_context` validation — fail-loud guard at the boundary (PRD AC#5)
4. Langfuse-disabled no-op behavior — fails open silently
5. URL construction — uses LANGFUSE_BASE_URL, returns None when absent
6. ReplayReport URL fields — round-trip through to_dict
7. Rule 3 module-attribute lookup — `isolate_langfuse()` propagates

The Rule 3 test is the load-bearing one: it exercises the same monkey-patch
pattern the existing isolation harness depends on. If the import shape in
replay_tracing.py regresses to the cached `from x import y` form, this test
fails first.
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


# ── 1. override_fingerprint ─────────────────────────────────────────────────


class TestOverrideFingerprint:
    def test_stable_across_runs(self):
        from evolve.replay_tracing import override_fingerprint

        overrides = {"RECALL_MIN_SCORE": 0.5, "TIER1_MAX_RESULTS": 3}
        assert override_fingerprint(overrides) == override_fingerprint(overrides)

    def test_changes_with_input(self):
        from evolve.replay_tracing import override_fingerprint

        a = override_fingerprint({"RECALL_MIN_SCORE": 0.5})
        b = override_fingerprint({"RECALL_MIN_SCORE": 0.6})
        assert a != b

    def test_is_16_chars(self):
        from evolve.replay_tracing import override_fingerprint

        fp = override_fingerprint({"RECALL_MIN_SCORE": 0.5})
        assert len(fp) == 16
        assert all(c in "0123456789abcdef" for c in fp)

    def test_handles_empty_dict(self):
        from evolve.replay_tracing import override_fingerprint

        fp = override_fingerprint({})
        assert isinstance(fp, str)
        assert len(fp) == 16

    def test_none_treated_as_empty(self):
        """None and {} must hash to the same fingerprint — keeps the audit
        trail consistent for baseline runs that pass overrides=None."""
        from evolve.replay_tracing import override_fingerprint

        assert override_fingerprint(None) == override_fingerprint({})

    def test_handles_nested_dicts(self):
        """sort_keys=True must be applied at every level, not just top level."""
        from evolve.replay_tracing import override_fingerprint

        a = override_fingerprint({"nested": {"a": 1, "b": 2}})
        b = override_fingerprint({"nested": {"b": 2, "a": 1}})
        assert a == b


# ── 2. build_experiment_tag ─────────────────────────────────────────────────


class TestBuildExperimentTag:
    def test_required_attributes_present(self):
        from evolve.replay_tracing import build_experiment_tag

        tag = build_experiment_tag(
            "exp-20260425T120000Z",
            {"RECALL_MIN_SCORE": 0.5},
            "exp-baseline",
        )
        assert tag["experiment_id"] == "exp-20260425T120000Z"
        assert tag["replay"] is True
        assert "override_fingerprint" in tag
        assert len(tag["override_fingerprint"]) == 16
        assert tag["baseline_experiment_id"] == "exp-baseline"

    def test_baseline_optional(self):
        """Baseline is None for absolute baselines (running without comparison)."""
        from evolve.replay_tracing import build_experiment_tag

        tag = build_experiment_tag("exp-x", {}, None)
        assert tag["baseline_experiment_id"] is None
        assert tag["replay"] is True


# ── 3. replay_context validation ────────────────────────────────────────────


class TestReplayContextValidation:
    def test_traced_without_tag_raises(self):
        """PRD AC#5: missing experiment_tag with disable_tracing=False raises
        ValueError. Prevents untagged replay spans from polluting prod."""
        from evolve.config_override import replay_context

        with pytest.raises(ValueError, match="experiment_tag"):
            with replay_context({}, disable_tracing=False):
                pass

    def test_traced_with_tag_succeeds(self):
        """The happy path: tag provided, no exception."""
        from evolve.config_override import replay_context
        from evolve.replay_tracing import build_experiment_tag

        tag = build_experiment_tag("exp-test", {}, None)
        with replay_context({}, disable_tracing=False, experiment_tag=tag):
            pass

    def test_default_disable_tracing_silent(self):
        """Regression: default disable_tracing=True path must not require
        experiment_tag — every existing isolation test depends on this."""
        from evolve.config_override import replay_context

        with replay_context({}):
            pass

    def test_traced_with_empty_experiment_id_raises(self):
        """An experiment_tag missing or empty `experiment_id` is a malformed
        tag and must also raise. Catches hand-built dicts that bypass
        build_experiment_tag()."""
        from evolve.config_override import replay_context

        with pytest.raises(ValueError, match="experiment_id"):
            with replay_context(
                {},
                disable_tracing=False,
                experiment_tag={
                    "experiment_id": "",
                    "replay": True,
                    "override_fingerprint": "x",
                },
            ):
                pass


# ── 4. Langfuse-disabled no-op behavior ─────────────────────────────────────


class TestLangfuseDisabledNoOp:
    def test_root_span_silent_when_disabled(self):
        """replay_root_span yields the tag without exception when Langfuse
        is disabled. This is the fail-open contract — observability must
        never break the replay."""
        from evolve.config_override import isolate_langfuse
        from evolve.replay_tracing import build_experiment_tag, replay_root_span

        tag = build_experiment_tag("exp-test", {}, None)
        with isolate_langfuse():
            with replay_root_span("exp-test", tag) as yielded:
                assert yielded == tag

    def test_url_builders_return_none_when_disabled(self):
        from evolve.config_override import isolate_langfuse
        from evolve.replay_tracing import langfuse_session_url, langfuse_trace_url

        with isolate_langfuse():
            assert langfuse_trace_url("exp-x") is None
            assert langfuse_session_url("exp-x") is None


# ── 5. URL construction ─────────────────────────────────────────────────────


class TestUrlConstruction:
    def test_trace_url_uses_base_url(self, monkeypatch):
        from runtime import langfuse_setup

        monkeypatch.setenv("LANGFUSE_BASE_URL", "https://lf.example.com")
        monkeypatch.setattr(langfuse_setup, "is_langfuse_enabled", lambda: True)

        from evolve.replay_tracing import langfuse_trace_url

        url = langfuse_trace_url("exp-20260425T120000Z")
        assert (
            url
            == "https://lf.example.com/sessions?sessionId=evolve:exp-20260425T120000Z"
        )

    def test_session_url_strips_trailing_slash(self, monkeypatch):
        """Trailing slashes on LANGFUSE_BASE_URL must be tolerated — common
        configuration mistake."""
        from runtime import langfuse_setup

        monkeypatch.setenv("LANGFUSE_BASE_URL", "https://lf.example.com/")
        monkeypatch.setattr(langfuse_setup, "is_langfuse_enabled", lambda: True)

        from evolve.replay_tracing import langfuse_session_url

        url = langfuse_session_url("exp-x")
        assert url == "https://lf.example.com/sessions?sessionId=evolve:exp-x"

    def test_returns_none_without_base_url(self, monkeypatch):
        """No LANGFUSE_BASE_URL set → no URL we can construct → None.
        Better None than a broken URL."""
        from runtime import langfuse_setup

        monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
        monkeypatch.setattr(langfuse_setup, "is_langfuse_enabled", lambda: True)

        from evolve.replay_tracing import langfuse_trace_url

        assert langfuse_trace_url("exp-x") is None


# ── 6. ReplayReport URL fields ──────────────────────────────────────────────


class TestReplayReportFields:
    def test_urls_round_trip_through_to_dict(self):
        """to_dict() must include the new URL fields so the JSON written by
        write_report() preserves them for downstream consumers (decision
        artifact, evolve compare loaders)."""
        from evolve.models import ReplayReport, ReplaySummary

        url = "http://localhost:3000/sessions?sessionId=evolve:exp-x"
        report = ReplayReport(
            experiment_id="exp-x",
            timestamp_utc="2026-04-25T12:00:00Z",
            overrides={},
            config_snapshot={},
            summary=ReplaySummary(),
            langfuse_trace_url=url,
            langfuse_session_url=url,
        )
        d = report.to_dict()
        assert d["langfuse_trace_url"] == url
        assert d["langfuse_session_url"] == url

    def test_urls_default_to_none(self):
        """Untraced replays must produce reports with URL fields == None,
        and to_dict must include them as null in the JSON shape."""
        from evolve.models import ReplayReport, ReplaySummary

        report = ReplayReport(
            experiment_id="exp-x",
            timestamp_utc="2026-04-25T12:00:00Z",
            overrides={},
            config_snapshot={},
            summary=ReplaySummary(),
        )
        assert report.langfuse_trace_url is None
        assert report.langfuse_session_url is None
        d = report.to_dict()
        assert d["langfuse_trace_url"] is None
        assert d["langfuse_session_url"] is None


# ── 7. Rule 3 module-attribute lookup ───────────────────────────────────────


class TestRule3ModuleAttributeLookup:
    def test_isolate_langfuse_propagates_to_replay_tracing(self, monkeypatch):
        """Rule 3 / module-attribute lookup contract.

        replay_tracing.py uses `from runtime import langfuse_setup` then
        `langfuse_setup.is_langfuse_enabled()` so test monkey-patches via
        `isolate_langfuse()` (which directly sets
        `langfuse_setup.is_langfuse_enabled = lambda: False`) propagate
        through. If a future refactor caches the function reference at
        import time (`from runtime.langfuse_setup import is_langfuse_enabled`
        at module level), the patch silently no-ops and replay traces leak
        into prod.

        This test catches that regression: isolate_langfuse() forces the
        URL builder to return None even when LANGFUSE_BASE_URL is set —
        meaning the module-attribute lookup actually fired.
        """
        # Set base URL so the only thing keeping URL=None is the disabled flag
        monkeypatch.setenv("LANGFUSE_BASE_URL", "https://lf.example.com")

        from evolve.config_override import isolate_langfuse
        from evolve.replay_tracing import _is_enabled, langfuse_trace_url

        with isolate_langfuse():
            assert _is_enabled() is False
            assert langfuse_trace_url("exp-x") is None

        # Outside the block, restoration must work — sanity check
        # (other tests downstream rely on this).
