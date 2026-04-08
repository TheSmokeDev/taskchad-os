"""Tests for PID file management utilities.

These tests validate the PID lifecycle that Phase 1 (System Reliability) depends on:
- Writing/reading PID files
- Detecting stale (dead) processes
- Detecting live processes
- Cleanup of stale PID files
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from shared import is_pid_alive, read_pid, remove_pid, write_pid

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWriteReadPid:
    """Test basic PID file read/write/remove lifecycle."""

    def test_write_and_read(self, tmp_pid_file: Path) -> None:
        write_pid(tmp_pid_file)
        assert read_pid(tmp_pid_file) == os.getpid()

    def test_read_missing_file(self, tmp_pid_file: Path) -> None:
        assert read_pid(tmp_pid_file) is None

    def test_read_corrupt_file(self, tmp_pid_file: Path) -> None:
        tmp_pid_file.write_text("not-a-number", encoding="utf-8")
        assert read_pid(tmp_pid_file) is None

    def test_read_empty_file(self, tmp_pid_file: Path) -> None:
        tmp_pid_file.write_text("", encoding="utf-8")
        assert read_pid(tmp_pid_file) is None

    def test_remove_pid(self, tmp_pid_file: Path) -> None:
        write_pid(tmp_pid_file)
        assert tmp_pid_file.exists()
        remove_pid(tmp_pid_file)
        assert not tmp_pid_file.exists()

    def test_remove_nonexistent(self, tmp_pid_file: Path) -> None:
        # Should not raise
        remove_pid(tmp_pid_file)


class TestIsPidAlive:
    """Test process alive detection (cross-platform)."""

    def test_current_process_is_alive(self) -> None:
        assert is_pid_alive(os.getpid()) is True

    def test_dead_process_is_not_alive(self) -> None:
        # Spawn a short-lived process and wait for it to exit
        proc = subprocess.Popen(
            [sys.executable, "-c", "pass"],
        )
        proc.wait(timeout=10)
        # Small delay to ensure OS cleans up
        time.sleep(0.2)
        assert is_pid_alive(proc.pid) is False

    def test_nonexistent_pid(self) -> None:
        # PID 4 is System on Windows, but 99999 is unlikely to exist
        assert is_pid_alive(99999) is False

    def test_live_subprocess(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
        )
        try:
            assert is_pid_alive(proc.pid) is True
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific test")
    def test_os_kill_is_broken_on_windows(self) -> None:
        """Verify that os.kill(pid, 0) gives wrong answer on Windows.

        This test documents the bug that motivated using ctypes instead.
        If this test ever FAILS (os.kill becomes correct), we can simplify is_pid_alive.
        """
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(timeout=10)
        time.sleep(0.2)

        # os.kill should wrongly report alive
        try:
            os.kill(proc.pid, 0)
            os_kill_says_alive = True
        except OSError:
            os_kill_says_alive = False

        # Our is_pid_alive (ctypes) should correctly report dead
        assert os_kill_says_alive is True, "os.kill bug was fixed — simplify is_pid_alive!"
        assert is_pid_alive(proc.pid) is False


class TestStaleDetection:
    """Test full stale PID detection workflow."""

    def test_stale_pid_file_with_dead_process(self, tmp_pid_file: Path) -> None:
        """PID file points to dead process → should detect as stale."""
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(timeout=10)
        time.sleep(0.2)

        # Write dead PID to file
        tmp_pid_file.write_text(str(proc.pid), encoding="utf-8")

        pid = read_pid(tmp_pid_file)
        assert pid is not None
        assert is_pid_alive(pid) is False

    def test_pid_file_with_live_process(self, tmp_pid_file: Path) -> None:
        """PID file points to live process → should detect as alive."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
        )
        try:
            tmp_pid_file.write_text(str(proc.pid), encoding="utf-8")

            pid = read_pid(tmp_pid_file)
            assert pid is not None
            assert is_pid_alive(pid) is True
        finally:
            proc.terminate()
            proc.wait(timeout=5)
