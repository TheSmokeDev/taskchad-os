"""Tests for the extension manager — registry, discovery, dispatch, diagnostics."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import sys

_CHAT_DIR = Path(__file__).resolve().parent.parent.parent / "chat"
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_CHAT_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR))

from extension_manager import CommandSpec, ExtensionManager, IntentSpec


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def manager():
    """Fresh ExtensionManager with no commands."""
    return ExtensionManager()


@pytest.fixture
def populated_manager():
    """Manager with core commands registered."""
    from commands import CATEGORIES, COMMANDS, CORE_INTENTS
    from core_handlers import CORE_HANDLERS

    m = ExtensionManager()
    m.register_core_commands(COMMANDS, CATEGORIES, CORE_HANDLERS)
    m.register_core_intents(CORE_INTENTS)
    return m


@pytest.fixture
def tmp_ext_dir():
    """Temporary directory for test extensions."""
    d = Path(tempfile.mkdtemp())
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _make_extension(base: Path, ext_id: str, **overrides) -> Path:
    """Create a minimal valid extension directory."""
    ext_dir = base / ext_id
    ext_dir.mkdir(exist_ok=True)

    manifest = {
        "id": ext_id,
        "name": f"Test {ext_id}",
        "version": "1.0.0",
        "enabledByDefault": True,
        "commands": [
            {
                "name": f"{ext_id}-cmd",
                "description": f"Test command for {ext_id}",
                "type": "router",
                "minRole": "viewer",
                "handler": "handlers:handle_test",
            }
        ],
        "dataIntents": [],
        "envRequirements": [],
    }
    manifest.update(overrides)

    (ext_dir / "extension.json").write_text(json.dumps(manifest), encoding="utf-8")
    (ext_dir / "handlers.py").write_text(
        "async def handle_test(adapter, incoming, args, *, collect_only=False):\n"
        f"    return 'Response from {ext_id}'\n",
        encoding="utf-8",
    )
    return ext_dir


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_register_command(self, manager: ExtensionManager):
        spec = CommandSpec(name="test", description="Test", type="router", min_role="viewer")
        manager.register_command(spec)
        assert "test" in manager._commands

    def test_duplicate_command_raises(self, manager: ExtensionManager):
        spec = CommandSpec(name="test", description="Test", type="router", min_role="viewer")
        manager.register_command(spec)
        with pytest.raises(ValueError, match="Duplicate command"):
            manager.register_command(spec)

    def test_register_intent(self, manager: ExtensionManager):
        intent = IntentSpec(command="test", keywords=["hello"])
        manager.register_intent(intent)
        assert len(manager._intents) == 1

    def test_command_regex_recompiles(self, manager: ExtensionManager):
        spec1 = CommandSpec(name="alpha", description="A", type="router", min_role="viewer")
        manager.register_command(spec1)
        regex1 = manager.command_regex

        spec2 = CommandSpec(name="beta", description="B", type="router", min_role="viewer")
        manager.register_command(spec2)
        regex2 = manager.command_regex

        assert regex1 is not regex2  # Recompiled
        assert regex2.match("/beta args")

    def test_register_core_commands(self, populated_manager: ExtensionManager):
        assert len(populated_manager._commands) > 0
        assert "help" in populated_manager._commands
        assert "status" in populated_manager._commands
        assert "extensions" in populated_manager._commands


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

class TestQueryHelpers:
    def test_get_all_command_names(self, populated_manager: ExtensionManager):
        names = populated_manager.get_all_command_names()
        assert "help" in names
        assert "email" in names

    def test_get_router_commands(self, populated_manager: ExtensionManager):
        router = populated_manager.get_router_commands()
        assert "help" in router
        assert "email" in router
        # Engine commands should NOT be in router set
        assert "calendar" not in router

    def test_get_engine_command_description(self, populated_manager: ExtensionManager):
        desc = populated_manager.get_engine_command_description("calendar")
        assert desc is not None
        assert "Calendar" in desc

        assert populated_manager.get_engine_command_description("help") is None  # router cmd

    def test_get_command_min_role(self, populated_manager: ExtensionManager):
        assert populated_manager.get_command_min_role("help") == "viewer"
        assert populated_manager.get_command_min_role("email") == "admin"
        assert populated_manager.get_command_min_role("nonexistent") == "viewer"

    def test_get_help_text(self, populated_manager: ExtensionManager):
        text = populated_manager.get_help_text()
        assert "/help" in text
        assert "/email" in text
        assert "Session & Mode" in text

    def test_get_help_text_role_filter(self, populated_manager: ExtensionManager):
        text = populated_manager.get_help_text(user_role="viewer")
        assert "/help" in text
        assert "/email" not in text  # admin only


# ---------------------------------------------------------------------------
# Intent detection
# ---------------------------------------------------------------------------

class TestIntentDetection:
    def test_detect_intents_keyword(self, populated_manager: ExtensionManager):
        detected = populated_manager.detect_intents("check my email")
        assert "email" in detected

    def test_detect_intents_multiple(self, populated_manager: ExtensionManager):
        detected = populated_manager.detect_intents("check email and budget")
        assert "email" in detected
        assert "budget" in detected

    def test_detect_intents_broad_query(self, populated_manager: ExtensionManager):
        detected = populated_manager.detect_intents("how are we looking across all boards")
        assert len(detected) >= 2  # brief intents

    def test_wants_analysis_true(self, populated_manager: ExtensionManager):
        assert populated_manager.wants_analysis("good morning, how are we looking?")
        assert populated_manager.wants_analysis("summarize everything")

    def test_wants_analysis_false(self, populated_manager: ExtensionManager):
        assert not populated_manager.wants_analysis("check my email")

    def test_get_brief_intents(self, populated_manager: ExtensionManager):
        brief = populated_manager.get_brief_intents()
        assert "email" in brief
        assert "budget" in brief


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

class TestDispatch:
    @pytest.mark.asyncio
    async def test_dispatch_core_handler(self, populated_manager: ExtensionManager):
        incoming = MagicMock()
        incoming.user_role = "admin"
        result = await populated_manager.dispatch("help", None, incoming, "")
        assert result is not None
        assert "Available Commands" in result

    @pytest.mark.asyncio
    async def test_dispatch_role_denied(self, populated_manager: ExtensionManager):
        incoming = MagicMock()
        incoming.user_role = "viewer"
        result = await populated_manager.dispatch("email", None, incoming, "")
        assert "Permission denied" in result

    @pytest.mark.asyncio
    async def test_dispatch_nonexistent_returns_none(self, populated_manager: ExtensionManager):
        incoming = MagicMock()
        incoming.user_role = "admin"
        result = await populated_manager.dispatch("nonexistent", None, incoming, "")
        assert result is None

    @pytest.mark.asyncio
    async def test_dispatch_engine_command_returns_none(self, populated_manager: ExtensionManager):
        # Engine commands should not dispatch via router
        incoming = MagicMock()
        incoming.user_role = "admin"
        result = await populated_manager.dispatch("calendar", None, incoming, "")
        assert result is None  # type != "router"


# ---------------------------------------------------------------------------
# Extension discovery
# ---------------------------------------------------------------------------

class TestDiscovery:
    def test_discover_valid_extension(self, populated_manager: ExtensionManager, tmp_ext_dir: Path):
        _make_extension(tmp_ext_dir, "test-ext")
        discovered = populated_manager.discover([tmp_ext_dir])
        assert len(discovered) == 1
        assert discovered[0].id == "test-ext"
        assert discovered[0].status == "loaded"
        assert "test-ext-cmd" in populated_manager._commands

    def test_discover_bad_json(self, populated_manager: ExtensionManager, tmp_ext_dir: Path):
        ext_dir = tmp_ext_dir / "bad-json"
        ext_dir.mkdir()
        (ext_dir / "extension.json").write_text("{ not json", encoding="utf-8")
        discovered = populated_manager.discover([tmp_ext_dir])
        assert discovered[0].status == "error"

    def test_discover_missing_fields(self, populated_manager: ExtensionManager, tmp_ext_dir: Path):
        ext_dir = tmp_ext_dir / "missing"
        ext_dir.mkdir()
        (ext_dir / "extension.json").write_text('{"id": "missing"}', encoding="utf-8")
        discovered = populated_manager.discover([tmp_ext_dir])
        assert discovered[0].status == "error"

    def test_discover_missing_env(self, populated_manager: ExtensionManager, tmp_ext_dir: Path):
        _make_extension(
            tmp_ext_dir, "env-test",
            envRequirements=[{"name": "NONEXISTENT_VAR_99", "required": True}],
        )
        discovered = populated_manager.discover([tmp_ext_dir])
        assert discovered[0].status == "missing_env"
        assert "NONEXISTENT_VAR_99" in discovered[0].missing_env
        # Command should NOT be registered
        assert "env-test-cmd" not in populated_manager._commands

    def test_discover_deny_list(self, populated_manager: ExtensionManager, tmp_ext_dir: Path):
        _make_extension(tmp_ext_dir, "denied-ext")
        populated_manager.configure_allow_deny(deny=["denied-ext"])
        discovered = populated_manager.discover([tmp_ext_dir])
        assert discovered[0].status == "disabled"
        assert "denied-ext-cmd" not in populated_manager._commands

    def test_discover_allow_list(self, populated_manager: ExtensionManager, tmp_ext_dir: Path):
        _make_extension(tmp_ext_dir, "allowed")
        _make_extension(tmp_ext_dir, "not-allowed")
        populated_manager.configure_allow_deny(allow=["allowed"])
        discovered = populated_manager.discover([tmp_ext_dir])
        statuses = {e.id: e.status for e in discovered}
        assert statuses["allowed"] == "loaded"
        assert statuses["not-allowed"] == "disabled"

    def test_discover_disabled_by_default(self, populated_manager: ExtensionManager, tmp_ext_dir: Path):
        _make_extension(tmp_ext_dir, "disabled-default", enabledByDefault=False)
        discovered = populated_manager.discover([tmp_ext_dir])
        assert discovered[0].status == "disabled"
        assert "disabled-default-cmd" not in populated_manager._commands

    def test_discover_possible_duplicate_collision(
        self, populated_manager: ExtensionManager, tmp_ext_dir: Path,
    ):
        _make_extension(
            tmp_ext_dir,
            "dup-help",
            commands=[{
                "name": "help",
                "description": "Show all available commands",
                "type": "router",
                "minRole": "viewer",
                "handler": "handlers:handle_test",
            }],
        )

        discovered = populated_manager.discover([tmp_ext_dir])
        ext = discovered[0]

        assert ext.status == "partial"
        assert ext.command_collisions
        assert ext.command_collisions[0].kind == "possible_duplicate"
        assert "already have this behavior" in ext.command_collisions[0].guidance.lower()

        report = populated_manager.doctor()
        assert "keep the existing command" in report.lower()

    def test_discover_name_conflict_suggests_rename(
        self, populated_manager: ExtensionManager, tmp_ext_dir: Path,
    ):
        _make_extension(
            tmp_ext_dir,
            "collision-test",
            commands=[{
                "name": "help",
                "description": "Show weather onboarding steps",
                "type": "router",
                "minRole": "viewer",
                "handler": "handlers:handle_test",
            }],
            dataIntents=[{
                "command": "help",
                "keywords": ["weather onboarding"],
                "includedInBrief": False,
            }],
        )

        discovered = populated_manager.discover([tmp_ext_dir])
        ext = discovered[0]

        assert ext.status == "partial"
        assert ext.command_collisions[0].kind == "name_conflict"
        assert ext.command_collisions[0].suggested_name == "collision_test_help"
        assert not any(
            intent.extension_id == "collision-test" and intent.command == "help"
            for intent in populated_manager._intents
        )

        report = populated_manager.doctor()
        assert "rename the incoming command" in report.lower()
        assert "/collision_test_help" in report


# ---------------------------------------------------------------------------
# Lazy handler loading
# ---------------------------------------------------------------------------

class TestLazyLoading:
    @pytest.mark.asyncio
    async def test_handler_loaded_on_first_dispatch(
        self, populated_manager: ExtensionManager, tmp_ext_dir: Path,
    ):
        _make_extension(tmp_ext_dir, "lazy-test")
        populated_manager.discover([tmp_ext_dir])

        spec = populated_manager._commands.get("lazy-test-cmd")
        assert spec is not None
        assert spec.handler is None  # Not loaded yet

        incoming = MagicMock()
        incoming.user_role = "admin"
        result = await populated_manager.dispatch("lazy-test-cmd", None, incoming, "")
        assert result == "Response from lazy-test"
        assert spec.handler is not None  # Now loaded

    @pytest.mark.asyncio
    async def test_broken_handler_returns_error(
        self, populated_manager: ExtensionManager, tmp_ext_dir: Path,
    ):
        ext_dir = _make_extension(tmp_ext_dir, "broken-handler")
        (ext_dir / "handlers.py").write_text("# No handle_test function\n", encoding="utf-8")
        populated_manager.discover([tmp_ext_dir])

        incoming = MagicMock()
        incoming.user_role = "admin"
        result = await populated_manager.dispatch("broken-handler-cmd", None, incoming, "")
        assert "Extension error" in result or "error" in result.lower()


# ---------------------------------------------------------------------------
# Enable / Disable
# ---------------------------------------------------------------------------

class TestEnableDisable:
    def test_enable_disabled_extension(
        self, populated_manager: ExtensionManager, tmp_ext_dir: Path,
    ):
        _make_extension(tmp_ext_dir, "toggle-ext", enabledByDefault=False)
        populated_manager.discover([tmp_ext_dir])
        assert "toggle-ext-cmd" not in populated_manager._commands

        result = populated_manager.enable_extension("toggle-ext")
        assert "enabled" in result.lower()
        assert "toggle-ext-cmd" in populated_manager._commands

    def test_disable_enabled_extension(
        self, populated_manager: ExtensionManager, tmp_ext_dir: Path,
    ):
        _make_extension(tmp_ext_dir, "toggle2")
        populated_manager.discover([tmp_ext_dir])
        assert "toggle2-cmd" in populated_manager._commands

        result = populated_manager.disable_extension("toggle2")
        assert "disabled" in result.lower()
        assert "toggle2-cmd" not in populated_manager._commands

    def test_enable_nonexistent(self, populated_manager: ExtensionManager):
        result = populated_manager.enable_extension("nonexistent")
        assert "not found" in result.lower()

    def test_disable_partial_extension_keeps_core_command(
        self, populated_manager: ExtensionManager, tmp_ext_dir: Path,
    ):
        _make_extension(
            tmp_ext_dir,
            "collision-disable",
            commands=[{
                "name": "help",
                "description": "Show weather onboarding steps",
                "type": "router",
                "minRole": "viewer",
                "handler": "handlers:handle_test",
            }],
        )

        populated_manager.discover([tmp_ext_dir])
        assert populated_manager._commands["help"].extension_id is None

        result = populated_manager.disable_extension("collision-disable")
        assert "disabled" in result.lower()
        assert "help" in populated_manager._commands
        assert populated_manager._commands["help"].extension_id is None


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

class TestDiagnostics:
    def test_get_diagnostics_no_extensions(self, populated_manager: ExtensionManager):
        diag = populated_manager.get_diagnostics()
        assert "No extensions discovered" in diag

    def test_get_diagnostics_with_extension(
        self, populated_manager: ExtensionManager, tmp_ext_dir: Path,
    ):
        _make_extension(tmp_ext_dir, "diag-ext")
        populated_manager.discover([tmp_ext_dir])
        diag = populated_manager.get_diagnostics()
        assert "diag-ext" in diag.lower() or "Diag" in diag

    def test_doctor_healthy(self, populated_manager: ExtensionManager, tmp_ext_dir: Path):
        _make_extension(tmp_ext_dir, "healthy-ext")
        populated_manager.discover([tmp_ext_dir])
        report = populated_manager.doctor()
        assert "healthy" in report.lower()

    def test_doctor_missing_handler_file(
        self, populated_manager: ExtensionManager, tmp_ext_dir: Path,
    ):
        ext_dir = _make_extension(tmp_ext_dir, "no-handler")
        (ext_dir / "handlers.py").unlink()  # Delete handler file
        populated_manager.discover([tmp_ext_dir])
        report = populated_manager.doctor()
        assert "missing" in report.lower()
