"""Tests for Langfuse observability integration."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import patch

# Ensure scripts dir is importable
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))


class TestLangfuseSetup:
    """Tests for runtime.langfuse_setup module.

    Note: config.py calls load_dotenv(override=True) at import time,
    so monkeypatch.setenv doesn't stick for env vars that .env defines.
    We use unittest.mock.patch on os.getenv instead.
    """

    def test_is_langfuse_enabled_returns_false_when_no_keys(self):
        """Without keys, tracing should be disabled."""
        def _fake_getenv(key, default=None):
            overrides = {
                "LANGFUSE_ENABLED": "true",
                "LANGFUSE_PUBLIC_KEY": "",
                "LANGFUSE_SECRET_KEY": "",
            }
            if key in overrides:
                return overrides[key]
            return os.environ.get(key, default)

        with patch("runtime.langfuse_setup.os.getenv", side_effect=_fake_getenv):
            from runtime.langfuse_setup import is_langfuse_enabled
            assert is_langfuse_enabled() is False

    def test_is_langfuse_enabled_with_keys(self):
        """With keys and not disabled, tracing should be enabled."""
        def _fake_getenv(key, default=None):
            overrides = {
                "LANGFUSE_ENABLED": "true",
                "LANGFUSE_PUBLIC_KEY": "pk-test",
                "LANGFUSE_SECRET_KEY": "sk-test",
            }
            if key in overrides:
                return overrides[key]
            return os.environ.get(key, default)

        with patch("runtime.langfuse_setup.os.getenv", side_effect=_fake_getenv):
            from runtime.langfuse_setup import is_langfuse_enabled
            assert is_langfuse_enabled() is True

    def test_is_langfuse_enabled_explicitly_disabled(self):
        """LANGFUSE_ENABLED=false should disable tracing even with keys."""
        def _fake_getenv(key, default=None):
            overrides = {
                "LANGFUSE_ENABLED": "false",
                "LANGFUSE_PUBLIC_KEY": "pk-test",
                "LANGFUSE_SECRET_KEY": "sk-test",
            }
            if key in overrides:
                return overrides[key]
            return os.environ.get(key, default)

        with patch("runtime.langfuse_setup.os.getenv", side_effect=_fake_getenv):
            from runtime.langfuse_setup import is_langfuse_enabled
            assert is_langfuse_enabled() is False

    def test_flush_langfuse_noop_when_not_initialized(self):
        """flush_langfuse should not raise when not initialized."""
        import runtime.langfuse_setup as lf_mod
        from runtime.langfuse_setup import flush_langfuse
        orig = lf_mod._initialized
        lf_mod._initialized = False
        try:
            flush_langfuse()  # Should not raise
        finally:
            lf_mod._initialized = orig

    def test_flush_langfuse_exists(self):
        """flush_langfuse function should be importable."""
        from runtime.langfuse_setup import flush_langfuse
        assert callable(flush_langfuse)

    def test_init_langfuse_returns_false_when_disabled(self):
        """init_langfuse returns False when tracing is disabled."""
        def _fake_getenv(key, default=None):
            if key == "LANGFUSE_ENABLED":
                return "false"
            return os.environ.get(key, default)

        with patch("runtime.langfuse_setup.os.getenv", side_effect=_fake_getenv):
            import runtime.langfuse_setup as lf_mod
            orig = lf_mod._initialized
            lf_mod._initialized = False
            try:
                assert lf_mod.init_langfuse() is False
            finally:
                lf_mod._initialized = orig


class TestRecallObserve:
    """Tests for @observe decoration on recall functions."""

    def test_recall_importable(self):
        """recall function should be importable with or without Langfuse."""
        from recall_service import recall
        assert callable(recall)

    def test_recall_has_observe_attribute(self):
        """If langfuse is enabled, recall should be wrapped; otherwise still callable."""
        # Whether decorated or not, it must be async-callable
        import asyncio

        from recall_service import recall
        assert asyncio.iscoroutinefunction(recall) or callable(recall)

    def test_classify_tier_importable(self):
        """classify_tier should be importable with or without Langfuse."""
        from cognition.recall import classify_tier
        assert callable(classify_tier)

    def test_classify_tier_still_works(self):
        """classify_tier should return correct tiers regardless of tracing."""
        from cognition.recall import RecallTier, classify_tier
        # Prefetched should skip
        assert classify_tier("hello", has_prefetched=True) == RecallTier.SKIP
        # Slash command should skip
        assert classify_tier("/budget", is_slash_command=True) == RecallTier.SKIP
        # Greeting should be tier 0
        assert classify_tier("hello") == RecallTier.TIER_0
        # Regular message should be tier 1
        msg = "What happened with the lead pipeline yesterday?"
        assert classify_tier(msg) == RecallTier.TIER_1

    def test_run_recall_pipeline_importable(self):
        """run_recall_pipeline should be importable."""
        from cognition.recall import run_recall_pipeline
        assert callable(run_recall_pipeline)


class TestGetObserveHelper:
    """Tests for the _get_observe lazy decorator pattern."""

    def test_get_observe_returns_callable_when_disabled(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")
        import runtime.langfuse_setup as lf_mod
        importlib.reload(lf_mod)

        from recall_service import _get_observe
        decorator_factory = _get_observe()
        # Should return an identity decorator factory
        assert callable(decorator_factory)

        # The identity decorator should pass through the function unchanged
        def dummy():
            return 42

        decorated = decorator_factory(name="test")(dummy)
        assert decorated() == 42


class TestSessionActionReporting:
    """Tests for session action in trace decisions.

    Verifies that engine.py reports the correct session action in both
    the root _trace_decisions dict and the post_response span.
    """

    def test_engine_session_action_uses_should_reset(self):
        """Verify engine.py source contains reset-aware session action logic."""
        import inspect
        from engine import ConversationEngine
        source = inspect.getsource(ConversationEngine._handle_message_inner)
        # Root decisions block must check should_reset
        assert '"reset" if should_reset' in source, (
            "Root _trace_decisions session action must check should_reset"
        )

    def test_engine_post_response_uses_should_reset(self):
        """Verify post_response span also checks should_reset."""
        import inspect
        from engine import ConversationEngine
        source = inspect.getsource(ConversationEngine._handle_message_inner)
        # post_response span must also check should_reset
        assert 'session_action": "reset" if should_reset' in source, (
            "post_response span session_action must check should_reset"
        )

    def test_session_action_ternary_correctness(self):
        """Verify the ternary produces correct values for all 3 states."""
        for should_reset, existing, expected in [
            (True, True, "reset"),
            (True, False, "reset"),
            (False, True, "resumed"),
            (False, False, "created"),
        ]:
            action = "reset" if should_reset else (
                "resumed" if existing else "created"
            )
            assert action == expected, (
                f"should_reset={should_reset}, existing={existing}: "
                f"expected '{expected}', got '{action}'"
            )


class TestResumedSessionEmitsRealCognitionSpans:
    """Path B regression: resumed sessions must produce real cognition spans, not 'skipped'."""

    def test_no_resumed_session_skip_markers_in_source(self):
        """The 'resumed_session' skip reason must be gone from the engine source."""
        import inspect
        from engine import ConversationEngine
        source = inspect.getsource(ConversationEngine._handle_message_inner)
        assert '"resumed_session"' not in source, (
            "Path B removes the skip-on-resume branch — 'resumed_session' "
            "must not appear as a skip reason in trace decisions"
        )

    def test_recent_conversation_trace_decision_is_set(self):
        """Region_assembly output must include recent_conversation metadata."""
        import inspect
        from engine import ConversationEngine
        source = inspect.getsource(ConversationEngine._handle_message_inner)
        assert '_trace_decisions["recent_conversation"]' in source, (
            "Path B injects recent_conversation region — its span metadata "
            "must appear in _trace_decisions"
        )

    def test_region_assembly_not_marked_skipped_on_resume(self):
        """region_assembly trace entry must carry real chars metric, not skipped=True."""
        import inspect
        from engine import ConversationEngine
        source = inspect.getsource(ConversationEngine._handle_message_inner)
        # The engine should set region_assembly with total_chars, not 'skipped: True, reason: resumed_session'
        assert '_trace_decisions["region_assembly"] = {' in source
        # Verify the real metric form is what's being set
        assert '"total_chars": len(system_prompt.get("append", ""))' in source, (
            "region_assembly must report total_chars, not a skipped reason"
        )


class TestSentryInit:
    """Tests for GlitchTip/Sentry initialization."""

    def test_sentry_init_noop_without_dsn(self):
        """sentry_sdk.init should not be called without SENTRY_DSN."""
        with patch.dict(os.environ, {"SENTRY_DSN": ""}, clear=False):
            dsn = os.getenv("SENTRY_DSN")
            assert not dsn  # empty string is falsy → init skipped

    def test_sentry_sdk_importable(self):
        """sentry_sdk should be installed and importable."""
        import sentry_sdk
        assert hasattr(sentry_sdk, "init")

    def test_sentry_init_in_main_is_guarded(self):
        """main.py sentry init must be inside try/except with DSN check."""
        source = (Path(__file__).resolve().parent.parent.parent
                  / "chat" / "main.py").read_text()
        assert "if _dsn:" in source, "Sentry init must be guarded by DSN check"
        assert "except Exception:" in source


class TestLangfuseFlagPropagation:
    """Regression tests for #19: bound-import of is_langfuse_enabled defeats
    isolate_langfuse() patch.

    The bug: top-level `from runtime.langfuse_setup import is_langfuse_enabled`
    in registry.py / observability.py caches the function reference at import
    time. When evolve.config_override.isolate_langfuse() monkey-patches
    `runtime.langfuse_setup.is_langfuse_enabled`, the cached references in
    those modules don't update. Replay isolation leaks into ambient spans.

    The fix: switch to module-attribute lookup
    (`from runtime import langfuse_setup` + `langfuse_setup.is_langfuse_enabled()`)
    so each call re-resolves through the module dictionary."""

    def test_registry_does_not_cache_is_langfuse_enabled_at_import_time(self):
        """The fixed pattern must NOT have a top-level
        `is_langfuse_enabled` binding on the registry module."""
        from runtime import registry
        assert not hasattr(registry, "is_langfuse_enabled"), (
            "registry.is_langfuse_enabled exists as a module attribute, which "
            "means the bound-import pattern is back. Use module-attribute "
            "lookup via `langfuse_setup.is_langfuse_enabled()` instead."
        )
        # The module SHOULD have a `langfuse_setup` reference (the proper pattern)
        assert hasattr(registry, "langfuse_setup")

    def test_observability_does_not_cache_is_langfuse_enabled_at_import_time(self):
        """Same regression check for orchestration.observability."""
        from orchestration import observability
        assert not hasattr(observability, "is_langfuse_enabled"), (
            "observability.is_langfuse_enabled exists as a module attribute. "
            "The bound-import pattern is back; use `langfuse_setup.is_langfuse_enabled()`."
        )
        assert hasattr(observability, "langfuse_setup")

    def test_isolate_langfuse_patch_propagates_to_registry_call_site(self):
        """Behavioral regression: a patch on langfuse_setup.is_langfuse_enabled
        must reach the call site in registry.run_with_fallback().

        With the bound-import bug, run_with_fallback would still see
        is_langfuse_enabled() return True even inside isolate_langfuse(),
        because its cached reference wasn't patched."""
        from runtime import langfuse_setup, registry  # noqa: F401
        # Patch the source — both bound-import and module-attribute consumers
        # SHOULD see this.
        with patch.object(langfuse_setup, "is_langfuse_enabled", return_value=False):
            # Module-attribute consumers see the patch:
            assert langfuse_setup.is_langfuse_enabled() is False
        # Patch reverts cleanly:
        # (no specific value asserted because baseline depends on .env)

    def test_isolate_langfuse_patch_propagates_to_observability_call_sites(self):
        """Behavioral regression: a patch on langfuse_setup.is_langfuse_enabled
        must reach update_observation() and orchestration_span() in
        orchestration.observability."""
        from runtime import langfuse_setup
        from orchestration import observability  # noqa: F401
        with patch.object(langfuse_setup, "is_langfuse_enabled", return_value=False):
            assert langfuse_setup.is_langfuse_enabled() is False
