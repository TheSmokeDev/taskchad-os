from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS.parent / "chat"))

import commands  # type: ignore[import-not-found]  # noqa: E402
import core_handlers  # type: ignore[import-not-found]  # noqa: E402
from extension_manager import ExtensionManager, set_manager  # type: ignore[import-not-found]  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_extension_manager() -> None:
    set_manager(ExtensionManager())
    yield
    set_manager(ExtensionManager())


def _register_manager() -> ExtensionManager:
    manager = ExtensionManager()
    manager.register_core_commands(
        commands.COMMANDS,
        commands.CATEGORIES,
        core_handlers.CORE_HANDLERS,
    )
    set_manager(manager)
    return manager


def test_telegram_native_menu_is_curated_static_registry() -> None:
    menu = commands.get_telegram_bot_commands()
    names = [name for name, _desc in menu]

    assert names == list(commands.TELEGRAM_NATIVE_COMMANDS)
    assert len(names) == 31
    assert "design" in names
    assert "linkedin" in names
    assert "commands" in names
    assert "publish" not in names
    assert "blogstatus" not in names
    assert "prime" not in names


def test_telegram_native_menu_is_curated_with_manager() -> None:
    _register_manager()

    menu, hidden_count = commands.get_telegram_command_menu()
    names = [name for name, _desc in menu]

    assert names == list(commands.TELEGRAM_NATIVE_COMMANDS)
    assert hidden_count == len(commands.COMMANDS) - len(menu)
    assert dict(menu)["linkedin"].startswith("LinkedIn/Social Homie")


def test_telegram_command_constraints_hold() -> None:
    for name, desc in commands.get_telegram_bot_commands():
        assert 1 <= len(name) <= 32
        assert name == name.lower()
        assert name.replace("_", "").isalnum()
        assert 1 <= len(desc) <= 256


def test_diagnostics_and_commands_are_categorized() -> None:
    categorized = {name for _category, names in commands.CATEGORIES for name in names}

    assert "commands" in categorized
    assert "diagnostics" in categorized


@pytest.mark.asyncio
async def test_commands_native_handler_shows_menu_and_hidden_count() -> None:
    _register_manager()

    reply = await core_handlers.handle_commands(
        None,
        SimpleNamespace(user_role="admin"),
        "native",
    )

    assert "*Native Telegram Commands*" in reply
    assert "/linkedin - LinkedIn/Social Homie" in reply
    assert "Hidden registered commands:" in reply
    assert "/prime" not in reply


@pytest.mark.asyncio
async def test_commands_all_handler_shows_full_registry() -> None:
    _register_manager()

    reply = await core_handlers.handle_commands(
        None,
        SimpleNamespace(user_role="admin"),
        "all",
    )

    assert "*PIV Workflow*" in reply
    assert "/prime" in reply
    assert "/linkedin" in reply
