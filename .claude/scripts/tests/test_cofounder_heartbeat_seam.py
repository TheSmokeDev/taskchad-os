"""Tests for the US-006 heartbeat seam: guarded co-founder pass invocation.

The seam lives at the END of ``heartbeat.main()``, AFTER
``asyncio.run(run_heartbeat(...))`` returns — outside the active-hours gate
(which lives INSIDE ``run_heartbeat``), so a 3 AM heartbeat that skips alert
work still advances builds.

Code paths (Testing Principle — one test per path):

1. Pass fires after run_heartbeat returns; ``--test`` flows ``dry_run=True``.
2. Non-test mode flows ``dry_run=False``.
3. Raising run_pass: ``main()`` completes (exit behavior untouched) and the
   failure is appended to heartbeat_errors.log.
4. Broken cofounder package (the lazy import itself raises): heartbeat
   survives and logs the ImportError.
5. The error logger's own write failure is swallowed (fail-open squared).
6. Real ``run_pass`` wired end-to-end: disabled co-founder = quiet no-op.

Exit-code note: ``heartbeat.main()`` returns None on success and the
``__main__`` guard only alters the exit code by re-raising — so "exit
behavior untouched" is proven in-process by ``main()`` completing without
an exception escaping.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import heartbeat


def _err_log(project_root: Path) -> Path:
    return project_root / ".claude" / "scripts" / "heartbeat_errors.log"


@pytest.fixture
def seam_harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    """Stub the heavy heartbeat internals; return the shared call-order list.

    Routes ``PROJECT_ROOT`` (and therefore heartbeat_errors.log) into the
    tmp tree so tests never touch the repo's real error log and can assert
    presence/absence of appended failures.
    """
    calls: list[str] = []

    async def fake_run_heartbeat(test_mode: bool = False):
        calls.append("run_heartbeat")
        return None

    monkeypatch.setattr(heartbeat, "run_heartbeat", fake_run_heartbeat)
    monkeypatch.setattr(heartbeat, "ensure_directories", lambda: None)
    monkeypatch.setattr(heartbeat, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".claude" / "scripts").mkdir(parents=True)
    return calls


def test_pass_runs_after_heartbeat_and_test_flag_flows(
    seam_harness: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: list[dict] = []

    def fake_run_pass(**kwargs):
        seam_harness.append("run_pass")
        captured.append(kwargs)

    monkeypatch.setattr("cofounder.run_pass.run_pass", fake_run_pass)
    monkeypatch.setattr(sys, "argv", ["heartbeat.py", "--test"])

    heartbeat.main()

    assert seam_harness == ["run_heartbeat", "run_pass"]
    assert captured == [{"dry_run": True}]
    assert not _err_log(tmp_path).exists()


def test_non_test_mode_passes_dry_run_false(
    seam_harness: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: list[dict] = []

    def fake_run_pass(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr("cofounder.run_pass.run_pass", fake_run_pass)
    monkeypatch.setattr(sys, "argv", ["heartbeat.py"])

    heartbeat.main()

    assert captured == [{"dry_run": False}]
    assert not _err_log(tmp_path).exists()


def test_raising_pass_leaves_heartbeat_exit_untouched(
    seam_harness: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom(**kwargs):
        raise RuntimeError("cofounder pass exploded")

    monkeypatch.setattr("cofounder.run_pass.run_pass", boom)
    monkeypatch.setattr(sys, "argv", ["heartbeat.py", "--test"])

    heartbeat.main()  # must not raise — the heartbeat exit code stays 0

    assert seam_harness == ["run_heartbeat"]
    log_text = _err_log(tmp_path).read_text(encoding="utf-8")
    assert "cofounder pass" in log_text
    assert "RuntimeError: cofounder pass exploded" in log_text


def test_broken_cofounder_package_cannot_break_heartbeat(
    seam_harness: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # None in sys.modules makes the seam's lazy `from cofounder.run_pass
    # import run_pass` raise ImportError — the broken-package case the lazy
    # import inside try/except exists for.
    monkeypatch.setitem(sys.modules, "cofounder.run_pass", None)
    monkeypatch.setattr(sys, "argv", ["heartbeat.py", "--test"])

    heartbeat.main()

    log_text = _err_log(tmp_path).read_text(encoding="utf-8")
    # Python raises ModuleNotFoundError (ImportError subclass) for the
    # None-in-sys.modules broken-package case.
    assert "ModuleNotFoundError" in log_text
    assert "cofounder.run_pass" in log_text


def test_error_log_write_failure_is_swallowed(
    seam_harness: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("cofounder.run_pass.run_pass", boom)
    # Point PROJECT_ROOT somewhere `.claude/scripts` does NOT exist so the
    # error-log open() itself fails — the logger must swallow that too.
    monkeypatch.setattr(heartbeat, "PROJECT_ROOT", tmp_path / "missing")
    monkeypatch.setattr(sys, "argv", ["heartbeat.py", "--test"])

    heartbeat.main()  # neither the pass failure nor the log failure escapes


def test_real_run_pass_disabled_is_quiet_noop(
    seam_harness: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # End-to-end wiring proof: the REAL run_pass executes through the seam
    # and no-ops quietly while the co-founder is disabled (the default state
    # until the operator's Phase 9 flip). No error log, no exception.
    monkeypatch.setenv("COFOUNDER_ENABLED", "false")
    monkeypatch.delenv("HOMIE_KILLSWITCH_COFOUNDER", raising=False)
    monkeypatch.setattr(sys, "argv", ["heartbeat.py", "--test"])

    heartbeat.main()

    assert seam_harness == ["run_heartbeat"]
    assert not _err_log(tmp_path).exists()
