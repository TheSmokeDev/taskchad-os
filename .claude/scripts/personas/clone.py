"""Profile clone, export, and import — including path-traversal-safe extract.

Phase 2 / PRP-7b Workstream 3. Owns the clone copy primitives that
``personas.lifecycle.create_profile`` calls into via lazy imports
(``from .clone import _copytree_with_strip / _clone_config_files /
_clone_subdir_files``), plus the operator-facing ``clone_profile``,
``export_profile``, and ``import_profile`` helpers.

Hermes-faithful clone deviation (PRD §7.12, §14.25):
    By default ``--clone`` strips ``.env`` tokens via ``_strip_env_secrets``.
    Hermes copies ``.env`` verbatim. The Homie deviates because Telegram
    allows ONE polling process per ``TELEGRAM_BOT_TOKEN`` — sharing the
    token between profiles guarantees collision (the second bot startup
    fails or both fight over the polling lease). ``--clone-secrets``
    opts back into the Hermes-faithful verbatim copy for users who want
    it (e.g. cloning into a sandbox where the token won't actually start
    a bot).

Module exports (consumed by ``chat/cli.py`` Click handlers + lifecycle.py):
    clone_profile(src_name, dst_name, *, full=False, carry_secrets=False)
    export_profile(name, output_path=None) -> Path
    import_profile(archive_path, *, as_name=None, force=False) -> Path

Helpers (private — same-package consumers in lifecycle.py + tests):
    _strip_env_secrets(env_text) -> str
    _CLONE_CONFIG_FILES, _CLONE_SUBDIR_FILES, _CLONE_ALL_STRIP constants
    _clone_config_files(src, dst, *, carry_secrets)
    _clone_subdir_files(src, dst)
    _copytree_with_strip(src, dst, *, carry_secrets)
    _normalize_profile_archive_parts(member_name) -> list[str]
    _safe_extract_profile_archive(archive, destination)
    _inspect_profile_archive_roots(archive) -> set[str]

Anti-pattern compliance:
    - Rule 1 (None sentinel): ``export_profile`` defaults ``output_path=None``
      and resolves the timestamped exports path INSIDE the function body
      (``get_default_homie_root() / "exports"``).
    - Rule 2 (physical state): ``_inspect_profile_archive_roots`` reads
      ``tarfile.getmembers()`` directly — no sidecar manifest.
    - Rule 3 (langfuse module-attribute import): N/A — no Langfuse.

Hermes anchors:
    - hermes_cli/profiles.py:814-832 (_normalize_profile_archive_parts)
    - hermes_cli/profiles.py:834-863 (member iteration shape for
      _safe_extract_profile_archive)
"""

from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Optional

from .core import get_default_homie_root, validate_persona_name


# =============================================================================
# CLONE FILE INVENTORIES (Hermes parallel, extended for The Homie layout)
# =============================================================================
#
# Hermes anchors:
#   _CLONE_CONFIG_FILES — Hermes uses ["config.yaml", ".env", "SOUL.md"];
#   The Homie extends to memory/ identity files. Phase 2 deviation: ``.env``
#   is secret-stripped by default unless --clone-secrets is passed.

_CLONE_CONFIG_FILES: list[str] = [
    "config.yaml",
    ".env",
    "memory/SOUL.md",
]

_CLONE_SUBDIR_FILES: list[str] = [
    "memory/MEMORY.md",
    "memory/USER.md",
]

# Files / dirs stripped after --clone-all (Hermes pattern, extended).
# Hermes' equivalents are gateway.* — The Homie ports to bot.* + adds the
# state-snapshot files that should not survive a clone.
_CLONE_ALL_STRIP: list[str] = [
    "run/bot.pid",
    "run/bot.lock",
    "state/heartbeat-state.json",
    "state/dream-state.json",
    "state/reflection-state.json",
    "state/weekly-state.json",
    ".delete.lock",
]


# =============================================================================
# SECRET-STRIP HELPER (Phase 2 deviation from Hermes)
# =============================================================================


def _strip_env_secrets(env_text: str) -> str:
    """Replace ``KEY=value`` with ``KEY=`` line-by-line, preserving
    comments and blank lines.

    Phase 2 deviation from Hermes (PRD §7.12, §14.25): secrets are stripped
    by default during ``--clone``. ``--clone-secrets`` opts back into the
    Hermes-faithful verbatim-copy behavior. Rationale: Telegram allows
    ONE polling process per ``TELEGRAM_BOT_TOKEN``; sharing creates
    collision. Default-strip protects the most-common multi-persona use
    case.

    Scope (R1 minor — explicitly limited):
        - Plain ``KEY=value`` and ``KEY="value"`` are stripped to ``KEY=``.
        - Lines starting with ``#`` (comments) are preserved untouched.
        - Blank lines are preserved untouched.
        - ``export KEY=value`` is OUT OF SCOPE — ``.env`` files consumed
          by ``python-dotenv`` don't use ``export``. If a user manually
          writes ``export FOO=secret`` into ``.env``, this function
          preserves the line (which would leak the secret on ``--clone``);
          tests assert this scope explicitly so the limitation is visible.
        - Trailing newline (LF or no-LF) is preserved on the output.
    """
    lines: list[str] = []
    for line in env_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            lines.append(line)
            continue
        if "=" in stripped:
            key, _, _value = stripped.partition("=")
            lines.append(f"{key}=")
        else:
            lines.append(line)
    out = "\n".join(lines)
    if env_text.endswith("\n"):
        out += "\n"
    return out


# =============================================================================
# COPY HELPERS — invoked by lifecycle.create_profile via lazy import
# =============================================================================


def _copytree_with_strip(
    src: Path, dst: Path, *, carry_secrets: bool
) -> None:
    """Full ``shutil.copytree`` with runtime-state strip + optional secret
    strip on ``.env``.

    Used by the ``--clone-all`` lifecycle path. After copying, removes the
    files / dirs in ``_CLONE_ALL_STRIP`` (best-effort) plus all ``*.lock``
    files via ``shutil.ignore_patterns``. If ``carry_secrets=False``,
    rewrites ``dst/.env`` through ``_strip_env_secrets``.

    R2 NM5 — note: lifecycle's ``create_profile`` re-runs the
    ``_REQUIRED_*`` bootstrap loops AFTER this helper to backfill any
    Phase 2 dirs the source was missing. This helper does NOT itself
    bootstrap missing dirs.

    R-post-build F3 — fail CLOSED on secret-strip failure. When
    ``carry_secrets=False``, the ``.env`` rewrite MUST succeed or this
    helper raises ``OSError``. The previous "best-effort" pass meant a
    Windows readonly attribute / ACL on ``dst/.env`` would silently leave
    the verbatim copy in place — clone-all would succeed with full
    secrets exposed. The new contract: rewrite-or-raise.
    """
    # Initial copy (NO symlinks — symlinks=False) ignoring transient state.
    shutil.copytree(
        src,
        dst,
        symlinks=False,
        ignore=shutil.ignore_patterns(
            "*.lock",
            "bot.pid",
            "bot.log",
            "*.log",
            "__pycache__",
            "*.tmp",
        ),
    )
    # Explicit strip of named runtime files / dirs.
    for stale in _CLONE_ALL_STRIP:
        path = dst / stale
        try:
            path.unlink(missing_ok=True)
        except IsADirectoryError:
            shutil.rmtree(path, ignore_errors=True)
        except OSError:
            # Best-effort — don't block clone on cleanup failure.
            pass
    # Secret-strip ``.env`` unless --clone-secrets. R-post-build F3 —
    # fail CLOSED: if the rewrite fails (Windows readonly attr, ACL,
    # disk error), DO NOT keep the verbatim copy. Either rewrite
    # successfully, or raise so the caller knows the clone is unsafe.
    if not carry_secrets:
        env_path = dst / ".env"
        if env_path.exists():
            try:
                env_path.write_text(
                    _strip_env_secrets(
                        env_path.read_text(encoding="utf-8")
                    ),
                    encoding="utf-8",
                )
            except OSError as exc:
                # Don't leave secrets behind. Try to remove the verbatim
                # copy first (so an early failure mode doesn't leak). If
                # removal also fails, raise the original error so the
                # caller sees "clone failed" and can rerun, instead of
                # "clone succeeded with secrets present".
                try:
                    env_path.unlink()
                except OSError:
                    pass
                raise OSError(
                    f"Failed to strip secrets from {env_path}; "
                    f"refusing to leave verbatim .env at clone destination."
                ) from exc


def _resolve_clone_source_path(
    src: Path,
    relpath: str,
    *,
    source_memory_dir: Optional[Path] = None,
) -> Path:
    """Resolve a clone-source path with an optional default-memory adapter.

    R-post-build F5 — when cloning from ``"default"``, the default
    profile's memory lives at ``<install>/vault/memory/``, NOT under
    a ``memory/`` subdir of the source. Without this adapter, the clone
    helpers look for ``<install>/memory/SOUL.md`` and silently miss the
    real default identity files.

    When *source_memory_dir* is provided AND *relpath* starts with
    ``memory/``, this helper rewrites the lookup to land inside
    *source_memory_dir* (e.g. ``memory/SOUL.md`` ->
    ``<source_memory_dir>/SOUL.md``). Otherwise the lookup is the
    straight ``src / relpath`` join.

    Used by ``_clone_config_files`` and ``_clone_subdir_files``.
    """
    if (
        source_memory_dir is not None
        and relpath.startswith("memory/")
    ):
        # ``relpath = "memory/SOUL.md"`` -> ``source_memory_dir / "SOUL.md"``.
        rel_inside_memory = relpath[len("memory/"):]
        return source_memory_dir / rel_inside_memory
    return src / relpath


def _clone_config_files(
    src: Path,
    dst: Path,
    *,
    carry_secrets: bool,
    source_memory_dir: Optional[Path] = None,
) -> None:
    """Copy ``_CLONE_CONFIG_FILES`` from *src* to *dst*; ``.env`` is
    secret-stripped unless ``carry_secrets=True``.

    Used by the light-clone lifecycle path (``--clone`` without
    ``--clone-all``). Missing source files are silently skipped — the
    light-clone is best-effort: identity files that exist get copied,
    everything else gets the seeded empty body from
    ``_seed_identity_body``.

    R-post-build F5: ``source_memory_dir`` is None-sentinel. When
    provided (e.g. ``clone_from="default"``), every ``memory/*`` lookup
    is rewritten to land inside that dir instead of ``src/memory/*``.
    """
    for relpath in _CLONE_CONFIG_FILES:
        src_path = _resolve_clone_source_path(
            src, relpath, source_memory_dir=source_memory_dir
        )
        if not src_path.exists():
            continue
        dst_path = dst / relpath
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        if relpath == ".env" and not carry_secrets:
            dst_path.write_text(
                _strip_env_secrets(
                    src_path.read_text(encoding="utf-8")
                ),
                encoding="utf-8",
            )
        else:
            shutil.copy2(src_path, dst_path)


def _clone_subdir_files(
    src: Path,
    dst: Path,
    *,
    source_memory_dir: Optional[Path] = None,
) -> None:
    """Copy ``_CLONE_SUBDIR_FILES`` (``memory/*``) from *src* to *dst*.

    These files never contain secrets so the secret-strip path doesn't
    apply. Missing source files are silently skipped.

    R-post-build F5: ``source_memory_dir`` is None-sentinel. When
    provided (e.g. ``clone_from="default"``), every ``memory/*`` lookup
    is rewritten to land inside that dir.
    """
    for relpath in _CLONE_SUBDIR_FILES:
        src_path = _resolve_clone_source_path(
            src, relpath, source_memory_dir=source_memory_dir
        )
        if not src_path.exists():
            continue
        dst_path = dst / relpath
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dst_path)


# =============================================================================
# CLONE_PROFILE — operator-facing wrapper
# =============================================================================


def clone_profile(
    src_name: str,
    dst_name: str,
    *,
    full: bool = False,
    carry_secrets: bool = False,
) -> Path:
    """Clone profile *src_name* into a new profile *dst_name*.

    Convenience wrapper around the same private helpers
    ``personas.lifecycle.create_profile`` uses in its clone branch.

    Args:
        src_name: Source profile name. ``"default"`` is special-cased to
            the install-dir layout (Phase 1's ``get_default_paths``).
            Any other name is validated via ``validate_persona_name``
            and resolved via ``personas.lifecycle._profile_root``.
        dst_name: Destination profile name. Validated. Cannot be
            ``"default"`` (built-in profile is not a clone target).
        full: If True, runs ``_copytree_with_strip`` (full tree copy
            with runtime-state strip). If False, runs the light-clone
            path (``_clone_config_files`` + ``_clone_subdir_files``).
        carry_secrets: If True, copies ``.env`` verbatim (Hermes-faithful).
            If False (default), strips ``.env`` tokens via
            ``_strip_env_secrets``.

    Returns:
        Path to the newly-created destination profile dir.

    Raises:
        ValueError: ``dst_name`` is invalid, reserved, or ``"default"``.
        FileNotFoundError: Source profile does not exist.
        FileExistsError: Destination profile already exists.
    """
    # Lazy import to avoid a hard dependency cycle: lifecycle imports
    # clone (via the call sites in create_profile), so clone must lazy-
    # import lifecycle's helpers.
    from .core import get_default_paths
    from .lifecycle import (
        _default_install_root_for_clone,
        _profile_root,
    )

    # Destination validation.
    validate_persona_name(dst_name)
    if dst_name == "default":
        # Defense in depth — `validate_persona_name` already rejects
        # "default" because it lives in `core._RESERVED`. Kept as a
        # readable error path for direct callers.
        raise ValueError("Cannot clone to 'default' — it is the built-in profile.")

    # Source resolution — `"default"` is a legitimate clone SOURCE.
    # R-post-build F5: when source is "default", the memory dir is at
    # ``<install>/vault/memory/``, NOT ``<install>/memory/``. Pass
    # the real memory dir as ``source_memory_dir`` so the clone helpers
    # source identity files from the right place.
    source_memory_dir: Optional[Path] = None
    if src_name == "default":
        source_dir = _default_install_root_for_clone()
        source_memory_dir = get_default_paths()["memory"]
    else:
        validate_persona_name(src_name)
        source_dir = _profile_root(src_name)
    if not source_dir.is_dir():
        raise FileNotFoundError(
            f"Source profile '{src_name}' does not exist at {source_dir}"
        )

    dst_dir = _profile_root(dst_name)
    if dst_dir.exists():
        raise FileExistsError(
            f"Profile '{dst_name}' already exists at {dst_dir}"
        )

    if full:
        _copytree_with_strip(source_dir, dst_dir, carry_secrets=carry_secrets)
    else:
        dst_dir.mkdir(parents=True, exist_ok=True)
        _clone_config_files(
            source_dir,
            dst_dir,
            carry_secrets=carry_secrets,
            source_memory_dir=source_memory_dir,
        )
        _clone_subdir_files(
            source_dir, dst_dir, source_memory_dir=source_memory_dir
        )

    return dst_dir


# =============================================================================
# ARCHIVE NORMALIZATION + SAFE EXTRACT (R1 BLOCKER carry-over from Phase 1)
# =============================================================================


def _normalize_profile_archive_parts(member_name: str) -> list[str]:
    """Reject absolute paths, ``..`` components, drive letters, and empty
    components.

    Hermes anchor: ``hermes_cli/profiles.py:814-832`` (verbatim shape).
    Returns a list of POSIX-style path components safe to ``Path.joinpath``
    onto the destination. Raises ``ValueError`` on any unsafe shape.
    """
    if not member_name:
        raise ValueError(f"Unsafe archive member: {member_name!r}")
    normalized = member_name.replace("\\", "/")
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(member_name)
    if (
        not normalized
        or posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
    ):
        raise ValueError(f"Unsafe archive member: {member_name!r}")
    parts = [p for p in posix.parts if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise ValueError(f"Unsafe archive member: {member_name!r}")
    return parts


def _inspect_profile_archive_roots(archive: Path) -> set[str]:
    """Return the set of top-level directory names in *archive*.

    Hermes-faithful: ``import_profile`` requires exactly one top-level
    directory (the profile name). Multi-root archives are rejected by
    the caller. Empty archive returns ``set()`` and the caller raises.

    Reads members directly via ``tarfile.getmembers()`` (Rule 2 —
    physical state, no sidecar manifest).
    """
    roots: set[str] = set()
    with tarfile.open(archive, "r:gz") as tf:
        for member in tf.getmembers():
            if not member.name:
                continue
            try:
                parts = _normalize_profile_archive_parts(member.name)
            except ValueError:
                # Unsafe member name in the archive — bubble the rejection
                # up to the caller for a coherent error message.
                raise
            if parts:
                roots.add(parts[0])
    return roots


def _safe_extract_profile_archive(archive: Path, destination: Path) -> None:
    """Extract *archive* into *destination* with two-layer defense.

    LAYER 1 (Python 3.12+ PEP 706): per-member
    ``tarfile.data_filter(member, str(destination))`` — raises
    ``tarfile.FilterError`` on symlinks pointing outside, absolute paths,
    ``..`` components, device files, FIFOs, etc.

    LAYER 2 (Hermes shape — defense in depth): manual member-by-member
    iteration with ``_normalize_profile_archive_parts`` (rejects drive
    letters and empty components — ``data_filter`` on POSIX may not
    normalize Windows-style absolutes) plus ``is_relative_to``
    containment check on the resolved target.

    R1 M2 fix (carried over from Phase 1): the earlier draft set
    ``tf.extraction_filter`` and iterated via ``extractfile()`` — which
    means the data_filter never actually ran (``extractfile`` is a raw
    stream read, NOT extraction). This implementation EXPLICITLY calls
    ``tarfile.data_filter(member, str(destination))`` per member so it
    raises BEFORE the manual containment check is reached. The test
    suite includes a monkeypatch on ``tarfile.data_filter`` that asserts
    it was called exactly once per non-rejected member.
    """
    destination = destination.resolve()
    with tarfile.open(archive, "r:gz") as tf:
        for member in tf.getmembers():
            # Layer 1 (R1 M2): explicit per-member data_filter. Raises
            # ``tarfile.FilterError`` on absolute paths, ``..`` traversal,
            # symlinks pointing outside the destination, device/FIFO
            # members, etc.
            tarfile.data_filter(member, str(destination))

            # Layer 2 (Hermes): manual containment + drive-letter / empty-
            # part check. Catches Windows-style absolute paths that
            # ``data_filter`` on POSIX may not normalize.
            parts = _normalize_profile_archive_parts(member.name)
            target = destination.joinpath(*parts)
            try:
                target.resolve().relative_to(destination)
            except ValueError:
                raise ValueError(
                    f"Archive member {member.name!r} would extract outside "
                    f"destination: {target}"
                )

            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                # Reject symlinks, devices, FIFOs (data_filter would have
                # already rejected most of these — defense in depth).
                raise ValueError(
                    f"Unsupported archive member type: {member.name!r}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tf.extractfile(member)
            if extracted is None:
                raise ValueError(
                    f"Cannot read archive member: {member.name!r}"
                )
            with extracted, open(target, "wb") as dst:
                shutil.copyfileobj(extracted, dst)
            try:
                os.chmod(target, member.mode & 0o777)
            except OSError:
                # Some filesystems (e.g. SMB / FAT) don't honor POSIX
                # perms — best-effort, don't block extraction.
                pass


# =============================================================================
# EXPORT_PROFILE — operator-facing
# =============================================================================


def _default_export_ignore(_dir: str, names: list[str]) -> list[str]:
    """Ignore patterns for ``shutil.copytree`` during export staging.

    Mirrors Hermes' export ignore list: excludes infrastructure dirs
    (caches, virtual envs, worktrees), credentials directories, and
    lock / log files.

    R-post-build F2 / F3 hardening:
        - Deny ``.env`` AT EVERY DEPTH, not just at the staging root. The
          previous version relied on a top-level post-stage ``unlink`` of
          ``staged/.env``, which missed nested ``.env`` files (e.g.
          ``staged/.claude/scripts/.env`` — exactly the case in the
          install-repo layout).
        - Deny ``credentials`` AT EVERY DEPTH, not just at the staging
          root. Same reasoning — ``staged/.claude/scripts/integrations/``
          is the real install-dir credentials home.
        - Deny common token / cred filenames by suffix (``*.pem``,
          ``*token*.json``, ``*credentials*.json``) — defense in depth so
          a stray cred file outside ``credentials/`` cannot land in the
          archive.

    The post-stage scan in ``export_profile`` validates this filter by
    walking the staged tree and raising if any secret path slipped
    through.
    """
    ignored: list[str] = []
    for n in names:
        lower = n.lower()
        # Always-deny: caches, venvs, worktrees, model caches.
        if n in {
            "__pycache__",
            ".venv",
            "venv",
            "node_modules",
            "models",
            ".git",
            "claude-worktrees",
            "codex-worktrees",
            # F2 — credentials at every depth.
            "credentials",
            "integrations",  # install-dir credentials root
        }:
            ignored.append(n)
            continue
        # Always-deny: lock / pid / log files (transient runtime state).
        if (
            n.endswith(".lock")
            or n.endswith(".log")
            or n.endswith(".tmp")
            or n in {"bot.pid"}
        ):
            ignored.append(n)
            continue
        # F2 — deny .env at every depth (nested .env in install repo
        # layout would otherwise slip through a top-level-only strip).
        if n == ".env" or n.startswith(".env."):
            ignored.append(n)
            continue
        # F2 defense-in-depth — deny common token / credential filenames.
        if (
            lower.endswith(".pem")
            or "token" in lower and lower.endswith(".json")
            or "credentials" in lower and lower.endswith(".json")
            or lower.endswith(".key")
        ):
            ignored.append(n)
            continue
    return ignored


# Filename / path patterns that MUST NOT appear in a staged export tree.
# Used by ``_assert_no_secrets_in_staged_tree`` to fail CLOSED if the
# ignore filter let anything through (defense in depth).
_SECRET_DENY_BASENAMES = frozenset({".env"})


def _is_secret_path(rel_parts: tuple[str, ...]) -> bool:
    """Return True if a staged-tree relative path looks like a secret.

    Used by the post-stage scanner. ``rel_parts`` is the tuple of path
    components relative to the staging root (e.g.
    ``(".claude", "scripts", ".env")`` or
    ``("credentials", "google_token.json")``).
    """
    if not rel_parts:
        return False
    last = rel_parts[-1].lower()
    # Any .env (root or nested).
    if rel_parts[-1] in _SECRET_DENY_BASENAMES:
        return True
    if rel_parts[-1].startswith(".env."):
        return True
    # Anything under a credentials/ or integrations/ dir.
    for part in rel_parts[:-1]:
        if part in {"credentials", "integrations"}:
            return True
    # Common credential filename heuristics.
    if last.endswith(".pem") or last.endswith(".key"):
        return True
    if "token" in last and last.endswith(".json"):
        return True
    if "credentials" in last and last.endswith(".json"):
        return True
    return False


def _assert_no_secrets_in_staged_tree(staged: Path) -> None:
    """R-post-build F3 — walk *staged* and raise if any secret path remains.

    The ``shutil.copytree`` ignore filter denies known secret shapes up
    front; this scan is the post-stage validator that proves nothing
    slipped through. If ANY ``.env``, ``credentials/``-anchored path, or
    token-shaped file is found, the export FAILS CLOSED — we never
    write a tarball that could contain a real token.
    """
    leaks: list[str] = []
    for path in staged.rglob("*"):
        if path.is_dir():
            continue
        try:
            rel = path.relative_to(staged)
        except ValueError:
            continue
        if _is_secret_path(rel.parts):
            leaks.append(str(rel))
    if leaks:
        raise RuntimeError(
            "Refusing to export profile archive — secret-shaped paths "
            f"survived the ignore filter: {leaks[:10]}"
            + (f" (and {len(leaks) - 10} more)" if len(leaks) > 10 else "")
        )


def _stage_default_export_tree(staged: Path) -> None:
    """R-post-build F2 — build a profile-shaped staging tree for the
    DEFAULT profile.

    The default profile lives at the install repo root, not under
    ``~/.homie/profiles/<name>/``. ``export_profile("default")`` MUST
    NOT recursively copy the install repo — that would archive the
    private codebase, ``.git``, ``.claude/scripts/.env``, and so on.

    Instead we stage an explicit profile-shaped tree by mapping the
    SAFE keys from ``get_default_paths()`` into the named-profile
    layout under *staged*. Excluded:
        - ``env_file`` (.claude/scripts/.env — secret)
        - ``credentials`` (.claude/scripts/integrations — secret)
        - ``workspace`` (the install repo root — way too broad)
        - ``data`` (memory.db, chat.db — heavy, regenerable, may
          contain user content; intentionally not exported by default)

    Mapped keys (all directories; copied verbatim into named-profile
    layout):
        - ``memory``   -> staged/memory
        - ``state``    -> staged/state
        - ``logs``     -> staged/logs
        - ``run``      -> staged/run
        - ``archon``   -> staged/archon
        - ``home``     -> staged/home
        - ``cron``     -> staged/cron
        - ``sessions`` -> staged/sessions
        - ``skills``   -> staged/skills

    The archive consumer (``import_profile``) treats the staged tree as
    a regular profile dir, so re-importing the default export creates a
    named profile with the default identity — exactly what the operator
    expects.
    """
    # Lazy import — avoid cycle.
    from .core import get_default_paths

    default_paths = get_default_paths()
    # Keys we map directly — these are dirs that align cleanly with the
    # named-profile layout.
    safe_keys = (
        "memory",
        "state",
        "logs",
        "run",
        "archon",
        "home",
        "cron",
        "sessions",
        "skills",
    )
    staged.mkdir(parents=True, exist_ok=True)
    for key in safe_keys:
        src = default_paths.get(key)
        if src is None or not src.is_dir():
            # Source missing — skip (the staged tree just won't contain
            # this subdir; downstream import will treat it as empty).
            continue
        dst = staged / key
        # Copy with the same ignore filter as named-profile export so a
        # nested .env / credentials / token cannot slip through (defense
        # in depth on top of the safe-key allowlist).
        shutil.copytree(
            src,
            dst,
            symlinks=False,
            ignore=_default_export_ignore,
        )


def export_profile(
    name: str,
    output_path: Optional[str] = None,
) -> Path:
    """Export profile *name* as a ``.tar.gz`` archive.

    Args:
        name: Profile name to export. Validated.
        output_path: Optional destination archive path. None-sentinel
            (Anti-pattern Rule 1) — when ``None`` the default path is
            ``~/.homie/exports/<name>-<YYYYMMDD-HHMMSS>.tar.gz``,
            resolved at call time so ``HOMIE_HOME`` env changes between
            calls are honored.

    Returns:
        Path to the written ``.tar.gz`` archive.

    Security contract (R-post-build F2 / F3 — fail CLOSED):
        - The exported archive NEVER contains ``.env`` (at any depth) or
          any ``credentials/`` / ``integrations/`` dir contents.
        - For ``name == "default"``, we DO NOT recursively copy the
          install repo. We stage an explicit profile-shaped tree from
          ``get_default_paths()`` safe keys (memory, state, logs, run,
          archon, home, cron, sessions, skills). ``env_file``,
          ``credentials``, ``workspace`` (repo root), and ``data`` are
          intentionally excluded.
        - The ignore filter denies known secret shapes up front; a
          post-stage scan validates the staged tree contains no
          secret-shaped paths and RAISES (no archive written) if any
          slipped through.
        - There is no opt-in flag for carrying secrets in an exported
          archive — the only way to share a profile with real secrets is
          out-of-band.
    """
    # Lazy import to avoid cycles.
    from .lifecycle import _profile_root

    # Resolve source dir (default profile is special-cased).
    if name == "default":
        source_dir = None  # F2 — staged via _stage_default_export_tree
    else:
        validate_persona_name(name)
        source_dir = _profile_root(name)
        if not source_dir.is_dir():
            raise FileNotFoundError(
                f"Profile '{name}' does not exist at {source_dir}"
            )

    # Anti-pattern Rule 1: resolve default at call time — never bind
    # ``get_default_homie_root()`` as a default arg.
    if output_path is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        exports_dir = get_default_homie_root() / "exports"
        exports_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(exports_dir / f"{name}-{ts}.tar.gz")

    out_path = Path(output_path).expanduser().resolve(strict=False)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Stage a temp copy with the ignore list so the archive contains a
    # clean snapshot. The archive's top-level dir is the profile name.
    with tempfile.TemporaryDirectory(prefix="homie-export-") as tmp:
        staged = Path(tmp) / name
        if name == "default":
            # F2 — explicit profile-shaped staging (NEVER recursively
            # copy the install repo).
            _stage_default_export_tree(staged)
        else:
            shutil.copytree(
                source_dir,
                staged,
                symlinks=False,
                ignore=_default_export_ignore,
            )

        # F3 — post-stage scan: fail CLOSED if any secret-shaped path
        # survived the ignore filter. No more best-effort unlink — if
        # this scan finds anything, we DO NOT write the archive.
        _assert_no_secrets_in_staged_tree(staged)

        # ``shutil.make_archive`` derives final filename from base + format;
        # we want the file to land at ``out_path`` exactly. Build the
        # archive in tmp and then move it into place atomically.
        base_name = str(Path(tmp) / name)
        produced = shutil.make_archive(
            base_name=base_name,
            format="gztar",
            root_dir=str(Path(tmp)),
            base_dir=name,
        )
        produced_path = Path(produced)
        # Atomic move into final location.
        if out_path.exists():
            out_path.unlink()
        shutil.move(str(produced_path), str(out_path))

    return out_path


# =============================================================================
# IMPORT_PROFILE — operator-facing
# =============================================================================


def import_profile(
    archive_path: str,
    *,
    as_name: Optional[str] = None,
    force: bool = False,
) -> Path:
    """Import a profile archive into ``~/.homie/profiles/<name>/``.

    Args:
        archive_path: Path to the ``.tar.gz`` produced by ``export_profile``.
        as_name: Override profile name on import. None-sentinel — when
            ``None`` the name is inferred from the archive's single
            top-level directory.
        force: When True, overwrites an existing profile dir at the
            destination. Default False raises ``FileExistsError``.

    Returns:
        Path to the freshly-imported profile dir.

    Raises:
        FileNotFoundError: Archive does not exist.
        ValueError: Archive contains zero or multiple top-level dirs;
            archive contains a member with an unsafe path; ``as_name``
            is invalid.
        FileExistsError: Destination profile exists and ``force=False``.
    """
    # Lazy import to avoid cycles.
    from .lifecycle import _profile_root

    archive = Path(archive_path).expanduser().resolve(strict=False)
    if not archive.is_file():
        raise FileNotFoundError(f"Archive does not exist: {archive}")

    roots = _inspect_profile_archive_roots(archive)
    if len(roots) == 0:
        raise ValueError(
            f"Archive {archive} is empty; expected exactly one top-level "
            "profile directory."
        )
    if len(roots) > 1:
        raise ValueError(
            f"Archive {archive} must contain exactly one top-level "
            f"directory; found {sorted(roots)}."
        )
    archive_root_name = next(iter(roots))

    target_name = as_name if as_name is not None else archive_root_name
    validate_persona_name(target_name)
    if target_name == "default":
        # Defense in depth — `validate_persona_name` already rejects this.
        raise ValueError(
            "Cannot import to 'default' — it is the built-in profile."
        )

    dst_dir = _profile_root(target_name)
    if dst_dir.exists():
        if not force:
            raise FileExistsError(
                f"Profile '{target_name}' already exists at {dst_dir}; "
                "pass force=True to overwrite."
            )
        # Force overwrite — remove existing dir before extracting.
        shutil.rmtree(dst_dir, ignore_errors=True)

    dst_dir.parent.mkdir(parents=True, exist_ok=True)

    # Extract into a tmpdir first, then atomically rename into place so a
    # mid-extract failure can never leave a partial profile dir at the
    # destination.
    with tempfile.TemporaryDirectory(
        prefix="homie-import-", dir=str(dst_dir.parent)
    ) as tmp:
        tmp_path = Path(tmp)
        _safe_extract_profile_archive(archive, tmp_path)
        extracted_root = tmp_path / archive_root_name
        if not extracted_root.is_dir():
            raise ValueError(
                f"Archive {archive} top-level directory "
                f"{archive_root_name!r} not found after extraction."
            )
        # If as_name differs from archive_root_name, rename inside the
        # tmpdir before moving to the final location.
        if archive_root_name != target_name:
            renamed = tmp_path / target_name
            shutil.move(str(extracted_root), str(renamed))
            extracted_root = renamed
        # Atomic move into final destination. ``shutil.move`` falls back
        # to copy+delete across filesystems but here tmp + dst share a
        # parent so it's a real rename.
        shutil.move(str(extracted_root), str(dst_dir))

    return dst_dir
