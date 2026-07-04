"""Backup / restore / quick snapshots for The Homie.

Port of Hermes v0.18 ``hermes_cli/backup.py`` (MIT, @5445e42b8) adapted to
The Homie's scattered, persona-resolved roots. Where Hermes walks one
``HERMES_HOME`` root, The Homie's irreplaceable state lives in curated
locations resolved through ``config`` at CALL time (Rule 1):

- ``config.MEMORY_DIR``  -> the Obsidian vault           (arc prefix ``vault/``)
- ``config.DATA_DIR``    -> runtime SQLite DBs            (arc prefix ``data/``)
- ``config.STATE_DIR``   -> per-machine state JSONs       (arc prefix ``state/``)
- ``config.ENV_FILE``    -> secrets, opt-in ONLY          (arc prefix ``secrets/``)

The backup NEVER walks ``config.PROJECT_ROOT`` (that would zip ``.git``,
``node_modules``, the entire codebase). Arcnames use stable logical prefixes
with forward slashes (``.as_posix()``) so an archive taken on the default
profile restores correctly on a named profile and vice-versa.

``restore`` is default-denied: it refuses without ``--yes``, refuses while the
bot PID is alive, traversal-guards every entry, and atomically swaps SQLite
DBs. Quick snapshots keep a ring of 20 fast copies of the live runtime DBs +
small state JSONs under ``config.STATE_DIR/state-snapshots/``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exclusion rules (Hermes _EXCLUDED_* verbatim in behavior, Homie-adapted set)
# ---------------------------------------------------------------------------

# Directory names to skip entirely (matched against each path component).
# Regeneratable caches/deps plus the backup/snapshot output dirs themselves
# (so a full backup never recursively swallows prior snapshots or backups).
_EXCLUDED_DIRS = frozenset({
    "__pycache__",
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "site-packages",
    ".cache",
    ".tox",
    ".nox",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "models",           # embedding model cache (config.EMBEDDING_CACHE_DIR)
    "cabinet-voice",    # session-local voice artifacts
    "state-snapshots",  # quick-snapshot ring — never nest snapshots
    "backups",          # prior full backups — never nest backups
})

# File-name suffixes to skip. SQLite sidecars are excluded because the backup
# takes a consistent ``*.db`` snapshot via ``sqlite3.backup()`` — shipping a
# live WAL/-shm/-journal alongside would pair a fresh snapshot with stale
# sidecar state and produce a torn restore on the next open.
_EXCLUDED_SUFFIXES = (
    ".pyc",
    ".pyo",
    ".db-wal",
    ".db-shm",
    ".db-journal",
    ".lock",
    ".log",
    ".bak",
)

# File names to skip on backup (runtime state meaningless on another machine).
_EXCLUDED_NAMES = {"bot.pid"}

# File names restore must never overwrite (machine-local runtime state).
_RESTORE_SKIP_NAMES = {"bot.pid"}

# zipfile.open() drops Unix mode bits on extract; restore tightens these 0600.
_SECRET_FILE_NAMES = {".env"}

# Logical arcname prefixes — decoupled from physical location so archives are
# portable across profiles (named profiles live outside PROJECT_ROOT).
_PREFIX_VAULT = "vault/"
_PREFIX_DATA = "data/"
_PREFIX_STATE = "state/"
_PREFIX_SECRETS = "secrets/"
_KNOWN_PREFIXES = (_PREFIX_VAULT, _PREFIX_DATA, _PREFIX_STATE, _PREFIX_SECRETS)

# Quick snapshots
_SNAPSHOTS_DIRNAME = "state-snapshots"
_SNAPSHOT_DEFAULT_KEEP = 20

# Small state JSONs included in quick snapshots (alongside the live runtime
# DBs). memory.db is deliberately OUT of the snapshot set — it is regenerable
# from the vault via memory_index.py and 35MB x keep=20 is wasteful.
_STATE_SNAPSHOT_JSON_NAMES = (
    "heartbeat-state.json",
    "dream-state.json",
    "reflection-state.json",
    "weekly-state.json",
    "flush-state.json",
)


# ---------------------------------------------------------------------------
# Exclusion / skip predicates (mirror hermes backup.py:211-249)
# ---------------------------------------------------------------------------

def _should_exclude(rel_path: Path) -> bool:
    """Return True if *rel_path* (relative to its backup root) should be skipped."""
    for part in rel_path.parts:
        if part in _EXCLUDED_DIRS:
            return True

    name = rel_path.name
    if name in _EXCLUDED_NAMES:
        return True
    if name.endswith(_EXCLUDED_SUFFIXES):
        return True
    return False


def _should_skip_backup_file(abs_path: Path, rel_path: Path, out_path: Path) -> bool:
    """Return True when a candidate file should not be written to a backup zip."""
    if _should_exclude(rel_path):
        return True

    # zipfile.write() follows file symlinks, so skip links before any archive
    # write can copy data from outside the walked root.
    if abs_path.is_symlink():
        return True

    # Self-reference skip: never archive the in-progress zip itself.
    try:
        return abs_path.resolve() == out_path.resolve()
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# SQLite safe copy (hermes :256-276 + cross-platform URI + bounded lock retry)
# ---------------------------------------------------------------------------

def _safe_copy_db(src: Path, dst: Path) -> bool:
    """Copy a SQLite database safely using the backup() API.

    Handles WAL mode — produces a consistent snapshot even while the DB is
    being written to. The source is opened READ-ONLY through a cross-platform
    file URI: ``src.resolve().as_uri() + "?mode=ro"``. Hermes's
    ``f"file:{src}?mode=ro"`` is POSIX-only — on Windows the backslashes +
    drive colon make a malformed URI that sqlite rejects, silently forcing the
    torn ``shutil.copy2`` fallback. Falls back to raw copy only after the
    backup() path (with a bounded locked-DB retry) fails.
    """
    src_uri = src.resolve().as_uri() + "?mode=ro"
    for attempt in range(5):
        try:
            conn = sqlite3.connect(src_uri, uri=True)
            try:
                backup_conn = sqlite3.connect(str(dst))
                try:
                    conn.backup(backup_conn)
                finally:
                    backup_conn.close()
            finally:
                conn.close()
            return True
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() and attempt < 4:
                time.sleep(0.1)
                continue
            logger.warning("SQLite safe copy failed for %s: %s", src, exc)
            break
        except Exception as exc:
            logger.warning("SQLite safe copy failed for %s: %s", src, exc)
            break
    try:
        shutil.copy2(src, dst)
        return True
    except Exception as exc2:
        logger.error("Raw copy also failed for %s: %s", src, exc2)
        return False


# ---------------------------------------------------------------------------
# Backup roots + file collection (Homie adaptation of the single-root walk)
# ---------------------------------------------------------------------------

def _backup_roots(include_secrets: bool = False) -> list[tuple[Path, str]]:
    """The curated (root, logical_prefix) pairs, resolved at CALL time.

    NEVER includes ``config.PROJECT_ROOT`` — the irreplaceable state is the
    vault + DB dir + state dir (+ optionally the profile ``.env`` FILE).
    """
    import config

    roots: list[tuple[Path, str]] = [
        (Path(config.MEMORY_DIR), _PREFIX_VAULT),
        (Path(config.DATA_DIR), _PREFIX_DATA),
        (Path(config.STATE_DIR), _PREFIX_STATE),
    ]
    if include_secrets:
        env_file = getattr(config, "ENV_FILE", None)
        if env_file and Path(env_file).exists():
            roots.append((Path(env_file), _PREFIX_SECRETS))
    return [(r, p) for r, p in roots if r and r.exists()]


def _iter_backup_files(
    root: Path,
    prefix: str,
    out_path: Path,
    other_roots: set[Path] | None = None,
) -> list[tuple[Path, str]]:
    """Yield (abs_path, arcname) pairs for one backup root.

    *root* may be a single FILE (the secrets ``.env``) or a directory.
    *other_roots* holds the RESOLVED paths of the other backup roots so a
    nested root (the default profile's STATE_DIR lives inside DATA_DIR) is
    owned by exactly one logical prefix and never archived twice.
    """
    files: list[tuple[Path, str]] = []
    if root.is_file():
        rel = Path(root.name)
        if not _should_skip_backup_file(root, rel, out_path):
            files.append((root, prefix + rel.as_posix()))
        return files
    if not root.is_dir():
        return files

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dp = Path(dirpath)
        kept: list[str] = []
        for d in dirnames:
            if d in _EXCLUDED_DIRS:
                continue
            if other_roots:
                try:
                    if (dp / d).resolve() in other_roots:
                        continue
                except OSError:
                    pass
            kept.append(d)
        dirnames[:] = kept

        for fname in filenames:
            fpath = dp / fname
            try:
                rel = fpath.relative_to(root)
            except ValueError:
                continue
            if _should_skip_backup_file(fpath, rel, out_path):
                continue
            files.append((fpath, prefix + rel.as_posix()))
    return files


def _format_size(nbytes: float) -> str:
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}" if unit != "B" else f"{int(nbytes)} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


# ---------------------------------------------------------------------------
# Full backup
# ---------------------------------------------------------------------------

def create_backup(
    out_path: Path | str | None = None,
    include_secrets: bool = False,
    json_out: bool = False,
) -> Path | None:
    """Create a curated zip backup of the current profile's state.

    Returns the written archive path, or None when there was nothing to back
    up. Secrets (``config.ENV_FILE``) are EXCLUDED unless *include_secrets*.
    """
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    if out_path:
        out = Path(out_path).expanduser()
        if out.is_dir():
            out = out / f"thehomie-backup-{stamp}.zip"
    else:
        out = Path.home() / f"thehomie-backup-{stamp}.zip"
    if out.suffix.lower() != ".zip":
        out = out.with_suffix(out.suffix + ".zip")
    try:
        out = out.resolve()
    except OSError:
        pass
    out.parent.mkdir(parents=True, exist_ok=True)

    roots = _backup_roots(include_secrets)
    if not roots:
        if not json_out:
            print("No backup roots found — nothing to back up.")
        else:
            print(json.dumps({"path": None, "files": 0, "error": "no backup roots found"}))
        return None

    resolved_roots: set[Path] = set()
    for r, _p in roots:
        try:
            resolved_roots.add(r.resolve())
        except OSError:
            pass

    files_to_add: list[tuple[Path, str]] = []
    for r, prefix in roots:
        try:
            r_res = r.resolve()
        except OSError:
            r_res = r
        files_to_add.extend(
            _iter_backup_files(r, prefix, out, other_roots=resolved_roots - {r_res})
        )

    if not files_to_add:
        if not json_out:
            print("No files to back up.")
        else:
            print(json.dumps({"path": None, "files": 0, "error": "no files to back up"}))
        return None

    file_count = len(files_to_add)
    if not json_out:
        print(f"Backing up {file_count} files ...")

    total_bytes = 0
    errors: list[str] = []
    t0 = time.monotonic()

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for i, (abs_path, arcname) in enumerate(files_to_add, 1):
            try:
                if abs_path.suffix == ".db":
                    # Stage the WAL-safe snapshot alongside the output zip so
                    # the temp file lives on the same filesystem (the system
                    # /tmp may be a small tmpfs that silently truncates a
                    # large DB).
                    with tempfile.NamedTemporaryFile(
                        suffix=".db", delete=False, dir=str(out.parent)
                    ) as tmp:
                        tmp_db = Path(tmp.name)
                    try:
                        if _safe_copy_db(abs_path, tmp_db):
                            zf.write(tmp_db, arcname=arcname)
                            total_bytes += tmp_db.stat().st_size
                        else:
                            errors.append(f"  {arcname}: SQLite safe copy failed")
                            continue
                    finally:
                        tmp_db.unlink(missing_ok=True)
                else:
                    zf.write(abs_path, arcname=arcname)
                    total_bytes += abs_path.stat().st_size
            except (PermissionError, OSError, ValueError) as exc:
                errors.append(f"  {arcname}: {exc}")
                continue

            if not json_out and i % 500 == 0:
                print(f"  {i}/{file_count} files ...")

    elapsed = time.monotonic() - t0
    zip_size = out.stat().st_size

    if json_out:
        payload: dict[str, Any] = {
            "path": str(out),
            "files": file_count,
            "original_bytes": total_bytes,
            "compressed_bytes": zip_size,
            "seconds": round(elapsed, 1),
            "include_secrets": include_secrets,
            "errors": errors,
        }
        if include_secrets:
            payload["warning"] = "archive contains SECRETS (.env) — store it securely"
        print(json.dumps(payload))
        return out

    print()
    print(f"Backup complete: {out}")
    print(f"  Files:       {file_count}")
    print(f"  Original:    {_format_size(total_bytes)}")
    print(f"  Compressed:  {_format_size(zip_size)}")
    print(f"  Time:        {elapsed:.1f}s")

    if include_secrets:
        print()
        print("WARNING: this archive contains SECRETS (.env) — store it securely.")

    if errors:
        print(f"\n  Warnings ({len(errors)} files skipped):")
        for e in errors[:10]:
            print(e)
        if len(errors) > 10:
            print(f"  ... and {len(errors) - 10} more")

    print(f"\nRestore with: thehomie restore {out.name} --dry-run")
    return out


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def _prefix_roots() -> dict[str, Path]:
    """Map logical arc prefix -> the CURRENT profile's config root (call time)."""
    import config

    return {
        _PREFIX_VAULT: Path(config.MEMORY_DIR),
        _PREFIX_DATA: Path(config.DATA_DIR),
        _PREFIX_STATE: Path(config.STATE_DIR),
        _PREFIX_SECRETS: Path(config.ENV_FILE).parent,
    }


def _classify_member(arcname: str) -> tuple[str, Path | None]:
    """Classify one archive member -> (action, target).

    Actions with a target: ``db-swap`` / ``overwrite`` / ``add``.
    Actions without: ``blocked`` (traversal), ``skip-runtime`` (machine-local
    names), ``skip-unknown`` (unrecognized prefix).

    The traversal guard is layered (archive contents are attacker-controlled
    once an archive leaves the machine): explicit isabs / ``..`` pre-checks,
    then the final ``resolve().relative_to(root)`` gate — on Windows,
    ``Path("C:/repo") / "C:/evil"`` resolves to the absolute RHS, so the
    final gate is load-bearing, not belt-and-suspenders.
    """
    for prefix, root in _prefix_roots().items():
        if not arcname.startswith(prefix):
            continue
        rel = arcname[len(prefix):]
        if not rel:
            return ("blocked", None)
        if os.path.isabs(rel) or ".." in Path(rel).parts:
            return ("blocked", None)
        target = root / rel
        try:
            target.resolve().relative_to(root.resolve())
        except (ValueError, OSError):
            return ("blocked", None)
        name = Path(rel).name
        if name in _RESTORE_SKIP_NAMES or name.endswith((".lock", ".pid")):
            return ("skip-runtime", None)
        if target.suffix == ".db":
            return ("db-swap", target)
        if target.exists():
            return ("overwrite", target)
        return ("add", target)
    return ("skip-unknown", None)


def _plan_restore(zf: zipfile.ZipFile) -> list[tuple[str, str, Path | None]]:
    """Build the per-entry restore plan: (action, arcname, target)."""
    plan: list[tuple[str, str, Path | None]] = []
    for member in zf.namelist():
        if member.endswith("/"):
            continue
        action, target = _classify_member(member)
        plan.append((action, member, target))
    return plan


def _validate_backup_zip(zf: zipfile.ZipFile) -> tuple[bool, str]:
    """Check that a zip looks like a The Homie backup. Returns (ok, reason)."""
    names = [n for n in zf.namelist() if not n.endswith("/")]
    if not names:
        return False, "zip archive is empty"
    if not any(n.startswith(_KNOWN_PREFIXES) for n in names):
        return False, (
            "zip does not appear to be a thehomie backup "
            "(no vault/, data/, state/, or secrets/ entries)"
        )
    return True, ""


def _pid_alive(pid: int) -> bool:
    """Best-effort PID liveness — psutil primary, os.kill(pid, 0) fallback."""
    try:
        import psutil

        return bool(psutil.pid_exists(pid))
    except Exception:
        pass
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _is_bot_running() -> bool:
    """True when ``config.BOT_PID_FILE`` exists and holds a LIVE pid.

    Any parse/IO error returns False (absence of proof of life = allow; a
    readable live pid file = refuse).
    """
    import config

    try:
        pid_file = getattr(config, "BOT_PID_FILE", None)
        if not pid_file:
            return False
        pid_file = Path(pid_file)
        if not pid_file.exists():
            return False
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    return _pid_alive(pid)


def _refuse(message: str, json_out: bool) -> bool:
    if json_out:
        print(json.dumps({"success": False, "error": message}))
    else:
        print(message)
    return False


def restore_backup(
    archive: Path | str,
    *,
    dry_run: bool = False,
    yes: bool = False,
    force: bool = False,
    json_out: bool = False,
) -> bool:
    """Restore a backup archive onto the CURRENT profile's roots.

    Default-denied: without ``--dry-run`` it refuses unless *yes*; it refuses
    while the bot PID is alive (never bypassed — *force* only skips the
    "target already has state" confirmation). ``--dry-run`` prints the plan
    and mutates NOTHING.
    """
    archive = Path(archive).expanduser()
    if not archive.is_file():
        return _refuse(f"Error: file not found: {archive}", json_out)
    if not zipfile.is_zipfile(archive):
        return _refuse(f"Error: not a valid zip file: {archive}", json_out)

    if not dry_run:
        if _is_bot_running():
            return _refuse(
                "Refusing: the bot is running. Stop it first "
                "(kill the PID in bot.pid / bash .claude/chat/run_chat.sh stop), then retry.",
                json_out,
            )
        if not yes:
            return _refuse(
                "Refusing: restore is destructive. Re-run with --dry-run to preview, "
                "or --yes to confirm.",
                json_out,
            )

    with zipfile.ZipFile(archive, "r") as zf:
        ok, reason = _validate_backup_zip(zf)
        if not ok:
            return _refuse(f"Error: {reason}", json_out)

        plan = _plan_restore(zf)
        actionable = [(a, m, t) for a, m, t in plan if t is not None]
        blocked = [m for a, m, t in plan if a == "blocked"]

        if dry_run:
            if json_out:
                print(json.dumps({
                    "dry_run": True,
                    "plan": [
                        {"action": a, "member": m, "target": str(t) if t else None}
                        for a, m, t in plan
                    ],
                }))
                return True
            print(f"Restore plan for {archive.name}:")
            for action, member, _target in plan:
                print(f"  {action:12} {member}")
            print(
                f"\n{len(actionable)} entries would be restored, "
                f"{len(plan) - len(actionable)} skipped/blocked."
            )
            print("Dry run — nothing was written.")
            return True

        if not actionable:
            return _refuse("No restorable entries in the archive.", json_out)

        # Hermes-faithful "target already has state" confirmation (:557-569):
        # --force skips it; a non-interactive stdin (EOF/OSError) aborts.
        overwrites = [t for a, _m, t in actionable if a == "overwrite" or (t and t.exists())]
        if overwrites and not force:
            if json_out:
                return _refuse(
                    f"Refusing: {len(overwrites)} existing file(s) would be overwritten. "
                    "Re-run with --force to confirm overwrite.",
                    json_out,
                )
            print(f"Warning: {len(overwrites)} existing file(s) will be overwritten.")
            try:
                answer = input("Continue? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt, OSError):
                print("\nAborted (non-interactive — re-run with --force).")
                return False
            if answer not in {"y", "yes"}:
                print("Aborted.")
                return False

        restored = 0
        errors: list[str] = []
        t0 = time.monotonic()

        for action, member, target in actionable:
            assert target is not None
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if action == "db-swap":
                    # Atomic swap: extract to a temp file IN the target dir
                    # (same filesystem), then os.replace over the live path.
                    with tempfile.NamedTemporaryFile(
                        suffix=".db", delete=False, dir=str(target.parent)
                    ) as tmp:
                        tmp_db = Path(tmp.name)
                    try:
                        with zf.open(member) as src, open(tmp_db, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        os.replace(tmp_db, target)
                    finally:
                        tmp_db.unlink(missing_ok=True)
                else:
                    with zf.open(member) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                if target.name in _SECRET_FILE_NAMES:
                    try:
                        os.chmod(target, 0o600)
                    except OSError:
                        pass
                restored += 1
            except (PermissionError, OSError, ValueError) as exc:
                errors.append(f"  {member}: {exc}")

        elapsed = time.monotonic() - t0

        if json_out:
            print(json.dumps({
                "success": restored > 0,
                "restored": restored,
                "blocked": len(blocked),
                "errors": errors,
                "seconds": round(elapsed, 1),
            }))
            return restored > 0

        print(f"\nRestore complete: {restored} files restored in {elapsed:.1f}s")
        if blocked:
            print(f"  Blocked {len(blocked)} traversal-unsafe entr(ies):")
            for m in blocked[:10]:
                print(f"    {m}")
        if errors:
            print(f"\n  Warnings ({len(errors)} files skipped):")
            for e in errors[:10]:
                print(e)
        return restored > 0


# ---------------------------------------------------------------------------
# Quick state snapshots (hermes :793-990, :1101-1120 — Homie targets)
# ---------------------------------------------------------------------------

def _snapshot_root() -> Path:
    import config

    return Path(config.STATE_DIR) / _SNAPSHOTS_DIRNAME


def _snapshot_targets() -> list[tuple[Path, str]]:
    """The quick-snapshot set, resolved at CALL time: the SMALL non-regenerable
    live runtime DBs + small state JSONs. memory.db is deliberately excluded
    (regenerable from the vault, 35MB x keep=20)."""
    import config

    candidates: list[tuple[Path, str]] = [
        (Path(config.CHAT_DB_PATH), _PREFIX_DATA + "chat.db"),
        (Path(config.ORCHESTRATION_DB_PATH), _PREFIX_DATA + "orchestration.db"),
        (Path(config.DASHBOARD_DB_PATH), _PREFIX_DATA + "dashboard.db"),
    ]
    state_dir = Path(config.STATE_DIR)
    for name in _STATE_SNAPSHOT_JSON_NAMES:
        candidates.append((state_dir / name, _PREFIX_STATE + name))
    return [(p, arc) for p, arc in candidates if p.is_file()]


def create_quick_snapshot(
    label: str | None = None,
    keep: int | None = None,
) -> str | None:
    """Create a quick state snapshot. Returns the snapshot id, or None when
    no target files exist. Auto-prunes beyond *keep* (default 20)."""
    root = _snapshot_root()

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    safe_label = None
    if label:
        safe_label = re.sub(r"[^A-Za-z0-9._-]+", "-", str(label)).strip("-.")
    snap_id = f"{ts}-{safe_label}" if safe_label else ts
    snap_dir = root / snap_id
    snap_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, int] = {}
    for src, arc in _snapshot_targets():
        dst = snap_dir / arc
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            if src.suffix == ".db":
                if not _safe_copy_db(src, dst):
                    continue
            else:
                shutil.copy2(src, dst)
            manifest[arc] = dst.stat().st_size
        except (OSError, PermissionError) as exc:
            logger.warning("Could not snapshot %s: %s", arc, exc)

    if not manifest:
        shutil.rmtree(snap_dir, ignore_errors=True)
        return None

    meta = {
        "id": snap_id,
        "timestamp": ts,
        "label": label,
        "file_count": len(manifest),
        "total_size": sum(manifest.values()),
        "files": manifest,
    }
    with open(snap_dir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    _prune_quick_snapshots(keep=_SNAPSHOT_DEFAULT_KEEP if keep is None else keep)

    logger.info("State snapshot created: %s (%d files)", snap_id, len(manifest))
    return snap_id


def list_quick_snapshots(limit: int = 20) -> list[dict[str, Any]]:
    """List existing quick state snapshots, most recent first."""
    root = _snapshot_root()
    if not root.exists():
        return []

    results: list[dict[str, Any]] = []
    for d in sorted(root.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        manifest_path = d / "manifest.json"
        if manifest_path.exists():
            try:
                with open(manifest_path, encoding="utf-8") as f:
                    results.append(json.load(f))
            except (json.JSONDecodeError, OSError):
                results.append({"id": d.name, "file_count": 0, "total_size": 0})
        if len(results) >= limit:
            break

    return results


def restore_quick_snapshot(snapshot_id: str) -> bool:
    """Restore state from a quick snapshot (atomic DB swap).

    Returns True if at least one file was restored. The id is validated
    (no separators / traversal) BEFORE any disk access; every manifest entry
    is traversal-guarded on both the source and target side.
    """
    # Security: reject snapshot_id values that contain path separators or
    # traversal sequences so that `root / snapshot_id` stays inside root.
    if (
        not snapshot_id
        or "/" in snapshot_id
        or "\\" in snapshot_id
        or snapshot_id in (".", "..")
    ):
        logger.error("Invalid snapshot_id: %s", snapshot_id)
        return False

    root = _snapshot_root()
    snap_dir = root / snapshot_id

    # Confirm the resolved path is still inside root (handles symlinks etc.)
    try:
        snap_dir.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        logger.error("Snapshot path traversal blocked for id: %s", snapshot_id)
        return False

    if not snap_dir.is_dir():
        return False

    manifest_path = snap_dir / "manifest.json"
    if not manifest_path.exists():
        return False

    try:
        with open(manifest_path, encoding="utf-8") as f:
            meta = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False

    restored = 0
    for rel in meta.get("files", {}):
        # Source-side guard: the snapshot copy must live inside snap_dir.
        src = snap_dir / rel
        try:
            src.resolve().relative_to(snap_dir.resolve())
        except (ValueError, OSError):
            logger.error("Manifest path traversal blocked: %s", rel)
            continue

        # Target-side guard: same prefix->root mapping + traversal gate as
        # the full restore.
        action, target = _classify_member(rel)
        if target is None:
            logger.error("Manifest entry not restorable (%s): %s", action, rel)
            continue

        if not src.exists():
            continue

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.suffix == ".db":
                with tempfile.NamedTemporaryFile(
                    suffix=".db", delete=False, dir=str(target.parent)
                ) as tmp:
                    tmp_db = Path(tmp.name)
                try:
                    shutil.copy2(src, tmp_db)
                    os.replace(tmp_db, target)
                finally:
                    tmp_db.unlink(missing_ok=True)
            else:
                shutil.copy2(src, target)
            restored += 1
        except (OSError, PermissionError) as exc:
            logger.error("Failed to restore %s: %s", rel, exc)

    logger.info("Restored %d files from snapshot %s", restored, snapshot_id)
    return restored > 0


def _prune_quick_snapshots(keep: int | None = None, root: Path | None = None) -> int:
    """Remove oldest quick snapshots beyond the keep limit. Returns count deleted."""
    if root is None:
        root = _snapshot_root()
    if keep is None:
        keep = _SNAPSHOT_DEFAULT_KEEP
    if not root.exists():
        return 0

    dirs = sorted(
        (d for d in root.iterdir() if d.is_dir()),
        key=lambda d: d.name,
        reverse=True,
    )

    deleted = 0
    for d in dirs[keep:]:
        try:
            shutil.rmtree(d)
            deleted += 1
        except OSError as exc:
            logger.warning("Failed to prune snapshot %s: %s", d.name, exc)

    return deleted
