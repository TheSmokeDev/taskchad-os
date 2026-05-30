from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS.parent / "chat"))

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
        lambda *, port=None: {
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
        lambda *, port: {
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
        lambda *, port: {"cdp_port": port, "cdp_reachable": True},
    )
    monkeypatch.setattr(browser_control, "browser_status", lambda *, port: {"port": port})
    monkeypatch.setattr(browser_control, "format_browser_status", lambda status, **_kwargs: f"status:{status['port']}")
    monkeypatch.setattr(core_handlers, "_audit_browser_action", lambda **_kwargs: None)

    message = await core_handlers.handle_browser(None, None, "status")

    assert message == "status:9222"


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
        lambda *, port: {"cdp_port": port, "cdp_reachable": True},
    )
    monkeypatch.setattr(core_handlers, "_audit_browser_action", lambda **kwargs: audits.append(kwargs))

    message = await core_handlers.handle_browser(None, None, "open")

    assert "Browser workflow blocked" in message
    assert "browser.open" in message
    assert audits[0]["workflow_id"] == "browser.open"
    assert audits[0]["outcome"] == "blocked"


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
