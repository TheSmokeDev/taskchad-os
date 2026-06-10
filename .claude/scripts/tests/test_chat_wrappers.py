"""Wrapper ``--dry-run`` contract tests (issue #34).

INVARIANT — NEVER invoke a wrapper WITHOUT ``--dry-run`` from this module.
The non-dry-run path of ``run_chat.sh`` kills existing bot processes
(``_kill_existing`` → pid-file kill + ``cleanup_all_bot_processes()``),
deletes the pid file, truncates the bot log at spawn, and forks a bot that
acquires the GLOBAL Windows mutex. ``run_chat.bat`` spawns via ``start /b``.
The precedent tests in ``test_persona_bot_lifecycle.py`` deliberately fork
the bot under a fake profile — do NOT copy that shape here.

Defense-in-depth: every test runs the wrapper under a fake ``HOMIE_HOME``
(tmp_path) with an empty ``TELEGRAM_BOT_TOKEN``. Even if ``--dry-run``
parsing regresses and a wrapper falls through to the kill/spawn path,
``cleanup_all_bot_processes()`` filters candidate processes by EXACT
``HOMIE_HOME`` match FAIL-CLOSED (see shared.py), so the real bot can never
be killed and any pid/log writes land inside tmp_path.

Accepted dry-run side effects (documented, benign):
- ``run_chat.bat`` writes + deletes a resolver temp file in ``%TEMP%``.
- Both wrappers would run ``uv sync`` if the checkout venv were missing;
  it exists here because pytest itself runs from that venv.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

CHAT_DIR = Path(__file__).resolve().parent.parent.parent / "chat"


def _git_bash() -> str | None:
    """Resolve a usable bash for the .sh wrappers.

    On Windows, ``shutil.which("bash")`` may return the WSL launcher at
    ``System32\\bash.exe`` — running the wrappers under WSL is itself the
    ``C:\\mnt\\c`` failure mode issue #34 fixes (WSL interop passes
    ``/mnt/c`` args verbatim to Windows python.exe), so prefer Git Bash
    explicitly and never fall back to the WSL launcher.
    """
    candidates: list[Path] = []
    found = shutil.which("bash")
    if found:
        candidates.append(Path(found))
    if sys.platform == "win32":
        candidates.append(Path(r"C:\Program Files\Git\bin\bash.exe"))
        candidates.append(Path(r"C:\Program Files\Git\usr\bin\bash.exe"))
    for cand in candidates:
        if not cand.exists():
            continue
        if sys.platform == "win32" and "system32" in str(cand).lower():
            continue  # WSL launcher — never use it for these wrappers
        return str(cand)
    return None


def _make_fake_profile(tmp_path: Path) -> Path:
    """Minimal fake profile dir, same shape as test_persona_bot_lifecycle."""
    profile_dir = tmp_path / ".homie" / "profiles" / "dryrun-test"
    profile_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("memory", "data", "state", "run", "logs", "credentials"):
        (profile_dir / sub).mkdir(parents=True, exist_ok=True)
    (profile_dir / ".env").write_text("# fake env\n", encoding="utf-8")
    return profile_dir


_ADAPTER_TOKEN_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "WHATSAPP_TOKEN",
)


def _blank_adapter_tokens(env: dict[str, str]) -> None:
    """Defense-in-depth: even on a fall-through the bot could never connect
    ANY adapter, not just Telegram."""
    for var in _ADAPTER_TOKEN_VARS:
        env[var] = ""


def _build_env(profile_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HOMIE_HOME"] = str(profile_dir)
    _blank_adapter_tokens(env)
    return env


def _make_fake_home(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """Fake home with a ``<home>/.homie/profiles/<name>`` tree, for tests
    that exercise ``--profile NAME`` forwarding (the wrappers resolve the
    profile root from ``$HOME`` / ``%USERPROFILE%``, NOT from HOMIE_HOME)."""
    home = tmp_path / "home"
    profile_dir = home / ".homie" / "profiles" / name
    for sub in ("memory", "data", "state", "run", "logs", "credentials"):
        (profile_dir / sub).mkdir(parents=True, exist_ok=True)
    (profile_dir / ".env").write_text("# fake env\n", encoding="utf-8")
    return home, profile_dir


def _build_profile_env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    # bash wrappers resolve ``$HOME/.homie/profiles/<name>`` — forward
    # slashes so the bash-side ``[ -d ... ]`` existence check is unambiguous.
    env["HOME"] = str(home).replace("\\", "/")
    # run_chat.bat resolves ``%USERPROFILE%\.homie\profiles\<name>``; the
    # resolver subprocess's Path.home() lands in the same fake home.
    env["USERPROFILE"] = str(home)
    # The --profile flag must be the ONLY profile source in these tests.
    env.pop("HOMIE_HOME", None)
    _blank_adapter_tokens(env)
    return env


def _parse_labeled(output: str, label: str) -> str:
    """Return the value of a ``LABEL: value`` line (first colon split only,
    so Windows drive colons survive)."""
    prefix = label.upper() + ":"
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith(prefix):
            return stripped.split(":", 1)[1].strip()
    return ""


def _assert_no_wsl_path_leak(output: str) -> None:
    low = output.lower()
    assert "/mnt/c" not in low, f"WSL-style /mnt/c path leaked: {output!r}"
    assert "c:\\mnt\\c" not in low, f"mistranslated C:\\mnt\\c path: {output!r}"


@pytest.mark.skipif(_git_bash() is None, reason="no usable (non-WSL) bash found")
def test_run_chat_sh_dry_run(tmp_path: Path) -> None:
    """``run_chat.sh --dry-run`` resolves paths and exits 0 with ZERO side
    effects — no kill, no pid write, no spawn, no log truncation."""
    script = CHAT_DIR / "run_chat.sh"
    if not script.exists():
        pytest.skip(f"{script} missing")

    profile_dir = _make_fake_profile(tmp_path)
    proc = subprocess.run(
        [_git_bash(), str(script), "--dry-run"],
        capture_output=True,
        text=True,
        env=_build_env(profile_dir),
        timeout=60,
    )
    output = (proc.stdout or "") + (proc.stderr or "")

    assert proc.returncode == 0, f"dry-run exited {proc.returncode}: {output!r}"
    assert "DRY RUN" in output, f"missing DRY RUN marker: {output!r}"
    _assert_no_wsl_path_leak(output)

    python_path = _parse_labeled(output, "PYTHON")
    main_py = _parse_labeled(output, "MAIN_PY")
    pid_file = _parse_labeled(output, "PID_FILE")
    log_file = _parse_labeled(output, "LOG_FILE")

    assert python_path and Path(python_path).exists(), (
        f"PYTHON does not point at an existing interpreter: {python_path!r}"
    )
    assert main_py and Path(main_py).exists(), (
        f"MAIN_PY does not point at an existing file: {main_py!r}"
    )
    expected_pid = profile_dir / "run" / "bot.pid"
    assert pid_file and os.path.normcase(os.path.normpath(pid_file)) == os.path.normcase(
        os.path.normpath(str(expected_pid))
    ), f"PID_FILE={pid_file!r} is not the canonical {expected_pid}"
    assert Path(pid_file).parent.exists(), "resolved pid dir missing in fake profile"
    assert log_file and Path(log_file).parent.exists(), (
        f"LOG_FILE parent missing: {log_file!r}"
    )

    # Negative side-effect proof — the dry run must short-circuit BEFORE
    # _kill_existing(), the pid write, and the bot spawn.
    assert "Stopping old bot" not in output
    assert "Stopped active-profile bots" not in output
    assert "Telegram bot started" not in output
    assert not expected_pid.exists(), "dry run must NOT create the pid file"
    assert not (profile_dir / "logs" / "bot.log").exists(), (
        "dry run must NOT create/truncate the bot log"
    )


@pytest.mark.skipif(_git_bash() is None, reason="no usable (non-WSL) bash found")
def test_bot_status_sh_dry_run(tmp_path: Path) -> None:
    """``bot-status.sh --dry-run`` prints resolved paths and exits 0 before
    the --kill-all-homies branch and the status scan."""
    script = CHAT_DIR / "bot-status.sh"
    if not script.exists():
        pytest.skip(f"{script} missing")

    profile_dir = _make_fake_profile(tmp_path)
    proc = subprocess.run(
        [_git_bash(), str(script), "--dry-run"],
        capture_output=True,
        text=True,
        env=_build_env(profile_dir),
        timeout=60,
    )
    output = (proc.stdout or "") + (proc.stderr or "")

    assert proc.returncode == 0, f"dry-run exited {proc.returncode}: {output!r}"
    assert "DRY RUN" in output, f"missing DRY RUN marker: {output!r}"
    _assert_no_wsl_path_leak(output)

    python_path = _parse_labeled(output, "PYTHON")
    pid_file = _parse_labeled(output, "PID_FILE")
    log_file = _parse_labeled(output, "LOG_FILE")

    assert python_path and Path(python_path).exists(), (
        f"PYTHON does not point at an existing interpreter: {python_path!r}"
    )
    expected_pid = profile_dir / "run" / "bot.pid"
    assert pid_file and os.path.normcase(os.path.normpath(pid_file)) == os.path.normcase(
        os.path.normpath(str(expected_pid))
    ), f"PID_FILE={pid_file!r} is not the canonical {expected_pid}"
    assert log_file and Path(log_file).parent.exists(), (
        f"LOG_FILE parent missing: {log_file!r}"
    )

    # Negative side-effect proof — must exit BEFORE the kill branch and the
    # status report (the dry run is a pure path probe).
    assert "Killed PIDs" not in output
    assert "Homie Bot Status" not in output
    assert "STATUS:" not in output
    assert not expected_pid.exists(), "dry run must NOT create the pid file"


@pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("cmd") is None,
    reason="run_chat.bat is the Windows cmd.exe entry point",
)
def test_run_chat_bat_dry_run(tmp_path: Path) -> None:
    """``run_chat.bat --dry-run`` resolves paths and exits 0 BEFORE the
    ``start /b`` spawn (the only side-effecting step in the .bat)."""
    script = CHAT_DIR / "run_chat.bat"
    if not script.exists():
        pytest.skip(f"{script} missing")

    profile_dir = _make_fake_profile(tmp_path)
    proc = subprocess.run(
        ["cmd", "/c", str(script), "--dry-run"],
        capture_output=True,
        text=True,
        env=_build_env(profile_dir),
        timeout=60,
    )
    output = (proc.stdout or "") + (proc.stderr or "")

    assert proc.returncode == 0, f"dry-run exited {proc.returncode}: {output!r}"
    assert "DRY RUN" in output, f"missing DRY RUN marker: {output!r}"
    assert "Service resolver failed" not in output, (
        f"resolver failed under a valid HOMIE_HOME: {output!r}"
    )
    _assert_no_wsl_path_leak(output)

    python_path = _parse_labeled(output, "PYTHON")
    main_py = _parse_labeled(output, "MAIN_PY")
    pid_file = _parse_labeled(output, "PID_FILE")
    log_file = _parse_labeled(output, "LOG_FILE")

    assert python_path and Path(python_path).exists(), (
        f"PYTHON does not point at an existing interpreter: {python_path!r}"
    )
    assert main_py and Path(main_py).exists(), (
        f"MAIN_PY does not point at an existing file: {main_py!r}"
    )
    expected_pid = profile_dir / "run" / "bot.pid"
    assert pid_file and os.path.normcase(os.path.normpath(pid_file)) == os.path.normcase(
        os.path.normpath(str(expected_pid))
    ), f"PID_FILE={pid_file!r} is not the canonical {expected_pid}"
    assert log_file and Path(log_file).parent.exists(), (
        f"LOG_FILE parent missing: {log_file!r}"
    )

    # Negative side-effect proof — must exit BEFORE `start /b`.
    assert "Telegram bot started" not in output
    assert not expected_pid.exists(), "dry run must NOT create the pid file"
    assert not (profile_dir / "logs" / "bot.log").exists(), (
        "dry run must NOT create/truncate the bot log"
    )


@pytest.mark.skipif(_git_bash() is None, reason="no usable (non-WSL) bash found")
def test_run_chat_sh_dry_run_forwards_profile(tmp_path: Path) -> None:
    """``run_chat.sh --profile NAME --dry-run`` must resolve PROFILE-scoped
    paths (issue #34 AC: profile/runtime argument forwarding). Would fail if
    the profile pre-parse loop or the resolver argv forward regressed: the
    PID_FILE would fall back to default-profile paths outside the fake home."""
    script = CHAT_DIR / "run_chat.sh"
    if not script.exists():
        pytest.skip(f"{script} missing")

    home, profile_dir = _make_fake_home(tmp_path, "dryrun-prof")
    proc = subprocess.run(
        [_git_bash(), str(script), "--profile", "dryrun-prof", "--dry-run"],
        capture_output=True,
        text=True,
        env=_build_profile_env(home),
        timeout=60,
    )
    output = (proc.stdout or "") + (proc.stderr or "")

    assert proc.returncode == 0, f"profile dry-run exited {proc.returncode}: {output!r}"
    assert "DRY RUN" in output, f"missing DRY RUN marker: {output!r}"
    assert "ERROR: Profile" not in output, f"profile lookup failed: {output!r}"
    _assert_no_wsl_path_leak(output)

    pid_file = _parse_labeled(output, "PID_FILE")
    expected_pid = profile_dir / "run" / "bot.pid"
    assert pid_file and os.path.normcase(os.path.normpath(pid_file)) == os.path.normcase(
        os.path.normpath(str(expected_pid))
    ), f"PID_FILE={pid_file!r} did not resolve to the NAMED profile {expected_pid}"

    # Same zero-side-effect contract as the default-profile dry run.
    assert "Stopping old bot" not in output
    assert "Telegram bot started" not in output
    assert not expected_pid.exists(), "profile dry run must NOT create the pid file"


@pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("cmd") is None,
    reason="run_chat.bat is the Windows cmd.exe entry point",
)
def test_run_chat_bat_dry_run_forwards_profile(tmp_path: Path) -> None:
    """``run_chat.bat --profile NAME --dry-run`` must resolve PROFILE-scoped
    paths through :_homie_parse_profile + the resolver argv forward."""
    script = CHAT_DIR / "run_chat.bat"
    if not script.exists():
        pytest.skip(f"{script} missing")

    home, profile_dir = _make_fake_home(tmp_path, "dryrun-prof")
    proc = subprocess.run(
        ["cmd", "/c", str(script), "--profile", "dryrun-prof", "--dry-run"],
        capture_output=True,
        text=True,
        env=_build_profile_env(home),
        timeout=60,
    )
    output = (proc.stdout or "") + (proc.stderr or "")

    assert proc.returncode == 0, f"profile dry-run exited {proc.returncode}: {output!r}"
    assert "DRY RUN" in output, f"missing DRY RUN marker: {output!r}"
    assert "ERROR: Profile" not in output, f"profile lookup failed: {output!r}"
    assert "Service resolver failed" not in output, (
        f"resolver failed under the fake profile home: {output!r}"
    )
    _assert_no_wsl_path_leak(output)

    pid_file = _parse_labeled(output, "PID_FILE")
    expected_pid = profile_dir / "run" / "bot.pid"
    assert pid_file and os.path.normcase(os.path.normpath(pid_file)) == os.path.normcase(
        os.path.normpath(str(expected_pid))
    ), f"PID_FILE={pid_file!r} did not resolve to the NAMED profile {expected_pid}"

    # Same zero-side-effect contract as the default-profile dry run.
    assert "Telegram bot started" not in output
    assert not expected_pid.exists(), "profile dry run must NOT create the pid file"
