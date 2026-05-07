"""Tests for dashboard_bot_lifecycle.py — PRD-8 Phase 3 / WS2 (R3 NM1).

Delegation contract — every "is the bot running" check goes through
``shared.is_pid_alive`` (Rule 2 physical state), env scrubbing drops
dashboard-only keys before subprocess spawn, profile-aware paths
resolve through personas.services for the TARGET persona.

The 7 canonical test names per PRP §1170-1176:
  * test_activate_starts_bot_and_writes_pid
  * test_activate_idempotent
  * test_deactivate_signals_sigterm_and_clears_pid
  * test_deactivate_idempotent
  * test_deactivate_escalates_to_sigkill_on_timeout
  * test_restart_chains
  * test_is_running_reads_pid_and_cmdline
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


@pytest.fixture
def fake_profile(tmp_path, monkeypatch):
    """Build a minimal named-profile layout under HOMIE_HOME."""
    homie = tmp_path / ".homie"
    profile_dir = homie / "profiles" / "sales"
    for sub in ("memory", "data", "state", "run", "logs"):
        (profile_dir / sub).mkdir(parents=True, exist_ok=True)
    (profile_dir / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=test-token\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOMIE_HOME", str(homie))
    return profile_dir


# ── is_running ───────────────────────────────────────────────────────────


def test_is_running_reads_pid_and_cmdline(fake_profile, monkeypatch):
    """is_running goes through shared.is_pid_alive (Rule 2)."""
    import dashboard_bot_lifecycle as dbl

    # Mock shared.read_pid + shared.is_pid_alive.
    with patch("dashboard_bot_lifecycle.shared.read_pid", return_value=12345), \
         patch("dashboard_bot_lifecycle.shared.is_pid_alive", return_value=True):
        assert dbl.is_running("sales") is True

    # Pid file present but PID is dead — Rule 2: returns False.
    with patch("dashboard_bot_lifecycle.shared.read_pid", return_value=99999), \
         patch("dashboard_bot_lifecycle.shared.is_pid_alive", return_value=False):
        assert dbl.is_running("sales") is False

    # Pid file missing — returns False without raising.
    with patch("dashboard_bot_lifecycle.shared.read_pid", return_value=None):
        assert dbl.is_running("sales") is False


# ── activate ─────────────────────────────────────────────────────────────


def test_activate_idempotent(fake_profile):
    """Activate when already running returns ``already_running`` with the existing pid."""
    import dashboard_bot_lifecycle as dbl

    with patch("dashboard_bot_lifecycle.shared.read_pid", return_value=4242), \
         patch("dashboard_bot_lifecycle.shared.is_pid_alive", return_value=True), \
         patch("dashboard_bot_lifecycle.shared.file_lock") as fake_lock:
        fake_lock.return_value.__enter__ = lambda *a, **k: None
        fake_lock.return_value.__exit__ = lambda *a, **k: None
        result = dbl.activate("sales")
    assert result["status"] == "already_running"
    assert result["pid"] == 4242


def test_activate_starts_bot_and_writes_pid(fake_profile):
    """Activate spawns subprocess and writes pid to the canonical path."""
    import dashboard_bot_lifecycle as dbl

    fake_proc = MagicMock()
    fake_proc.pid = 7777

    with patch("dashboard_bot_lifecycle.shared.read_pid", return_value=None), \
         patch("dashboard_bot_lifecycle.shared.is_pid_alive", return_value=False), \
         patch("dashboard_bot_lifecycle.shared.file_lock") as fake_lock, \
         patch("dashboard_bot_lifecycle.subprocess.Popen", return_value=fake_proc) as mock_popen:
        fake_lock.return_value.__enter__ = lambda *a, **k: None
        fake_lock.return_value.__exit__ = lambda *a, **k: None

        result = dbl.activate("sales")
        assert result["status"] == "running"
        assert result["pid"] == 7777
        # Popen was called with python + the bot main entry.
        assert mock_popen.called
        args, kwargs = mock_popen.call_args
        cmd = args[0]
        assert cmd[0] == sys.executable
        assert cmd[1].endswith("main.py")
        # Env scrubbed — no DASHBOARD_TOKEN leak.
        env = kwargs["env"]
        assert "DASHBOARD_TOKEN" not in env
        assert "DASHBOARD_BIND" not in env
        # HOMIE_HOME forced to TARGET profile root.
        assert env["HOMIE_HOME"] == str(fake_profile)


def test_activate_scrubs_dashboard_token_from_subprocess_env(fake_profile, monkeypatch):
    """DASHBOARD_TOKEN never leaks into the bot subprocess env."""
    import dashboard_bot_lifecycle as dbl

    monkeypatch.setenv("DASHBOARD_TOKEN", "secret-dashboard-token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-token-keep-me")

    fake_proc = MagicMock()
    fake_proc.pid = 8888

    with patch("dashboard_bot_lifecycle.shared.read_pid", return_value=None), \
         patch("dashboard_bot_lifecycle.shared.is_pid_alive", return_value=False), \
         patch("dashboard_bot_lifecycle.shared.file_lock") as fake_lock, \
         patch("dashboard_bot_lifecycle.subprocess.Popen", return_value=fake_proc) as mock_popen:
        fake_lock.return_value.__enter__ = lambda *a, **k: None
        fake_lock.return_value.__exit__ = lambda *a, **k: None

        dbl.activate("sales")
        env = mock_popen.call_args.kwargs["env"]
        assert "DASHBOARD_TOKEN" not in env
        # TELEGRAM_BOT_TOKEN is on the bot-creds whitelist — passes through.
        assert env.get("TELEGRAM_BOT_TOKEN") == "telegram-token-keep-me"


def test_activate_scrubs_dashboard_bind_from_subprocess_env(fake_profile, monkeypatch):
    import dashboard_bot_lifecycle as dbl

    monkeypatch.setenv("DASHBOARD_BIND", "0.0.0.0")
    fake_proc = MagicMock()
    fake_proc.pid = 9999

    with patch("dashboard_bot_lifecycle.shared.read_pid", return_value=None), \
         patch("dashboard_bot_lifecycle.shared.is_pid_alive", return_value=False), \
         patch("dashboard_bot_lifecycle.shared.file_lock") as fake_lock, \
         patch("dashboard_bot_lifecycle.subprocess.Popen", return_value=fake_proc) as mock_popen:
        fake_lock.return_value.__enter__ = lambda *a, **k: None
        fake_lock.return_value.__exit__ = lambda *a, **k: None
        dbl.activate("sales")
        env = mock_popen.call_args.kwargs["env"]
        assert "DASHBOARD_BIND" not in env


def test_activate_forces_homie_home_for_target_persona(fake_profile):
    import dashboard_bot_lifecycle as dbl

    fake_proc = MagicMock()
    fake_proc.pid = 1111

    with patch("dashboard_bot_lifecycle.shared.read_pid", return_value=None), \
         patch("dashboard_bot_lifecycle.shared.is_pid_alive", return_value=False), \
         patch("dashboard_bot_lifecycle.shared.file_lock") as fake_lock, \
         patch("dashboard_bot_lifecycle.subprocess.Popen", return_value=fake_proc) as mock_popen:
        fake_lock.return_value.__enter__ = lambda *a, **k: None
        fake_lock.return_value.__exit__ = lambda *a, **k: None
        dbl.activate("sales")
        env = mock_popen.call_args.kwargs["env"]
        assert env["HOMIE_HOME"] == str(fake_profile)


# ── deactivate ───────────────────────────────────────────────────────────


def test_deactivate_idempotent(fake_profile):
    """deactivate when no pid file → returns 'already_stopped' without raising."""
    import dashboard_bot_lifecycle as dbl

    with patch("dashboard_bot_lifecycle.shared.read_pid", return_value=None), \
         patch("dashboard_bot_lifecycle.shared.file_lock") as fake_lock:
        fake_lock.return_value.__enter__ = lambda *a, **k: None
        fake_lock.return_value.__exit__ = lambda *a, **k: None
        result = dbl.deactivate("sales")
    assert result["status"] == "already_stopped"


def test_deactivate_stale_pid_file_returns_already_stopped(fake_profile):
    """Pid file present but process is dead — clean and return already_stopped."""
    import dashboard_bot_lifecycle as dbl

    # Pre-seed a stale pid file.
    pid_file = fake_profile / "run" / "bot.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text("99999", encoding="utf-8")

    with patch("dashboard_bot_lifecycle.shared.read_pid", return_value=99999), \
         patch("dashboard_bot_lifecycle.shared.is_pid_alive", return_value=False), \
         patch("dashboard_bot_lifecycle.shared.file_lock") as fake_lock:
        fake_lock.return_value.__enter__ = lambda *a, **k: None
        fake_lock.return_value.__exit__ = lambda *a, **k: None
        result = dbl.deactivate("sales")
    assert result["status"] == "already_stopped"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific")
def test_deactivate_windows_uses_taskkill(fake_profile):
    """Windows path uses taskkill /F /PID — mirrors shared.cleanup_stale_pid."""
    import dashboard_bot_lifecycle as dbl

    # Sequence of is_pid_alive calls — alive on entry check, dead after taskkill.
    call_count = [0]

    def fake_alive(_pid):
        call_count[0] += 1
        # First call (entry guard) and second call (post-taskkill verify) get alive=True
        # Third+ call (final verify) get alive=False.
        return call_count[0] < 3

    with patch("dashboard_bot_lifecycle.shared.read_pid", return_value=12345), \
         patch("dashboard_bot_lifecycle.shared.is_pid_alive", side_effect=fake_alive), \
         patch("dashboard_bot_lifecycle.shared.file_lock") as fake_lock, \
         patch("dashboard_bot_lifecycle.subprocess.run") as mock_run:
        fake_lock.return_value.__enter__ = lambda *a, **k: None
        fake_lock.return_value.__exit__ = lambda *a, **k: None
        result = dbl.deactivate("sales", grace_seconds=1)
        # Verify taskkill was called.
        assert mock_run.called
        cmd = mock_run.call_args.args[0]
        assert cmd[0] == "taskkill"
        assert "/F" in cmd
        assert "/PID" in cmd
        assert result["status"] == "stopped"


@pytest.mark.skipif(sys.platform == "win32", reason="Unix-specific")
def test_deactivate_unix_uses_sigterm(fake_profile):
    """Unix path uses signal.SIGTERM with grace window."""
    import dashboard_bot_lifecycle as dbl
    import signal

    alive_seq = iter([True, True, False])

    def fake_alive(_pid):
        try:
            return next(alive_seq)
        except StopIteration:
            return False

    with patch("dashboard_bot_lifecycle.shared.read_pid", return_value=12345), \
         patch("dashboard_bot_lifecycle.shared.is_pid_alive", side_effect=fake_alive), \
         patch("dashboard_bot_lifecycle.shared.file_lock") as fake_lock, \
         patch("dashboard_bot_lifecycle.os.kill") as mock_kill:
        fake_lock.return_value.__enter__ = lambda *a, **k: None
        fake_lock.return_value.__exit__ = lambda *a, **k: None
        dbl.deactivate("sales", grace_seconds=1)
        # SIGTERM was issued.
        assert mock_kill.called
        call_args = mock_kill.call_args.args
        assert call_args[1] == signal.SIGTERM


def test_deactivate_signals_sigterm_and_clears_pid(fake_profile):
    """End-to-end: deactivate kills + cleans pid file."""
    import dashboard_bot_lifecycle as dbl

    pid_file = fake_profile / "run" / "bot.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text("12345", encoding="utf-8")

    alive_seq = iter([True, False, False])

    def fake_alive(_pid):
        try:
            return next(alive_seq)
        except StopIteration:
            return False

    if sys.platform == "win32":
        kill_patch = patch("dashboard_bot_lifecycle.subprocess.run")
    else:
        kill_patch = patch("dashboard_bot_lifecycle.os.kill")

    with patch("dashboard_bot_lifecycle.shared.read_pid", return_value=12345), \
         patch("dashboard_bot_lifecycle.shared.is_pid_alive", side_effect=fake_alive), \
         patch("dashboard_bot_lifecycle.shared.file_lock") as fake_lock, \
         kill_patch:
        fake_lock.return_value.__enter__ = lambda *a, **k: None
        fake_lock.return_value.__exit__ = lambda *a, **k: None
        result = dbl.deactivate("sales", grace_seconds=1)
    assert result["status"] == "stopped"
    # Pid file cleaned.
    assert not pid_file.exists()


def test_deactivate_escalates_to_sigkill_on_timeout(fake_profile):
    """Process refuses to die after SIGTERM → escalates to SIGKILL → final RuntimeError."""
    import dashboard_bot_lifecycle as dbl
    import signal

    if sys.platform == "win32":
        # Windows uses taskkill — RuntimeError if pid stays alive after.
        with patch("dashboard_bot_lifecycle.shared.read_pid", return_value=12345), \
             patch("dashboard_bot_lifecycle.shared.is_pid_alive", return_value=True), \
             patch("dashboard_bot_lifecycle.shared.file_lock") as fake_lock, \
             patch("dashboard_bot_lifecycle.subprocess.run"):
            fake_lock.return_value.__enter__ = lambda *a, **k: None
            fake_lock.return_value.__exit__ = lambda *a, **k: None
            with pytest.raises(RuntimeError):
                dbl.deactivate("sales", grace_seconds=1)
    else:
        # Unix: process refuses both SIGTERM and SIGKILL.
        with patch("dashboard_bot_lifecycle.shared.read_pid", return_value=12345), \
             patch("dashboard_bot_lifecycle.shared.is_pid_alive", return_value=True), \
             patch("dashboard_bot_lifecycle.shared.file_lock") as fake_lock, \
             patch("dashboard_bot_lifecycle.os.kill") as mock_kill:
            fake_lock.return_value.__enter__ = lambda *a, **k: None
            fake_lock.return_value.__exit__ = lambda *a, **k: None
            with pytest.raises(RuntimeError):
                dbl.deactivate("sales", grace_seconds=1)
            # Both SIGTERM AND SIGKILL were sent.
            kill_signals = [c.args[1] for c in mock_kill.call_args_list]
            assert signal.SIGTERM in kill_signals
            assert signal.SIGKILL in kill_signals


def test_deactivate_rule2_physical_state_check(fake_profile):
    """Rule 2 — every is-running check uses shared.is_pid_alive (NOT file presence)."""
    import dashboard_bot_lifecycle as dbl

    # Pid file PRESENT but process dead — Rule 2 says: not running.
    with patch("dashboard_bot_lifecycle.shared.read_pid", return_value=12345) as mock_read, \
         patch("dashboard_bot_lifecycle.shared.is_pid_alive", return_value=False) as mock_alive, \
         patch("dashboard_bot_lifecycle.shared.file_lock") as fake_lock:
        fake_lock.return_value.__enter__ = lambda *a, **k: None
        fake_lock.return_value.__exit__ = lambda *a, **k: None
        result = dbl.deactivate("sales")
        # is_pid_alive was called — proving Rule 2 compliance.
        assert mock_alive.called
        # Did NOT proceed to kill because shared.is_pid_alive said False.
        assert result["status"] == "already_stopped"


# ── restart ──────────────────────────────────────────────────────────────


def test_restart_chains(fake_profile):
    """restart calls deactivate then activate."""
    import dashboard_bot_lifecycle as dbl

    with patch("dashboard_bot_lifecycle.deactivate", return_value={"persona_id": "sales", "status": "stopped"}) as mock_deact, \
         patch("dashboard_bot_lifecycle.activate", return_value={"persona_id": "sales", "pid": 5555, "status": "running"}) as mock_act, \
         patch("dashboard_bot_lifecycle.shared.read_pid", return_value=2222):
        result = dbl.restart("sales")
        assert mock_deact.called
        assert mock_act.called
        assert result["status"] == "restarted"
        assert result["old_pid"] == 2222
        assert result["new_pid"] == 5555


# ── multi-persona isolation ──────────────────────────────────────────────


def test_activate_two_personas_do_not_corrupt_each_others_pid_files(tmp_path, monkeypatch):
    """Two activate calls for different personas write distinct pid files."""
    import dashboard_bot_lifecycle as dbl

    homie = tmp_path / ".homie"
    for name in ("sales", "engineering"):
        for sub in ("memory", "data", "state", "run", "logs"):
            (homie / "profiles" / name / sub).mkdir(parents=True, exist_ok=True)
        (homie / "profiles" / name / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("HOMIE_HOME", str(homie))

    sales_proc = MagicMock(pid=1001)
    eng_proc = MagicMock(pid=1002)

    proc_iter = iter([sales_proc, eng_proc])

    def fake_popen(*args, **kwargs):
        return next(proc_iter)

    with patch("dashboard_bot_lifecycle.shared.read_pid", return_value=None), \
         patch("dashboard_bot_lifecycle.shared.is_pid_alive", return_value=False), \
         patch("dashboard_bot_lifecycle.shared.file_lock") as fake_lock, \
         patch("dashboard_bot_lifecycle.subprocess.Popen", side_effect=fake_popen):
        fake_lock.return_value.__enter__ = lambda *a, **k: None
        fake_lock.return_value.__exit__ = lambda *a, **k: None

        sales_result = dbl.activate("sales")
        eng_result = dbl.activate("engineering")

    assert sales_result["pid"] == 1001
    assert eng_result["pid"] == 1002

    # Pid files written under TARGET profile dirs (not corrupted across).
    sales_pid = (homie / "profiles" / "sales" / "run" / "bot.pid").read_text().strip()
    eng_pid = (homie / "profiles" / "engineering" / "run" / "bot.pid").read_text().strip()
    assert sales_pid == "1001"
    assert eng_pid == "1002"
