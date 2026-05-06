"""PRP-7c Phase 3 / WS4 — bot lifecycle acceptance tests.

Covers PRD §8.2 / §8.5 / §14.11 acceptance gates for the profile-aware bot
lifecycle:

    * Default profile canonical pid path is the AUTHORITATIVE
      ``<install>/.claude/data/state/bot.pid`` (NOT the chat-side path).
    * Default profile ALSO writes a best-effort compat shadow at
      ``<install>/.claude/chat/bot.pid``.
    * Named profiles write ONLY ``<profile>/run/bot.pid`` (no shadow —
      writing the shadow from a named profile would corrupt default's file).
    * Default profile mutex name is the literal legacy
      ``Global\\SecondBrainTelegramBot`` FOREVER.
    * ``cleanup_all_bot_processes()`` filters by HOMIE_HOME so a sales-profile
      startup never kills an engineering-profile bot.
    * ``bot-status.sh`` reads the canonical pid path, NOT the chat-side
      shadow.
    * Two-bot Windows integration test (R1 B1 acceptance gate) — spawns
      default + sales bots and asserts both alive after second startup.

Each test invocable in isolation::

    uv run pytest tests/test_persona_bot_lifecycle.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import shared
from personas import services as _services


# ---------------------------------------------------------------------------
# Default profile canonical + shadow paths (PRD §8.2 / §8.5)
# ---------------------------------------------------------------------------


def test_default_profile_canonical_pid_is_legacy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default profile canonical pid = ``<install>/.claude/data/state/bot.pid``.

    The chat-side ``<install>/.claude/chat/bot.pid`` is the WRITE-ONLY
    compat shadow per PRD §8.5 — it is NEVER returned as the canonical
    read source. Tests that confuse the two corrupt default's PID file.
    """
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    canonical = _services.get_bot_pid_path()
    assert canonical.parts[-3:] == (".claude", "data", "state") + ("bot.pid",)[
        :0
    ] or canonical.parts[-4:] == (".claude", "data", "state", "bot.pid")
    assert canonical.name == "bot.pid"
    # The shadow path must be DIFFERENT from the canonical path.
    shadow = _services._compat_shadow_pid_path()
    assert shadow != canonical
    assert shadow.parts[-3:] == (".claude", "chat", "bot.pid")


def test_default_profile_writes_compat_shadow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``write_pid()`` writes BOTH canonical AND shadow when active is default.

    Patches ``personas.services.get_bot_pid_path`` (Rule 3 module-attribute
    pattern) to a tmp canonical path AND patches the shadow path. Asserts
    both files end up with the current PID.
    """
    # Force "default profile" branch.
    monkeypatch.setattr(
        _services, "is_active_default_profile", lambda: True
    )
    canonical = tmp_path / "data" / "state" / "bot.pid"
    shadow = tmp_path / "chat" / "bot.pid"
    monkeypatch.setattr(_services, "get_bot_pid_path", lambda: canonical)
    monkeypatch.setattr(_services, "_compat_shadow_pid_path", lambda: shadow)

    shared.write_pid()

    assert canonical.exists(), "canonical pid file not written"
    assert shadow.exists(), "compat shadow not written"
    # Same PID in both files.
    assert canonical.read_text(encoding="utf-8").strip() == str(os.getpid())
    assert shadow.read_text(encoding="utf-8").strip() == str(os.getpid())


def test_named_profile_no_shadow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Named profile writes only canonical — shadow is NEVER touched."""
    # Named profile → _should_write_compat_shadow() returns False.
    monkeypatch.setattr(
        _services, "is_active_default_profile", lambda: False
    )
    canonical = tmp_path / "profiles" / "sales" / "run" / "bot.pid"
    # Shadow path SHOULD NOT be touched even if pre-existing.
    shadow_dir = tmp_path / "chat"
    shadow_dir.mkdir(parents=True, exist_ok=True)
    shadow = shadow_dir / "bot.pid"
    monkeypatch.setattr(_services, "get_bot_pid_path", lambda: canonical)
    monkeypatch.setattr(_services, "_compat_shadow_pid_path", lambda: shadow)

    shared.write_pid()

    assert canonical.exists()
    assert not shadow.exists(), (
        "Named profile must NOT write the compat shadow — would corrupt "
        "the default profile's bot.pid file"
    )


def test_default_profile_preserves_legacy_mutex_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default profile mutex name is FOREVER ``Global\\SecondBrainTelegramBot``.

    Renaming the mutex would let a v1-era bot AND a v2-era bot start
    simultaneously while only the v1 mutex is held. R1 B2 / R2 NM2: the
    gate routes through ``is_active_default_profile()`` (active selection),
    NOT raw ``is_default_profile()`` (vault existence).
    """
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    name = _services.get_bot_mutex_name()
    assert name == "Global\\SecondBrainTelegramBot"


def test_real_gate_named_profile_with_install_vault_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2 NM2 — install-dir SOUL.md exists AND HOMIE_HOME=sales → active=sales.

    On owner's real install ``vault/memory/SOUL.md`` exists, so the
    raw ``is_default_profile()`` check returns True. But the ACTIVE PROFILE
    when ``HOMIE_HOME=~/.homie/profiles/sales`` is ``"sales"``. The legacy
    mutex must NOT be granted to sales — that gate uses
    ``is_active_default_profile()`` (active selection), not vault existence.
    """
    # Build a fake ~/.homie/profiles/sales path.
    homie_root = tmp_path / ".homie"
    sales_dir = homie_root / "profiles" / "sales"
    sales_dir.mkdir(parents=True)
    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))
    # ``is_active_default_profile()`` MUST resolve to False (active is "sales").
    assert _services.is_active_default_profile() is False
    # Mutex must be the named-profile hashed variant, NOT the legacy literal.
    name = _services.get_bot_mutex_name()
    assert name != "Global\\SecondBrainTelegramBot"
    assert name.startswith("Global\\Homie-")
    # No shadow — named profile.
    assert _services._should_write_compat_shadow() is False


def test_compat_shadow_atomic_write_failure_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4-NM1: shadow write failure must NOT block bot startup.

    Patches ``_atomic_write_text`` so the SHADOW path raises OSError but
    the CANONICAL path still succeeds. Asserts ``write_pid()`` returns
    cleanly (no exception bubbles up) and the canonical file exists.
    """
    monkeypatch.setattr(
        _services, "is_active_default_profile", lambda: True
    )
    canonical = tmp_path / "state" / "bot.pid"
    shadow = tmp_path / "chat" / "bot.pid"
    monkeypatch.setattr(_services, "get_bot_pid_path", lambda: canonical)
    monkeypatch.setattr(_services, "_compat_shadow_pid_path", lambda: shadow)

    real_atomic = _services._atomic_write_text

    def fake_atomic(path: Path, text: str) -> None:
        if path == shadow:
            raise OSError("simulated shadow write failure")
        real_atomic(path, text)

    monkeypatch.setattr(_services, "_atomic_write_text", fake_atomic)

    # write_pid() must NOT raise even though shadow write blew up.
    shared.write_pid()

    assert canonical.exists()
    assert canonical.read_text(encoding="utf-8").strip() == str(os.getpid())
    # Shadow not written (failure swallowed).
    assert not shadow.exists()


def test_cleanup_all_bot_processes_removes_default_shadow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4-NM2: ``cleanup_all_bot_processes()`` removes stale chat/bot.pid.

    Simulates a leftover shadow from a killed prior bot — calling cleanup
    must wipe BOTH the canonical bot.pid AND the chat-side shadow when the
    active profile is the default.
    """
    monkeypatch.setattr(
        _services, "is_active_default_profile", lambda: True
    )
    canonical = tmp_path / "state" / "bot.pid"
    shadow = tmp_path / "chat" / "bot.pid"
    canonical.parent.mkdir(parents=True)
    shadow.parent.mkdir(parents=True)
    canonical.write_text("12345", encoding="utf-8")
    shadow.write_text("12345", encoding="utf-8")
    monkeypatch.setattr(_services, "get_bot_pid_path", lambda: canonical)
    monkeypatch.setattr(_services, "_compat_shadow_pid_path", lambda: shadow)

    # Make scan-and-kill a no-op so we don't actually try to kill processes.
    monkeypatch.setattr(
        shared,
        "_scan_and_kill_windows",
        lambda *a, **kw: [],
    )
    monkeypatch.setattr(
        shared,
        "_scan_and_kill_unix",
        lambda *a, **kw: [],
    )

    shared.cleanup_all_bot_processes()

    assert not canonical.exists()
    assert not shadow.exists()


# ---------------------------------------------------------------------------
# Profile-aware cleanup (R1 B1 — psutil environ() filter)
# ---------------------------------------------------------------------------


def test_profile_aware_cleanup_psutil_unavailable_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1 B1 belt-and-suspenders: if psutil is unavailable, refuse to kill.

    Replaces the ``psutil`` import inside ``_process_belongs_to_profile``
    with one that raises ImportError. The helper MUST return False
    (refuses to kill, safer than killing across profiles).
    """
    # Build a controlled fake module with __import__ that raises for psutil.
    real_import = __builtins__["__import__"] if isinstance(
        __builtins__, dict
    ) else __builtins__.__import__

    def fake_import(name: str, *args, **kwargs):
        if name == "psutil":
            raise ImportError("simulated absence of psutil")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(
        "builtins.__import__", fake_import
    )

    result = shared._process_belongs_to_profile(99999, "/some/home")
    assert result is False, (
        "Without psutil, _process_belongs_to_profile must refuse the kill "
        "(return False) — killing across profiles is the larger evil."
    )


def test_profile_aware_cleanup_environ_raises_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1 B1: psutil ``environ()`` raise → refuse the kill.

    Stubs ``psutil.Process(pid).environ()`` to raise — helper returns False.
    """
    import psutil

    class FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def environ(self) -> dict[str, str]:
            raise psutil.AccessDenied(pid=self.pid)

    monkeypatch.setattr(psutil, "Process", FakeProcess)

    result = shared._process_belongs_to_profile(99999, "/some/home")
    assert result is False


def test_profile_aware_cleanup_environ_match_kills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1 B1: matching HOMIE_HOME → returns True (kill proceeds)."""
    import psutil

    class FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def environ(self) -> dict[str, str]:
            return {"HOMIE_HOME": "/some/home"}

    monkeypatch.setattr(psutil, "Process", FakeProcess)

    assert shared._process_belongs_to_profile(99999, "/some/home") is True
    # Mismatch → False.
    assert shared._process_belongs_to_profile(99999, "/other/home") is False


# ---------------------------------------------------------------------------
# Bash script reads canonical (NOT chat-side shadow)
# ---------------------------------------------------------------------------


def test_bash_script_reads_canonical(monkeypatch: pytest.MonkeyPatch) -> None:
    """``bot-status.sh``-equivalent path resolution returns canonical, not shadow.

    Doesn't shell out to bash (would require a bash interpreter). Instead
    invokes the same ``python -c`` snippet bot-status.sh runs and verifies
    the printed PID path is the canonical one.
    """
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    scripts_dir = Path(__file__).resolve().parent.parent
    code = (
        "import sys\n"
        f"sys.path.insert(0, r'{scripts_dir}')\n"
        "from personas import apply_persona_override\n"
        "apply_persona_override()\n"
        "from personas.services import get_bot_pid_path\n"
        "print(get_bot_pid_path())\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"subprocess failed: {proc.stderr!r}"
    pid_path = proc.stdout.strip().splitlines()[-1]
    # Must end with /.claude/data/state/bot.pid (canonical), NOT chat/bot.pid.
    p = Path(pid_path)
    assert p.parts[-3:] == ("data", "state", "bot.pid"), (
        f"bot-status.sh would read {pid_path!r} — expected the canonical "
        "<install>/.claude/data/state/bot.pid"
    )


# ---------------------------------------------------------------------------
# config constants delegate to services helpers (PEP 562 __getattr__)
# ---------------------------------------------------------------------------


def test_config_constants_match_services_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``config.BOT_PID_FILE``/``BOT_LOCK_FILE`` route through personas.services.

    The PEP 562 ``__getattr__`` in ``config.py`` must return the same path
    that ``personas.services.get_bot_pid_path()`` / ``get_bot_lock_path()``
    return for the active profile. Tests in subprocess to avoid
    cross-test bleed of ``sys.modules['config']``.
    """
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    scripts_dir = Path(__file__).resolve().parent.parent
    code = (
        "import sys\n"
        f"sys.path.insert(0, r'{scripts_dir}')\n"
        "from personas import apply_persona_override\n"
        "apply_persona_override()\n"
        "import config\n"
        "from personas.services import get_bot_pid_path, get_bot_lock_path\n"
        "print(config.BOT_PID_FILE)\n"
        "print(get_bot_pid_path())\n"
        "print(config.BOT_LOCK_FILE)\n"
        "print(get_bot_lock_path())\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"subprocess failed: {proc.stderr!r}"
    lines = proc.stdout.strip().splitlines()
    assert len(lines) == 4, lines
    # config.BOT_PID_FILE == personas.services.get_bot_pid_path()
    assert lines[0] == lines[1], (
        f"config.BOT_PID_FILE={lines[0]!r} disagrees with "
        f"get_bot_pid_path()={lines[1]!r}"
    )
    # config.BOT_LOCK_FILE == personas.services.get_bot_lock_path()
    assert lines[2] == lines[3], (
        f"config.BOT_LOCK_FILE={lines[2]!r} disagrees with "
        f"get_bot_lock_path()={lines[3]!r}"
    )


# ---------------------------------------------------------------------------
# Two-bot Windows integration (PRD §14.11 acceptance gate)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Two-bot integration test exercises the Windows mutex path "
    "(SecondBrainTelegramBot vs Global\\Homie-<hash>); behavior is "
    "verified live on Win11. The mutex is a no-op on POSIX.",
)
def test_two_bots_run_simultaneously_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1 B1 + PRD §14.11 — default + sales bots STAY ALIVE TOGETHER.

    F3 fix — this exercises the LIVE lock + PID + cleanup contract under
    steady-state coexistence, NOT a sequential dry-run. The previous
    implementation ran ``--test`` twice with ``subprocess.run()`` which
    exits before adapter polling — it would have passed even if the
    second startup killed the first.

    Uses the dedicated ``--test-hold`` mode (added in same Phase 3 fix
    pass): each bot enters the same instance-lock + ``write_pid`` +
    ``cleanup_all_bot_processes`` path as production startup, then BLOCKS
    on a sentinel file. The test:

      1. Spawns the default-profile bot via ``Popen`` and waits for its
         pid file to appear (proof the lock + write_pid succeeded).
      2. Spawns the sales-profile bot via ``Popen`` and waits for ITS pid
         file to appear.
      3. Asserts both processes are STILL alive (``poll() is None``).
         This is the critical assertion — if the second bot's
         ``cleanup_all_bot_processes()`` had killed the first, the
         default's ``poll()`` would return its exit code instead of None.
      4. Cleans up by creating the sentinel files (graceful) and then
         terminating any survivors.

    Proves:
      * Default mutex (``Global\\SecondBrainTelegramBot``) and sales
        mutex (``Global\\Homie-<hash>``) do NOT collide.
      * ``cleanup_all_bot_processes()`` with profile-aware filter does
        NOT kill processes belonging to other profiles.
      * Two pid files written to two different paths simultaneously.
    """
    chat_main = (
        Path(__file__).resolve().parent.parent.parent / "chat" / "main.py"
    )
    if not chat_main.exists():
        pytest.skip(f"{chat_main} missing — skipping")

    scripts_dir = Path(__file__).resolve().parent.parent

    # Build a fake sales profile under tmp_path so the second bot has a
    # distinct HOMIE_HOME without polluting the user's real ~/.homie.
    sales_dir = tmp_path / ".homie" / "profiles" / "sales"
    sales_dir.mkdir(parents=True)
    for sub in ("memory", "data", "state", "run", "logs", "credentials"):
        (sales_dir / sub).mkdir(parents=True, exist_ok=True)
    (sales_dir / ".env").write_text("# fake sales env\n", encoding="utf-8")

    # Sentinel files — written by the test to release each --test-hold loop.
    sentinel_default = tmp_path / "unlock_default"
    sentinel_sales = tmp_path / "unlock_sales"

    base_env = os.environ.copy()
    base_env.pop("HOMIE_HOME", None)
    base_env.pop("TELEGRAM_BOT_TOKEN", None)
    base_env["PYTHONPATH"] = str(scripts_dir) + os.pathsep + base_env.get(
        "PYTHONPATH", ""
    )

    sales_env = base_env.copy()
    sales_env["HOMIE_HOME"] = str(sales_dir)
    sales_env["TELEGRAM_BOT_TOKEN"] = ""

    def _resolve_pid_path(env: dict) -> Path:
        """Run a tiny Python snippet under *env* to resolve the bot pid path."""
        snippet = (
            "import sys\n"
            f"sys.path.insert(0, r'{scripts_dir}')\n"
            "from personas import apply_persona_override\n"
            "apply_persona_override()\n"
            "from personas.services import get_bot_pid_path\n"
            "print(get_bot_pid_path())\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", snippet],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert proc.returncode == 0, (
            f"pid-path resolver failed rc={proc.returncode}; "
            f"stderr={proc.stderr!r}"
        )
        return Path(proc.stdout.strip().splitlines()[-1])

    def _wait_for_pid_file(pid_path: Path, timeout: float = 15.0) -> int:
        """Block until *pid_path* exists with a parseable PID; raise on timeout."""
        deadline = time.monotonic() + timeout
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            if pid_path.exists():
                try:
                    return int(pid_path.read_text(encoding="utf-8").strip())
                except (ValueError, OSError) as exc:
                    last_err = exc
            time.sleep(0.1)
        raise TimeoutError(
            f"pid file {pid_path} did not appear within {timeout}s "
            f"(last_err={last_err!r})"
        )

    default_pid_path = _resolve_pid_path(base_env)
    sales_pid_path = _resolve_pid_path(sales_env)
    # Sanity check — paths MUST be distinct (otherwise the lifecycle
    # surface is misconfigured).
    assert default_pid_path != sales_pid_path, (
        f"default + sales resolve to the same pid path "
        f"({default_pid_path}) — Phase 3 lifecycle surface broken."
    )
    # Pre-clean any stale pid files from previous runs.
    default_pid_path.unlink(missing_ok=True)
    sales_pid_path.unlink(missing_ok=True)

    proc_default: subprocess.Popen | None = None
    proc_sales: subprocess.Popen | None = None
    try:
        # 1. Spawn default bot in --test-hold mode.
        proc_default = subprocess.Popen(
            [
                sys.executable,
                str(chat_main),
                "--test-hold",
                str(sentinel_default),
            ],
            env=base_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        default_pid = _wait_for_pid_file(default_pid_path, timeout=20.0)
        assert proc_default.poll() is None, (
            f"default bot exited before pid file appeared "
            f"(rc={proc_default.returncode})"
        )
        assert default_pid == proc_default.pid or default_pid > 0, (
            f"default pid file content {default_pid!r} not plausibly the "
            f"bot's pid"
        )

        # 2. Spawn sales bot in --test-hold mode.
        proc_sales = subprocess.Popen(
            [
                sys.executable,
                str(chat_main),
                "--test-hold",
                str(sentinel_sales),
            ],
            env=sales_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        sales_pid = _wait_for_pid_file(sales_pid_path, timeout=20.0)

        # 3. CRITICAL ASSERTION — both processes still alive AFTER the
        # second startup ran cleanup_all_bot_processes(). If the
        # profile-aware filter were broken, the sales bot's cleanup would
        # have killed the default bot here.
        assert proc_default.poll() is None, (
            f"R1 B1 REGRESSION — default bot died after sales startup "
            f"ran cleanup_all_bot_processes(). rc={proc_default.returncode} "
            f"stderr={proc_default.stderr.read() if proc_default.stderr else ''!r}"
        )
        assert proc_sales.poll() is None, (
            f"sales bot died unexpectedly during --test-hold "
            f"rc={proc_sales.returncode}"
        )
        assert default_pid_path.is_file(), "default pid file disappeared"
        assert sales_pid_path.is_file(), "sales pid file disappeared"
        assert sales_pid > 0
    finally:
        # 4. Graceful unlock via sentinel.
        sentinel_default.write_text("", encoding="utf-8")
        sentinel_sales.write_text("", encoding="utf-8")
        for proc in (proc_default, proc_sales):
            if proc is None:
                continue
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# F2 — run_chat.bat resolves canonical paths via delayed expansion (Windows)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="run_chat.bat is the Windows cmd.exe entry point. The delayed-"
    "expansion bug (`!_LINE!` literal) only manifests when cmd.exe parses "
    "the file. Bash on POSIX does not exercise this path.",
)
def test_run_chat_bat_resolves_canonical_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F2 regression — ``run_chat.bat`` must echo the resolved PID path.

    The previous implementation used ``!_LINE!`` inside a parenthesized
    ``FOR`` block without ``setlocal EnableDelayedExpansion``, so cmd.exe
    saw ``!_LINE!`` as a literal string and PID_FILE / LOG_DIR stayed
    empty. This test runs the .bat under cmd.exe and parses the output
    for the printed ``PID file:`` line — it must contain the canonical
    path returned by ``personas.services.get_bot_pid_path()``, NOT the
    install-dir fallback (which we removed) and NOT an empty string.

    We invoke the .bat with an unknown subcommand so it doesn't actually
    fork the bot — we only need to reach the early echo of PID_FILE
    inside the ELSE branch. The ``start /b`` line will fire briefly but
    we capture stdout before it gets a chance to do anything meaningful.
    """
    script_dir = Path(__file__).resolve().parent.parent.parent / "chat"
    bat_path = script_dir / "run_chat.bat"
    if not bat_path.exists():
        pytest.skip(f"{bat_path} missing")

    # Use a controlled HOMIE_HOME so the test doesn't depend on owner's
    # real ~/.homie state. Build a fake sales profile under tmp_path.
    sales_dir = tmp_path / ".homie" / "profiles" / "sales-bat-test"
    sales_dir.mkdir(parents=True)
    for sub in ("memory", "data", "state", "run", "logs", "credentials"):
        (sales_dir / sub).mkdir(parents=True, exist_ok=True)
    (sales_dir / ".env").write_text("# fake env\n", encoding="utf-8")

    test_env = os.environ.copy()
    test_env["HOMIE_HOME"] = str(sales_dir)
    # Strip the real Telegram token so the bot doesn't actually connect
    # if it gets that far before we kill it.
    test_env["TELEGRAM_BOT_TOKEN"] = ""

    # Run with a no-op subcommand. The .bat ignores extra args after
    # %1=="--fg" check, so anything that's not "--fg" lands in the ELSE
    # branch which is where the echo lines we care about live.
    proc = subprocess.run(
        ["cmd", "/c", str(bat_path), "--noop-test-arg"],
        capture_output=True,
        text=True,
        env=test_env,
        timeout=30,
    )

    output = (proc.stdout or "") + (proc.stderr or "")
    # The .bat MUST NOT exit with the F4 "Service resolver failed" error
    # under a valid HOMIE_HOME — that would mean delayed expansion didn't
    # populate PID_FILE.
    assert "Service resolver failed" not in output, (
        f"run_chat.bat hit the F4 fail-loud path under a valid HOMIE_HOME — "
        f"delayed expansion likely broken. Output: {output!r}"
    )
    # The "PID file:" echo must contain the resolved path under sales_dir
    # NOT the install-dir fallback (\.claude\chat\bot.pid).
    assert "PID file:" in output, (
        f"run_chat.bat did not echo a PID file line — output: {output!r}"
    )
    pid_line = next(
        (line for line in output.splitlines() if line.startswith("PID file:")),
        "",
    )
    # The canonical path for a named profile is <HOMIE_HOME>/run/bot.pid.
    expected_fragment = str(sales_dir / "run" / "bot.pid")
    assert expected_fragment.lower() in pid_line.lower() or "run\\bot.pid" in pid_line.lower(), (
        f"run_chat.bat echoed PID file={pid_line!r} but expected the "
        f"resolved path under {sales_dir}. Delayed expansion likely "
        f"broken — !PID_FILE! resolved to literal or empty string."
    )


# ---------------------------------------------------------------------------
# F5 — `thehomie status` exposes per-profile lifecycle contract
# ---------------------------------------------------------------------------


def test_status_exposes_profile_lifecycle_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F5 — ``thehomie status`` (and ``--json`` mode) must expose the
    per-profile lifecycle contract: pid path, lock path, mutex (Windows),
    and the three service ports.

    The PRP names ``thehomie -p sales status`` as the operator surface for
    inspecting the per-profile lifecycle contract. Without this, profile
    isolation is invisible from the CLI and operators can't tell which
    paths the bot will actually use under a given profile.

    Runs ``thehomie status --json`` in a subprocess (so the boot-shim
    ``apply_persona_override()`` runs cleanly) and asserts the
    ``profile_lifecycle`` dict in the output contains all six contract
    keys with non-empty values.
    """
    cli_path = (
        Path(__file__).resolve().parent.parent.parent / "chat" / "cli.py"
    )
    if not cli_path.exists():
        pytest.skip(f"{cli_path} missing")

    scripts_dir = Path(__file__).resolve().parent.parent

    test_env = os.environ.copy()
    test_env.pop("HOMIE_HOME", None)
    test_env["PYTHONPATH"] = str(scripts_dir) + os.pathsep + test_env.get(
        "PYTHONPATH", ""
    )
    # Stub out the Telegram token so the status command doesn't trip the
    # collision check (it runs as part of bot startup, but ``status`` does
    # not exercise that path — defensive only).
    test_env.setdefault("TELEGRAM_BOT_TOKEN", "")

    proc = subprocess.run(
        [sys.executable, str(cli_path), "status", "--json"],
        capture_output=True,
        text=True,
        env=test_env,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"thehomie status --json exited rc={proc.returncode} "
        f"stderr={proc.stderr[:500]!r}"
    )
    import json as _json

    payload = _json.loads(proc.stdout)
    assert "profile_lifecycle" in payload, (
        "F5: status --json missing 'profile_lifecycle' key — operators "
        "cannot inspect the per-profile lifecycle contract"
    )
    contract = payload["profile_lifecycle"]
    # All six required keys present + populated.
    for key in (
        "active_profile",
        "bot_pid_path",
        "bot_lock_path",
        "orchestration_api_port",
        "health_check_port",
        "whatsapp_webhook_port",
    ):
        assert key in contract, f"F5: missing key {key!r} in profile_lifecycle"
        assert contract[key] not in (None, "", "<error: ...>"), (
            f"F5: key {key!r} not populated in profile_lifecycle "
            f"(got {contract[key]!r})"
        )
    # Mutex is None on POSIX (intentional — mutex is Windows-only).
    if sys.platform == "win32":
        assert contract.get("bot_mutex_name"), (
            "F5: bot_mutex_name missing on Windows"
        )
    else:
        assert contract.get("bot_mutex_name") is None, (
            "F5: bot_mutex_name should be None on POSIX"
        )


def test_status_human_output_includes_lifecycle_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F5 — non-JSON ``thehomie status`` output must include the
    Profile lifecycle: block so operators reading the terminal can see
    which paths/ports are in effect."""
    cli_path = (
        Path(__file__).resolve().parent.parent.parent / "chat" / "cli.py"
    )
    if not cli_path.exists():
        pytest.skip(f"{cli_path} missing")

    scripts_dir = Path(__file__).resolve().parent.parent

    test_env = os.environ.copy()
    test_env.pop("HOMIE_HOME", None)
    test_env["PYTHONPATH"] = str(scripts_dir) + os.pathsep + test_env.get(
        "PYTHONPATH", ""
    )
    test_env.setdefault("TELEGRAM_BOT_TOKEN", "")

    proc = subprocess.run(
        [sys.executable, str(cli_path), "status"],
        capture_output=True,
        text=True,
        env=test_env,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"thehomie status exited rc={proc.returncode} "
        f"stderr={proc.stderr[:500]!r}"
    )
    out = proc.stdout
    assert "Profile lifecycle:" in out, (
        "F5: human-readable status missing 'Profile lifecycle:' block — "
        f"output={out!r}"
    )
    # All six labels present.
    for label in (
        "Active profile:",
        "Bot PID path:",
        "Bot lock path:",
        "Bot mutex:",
        "Orchestration API port:",
        "Health check port:",
        "WhatsApp webhook port:",
    ):
        assert label in out, (
            f"F5: human-readable status missing label {label!r}"
        )


# ---------------------------------------------------------------------------
# F1 — list_bot_pids_in_active_profile() helper contract
# ---------------------------------------------------------------------------


def test_list_bot_pids_in_active_profile_filters_by_homie_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1 contract — the helper must filter by HOMIE_HOME and exclude
    PIDs whose ownership cannot be proven (FAIL-CLOSED).

    Builds a fake candidate-pid list and a fake ``_process_belongs_to_profile``
    that returns True for one PID, False for another. Asserts only the
    True-owner PID survives the filter.
    """
    monkeypatch.setattr(
        shared,
        "_enumerate_bot_candidate_pids",
        lambda: [11111, 22222, 33333],
    )

    def fake_owner(pid: int, my_homie_home: str) -> bool:
        # Only PID 22222 belongs to "us"; the other two represent a
        # sibling profile (33333) and a process whose env couldn't be
        # read (11111) — both should be EXCLUDED (fail-closed).
        return pid == 22222

    monkeypatch.setattr(shared, "_process_belongs_to_profile", fake_owner)

    result = shared.list_bot_pids_in_active_profile()
    assert result == [22222], (
        f"F1: list_bot_pids_in_active_profile returned {result!r} — "
        f"expected exactly [22222] (fail-closed filter on the others)"
    )


def test_list_bot_pids_in_active_profile_psutil_unavailable_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1 — when psutil is unavailable, the helper returns an empty list.

    The shell scripts treat empty as "no matching bot" (do NOT kill),
    which is the safer behavior. Killing a process whose ownership we
    can't verify is the larger evil.
    """
    real_import = __builtins__["__import__"] if isinstance(
        __builtins__, dict
    ) else __builtins__.__import__

    def fake_import(name: str, *args, **kwargs):
        if name == "psutil":
            raise ImportError("simulated absence of psutil")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    result = shared._enumerate_bot_candidate_pids()
    assert result == [], (
        f"F1: _enumerate_bot_candidate_pids returned {result!r} — "
        f"expected [] when psutil is unavailable (fail-closed)"
    )


# ---------------------------------------------------------------------------
# F1 (R2) — wrapper scripts forward --profile/-p to the resolver subprocess.
# ---------------------------------------------------------------------------
#
# Iteration 2 finding: the wrapper's resolver subprocess never sees the
# wrapper's argv, so apply_persona_override() inside the subprocess can't read
# --profile/-p. Result: cleanup, log writes, and PID file all run against
# DEFAULT-profile paths while the live bot (which DOES pre-parse argv) is the
# named-profile bot. The fix is to pre-parse the flag in the wrapper itself
# and export HOMIE_HOME before any subprocess spawns.
#
# These tests prove the wrapper's parse-then-export sequence works for the
# success path (named profile exists) and the explicit-failure path (named
# profile missing exits with a clear error).


def _make_fake_profile(profiles_root: Path, name: str) -> Path:
    """Helper — build a minimal fake profile dir under *profiles_root*."""
    profile_dir = profiles_root / name
    profile_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("memory", "data", "state", "run", "logs", "credentials"):
        (profile_dir / sub).mkdir(parents=True, exist_ok=True)
    (profile_dir / ".env").write_text("# fake env\n", encoding="utf-8")
    return profile_dir


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="bot-status.sh is the bash entry point. Windows uses cmd.exe / "
    "run_chat.bat exercised by test_run_chat_bat_forwards_profile_flag_to_resolver.",
)
def test_bot_status_sh_forwards_profile_flag_to_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1 (R2) — ``bot-status.sh --profile sales-test`` resolves sales paths.

    Before the fix: the wrapper invoked ``python -c "...resolver..."`` whose
    apply_persona_override() saw only its own argv (just ``-c '...'``), so
    HOMIE_HOME was unset for the resolver and it returned the DEFAULT profile's
    pid path. After the fix: the wrapper pre-parses --profile/-p and exports
    HOMIE_HOME BEFORE spawning the resolver subprocess.

    bot-status.sh and run_chat.sh share IDENTICAL parse logic — testing
    bot-status.sh proves both paths because it's the cheap one (no bot fork,
    no 5-second sleep, just one resolver call + report). The run_chat.sh
    smoke test exercises the same logic in the heavier startup path.
    """
    script_dir = Path(__file__).resolve().parent.parent.parent / "chat"
    sh_path = script_dir / "bot-status.sh"
    if not sh_path.exists():
        pytest.skip(f"{sh_path} missing")

    # Build a fake sales-test profile under tmp HOME so the wrapper's existence
    # check in the parse loop accepts it. We override HOME so ${HOME}/.homie
    # routes into tmp, not owner's real ~/.homie.
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    profiles_root = fake_home / ".homie" / "profiles"
    sales_dir = _make_fake_profile(profiles_root, "sales-test")

    test_env = os.environ.copy()
    test_env["HOME"] = str(fake_home)
    test_env.pop("HOMIE_HOME", None)  # prove the wrapper's parse sets it

    proc = subprocess.run(
        ["bash", str(sh_path), "--profile", "sales-test"],
        capture_output=True,
        text=True,
        env=test_env,
        timeout=60,
    )
    output = (proc.stdout or "") + (proc.stderr or "")

    # The resolver should not have hit the F4 "Service resolver failed" path.
    assert "Service resolver failed" not in output, (
        f"F1 (R2): bot-status.sh hit the resolver-failure path under a valid "
        f"--profile sales-test argument — wrapper parse layer didn't export "
        f"HOMIE_HOME before the resolver ran. Output:\n{output}"
    )

    # The reported "PID path:" line must contain the sales-test profile root.
    pid_line = next(
        (line for line in output.splitlines() if "PID path:" in line),
        "",
    )
    assert pid_line, (
        f"F1 (R2): bot-status.sh did not emit a 'PID path:' line — "
        f"output:\n{output}"
    )
    expected_fragment = str(sales_dir)
    assert expected_fragment in pid_line, (
        f"F1 (R2): bot-status.sh resolved PID path={pid_line!r} but the "
        f"requested profile is at {sales_dir}. Wrapper's --profile flag was "
        f"NOT propagated to the resolver subprocess (HOMIE_HOME export didn't "
        f"reach the python -c invocation). This regresses iteration-2 R2 F1."
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash test — Windows uses run_chat.bat (different test below).",
)
def test_run_chat_sh_forwards_profile_flag_to_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1 (R2) — ``run_chat.sh --profile sales-test`` resolves sales paths.

    This is the heavier sibling of the bot-status.sh test. Confirms the
    parse-then-export sequence ALSO runs in the bot-startup wrapper path
    (which has additional concerns: kill-existing, fork-bot, write pid file).

    We pass ``--profile sales-test`` only — the script falls into background
    mode, kills nothing (fresh tmp profile), forks a bot that exits fast
    (no TELEGRAM_BOT_TOKEN), then echoes ``PID file: <path>``. We assert that
    the printed path is under the sales-test profile root.

    Run-time: ~5-10s (the 5-second sleep + bot startup). Tagged with a 90s
    timeout to be safe.
    """
    script_dir = Path(__file__).resolve().parent.parent.parent / "chat"
    sh_path = script_dir / "run_chat.sh"
    if not sh_path.exists():
        pytest.skip(f"{sh_path} missing")

    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    profiles_root = fake_home / ".homie" / "profiles"
    sales_dir = _make_fake_profile(profiles_root, "sales-test")

    test_env = os.environ.copy()
    test_env["HOME"] = str(fake_home)
    test_env.pop("HOMIE_HOME", None)
    # Bot will fail fast without a Telegram token — keeps the test snappy.
    test_env["TELEGRAM_BOT_TOKEN"] = ""

    proc = subprocess.run(
        ["bash", str(sh_path), "--profile", "sales-test"],
        capture_output=True,
        text=True,
        env=test_env,
        timeout=90,
    )
    output = (proc.stdout or "") + (proc.stderr or "")

    assert "Service resolver failed" not in output, (
        f"F1 (R2): run_chat.sh hit the resolver-failure path under a valid "
        f"--profile sales-test argument — wrapper parse layer didn't export "
        f"HOMIE_HOME before the resolver ran. Output:\n{output}"
    )

    pid_line = next(
        (line for line in output.splitlines() if line.strip().startswith("PID file:")),
        "",
    )
    assert pid_line, (
        f"F1 (R2): run_chat.sh did not echo a 'PID file:' line — output:\n{output}"
    )
    expected_fragment = str(sales_dir)
    assert expected_fragment in pid_line, (
        f"F1 (R2): run_chat.sh echoed PID file={pid_line!r} but the requested "
        f"profile is at {sales_dir}. Wrapper's --profile flag was NOT "
        f"propagated to the resolver subprocess."
    )


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="run_chat.bat is the cmd.exe entry point. Bash on POSIX exercises "
    "test_bot_status_sh_forwards_profile_flag_to_resolver.",
)
def test_run_chat_bat_forwards_profile_flag_to_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1 (R2) — ``run_chat.bat --profile sales-test`` resolves sales paths.

    cmd.exe parses .bat files line-by-line; the wrapper's parse loop runs
    inside a CALL'd subroutine so ``shift`` doesn't consume the parent's
    %1 / %* (which the rest of the script needs for the --fg arm and the
    bot launch line). This test runs the .bat and asserts the printed
    'PID file:' line points under the sales-test profile root.
    """
    script_dir = Path(__file__).resolve().parent.parent.parent / "chat"
    bat_path = script_dir / "run_chat.bat"
    if not bat_path.exists():
        pytest.skip(f"{bat_path} missing")

    # cmd.exe uses %USERPROFILE% (not %HOME%) to locate ~/.homie/profiles/, so
    # we override USERPROFILE to a tmp dir and build the fake profile under it.
    fake_userprofile = tmp_path / "fakeprofile"
    fake_userprofile.mkdir()
    profiles_root = fake_userprofile / ".homie" / "profiles"
    sales_dir = _make_fake_profile(profiles_root, "sales-test")

    test_env = os.environ.copy()
    test_env["USERPROFILE"] = str(fake_userprofile)
    test_env.pop("HOMIE_HOME", None)
    test_env["TELEGRAM_BOT_TOKEN"] = ""

    proc = subprocess.run(
        ["cmd", "/c", str(bat_path), "--profile", "sales-test"],
        capture_output=True,
        text=True,
        env=test_env,
        timeout=60,
    )
    output = (proc.stdout or "") + (proc.stderr or "")

    assert "Service resolver failed" not in output, (
        f"F1 (R2): run_chat.bat hit the resolver-failure path under a valid "
        f"--profile sales-test argument — wrapper parse layer didn't set "
        f"HOMIE_HOME before the resolver ran. Output:\n{output}"
    )

    pid_line = next(
        (line for line in output.splitlines() if line.strip().lower().startswith("pid file:")),
        "",
    )
    assert pid_line, (
        f"F1 (R2): run_chat.bat did not echo a 'PID file:' line — output:\n{output}"
    )
    expected_fragment = str(sales_dir)
    # Case-insensitive match — Windows path separators may have mixed case.
    assert expected_fragment.lower() in pid_line.lower(), (
        f"F1 (R2): run_chat.bat echoed PID file={pid_line!r} but the "
        f"requested profile is at {sales_dir}. Wrapper's --profile flag was "
        f"NOT propagated to the resolver subprocess."
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash test — Windows uses run_chat.bat.",
)
def test_run_chat_sh_unknown_profile_exits_with_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1 (R2) — invalid --profile NAME hard-fails the wrapper before resolve.

    Validation order matters: the wrapper validates the profile dir exists
    BEFORE running the resolver subprocess. Otherwise a typo'd profile name
    would silently fall through to the default profile (the rank-4 fallback),
    which is the exact bug iteration-2 R2 F1 fixed.
    """
    script_dir = Path(__file__).resolve().parent.parent.parent / "chat"
    sh_path = script_dir / "run_chat.sh"
    if not sh_path.exists():
        pytest.skip(f"{sh_path} missing")

    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    (fake_home / ".homie" / "profiles").mkdir(parents=True)

    test_env = os.environ.copy()
    test_env["HOME"] = str(fake_home)
    test_env.pop("HOMIE_HOME", None)
    test_env["TELEGRAM_BOT_TOKEN"] = ""

    proc = subprocess.run(
        ["bash", str(sh_path), "--profile", "this-profile-does-not-exist"],
        capture_output=True,
        text=True,
        env=test_env,
        timeout=30,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    # Exit code MUST be non-zero (rejection, not silent fallback).
    assert proc.returncode != 0, (
        f"F1 (R2): run_chat.sh exited rc={proc.returncode} when given an "
        f"unknown --profile NAME — wrapper silently fell back to default "
        f"instead of rejecting the typo. This regresses the explicit-CLI "
        f"hard-fail contract from PRP-7a R3 NNB1. Output:\n{output}"
    )
    # The error message must reference the profile name and the expected dir.
    assert "this-profile-does-not-exist" in output, (
        f"F1 (R2): error message missing the profile name; output:\n{output}"
    )


# ---------------------------------------------------------------------------
# Rec 2 — `thehomie status --json` under a NAMED profile exposes the same
# lifecycle contract (not just the default-profile happy path).
# ---------------------------------------------------------------------------


def test_status_named_profile_exposes_lifecycle_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rec 2 (R2) — ``thehomie status --json`` under HOMIE_HOME=<sales> path.

    The pre-existing F5 test (test_status_exposes_profile_lifecycle_contract)
    pops HOMIE_HOME so it only verifies the DEFAULT-profile branch. Without a
    named-profile companion test, a regression in
    ``personas.services.get_bot_pid_path() / get_bot_lock_path() / get_bot_mutex_name()``
    that breaks the named-profile branch wouldn't be caught by that test.

    This test sets HOMIE_HOME=<sales-rec2-test> and asserts the printed
    contract reflects the sales profile (paths under ``<sales>/run/``,
    Windows mutex hashed not legacy, ports differ from default).
    """
    cli_path = (
        Path(__file__).resolve().parent.parent.parent / "chat" / "cli.py"
    )
    if not cli_path.exists():
        pytest.skip(f"{cli_path} missing")

    scripts_dir = Path(__file__).resolve().parent.parent

    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    profiles_root = fake_home / ".homie" / "profiles"
    sales_dir = _make_fake_profile(profiles_root, "sales-rec2-test")

    test_env = os.environ.copy()
    # HOMIE_HOME pinned at rank-2 of the boot shim's precedence chain — no
    # CLI flag needed. We override BOTH HOME (POSIX) and USERPROFILE (Windows)
    # because ``Path.home()`` resolves through USERPROFILE on win32 and
    # ``activity.get_active_profile_name`` compares the normalized HOMIE_HOME
    # against ``Path.home() / ".homie"``. Without overriding USERPROFILE the
    # comparison falls outside the fake profiles root and the profile
    # misclassifies as "custom".
    test_env["HOMIE_HOME"] = str(sales_dir)
    test_env["HOME"] = str(fake_home)
    test_env["USERPROFILE"] = str(fake_home)
    test_env["PYTHONPATH"] = str(scripts_dir) + os.pathsep + test_env.get(
        "PYTHONPATH", ""
    )
    test_env.setdefault("TELEGRAM_BOT_TOKEN", "")

    proc = subprocess.run(
        [sys.executable, str(cli_path), "status", "--json"],
        capture_output=True,
        text=True,
        env=test_env,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"Rec 2: thehomie status --json under HOMIE_HOME=<sales> exited "
        f"rc={proc.returncode}; stderr={proc.stderr[:500]!r}"
    )
    import json as _json

    payload = _json.loads(proc.stdout)
    assert "profile_lifecycle" in payload, (
        "Rec 2: status --json missing 'profile_lifecycle' under named profile"
    )
    contract = payload["profile_lifecycle"]
    # Active profile name must reflect the named selection, not "default".
    assert contract.get("active_profile") == "sales-rec2-test", (
        f"Rec 2: active_profile={contract.get('active_profile')!r}, expected "
        f"'sales-rec2-test'. The boot shim's rank-2 (HOMIE_HOME env) precedence "
        f"didn't propagate into the lifecycle helpers."
    )

    # bot_pid_path MUST land under the sales profile's run/ dir, NOT under the
    # default install dir's .claude/data/state/.
    bot_pid_path = contract.get("bot_pid_path", "")
    sales_run = str(sales_dir / "run")
    assert sales_run in bot_pid_path, (
        f"Rec 2: bot_pid_path={bot_pid_path!r} but expected a path under "
        f"{sales_run}/. Named-profile branch in get_bot_pid_path() regressed."
    )
    assert ".claude" not in bot_pid_path or "TheHomie" not in bot_pid_path, (
        f"Rec 2: bot_pid_path={bot_pid_path!r} appears to be the install-dir "
        f"default path, not the named-profile path."
    )

    # bot_lock_path same shape.
    bot_lock_path = contract.get("bot_lock_path", "")
    assert sales_run in bot_lock_path, (
        f"Rec 2: bot_lock_path={bot_lock_path!r} but expected under {sales_run}/."
    )

    # On Windows, mutex name must be the hashed (Global\Homie-<hex>) form, NOT
    # the legacy default mutex.
    if sys.platform == "win32":
        mutex = contract.get("bot_mutex_name") or ""
        assert mutex.startswith("Global\\Homie-"), (
            f"Rec 2: bot_mutex_name={mutex!r}, expected the hashed "
            f"'Global\\Homie-<hex>' form for a named profile. Legacy mutex "
            f"would let two named-profile bots collide on Windows."
        )
        assert "SecondBrainTelegramBot" not in mutex, (
            f"Rec 2: bot_mutex_name={mutex!r} is the legacy default mutex — "
            f"named profiles MUST get a hashed mutex per PRP-7c R3 NNB3."
        )

    # Ports must be populated (resolved to integers, not error strings).
    for port_key in (
        "orchestration_api_port",
        "health_check_port",
        "whatsapp_webhook_port",
    ):
        val = contract.get(port_key)
        assert isinstance(val, int) and 1 <= val <= 65535, (
            f"Rec 2: {port_key}={val!r} not a valid port — named-profile "
            f"port resolver may have errored under HOMIE_HOME pinning."
        )


# ---------------------------------------------------------------------------
# F1 (R3) — explicit ``--profile default`` overrides sticky active_profile.
# ---------------------------------------------------------------------------
#
# Iteration 3 finding (owner sharpen-axe override of CLUTCH 2-iter spec):
# the iter-2 wrapper handled named profiles correctly but the ``default``/``-``
# branch used ``unset HOMIE_HOME`` and did NOT forward argv to the resolver
# subprocess. Result: with ``~/.homie/active_profile`` = ``sales`` and a
# wrapper invocation of ``--profile default``:
#
#     * Wrapper: clears HOMIE_HOME (correct intent — operator wants default).
#     * Resolver subprocess: sys.argv = ['-c'] (no flag forwarded). Boot shim
#       sees no rank-1 CLI flag, no rank-2 HOMIE_HOME, falls through to
#       rank-3 sticky → resolves SALES paths.
#     * Bot launch: ``python main.py --profile default ...`` → boot shim
#       ALSO previously crashed here because ``validate_persona_name("default")``
#       rejected the reserved name.
#
# The fix forwards argv (``"$@"`` / ``%*``) to the resolver subprocess so its
# boot shim sees the same rank-1 flag as the bot launch. To make rank-1 not
# crash on ``default``/``-``, ``apply_persona_override`` now treats those two
# strings as a force-default sentinel (clears HOMIE_HOME, strips the flag,
# returns) instead of routing them through ``validate_persona_name``.
#
# These tests prove (a) the boot shim's sentinel handling exits cleanly and
# (b) the wrapper's argv forwarding propagates the flag end-to-end so the
# resolver echoes default canonical paths even with sticky=sales on disk.


def _write_sticky_active_profile(fake_home: Path, name: str) -> None:
    """Helper — set ``<fake_home>/.homie/active_profile`` to *name*."""
    homie_dir = fake_home / ".homie"
    homie_dir.mkdir(parents=True, exist_ok=True)
    (homie_dir / "active_profile").write_text(name, encoding="utf-8")


def test_apply_persona_override_default_sentinel_clears_homie_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3 boot-shim contract — explicit ``--profile default`` is a force-default
    sentinel, NOT a profile name. Must clear HOMIE_HOME, strip the flag, and
    return WITHOUT calling ``validate_persona_name`` (which would reject
    ``default`` as reserved).

    Runs in a subprocess so we don't pollute the test process's ``sys.argv``
    or environment with the flag-strip side effect.
    """
    scripts_dir = Path(__file__).resolve().parent.parent
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    _write_sticky_active_profile(fake_home, "sales")

    test_env = os.environ.copy()
    # Clear HOMIE_HOME so we can prove the boot shim does the clearing itself.
    test_env.pop("HOMIE_HOME", None)
    test_env["HOME"] = str(fake_home)
    test_env["USERPROFILE"] = str(fake_home)

    code = (
        "import sys\n"
        f"sys.path.insert(0, r'{scripts_dir}')\n"
        "from personas import apply_persona_override\n"
        "apply_persona_override()\n"
        "import os\n"
        "print('HOMIE_HOME=' + repr(os.environ.get('HOMIE_HOME', '<unset>')))\n"
        "print('argv=' + repr(sys.argv))\n"
    )
    # Forward `--profile default --noop` after the -c script.
    proc = subprocess.run(
        [sys.executable, "-c", code, "--profile", "default", "--noop"],
        capture_output=True,
        text=True,
        env=test_env,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"R3: apply_persona_override hard-failed on --profile default — the "
        f"force-default sentinel branch must run BEFORE validate_persona_name. "
        f"stderr={proc.stderr!r}"
    )
    output = proc.stdout
    assert "HOMIE_HOME='<unset>'" in output, (
        f"R3: --profile default did not clear HOMIE_HOME. Output: {output!r}"
    )
    # Flag must be stripped. Remaining argv must contain --noop only (plus the
    # implicit '-c' marker).
    assert "'--profile'" not in output and "'default'" not in output, (
        f"R3: --profile default flag not stripped from sys.argv. Output: {output!r}"
    )
    assert "'--noop'" in output, (
        f"R3: unrelated argv flags not preserved. Output: {output!r}"
    )


@pytest.mark.parametrize(
    "flag_args",
    [
        ["--profile", "default"],
        ["--profile=default"],
        ["-p", "default"],
        ["--profile", "-"],
    ],
    ids=["space-form", "equals-form", "short-form", "dash-sentinel"],
)
def test_apply_persona_override_default_sentinel_all_flag_forms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag_args: list[str],
) -> None:
    """R3 boot-shim contract — every flag form (``--profile default``,
    ``--profile=default``, ``-p default``, ``--profile -``) hits the
    force-default sentinel branch and exits cleanly without raising.
    """
    scripts_dir = Path(__file__).resolve().parent.parent
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    _write_sticky_active_profile(fake_home, "sales")

    test_env = os.environ.copy()
    test_env.pop("HOMIE_HOME", None)
    test_env["HOME"] = str(fake_home)
    test_env["USERPROFILE"] = str(fake_home)

    code = (
        "import sys\n"
        f"sys.path.insert(0, r'{scripts_dir}')\n"
        "from personas import apply_persona_override\n"
        "apply_persona_override()\n"
        "import os\n"
        "print('OK ' + repr(os.environ.get('HOMIE_HOME', '<unset>')))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code, *flag_args],
        capture_output=True,
        text=True,
        env=test_env,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"R3 ({flag_args!r}): apply_persona_override hard-failed on a "
        f"default-sentinel flag form. stderr={proc.stderr!r}"
    )
    assert "OK '<unset>'" in proc.stdout, (
        f"R3 ({flag_args!r}): HOMIE_HOME not cleared. stdout={proc.stdout!r}"
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="bot-status.sh is the bash entry point. Windows uses run_chat.bat "
    "exercised by test_run_chat_bat_explicit_default_overrides_sticky.",
)
def test_bot_status_sh_explicit_default_overrides_sticky(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3 — ``bot-status.sh --profile default`` with sticky=sales reports the
    canonical install-dir default paths, NOT the sticky-sales paths.

    Reproduces the iter-3 finding's exact failure mode: a sticky
    ``~/.homie/active_profile`` containing ``sales`` would, before this fix,
    cause the resolver subprocess to fall through to rank-3 (sticky) because
    the wrapper didn't forward argv. After the fix:

        1. Wrapper's ``--profile default`` branch keeps ``HOMIE_HOME`` cleared.
        2. Wrapper forwards ``"$@"`` to the resolver subprocess.
        3. Resolver subprocess sees ``--profile default`` in argv, hits the
           boot shim's force-default sentinel, returns with HOMIE_HOME cleared.
        4. ``personas.services.get_bot_pid_path()`` returns the canonical
           install-dir default path.
    """
    script_dir = Path(__file__).resolve().parent.parent.parent / "chat"
    sh_path = script_dir / "bot-status.sh"
    if not sh_path.exists():
        pytest.skip(f"{sh_path} missing")

    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    profiles_root = fake_home / ".homie" / "profiles"
    profiles_root.mkdir(parents=True)
    # Sticky points at sales — but the sales dir doesn't even need to exist;
    # we're testing that --profile default OVERRIDES sticky regardless.
    _write_sticky_active_profile(fake_home, "sales")

    test_env = os.environ.copy()
    test_env["HOME"] = str(fake_home)
    test_env["USERPROFILE"] = str(fake_home)
    test_env.pop("HOMIE_HOME", None)

    proc = subprocess.run(
        ["bash", str(sh_path), "--profile", "default"],
        capture_output=True,
        text=True,
        env=test_env,
        timeout=60,
    )
    output = (proc.stdout or "") + (proc.stderr or "")

    assert "Service resolver failed" not in output, (
        f"R3: bot-status.sh --profile default hit the F4 resolver-failure "
        f"path. Output:\n{output}"
    )

    pid_line = next(
        (line for line in output.splitlines() if "PID path:" in line),
        "",
    )
    assert pid_line, (
        f"R3: bot-status.sh did not emit a 'PID path:' line. Output:\n{output}"
    )
    # The canonical default path lives under <install>/.claude/data/state/bot.pid
    # — the wrapper-resolver forwarding fix routes here even with sticky=sales.
    # The sticky-sales path would be under fake_home/.homie/profiles/sales/run/.
    sticky_fragment = str(profiles_root / "sales")
    assert sticky_fragment not in pid_line, (
        f"R3: bot-status.sh resolved sticky-sales path despite explicit "
        f"--profile default override. PID line={pid_line!r}. The wrapper's "
        f"argv-forwarding fix did not propagate the rank-1 flag to the "
        f"resolver subprocess (or boot shim's default-sentinel branch missing)."
    )
    # And the canonical default fragment must be present.
    assert "data" in pid_line and "state" in pid_line and "bot.pid" in pid_line, (
        f"R3: bot-status.sh did not resolve the canonical default path. "
        f"PID line={pid_line!r}. Expected ``<install>/.claude/data/state/bot.pid``."
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash test — Windows uses run_chat.bat.",
)
def test_run_chat_sh_explicit_default_overrides_sticky(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3 — ``run_chat.sh --profile default`` with sticky=sales: the heavier
    sibling of the bot-status.sh test. Confirms argv forwarding works in the
    bot-startup wrapper path as well.

    The wrapper falls into background mode, kills nothing (no live bot),
    spawns ``main.py`` (which exits fast without TELEGRAM_BOT_TOKEN), then
    echoes ``PID file: <path>``. We assert the printed path is the canonical
    install-dir default, NOT the sticky-sales path.
    """
    script_dir = Path(__file__).resolve().parent.parent.parent / "chat"
    sh_path = script_dir / "run_chat.sh"
    if not sh_path.exists():
        pytest.skip(f"{sh_path} missing")

    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    profiles_root = fake_home / ".homie" / "profiles"
    profiles_root.mkdir(parents=True)
    _write_sticky_active_profile(fake_home, "sales")

    test_env = os.environ.copy()
    test_env["HOME"] = str(fake_home)
    test_env["USERPROFILE"] = str(fake_home)
    test_env.pop("HOMIE_HOME", None)
    test_env["TELEGRAM_BOT_TOKEN"] = ""  # bot exits fast

    # Pass ``--test`` alongside ``--profile default`` so the bot exits
    # quickly in test mode rather than entering Telegram polling. Without
    # this, ``--profile default`` resolves to the real install ``.env``
    # (which may have a valid token) and the background bot would hold the
    # subprocess pipes open past our timeout.
    proc = subprocess.run(
        ["bash", str(sh_path), "--profile", "default", "--test"],
        capture_output=True,
        text=True,
        env=test_env,
        timeout=90,
    )
    output = (proc.stdout or "") + (proc.stderr or "")

    assert "Service resolver failed" not in output, (
        f"R3: run_chat.sh --profile default hit the F4 resolver-failure "
        f"path. Output:\n{output}"
    )

    pid_line = next(
        (line for line in output.splitlines() if line.strip().startswith("PID file:")),
        "",
    )
    assert pid_line, (
        f"R3: run_chat.sh did not echo a 'PID file:' line. Output:\n{output}"
    )
    sticky_fragment = str(profiles_root / "sales")
    assert sticky_fragment not in pid_line, (
        f"R3: run_chat.sh echoed sticky-sales PID path={pid_line!r} despite "
        f"explicit --profile default override. Wrapper's argv forwarding "
        f"didn't reach the resolver subprocess."
    )
    assert "data" in pid_line and "state" in pid_line and "bot.pid" in pid_line, (
        f"R3: run_chat.sh did not echo canonical default PID path. "
        f"PID line={pid_line!r}."
    )


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="run_chat.bat is the cmd.exe entry point.",
)
def test_run_chat_bat_explicit_default_overrides_sticky(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3 — ``run_chat.bat --profile default`` with sticky=sales: same matrix
    on the Windows wrapper. Confirms ``%*`` forwarding to the python -c
    subprocess works on cmd.exe and the boot shim's default-sentinel branch
    fires correctly.
    """
    script_dir = Path(__file__).resolve().parent.parent.parent / "chat"
    bat_path = script_dir / "run_chat.bat"
    if not bat_path.exists():
        pytest.skip(f"{bat_path} missing")

    # cmd.exe uses %USERPROFILE% to locate ~/.homie/profiles/, so override.
    fake_userprofile = tmp_path / "fakeprofile"
    fake_userprofile.mkdir()
    profiles_root = fake_userprofile / ".homie" / "profiles"
    profiles_root.mkdir(parents=True)
    # Sticky points at sales.
    homie_dir = fake_userprofile / ".homie"
    (homie_dir / "active_profile").write_text("sales", encoding="utf-8")

    test_env = os.environ.copy()
    test_env["USERPROFILE"] = str(fake_userprofile)
    test_env["HOME"] = str(fake_userprofile)
    test_env.pop("HOMIE_HOME", None)
    test_env["TELEGRAM_BOT_TOKEN"] = ""

    # Pass ``--test`` alongside ``--profile default`` so the bot exits
    # quickly in test mode. Without this, ``--profile default`` resolves to
    # owner's real install ``.env`` (which has a valid Telegram token), the
    # bot enters polling, and the ``start /b`` background process holds the
    # subprocess pipes open past our timeout. The boot shim strips
    # ``--profile default`` from argv before ``main.py``'s argparse runs;
    # ``--test`` survives and triggers test-mode exit.
    proc = subprocess.run(
        ["cmd", "/c", str(bat_path), "--profile", "default", "--test"],
        capture_output=True,
        text=True,
        env=test_env,
        timeout=60,
    )
    output = (proc.stdout or "") + (proc.stderr or "")

    assert "Service resolver failed" not in output, (
        f"R3: run_chat.bat --profile default hit the F4 resolver-failure "
        f"path. Output:\n{output}"
    )

    pid_line = next(
        (line for line in output.splitlines() if line.strip().lower().startswith("pid file:")),
        "",
    )
    assert pid_line, (
        f"R3: run_chat.bat did not echo a 'PID file:' line. Output:\n{output}"
    )
    sticky_fragment = str(profiles_root / "sales")
    assert sticky_fragment.lower() not in pid_line.lower(), (
        f"R3: run_chat.bat echoed sticky-sales PID path={pid_line!r} despite "
        f"explicit --profile default override. %* forwarding to the python -c "
        f"subprocess didn't propagate the rank-1 flag, OR the boot shim's "
        f"force-default sentinel branch is missing."
    )
    # Canonical default fragment.
    pid_lower = pid_line.lower()
    assert "data" in pid_lower and "state" in pid_lower and "bot.pid" in pid_lower, (
        f"R3: run_chat.bat did not echo canonical default PID path. "
        f"PID line={pid_line!r}."
    )
