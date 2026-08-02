"""Computer-use tests — desktop queue, window control, and screen looks.

The GUI stack is stubbed at module scope (``_ensure_gui`` returns fakes), so
nothing here moves a real mouse, steals focus, or shells out to a vision CLI.
The desktop queue is a temp file, never the operator's live one.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))

import talk_computer


class _FakeWindow:
    def __init__(self, title: str, left: int = 0, top: int = 0, width: int = 100, height: int = 50):
        self.title = title
        self.left, self.top, self.width, self.height = left, top, width, height
        self.isMinimized = False
        self.activated = 0

    def activate(self) -> None:
        self.activated += 1

    def restore(self) -> None:
        self.isMinimized = False


class _FakeAutoGui:
    FAILSAFE = True
    PAUSE = 0.0

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def typewrite(self, text: str, interval: float = 0.0) -> None:
        self.calls.append(("typewrite", text))

    def press(self, key: str) -> None:
        self.calls.append(("press", key))

    def hotkey(self, *keys: str) -> None:
        self.calls.append(("hotkey", keys))

    def click(self, x: int, y: int) -> None:
        self.calls.append(("click", x, y))

    def screenshot(self, path: str) -> None:
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")
        self.calls.append(("screenshot", path))


class _FakeGetWindow:
    def __init__(self, windows: list[_FakeWindow], active: _FakeWindow | None = None):
        self._windows = windows
        self._active = active

    def getAllWindows(self) -> list[_FakeWindow]:
        return self._windows

    def getActiveWindow(self) -> _FakeWindow | None:
        return self._active


@pytest.fixture
def gui(monkeypatch: pytest.MonkeyPatch):
    """Install a fake GUI stack; returns (pyautogui, windows-by-title)."""

    fake_gui = _FakeAutoGui()
    terminal = _FakeWindow("thehomie — Claude Code", left=10, top=20, width=200, height=100)
    editor = _FakeWindow("notes.md - Editor")
    previous = _FakeWindow("Chrome")
    fake_gw = _FakeGetWindow([terminal, editor, previous], active=previous)
    monkeypatch.setattr(talk_computer, "_ensure_gui", lambda: (fake_gui, fake_gw))
    monkeypatch.setattr(talk_computer.time, "sleep", lambda _s: None)
    return SimpleNamespace(gui=fake_gui, terminal=terminal, editor=editor, previous=previous)


# ─── settings ─────────────────────────────────────────────────────────────


def test_settings_resolve_at_call_time(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    queue = tmp_path / "commands.jsonl"
    monkeypatch.setenv("TALK_DESKTOP_QUEUE", str(queue))
    monkeypatch.setenv("TALK_LOOK_TIMEOUT_S", "45")

    settings = talk_computer.get_computer_settings()

    assert settings["queue_path"] == queue
    assert settings["look_timeout_s"] == 45


def test_settings_fall_back_when_the_timeout_is_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TALK_LOOK_TIMEOUT_S", "soon")

    assert talk_computer.get_computer_settings()["look_timeout_s"] == 120


# ─── desktop queue ────────────────────────────────────────────────────────


def test_queue_command_writes_one_daemon_shaped_line(tmp_path: Path) -> None:
    queue = tmp_path / "commands.jsonl"

    talk_computer.queue_desktop_command(queue, {"action": "run", "command": "npm test"})
    talk_computer.queue_desktop_command(queue, {"action": "notify", "message": "hi"})

    lines = queue.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["action"] == "run" and first["command"] == "npm test"
    assert first["ts"]  # the daemon's wire shape carries a timestamp


def test_queue_failure_is_a_speakable_error(tmp_path: Path) -> None:
    blocked = tmp_path / "file.txt"
    blocked.write_text("i am a file", encoding="utf-8")

    with pytest.raises(talk_computer.ComputerError, match="could not reach the desktop queue"):
        talk_computer.queue_desktop_command(blocked / "commands.jsonl", {"action": "notify"})


def test_agent_running_is_false_without_a_pid_file(tmp_path: Path) -> None:
    assert talk_computer.desktop_agent_running(tmp_path / "commands.jsonl") is False


def test_agent_running_is_false_for_a_dead_pid(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "desktop-agent.pid").write_text("999999", encoding="utf-8")

    assert talk_computer.desktop_agent_running(tmp_path / "commands.jsonl") is False


def test_agent_running_is_true_for_this_process(tmp_path: Path) -> None:
    import os

    state = tmp_path / "state"
    state.mkdir()
    (state / "desktop-agent.pid").write_text(str(os.getpid()), encoding="utf-8")

    assert talk_computer.desktop_agent_running(tmp_path / "commands.jsonl") is True


def test_ensure_agent_explains_a_missing_install(tmp_path: Path) -> None:
    with pytest.raises(talk_computer.ComputerError, match="desktop agent isn't installed"):
        talk_computer.ensure_desktop_agent(tmp_path / "commands.jsonl")


# ─── window control ───────────────────────────────────────────────────────


def test_find_window_matches_a_title_substring(gui) -> None:
    assert talk_computer.find_window("claude code").title == gui.terminal.title


def test_find_window_miss_lists_candidates_so_the_model_self_corrects(gui) -> None:
    with pytest.raises(talk_computer.ComputerError) as excinfo:
        talk_computer.find_window("photoshop")

    message = str(excinfo.value)
    assert "no window matching 'photoshop'" in message
    assert "Claude Code" in message  # the candidates are spoken back


def test_find_window_needs_a_title(gui) -> None:
    with pytest.raises(talk_computer.ComputerError, match="which window"):
        talk_computer.find_window("  ")


def test_type_into_window_focuses_types_enters_and_restores(gui) -> None:
    result = talk_computer.type_into_window("claude code", "run the tests")

    assert gui.terminal.activated == 1
    assert gui.gui.calls == [("typewrite", "run the tests"), ("press", "enter")]
    assert gui.previous.activated == 1  # focus handed back
    assert "Typed 13 characters" in result


def test_type_into_window_can_skip_enter(gui) -> None:
    talk_computer.type_into_window("claude code", "draft", press_enter=False)

    assert gui.gui.calls == [("typewrite", "draft")]


def test_type_into_window_needs_text(gui) -> None:
    with pytest.raises(talk_computer.ComputerError, match="nothing to type"):
        talk_computer.type_into_window("claude", "")


def test_press_keys_splits_a_combo(gui) -> None:
    talk_computer.press_keys("ctrl+c")

    assert gui.gui.calls == [("hotkey", ("ctrl", "c"))]


def test_press_keys_single_key_and_window_focus(gui) -> None:
    result = talk_computer.press_keys("enter", "claude code")

    assert gui.gui.calls == [("press", "enter")]
    assert gui.terminal.activated == 1
    assert "in 'thehomie — Claude Code'" in result


def test_click_uses_coordinates(gui) -> None:
    talk_computer.click(400, 300)

    assert gui.gui.calls == [("click", 400, 300)]


def test_click_targets_a_window_center(gui) -> None:
    talk_computer.click(None, None, "claude code")

    assert gui.gui.calls == [("click", 110, 70)]  # left+w/2, top+h/2


def test_click_without_a_target_is_a_speakable_error(gui) -> None:
    with pytest.raises(talk_computer.ComputerError, match="either screen coordinates"):
        talk_computer.click(None, None, None)


# ─── screen look ──────────────────────────────────────────────────────────


def test_capture_prunes_to_the_last_twenty(gui, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    screens = tmp_path / "screens"
    screens.mkdir()
    for index in range(25):
        (screens / f"old-{index:02d}.png").write_bytes(b"x")
    monkeypatch.setattr(talk_computer, "_screens_dir", lambda: screens)

    path = talk_computer.capture_screen()

    assert path.exists()
    assert len(list(screens.glob("*.png"))) <= talk_computer._MAX_SCREENSHOTS


@pytest.fixture
def resolved_cli(monkeypatch: pytest.MonkeyPatch):
    """The vision CLI resolves to a real path (an npm .cmd shim on Windows)."""

    import shutil

    monkeypatch.setattr(shutil, "which", lambda _name: "C:/npm/codex.cmd")


def test_describe_screen_returns_the_model_text(
    resolved_cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(argv, **kwargs):
        assert argv[1] == "exec"
        # A bare 'codex' never resolves through subprocess on Windows.
        assert argv[0] == "C:/npm/codex.cmd"
        # `--image` is greedy multi-value: the prompt MUST precede it or the
        # CLI swallows it as a second image path and exits "No prompt provided".
        assert argv.index(talk_computer.LOOK_PROMPT) < argv.index("--image")
        assert argv[-1] == "shot.png"
        return SimpleNamespace(returncode=0, stdout="A terminal with green tests.", stderr="")

    monkeypatch.setattr(talk_computer.subprocess, "run", fake_run)

    assert talk_computer.describe_screen(Path("shot.png")) == "A terminal with green tests."


def test_describe_screen_reports_an_unresolvable_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _name: None)

    with pytest.raises(talk_computer.ComputerError, match="isn't on PATH"):
        talk_computer.describe_screen(Path("shot.png"))


def test_describe_screen_reports_a_timeout(resolved_cli, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 120)

    monkeypatch.setattr(talk_computer.subprocess, "run", fake_run)

    with pytest.raises(talk_computer.ComputerError, match="longer than"):
        talk_computer.describe_screen(Path("shot.png"))


def test_describe_screen_reports_a_nonzero_exit(resolved_cli, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=2, stdout="", stderr="model unavailable")

    monkeypatch.setattr(talk_computer.subprocess, "run", fake_run)

    with pytest.raises(talk_computer.ComputerError, match="model unavailable"):
        talk_computer.describe_screen(Path("shot.png"))


def test_clean_for_speech_strips_console_mangling() -> None:
    mangled = 'The window titled �HomieProof,� shows  “done”.\n\nNo errors.'

    assert talk_computer.clean_for_speech(mangled) == (
        'The window titled HomieProof, shows "done". No errors.'
    )
