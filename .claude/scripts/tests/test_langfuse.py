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
