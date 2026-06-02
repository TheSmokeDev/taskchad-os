"""Cabinet voice single-session lifecycle tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import config  # noqa: E402
from cabinet.voice import lifecycle  # noqa: E402


class _FakeStdout:
    def __init__(self, line: str) -> None:
        self._line = line

    def readline(self) -> str:
        return self._line


class _FakeProc:
    def __init__(self, pid: int = 4242, line: str | None = None) -> None:
        self.pid = pid
        self.stdout = _FakeStdout(line or json.dumps({
            "status": "ready",
            "ws_url": "ws://localhost:7860",
        }) + "\n")
        self.returncode = None

    def poll(self) -> None:
        return None


@pytest.fixture
def isolated_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_dir = tmp_path / "state"
    logs_dir = tmp_path / "logs"
    profile_root = tmp_path / "profile"
    state_dir.mkdir()
    logs_dir.mkdir()
    profile_root.mkdir()
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    monkeypatch.setattr(lifecycle._persona_services, "get_log_dir", lambda: logs_dir)
    monkeypatch.setattr(lifecycle, "_active_profile_root", lambda: profile_root)
    monkeypatch.setattr(lifecycle, "_capabilities", lambda: {
        "pipecat": True,
        "ffmpeg": True,
        "stt": True,
        "tts": True,
    })
    return state_dir


def test_status_returns_stopped_when_no_state(isolated_lifecycle: Path) -> None:
    out = lifecycle.status(meeting_id=1, chat_id="chat-a")
    assert out["status"] == "stopped"
    assert out["pid"] is None
    assert out["matchesMeeting"] is True
    assert out["capabilities"] == {
        "pipecat": True,
        "ffmpeg": True,
        "stt": True,
        "tts": True,
    }


def test_start_records_ready_state_and_is_idempotent(
    isolated_lifecycle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alive = {4242}
    popen_calls: list[list[str]] = []

    def _fake_popen(cmd, **_kwargs):
        popen_calls.append(cmd)
        return _FakeProc(pid=4242)

    monkeypatch.setattr(lifecycle.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(lifecycle, "_is_alive", lambda pid: int(pid) in alive)

    first = lifecycle.start_session(meeting_id=7, chat_id="chat-a")
    assert first["status"] == "ready"
    assert first["meetingId"] == 7
    assert first["pid"] == 4242
    assert first["wsUrl"] == "ws://localhost:7860"
    assert first["action"] == "started"
    assert popen_calls[0][2] == "cabinet.voice.voice_server"

    second = lifecycle.start_session(meeting_id=7, chat_id="chat-a")
    assert second["action"] == "already_running"
    assert len(popen_calls) == 1


def test_start_refuses_different_active_meeting(
    isolated_lifecycle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lifecycle.subprocess, "Popen", lambda *_args, **_kwargs: _FakeProc(pid=4242))
    monkeypatch.setattr(lifecycle, "_is_alive", lambda pid: int(pid) == 4242)

    lifecycle.start_session(meeting_id=7, chat_id="chat-a")
    with pytest.raises(lifecycle.VoiceSessionActive) as err:
        lifecycle.start_session(meeting_id=8, chat_id="chat-a")
    assert err.value.status["meetingId"] == 7


def test_stop_cleans_stale_state(
    isolated_lifecycle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = isolated_lifecycle / "cabinet-voice-session.json"
    state_path.write_text(json.dumps({
        "status": "ready",
        "meetingId": 7,
        "chatId": "chat-a",
        "pid": 99999,
        "startedAt": 100.0,
    }), encoding="utf-8")
    monkeypatch.setattr(lifecycle, "_is_alive", lambda _pid: False)

    out = lifecycle.stop_session(meeting_id=7, chat_id="chat-a")
    assert out["status"] == "stopped"
    assert out["active"] is False
    assert out["action"] == "already_stopped"
    assert out["pid"] is None
    assert out["stoppedAt"] is not None


def test_start_failure_records_crashed_state(
    isolated_lifecycle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        lifecycle.subprocess,
        "Popen",
        lambda *_args, **_kwargs: _FakeProc(pid=4242, line='{"status":"error"}\n'),
    )
    monkeypatch.setattr(lifecycle, "_is_alive", lambda pid: int(pid) == 4242)
    monkeypatch.setattr(lifecycle, "_stop_pid", lambda *_args, **_kwargs: None)

    with pytest.raises(lifecycle.VoiceStartFailed):
        lifecycle.start_session(meeting_id=7, chat_id="chat-a")

    out = lifecycle.status(meeting_id=7, chat_id="chat-a")
    assert out["status"] == "crashed"
    assert out["active"] is False
    assert out["lastError"]
