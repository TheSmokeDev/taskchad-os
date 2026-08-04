from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS.parent / "chat"))

import commands  # type: ignore[import-not-found]  # noqa: E402
import core_handlers  # type: ignore[import-not-found]  # noqa: E402
from adapters.discord import get_discord_native_command_menu  # type: ignore[import-not-found]  # noqa: E402
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
    assert "design" in names
    assert "linkedin" in names
    assert "video" in names
    assert "vault" in names
    assert "skills" in names
    assert "commands" in names
    # Operator Automation UX (Phase 2)
    assert "recap" in names
    assert "blueprints" in names
    assert "suggestions" in names
    # Menu currency (+8, 2026-07-11): sibling commands that dispatched but were
    # missing from the native menu.
    for added in ("pemail", "cleanup", "accounts", "gsc", "analytics", "team", "teamtick", "quote"):
        assert added in names, added
    assert "publish" not in names
    assert "blogstatus" not in names
    assert "prime" not in names


def test_every_command_makes_an_explicit_menu_decision() -> None:
    """Every COMMANDS row is either in the native menu or explicitly excluded —
    never silently omitted. Closes the `/video` drift class: a command that
    forgets the menu decision fails here instead of never autocompleting.

    Also proves neither set has zombie entries (names that no longer exist in
    COMMANDS)."""
    registry = {name for name, *_rest in commands.COMMANDS}
    menu = set(commands.TELEGRAM_NATIVE_COMMANDS)
    excluded = set(commands.NATIVE_MENU_EXCLUDED)

    # A command can't be both shown and hidden.
    assert menu & excluded == set(), sorted(menu & excluded)
    # The two buckets exactly cover the registry — nothing unclassified, no zombies.
    assert menu | excluded == registry, {
        "unclassified": sorted(registry - menu - excluded),
        "menu_zombies": sorted(menu - registry),
        "excluded_zombies": sorted(excluded - registry),
    }
    # Every excluded name actually exists in COMMANDS (no dead entries).
    assert excluded <= registry, sorted(excluded - registry)
    # Every menu name actually exists in COMMANDS (a menu name with no COMMANDS
    # row is silently dropped by get_telegram_command_menu — catch it here).
    assert menu <= registry, sorted(menu - registry)


def test_telegram_native_menu_is_curated_with_manager() -> None:
    _register_manager()

    menu, hidden_count = commands.get_telegram_command_menu()
    names = [name for name, _desc in menu]

    assert names == list(commands.TELEGRAM_NATIVE_COMMANDS)
    assert hidden_count == len(commands.COMMANDS) - len(menu)
    assert dict(menu)["linkedin"].startswith("LinkedIn workshop")


def test_telegram_command_constraints_hold() -> None:
    for name, desc in commands.get_telegram_bot_commands():
        assert 1 <= len(name) <= 32
        assert name == name.lower()
        assert name.replace("_", "").isalnum()
        assert 1 <= len(desc) <= 256


def test_discord_native_menu_reuses_curated_slash_registry() -> None:
    menu = get_discord_native_command_menu()
    names = [name for name, _desc in menu]

    assert names == [name for name in commands.TELEGRAM_NATIVE_COMMANDS if name != "vault"]
    assert "video" in names
    assert "vault" not in names
    assert all(1 <= len(desc) <= 100 for _name, desc in menu)


def test_discord_native_model_command_mentions_kimi() -> None:
    """The Discord-native /model description must fit the 100-char cap AND name
    the kimi lane — the compact form keeps every lane visible in the Discord
    slash menu (longer text gets truncated with '...')."""

    menu = dict(get_discord_native_command_menu())
    assert "model" in menu
    assert "kimi" in menu["model"]
    assert len(menu["model"]) <= 100


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
    assert "/linkedin - LinkedIn workshop" in reply
    assert "/vault - Vault operations" in reply
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


# --- Native command uniqueness (issue #18) ------------------------------------
# A duplicate native slash-command name makes the Discord library raise
# CommandAlreadyRegistered and crashes bot startup for every channel. These lock
# the invariant: the menu handed to any adapter is always unique.


def test_shipped_native_menu_has_no_duplicates() -> None:
    """Regression guard: the real curated menu must never ship a duplicate."""
    names = list(commands.TELEGRAM_NATIVE_COMMANDS)
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, dupes


def test_enforce_native_uniqueness_dedupes_and_preserves_order() -> None:
    result = commands._enforce_native_command_uniqueness(
        ("help", "status", "help", "brief"),
        ("help", "status", "brief"),
    )
    assert result == ("help", "status", "brief")


def test_enforce_native_uniqueness_is_noop_on_clean_menu() -> None:
    clean = ("a", "b", "c")
    assert commands._enforce_native_command_uniqueness(clean, clean) == clean


def test_enforce_native_uniqueness_blames_overlay_readd_on_extension(caplog) -> None:
    """An overlay re-adding a core name, or adding its own name twice, is blamed
    on the local extension — not the core menu — so an operator remediates the
    real source (issue #18 adversarial-review finding)."""
    # Merged = core ("help","status") + overlay re-adds "help" and adds "x" twice.
    with caplog.at_level(logging.WARNING, logger="commands"):
        result = commands._enforce_native_command_uniqueness(
            ("help", "status", "help", "x", "x"),
            ("help", "status"),  # ordered core, before the overlay merged
        )
    assert result == ("help", "status", "x")
    assert "/help (local extension)" in caplog.text
    assert "/x (local extension)" in caplog.text
    # Not mis-attributed to core (the static remediation text mentions "core
    # menu"; the attribution tag "/name (core menu)" must not appear).
    assert "(core menu)" not in caplog.text


def test_enforce_native_uniqueness_blames_core_double_on_core(caplog) -> None:
    """A name duplicated within the core menu itself (a dev typo) is blamed on
    the core menu."""
    with caplog.at_level(logging.WARNING, logger="commands"):
        result = commands._enforce_native_command_uniqueness(
            ("help", "help", "status"),
            ("help", "help", "status"),  # the duplicate lives in the core order
        )
    assert result == ("help", "status")
    assert "/help (core menu)" in caplog.text


def test_menu_builder_dedupes_a_duplicated_source(monkeypatch) -> None:
    """Even if TELEGRAM_NATIVE_COMMANDS is reassigned with a duplicate, the shared
    builder every adapter consumes still returns unique names."""
    _register_manager()
    dupd = commands.TELEGRAM_NATIVE_COMMANDS + ("help", "status")
    monkeypatch.setattr(commands, "TELEGRAM_NATIVE_COMMANDS", dupd)
    menu, _hidden = commands.get_telegram_command_menu()
    names = [name for name, _desc in menu]
    assert len(names) == len(set(names)), [n for n in names if names.count(n) > 1]


def test_discord_menu_never_hands_registration_a_duplicate(monkeypatch) -> None:
    """The Discord registration loop calls add_command once per menu entry; a
    duplicate would raise CommandAlreadyRegistered. Prove the menu is unique even
    when the source tuple carries a duplicate."""
    _register_manager()
    dupd = commands.TELEGRAM_NATIVE_COMMANDS + ("help", "model")
    monkeypatch.setattr(commands, "TELEGRAM_NATIVE_COMMANDS", dupd)
    names = [name for name, _desc in get_discord_native_command_menu()]
    assert len(names) == len(set(names)), [n for n in names if names.count(n) > 1]
