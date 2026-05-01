"""PRP-7b WS4 — wrapper template + collision + lifecycle wrapper tests.

Disposition coverage:
    - R1 B3 — wrapper accepts EXPLICIT profile_root (not implicit
      get_homie_home()).
    - R1 B4 — OS-flag pre-validation in `create_wrapper_alias` raises
      LifecycleError BEFORE any file write.
    - R3 NNM2 — escape-aware byte content + round-trip safety:
        * POSIX: shlex.quote on profile_root + name
        * Windows .cmd: `set "HOMIE_HOME=<escaped>"` with `_cmd_escape`
        * Windows .ps1: `_ps_single_quote`
        * launchd: plistlib (NOT hand-built XML)
        * systemd: double-quoted Environment + WorkingDirectory
    - R1 M3 — lifecycle integration: wrapper points at the NEW profile, NOT
      the active process's HOMIE_HOME.

Cross-platform skips: helper-level byte tests are platform-conditional.
Lifecycle integration tests use monkeypatch on `sys.platform`.
"""
from __future__ import annotations

import plistlib
import shlex
import sys
from pathlib import Path

import pytest

from personas.lifecycle import LifecycleError
from personas.wrappers import (
    _check_alias_collision,
    _cmd_escape,
    _create_posix_wrapper,
    _create_windows_wrappers,
    _get_posix_bin_dir,
    _get_windows_bin_dir,
    _launchd_plist_content,
    _posix_wrapper_content,
    _ps_single_quote,
    _systemd_unit_content,
    _windows_cmd_content,
    _windows_ps1_content,
    create_wrapper_alias,
)


# ---------------------------------------------------------------------------
# Bin-dir resolvers — Rule 1 None sentinel + env override
# ---------------------------------------------------------------------------


def test_get_posix_bin_dir_default_is_user_local_bin(monkeypatch):
    """Default POSIX bin dir is ~/.local/bin (no HOMIE_BIN_DIR override)."""
    monkeypatch.delenv("HOMIE_BIN_DIR", raising=False)
    bin_dir = _get_posix_bin_dir()
    assert bin_dir.name == "bin"
    assert bin_dir.parent.name == ".local"


def test_get_posix_bin_dir_honors_homie_bin_dir_env(monkeypatch, tmp_path):
    """HOMIE_BIN_DIR env override is honored on every call."""
    override = tmp_path / "custom-bin"
    monkeypatch.setenv("HOMIE_BIN_DIR", str(override))
    assert _get_posix_bin_dir() == override.resolve(strict=False)


def test_get_windows_bin_dir_default_is_appdata_programs(monkeypatch):
    monkeypatch.delenv("HOMIE_BIN_DIR", raising=False)
    bin_dir = _get_windows_bin_dir()
    # Default: %USERPROFILE%\AppData\Local\Programs\thehomie\bin
    assert bin_dir.name == "bin"


def test_get_windows_bin_dir_honors_homie_bin_dir_env(monkeypatch, tmp_path):
    override = tmp_path / "custom-bin"
    monkeypatch.setenv("HOMIE_BIN_DIR", str(override))
    assert _get_windows_bin_dir() == override.resolve(strict=False)


# ---------------------------------------------------------------------------
# Escape helpers
# ---------------------------------------------------------------------------


def test_cmd_escape_doubles_percent():
    """`%` -> `%%` for batch-file literal interpretation."""
    assert _cmd_escape("100%") == "100%%"


def test_cmd_escape_caret_first_then_ampersand():
    """`^` is doubled FIRST so subsequent escapes don't get re-escaped."""
    assert _cmd_escape("a&b") == "a^&b"


def test_cmd_escape_double_quote():
    assert _cmd_escape('a"b') == 'a^"b'


def test_ps_single_quote_doubles_apostrophe():
    """PowerShell single-quoted strings escape `'` as `''`."""
    assert _ps_single_quote("O'Brien") == "O''Brien"


def test_ps_single_quote_preserves_no_apostrophe():
    assert _ps_single_quote("plain-path") == "plain-path"


# ---------------------------------------------------------------------------
# Content templates — POSIX
# ---------------------------------------------------------------------------


def test_posix_wrapper_content_uses_shlex_quote_for_root_and_name():
    """R3 NNM2 — both profile_root and name go through shlex.quote()."""
    profile_root = Path("/tmp/has space/sales")
    content = _posix_wrapper_content("sales", profile_root)
    assert content.startswith("#!/bin/sh\n")
    # The path with a space should be quoted (shlex wraps in single quotes).
    assert shlex.quote(str(profile_root)) in content
    assert "exec thehomie -p" in content


def test_posix_wrapper_content_handles_apostrophe():
    """R3 NNM2 — paths with apostrophes round-trip via shlex.quote."""
    profile_root = Path("/tmp/O'Brien/sales")
    content = _posix_wrapper_content("sales", profile_root)
    # shlex.quote wraps in single quotes and escapes embedded ' as '"'"'.
    assert shlex.quote(str(profile_root)) in content


# ---------------------------------------------------------------------------
# Content templates — Windows .cmd
# ---------------------------------------------------------------------------


def test_windows_cmd_content_wraps_set_pair_in_quotes():
    """R3 NNM2 — `set "HOMIE_HOME=value"` (quotes wrap WHOLE pair)."""
    profile_root = Path("C:/users/owner/.homie/profiles/sales")
    content = _windows_cmd_content("sales", profile_root)
    # Must contain the canonical batch shape.
    assert 'set "HOMIE_HOME=' in content
    assert content.endswith("\r\n")  # CRLF line endings


def test_windows_cmd_content_doubles_percent_in_path():
    """Path with literal `%` is doubled to `%%` per batch-file convention."""
    profile_root = Path("C:/tmp/100%-test/sales")
    content = _windows_cmd_content("sales", profile_root)
    # The escaped value should contain `100%%-test` (literal `%%`).
    assert "100%%-test" in content


def test_windows_cmd_content_caret_escapes_ampersand():
    """`&` in path is `^&` to prevent batch-statement termination."""
    profile_root = Path("C:/tmp/A & B/sales")
    content = _windows_cmd_content("sales", profile_root)
    assert "A ^& B" in content


def test_windows_ps1_content_doubles_apostrophe():
    """R3 NNM2 — apostrophe in path is doubled inside single-quoted string."""
    profile_root = Path("C:/users/O'Brien/sales")
    content = _windows_ps1_content("sales", profile_root)
    # PowerShell single-quote escape: 'O''Brien'
    assert "O''Brien" in content


def test_windows_ps1_content_starts_with_env_assignment():
    profile_root = Path("C:/tmp/sales")
    content = _windows_ps1_content("sales", profile_root)
    assert content.startswith("$env:HOMIE_HOME = '")
    assert "thehomie -p sales @args" in content


# ---------------------------------------------------------------------------
# launchd plist — plistlib-faithful (NOT byte-exact)
# ---------------------------------------------------------------------------


def test_launchd_plist_content_parses_via_plistlib():
    """R3 NNM2 — plist content parses via plistlib.loads, NOT byte-exact."""
    profile_root = Path("/tmp/sales-profile")
    content = _launchd_plist_content("sales", profile_root, "/usr/local/bin/thehomie")
    parsed = plistlib.loads(content.encode("utf-8"))
    assert parsed["Label"] == "com.smokedev.homie.sales"
    assert parsed["ProgramArguments"] == [
        "/usr/local/bin/thehomie", "-p", "sales", "chat"
    ]
    assert parsed["WorkingDirectory"] == str(profile_root)
    assert parsed["EnvironmentVariables"]["HOMIE_HOME"] == str(profile_root)
    assert parsed["RunAtLoad"] is True


def test_launchd_plist_content_handles_special_chars():
    """plistlib auto-escapes `&` < > " ' so paths round-trip exactly."""
    profile_root = Path("/tmp/A & B/sales")
    content = _launchd_plist_content("sales", profile_root, "/usr/local/bin/thehomie")
    parsed = plistlib.loads(content.encode("utf-8"))
    assert parsed["WorkingDirectory"] == str(profile_root)
    assert parsed["EnvironmentVariables"]["HOMIE_HOME"] == str(profile_root)


# ---------------------------------------------------------------------------
# systemd unit — double-quoted Environment + WorkingDirectory
# ---------------------------------------------------------------------------


def test_systemd_unit_content_quotes_environment_and_working_dir():
    """R3 NNM2 — both keys are double-quoted (consistent quoting).

    Build the expected escaped path via the same `_systemd_value_escape`
    primitive so the test is platform-stable (Path() rendering differs
    POSIX vs Windows; we don't care about path-string rendering, only
    that BOTH keys end up double-quoted with the SAME escaped value).
    """
    from personas.wrappers import _systemd_value_escape

    profile_root = Path("/tmp/has space/sales")
    content = _systemd_unit_content(
        "sales", profile_root, "/usr/local/bin/thehomie"
    )
    expected = _systemd_value_escape(str(profile_root))
    # Both Environment= and WorkingDirectory= use double-quotes around
    # the SAME escaped value.
    assert f'WorkingDirectory="{expected}"' in content
    assert f'Environment="HOMIE_HOME={expected}"' in content


def test_systemd_unit_content_escapes_backslash_and_quote():
    """systemd escape: backslash + quote per `man systemd.exec`."""
    profile_root = Path('/tmp/has"quote\\backslash/sales')
    content = _systemd_unit_content(
        "sales", profile_root, "/usr/local/bin/thehomie"
    )
    # Backslashes doubled, quotes escaped.
    assert "\\\\" in content
    assert '\\"' in content


# ---------------------------------------------------------------------------
# _create_posix_wrapper — file write + chmod
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only test")
def test_create_posix_wrapper_writes_executable(tmp_path):
    """POSIX wrapper file is executable + contains expected shape."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    profile_root = tmp_path / "sales-profile"
    profile_root.mkdir()
    wrapper = _create_posix_wrapper("sales", profile_root, bin_dir=bin_dir)
    assert wrapper == bin_dir / "sales-homie"
    assert wrapper.exists()
    # Mode bits include execute.
    mode = wrapper.stat().st_mode & 0o111
    assert mode != 0, "wrapper not executable"
    text = wrapper.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n")
    assert str(profile_root) in text


# ---------------------------------------------------------------------------
# _create_windows_wrappers — both .cmd and .ps1
# ---------------------------------------------------------------------------


def test_create_windows_wrappers_writes_both_files(tmp_path):
    """Both `.cmd` and `.ps1` wrappers materialize on disk."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    profile_root = tmp_path / "sales-profile"
    profile_root.mkdir()
    cmd_path, ps1_path = _create_windows_wrappers(
        "sales", profile_root, bin_dir=bin_dir
    )
    assert cmd_path == bin_dir / "sales-homie.cmd"
    assert ps1_path == bin_dir / "sales-homie.ps1"
    assert cmd_path.exists()
    assert ps1_path.exists()
    assert "thehomie -p sales" in cmd_path.read_text(encoding="utf-8")
    assert "thehomie -p sales" in ps1_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# _check_alias_collision — content-verify behavior
# ---------------------------------------------------------------------------


def test_check_alias_collision_returns_none_when_no_collision(monkeypatch):
    """No file on PATH for `<name>-homie` -> None."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _x: None)
    assert _check_alias_collision("sales") is None


def test_check_alias_collision_returns_none_when_our_wrapper(
    tmp_path, monkeypatch
):
    """Existing file containing `thehomie -p` is OUR wrapper — safe to overwrite."""
    fake = tmp_path / "sales-homie"
    fake.write_text("# previous install\nexec thehomie -p sales \"$@\"\n")
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _x: str(fake))
    assert _check_alias_collision("sales") is None


def test_check_alias_collision_flags_foreign_file(tmp_path, monkeypatch):
    """Existing file NOT containing `thehomie -p` -> non-None message."""
    foreign = tmp_path / "sales-homie"
    foreign.write_text("# user-authored shell script\necho hello\n")
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _x: str(foreign))
    msg = _check_alias_collision("sales")
    assert msg is not None
    assert "collides" in msg.lower()


# ---------------------------------------------------------------------------
# create_wrapper_alias — OS-flag pre-validation (R1 B4)
# ---------------------------------------------------------------------------


def test_create_wrapper_alias_launchd_on_non_darwin_raises(tmp_path, monkeypatch):
    """install_launchd=True on non-darwin raises LifecycleError BEFORE write."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HOMIE_BIN_DIR", str(tmp_path / "bin"))
    profile_root = tmp_path / "sales-profile"
    profile_root.mkdir()
    with pytest.raises(LifecycleError, match="launchd"):
        create_wrapper_alias("sales", profile_root, install_launchd=True)
    # No wrappers were written — bin dir might not even exist yet.
    bin_dir = tmp_path / "bin"
    if bin_dir.exists():
        assert not any(bin_dir.iterdir()), "wrapper file leaked despite OS-mismatch"


def test_create_wrapper_alias_systemd_on_non_linux_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOMIE_BIN_DIR", str(tmp_path / "bin"))
    profile_root = tmp_path / "sales-profile"
    profile_root.mkdir()
    with pytest.raises(LifecycleError, match="systemd"):
        create_wrapper_alias("sales", profile_root, install_systemd=True)


# ---------------------------------------------------------------------------
# R1 B3 — wrapper points at NEW profile, not active process
# ---------------------------------------------------------------------------


def test_wrapper_uses_explicit_profile_root_not_active_homie_home(
    tmp_path, monkeypatch
):
    """R1 B3 — even if HOMIE_HOME points elsewhere, the wrapper bakes the
    explicit `profile_root` parameter."""
    # Active process points at "wrong" HOMIE_HOME.
    monkeypatch.setenv("HOMIE_HOME", str(tmp_path / "wrong"))
    monkeypatch.setenv("HOMIE_BIN_DIR", str(tmp_path / "bin"))
    target = tmp_path / "right" / "sales-profile"
    target.mkdir(parents=True)

    paths = create_wrapper_alias("sales", target)

    if sys.platform == "win32":
        wrapper_text = paths.windows_cmd.read_text(encoding="utf-8")
    else:
        wrapper_text = paths.posix_shell.read_text(encoding="utf-8")
    assert str(target) in wrapper_text
    # The "wrong" HOMIE_HOME should NOT appear in the wrapper content.
    assert str(tmp_path / "wrong") not in wrapper_text
