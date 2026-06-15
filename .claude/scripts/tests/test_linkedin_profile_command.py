from __future__ import annotations

import asyncio
from pathlib import Path
import sys


CHAT_DIR = Path(__file__).resolve().parents[2] / "chat"
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CHAT_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))

import commands  # noqa: E402
import core_handlers  # noqa: E402
from extension_manager import ExtensionManager  # noqa: E402


def test_linkedin_profile_command_is_router_registered() -> None:
    command_rows = {name: (desc, typ, role) for name, desc, typ, role in commands.COMMANDS}

    desc, typ, role = command_rows["linkedin_profile"]
    assert typ == "router"
    assert role == "admin"
    assert "visible Chrome CDP" in desc
    assert core_handlers.CORE_HANDLERS["linkedin_profile"] is core_handlers.handle_linkedin_profile


def test_linkedin_profile_command_appears_in_integrations_help() -> None:
    manager = ExtensionManager()
    manager.register_core_commands(commands.COMMANDS, commands.CATEGORIES, core_handlers.CORE_HANDLERS)

    help_text = manager.get_help_text(user_role="admin")

    assert "*Integrations*" in help_text
    assert "/linkedin_profile" in help_text


def test_linkedin_profile_usage_is_deterministic() -> None:
    # An invalid subcommand returns the deterministic unknown-command usage string.
    # (args="" would route to the live `status` subcommand, which probes the
    # visible-browser CDP session and is env-dependent.)
    reply = asyncio.run(
        core_handlers.handle_linkedin_profile(
            adapter=None,
            incoming=None,
            args="bogus",
        )
    )

    assert "Unknown LinkedIn profile command" in reply
    assert "/linkedin_profile status" in reply
    assert "/linkedin_profile open" in reply


def test_agent_browser_command_falls_back_to_npm_bin(tmp_path) -> None:
    import browser_control

    # Windows: with no override and an empty PATH, the resolver finds the
    # npm-installed agent-browser.cmd under %APPDATA%\npm.
    npm_dir = tmp_path / "npm"
    npm_dir.mkdir()
    agent_browser = npm_dir / "agent-browser.cmd"
    agent_browser.write_text("@echo off\n", encoding="utf-8")

    resolution = browser_control.resolve_agent_browser_command(
        environ={"PATH": "", "APPDATA": str(tmp_path)},
        platform_name="Windows",
    )
    assert resolution.command == (str(agent_browser),)
    assert resolution.source == "windows-npm"


def test_agent_browser_command_falls_back_to_bare_name() -> None:
    import browser_control

    # Non-Windows (or no npm install found): falls back to the bare command name.
    resolution = browser_control.resolve_agent_browser_command(
        environ={"PATH": ""},
        platform_name="Linux",
    )
    assert resolution.command == ("agent-browser",)
    assert resolution.source == "fallback"


# ── Social-write commands (Phase 1) — registration + handler gate + NL routing ──


def test_linkedin_write_commands_are_router_registered() -> None:
    command_rows = {name: (desc, typ, role) for name, desc, typ, role in commands.COMMANDS}
    for name in ("linkedin_post", "linkedin_connect"):
        desc, typ, role = command_rows[name]
        assert typ == "router"
        assert role == "admin"
        assert "visible Chrome CDP" in desc
    assert core_handlers.CORE_HANDLERS["linkedin_post"] is core_handlers.handle_linkedin_post
    assert core_handlers.CORE_HANDLERS["linkedin_connect"] is core_handlers.handle_linkedin_connect


def test_linkedin_write_commands_appear_in_native_menu_and_categories() -> None:
    assert "linkedin_post" in commands.TELEGRAM_NATIVE_COMMANDS
    assert "linkedin_connect" in commands.TELEGRAM_NATIVE_COMMANDS
    integrations = next(cmds for cat, cmds in commands.CATEGORIES if cat == "Integrations")
    assert "linkedin_post" in integrations
    assert "linkedin_connect" in integrations


def _patch_no_real_browser(monkeypatch, *, driven: list) -> None:
    """Force the social-write driver to never touch a real browser."""
    import browser_control
    import social_write_driver

    monkeypatch.setattr(browser_control, "browser_readiness",
                        lambda *, port=None: {"enabled": True, "cdp_port": port, "cdp_reachable": True})
    monkeypatch.setattr(core_handlers, "_audit_browser_action", lambda **kw: None)

    def _fake_drive(self, task, *, port):
        driven.append((getattr(task, "workflow_id", None), getattr(task, "payload_text", None)))
        return True, "drove (fake)"

    monkeypatch.setattr(
        social_write_driver.AgentBrowserSocialWriteDriver, "drive", _fake_drive
    )
    monkeypatch.setattr(
        social_write_driver.AgentBrowserSocialWriteDriver,
        "readiness",
        lambda self, *, port: {"enabled": True, "cdp_port": port, "cdp_reachable": True},
    )
    monkeypatch.setattr(
        social_write_driver.AgentBrowserSocialWriteDriver,
        "screenshot",
        lambda self, *, port, workflow_id: None,
    )
    monkeypatch.setattr(
        social_write_driver.AgentBrowserSocialWriteDriver, "resolve_port", lambda self: 9222
    )
    monkeypatch.setattr(
        social_write_driver, "append_tracker_row", lambda **kw: True
    )


def test_linkedin_post_blocks_without_approval_phrase(monkeypatch) -> None:
    driven: list = []
    _patch_no_real_browser(monkeypatch, driven=driven)

    reply = asyncio.run(
        core_handlers.handle_linkedin_post(
            adapter=None,
            incoming=None,
            args="https://www.linkedin.com/feed/ | here is a great post body",
        )
    )

    assert "blocked" in reply.lower()
    assert driven == []  # NO task dispatched, NO drive


def test_linkedin_post_body_with_embedded_phrase_is_blocked(monkeypatch) -> None:
    """NM1 at the handler level: an operator message whose post BODY contains the
    approval phrase mid-text, with no trailing confirmation, is BLOCKED."""
    driven: list = []
    _patch_no_real_browser(monkeypatch, driven=driven)

    reply = asyncio.run(
        core_handlers.handle_linkedin_post(
            adapter=None,
            incoming=None,
            args="https://www.linkedin.com/feed/ | our slogan is 'post this to linkedin now' btw",
        )
    )

    assert "blocked" in reply.lower()
    assert driven == []  # the embedded phrase did NOT auto-approve


def test_linkedin_post_body_ENDS_WITH_phrase_is_blocked(monkeypatch) -> None:
    """THE FIX (ban-safety): a drafted BODY whose LAST words are the approval
    phrase, with NO separate trailing confirmation segment, must be BLOCKED.

    This is the body-can-auto-approve vector from the adversarial review — under
    the old gate-on-`raw` `.endswith` check this auto-approved. The body is now
    structurally isolated from the confirmation segment, so it can never approve.
    """
    driven: list = []
    _patch_no_real_browser(monkeypatch, driven=driven)

    reply = asyncio.run(
        core_handlers.handle_linkedin_post(
            adapter=None,
            incoming=None,
            # Body legitimately ends with a call-to-action that IS the phrase.
            args="https://www.linkedin.com/feed/ | Reminder to the team: post this to linkedin now",
        )
    )

    assert "blocked" in reply.lower()
    assert driven == []  # NO task dispatched, NO drive


def test_linkedin_connect_note_ENDS_WITH_phrase_is_blocked(monkeypatch) -> None:
    """THE FIX (ban-safety, connect path): a note ending with the approval phrase
    and NO separate confirmation segment must be BLOCKED."""
    driven: list = []
    _patch_no_real_browser(monkeypatch, driven=driven)

    reply = asyncio.run(
        core_handlers.handle_linkedin_connect(
            adapter=None,
            incoming=None,
            args="https://www.linkedin.com/in/someone | please send this linkedin connection request now",
        )
    )

    assert "blocked" in reply.lower()
    assert driven == []


def test_linkedin_post_allows_with_isolated_confirmation_segment(monkeypatch) -> None:
    """THE FIX (allow path): a correct STRUCTURED command with the approval phrase
    as the final pipe-delimited segment is ALLOWED, and the body is landed clean."""
    driven: list = []
    _patch_no_real_browser(monkeypatch, driven=driven)

    reply = asyncio.run(
        core_handlers.handle_linkedin_post(
            adapter=None,
            incoming=None,
            args="https://www.linkedin.com/feed/ | here is my body | post this to linkedin now",
        )
    )

    assert "blocked" not in reply.lower()
    assert len(driven) == 1
    workflow_id, body = driven[0]
    assert workflow_id == "linkedin.post.create"
    # the confirmation segment never enters the landed body
    assert "post this to linkedin now" not in body
    assert body.strip() == "here is my body"


def test_linkedin_connect_allows_with_isolated_confirmation_segment(monkeypatch) -> None:
    driven: list = []
    _patch_no_real_browser(monkeypatch, driven=driven)

    reply = asyncio.run(
        core_handlers.handle_linkedin_connect(
            adapter=None,
            incoming=None,
            args="https://www.linkedin.com/in/someone | great to connect | send this linkedin connection request now",
        )
    )

    assert "blocked" not in reply.lower()
    assert len(driven) == 1
    assert driven[0][0] == "linkedin.connection.request"


def test_nl_post_to_linkedin_routes_to_browserops_not_a_write() -> None:
    """R1-M4: NL phrasing routes to the read-only browserops intent; the write
    path is reachable only via the explicit /linkedin_post slash command."""
    from extension_manager import ExtensionManager

    manager = ExtensionManager()
    manager.register_core_commands(commands.COMMANDS, commands.CATEGORIES, core_handlers.CORE_HANDLERS)
    manager.register_core_intents(commands.CORE_INTENTS)

    intents = manager.detect_intents("post this to linkedin")
    assert "browserops" in intents
    assert "linkedin_post" not in intents
    assert "linkedin_connect" not in intents
    # neither write command is ever a natural-language intent target
    nl_commands = {i.command for i in manager._intents}
    assert "linkedin_post" not in nl_commands
    assert "linkedin_connect" not in nl_commands
