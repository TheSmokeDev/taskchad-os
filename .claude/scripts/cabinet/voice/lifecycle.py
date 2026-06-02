"""Single-session Cabinet voice subprocess lifecycle.

Python owns Cabinet voice process state. Dashboard/Hono callers should only
reach this module through the orchestration HTTP API.
"""

from __future__ import annotations

import importlib.util
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import config
import shared
from personas import services as _persona_services
from runtime.subprocess_env import get_scrubbed_sdk_env
from security import redact as _redact_mod

from . import config as voice_config

_SCRIPTS_DIR = Path(__file__).resolve().parents[2]
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))

_redact = _redact_mod.redact


class VoiceLifecycleError(RuntimeError):
    """Base class for voice lifecycle failures."""


class VoiceSessionActive(VoiceLifecycleError):
    """A different live voice subprocess is already running."""

    def __init__(self, status: dict[str, Any]):
        self.status = status
        super().__init__("voice_session_active")


class VoiceSessionMismatch(VoiceLifecycleError):
    """The requested meeting does not match the active voice subprocess."""

    def __init__(self, status: dict[str, Any]):
        self.status = status
        super().__init__("voice_session_mismatch")


class VoiceStartFailed(VoiceLifecycleError):
    """Voice subprocess failed before the ready handshake."""

    def __init__(self, status: dict[str, Any], reason: str):
        self.status = status
        self.reason = reason
        super().__init__(reason)


def _state_path() -> Path:
    return Path(config.STATE_DIR) / "cabinet-voice-session.json"


def _lock_path() -> Path:
    # shared.file_lock appends ".lock"; this resolves to
    # <state>/cabinet-voice-session.lock on disk.
    return Path(config.STATE_DIR) / "cabinet-voice-session"


def _log_dir() -> Path:
    return _persona_services.get_log_dir() / "cabinet-voice"


def _start_timeout_s() -> float:
    raw = os.environ.get("CABINET_VOICE_START_TIMEOUT_S", "")
    if not raw:
        return 10.0
    try:
        return max(float(raw), 1.0)
    except ValueError:
        return 10.0


def _active_profile_root() -> Path:
    from personas import get_active_profile_name  # noqa: PLC0415
    from personas.core import get_default_paths  # noqa: PLC0415
    from personas.lifecycle import resolve_profile_root  # noqa: PLC0415

    active = get_active_profile_name()
    if active == "default":
        return get_default_paths()["memory"].parent.parent
    return resolve_profile_root(active)


def _capabilities() -> dict[str, bool]:
    try:
        import voice as voice_module  # noqa: PLC0415
        voice_caps = voice_module.voice_capabilities()
    except Exception:
        voice_caps = {"stt": False, "tts": False}
    return {
        "pipecat": importlib.util.find_spec("pipecat") is not None,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "stt": bool(voice_caps.get("stt")),
        "tts": bool(voice_caps.get("tts")),
    }


def _base_state() -> dict[str, Any]:
    return {
        "status": "stopped",
        "meetingId": None,
        "chatId": "",
        "pid": None,
        "port": voice_config.voice_port(),
        "bind": voice_config.voice_bind(),
        "wsUrl": None,
        "startedAt": None,
        "readyAt": None,
        "stoppedAt": None,
        "uptimeS": None,
        "lastError": None,
        "logPath": None,
        "capabilities": _capabilities(),
    }


def _read_state() -> dict[str, Any]:
    path = _state_path()
    if not path.is_file():
        return _base_state()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = _base_state()
        state["status"] = "stale"
        state["lastError"] = "state file unreadable"
        return state
    if not isinstance(raw, dict):
        return _base_state()
    state = _base_state()
    state.update(raw)
    return state


def _write_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _is_alive(pid: Any) -> bool:
    try:
        return shared.is_pid_alive(int(pid))
    except (TypeError, ValueError):
        return False


def _normalize_state(state: dict[str, Any]) -> dict[str, Any]:
    state = dict(_base_state() | state)
    pid = state.get("pid")
    alive = _is_alive(pid) if pid is not None else False
    started_at = state.get("startedAt")
    if alive:
        if state.get("status") not in {"starting", "ready"}:
            state["status"] = "ready"
        if isinstance(started_at, (int, float)):
            state["uptimeS"] = max(0, int(time.time() - float(started_at)))
        else:
            state["uptimeS"] = None
    else:
        if pid is not None and state.get("status") in {"starting", "ready"}:
            state["status"] = "stale"
            state["lastError"] = "tracked voice process is not running"
        state["pid"] = None if state.get("status") in {"stopped", "stale"} else pid
        state["uptimeS"] = None
    state["active"] = state.get("status") in {"starting", "ready"} and state.get("pid") is not None
    state["capabilities"] = _capabilities()
    return state


def status(
    meeting_id: int | None = None,
    chat_id: str | None = None,
) -> dict[str, Any]:
    state = _normalize_state(_read_state())
    active_meeting = state.get("meetingId")
    matches = meeting_id is None or active_meeting in {None, meeting_id}
    if chat_id and state.get("chatId") and state.get("chatId") != chat_id:
        matches = False
    state["requestedMeetingId"] = meeting_id
    state["matchesMeeting"] = bool(matches)
    return state


def _reader_line(stream, out: "queue.Queue[str]") -> None:
    try:
        out.put(stream.readline() if stream is not None else "")
    except Exception as exc:  # noqa: BLE001
        out.put(f'{{"status":"error","error":{json.dumps(str(exc))}}}')


def _wait_for_ready(proc: subprocess.Popen, timeout_s: float) -> dict[str, Any]:
    if proc.stdout is None:
        raise RuntimeError("voice subprocess stdout unavailable")
    lines: "queue.Queue[str]" = queue.Queue(maxsize=1)
    reader = threading.Thread(target=_reader_line, args=(proc.stdout, lines), daemon=True)
    reader.start()
    try:
        line = lines.get(timeout=timeout_s)
    except queue.Empty as exc:
        raise TimeoutError("voice subprocess did not emit ready handshake") from exc
    if proc.poll() is not None and not line:
        raise RuntimeError(f"voice subprocess exited before ready (code={proc.returncode})")
    try:
        payload = json.loads(line.strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"voice subprocess ready line was not JSON: {_redact(line[:120])}") from exc
    if not isinstance(payload, dict) or payload.get("status") != "ready":
        raise RuntimeError(f"voice subprocess did not report ready: {_redact(str(payload))}")
    return payload


def _stop_pid(pid: int, grace_seconds: float) -> None:
    if not _is_alive(pid):
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=max(10, int(grace_seconds) + 2),
            )
        except Exception:
            pass
        deadline = time.monotonic() + max(grace_seconds, 1.0)
        while time.monotonic() < deadline:
            if not _is_alive(pid):
                return
            time.sleep(0.1)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _is_alive(pid):
            return
        time.sleep(0.1)
    if _is_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return


def _stopped_state(previous: dict[str, Any], error: str | None = None) -> dict[str, Any]:
    state = _base_state()
    state["meetingId"] = previous.get("meetingId")
    state["chatId"] = previous.get("chatId") or ""
    state["logPath"] = previous.get("logPath")
    state["lastError"] = error
    state["stoppedAt"] = time.time()
    return state


def start_session(
    meeting_id: int,
    chat_id: str | None = None,
) -> dict[str, Any]:
    chat_id = (chat_id or "").strip()
    with shared.file_lock(_lock_path()):
        current = _normalize_state(_read_state())
        if current.get("active"):
            if current.get("meetingId") == meeting_id and (not chat_id or current.get("chatId") == chat_id):
                current["action"] = "already_running"
                return current
            raise VoiceSessionActive(current)

        port = voice_config.voice_port()
        bind = voice_config.voice_bind()
        log_dir = _log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"cabinet-voice-{meeting_id}.log"
        env = get_scrubbed_sdk_env(profile_root=_active_profile_root())
        cmd = [
            sys.executable,
            "-m",
            "cabinet.voice.voice_server",
            "--meeting-id",
            str(meeting_id),
            "--port",
            str(port),
            "--host",
            bind,
        ]
        if chat_id:
            cmd.extend(["--chat-id", chat_id])

        started_at = time.time()
        starting_state = _base_state()
        starting_state.update({
            "status": "starting",
            "meetingId": meeting_id,
            "chatId": chat_id,
            "port": port,
            "bind": bind,
            "startedAt": started_at,
            "logPath": str(log_path),
        })
        _write_state(starting_state)

        log_handle = open(log_path, "ab")  # noqa: SIM115
        try:
            popen_kwargs: dict[str, Any] = {
                "cwd": str(config.SCRIPTS_DIR),
                "env": env,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": log_handle,
                "text": True,
                "bufsize": 1,
            }
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = (
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                )
            else:
                popen_kwargs["start_new_session"] = True
            proc = subprocess.Popen(cmd, **popen_kwargs)
        finally:
            log_handle.close()

        state = dict(starting_state)
        state["pid"] = proc.pid
        _write_state(state)
        try:
            ready = _wait_for_ready(proc, _start_timeout_s())
        except Exception as exc:  # noqa: BLE001
            reason = _redact(str(exc))
            if _is_alive(proc.pid):
                _stop_pid(proc.pid, grace_seconds=3.0)
            failed = dict(state)
            failed["status"] = "crashed"
            failed["pid"] = None
            failed["lastError"] = reason
            failed = _normalize_state(failed)
            _write_state(failed)
            raise VoiceStartFailed(failed, reason) from exc

        ready_at = time.time()
        state.update({
            "status": "ready",
            "wsUrl": ready.get("ws_url") or f"ws://localhost:{port}",
            "readyAt": ready_at,
            "uptimeS": max(0, int(ready_at - started_at)),
            "lastError": None,
            "action": "started",
        })
        _write_state(state)
        return _normalize_state(state)


def stop_session(
    meeting_id: int | None = None,
    chat_id: str | None = None,
    *,
    grace_seconds: float | None = None,
) -> dict[str, Any]:
    chat_id = (chat_id or "").strip()
    if grace_seconds is None:
        grace_seconds = float(getattr(config, "DASHBOARD_BOT_GRACE_SECONDS", 5))
    with shared.file_lock(_lock_path()):
        current = _normalize_state(_read_state())
        active_meeting = current.get("meetingId")
        if meeting_id is not None and active_meeting not in {None, meeting_id}:
            raise VoiceSessionMismatch(current)
        if chat_id and current.get("chatId") and current.get("chatId") != chat_id:
            raise VoiceSessionMismatch(current)
        pid = current.get("pid")
        if pid is not None and _is_alive(pid):
            _stop_pid(int(pid), grace_seconds=grace_seconds)
        stopped = _stopped_state(current)
        stopped["action"] = "stopped" if pid is not None else "already_stopped"
        stopped = _normalize_state(stopped)
        _write_state(stopped)
        return stopped


def restart_session(
    meeting_id: int,
    chat_id: str | None = None,
) -> dict[str, Any]:
    chat_id = (chat_id or "").strip()
    with shared.file_lock(_lock_path()):
        current = _normalize_state(_read_state())
        if current.get("active") and current.get("meetingId") != meeting_id:
            raise VoiceSessionActive(current)
    stop_session(meeting_id=meeting_id, chat_id=chat_id)
    result = start_session(meeting_id=meeting_id, chat_id=chat_id)
    result["action"] = "restarted"
    _write_state(result)
    return result


__all__ = [
    "VoiceLifecycleError",
    "VoiceSessionActive",
    "VoiceSessionMismatch",
    "VoiceStartFailed",
    "restart_session",
    "start_session",
    "status",
    "stop_session",
]
