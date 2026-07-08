from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS.parent / "chat"))

import adb_control  # type: ignore[import-not-found]  # noqa: E402
import browser_control  # type: ignore[import-not-found]  # noqa: E402
import browser_ops  # type: ignore[import-not-found]  # noqa: E402
import commands  # type: ignore[import-not-found]  # noqa: E402
import core_handlers  # type: ignore[import-not-found]  # noqa: E402


def _command_row(name: str) -> tuple[str, str, str, str] | None:
    for row in commands.COMMANDS:
        if row[0] == name:
            return row
    return None


def test_browser_commands_are_registered_as_router_admin() -> None:
    for name in ("browser", "browserops", "linkedin_profile"):
        row = _command_row(name)
        assert row is not None
        assert row[2] == "router"
        assert row[3] == "admin"
        assert name in core_handlers.CORE_HANDLERS


def test_browser_commands_appear_in_integrations_help_category() -> None:
    categories = {name: items for name, items in commands.CATEGORIES}
    integrations = categories["Integrations"]
    assert "browser" in integrations
    assert "browserops" in integrations
    assert "linkedin_profile" in integrations


def test_resolve_agent_browser_from_path(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_which(executable: str, path: str | None = None) -> str | None:
        if executable == "agent-browser":
            return "C:\\Tools\\agent-browser.cmd"
        return None

    monkeypatch.setattr(browser_control.shutil, "which", fake_which)

    resolved = browser_control.resolve_agent_browser_command(environ={"PATH": "C:\\Tools"})

    assert resolved.command == ("C:\\Tools\\agent-browser.cmd",)
    assert resolved.source == "path"


def test_resolve_agent_browser_windows_npm_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(browser_control.shutil, "which", lambda *_args, **_kwargs: None)
    npm = tmp_path / "npm"
    npm.mkdir()
    browser_cmd = npm / "agent-browser.cmd"
    browser_cmd.write_text("@echo off\n", encoding="utf-8")

    resolved = browser_control.resolve_agent_browser_command(
        environ={"APPDATA": str(tmp_path), "PATH": ""},
        platform_name="Windows",
    )

    assert resolved.command == (str(browser_cmd),)
    assert resolved.source == "windows-npm"


def test_cdp_status_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        browser_control,
        "_read_json_url",
        lambda _url, timeout: {
            "Browser": "Chrome/126",
            "Protocol-Version": "1.3",
            "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/browser/1",
        },
    )

    status = browser_control.get_cdp_version(9222)

    assert status["reachable"] is True
    assert status["browser"] == "Chrome/126"
    assert status["websocket_debugger_url"] is True


def test_cdp_status_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_os_error(_url: str, *, timeout: float) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(browser_control, "_read_json_url", raise_os_error)

    status = browser_control.get_cdp_version(9222)

    assert status["reachable"] is False
    assert "connection refused" in status["error"]


def test_linkedin_profile_url_requires_explicit_configuration() -> None:
    assert browser_control.resolve_linkedin_profile_url(environ={}) is None
    assert (
        browser_control.resolve_linkedin_profile_url(
            environ={
                "HOMIE_LINKEDIN_PROFILE_URL": "https://www.linkedin.com/in/test/"
            }
        )
        == "https://www.linkedin.com/in/test/"
    )


def test_tabs_are_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        browser_control,
        "_read_json_url",
        lambda _url, timeout: [
            {
                "id": "tab-1",
                "type": "page",
                "title": "https://www.linkedin.com/feed/?token=secret#section",
                "url": "https://www.linkedin.com/feed/?token=secret#section",
            }
        ],
    )

    tabs = browser_control.list_cdp_tabs(9222)

    assert tabs["reachable"] is True
    assert tabs["tabs"][0]["title"] == "https://www.linkedin.com/feed/"
    assert tabs["tabs"][0]["url"] == "https://www.linkedin.com/feed/"


def test_visible_chrome_guard_accepts_non_headless_process() -> None:
    def runner(_cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0,
            stdout="chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\\ChromeProfile",
            stderr="",
        )

    guard = browser_control.chrome_visibility_guard(9222, platform_name="Windows", runner=runner)

    assert guard["ok"] is True
    assert guard["status"] == "visible"


def test_visible_chrome_guard_rejects_headless_process() -> None:
    def runner(_cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0,
            stdout="chrome.exe --headless --remote-debugging-port=9222",
            stderr="",
        )

    guard = browser_control.chrome_visibility_guard(9222, platform_name="Windows", runner=runner)

    assert guard["ok"] is False
    assert guard["status"] == "headless"


def test_agent_browser_argv_is_argument_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        browser_control,
        "resolve_agent_browser_command",
        lambda **_kwargs: browser_control.AgentBrowserResolution(("agent-browser",), "test"),
    )

    argv, _resolution = browser_control.build_agent_browser_argv(
        ["open", "https://example.com"],
        port=9222,
    )

    assert argv == ["agent-browser", "--cdp", "9222", "open", "https://example.com"]


def test_agent_browser_global_runner_omits_cdp(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(
        browser_control,
        "resolve_agent_browser_command",
        lambda **_kwargs: browser_control.AgentBrowserResolution(("agent-browser",), "test"),
    )

    def runner(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="core guide", stderr="")

    result = browser_control.run_agent_browser_global(
        ["skills", "get", "core"],
        runner=runner,
    )

    assert result.ok is True
    assert calls == [["agent-browser", "skills", "get", "core"]]
    assert "--cdp" not in result.command_label


def test_browser_stream_status_parses_agent_browser_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_agent_browser(args: list[str], *, port: int, **_kwargs: object):
        assert args == ["--json", "stream", "status"]
        assert port == 9222
        return browser_control.CommandResult(
            ok=True,
            returncode=0,
            stdout=json.dumps(
                {
                    "success": True,
                    "data": {
                        "enabled": True,
                        "connected": True,
                        "port": 31137,
                        "screencasting": False,
                    },
                    "error": None,
                }
            ),
            stderr="",
            command_label="agent-browser --cdp 9222 --json stream status",
        )

    monkeypatch.setattr(browser_control, "run_agent_browser", fake_run_agent_browser)

    status = browser_control.browser_stream_status(port=9222)

    assert status == {
        "enabled": True,
        "connected": True,
        "port": 31137,
        "screencasting": False,
        "reason": "ready",
    }


def test_browser_stream_status_redacts_failure_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run_agent_browser(args: list[str], *, port: int, **_kwargs: object):
        return browser_control.CommandResult(
            ok=False,
            returncode=1,
            stdout="",
            stderr="failed at https://example.com/path?token=secret#frag",
            command_label="agent-browser --cdp 9222 --json stream status",
        )

    monkeypatch.setattr(browser_control, "run_agent_browser", fake_run_agent_browser)

    status = browser_control.browser_stream_status(port=9222)

    assert status["reason"] == "failed at https://example.com/path"
    assert "secret" not in json.dumps(status)
    assert "#frag" not in json.dumps(status)


def test_browser_stream_enable_is_idempotent_when_already_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_agent_browser(args: list[str], *, port: int, **_kwargs: object):
        assert port == 9222
        if args == ["--json", "stream", "enable"]:
            return browser_control.CommandResult(
                ok=False,
                returncode=1,
                stdout=json.dumps(
                    {
                        "success": False,
                        "data": None,
                        "error": "Streaming is already enabled for this session",
                    }
                ),
                stderr="",
                command_label="agent-browser --cdp 9222 --json stream enable",
            )
        assert args == ["--json", "stream", "status"]
        return browser_control.CommandResult(
            ok=True,
            returncode=0,
            stdout=json.dumps(
                {
                    "success": True,
                    "data": {
                        "enabled": True,
                        "connected": True,
                        "port": 31137,
                        "screencasting": False,
                    },
                    "error": None,
                }
            ),
            stderr="",
            command_label="agent-browser --cdp 9222 --json stream status",
        )

    monkeypatch.setattr(browser_control, "run_agent_browser", fake_run_agent_browser)

    status = browser_control.browser_stream_enable(port=9222)

    assert status["enabled"] is True
    assert status["connected"] is True
    assert status["port"] == 31137


def test_capture_browser_screenshot_png_deletes_temp_file(tmp_path: Path) -> None:
    created: list[Path] = []

    def runner(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        path = Path(cmd[-1])
        created.append(path)
        path.write_bytes(b"\x89PNG\r\n\x1a\nviewer")
        return SimpleNamespace(returncode=0, stdout="screenshot saved", stderr="")

    data = browser_control.capture_browser_screenshot_png(port=9222, runner=runner)

    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert created
    assert all(not path.exists() for path in created)


def test_browser_viewer_status_is_read_only_and_url_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        browser_control,
        "browser_readiness",
        lambda *, port=None, target="desktop": {
            "status": "attention",
            "cdp_port": 9222,
            "cdp_reachable": True,
            "browser": "Chrome/126",
            "visible_guard": "visible",
            "tab_count": 2,
            "reason": "see https://example.com/path?token=secret#frag",
        },
    )
    monkeypatch.setattr(
        browser_control,
        "browser_stream_status",
        lambda *, port, target="desktop": {
            "enabled": True,
            "connected": True,
            "port": 31137,
            "screencasting": False,
            "reason": "ready",
        },
    )

    status = browser_control.browser_viewer_status()

    assert status["mode"] == "read_only"
    assert status["controls"] == {"browser_input": False, "navigation": False}
    assert status["readiness"]["reason"] == "see https://example.com/path"
    assert "secret" not in json.dumps(status)
    assert "#frag" not in json.dumps(status)


@pytest.mark.asyncio
async def test_browser_status_handler_uses_shared_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core_handlers, "shlex", core_handlers.shlex)
    monkeypatch.setattr(browser_control, "resolve_cdp_port", lambda *_, **__: 9222)
    monkeypatch.setattr(
        browser_control,
        "browser_readiness",
        lambda *, port, target="desktop": {"cdp_port": port, "cdp_reachable": True},
    )
    monkeypatch.setattr(browser_control, "browser_status", lambda *, port: {"port": port})
    monkeypatch.setattr(browser_control, "format_browser_status", lambda status, **_kwargs: f"status:{status['port']}")
    monkeypatch.setattr(core_handlers, "_audit_browser_action", lambda **_kwargs: None)

    message = await core_handlers.handle_browser(None, None, "status")

    assert message == "status:9222"


@pytest.mark.asyncio
async def test_browser_handler_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PhoneOps F6 (issue #94 class): a slow agent-browser subprocess must not
    starve the bot's event loop — before the asyncio.to_thread fix, one stalled
    /browser call froze every other chat user, the heartbeat, and the MC relay."""
    import asyncio
    import time as _time

    monkeypatch.setattr(browser_control, "resolve_cdp_port", lambda *_, **__: 9222)
    monkeypatch.setattr(
        browser_control,
        "browser_readiness",
        lambda *, port, target="desktop": {"cdp_port": port, "cdp_reachable": True},
    )

    def slow_run_agent_browser(
        _args: list[str], *, port: int, session: str | None = None, **_k: Any
    ) -> SimpleNamespace:
        _time.sleep(0.4)  # stands in for a hung CDP/adb chain
        return SimpleNamespace(
            ok=True,
            output="snapshot text",
            returncode=0,
            command_label="agent-browser snapshot",
        )

    monkeypatch.setattr(browser_control, "run_agent_browser", slow_run_agent_browser)
    monkeypatch.setattr(core_handlers, "_audit_browser_action", lambda **_kwargs: None)

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    ticker_task = asyncio.create_task(ticker())
    try:
        message = await core_handlers.handle_browser(None, None, "snapshot")
    finally:
        ticker_task.cancel()

    assert "snapshot text" in message
    assert ticks >= 5, f"event loop starved during /browser snapshot (ticks={ticks})"


@pytest.mark.asyncio
async def test_browserops_handler_renders_capabilities_and_audits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audits: list[dict[str, object]] = []
    readiness = {
        "status": "ready",
        "cdp_port": 9222,
        "cdp_reachable": True,
        "browser": "Chrome/126",
        "visible_guard": "visible",
        "tab_count": 1,
        "reason": "ready",
    }
    pack = {
        "specialist": {"name": "Browser Homie", "lane": "browserops"},
        "readiness": readiness,
        "stream": {"enabled": False, "connected": False, "port": None},
        "guide": {"source": "agent-browser skills get core", "reason": "not requested"},
        "rules": ["Attach to visible Chrome"],
        "workflows": [],
    }

    monkeypatch.setattr(browser_control, "browser_readiness", lambda: readiness)
    monkeypatch.setattr(
        browser_ops,
        "build_browserops_capability_pack",
        lambda *_args, **_kwargs: pack,
    )
    monkeypatch.setattr(
        browser_ops,
        "format_browserops_capabilities",
        lambda _pack: "Browser Homie ready",
    )
    monkeypatch.setattr(
        core_handlers,
        "_audit_browser_action",
        lambda **kwargs: audits.append(kwargs),
    )

    message = await core_handlers.handle_browserops(None, None, "capabilities")

    assert message == "Browser Homie ready"
    assert audits[0]["workflow_id"] == "browserops.capabilities"
    assert audits[-1]["outcome"] == "succeeded"


@pytest.mark.asyncio
async def test_browser_open_without_url_is_audited_as_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audits: list[dict[str, object]] = []

    monkeypatch.setattr(browser_control, "resolve_cdp_port", lambda *_, **__: 9222)
    monkeypatch.setattr(
        browser_control,
        "browser_readiness",
        lambda *, port, target="desktop": {"cdp_port": port, "cdp_reachable": True},
    )
    monkeypatch.setattr(core_handlers, "_audit_browser_action", lambda **kwargs: audits.append(kwargs))

    message = await core_handlers.handle_browser(None, None, "open")

    assert "Browser workflow blocked" in message
    assert "browser.open" in message
    assert audits[0]["workflow_id"] == "browser.open"
    assert audits[0]["outcome"] == "blocked"


# ── P4.0/Decision 5 — the /browser chat-command target ───────────────────────


def test_extract_browser_target_parses_keyword_and_flag() -> None:
    ex = core_handlers._extract_browser_target
    targets = browser_control.BROWSER_TARGETS
    assert ex(["status", "ghost"], targets) == ("ghost", ["status"])
    assert ex(["ghost", "status"], targets) == ("ghost", ["status"])
    assert ex(["open", "https://x.com", "phone"], targets) == ("phone", ["open", "https://x.com"])
    assert ex(["status", "--target", "ghost"], targets) == ("ghost", ["status"])
    assert ex(["status", "--target=phone"], targets) == ("phone", ["status"])
    assert ex(["status"], targets) == ("desktop", ["status"])
    # A bare 'desktop' is NEVER stripped — a real arg can't be eaten as a target.
    assert ex(["open", "desktop"], targets) == ("desktop", ["open", "desktop"])


@pytest.mark.asyncio
async def test_browser_command_ghost_blocked_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMIE_GHOST_ENABLED", raising=False)
    audits: list[dict[str, object]] = []
    monkeypatch.setattr(core_handlers, "_audit_browser_action", lambda **k: audits.append(k))

    message = await core_handlers.handle_browser(None, None, "status ghost")

    assert "Ghost is disabled" in message
    assert "HOMIE_GHOST_ENABLED" in message
    assert audits[0]["outcome"] == "blocked"


@pytest.mark.asyncio
async def test_browser_command_phone_blocked_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    # Closes the P3.0 gap: the /browser command now gates the phone too.
    monkeypatch.delenv("HOMIE_PHONEOPS_ENABLED", raising=False)
    audits: list[dict[str, object]] = []
    monkeypatch.setattr(core_handlers, "_audit_browser_action", lambda **k: audits.append(k))

    message = await core_handlers.handle_browser(None, None, "tabs phone")

    assert "PhoneOps is disabled" in message
    assert audits[0]["outcome"] == "blocked"


@pytest.mark.asyncio
async def test_browser_capabilities_ghost_gated_before_browserops_delegation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PhoneOps review F2 (issue #90): `/browser capabilities ghost` with the
    ghost disabled must refuse — not silently answer for the desktop via the
    browserops delegation."""

    monkeypatch.delenv("HOMIE_GHOST_ENABLED", raising=False)
    audits: list[dict[str, object]] = []
    monkeypatch.setattr(core_handlers, "_audit_browser_action", lambda **k: audits.append(k))
    monkeypatch.setattr(
        core_handlers,
        "handle_browserops",
        lambda *_a, **_k: pytest.fail("gated target must not reach browserops"),
    )

    message = await core_handlers.handle_browser(None, None, "capabilities ghost")

    assert "Ghost is disabled" in message
    assert audits[0]["outcome"] == "blocked"
    assert audits[0]["target"] == "ghost"


@pytest.mark.asyncio
async def test_browser_capabilities_desktop_still_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Desktop (default target) delegation is byte-identical after the F2 gate
    reorder — capabilities/guide/context still reach browserops."""

    called: list[str] = []

    async def fake_browserops(adapter, incoming, delegated, *, collect_only=False):
        called.append(delegated)
        return "caps"

    monkeypatch.setattr(core_handlers, "handle_browserops", fake_browserops)
    monkeypatch.setattr(core_handlers, "_audit_browser_action", lambda **_k: None)

    message = await core_handlers.handle_browser(None, None, "capabilities")

    assert message == "caps"
    assert called == ["capabilities"]


@pytest.mark.asyncio
async def test_browser_invalid_target_flag_refuses_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PhoneOps review F3 (issue #91): a typo'd --target value must refuse
    loudly, never silently fall back to the operator's desktop Chrome."""

    audits: list[dict[str, object]] = []
    monkeypatch.setattr(core_handlers, "_audit_browser_action", lambda **k: audits.append(k))

    message = await core_handlers.handle_browser(None, None, "status --target ghsot")

    assert "Browser target error" in message
    assert "ghsot" in message
    assert audits[0]["outcome"] == "blocked"
    assert audits[0]["target"] == "ghsot"  # the REJECTED raw value (issue #100)


@pytest.mark.asyncio
async def test_browser_trailing_typo_target_refuses_instead_of_desktop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The review's exact scenario: `/browser open <url> ghsot` used to open the
    URL on the operator's visible DESKTOP Chrome with a success reply."""

    audits: list[dict[str, object]] = []
    monkeypatch.setattr(core_handlers, "_audit_browser_action", lambda **k: audits.append(k))
    monkeypatch.setattr(
        browser_control,
        "run_agent_browser",
        lambda *_a, **_k: pytest.fail("misrouted command must not reach the desktop browser"),
    )

    message = await core_handlers.handle_browser(
        None, None, "open https://internal-admin.example ghsot"
    )

    assert "Unexpected browser argument" in message
    assert "ghsot" in message
    assert audits[0]["outcome"] == "blocked"


def test_extract_browser_target_mid_list_keyword_not_consumed() -> None:
    """Bare phone/ghost counts only in leading/trailing position (issue #91) —
    a mid-list token is a real argument; invalid flag values raise."""

    ex = core_handlers._extract_browser_target
    targets = browser_control.BROWSER_TARGETS
    assert ex(["open", "phone", "https://x.com"], targets) == (
        "desktop",
        ["open", "phone", "https://x.com"],
    )
    with pytest.raises(ValueError, match="ghsot"):
        ex(["status", "--target", "ghsot"], targets)
    with pytest.raises(ValueError, match="tablet"):
        ex(["status", "--target=tablet"], targets)


@pytest.mark.asyncio
async def test_browser_command_ghost_status_renders_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMIE_GHOST_ENABLED", "true")
    monkeypatch.setenv("HOMIE_GHOST_ADB_SERIAL", "emulator-5554")
    monkeypatch.setattr(
        browser_control, "resolve_target_port", lambda t: 18224 if t == "ghost" else 18222
    )
    monkeypatch.setattr(
        browser_control,
        "browser_readiness",
        lambda *, port, target="desktop": {
            "cdp_port": port,
            "target": target,
            "status": "ready",
            "cdp_reachable": True,
        },
    )
    labels: list[str] = []

    def fake_format(readiness, *, label="Browser"):
        labels.append(label)
        return f"ghost-status:{readiness['cdp_port']}"

    monkeypatch.setattr(browser_control, "format_browser_readiness", fake_format)
    monkeypatch.setattr(core_handlers, "_audit_browser_action", lambda **_k: None)

    message = await core_handlers.handle_browser(None, None, "status ghost")

    assert message == "ghost-status:18224"  # ghost port, readiness-based status
    assert labels == ["Ghost Browser"]


@pytest.mark.asyncio
async def test_browser_command_ghost_open_threads_serial_and_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import browser_workflows  # type: ignore[import-not-found]

    monkeypatch.setenv("HOMIE_GHOST_ENABLED", "true")
    monkeypatch.setenv("HOMIE_GHOST_ADB_SERIAL", "emulator-5554")
    monkeypatch.setenv("HOMIE_PHONE_ADB_SERIAL", "192.168.0.174:5555")
    monkeypatch.setattr(browser_control, "resolve_target_port", lambda _t: 18224)
    monkeypatch.setattr(
        browser_control,
        "browser_readiness",
        lambda *, port, target="desktop": {"cdp_port": port, "cdp_reachable": True},
    )
    monkeypatch.setattr(
        browser_workflows,
        "require_browser_workflow_permission",
        lambda wid, raw, **k: SimpleNamespace(
            allowed=True,
            outcome="allowed",
            reason="ok",
            workflow_id=wid,
            target_url=k.get("target_url"),
            next_action="",
        ),
    )
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        browser_control,
        "ensure_phone_chrome_ready",
        lambda *, local_port=None, serial=None, runner=None: seen.update(
            serial=serial, port=local_port
        )
        or True,
    )

    def fake_run(args, *, port, session=None, **_k):
        seen["session"] = session
        return SimpleNamespace(ok=True, output="", returncode=0, command_label="agent-browser open")

    monkeypatch.setattr(browser_control, "run_agent_browser", fake_run)
    monkeypatch.setattr(core_handlers, "_audit_browser_action", lambda **_k: None)

    message = await core_handlers.handle_browser(None, None, "open https://example.com ghost")

    assert "Opened in visible browser" in message
    assert seen["serial"] == "emulator-5554"  # ghost serial, never the phone's
    assert seen["session"] == "homie-ghost"


@pytest.mark.asyncio
async def test_linkedin_profile_wrapper_uses_shared_browser_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], int]] = []

    def fake_run_agent_browser(args: list[str], *, port: int) -> browser_control.CommandResult:
        calls.append((args, port))
        return browser_control.CommandResult(
            ok=True,
            returncode=0,
            stdout="opened",
            stderr="",
            command_label="agent-browser --cdp 9222 open",
        )

    monkeypatch.setattr(browser_control, "resolve_cdp_port", lambda *_, **__: 9222)
    monkeypatch.setattr(
        browser_control,
        "browser_readiness",
        lambda *, port: {"cdp_port": port, "cdp_reachable": True},
    )
    monkeypatch.setattr(
        browser_control,
        "resolve_linkedin_profile_url",
        lambda: "https://www.linkedin.com/in/test/",
    )
    monkeypatch.setattr(browser_control, "run_agent_browser", fake_run_agent_browser)
    monkeypatch.setattr(core_handlers, "_audit_browser_action", lambda **_kwargs: None)

    message = await core_handlers.handle_linkedin_profile(None, None, "open")

    assert "Opened LinkedIn profile" in message
    assert calls == [(["open", "https://www.linkedin.com/in/test/"], 9222)]


@pytest.mark.asyncio
async def test_linkedin_profile_open_blocks_without_configured_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audits: list[dict[str, object]] = []

    monkeypatch.setattr(browser_control, "resolve_cdp_port", lambda *_, **__: 9222)
    monkeypatch.setattr(
        browser_control,
        "browser_readiness",
        lambda *, port: {"cdp_port": port, "cdp_reachable": True},
    )
    monkeypatch.setattr(browser_control, "resolve_linkedin_profile_url", lambda: None)
    monkeypatch.setattr(
        core_handlers,
        "_audit_browser_action",
        lambda **kwargs: audits.append(kwargs),
    )

    message = await core_handlers.handle_linkedin_profile(None, None, "open")

    assert "LinkedIn profile URL is not configured" in message
    assert audits[0]["workflow_id"] == "linkedin.profile.open"
    assert audits[0]["outcome"] == "blocked"


@pytest.mark.asyncio
async def test_linkedin_profile_edit_blocks_without_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audits: list[dict[str, object]] = []

    monkeypatch.setattr(browser_control, "resolve_cdp_port", lambda *_, **__: 9222)
    monkeypatch.setattr(
        browser_control,
        "browser_readiness",
        lambda *, port: {"cdp_port": port, "cdp_reachable": True},
    )
    monkeypatch.setattr(
        browser_control,
        "resolve_linkedin_profile_url",
        lambda: pytest.fail("blocked write path must not resolve profile URL"),
    )
    monkeypatch.setattr(
        core_handlers,
        "_audit_browser_action",
        lambda **kwargs: audits.append(kwargs),
    )

    message = await core_handlers.handle_linkedin_profile(None, None, "edit")

    assert "Browser workflow blocked" in message
    assert "linkedin.profile.edit" in message
    assert audits[0]["workflow_id"] == "linkedin.profile.edit"
    assert audits[0]["outcome"] == "blocked"


@pytest.mark.asyncio
async def test_linkedin_profile_nl_open_phrase_infers_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Issue #36 — NL intent dispatch passes args=""; an "open" navigation signal in
    # the message text must infer the gated open subcommand.
    calls: list[tuple[list[str], int]] = []

    def fake_run_agent_browser(args: list[str], *, port: int) -> browser_control.CommandResult:
        calls.append((args, port))
        return browser_control.CommandResult(
            ok=True,
            returncode=0,
            stdout="opened",
            stderr="",
            command_label="agent-browser --cdp 9222 open",
        )

    monkeypatch.setattr(browser_control, "resolve_cdp_port", lambda *_, **__: 9222)
    monkeypatch.setattr(
        browser_control,
        "browser_readiness",
        lambda *, port: {"cdp_port": port, "cdp_reachable": True},
    )
    monkeypatch.setattr(
        browser_control,
        "resolve_linkedin_profile_url",
        lambda: "https://www.linkedin.com/in/test/",
    )
    monkeypatch.setattr(browser_control, "run_agent_browser", fake_run_agent_browser)
    monkeypatch.setattr(core_handlers, "_audit_browser_action", lambda **_kwargs: None)

    incoming = SimpleNamespace(text="open my LinkedIn profile")
    message = await core_handlers.handle_linkedin_profile(None, incoming, "")

    assert "Opened LinkedIn profile" in message
    assert calls == [(["open", "https://www.linkedin.com/in/test/"], 9222)]


@pytest.mark.asyncio
async def test_linkedin_profile_nl_check_phrase_infers_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Issue #36 — without a navigation signal the NL path must default to the
    # read-only "status" subcommand and never open the browser.
    calls: list[tuple[list[str], int]] = []

    def fake_run_agent_browser(args: list[str], *, port: int) -> browser_control.CommandResult:
        calls.append((args, port))
        return browser_control.CommandResult(
            ok=True, returncode=0, stdout="", stderr="", command_label="",
        )

    monkeypatch.setattr(browser_control, "resolve_cdp_port", lambda *_, **__: 9222)
    monkeypatch.setattr(
        browser_control,
        "browser_readiness",
        lambda *, port: {"cdp_port": port, "cdp_reachable": True},
    )
    monkeypatch.setattr(
        browser_control,
        "browser_status",
        lambda *, port: {"cdp_port": port, "cdp_reachable": True},
    )
    monkeypatch.setattr(
        browser_control,
        "format_browser_status",
        lambda status, label: f"{label} status: reachable",
    )
    monkeypatch.setattr(
        browser_control,
        "resolve_linkedin_profile_url",
        lambda: pytest.fail("status path must not resolve the profile URL"),
    )
    monkeypatch.setattr(browser_control, "run_agent_browser", fake_run_agent_browser)
    monkeypatch.setattr(core_handlers, "_audit_browser_action", lambda **_kwargs: None)

    incoming = SimpleNamespace(text="check my LinkedIn profile")
    message = await core_handlers.handle_linkedin_profile(None, incoming, "")

    assert "LinkedIn Browser status: reachable" in message
    assert calls == []


# ── M12 phone-drive primitives ───────────────────────────────────────────

_SNAPSHOT_SAMPLE = """\
- link "About" [ref=e1]
- button "Google apps" [expanded=false, ref=e15]
- button "Share" [ref=e16]
  - generic [ref=e22] clickable [cursor:pointer]
- combobox "Search" [expanded=false, ref=e24]
- button "Google Search" [ref=e20]
- link "Privacy" [ref=e7]
"""


def test_parse_snapshot_elements_extracts_refs_and_drops_generic() -> None:
    elements = browser_control.parse_snapshot_elements(_SNAPSHOT_SAMPLE)
    refs = [e["ref"] for e in elements]
    assert "e22" not in refs  # unnamed generic wrapper dropped
    assert {"ref": "e20", "role": "button", "name": "Google Search"} in elements
    assert {"ref": "e24", "role": "combobox", "name": "Search"} in elements
    assert len(elements) == 6


def test_parse_snapshot_elements_dedupes_refs() -> None:
    doubled = _SNAPSHOT_SAMPLE + '- button "Google Search" [ref=e20]\n'
    elements = browser_control.parse_snapshot_elements(doubled)
    assert [e["ref"] for e in elements].count("e20") == 1


def test_build_browser_act_args_maps_every_kind() -> None:
    build = browser_control.build_browser_act_args
    assert build("click", ref="e12") == ["click", "@e12"]
    assert build("fill", ref="e24", text="hello world") == ["fill", "@e24", "hello world"]
    assert build("press", key="Enter") == ["press", "Enter"]
    assert build("press", key="Control+a") == ["press", "Control+a"]
    assert build("scroll") == ["scroll", "down", "600"]
    assert build("scroll", direction="up", amount=250) == ["scroll", "up", "250"]
    assert build("back") == ["back"]
    assert build("forward") == ["forward"]
    assert build("reload") == ["reload"]


@pytest.mark.parametrize(
    ("kind", "kwargs"),
    [
        ("click", {}),                             # missing ref
        ("click", {"ref": "not-a-ref"}),           # malformed ref
        ("click", {"ref": "e1; rm -rf"}),          # injection shape
        ("fill", {"ref": "e2"}),                   # missing text
        ("press", {"key": "Enter; whoami"}),       # malformed key
        ("scroll", {"direction": "sideways"}),     # bad direction
        ("scroll", {"amount": 999999}),            # out of range
        ("eval", {}),                              # kind never allowed
    ],
)
def test_build_browser_act_args_rejects_malformed_input(kind: str, kwargs: dict) -> None:
    with pytest.raises(ValueError):
        browser_control.build_browser_act_args(kind, **kwargs)


def test_browser_act_shells_mapped_argv_with_cdp_port() -> None:
    recorded: dict = {}

    def fake_runner(argv, **kwargs):
        recorded["argv"] = argv
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    result = browser_control.browser_act("click", ref="e12", port=18222, runner=fake_runner)
    assert result.ok
    assert recorded["argv"][-2:] == ["click", "@e12"]
    assert "--cdp" in recorded["argv"]
    assert "18222" in recorded["argv"]


def test_browser_viewer_act_workflows_are_registered() -> None:
    from browser_workflows import get_browser_workflow

    act = get_browser_workflow("browser.viewer.act")
    nav = get_browser_workflow("browser.viewer.navigate")
    elements = get_browser_workflow("browser.viewer.elements")
    assert act is not None and act.classification == "interact"
    assert nav is not None and nav.is_navigation
    assert elements is not None and elements.classification == "read"


def test_ensure_browser_window_restored_windows_only(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_runner(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="restored\n", stderr="")

    monkeypatch.setattr(browser_control.platform, "system", lambda: "Windows")
    assert browser_control.ensure_browser_window_restored(port=18222, runner=fake_runner)
    assert calls[0][0] == "powershell"
    assert "remote-debugging-port=18222" in calls[0][-1]

    monkeypatch.setattr(browser_control.platform, "system", lambda: "Linux")
    assert browser_control.ensure_browser_window_restored(port=18222, runner=fake_runner) is False
    assert len(calls) == 1  # non-Windows never shells


def test_browser_act_restores_window_before_input(monkeypatch) -> None:
    order: list[str] = []

    def fake_runner(argv, **kwargs):
        order.append("powershell" if argv[0] == "powershell" else "agent-browser")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(browser_control.platform, "system", lambda: "Windows")
    result = browser_control.browser_act("click", ref="e2", port=18222, runner=fake_runner)
    assert result.ok
    assert order == ["powershell", "agent-browser"]


# ── P3.0 PhoneOps — adb_control transport slice ──────────────────────────

_ADB_ENV = {"HOMIE_ADB_BIN": "adb", "HOMIE_PHONE_ADB_SERIAL": "100.114.68.10:5555"}

_DEVICES_CONNECTED = """\
List of devices attached
100.114.68.10:5555     device product:e3qxeea model:SM_S928U transport_id:2
"""

_FORWARD_PRESENT = "100.114.68.10:5555 tcp:18223 localabstract:chrome_devtools_remote\n"


def _make_adb_runner(responses: list[tuple[str, int, str]]):
    """Fake runner dispatching on the adb sub-command (argv minus binary/-s)."""

    calls: list[list[str]] = []

    def runner(argv, **_kwargs):
        calls.append(list(argv))
        args = argv[1:]
        if args[:1] == ["-s"]:
            args = args[2:]
        joined = " ".join(args)
        for prefix, returncode, stdout in responses:
            if joined.startswith(prefix):
                return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return runner, calls


def test_resolve_adb_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    assert adb_control.resolve_adb(environ={"HOMIE_ADB_BIN": "X:\\adb.exe"}) == "X:\\adb.exe"

    monkeypatch.setattr(adb_control.shutil, "which", lambda *_a, **_k: "C:\\pt\\adb.exe")
    assert adb_control.resolve_adb(environ={"PATH": "C:\\pt"}) == "C:\\pt\\adb.exe"

    monkeypatch.setattr(adb_control.shutil, "which", lambda *_a, **_k: None)
    monkeypatch.setattr(adb_control.os.path, "exists", lambda _p: False)
    with pytest.raises(FileNotFoundError):
        adb_control.resolve_adb(environ={})


def test_adb_device_state_parses_states() -> None:
    for state in ("device", "offline", "unauthorized"):
        runner, _ = _make_adb_runner(
            [("devices -l", 0, f"List of devices attached\n100.114.68.10:5555 {state}\n")]
        )
        result = adb_control.adb_device_state(runner=runner, environ=_ADB_ENV)
        assert result["state"] == state
        assert result["serial"] == "100.114.68.10:5555"


def test_adb_device_state_none_and_multiple() -> None:
    runner, _ = _make_adb_runner([("devices -l", 0, "List of devices attached\n")])
    missing = adb_control.adb_device_state(runner=runner, environ=_ADB_ENV)
    assert missing["state"] == "none"

    two = "List of devices attached\nserial-a device\nserial-b device\n"
    runner, _ = _make_adb_runner([("devices -l", 0, two)])
    no_serial_env = {"HOMIE_ADB_BIN": "adb"}
    result = adb_control.adb_device_state(runner=runner, environ=no_serial_env)
    assert result["state"] == "multiple"
    assert "HOMIE_PHONE_ADB_SERIAL" in result["detail"]

    runner, _ = _make_adb_runner([("devices -l", 0, "List of devices attached\nsolo-1 device\n")])
    solo = adb_control.adb_device_state(runner=runner, environ=no_serial_env)
    assert solo["state"] == "device"
    assert solo["serial"] == "solo-1"


def test_adb_scoped_calls_always_pass_serial() -> None:
    runner, calls = _make_adb_runner([("shell input keyevent", 0, "")])
    assert adb_control.wake_screen(runner=runner, environ=_ADB_ENV)
    assert calls[0][:3] == ["adb", "-s", "100.114.68.10:5555"]
    assert calls[0][-1] == "KEYCODE_WAKEUP"


def test_ensure_forward_is_idempotent_when_present() -> None:
    runner, calls = _make_adb_runner([("forward --list", 0, _FORWARD_PRESENT)])
    assert adb_control.ensure_forward(18223, runner=runner, environ=_ADB_ENV)
    forwards_added = [c for c in calls if "tcp:18223" in c and "--list" not in c]
    assert forwards_added == []  # present -> no re-add


def test_ensure_forward_readds_when_missing() -> None:
    runner, calls = _make_adb_runner([("forward --list", 0, "")])
    assert adb_control.ensure_forward(18223, runner=runner, environ=_ADB_ENV)
    add_call = calls[-1]
    assert add_call[:3] == ["adb", "-s", "100.114.68.10:5555"]
    assert add_call[3:] == ["forward", "tcp:18223", "localabstract:chrome_devtools_remote"]


def test_ensure_forward_outcome_reports_bind_failure() -> None:
    runner, _ = _make_adb_runner(
        [
            ("forward --list", 0, ""),
            ("forward tcp:18223", 1, "error: cannot bind listener: Address already in use"),
        ]
    )
    outcome = adb_control.ensure_forward_outcome(18223, runner=runner, environ=_ADB_ENV)
    assert outcome["ok"] is False
    assert outcome["status"] == "bind_failed"
    assert outcome["detail"] == "local port 18223 unavailable"


def test_ensure_forward_outcome_reports_generic_add_failure() -> None:
    runner, _ = _make_adb_runner(
        [("forward --list", 0, ""), ("forward tcp:18223", 1, "error: device offline")]
    )
    outcome = adb_control.ensure_forward_outcome(18223, runner=runner, environ=_ADB_ENV)
    assert outcome["ok"] is False
    assert outcome["detail"] == "could not establish adb forward tcp:18223"


def test_adb_transport_guard_ready_requires_device_and_forward() -> None:
    runner, _ = _make_adb_runner(
        [("devices -l", 0, _DEVICES_CONNECTED), ("forward --list", 0, _FORWARD_PRESENT)]
    )
    guard = adb_control.adb_transport_guard(18223, runner=runner, environ=_ADB_ENV)
    assert guard == {
        "status": "device",
        "ok": True,
        "detail": "adb device ready; forward tcp:18223 present",
    }


def test_adb_transport_guard_unauthorized() -> None:
    runner, _ = _make_adb_runner(
        [("devices -l", 0, "List of devices attached\n100.114.68.10:5555 unauthorized\n")]
    )
    guard = adb_control.adb_transport_guard(18223, runner=runner, environ=_ADB_ENV)
    assert guard["status"] == "unauthorized"
    assert guard["ok"] is False
    assert "accept the debugging prompt" in guard["detail"]


def test_adb_transport_guard_missing_adb(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adb_control.shutil, "which", lambda *_a, **_k: None)
    monkeypatch.setattr(adb_control.os.path, "exists", lambda _p: False)
    guard = adb_control.adb_transport_guard(18223, environ={"PATH": ""})
    assert guard == {"status": "unknown", "ok": False, "detail": "adb executable not found"}


def test_adb_transport_guard_reboot_reset_reason() -> None:
    runner, _ = _make_adb_runner(
        [
            ("devices -l", 0, "List of devices attached\n"),
            ("connect", 0, "failed to connect to '100.114.68.10:5555': Connection refused"),
        ]
    )
    guard = adb_control.adb_transport_guard(18223, runner=runner, environ=_ADB_ENV)
    assert guard["status"] == "no_device"
    assert guard["detail"] == (
        "wireless adb reset by reboot — re-run 'adb tcpip 5555' over USB or re-pair"
    )


def test_adb_transport_guard_unreachable_reason() -> None:
    runner, _ = _make_adb_runner(
        [
            ("devices -l", 0, "List of devices attached\n"),
            ("connect", 0, "failed to connect to '100.114.68.10:5555': Operation timed out"),
        ]
    )
    guard = adb_control.adb_transport_guard(18223, runner=runner, environ=_ADB_ENV)
    assert guard["status"] == "no_device"
    assert guard["detail"] == (
        "phone unreachable over adb — connect to 100.114.68.10:5555 failed"
    )


def test_adb_transport_guard_offline_persists_after_retry() -> None:
    offline = "List of devices attached\n100.114.68.10:5555 offline\n"
    runner, calls = _make_adb_runner(
        [("devices -l", 0, offline), ("connect", 0, "already connected")]
    )
    guard = adb_control.adb_transport_guard(18223, runner=runner, environ=_ADB_ENV)
    assert guard["status"] == "offline"
    assert "toggle wireless debugging" in guard["detail"]
    assert any("connect" in c for c in calls)  # one reconnect retry attempted


def test_adb_transport_guard_forward_failure_maps_no_forward() -> None:
    runner, _ = _make_adb_runner(
        [
            ("devices -l", 0, _DEVICES_CONNECTED),
            ("forward --list", 0, ""),
            ("forward tcp:18223", 1, "error: something broke"),
        ]
    )
    guard = adb_control.adb_transport_guard(18223, runner=runner, environ=_ADB_ENV)
    assert guard["status"] == "no_forward"
    assert guard["ok"] is False
    assert guard["detail"] == "could not establish adb forward tcp:18223"


# ── P3.0 PhoneOps — browser_control target dimension ─────────────────────


def test_resolve_target_port_desktop_and_phone() -> None:
    env = {"HOMIE_BROWSER_CDP_PORT": "18222"}
    assert browser_control.resolve_target_port("desktop", environ=env) == 18222
    assert browser_control.resolve_target_port(environ=env) == 18222  # default desktop
    assert browser_control.resolve_target_port("phone", environ=env) == 18223
    assert (
        browser_control.resolve_target_port("phone", environ={"HOMIE_PHONE_CDP_PORT": "18500"})
        == 18500
    )
    with pytest.raises(ValueError):
        browser_control.resolve_target_port("tablet", environ=env)


def test_resolve_target_registry_shape() -> None:
    env = {"HOMIE_BROWSER_CDP_PORT": "18222"}
    desktop = browser_control.resolve_target("desktop", environ=env)
    phone = browser_control.resolve_target("phone", environ=env)
    assert desktop == {"target": "desktop", "transport": "local", "port": 18222}
    assert phone == {"target": "phone", "transport": "adb", "port": 18223}
    with pytest.raises(ValueError):
        browser_control.resolve_target("tablet", environ=env)


def _patch_desktop_readiness_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_control, "resolve_cdp_port", lambda **_k: 18222)
    monkeypatch.setattr(
        browser_control,
        "resolve_agent_browser_command",
        lambda **_k: browser_control.AgentBrowserResolution(("agent-browser",), "path"),
    )
    monkeypatch.setattr(
        browser_control,
        "get_cdp_version",
        lambda _port, **_k: {"reachable": True, "browser": "Chrome/143"},
    )
    monkeypatch.setattr(
        browser_control,
        "chrome_visibility_guard",
        lambda _port, **_k: {"status": "visible", "ok": True, "detail": "visible"},
    )
    monkeypatch.setattr(
        browser_control,
        "list_cdp_tabs",
        lambda _port, **_k: {"reachable": True, "tabs": [{"id": "1"}]},
    )


def test_desktop_parity_readiness_target_kwarg_is_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_desktop_readiness_deps(monkeypatch)
    absent = browser_control.browser_readiness()
    explicit = browser_control.browser_readiness(target="desktop")
    assert json.dumps(absent, sort_keys=True) == json.dumps(explicit, sort_keys=True)
    assert "target" not in absent  # desktop readiness envelope unchanged from M12
    assert "transport" not in absent


def test_phone_readiness_mirrors_browser_readiness_key_for_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_desktop_readiness_deps(monkeypatch)
    desktop = browser_control.browser_readiness()

    monkeypatch.setattr(
        browser_control.adb_control,
        "adb_transport_guard",
        lambda _port, **_k: {"status": "device", "ok": True, "detail": "ready"},
    )
    phone = browser_control.phone_readiness(local_port=18223)

    assert set(desktop.keys()) <= set(phone.keys())
    assert set(phone.keys()) - set(desktop.keys()) == {"target", "transport"}
    assert phone["target"] == "phone"
    assert phone["transport"] == "adb"
    assert phone["status"] == "ready"
    assert phone["visible_guard"] == "device"
    assert phone["cdp_port"] == 18223
    assert phone["tab_count"] == 1


def test_phone_readiness_maps_cdp_failures_to_operator_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_desktop_readiness_deps(monkeypatch)
    monkeypatch.setattr(
        browser_control.adb_control,
        "adb_transport_guard",
        lambda _port, **_k: {"status": "device", "ok": True, "detail": "ready"},
    )

    monkeypatch.setattr(
        browser_control,
        "get_cdp_version",
        lambda _port, **_k: {"reachable": False, "error": "Connection refused"},
    )
    refused = browser_control.phone_readiness(local_port=18223)
    assert refused["status"] == "attention"
    assert refused["reason"] == (
        "phone Chrome devtools socket unavailable — open Chrome on the phone"
    )

    monkeypatch.setattr(
        browser_control,
        "get_cdp_version",
        lambda _port, **_k: {"reachable": False, "error": "The read operation timed out"},
    )
    frozen = browser_control.phone_readiness(local_port=18223)
    assert frozen["reason"] == "phone Chrome unresponsive (backgrounded or frozen)"

    # Chrome killed/fully frozen: the forward accepts, the phone socket is gone
    # (seen live 2026-07-06 with the operator holding the phone in the app).
    monkeypatch.setattr(
        browser_control,
        "get_cdp_version",
        lambda _port, **_k: {
            "reachable": False,
            "error": "Remote end closed connection without response",
        },
    )
    killed = browser_control.phone_readiness(local_port=18223)
    assert killed["reason"] == (
        "phone Chrome devtools socket unavailable — open Chrome on the phone"
    )


def test_phone_readiness_guard_failure_carries_guard_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_desktop_readiness_deps(monkeypatch)
    monkeypatch.setattr(
        browser_control.adb_control,
        "adb_transport_guard",
        lambda _port, **_k: {
            "status": "unauthorized",
            "ok": False,
            "detail": "adb not authorized — accept the debugging prompt on the phone",
        },
    )
    readiness = browser_control.phone_readiness(local_port=18223)
    assert readiness["enabled"] is False
    assert readiness["visible_guard"] == "unauthorized"
    assert "accept the debugging prompt" in readiness["reason"]


def test_browser_act_desktop_parity_and_phone_prehooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hooks: list[str] = []
    monkeypatch.setattr(
        browser_control,
        "ensure_browser_window_restored",
        lambda **_k: hooks.append("desktop-restore"),
    )
    monkeypatch.setattr(
        browser_control,
        "ensure_phone_chrome_ready",
        lambda **_k: hooks.append("phone-ready"),
    )

    argvs: list[list[str]] = []

    def fake_runner(argv, **_kwargs):
        argvs.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    default = browser_control.browser_act("click", ref="e2", port=18222, runner=fake_runner)
    explicit = browser_control.browser_act(
        "click", ref="e2", port=18222, target="desktop", runner=fake_runner
    )
    assert default.ok and explicit.ok
    assert argvs[0] == argvs[1]  # desktop parity: target kwarg changes nothing
    assert hooks == ["desktop-restore", "desktop-restore"]

    hooks.clear()
    phone = browser_control.browser_act(
        "click", ref="e2", port=18223, target="phone", runner=fake_runner
    )
    assert phone.ok
    assert hooks == ["phone-ready"]  # adb pre-hook, never the desktop restore
    assert "18223" in argvs[-1]


def test_phone_screenshot_and_snapshot_heal_the_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    healed: list[int] = []
    monkeypatch.setattr(
        browser_control,
        "_ensure_phone_transport",
        lambda port, **_k: healed.append(port),
    )

    def screenshot_runner(argv, **_kwargs):
        Path(argv[-1]).write_bytes(b"\x89PNG\r\n\x1a\nphone")
        return SimpleNamespace(returncode=0, stdout="saved", stderr="")

    data = browser_control.capture_browser_screenshot_png(
        port=18223, target="phone", runner=screenshot_runner
    )
    assert data.startswith(b"\x89PNG")
    assert healed == [18223]

    def snapshot_runner(argv, **_kwargs):
        return SimpleNamespace(returncode=0, stdout='- button "Go" [ref=e1]\n', stderr="")

    elements = browser_control.browser_snapshot_elements(
        port=18223, target="phone", runner=snapshot_runner
    )
    assert elements == [{"ref": "e1", "role": "button", "name": "Go"}]
    assert healed == [18223, 18223]


def test_phone_read_prehook_wakes_but_never_foregrounds_chrome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # E2E-decided read policy (2026-07-06): reads wake the screen (a dozing
    # phone freezes the renderer) but must NEVER hijack the foreground app.
    calls: list[str] = []
    monkeypatch.setattr(
        browser_control.adb_control,
        "ensure_forward",
        lambda _port, **_k: calls.append("forward") or True,
    )
    monkeypatch.setattr(
        browser_control.adb_control,
        "wake_screen",
        lambda **_k: calls.append("wake") or True,
    )
    monkeypatch.setattr(
        browser_control.adb_control,
        "dismiss_keyguard",
        lambda **_k: calls.append("dismiss") or True,
    )
    monkeypatch.setattr(
        browser_control.adb_control,
        "chrome_to_foreground",
        lambda **_k: pytest.fail("read path must never am-start Chrome"),
    )
    assert browser_control._ensure_phone_transport(18223)
    assert calls == ["forward", "wake", "dismiss"]


def test_ensure_phone_chrome_ready_full_recovery_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Spike-decided policy (2026-07-06): acts REQUIRE Chrome foregrounded —
    # forward heal -> wake -> dismiss keyguard -> am start, in that order.
    calls: list[str] = []
    monkeypatch.setattr(
        browser_control.adb_control,
        "ensure_forward",
        lambda _port, **_k: calls.append("forward") or True,
    )
    monkeypatch.setattr(
        browser_control.adb_control,
        "wake_screen",
        lambda **_k: calls.append("wake") or True,
    )
    monkeypatch.setattr(
        browser_control.adb_control,
        "dismiss_keyguard",
        lambda **_k: calls.append("dismiss") or True,
    )
    monkeypatch.setattr(
        browser_control.adb_control,
        "chrome_to_foreground",
        lambda **_k: calls.append("am-start") or True,
    )
    assert browser_control.ensure_phone_chrome_ready(local_port=18223)
    assert calls == ["forward", "wake", "dismiss", "am-start"]


def test_tree_kill_run_reaps_the_process_tree_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Live-E2E regression (2026-07-06): the .CMD wrapper dies at timeout but
    # the node child survives holding the pipes — communicate() blocked past
    # the deadline. The runner must taskkill /T the tree, then re-raise.
    import subprocess as sp

    kills: list[list[str]] = []

    class FakeProc:
        pid = 4242
        returncode = None
        calls = 0

        def communicate(self, timeout=None):
            FakeProc.calls += 1
            if FakeProc.calls == 1:
                raise sp.TimeoutExpired(["agent-browser"], timeout)
            return ("", "")

    monkeypatch.setattr(browser_control.platform, "system", lambda: "Windows")
    monkeypatch.setattr(browser_control.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(
        browser_control.subprocess,
        "run",
        lambda argv, **k: kills.append(list(argv)) or SimpleNamespace(returncode=0),
    )

    with pytest.raises(sp.TimeoutExpired):
        browser_control._tree_kill_run(["agent-browser", "open", "x"], timeout=20)

    assert kills == [["taskkill", "/T", "/F", "/PID", "4242"]]
    assert FakeProc.calls == 2  # post-kill drain ran


def test_adb_transport_guard_connect_timeout_maps_no_device() -> None:
    # Adversarial-review finding 1: `adb connect` to an off/asleep phone
    # blocks past the subprocess timeout — must map to the readiness table,
    # never escape as an exception (which became an unaudited 500).
    import subprocess as sp

    def runner(argv, **_kwargs):
        joined = " ".join(argv)
        if "devices -l" in joined:
            return SimpleNamespace(returncode=0, stdout="List of devices attached\n", stderr="")
        if "connect" in joined:
            raise sp.TimeoutExpired(argv, 15)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    guard = adb_control.adb_transport_guard(18223, runner=runner, environ=_ADB_ENV)
    assert guard["status"] == "no_device"
    assert guard["ok"] is False
    assert guard["detail"] == (
        "phone unreachable over adb — connect to 100.114.68.10:5555 failed"
    )


def test_default_runner_is_tree_kill_for_session_plain_for_desktop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Adversarial-review findings 2+3: tree-kill must ride EVERY phone
    # (isolated-session) command and must NOT touch bare desktop callers —
    # desktop timeout semantics are frozen M12 behavior (social writes rely
    # on the node child surviving to finish an in-flight action).
    used: list[str] = []

    def fake_tree_kill(argv, **_k):
        used.append("tree-kill")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_plain(argv, **_k):
        used.append("plain")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(browser_control, "_tree_kill_run", fake_tree_kill)
    monkeypatch.setattr(browser_control.subprocess, "run", fake_plain)

    browser_control.run_agent_browser(["snapshot"], port=18223, session="homie-phone")
    browser_control.run_agent_browser(["snapshot"], port=18222)
    assert used == ["tree-kill", "plain"]


def test_phone_helpers_reach_tree_kill_without_explicit_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The helpers must not eagerly bind subprocess.run and defeat the
    # session-based runner selection (the finding-2 shape).
    used: list[str] = []

    def fake_tree_kill(argv, **_k):
        used.append("tree-kill")
        return SimpleNamespace(returncode=0, stdout='- button "Go" [ref=e1]\n', stderr="")

    monkeypatch.setattr(browser_control, "_tree_kill_run", fake_tree_kill)
    monkeypatch.setattr(browser_control, "_ensure_phone_transport", lambda *a, **k: True)

    elements = browser_control.browser_snapshot_elements(port=18223, target="phone")
    assert elements == [{"ref": "e1", "role": "button", "name": "Go"}]
    assert used == ["tree-kill"]


def test_phone_stream_mutations_heal_the_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Adversarial-review finding 6: every phone action self-heals the forward.
    healed: list[int] = []
    monkeypatch.setattr(
        browser_control,
        "_ensure_phone_transport",
        lambda port, **_k: healed.append(port) or True,
    )

    def fake_run(args, *, port, session=None, **_k):
        return browser_control.CommandResult(
            ok=True,
            returncode=0,
            stdout=json.dumps(
                {
                    "success": True,
                    "data": {"enabled": True, "connected": True, "port": 31137, "screencasting": False},
                    "error": None,
                }
            ),
            stderr="",
            command_label="x",
        )

    monkeypatch.setattr(browser_control, "run_agent_browser", fake_run)

    browser_control.browser_stream_enable(port=18223, target="phone")
    browser_control.browser_stream_disable(port=18223, target="phone")
    assert healed == [18223, 18223]

    browser_control.browser_stream_enable(port=18222)  # desktop: no adb heal
    assert healed == [18223, 18223]


def test_phone_commands_use_isolated_session_desktop_does_not() -> None:
    # Spike evidence: a freeze event permanently wedges the DEFAULT daemon
    # session; phone commands ride an isolated --session so recovery is clean.
    recorded: list[list[str]] = []

    def fake_runner(argv, **_kwargs):
        recorded.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    browser_control.run_agent_browser(
        ["snapshot"], port=18223, session="homie-phone", runner=fake_runner
    )
    assert "--session" in recorded[0]
    assert "homie-phone" in recorded[0]
    # Session flags sit BEFORE the subcommand (global flag position).
    assert recorded[0].index("--session") < recorded[0].index("snapshot")

    browser_control.run_agent_browser(["snapshot"], port=18222, runner=fake_runner)
    assert "--session" not in recorded[1]  # desktop default byte-identical


def test_browser_viewer_status_echoes_target(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_readiness(*, port=None, target="desktop"):
        seen.append(target)
        return {
            "status": "ready",
            "cdp_port": 18223 if target == "phone" else 18222,
            "cdp_reachable": True,
            "browser": "Chrome/143",
            "visible_guard": "device" if target == "phone" else "visible",
            "tab_count": 1,
            "reason": "ready",
        }

    monkeypatch.setattr(browser_control, "browser_readiness", fake_readiness)
    monkeypatch.setattr(
        browser_control,
        "browser_stream_status",
        lambda *, port, target="desktop": {
            "enabled": False,
            "connected": False,
            "port": None,
            "screencasting": False,
            "reason": "ready",
        },
    )

    default = browser_control.browser_viewer_status()
    phone = browser_control.browser_viewer_status(target="phone")

    assert default["target"] == "desktop"
    assert phone["target"] == "phone"
    assert seen == ["desktop", "phone"]
    assert phone["readiness"]["visible_guard"] == "device"


# ── P4.0 Ghost — the third browser target (desktop | phone | ghost) ──────────


def test_ghost_in_browser_targets_and_port_18224() -> None:
    assert browser_control.BROWSER_TARGETS == ("desktop", "phone", "ghost")
    env = {"HOMIE_BROWSER_CDP_PORT": "18222"}
    assert browser_control.resolve_target_port("ghost", environ=env) == 18224
    assert (
        browser_control.resolve_target_port("ghost", environ={"HOMIE_GHOST_CDP_PORT": "18900"})
        == 18900
    )


def test_ghost_target_registry_shape() -> None:
    ghost = browser_control.resolve_target("ghost", environ={})
    assert ghost == {"target": "ghost", "transport": "adb", "port": 18224}


def test_ghost_uses_isolated_homie_ghost_session() -> None:
    assert browser_control.session_for_target("ghost") == "homie-ghost"
    assert browser_control.session_for_target("phone") == "homie-phone"
    assert browser_control.session_for_target("desktop") is None


def test_is_adb_target_covers_phone_and_ghost_only() -> None:
    assert browser_control.is_adb_target("phone")
    assert browser_control.is_adb_target("ghost")
    assert not browser_control.is_adb_target("desktop")
    assert not browser_control.is_adb_target(None)


def test_resolve_target_serial_is_per_target() -> None:
    env = {
        "HOMIE_PHONE_ADB_SERIAL": "192.168.0.174:5555",
        "HOMIE_GHOST_ADB_SERIAL": "emulator-5554",
    }
    assert browser_control.resolve_target_serial("phone", environ=env) == "192.168.0.174:5555"
    assert browser_control.resolve_target_serial("ghost", environ=env) == "emulator-5554"
    # A non-adb target has no serial env; whitespace is treated as unset.
    assert browser_control.resolve_target_serial("desktop", environ=env) is None
    assert (
        browser_control.resolve_target_serial("ghost", environ={"HOMIE_GHOST_ADB_SERIAL": "  "})
        is None
    )


def test_resolve_adb_serial_or_raise_refuses_ghost_without_serial() -> None:
    # The safety line (landmine 4-adjacent): a ghost action never falls back to
    # the phone serial. Ghost with a serial resolves it; ghost without RAISES.
    assert (
        browser_control._resolve_adb_serial_or_raise(
            "ghost", environ={"HOMIE_GHOST_ADB_SERIAL": "emulator-5554"}
        )
        == "emulator-5554"
    )
    with pytest.raises(RuntimeError, match="HOMIE_GHOST_ADB_SERIAL"):
        browser_control._resolve_adb_serial_or_raise("ghost", environ={})
    # Phone keeps its single-device autodetect ONLY while no ghost is
    # configured: a None serial is legitimate for ghost-less deployments.
    assert browser_control._resolve_adb_serial_or_raise("phone", environ={}) is None


def test_resolve_adb_serial_or_raise_refuses_phone_autodetect_with_ghost_configured() -> None:
    """PhoneOps review F1 (issue #89): the symmetric guard. Once a ghost serial
    is configured, a phone action without HOMIE_PHONE_ADB_SERIAL must RAISE —
    with only the ghost attached, un-scoped adb under the phone label would
    silently drive the ghost."""

    with pytest.raises(RuntimeError, match="HOMIE_PHONE_ADB_SERIAL"):
        browser_control._resolve_adb_serial_or_raise(
            "phone", environ={"HOMIE_GHOST_ADB_SERIAL": "emulator-5554"}
        )
    # Both serials set: phone resolves its OWN serial, never the ghost's.
    assert (
        browser_control._resolve_adb_serial_or_raise(
            "phone",
            environ={
                "HOMIE_PHONE_ADB_SERIAL": "R5CX12ABCDE",
                "HOMIE_GHOST_ADB_SERIAL": "emulator-5554",
            },
        )
        == "R5CX12ABCDE"
    )


def test_phone_readiness_refuses_autodetect_when_ghost_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PhoneOps review F1 (issue #89), probe half: phone readiness must not
    autodetect onto the ghost — the guard probe (ensure_forward / wake_screen)
    is what the review caught driving the ghost under the phone label."""

    monkeypatch.delenv("HOMIE_PHONE_ADB_SERIAL", raising=False)
    monkeypatch.setenv("HOMIE_GHOST_ADB_SERIAL", "emulator-5554")
    monkeypatch.setattr(
        browser_control.adb_control,
        "adb_transport_guard",
        lambda *_a, **_k: pytest.fail(
            "phone readiness must not probe adb un-scoped while a ghost is configured"
        ),
    )
    readiness = browser_control.phone_readiness(local_port=18223)
    assert readiness["enabled"] is False
    assert readiness["status"] == "attention"
    assert readiness["target"] == "phone"
    assert readiness["transport"] == "adb"
    assert "HOMIE_PHONE_ADB_SERIAL" in readiness["reason"]


def test_phone_readiness_scopes_probe_to_configured_serial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With HOMIE_PHONE_ADB_SERIAL set, the readiness guard probe is scoped to
    that serial (issue #89's two-devices-attached variant: an un-scoped probe
    regresses to 'multiple devices' purely because the ghost is paired)."""

    _patch_desktop_readiness_deps(monkeypatch)
    monkeypatch.setenv("HOMIE_PHONE_ADB_SERIAL", "R5CX12ABCDE")
    monkeypatch.setenv("HOMIE_GHOST_ADB_SERIAL", "emulator-5554")
    seen_serial: list[str | None] = []

    def fake_guard(_port, *, serial=None, **_k):
        seen_serial.append(serial)
        return {"status": "device", "ok": True, "detail": "ready"}

    monkeypatch.setattr(browser_control.adb_control, "adb_transport_guard", fake_guard)
    readiness = browser_control.phone_readiness(local_port=18223)
    assert seen_serial == ["R5CX12ABCDE"]
    assert readiness["target"] == "phone"


def test_phone_readiness_autodetect_survives_ghostless_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ghost configured -> the documented single-device autodetect is
    byte-identical (serial=None reaches the guard, no refusal)."""

    _patch_desktop_readiness_deps(monkeypatch)
    monkeypatch.delenv("HOMIE_PHONE_ADB_SERIAL", raising=False)
    monkeypatch.delenv("HOMIE_GHOST_ADB_SERIAL", raising=False)
    seen_serial: list[str | None] = []

    def fake_guard(_port, *, serial=None, **_k):
        seen_serial.append(serial)
        return {"status": "device", "ok": True, "detail": "ready"}

    monkeypatch.setattr(browser_control.adb_control, "adb_transport_guard", fake_guard)
    readiness = browser_control.phone_readiness(local_port=18223)
    assert seen_serial == [None]
    assert readiness["status"] == "ready"


def test_ghost_readiness_attention_when_serial_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # No HOMIE_GHOST_ADB_SERIAL -> the ghost must NOT probe adb (a None serial
    # would autodetect / fall back to the phone). Reports attention, no adb call.
    monkeypatch.delenv("HOMIE_GHOST_ADB_SERIAL", raising=False)
    monkeypatch.setattr(
        browser_control.adb_control,
        "adb_transport_guard",
        lambda *_a, **_k: pytest.fail("ghost readiness must not touch adb without its own serial"),
    )
    readiness = browser_control.ghost_readiness(local_port=18224)
    assert readiness["enabled"] is False
    assert readiness["status"] == "attention"
    assert readiness["target"] == "ghost"
    assert readiness["transport"] == "adb"
    assert readiness["cdp_port"] == 18224
    assert "HOMIE_GHOST_ADB_SERIAL" in readiness["reason"]


def test_ghost_readiness_mirrors_envelope_and_threads_ghost_serial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_desktop_readiness_deps(monkeypatch)
    monkeypatch.setenv("HOMIE_GHOST_ADB_SERIAL", "emulator-5554")
    seen_serial: list[str | None] = []

    def fake_guard(_port, *, serial=None, **_k):
        seen_serial.append(serial)
        return {"status": "device", "ok": True, "detail": "ready"}

    monkeypatch.setattr(browser_control.adb_control, "adb_transport_guard", fake_guard)
    desktop = browser_control.browser_readiness()
    ghost = browser_control.ghost_readiness(local_port=18224)

    assert set(ghost.keys()) - set(desktop.keys()) == {"target", "transport"}
    assert ghost["target"] == "ghost"
    assert ghost["transport"] == "adb"
    assert ghost["status"] == "ready"
    assert ghost["cdp_port"] == 18224
    assert ghost["tab_count"] == 1
    # The ghost serial (never the phone's) reached the transport guard.
    assert seen_serial == ["emulator-5554"]


def test_ghost_readiness_maps_cdp_failures_to_ghost_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_desktop_readiness_deps(monkeypatch)
    monkeypatch.setenv("HOMIE_GHOST_ADB_SERIAL", "emulator-5554")
    monkeypatch.setattr(
        browser_control.adb_control,
        "adb_transport_guard",
        lambda _p, **_k: {"status": "device", "ok": True, "detail": "ready"},
    )
    monkeypatch.setattr(
        browser_control,
        "get_cdp_version",
        lambda _p, **_k: {"reachable": False, "error": "Connection refused"},
    )
    refused = browser_control.ghost_readiness(local_port=18224)
    assert refused["reason"] == (
        "ghost Chrome devtools socket unavailable — open Chrome on the ghost"
    )


def test_ghost_readiness_guard_failure_uses_ghost_noun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Adversarial-review finding: a down ghost must not report "phone" — the
    # adb guard's phone-worded strings get the device noun rewritten for ghost.
    _patch_desktop_readiness_deps(monkeypatch)
    monkeypatch.setenv("HOMIE_GHOST_ADB_SERIAL", "emulator-5554")
    monkeypatch.setattr(
        browser_control.adb_control,
        "adb_transport_guard",
        lambda _p, **_k: {
            "status": "no_device",
            "ok": False,
            "detail": "phone unreachable over adb — connect to emulator-5554 failed",
        },
    )
    readiness = browser_control.ghost_readiness(local_port=18224)
    assert readiness["status"] == "attention"
    assert "ghost unreachable over adb" in readiness["reason"]
    assert "phone" not in readiness["reason"]  # no phone-wording bleed for the ghost


def test_ghost_act_threads_ghost_serial_never_the_phone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A ghost act drives its OWN device — the ghost serial reaches the prehook,
    # never the phone's, even when both env vars are set.
    monkeypatch.setenv("HOMIE_GHOST_ADB_SERIAL", "emulator-5554")
    monkeypatch.setenv("HOMIE_PHONE_ADB_SERIAL", "192.168.0.174:5555")
    seen: dict[str, Any] = {}

    def fake_ready(*, local_port=None, serial=None, runner=None):
        seen["serial"] = serial
        return True

    monkeypatch.setattr(browser_control, "ensure_phone_chrome_ready", fake_ready)

    def fake_runner(argv, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = browser_control.browser_act(
        "click", ref="e2", port=18224, target="ghost", runner=fake_runner
    )
    assert result.ok
    assert seen["serial"] == "emulator-5554"


def test_ghost_act_raises_without_serial_never_touches_the_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With no ghost serial, the act RAISES before any prehook / agent-browser
    # shell — it must never fall back to the phone.
    monkeypatch.delenv("HOMIE_GHOST_ADB_SERIAL", raising=False)
    monkeypatch.setenv("HOMIE_PHONE_ADB_SERIAL", "192.168.0.174:5555")
    monkeypatch.setattr(
        browser_control,
        "ensure_phone_chrome_ready",
        lambda **_k: pytest.fail("ghost act must raise before the adb prehook when serial unset"),
    )

    def fake_runner(argv, **_kwargs):
        pytest.fail("ghost act must not shell agent-browser without a ghost serial")

    with pytest.raises(RuntimeError, match="HOMIE_GHOST_ADB_SERIAL"):
        browser_control.browser_act(
            "click", ref="e2", port=18224, target="ghost", runner=fake_runner
        )


def test_ghost_screenshot_heals_forward_with_ghost_serial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOMIE_GHOST_ADB_SERIAL", "emulator-5554")
    seen: dict[str, Any] = {}

    def fake_transport(port, *, serial=None, runner=None):
        seen.update(port=port, serial=serial)
        return True

    monkeypatch.setattr(browser_control, "_ensure_phone_transport", fake_transport)

    def screenshot_runner(argv, **_kwargs):
        Path(argv[-1]).write_bytes(b"\x89PNG\r\n\x1a\nghost")
        return SimpleNamespace(returncode=0, stdout="saved", stderr="")

    data = browser_control.capture_browser_screenshot_png(
        port=18224, target="ghost", runner=screenshot_runner
    )
    assert data.startswith(b"\x89PNG")
    assert seen == {"port": 18224, "serial": "emulator-5554"}


def test_ghost_commands_use_isolated_homie_ghost_session() -> None:
    recorded: list[list[str]] = []

    def fake_runner(argv, **_kwargs):
        recorded.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    browser_control.run_agent_browser(
        ["snapshot"], port=18224, session="homie-ghost", runner=fake_runner
    )
    assert "--session" in recorded[0]
    assert "homie-ghost" in recorded[0]
    assert recorded[0].index("--session") < recorded[0].index("snapshot")


def test_get_ghost_settings_default_off_and_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import config  # type: ignore[import-not-found]

    monkeypatch.delenv("HOMIE_GHOST_ENABLED", raising=False)
    assert config.get_ghost_settings().enabled is False
    monkeypatch.setenv("HOMIE_GHOST_ENABLED", "true")
    assert config.get_ghost_settings().enabled is True  # Rule 1: no reload needed
    assert config.get_ghost_settings(enabled=False).enabled is False
