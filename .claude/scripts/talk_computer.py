"""Computer use for Talk mode — desktop, windows, keyboard, screen.

Three transports, picked per action:

1. **Desktop daemon** (open terminals, URLs, files, shell commands, toasts).
   A file-queue daemon owns the desktop: append one JSON line to
   ``commands.jsonl`` and it acts within ~500ms. No port, no socket. The
   daemon is auto-started if it is not running.
2. **In-process GUI control** (find a window, type into it, press keys,
   click). ``pyautogui`` + ``pygetwindow``, imported lazily so a headless
   box can still import this module. Sub-second, so it runs inline on the
   tool threadpool.
3. **Screenshot + vision** for "what's on my screen" — captured here,
   described by a vision model in an async run (see ``talk_tools``).

Focus discipline: typing into a window steals focus for ~1-2 seconds and
gives it back. The voice model is instructed to announce before doing it.
``pyautogui.FAILSAFE`` stays ON — slamming the mouse into a screen corner
aborts any in-flight automation.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

_log = logging.getLogger(__name__)

_DEFAULT_LOOK_TIMEOUT_S = 120
_DEFAULT_LOOK_BIN = "codex"
_MAX_SCREENSHOTS = 20
_TYPE_INTERVAL_S = 0.02
_FOCUS_SETTLE_S = 0.3
_MAX_WINDOW_CANDIDATES = 8


class ComputerError(Exception):
    """A computer-use action could not be performed (spoken to the operator)."""


def get_computer_settings() -> dict:
    """Resolve computer-use knobs at call time (Rule 1 — never at import)."""

    queue = (os.environ.get("TALK_DESKTOP_QUEUE") or "").strip()
    queue_path = Path(queue) if queue else Path.home() / ".claude" / "live-chat" / "commands.jsonl"
    try:
        look_timeout = int(os.environ.get("TALK_LOOK_TIMEOUT_S") or _DEFAULT_LOOK_TIMEOUT_S)
    except ValueError:
        look_timeout = _DEFAULT_LOOK_TIMEOUT_S
    return {
        "queue_path": queue_path,
        "look_timeout_s": look_timeout,
        "look_bin": (os.environ.get("TALK_LOOK_BIN") or _DEFAULT_LOOK_BIN).strip() or _DEFAULT_LOOK_BIN,
    }


# -- desktop daemon (file queue) ----------------------------------------------


def _agent_paths(queue_path: Path) -> tuple[Path, Path]:
    """(daemon script, pid file) derived from the queue's directory."""

    chat_dir = queue_path.parent
    return chat_dir / "desktop_agent.py", chat_dir / "state" / "desktop-agent.pid"


def _pid_alive(pid: int) -> bool:
    """Physical liveness check (Rule 2) — never trust a stale pid file."""

    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return False
        return True
    import ctypes  # noqa: PLC0415 — win32 only

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def desktop_agent_running(queue_path: Path) -> bool:
    _, pid_file = _agent_paths(queue_path)
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return _pid_alive(pid)


def ensure_desktop_agent(queue_path: Path) -> bool:
    """Start the desktop daemon if it is not already running. True if usable."""

    if desktop_agent_running(queue_path):
        return True
    script, _ = _agent_paths(queue_path)
    if not script.exists():
        raise ComputerError(
            f"the desktop agent isn't installed at {script.parent} — "
            "computer actions that need a terminal or toast can't run"
        )
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    launcher = str(pythonw if pythonw.exists() else sys.executable)
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([launcher, str(script)], **kwargs)  # noqa: S603 — operator's own daemon
    except OSError as exc:
        raise ComputerError(f"could not start the desktop agent: {exc}") from exc
    time.sleep(1.0)  # the daemon seeks to EOF on boot; queue after it is listening
    return True


def queue_desktop_command(queue_path: Path, payload: dict) -> None:
    """Append one JSON line in the daemon's wire shape."""

    line = dict(payload)
    line["ts"] = datetime.now().isoformat()
    try:
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        with open(queue_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(line) + "\n")
    except OSError as exc:
        raise ComputerError(f"could not reach the desktop queue: {exc}") from exc


# -- window / keyboard / mouse ------------------------------------------------


def _ensure_gui():
    """Lazy-import the GUI stack; a missing dep is a spoken install hint."""

    try:
        import pyautogui  # noqa: PLC0415
        import pygetwindow  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 — ImportError, but also X11/display errors
        raise ComputerError(
            "screen control isn't available in this environment "
            f"({type(exc).__name__}: {exc}) — install pyautogui and pygetwindow"
        ) from exc
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05
    return pyautogui, pygetwindow


def find_window(title_substring: str):
    """Find one window by case-insensitive title substring.

    A miss raises with the candidate titles so the model can self-correct on
    the next turn (same affordance as the skill resolver's close matches).
    """

    _, gw = _ensure_gui()
    wanted = (title_substring or "").strip().lower()
    if not wanted:
        raise ComputerError("which window? give me part of its title")
    titled = [w for w in gw.getAllWindows() if (getattr(w, "title", "") or "").strip()]
    for window in titled:
        if wanted in window.title.lower():
            return window
    candidates = [w.title for w in titled][:_MAX_WINDOW_CANDIDATES]
    hint = f" Open windows include: {'; '.join(candidates)}." if candidates else ""
    raise ComputerError(f"no window matching '{title_substring}'.{hint}")


def _activate(window) -> None:
    try:
        if getattr(window, "isMinimized", False):
            window.restore()
        window.activate()
    except Exception as exc:  # noqa: BLE001 — win32 activation is flaky under focus locks
        raise ComputerError(f"could not focus '{window.title}': {exc}") from exc
    time.sleep(_FOCUS_SETTLE_S)


def type_into_window(title_substring: str, text: str, press_enter: bool = True) -> str:
    """Focus a window, type, optionally Enter, then restore the prior focus."""

    if not text:
        raise ComputerError("nothing to type")
    pyautogui, gw = _ensure_gui()
    window = find_window(title_substring)
    try:
        previous = gw.getActiveWindow()
    except Exception:  # noqa: BLE001 — no active window is fine
        previous = None
    _activate(window)
    pyautogui.typewrite(text, interval=_TYPE_INTERVAL_S)
    if press_enter:
        pyautogui.press("enter")
    time.sleep(_FOCUS_SETTLE_S)
    if previous is not None and previous is not window:
        try:
            previous.activate()
        except Exception:  # noqa: BLE001 — best-effort restore, never fail the action
            pass
    return f"Typed {len(text)} characters into '{window.title}'."


def press_keys(keys: str, title_substring: str | None = None) -> str:
    """Press a key or a combo ('enter', 'ctrl+c'), optionally into a window."""

    pyautogui, _ = _ensure_gui()
    combo = (keys or "").strip().lower()
    if not combo:
        raise ComputerError("which keys?")
    target = ""
    if title_substring:
        window = find_window(title_substring)
        _activate(window)
        target = f" in '{window.title}'"
    parts = [part.strip() for part in combo.split("+") if part.strip()]
    if len(parts) > 1:
        pyautogui.hotkey(*parts)
    else:
        pyautogui.press(parts[0])
    return f"Pressed {combo}{target}."


def click(x: int | None = None, y: int | None = None, title_substring: str | None = None) -> str:
    """Click screen coordinates, or the center of a titled window."""

    pyautogui, _ = _ensure_gui()
    if title_substring:
        window = find_window(title_substring)
        _activate(window)
        cx = window.left + window.width // 2
        cy = window.top + window.height // 2
        pyautogui.click(cx, cy)
        return f"Clicked the middle of '{window.title}'."
    if x is None or y is None:
        raise ComputerError("click needs either screen coordinates or a window title")
    pyautogui.click(int(x), int(y))
    return f"Clicked at {int(x)}, {int(y)}."


# -- screen capture -----------------------------------------------------------


def _screens_dir() -> Path:
    import config  # noqa: PLC0415 — lazy: config binds paths at import

    return Path(config.DATA_DIR) / "talk" / "screens"


def _prune_screenshots(directory: Path) -> None:
    try:
        shots = sorted(directory.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for stale in shots[_MAX_SCREENSHOTS:]:
        try:
            stale.unlink()
        except OSError:
            pass


def capture_screen() -> Path:
    """Grab the screen to a pruned scratch directory; returns the PNG path."""

    pyautogui, _ = _ensure_gui()
    directory = _screens_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{time.strftime('%Y%m%d-%H%M%S')}.png"
    try:
        pyautogui.screenshot(str(path))
    except Exception as exc:  # noqa: BLE001 — locked session / no display
        raise ComputerError(f"could not capture the screen: {exc}") from exc
    _prune_screenshots(directory)
    return path


LOOK_PROMPT = (
    "Describe what is on this screen for someone listening, not reading: the "
    "active window, what it shows, and any errors or dialogs that matter. "
    "Three to six spoken sentences. No markdown, no lists."
)


def describe_screen(png_path: Path) -> str:
    """Run the vision model over a screenshot and return spoken-style text."""

    import shutil  # noqa: PLC0415

    settings = get_computer_settings()
    # On Windows the CLI is an npm .cmd shim: a bare name never resolves through
    # subprocess without PATHEXT expansion, so resolve to a real path first.
    resolved = shutil.which(settings["look_bin"])
    if not resolved:
        raise ComputerError(f"the vision CLI '{settings['look_bin']}' isn't on PATH")
    # `--image <FILE>...` is greedy: a prompt placed after it is swallowed as a
    # second image path and the CLI exits "No prompt provided". Prompt first.
    argv = [
        resolved,
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        LOOK_PROMPT,
        "--image",
        str(png_path),
    ]
    try:
        completed = subprocess.run(  # noqa: S603 — operator's own local CLI
            argv,
            capture_output=True,
            text=True,
            # The model writes UTF-8 (smart quotes, em dashes); Windows would
            # otherwise decode as cp1252 and blow up mid-description.
            encoding="utf-8",
            errors="replace",
            timeout=settings["look_timeout_s"],
        )
    except FileNotFoundError as exc:
        raise ComputerError(
            f"the vision CLI '{settings['look_bin']}' could not be launched: {exc}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ComputerError(
            f"looking at the screen took longer than {settings['look_timeout_s']} seconds"
        ) from exc
    text = clean_for_speech(completed.stdout or "")
    if completed.returncode != 0 and not text:
        err = (completed.stderr or "").strip()[-300:]
        raise ComputerError(f"the vision model failed (exit {completed.returncode}): {err}")
    return text or "(the vision model returned nothing)"


def clean_for_speech(text: str) -> str:
    """Strip console mangling so the result reads cleanly when spoken.

    The CLI emits typographic punctuation that survives the Windows console as
    undecodable bytes; they land as U+FFFD and would be read aloud as noise.
    """

    cleaned = text.replace("�", "")
    for fancy, plain in (("“", '"'), ("”", '"'), ("‘", "'"), ("’", "'")):
        cleaned = cleaned.replace(fancy, plain)
    return " ".join(cleaned.split())


__all__ = [
    "ComputerError",
    "LOOK_PROMPT",
    "capture_screen",
    "click",
    "describe_screen",
    "desktop_agent_running",
    "ensure_desktop_agent",
    "find_window",
    "get_computer_settings",
    "press_keys",
    "queue_desktop_command",
    "type_into_window",
]
