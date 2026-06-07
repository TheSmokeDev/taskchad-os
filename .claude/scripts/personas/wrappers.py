"""Per-OS wrapper / unit alias generation for persona profiles.

Phase 2 / PRP-7b Workstream 3 (clone-and-archive). Owns the wrapper-alias
layer that ``personas.lifecycle.create_profile`` calls into via
``from .wrappers import create_wrapper_alias`` (lazy import at call site).

Public-API contract (R1 B3 — load-bearing):
    Every wrapper / unit accepts an EXPLICIT ``profile_root`` parameter
    and uses it verbatim. This module does NOT call ``get_homie_home()``
    internally to resolve the target — that would return the CURRENT
    process's profile, which is the WRONG target when creating a NEW
    profile from a different active profile. Each shell wrapper sets
    ``HOMIE_HOME=<profile_root>`` BEFORE invoking ``thehomie -p <name>``
    so the alias is target-profile-aware regardless of the caller's
    environment.

Module exports (consumed by ``personas/lifecycle.py``):
    create_wrapper_alias(name, profile_root, *, install_launchd, install_systemd)
        — REQUIRED ``profile_root`` parameter. Top-level dispatch by
          ``sys.platform``. Validates OS-specific flags BEFORE any file
          write (R1 B4 + R3 NNM3 — raises ``LifecycleError``).
    remove_wrapper_alias(name)
        — Verifies content before unlink (Hermes-faithful pattern).
    WrapperPaths — dataclass surfaced to lifecycle for cleanup on rollback.

Helpers (private — same-package consumers in lifecycle.py + tests):
    _check_alias_collision(name) — Hermes-faithful collision check via
        ``shutil.which(name + "-homie")``.
    Content templates: _posix_wrapper_content, _windows_cmd_content,
        _windows_ps1_content, _launchd_plist_content, _systemd_unit_content.
    Bin-dir resolvers: _get_posix_bin_dir, _get_windows_bin_dir
        (None-sentinel + ``HOMIE_BIN_DIR`` env override per Rule 1).
    Per-OS install/uninstall: _install_launchd_plist, _uninstall_launchd_plist,
        _install_systemd_unit, _uninstall_systemd_unit.
    File creators: _create_posix_wrapper, _create_windows_wrappers.
    Escape helpers: _cmd_escape, _ps_single_quote.

Anti-pattern compliance (MEMORY.md Global Rules):
    - Rule 1 (None sentinel): ``bin_dir=None`` resolved at call time
      against ``HOMIE_BIN_DIR`` env. ``HOMIE_BIN_DIR`` is read inside
      ``_get_posix_bin_dir()`` / ``_get_windows_bin_dir()`` on every call,
      NEVER cached at module load.
    - Rule 2 (physical state): collision check uses ``shutil.which`` (real
      PATH walk) and reads file content to decide overwrite-safety. No
      sidecar registry.
    - Rule 3 (langfuse module-attribute import): N/A — no Langfuse.

Hermes anchors:
    - hermes_cli/profiles.py:227-242 — POSIX shell wrapper byte content.
    - hermes_cli/profiles.py:188-218 — _check_alias_collision shape
      (only allow overwrite when content carries the framework signature).
"""

from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .core import validate_persona_name


# =============================================================================
# DATACLASS
# =============================================================================


@dataclass
class WrapperPaths:
    """Files created by ``create_wrapper_alias``.

    Used by ``personas.lifecycle.create_profile`` for cleanup if the
    profile-create rollback path runs (R1 B4). Each field is ``None`` when
    the corresponding artifact was not created on this platform / flag set.
    """

    posix_shell: Optional[Path] = None
    windows_cmd: Optional[Path] = None
    windows_ps1: Optional[Path] = None
    launchd_plist: Optional[Path] = None
    systemd_unit: Optional[Path] = None


# =============================================================================
# ESCAPE HELPERS (R2 NM2 / R3 NNM2 — wrapper template path quoting)
# =============================================================================


def _cmd_escape(value: str) -> str:
    """Escape special characters for inclusion inside a Windows ``set "K=V"`` pair.

    R3 NNM2: returns the escaped value WITHOUT outer quotes — caller is
    responsible for wrapping the WHOLE assignment in quotes (the cmd batch
    canonical shape is ``set "HOMIE_HOME=value"``, NOT
    ``set HOMIE_HOME="value"``).

    Strategy:
        - ``^`` -> ``^^`` (caret is the cmd escape character; double it
          first so subsequent escapes don't get re-escaped).
        - ``&`` -> ``^&`` (would otherwise terminate the ``set`` statement).
        - ``"`` -> ``^"`` (caret-escapes a literal double quote).
        - ``%`` -> ``%%`` (batch-FILE convention for a literal ``%``;
          inside a ``.cmd`` file ``%%`` parses to a single ``%``. Caret
          escaping ``^%`` does NOT protect percent expansion in batch files).
    """
    return (
        value.replace("^", "^^")
             .replace("&", "^&")
             .replace('"', '^"')
             .replace("%", "%%")
    )


def _ps_single_quote(value: str) -> str:
    """Escape a value for inclusion inside a PowerShell single-quoted string.

    PowerShell single-quoted strings are LITERAL except for the embedded
    single quote, which is escaped by doubling: ``'it''s'`` parses as
    ``it's``. This is the PowerShell-canonical way to handle apostrophes
    in paths (see ``about_Quoting_Rules``).
    """
    return value.replace("'", "''")


# =============================================================================
# BIN-DIR RESOLVERS (Rule 1 — None sentinel + env override every call)
# =============================================================================


def _get_posix_bin_dir() -> Path:
    """Return the POSIX wrapper bin dir.

    Reads ``HOMIE_BIN_DIR`` env on every call (Rule 1 — never cached at
    module load). Default: ``~/.local/bin`` (the standard XDG-ish user
    bin dir; usually on ``$PATH`` already).
    """
    override = os.environ.get("HOMIE_BIN_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve(strict=False)
    return (Path.home() / ".local" / "bin").resolve(strict=False)


def _get_windows_bin_dir() -> Path:
    """Return the Windows wrapper bin dir.

    Reads ``HOMIE_BIN_DIR`` env on every call (Rule 1). Default:
    ``%USERPROFILE%\\AppData\\Local\\Programs\\thehomie\\bin`` — writable
    without admin and conventional for per-user CLI tools.
    """
    override = os.environ.get("HOMIE_BIN_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve(strict=False)
    return (
        Path.home() / "AppData" / "Local" / "Programs" / "thehomie" / "bin"
    ).resolve(strict=False)


# =============================================================================
# CONTENT TEMPLATES (R1 B3 — every template takes profile_root verbatim)
# =============================================================================
#
# All templates accept ``profile_root`` so the resulting alias / unit
# points at the SPECIFIC profile being created, NOT the active process's
# ``HOMIE_HOME``. All templates apply per-format escaping to ``profile_root``
# so paths containing spaces / apostrophes / ampersands round-trip
# correctly (R2 NM2).
#
# ``name`` is regex-validated upstream by ``validate_persona_name``
# (``^[a-z0-9][a-z0-9_-]{0,63}$``), so it is safe to interpolate verbatim.
# ``profile_root`` is NOT bounded — user ``$HOME`` can contain spaces,
# ampersands, apostrophes, carets, etc. — every template escapes it per
# the target format's quoting rules.


def _posix_wrapper_content(name: str, profile_root: Path) -> str:
    """POSIX shell wrapper content. Sets ``HOMIE_HOME`` for the target profile.

    Hermes anchor: ``hermes_cli/profiles.py:227-242`` — same shape, with
    explicit ``HOMIE_HOME`` export so the wrapper is target-profile-aware
    regardless of the caller's environment.

    R2 NM2: both ``profile_root`` and ``name`` go through ``shlex.quote()``
    so a path like ``/Users/Operator Name/.homie/profiles/sales`` doesn't
    fall apart into ``HOMIE_HOME=/Users/Operator Name/.homie/profiles/sales
    exec ...`` (where only ``/Users/Operator`` would be assigned and the
    rest parsed as a shell command).
    """
    quoted_root = shlex.quote(str(profile_root))
    quoted_name = shlex.quote(name)
    return (
        f'#!/bin/sh\n'
        f'HOMIE_HOME={quoted_root} exec thehomie -p {quoted_name} "$@"\n'
    )


def _windows_cmd_content(name: str, profile_root: Path) -> str:
    """Windows ``.cmd`` wrapper content.

    R3 NNM2: ``set "HOMIE_HOME=<escaped-value>"`` — quotes wrap the WHOLE
    ``KEY=value`` pair, NOT just the value. This is the batch-canonical
    shape: putting quotes only around the value (``set HOMIE_HOME="value"``)
    makes the literal ``"`` part of the env-var contents (the bot would
    later read ``HOMIE_HOME = "C:\\Users\\..."`` instead of
    ``HOMIE_HOME = C:\\Users\\...``). Wrapping the whole assignment keeps
    the equals sign part of the variable name and treats the value cleanly
    as everything after the ``=`` up to the closing quote.

    ``_cmd_escape()`` handles internal special chars (``^``, ``&``, ``"``,
    ``%``) so a value with ``&`` does not terminate the set statement,
    and ``%`` is doubled so paths like ``C:\\tmp\\100%-test\\`` survive
    batch-file parsing.
    """
    return (
        f'@echo off\r\n'
        f'set "HOMIE_HOME={_cmd_escape(str(profile_root))}"\r\n'
        f'thehomie -p {name} %*\r\n'
    )


def _windows_ps1_content(name: str, profile_root: Path) -> str:
    """PowerShell ``.ps1`` wrapper content.

    R2 NM2: PowerShell single-quoted strings are literal except for
    embedded single quotes, which get doubled. ``_ps_single_quote()``
    handles the apostrophe case so paths like ``C:\\Users\\O'Brien\\...``
    work.
    """
    return (
        f"$env:HOMIE_HOME = '{_ps_single_quote(str(profile_root))}'\r\n"
        f"thehomie -p {name} @args\r\n"
    )


def _launchd_plist_content(
    name: str, profile_root: Path, thehomie_path: str
) -> str:
    """launchd plist content (R2 NM2 — uses ``plistlib.dumps()`` instead of
    hand-built XML).

    Hand-built XML doesn't escape ``&``, ``<``, ``>``, ``"``, ``'`` in
    path values; a user home like ``/Users/A & B`` would produce malformed
    XML. ``plistlib`` handles every escape and produces parser-faithful
    output.
    """
    plist_dict = {
        "Label": f"com.thehomie.framework.{name}",
        "ProgramArguments": [thehomie_path, "-p", name, "chat"],
        "EnvironmentVariables": {"HOMIE_HOME": str(profile_root)},
        "WorkingDirectory": str(profile_root),
        "RunAtLoad": True,
        "StandardOutPath": str(profile_root / "logs" / "launchd.out"),
        "StandardErrorPath": str(profile_root / "logs" / "launchd.err"),
    }
    # ``plistlib.dumps()`` returns bytes (XML plist); decode to str so
    # the caller can ``write_text(..., encoding="utf-8")``.
    return plistlib.dumps(plist_dict).decode("utf-8")


def _systemd_value_escape(s: str) -> str:
    """Escape a value for inclusion inside a systemd ``KEY="value"`` pair.

    Per ``man systemd.exec`` ENVIRONMENT section, double-quoted values
    use POSIX-shell-like escapes: ``\\\\``, ``\\"``, ``\\$``.
    """
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")


def _systemd_unit_content(
    name: str, profile_root: Path, thehomie_path: str
) -> str:
    """systemd user unit content.

    R2 NM2 + R3 NNM2: paths are double-quote-wrapped in ``Environment=``
    AND ``WorkingDirectory=`` (consistent quoting). systemd unit files
    accept quoted values for both keys per ``man systemd.exec``. Without
    quotes systemd strips at the first whitespace, breaking paths that
    contain spaces (e.g. ``/home/Operator Name/.homie/profiles/sales``).
    Backslash + double-quote + dollar-sign characters are escaped per
    ``man systemd.exec``.
    """
    quoted_root = _systemd_value_escape(str(profile_root))
    return (
        '[Unit]\n'
        f'Description=The Homie - Persona {name}\n'
        'After=network-online.target\n\n'
        '[Service]\n'
        'Type=simple\n'
        f'ExecStart="{thehomie_path}" -p {name} chat\n'
        f'WorkingDirectory="{quoted_root}"\n'
        f'Environment="HOMIE_HOME={quoted_root}"\n'
        f'StandardOutput=append:{quoted_root}/logs/systemd.out\n'
        f'StandardError=append:{quoted_root}/logs/systemd.err\n'
        'Restart=on-failure\n\n'
        '[Install]\n'
        'WantedBy=default.target\n'
    )


# =============================================================================
# COLLISION CHECK (Hermes-faithful — content-verify before overwrite)
# =============================================================================


def _check_alias_collision(name: str) -> Optional[str]:
    """Return a non-None message if ``<name>-homie`` is already on PATH and
    is NOT one of our wrappers; return ``None`` if there's no collision OR
    the existing file IS one of our wrappers (safe to overwrite).

    Hermes anchor: ``hermes_cli/profiles.py:188-218`` — Hermes uses the
    same content-signature check (``hermes -p`` in our case
    ``thehomie -p``).

    The check runs ``shutil.which(<name>-homie)`` (Rule 2 — physical PATH
    walk) and reads the resulting file content. If the file contains
    ``thehomie -p`` it's recognized as our own wrapper from a previous
    install — safe to overwrite. Otherwise return a message describing
    the collision so the caller can decide.
    """
    found = shutil.which(f"{name}-homie")
    if found is None:
        return None
    found_path = Path(found)
    try:
        content = found_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # Couldn't read it — treat as collision (fail CLOSED).
        return (
            f"Existing alias at {found_path} could not be inspected; "
            f"refusing to overwrite."
        )
    if "thehomie -p" in content:
        # It's our own wrapper from a previous install — overwriting is
        # safe (caller will rewrite the same file with the new profile_root).
        return None
    return (
        f"Alias name '{name}-homie' collides with an existing file at "
        f"{found_path} that is not one of our wrappers."
    )


# =============================================================================
# FILE CREATORS (POSIX + Windows)
# =============================================================================


def _create_posix_wrapper(
    name: str,
    profile_root: Path,
    bin_dir: Optional[Path] = None,
) -> Path:
    """Write the POSIX shell wrapper to ``<bin_dir>/<name>-homie`` and
    chmod it executable.

    R1 B3: ``profile_root`` is REQUIRED — used verbatim in the wrapper
    content. Never resolves ``get_homie_home()`` (that would return the
    current process's profile, which is wrong when creating from a
    different active profile).

    Rule 1 None-sentinel: ``bin_dir=None`` is resolved at call time via
    ``_get_posix_bin_dir()`` so the env override picks up at runtime.
    """
    if bin_dir is None:
        bin_dir = _get_posix_bin_dir()
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper_path = bin_dir / f"{name}-homie"
    wrapper_path.write_text(
        _posix_wrapper_content(name, profile_root), encoding="utf-8"
    )
    # chmod +x (owner+group+other execute) on top of existing perms.
    try:
        st = wrapper_path.stat()
        wrapper_path.chmod(st.st_mode | 0o111)
    except OSError:
        # Best-effort — some filesystems (e.g. SMB / FAT) don't honor
        # POSIX perms. The wrapper still exists; user can invoke it via
        # ``sh <name>-homie`` if needed.
        pass
    return wrapper_path


def _create_windows_wrappers(
    name: str,
    profile_root: Path,
    bin_dir: Optional[Path] = None,
) -> tuple[Path, Path]:
    """Write Windows ``.cmd`` and ``.ps1`` wrappers; return both paths.

    R1 B3: ``profile_root`` is REQUIRED — used verbatim in both wrapper
    contents.

    Deviation 3 (PRD §9.1): Windows ships BOTH so the alias works whether
    invoked from cmd.exe or PowerShell.
    """
    if bin_dir is None:
        bin_dir = _get_windows_bin_dir()
    bin_dir.mkdir(parents=True, exist_ok=True)
    cmd_path = bin_dir / f"{name}-homie.cmd"
    ps1_path = bin_dir / f"{name}-homie.ps1"
    cmd_path.write_text(
        _windows_cmd_content(name, profile_root), encoding="utf-8"
    )
    ps1_path.write_text(
        _windows_ps1_content(name, profile_root), encoding="utf-8"
    )
    return cmd_path, ps1_path


# =============================================================================
# LAUNCHD (macOS) INSTALL / UNINSTALL
# =============================================================================


def _launchd_plist_path(name: str) -> Path:
    """Return the canonical path for the launchd plist.

    ``~/Library/LaunchAgents/com.thehomie.framework.<name>.plist`` per
    PRD §9.1 + Cross-Platform Notes table.
    """
    return (
        Path.home() / "Library" / "LaunchAgents"
        / f"com.thehomie.framework.{name}.plist"
    ).resolve(strict=False)


def _resolve_thehomie_path() -> str:
    """Return the path to the ``thehomie`` executable, or fall back to the
    bare name so launchd / systemd consult ``$PATH``.
    """
    found = shutil.which("thehomie")
    return found if found is not None else "thehomie"


def _install_launchd_plist(name: str, profile_root: Path) -> Path:
    """Write the launchd plist for *name* and ``launchctl load -w`` it.

    R1 B3: ``profile_root`` is REQUIRED. The plist's ``WorkingDirectory``
    and ``EnvironmentVariables.HOMIE_HOME`` both point at *profile_root*
    verbatim — never the current process's ``HOMIE_HOME``.

    Tests MOCK ``subprocess.run``. ``launchctl`` failure does NOT raise
    here; the file is still on disk and the operator can ``launchctl load``
    manually.
    """
    plist_path = _launchd_plist_path(name)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    thehomie_path = _resolve_thehomie_path()
    content = _launchd_plist_content(name, profile_root, thehomie_path)
    plist_path.write_text(content, encoding="utf-8")
    try:
        subprocess.run(
            ["launchctl", "load", "-w", str(plist_path)],
            check=False,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):
        # Best-effort — file is on disk; operator can load manually.
        pass
    return plist_path


def _uninstall_launchd_plist(name: str) -> bool:
    """Reverse ``_install_launchd_plist``. Returns True iff the plist was
    found and removed (best-effort ``launchctl unload`` first).

    ``profile_root`` is NOT needed — the unit name is
    ``com.thehomie.framework.<name>`` deterministically.
    """
    plist_path = _launchd_plist_path(name)
    if not plist_path.exists():
        return False
    try:
        subprocess.run(
            ["launchctl", "unload", str(plist_path)],
            check=False,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        plist_path.unlink()
    except OSError:
        return False
    return True


# =============================================================================
# SYSTEMD (Linux) INSTALL / UNINSTALL
# =============================================================================


def _systemd_unit_path(name: str) -> Path:
    """Return the canonical path for the systemd user unit.

    ``~/.config/systemd/user/homie-<name>.service`` per PRD §9.1.
    """
    return (
        Path.home() / ".config" / "systemd" / "user"
        / f"homie-{name}.service"
    ).resolve(strict=False)


def _install_systemd_unit(name: str, profile_root: Path) -> Path:
    """Write the systemd user unit for *name* and ``systemctl --user
    enable --now`` it.

    R1 B3: ``profile_root`` is REQUIRED. The unit's ``WorkingDirectory``
    and ``Environment="HOMIE_HOME=..."`` both point at *profile_root*
    verbatim.

    Tests MOCK ``subprocess.run``. systemctl failure does NOT raise here;
    the file is still on disk and the operator can ``daemon-reload`` /
    ``enable`` manually.
    """
    unit_path = _systemd_unit_path(name)
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    thehomie_path = _resolve_thehomie_path()
    content = _systemd_unit_content(name, profile_root, thehomie_path)
    unit_path.write_text(content, encoding="utf-8")
    svc_name = f"homie-{name}.service"
    for argv in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", svc_name],
    ):
        try:
            subprocess.run(argv, check=False, capture_output=True)
        except (OSError, subprocess.SubprocessError):
            # Best-effort — file is on disk; operator can enable manually.
            pass
    return unit_path


def _uninstall_systemd_unit(name: str) -> bool:
    """Reverse ``_install_systemd_unit``. Returns True iff the unit file
    was found and removed (best-effort ``systemctl --user disable / stop``
    first).

    ``profile_root`` is NOT needed — the unit name is
    ``homie-<name>.service`` deterministically.
    """
    unit_path = _systemd_unit_path(name)
    if not unit_path.exists():
        return False
    svc_name = f"homie-{name}.service"
    for argv in (
        ["systemctl", "--user", "disable", svc_name],
        ["systemctl", "--user", "stop", svc_name],
    ):
        try:
            subprocess.run(argv, check=False, capture_output=True)
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        unit_path.unlink()
    except OSError:
        return False
    try:
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            check=False,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    return True


# =============================================================================
# PUBLIC API — create_wrapper_alias / remove_wrapper_alias
# =============================================================================


def create_wrapper_alias(
    name: str,
    profile_root: Path,
    *,
    install_launchd: bool = False,
    install_systemd: bool = False,
    bin_dir: Optional[Path] = None,
) -> WrapperPaths:
    """Create per-OS wrapper alias for a SPECIFIC profile root.

    R1 B3 contract (LOAD-BEARING): ``profile_root`` is REQUIRED and used
    verbatim in every generated wrapper / unit. Never resolves
    ``get_homie_home()`` — that returns the CURRENT process profile,
    which is the wrong target when creating a new profile from a
    different active profile.

    R1 B4 + R3 NNM3 contract: OS-mismatched flags raise ``LifecycleError``
    BEFORE any file write. ``personas.lifecycle.create_profile`` ALSO
    pre-validates these flags — defense in depth so a partial profile
    dir never lands on disk.

    Hermes anchor: ``hermes_cli/profiles.py:227-242`` (POSIX shell
    wrapper byte content). The Homie writes ``<name>-homie`` filename
    (not bare ``<name>``) per Deviation 2 (PRD §9.1).

    Args:
        name: Persona name. Validated via ``validate_persona_name``.
        profile_root: On-disk root of the target profile. Used verbatim
            in every template; the wrapper sets ``HOMIE_HOME=<profile_root>``
            BEFORE invoking ``thehomie -p <name>``.
        install_launchd: macOS-only. Install launchd plist at
            ``~/Library/LaunchAgents/com.thehomie.framework.<name>.plist``.
        install_systemd: Linux-only. Install systemd user unit at
            ``~/.config/systemd/user/homie-<name>.service``.
        bin_dir: None-sentinel. Resolved via
            ``_get_posix_bin_dir()`` / ``_get_windows_bin_dir()`` at call
            time so the ``HOMIE_BIN_DIR`` env override is honored.

    Returns:
        ``WrapperPaths`` populated with the files that were written.
    """
    # Lazy import — `LifecycleError` lives in `personas.lifecycle`, which
    # imports this module. Importing at module top would cycle.
    from .lifecycle import LifecycleError

    validate_persona_name(name)

    # R1 B4 + R3 NNM3 — validate OS-specific flags BEFORE any file write.
    if install_launchd and sys.platform != "darwin":
        raise LifecycleError(
            f"--install-launchd requires macOS (darwin); current platform "
            f"is {sys.platform!r}."
        )
    if install_systemd and sys.platform != "linux":
        raise LifecycleError(
            f"--install-systemd requires Linux; current platform is "
            f"{sys.platform!r}."
        )

    paths = WrapperPaths()
    if sys.platform == "win32":
        paths.windows_cmd, paths.windows_ps1 = _create_windows_wrappers(
            name, profile_root, bin_dir,
        )
    else:
        paths.posix_shell = _create_posix_wrapper(
            name, profile_root, bin_dir,
        )

    if install_launchd:
        paths.launchd_plist = _install_launchd_plist(name, profile_root)
    if install_systemd:
        paths.systemd_unit = _install_systemd_unit(name, profile_root)
    return paths


def _verify_then_unlink(path: Path) -> bool:
    """Hermes-faithful content verify before unlink.

    Reads the file at *path* and only unlinks it if the content carries
    the framework signature (``thehomie -p``). If the file is missing
    return False; if the content does NOT carry the signature, leave the
    file alone and return False (we don't own it). On unlink success
    return True.
    """
    if not path.exists():
        return False
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if "thehomie -p" not in content:
        # Not our wrapper — leave it alone.
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def remove_wrapper_alias(name: str) -> None:
    """Reverse ``create_wrapper_alias`` for *name*.

    Hermes-faithful pattern: verify content carries the framework
    signature (``thehomie -p``) BEFORE unlink so a user-authored shell
    script that happens to share the name is never destroyed. Per-OS
    cleanup is best-effort — missing files are ignored.

    Walks both POSIX and Windows bin dirs so a profile created on one
    platform but removed in a cross-platform replay sweep still gets
    cleaned. ``profile_root`` is NOT needed — this layer only operates
    on canonical paths derived from *name* + the bin-dir resolvers.

    On macOS / Linux ALSO uninstalls the launchd plist / systemd unit
    deterministically (those are idempotent — return False if missing).
    """
    # POSIX wrapper.
    posix_bin = _get_posix_bin_dir()
    _verify_then_unlink(posix_bin / f"{name}-homie")

    # Windows wrappers — ``.cmd`` carries ``thehomie -p`` so the verify
    # check passes; ``.ps1`` also does.
    windows_bin = _get_windows_bin_dir()
    _verify_then_unlink(windows_bin / f"{name}-homie.cmd")
    _verify_then_unlink(windows_bin / f"{name}-homie.ps1")

    # macOS launchd / Linux systemd cleanup. Both helpers are
    # platform-aware and tolerate missing files.
    if sys.platform == "darwin":
        _uninstall_launchd_plist(name)
    elif sys.platform == "linux":
        _uninstall_systemd_unit(name)
