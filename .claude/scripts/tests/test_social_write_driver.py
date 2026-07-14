"""Focused tests for the visible-Chrome social write driver."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import social_write_driver
from browser_control import CommandResult


def _result(*, ok: bool, output: str = "") -> CommandResult:
    return CommandResult(
        ok=ok,
        returncode=0 if ok else 1,
        stdout=output if ok else "",
        stderr="" if ok else output,
        command_label="agent-browser test",
    )


def test_linkedin_post_opens_feed_in_fresh_tab(monkeypatch):
    calls: list[tuple[list[str], int, int]] = []

    def fake_run(args: list[str], *, port: int, timeout: int):
        calls.append((args, port, timeout))
        if args[:2] == ["tab", "new"]:
            return _result(ok=True, output=args[2])
        return _result(ok=False, output="stop after tab assertion")

    monkeypatch.setattr(social_write_driver, "run_agent_browser", fake_run)
    task = SimpleNamespace(payload_text="A real post", target_url="")

    ok, detail = social_write_driver.AgentBrowserSocialWriteDriver()._drive_post(
        task, port=18222
    )

    assert ok is False
    assert "stop after tab assertion" in detail
    assert calls[0] == (
        ["tab", "new", "https://www.linkedin.com/feed/"],
        18222,
        20,
    )
    assert ["open", "https://www.linkedin.com/feed/"] not in [call[0] for call in calls]


def test_linkedin_post_stops_when_fresh_tab_fails(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, port: int, timeout: int):
        calls.append(args)
        return _result(ok=False, output="new tab unavailable")

    monkeypatch.setattr(social_write_driver, "run_agent_browser", fake_run)
    task = SimpleNamespace(
        payload_text="A real post",
        target_url="https://www.linkedin.com/feed/",
    )

    ok, detail = social_write_driver.AgentBrowserSocialWriteDriver()._drive_post(
        task, port=18222
    )

    assert ok is False
    assert detail == "fresh tab failed: new tab unavailable"
    assert calls == [["tab", "new", "https://www.linkedin.com/feed/"]]


def test_linkedin_post_accepts_link_trigger_and_attaches_reviewed_media(
    monkeypatch, tmp_path
):
    media = tmp_path / "approved.png"
    media.write_bytes(b"png")
    calls: list[list[str]] = []
    dialog_cleanup_calls: list[bool] = []
    snapshots = iter(
        [
            'link "Start a post" [ref=e10]',
            'textbox "Text editor for creating content" [ref=e11]',
            'button "Add media" [ref=e12]',
            'button "Upload from computer" [ref=e13]',
            'button "Next" [ref=e14]',
            'button "Edit media preview" [ref=e15]\nbutton "Post" [ref=e16]',
        ]
    )

    def fake_run(args: list[str], *, port: int, timeout: int):
        calls.append(args)
        if args[0] == "snapshot":
            return _result(ok=True, output=next(snapshots))
        if args[0] == "get":
            return _result(ok=True, output="A real post")
        if args[0] == "eval":
            script = args[1]
            if "ED_OK" in script:
                return _result(ok=True, output="ED_OK")
            if "NO_POST_BTN" in script:
                return _result(ok=True, output="CLICKED")
            return _result(ok=True, output="POSTED")
        return _result(ok=True, output="Done")

    monkeypatch.setattr(social_write_driver, "run_agent_browser", fake_run)
    monkeypatch.setattr(
        social_write_driver,
        "_dismiss_windows_chrome_file_dialog",
        lambda: dialog_cleanup_calls.append(True),
    )
    task = SimpleNamespace(
        payload_text="A real post",
        target_url="",
        media_path=str(media),
    )

    ok, detail = social_write_driver.AgentBrowserSocialWriteDriver()._drive_post(
        task, port=18222
    )

    assert ok is True
    assert detail == "post submitted and confirmed"
    assert ["click", "e10"] in calls
    assert ["click", "e12"] in calls
    assert ["upload", "e13", str(media.resolve())] in calls
    assert ["click", "e14"] in calls
    assert dialog_cleanup_calls == [True]


def test_drive_routes_x_workflow_to_x_driver(monkeypatch):
    calls: list[tuple[object, int]] = []

    def fake_x(task, *, port: int):
        calls.append((task, port))
        return True, "x ok"

    driver = social_write_driver.AgentBrowserSocialWriteDriver()
    monkeypatch.setattr(driver, "_drive_x_post", fake_x)
    task = SimpleNamespace(
        workflow_id="x.post.create",
        action="post",
        payload_text="crypto plus AI",
    )

    ok, detail = driver.drive(task, port=18222)

    assert ok is True
    assert detail == "x ok"
    assert calls == [(task, 18222)]


def test_x_post_uses_live_composer_refs_and_confirms(monkeypatch):
    calls: list[list[str]] = []
    snapshots = iter(
        [
            'textbox "Post text" [ref=e45]\nbutton "Post" [disabled, ref=e20]',
            'textbox "Post text" [ref=e45]: crypto plus AI\nbutton "Post" [ref=e20]',
        ]
    )

    def fake_run(
        args: list[str], *, port: int, timeout: int, session: str | None = None
    ):
        calls.append(args)
        assert session == "primo-x"
        if args[0] == "snapshot":
            return _result(ok=True, output=next(snapshots))
        if args[:2] == ["get", "text"]:
            return _result(ok=True, output="crypto plus AI")
        if args[0] == "eval":
            return _result(ok=True, output="POSTED")
        return _result(ok=True, output="Done")

    monkeypatch.setattr(social_write_driver, "run_agent_browser", fake_run)
    task = SimpleNamespace(
        workflow_id="x.post.create",
        action="post",
        payload_text="crypto plus AI",
        target_url="",
        media_path=None,
    )

    ok, detail = social_write_driver.AgentBrowserSocialWriteDriver()._drive_x_post(
        task, port=18222
    )

    assert ok is True
    assert detail == "X post submitted and confirmed"
    assert calls[0] == ["tab", "new", "https://x.com/compose/post"]
    assert ["click", "e45"] in calls
    assert ["keyboard", "inserttext", "crypto plus AI"] in calls
    assert ["click", "e20"] in calls


def test_x_post_uploads_approved_image_before_submit(monkeypatch, tmp_path):
    media = tmp_path / "primo-approved.png"
    media.write_bytes(b"png")
    calls: list[list[str]] = []
    snapshots = iter(
        [
            'textbox "Post text" [ref=e45]\nbutton "Post" [disabled, ref=e20]',
            'textbox "Post text" [ref=e45]\nbutton "Choose Files" [ref=e42]',
            'textbox "Post text" [ref=e45]: crypto plus AI\nbutton "Post" [ref=e20]',
        ]
    )

    def fake_run(
        args: list[str], *, port: int, timeout: int, session: str | None = None
    ):
        calls.append(args)
        assert session == "primo-x"
        if args[0] == "snapshot":
            return _result(ok=True, output=next(snapshots))
        if args[:2] == ["get", "text"]:
            return _result(ok=True, output="crypto plus AI")
        if args[0] == "eval":
            return _result(ok=True, output="POSTED")
        return _result(ok=True, output="Done")

    monkeypatch.setattr(social_write_driver, "run_agent_browser", fake_run)
    monkeypatch.setattr(
        social_write_driver, "_dismiss_windows_chrome_file_dialog", lambda: None
    )
    task = SimpleNamespace(
        workflow_id="x.post.create",
        action="post",
        payload_text="crypto plus AI",
        target_url="",
        media_path=str(media),
    )

    ok, detail = social_write_driver.AgentBrowserSocialWriteDriver()._drive_x_post(
        task, port=18222
    )

    assert ok is True
    assert detail == "X post submitted and confirmed"
    assert ["upload", "e42", str(media.resolve())] in calls
    assert calls.index(["upload", "e42", str(media.resolve())]) < calls.index(
        ["click", "e20"]
    )


def test_x_post_refuses_over_limit_without_browser_calls(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(
        args: list[str], *, port: int, timeout: int, session: str | None = None
    ):
        calls.append(args)
        return _result(ok=True, output="Done")

    monkeypatch.setattr(social_write_driver, "run_agent_browser", fake_run)
    task = SimpleNamespace(payload_text="x" * 281, target_url="", media_path=None)

    ok, detail = social_write_driver.AgentBrowserSocialWriteDriver()._drive_x_post(
        task, port=18222
    )

    assert ok is False
    assert "exceeds 280" in detail
    assert calls == []


def test_x_post_retries_one_compose_timeout(monkeypatch):
    calls: list[list[str]] = []
    tab_attempts = 0
    snapshots = iter(
        [
            'textbox "Post text" [ref=e45]\nbutton "Post" [disabled, ref=e20]',
            'textbox "Post text" [ref=e45]: crypto plus AI\nbutton "Post" [ref=e20]',
        ]
    )

    def fake_run(
        args: list[str], *, port: int, timeout: int, session: str | None = None
    ):
        nonlocal tab_attempts
        calls.append(args)
        assert session == "primo-x"
        if args[:2] == ["tab", "new"]:
            tab_attempts += 1
            if tab_attempts == 1:
                raise subprocess.TimeoutExpired(args, timeout)
            return _result(ok=True, output=args[2])
        if args[0] == "snapshot":
            return _result(ok=True, output=next(snapshots))
        if args[:2] == ["get", "text"]:
            return _result(ok=True, output="crypto plus AI")
        if args[0] == "eval":
            return _result(ok=True, output="POSTED")
        return _result(ok=True, output="Done")

    monkeypatch.setattr(social_write_driver, "run_agent_browser", fake_run)
    task = SimpleNamespace(
        workflow_id="x.post.create",
        action="post",
        payload_text="crypto plus AI",
        target_url="",
        media_path=None,
    )

    ok, detail = social_write_driver.AgentBrowserSocialWriteDriver()._drive_x_post(
        task, port=18222
    )

    assert ok is True
    assert detail == "X post submitted and confirmed"
    assert tab_attempts == 2


def test_x_post_retries_slow_composer_snapshot(monkeypatch):
    calls: list[list[str]] = []
    snapshots = iter(
        [
            "(no interactive elements)",
            'textbox "Post text" [ref=e45]\nbutton "Post" [disabled, ref=e20]',
            'textbox "Post text" [ref=e45]: crypto plus AI\nbutton "Post" [ref=e20]',
        ]
    )

    def fake_run(
        args: list[str], *, port: int, timeout: int, session: str | None = None
    ):
        calls.append(args)
        assert session == "primo-x"
        if args[0] == "snapshot":
            return _result(ok=True, output=next(snapshots))
        if args[:2] == ["get", "text"]:
            return _result(ok=True, output="crypto plus AI")
        if args[0] == "eval":
            return _result(ok=True, output="POSTED")
        return _result(ok=True, output="Done")

    monkeypatch.setattr(social_write_driver, "run_agent_browser", fake_run)
    task = SimpleNamespace(
        workflow_id="x.post.create",
        action="post",
        payload_text="crypto plus AI",
        target_url="",
        media_path=None,
    )

    ok, detail = social_write_driver.AgentBrowserSocialWriteDriver()._drive_x_post(
        task, port=18222
    )

    assert ok is True
    assert detail == "X post submitted and confirmed"
    assert calls.count(["snapshot", "-i"]) == 3
    assert ["wait", "10000"] in calls
