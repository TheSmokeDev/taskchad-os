"""PRP-7b WS4 — quiesce-before-delete state machine tests.

Disposition coverage (LOAD-BEARING):
    - R1 B1 — delete-lock context wraps quiesce + remove_wrapper + rmtree.
    - R2 NB1 + R3 NNB1 + R3 NNB2 — Step 5 detects a real shared.file_lock
      held by a subprocess via byte-0 OS-lock probe.
    - R3 NNM2 — state-machine ordering: validate -> containment -> symlink
      -> delete-lock -> quiesce -> remove_wrapper -> rmtree.
    - R2 NM4 — symlink + path-escape rejected BEFORE any side effect.
    - Step 5 raises LifecycleError BEFORE step 6 (bot kill) when held.
    - R3 NNM3 — `_acquire_delete_lock` wraps FileExistsError as LifecycleError.

Companion subprocess helpers live in `tests/_holders/hold_state_lock.py`
and `tests/_holders/hold_byte0_winlock.py` (excluded from pytest collection
via `collect_ignore_glob = ["_holders/*"]`).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import shared
from personas.atomic import (
    _acquire_delete_lock,
    _check_state_file_lockholders,
    quiesce_profile,
)
from personas.lifecycle import (
    LifecycleError,
    _profile_root,
    create_profile,
    delete_profile,
)


SCRIPTS_DIR = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Step 0 — validate_persona_name first
# ---------------------------------------------------------------------------


def test_step0_validate_persona_name_runs_first(empty_homie_root):
    """Invalid name -> ValueError BEFORE any filesystem access."""
    with pytest.raises(ValueError):
        delete_profile("INVALID-NAME-UPPERCASE", yes=True)


# ---------------------------------------------------------------------------
# Step 2 — containment check before lock acquisition
# ---------------------------------------------------------------------------


def test_step2_nonexistent_profile_raises_file_not_found(empty_homie_root):
    """Profile dir missing -> FileNotFoundError (no side effects)."""
    with pytest.raises(FileNotFoundError):
        delete_profile("doesnotexist", yes=True)


# ---------------------------------------------------------------------------
# Step 3 — symlink rejection BEFORE lock acquisition (R2 NM4)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="os.symlink on Windows requires elevation or developer mode; "
    "the symlink-rejection invariant is platform-agnostic and the POSIX "
    "test path is sufficient coverage.",
)
def test_step3_symlink_rejected_before_lock_or_wrapper(empty_homie_root):
    """R2 NM4 — symlink rejected BEFORE any destructive side effect.

    Asserts:
      - LifecycleError raised with "symlink|escapes profiles root" message.
      - NO `.delete.lock` file written anywhere.
      - The real "sales" target dir is INTACT.
    """
    real = create_profile("sales", no_alias=True).path
    profiles_root = empty_homie_root / "profiles"
    evil = profiles_root / "evil"
    os.symlink(real, evil)

    # Pre-record state for "no side effect" assertions.
    assert not (evil / ".delete.lock").exists()
    assert not (real / ".delete.lock").exists()

    with pytest.raises(LifecycleError, match="symlink|escapes profiles root"):
        delete_profile("evil", yes=True)

    assert not (evil / ".delete.lock").exists()
    assert not (real / ".delete.lock").exists()
    assert real.exists(), "symlink rejection mutated the real profile dir"


# ---------------------------------------------------------------------------
# Step 4 — delete-lock acquisition + contention behavior
# ---------------------------------------------------------------------------


def test_step4_delete_lock_contention_raises_lifecycle_error(empty_homie_root):
    """Pre-create `.delete.lock` -> _acquire_delete_lock raises LifecycleError."""
    info = create_profile("sales", no_alias=True)
    lock_path = info.path / ".delete.lock"
    lock_path.write_text("12345")

    with pytest.raises(LifecycleError, match="being deleted by another process"):
        delete_profile("sales", yes=True)
    # Profile dir still on disk — destructive sequence never ran.
    assert info.path.exists()


def test_step4_acquire_delete_lock_releases_on_normal_exit(tmp_path):
    """The context manager unlinks the lock file on context exit."""
    profile_dir = tmp_path / "sales"
    profile_dir.mkdir()
    with _acquire_delete_lock(profile_dir):
        assert (profile_dir / ".delete.lock").exists()
    assert not (profile_dir / ".delete.lock").exists()


def test_step4_acquire_delete_lock_releases_on_exception(tmp_path):
    """The context manager unlinks the lock file on exception too."""
    profile_dir = tmp_path / "sales"
    profile_dir.mkdir()
    with pytest.raises(RuntimeError):
        with _acquire_delete_lock(profile_dir):
            assert (profile_dir / ".delete.lock").exists()
            raise RuntimeError("forced failure inside lock body")
    assert not (profile_dir / ".delete.lock").exists()


# ---------------------------------------------------------------------------
# R2 NB1 + R3 NNB1 + R3 NNB2 — Step 5 real-lock holder via subprocess
# ---------------------------------------------------------------------------


def test_step5_subprocess_holds_real_state_lock(empty_homie_root):
    """LOAD-BEARING — probe detects a real `shared.file_lock` holder via
    byte-0 OS-lock semantics.

    Spawns `tests/_holders/hold_state_lock.py` which calls
    `shared.file_lock(state_file)` and signals via stdout. While the
    subprocess holds the lock:
      a) `_check_state_file_lockholders` returns the held lock path.
      b) `delete_profile` raises LifecycleError matching "state locks held".
      c) Profile dir is INTACT (rmtree never ran).
    After the subprocess releases:
      d) probe returns empty list.
    """
    info = create_profile("sales", no_alias=True)
    profile_root = info.path
    state_dir = profile_root / "state"
    state_file = state_dir / "dream-state.json"
    state_file.touch()

    helper = Path(__file__).resolve().parent / "_holders" / "hold_state_lock.py"
    proc = subprocess.Popen(
        [sys.executable, str(helper), str(state_file)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=str(SCRIPTS_DIR),
    )
    try:
        line = proc.stdout.readline().strip()
        assert line == "LOCKED", (
            f"helper did not print LOCKED; got {line!r}; "
            f"stderr={proc.stderr.read() if proc.poll() else '<still running>'}"
        )
        # (a) Probe detects the real lock holder.
        held = _check_state_file_lockholders(state_dir)
        assert any(p.name == "dream-state.json.lock" for p in held), (
            f"probe missed the held lock: {held}"
        )
        # (b) delete_profile raises LifecycleError citing held locks.
        with pytest.raises(LifecycleError, match="state locks held"):
            delete_profile("sales", yes=True)
        # (c) Profile dir survives.
        assert profile_root.exists()
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)

    # (d) After release, probe returns empty.
    held_after = _check_state_file_lockholders(state_dir)
    assert not held_after, f"lock still showing held after release: {held_after}"


# ---------------------------------------------------------------------------
# R4 NNNM1 — Windows byte-0 vs EOF non-empty lock file regression
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="byte-0 vs EOF probe is a Windows-only msvcrt test",
)
def test_step5_windows_byte0_vs_eof_non_empty_lock_file(tmp_path):
    """R4 NNNM1 — probe must read byte 0, not EOF. Use a NON-EMPTY held lock file.

    Pre-write stale bytes into the lock file, then the helper opens "r+"
    (no truncate) and locks byte 0 directly via msvcrt.locking. A buggy
    "a"-mode probe would lock at EOF and miss byte 0 -> false negative ->
    delete proceeds while a real writer is active.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    lock_file = state_dir / "memory.json.lock"
    lock_file.write_text("stale-pid-marker-12345\n")

    helper = (
        Path(__file__).resolve().parent / "_holders" / "hold_byte0_winlock.py"
    )
    proc = subprocess.Popen(
        [sys.executable, str(helper), str(lock_file)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        line = proc.stdout.readline().strip()
        assert line == "LOCKED-BYTE0", (
            f"helper did not print LOCKED-BYTE0; got {line!r}"
        )
        # Probe MUST detect the held byte-0 lock even with stale content.
        held = _check_state_file_lockholders(state_dir)
        assert len(held) == 1, f"expected 1 held lock; got {held}"
        assert held[0].name == "memory.json.lock"
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


# ---------------------------------------------------------------------------
# R1 B1 — wrong-state-dir regression
# ---------------------------------------------------------------------------


def test_step5_uses_correct_state_dir_not_workspace_state(empty_homie_root):
    """R1 B1 — quiesce reads `get_persona_paths(name)["state"]`, NOT the
    OLD-broken `<profile>/workspace/state` path.

    Drop a `.lock` file at the OLD wrong path and assert delete still
    SUCCEEDS — proving the new code scans the correct directory.
    """
    info = create_profile("sales", no_alias=True)
    workspace_state = info.path / "workspace" / "state"
    workspace_state.mkdir(parents=True, exist_ok=True)
    # Idle .lock file at the wrong path — not actually held by any process.
    (workspace_state / "stale.lock").write_text("stale")

    # Delete should succeed because the real state dir (<profile>/state) is
    # empty.
    delete_profile("sales", yes=True)
    assert not info.path.exists()


# ---------------------------------------------------------------------------
# Step 9 (rmtree) — INSIDE delete-lock body (R1 B1)
# ---------------------------------------------------------------------------


def test_step9_rmtree_runs_inside_delete_lock_body(empty_homie_root):
    """R1 B1 — patch `shutil.rmtree` to verify .delete.lock exists at call time."""
    info = create_profile("sales", no_alias=True)
    profile_root = info.path

    seen_lock_existence: list[bool] = []
    real_rmtree = shutil.rmtree

    def spy_rmtree(path, *args, **kwargs):
        # Verify the delete-lock file exists at the moment rmtree is invoked.
        seen_lock_existence.append((profile_root / ".delete.lock").exists())
        # Call through to the real rmtree so the test cleans up.
        return real_rmtree(path, *args, **kwargs)

    # Patch the lifecycle module's rmtree reference.
    with patch("personas.lifecycle.shutil.rmtree", side_effect=spy_rmtree):
        delete_profile("sales", yes=True)

    assert seen_lock_existence, "rmtree was not called"
    assert seen_lock_existence[0] is True, (
        "rmtree ran outside the delete-lock body — invariant violated"
    )
    # Lock auto-released after rmtree.
    assert not (profile_root / ".delete.lock").exists()


# ---------------------------------------------------------------------------
# Step ordering — quiesce runs INSIDE delete-lock body
# ---------------------------------------------------------------------------


def test_step5_quiesce_runs_inside_delete_lock_body(empty_homie_root):
    """When `quiesce_profile` is called, `<profile>/.delete.lock` exists."""
    info = create_profile("sales", no_alias=True)
    profile_root = info.path

    saw_lock: list[bool] = []
    real_quiesce = quiesce_profile

    def spy_quiesce(name, root, *args, **kwargs):
        saw_lock.append((root / ".delete.lock").exists())
        return real_quiesce(name, root, *args, **kwargs)

    with patch("personas.lifecycle.quiesce_profile", side_effect=spy_quiesce):
        delete_profile("sales", yes=True)

    assert saw_lock, "quiesce_profile was not called"
    assert saw_lock[0] is True, (
        "quiesce ran OUTSIDE the delete-lock body — invariant violated"
    )


# ---------------------------------------------------------------------------
# Step 5 fail-CLOSED on iteration error (R2 NB1)
# ---------------------------------------------------------------------------


def test_step5_fail_closed_on_state_dir_unreadable(empty_homie_root, monkeypatch):
    """R2 NB1 — if `Path.rglob` raises OSError, `_check_state_file_lockholders`
    returns a sentinel non-empty list -> caller fails CLOSED."""
    info = create_profile("sales", no_alias=True)
    state_dir = info.path / "state"

    # Patch state_dir.rglob (via the Path class) to raise OSError.
    real_rglob = Path.rglob

    def bad_rglob(self, pattern, *args, **kwargs):
        if self == state_dir:
            raise OSError("iteration error")
        return real_rglob(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "rglob", bad_rglob)

    held = _check_state_file_lockholders(state_dir)
    # Sentinel `<scan-failed>` returned -> truthy list -> caller refuses.
    assert held, "expected sentinel non-empty list on rglob failure"


# ---------------------------------------------------------------------------
# Step 5 — empty state dir produces no holders
# ---------------------------------------------------------------------------


def test_step5_empty_state_dir_returns_no_holders(empty_homie_root):
    info = create_profile("sales", no_alias=True)
    held = _check_state_file_lockholders(info.path / "state")
    assert held == []


def test_step5_missing_state_dir_returns_no_holders(tmp_path):
    """Non-existent state dir returns [] (graceful)."""
    held = _check_state_file_lockholders(tmp_path / "doesnotexist")
    assert held == []


# ---------------------------------------------------------------------------
# Step 6 -> Step 7 ordering — bot-kill ALWAYS precedes unit-uninstall
# ---------------------------------------------------------------------------


def test_step6_precedes_step7_bot_kill_before_unit_uninstall(
    empty_homie_root, monkeypatch
):
    """Quiesce ladder ordering: Step 6 (bot kill) runs BEFORE Step 7
    (scheduled-unit cleanup).

    Records call order via a shared list patched onto `os.kill` and
    `_uninstall_launchd_plist` / `_uninstall_systemd_unit`. We force
    `sys.platform = "darwin"` so the launchd path is the unit hook.
    """
    info = create_profile("sales", no_alias=True)
    profile_root = info.path

    # Seed bot.pid with our own PID so is_pid_alive returns True and
    # SIGTERM/TerminateProcess actually executes.
    pid_file = profile_root / "run" / "bot.pid"
    pid_file.write_text(str(os.getpid()))

    call_order: list[str] = []

    # On Windows, os.kill semantics differ (quiesce uses ctypes); on POSIX
    # the SIGTERM lands via os.kill. Patch os.kill if POSIX, else patch
    # ctypes.windll.kernel32.GenerateConsoleCtrlEvent.
    real_is_alive_calls = {"n": 0}
    from personas import atomic as atomic_mod

    def fake_is_pid_alive(pid):
        # Return True ONCE (so the kill code path runs), then False
        # (so the loop exits without waiting 2s).
        real_is_alive_calls["n"] += 1
        return real_is_alive_calls["n"] == 1

    monkeypatch.setattr(atomic_mod, "is_pid_alive", fake_is_pid_alive)

    # Force the platform FIRST so the bot-kill code routes to the POSIX
    # `os.kill` branch (and the unit hook runs the launchd path).
    monkeypatch.setattr(sys, "platform", "darwin")

    # Patch os.kill UNCONDITIONALLY — quiesce will route through the POSIX
    # branch under the patched sys.platform = "darwin" regardless of the
    # actual host platform. Real os.kill on Windows would otherwise try
    # to kill our own test process via the SIGTERM call.
    def fake_kill(pid, sig):
        call_order.append("bot_kill")
        # Don't actually kill — just record.

    monkeypatch.setattr(os, "kill", fake_kill)

    # Patch _uninstall_launchd_plist via the wrappers module (atomic
    # imports it lazily).
    from personas import wrappers as wrappers_mod

    def fake_uninstall_launchd(name):
        call_order.append("unit_uninstall")
        return True

    monkeypatch.setattr(
        wrappers_mod, "_uninstall_launchd_plist", fake_uninstall_launchd
    )

    quiesce_profile("sales", profile_root)

    # Both steps ran AND bot_kill was recorded BEFORE unit_uninstall.
    assert "bot_kill" in call_order, f"step 6 not invoked: {call_order}"
    assert "unit_uninstall" in call_order, (
        f"step 7 not invoked: {call_order}"
    )
    assert call_order.index("bot_kill") < call_order.index("unit_uninstall"), (
        f"step ordering violated: {call_order}"
    )


# ---------------------------------------------------------------------------
# F1 (post-build adversarial review) — Step 5 ABORTS before Step 6 + Step 7
# ---------------------------------------------------------------------------


def test_step5_state_locks_abort_before_bot_kill_and_unit_uninstall(
    empty_homie_root, monkeypatch, live_pid_fixture
):
    """F1 — held state lock raises BEFORE bot kill (Step 6) or unit
    uninstall (Step 7).

    PRP-7b state-machine table line 1982 + the ordering invariant block at
    line 1994 require Step 5 (state-lock check) to abort the ladder BEFORE
    any destructive side effect. Otherwise a profile with a held state
    lock would get its bot terminated and its launchd / systemd unit
    uninstalled, then refuse rmtree — leaving a half-quiesced profile.

    Test setup:
      a) Create a `sales` profile.
      b) Hold a real `shared.file_lock` on a state file via subprocess
         (covers the LIVE writer path — same pattern as
         `test_step5_subprocess_holds_real_state_lock`).
      c) Seed `bot.pid` with the live_pid_fixture's PID so the bot-kill
         path WOULD be triggered if Step 5 didn't abort first.
      d) Patch `os.kill`, GenerateConsoleCtrlEvent, _uninstall_launchd_plist,
         and _uninstall_systemd_unit to record call order.
      e) Force `sys.platform = "darwin"` so the launchd uninstall is the
         scheduled-unit path.
      f) Call `delete_profile("sales", yes=True)`.

    Asserts:
      1. LifecycleError raised with "state locks held".
      2. `os.kill` was NOT invoked.
      3. `_uninstall_launchd_plist` was NOT invoked.
      4. The live bot pid is still alive (no SIGTERM landed on it).
      5. Profile dir is intact.
    """
    info = create_profile("sales", no_alias=True)
    profile_root = info.path
    state_dir = profile_root / "state"
    state_file = state_dir / "dream-state.json"
    state_file.touch()

    # Seed bot.pid with the live_pid_fixture's PID (a real subprocess) so
    # the bot-kill code path would attempt to kill it if Step 5 did NOT
    # abort first.
    live_pid, live_proc = live_pid_fixture
    pid_file = profile_root / "run" / "bot.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(live_pid))

    call_order: list[str] = []

    def fake_kill(pid, sig):
        call_order.append(f"os.kill({pid},{sig})")
        # Don't actually kill — we want the test to prove this never runs.

    monkeypatch.setattr(os, "kill", fake_kill)

    # Win32 path uses GenerateConsoleCtrlEvent / TerminateProcess via
    # ctypes.windll.kernel32 — we route through "darwin" below so the
    # POSIX os.kill branch is the relevant one. But also patch the win32
    # path defensively in case the test runs without monkeypatched
    # platform (it does monkeypatch but better safe than sorry).
    monkeypatch.setattr(sys, "platform", "darwin")

    from personas import wrappers as wrappers_mod

    def fake_uninstall_launchd(name):
        call_order.append(f"_uninstall_launchd_plist({name})")
        return True

    def fake_uninstall_systemd(name):
        call_order.append(f"_uninstall_systemd_unit({name})")
        return True

    monkeypatch.setattr(
        wrappers_mod, "_uninstall_launchd_plist", fake_uninstall_launchd
    )
    monkeypatch.setattr(
        wrappers_mod, "_uninstall_systemd_unit", fake_uninstall_systemd
    )

    # Spawn the lock holder.
    helper = Path(__file__).resolve().parent / "_holders" / "hold_state_lock.py"
    proc = subprocess.Popen(
        [sys.executable, str(helper), str(state_file)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=str(SCRIPTS_DIR),
    )
    try:
        line = proc.stdout.readline().strip()
        assert line == "LOCKED", (
            f"helper did not print LOCKED; got {line!r}; "
            f"stderr={proc.stderr.read() if proc.poll() else '<still running>'}"
        )

        # Action under test — delete must abort at Step 5 BEFORE Step 6/7.
        with pytest.raises(LifecycleError, match="state locks held"):
            delete_profile("sales", yes=True)

        # Step 6 (bot kill) must NOT have run.
        assert not any("os.kill" in c for c in call_order), (
            f"F1 violation: bot was killed despite held state lock. "
            f"call_order={call_order}"
        )
        # Step 7 (unit uninstall) must NOT have run.
        assert not any("uninstall" in c for c in call_order), (
            f"F1 violation: scheduled unit was uninstalled despite "
            f"held state lock. call_order={call_order}"
        )
        # Live subprocess from live_pid_fixture is still running.
        assert live_proc.poll() is None, (
            "live bot subprocess died — Step 5 did not abort before "
            "Step 6 (bot kill) ran"
        )
        # Profile dir is intact.
        assert profile_root.exists()
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
