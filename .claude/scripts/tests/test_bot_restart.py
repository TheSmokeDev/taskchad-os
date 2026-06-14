"""Tests for the self-restart path: scrub_nested_claude_state, spawn_detached,
the relauncher orchestration, and the /restart handler wiring.

Regression target: `/restart` used to kill the bot via `bash run_chat.sh` and
never bring it back (bash-PATH dependency, no profile, no nesting-marker scrub,
errors → DEVNULL). The fix is a pure-Python detached relauncher.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_CHAT_DIR = Path(__file__).resolve().parents[2] / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))


# --------------------------------------------------------------------------- #
# scrub_nested_claude_state
# --------------------------------------------------------------------------- #

def test_scrub_drops_markers_preserves_everything_else():
    from runtime.subprocess_env import (
        _NESTED_CLAUDE_CODE_STATE_KEYS,
        scrub_nested_claude_state,
    )

    parent = {k: "x" for k in _NESTED_CLAUDE_CODE_STATE_KEYS}
    parent.update(
        {
            "HOMIE_HOME": "/p/profile",
            "TELEGRAM_BOT_TOKEN": "tok",
            "HOME": "/home/u",
            "USERPROFILE": r"C:\Users\u",
            "PATH": "/usr/bin",
        }
    )
    out = scrub_nested_claude_state(parent)

    for key in _NESTED_CLAUDE_CODE_STATE_KEYS:
        assert key not in out, f"{key} should be scrubbed"
    assert out["HOMIE_HOME"] == "/p/profile"  # profile preserved
    assert out["TELEGRAM_BOT_TOKEN"] == "tok"  # creds preserved
    assert out["HOME"] == "/home/u"
    assert out["USERPROFILE"] == r"C:\Users\u"
    assert out["PATH"] == "/usr/bin"


def test_scrub_default_profile_homie_home_stays_absent(monkeypatch):
    from runtime.subprocess_env import scrub_nested_claude_state

    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    out = scrub_nested_claude_state()  # None → os.environ.copy()
    assert "CLAUDECODE" not in out
    assert "HOMIE_HOME" not in out  # default profile: never injected


# --------------------------------------------------------------------------- #
# spawn_detached
# --------------------------------------------------------------------------- #

def test_spawn_detached_platform_kwargs(monkeypatch, tmp_path):
    import shared

    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(shared.subprocess, "Popen", fake_popen)

    log = tmp_path / "logs" / "bot.log"
    pid = shared.spawn_detached(["py", "x"], env={"A": "1"}, log_path=log, cwd=tmp_path)

    assert pid == 4242
    assert captured["cmd"] == ["py", "x"]
    assert captured["kwargs"]["env"] == {"A": "1"}
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    assert captured["kwargs"]["stdin"] is shared.subprocess.DEVNULL
    assert captured["kwargs"]["stderr"] is shared.subprocess.STDOUT  # merged into log
    assert log.parent.exists()  # mkdir -p happened
    if sys.platform == "win32":
        assert captured["kwargs"].get("creationflags", 0) != 0
        assert "start_new_session" not in captured["kwargs"]
    else:
        assert captured["kwargs"].get("start_new_session") is True
        assert "creationflags" not in captured["kwargs"]


def test_spawn_detached_no_log_uses_devnull(monkeypatch, tmp_path):
    import shared

    captured = {}

    class FakeProc:
        pid = 1

    monkeypatch.setattr(
        shared.subprocess,
        "Popen",
        lambda cmd, **kw: captured.update(kw) or FakeProc(),
    )
    shared.spawn_detached(["py"], cwd=tmp_path)
    assert captured["stdout"] is shared.subprocess.DEVNULL
    assert captured["stderr"] is shared.subprocess.DEVNULL


# --------------------------------------------------------------------------- #
# relauncher orchestration
# --------------------------------------------------------------------------- #

def test_relaunch_waits_cleans_then_spawns(monkeypatch, tmp_path):
    import relaunch

    order = []
    monkeypatch.setattr(relaunch, "list_bot_pids_in_active_profile", lambda: [])
    monkeypatch.setattr(
        relaunch, "cleanup_all_bot_processes", lambda: order.append("cleanup") or []
    )
    monkeypatch.setattr(relaunch._services, "get_log_dir", lambda: tmp_path)
    monkeypatch.setattr(relaunch, "scrub_nested_claude_state", lambda: {"SCRUBBED": "1"})

    captured = {}

    def fake_spawn(cmd, **kw):
        order.append("spawn")
        captured["cmd"] = cmd
        captured["kw"] = kw
        return 777

    monkeypatch.setattr(relaunch, "spawn_detached", fake_spawn)

    pid = relaunch.relaunch()

    assert pid == 777
    assert order == ["cleanup", "spawn"]  # kill old BEFORE spawning new
    assert captured["cmd"][0] == sys.executable
    assert captured["cmd"][1].endswith("main.py")
    assert captured["kw"]["env"] == {"SCRUBBED": "1"}  # nesting markers scrubbed
    assert str(captured["kw"]["log_path"]).endswith("bot.log")


# --------------------------------------------------------------------------- #
# /restart handler wiring
# --------------------------------------------------------------------------- #

def test_handle_restart_collect_only_refuses():
    import core_handlers

    out = asyncio.run(
        core_handlers.handle_restart(None, None, "", collect_only=True)
    )
    assert "Cannot chain" in out


def test_handle_restart_spawns_relauncher_then_exits(monkeypatch):
    import core_handlers
    import shared

    captured = {}

    monkeypatch.setattr(
        shared,
        "spawn_detached",
        lambda cmd, **kw: captured.update(cmd=cmd, kw=kw) or 999,
    )

    class _Exit(Exception):
        pass

    def fake_exit(code):
        captured["exit_code"] = code
        raise _Exit()

    monkeypatch.setattr(core_handlers.os, "_exit", fake_exit)

    async def fake_sleep(_s):
        captured["slept"] = True

    monkeypatch.setattr(core_handlers.asyncio, "sleep", fake_sleep)

    sent = []

    class Adapter:
        async def send(self, msg):
            sent.append(msg)

    class Incoming:
        channel = "chan"
        thread = "thr"

    with pytest.raises(_Exit):
        asyncio.run(core_handlers.handle_restart(Adapter(), Incoming(), ""))

    assert sent and "Restarting" in sent[0].text
    assert captured["cmd"][0] == sys.executable
    assert captured["cmd"][1].endswith("relaunch.py")
    assert captured["exit_code"] == 0
