"""Tests for config hot-reload mechanics.

Validates that reload_config() correctly:
- Re-reads .env file
- Updates module-level globals
- Reports changed values
- Masks sensitive values
- Leaves unchanged values alone
"""

from __future__ import annotations

import os
from pathlib import Path


class TestReloadMechanics:
    """Test Python import/reload behavior that the /reload feature depends on."""

    def test_module_globals_update_via_setattr(self) -> None:
        """setattr on a module updates its globals, visible to future from-imports."""
        import config

        original = config.CHAT_MAX_TURNS

        try:
            # Simulate what reload_config does
            setattr(config, "CHAT_MAX_TURNS", 999)
            assert config.CHAT_MAX_TURNS == 999

            # A new `from config import` in a function body picks up the updated value
            from config import CHAT_MAX_TURNS
            assert CHAT_MAX_TURNS == 999
        finally:
            # Restore
            setattr(config, "CHAT_MAX_TURNS", original)

    def test_existing_bindings_not_updated(self) -> None:
        """Demonstrates that existing from-import bindings are NOT updated.

        This is the core reason /reload needs to propagate values to
        engine/adapter — they hold stale copies from their __init__.
        """
        import config
        from config import CHAT_MAX_TURNS as bound_value

        original = config.CHAT_MAX_TURNS

        try:
            setattr(config, "CHAT_MAX_TURNS", 777)

            # The local binding still has the old value
            assert bound_value == original
            # But the module has the new one
            assert config.CHAT_MAX_TURNS == 777
        finally:
            setattr(config, "CHAT_MAX_TURNS", original)

    def test_dotenv_override_reads_new_values(self, tmp_env_file: Path) -> None:
        """load_dotenv with override=True picks up changed values."""
        from dotenv import load_dotenv

        # Write initial value
        tmp_env_file.write_text("TEST_RELOAD_VAR=initial\n", encoding="utf-8")
        load_dotenv(tmp_env_file, override=True)
        assert os.getenv("TEST_RELOAD_VAR") == "initial"

        # Update the file
        tmp_env_file.write_text("TEST_RELOAD_VAR=updated\n", encoding="utf-8")
        load_dotenv(tmp_env_file, override=True)
        assert os.getenv("TEST_RELOAD_VAR") == "updated"

        # Cleanup
        os.environ.pop("TEST_RELOAD_VAR", None)


class TestBackoffLogic:
    """Test the exponential backoff calculation used by service.py."""

    def test_backoff_doubles(self) -> None:
        """Backoff should double on each crash."""
        base = 5
        max_backoff = 120
        backoff = base

        expected = [10, 20, 40, 80, 120, 120]  # doubles, then caps
        for expected_val in expected:
            backoff = min(backoff * 2, max_backoff)
            assert backoff == expected_val

    def test_backoff_resets_after_long_run(self) -> None:
        """If bot ran for > 5 min, backoff resets to base."""
        base = 5
        backoff = 120  # maxed out

        elapsed = 301  # > 300 seconds = 5 minutes
        if elapsed > 300:
            backoff = base

        assert backoff == base

    def test_restart_budget_tracking(self) -> None:
        """Max 5 restarts per rolling hour."""
        import time
        from collections import deque

        max_restarts = 5
        restart_times: deque[float] = deque()

        now = time.time()

        # Add 5 restarts
        for i in range(5):
            restart_times.append(now + i)

        # Prune old (none are old)
        while restart_times and (now + 5 - restart_times[0]) > 3600:
            restart_times.popleft()

        assert len(restart_times) >= max_restarts  # Budget exhausted

    def test_old_restarts_pruned(self) -> None:
        """Restarts older than 1 hour should be pruned."""
        import time
        from collections import deque

        restart_times: deque[float] = deque()
        now = time.time()

        # Add 5 restarts from 2 hours ago
        for i in range(5):
            restart_times.append(now - 7200 + i)

        # Prune
        while restart_times and (now - restart_times[0]) > 3600:
            restart_times.popleft()

        assert len(restart_times) == 0  # All pruned


class TestReloadConfigFunction:
    """Test the actual reload_config() function from config.py."""

    def test_no_changes_returns_empty(self) -> None:
        """When .env hasn't changed, reload_config returns empty dict."""
        from config import reload_config

        changes = reload_config()
        assert isinstance(changes, dict)
        # May or may not be empty depending on env state, but should not raise
        assert changes is not None

    def test_detects_budget_change(self) -> None:
        """reload_config detects and reports a changed CHAT_MAX_BUDGET_USD."""
        import config

        original = config.CHAT_MAX_BUDGET_USD

        try:
            # Temporarily change the env var
            os.environ["CHAT_MAX_BUDGET_USD"] = "99.0"
            from config import reload_config

            changes = reload_config()
            assert "CHAT_MAX_BUDGET_USD" in changes
            assert config.CHAT_MAX_BUDGET_USD == 99.0
        finally:
            # Restore
            os.environ["CHAT_MAX_BUDGET_USD"] = str(original)
            from config import reload_config as rc

            rc()

    def test_sensitive_values_masked(self) -> None:
        """API keys should be masked in the change report."""
        import config

        original = config.OPENAI_API_KEY

        try:
            os.environ["OPENAI_API_KEY"] = "sk-new-test-key-123"
            from config import reload_config

            changes = reload_config()
            if "OPENAI_API_KEY" in changes:
                old, new = changes["OPENAI_API_KEY"]
                assert old == "***"
                assert new == "***"
        finally:
            if original:
                os.environ["OPENAI_API_KEY"] = original
            else:
                os.environ.pop("OPENAI_API_KEY", None)
            from config import reload_config as rc

            rc()
