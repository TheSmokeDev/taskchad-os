"""migrate-default — dry-run + journal infrastructure (Phase 2 stub).

Phase 2 / PRP-7b Workstream 3. Owns the ``migrate_default_dry_run`` /
``migrate_default_apply`` operator-facing helpers that the
``thehomie profile migrate-default`` Click handler calls into.

Phase 2 ships:
    - ``migrate_default_dry_run`` — fully functional. Builds the
      ``MigrationOp`` list (move identity files, data dirs, state, .env).
      Reads physical state via ``get_default_paths()`` (Rule 2). Idempotent
      across repeated calls.
    - ``migrate_default_apply`` — STUB. Writes the migration journal at
      ``~/.homie/migration-journal.json`` (atomic via tmp + ``os.replace``),
      prints the documented stub message verbatim, returns ``None``,
      NEVER raises, NEVER actually moves files. Full file-move logic is
      Phase 8 follow-up.
    - ``_write_migration_journal`` — atomic journal writer.
    - ``_rollback_from_journal`` — Phase 2 stub (logs + returns None).

Anti-pattern compliance:
    - Rule 1 (None sentinel): ``journal_path=None`` resolved at call time
      against ``get_default_homie_root()``.
    - Rule 2 (physical state): dry-run reads filesystem state directly
      via ``Path.exists()`` / ``Path.iterdir()``. No sidecar registry.
    - Rule 3 (langfuse module-attribute import): N/A — no Langfuse.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .core import get_default_homie_root, get_default_paths


# Documented stub message — tested verbatim. Tests assert this exact string
# is printed by ``migrate_default_apply``. Do NOT change the wording without
# updating ``tests/test_persona_migrate_default.py``.
_APPLY_STUB_MESSAGE = (
    "migrate-default --apply is a Phase 8 follow-up; this is a stub. "
    "Journal infrastructure is in place. Use --dry-run to inspect what "
    "would move."
)

# Journal schema version — bump when the on-disk shape changes
# incompatibly so loaders can fail-fast on stale journals.
_JOURNAL_VERSION = 1


# =============================================================================
# DATACLASS — MigrationOp
# =============================================================================


@dataclass
class MigrationOp:
    """One file/dir move scheduled by ``migrate-default``.

    Phase 2 only writes ``op_type="move"`` (the dry-run + journal payload
    is the pure inventory of what Phase 8 will execute). The dataclass
    surface is forward-compatible with ``op_type="copy"`` for any
    follow-up that wants to keep the source in place.

    Fields:
        op_type: ``"move"`` (Phase 2 default) or ``"copy"`` (reserved).
        source: Absolute path to the source on disk.
        destination: Absolute path the operation would land at.
        completed: Phase 8 flag — Phase 2 writes ``False`` for every op
            since nothing actually happens. Kept on the dataclass so the
            journal shape matches Phase 8.
    """

    op_type: str
    source: Path
    destination: Path
    completed: bool = False


# =============================================================================
# DRY RUN
# =============================================================================


def _profile_default_target_root() -> Path:
    """Return the root the migrate-default flow will move INTO.

    Default-profile migration moves the install-dir layout into
    ``~/.homie/profiles/default/``. This helper resolves that on every
    call so the env-driven ``HOMIE_HOME`` is honored (Rule 1).
    """
    return (
        get_default_homie_root() / "profiles" / "default"
    ).resolve(strict=False)


def _identity_file_ops(
    install_memory: Path, target_memory: Path
) -> list[MigrationOp]:
    """Build move ops for top-level identity ``.md`` files.

    Iterates the install-dir memory root and emits one ``MigrationOp``
    per ``*.md`` file at the top level. Subdirectory files (concepts/,
    daily/, etc.) are covered by directory-level ops in the parent
    builder so we don't double-write.

    Reads physical state — ``Path.iterdir()`` (Rule 2).
    """
    ops: list[MigrationOp] = []
    if not install_memory.is_dir():
        return ops
    try:
        entries = sorted(install_memory.iterdir())
    except OSError:
        return ops
    for entry in entries:
        if entry.is_file() and entry.suffix == ".md":
            ops.append(
                MigrationOp(
                    op_type="move",
                    source=entry.resolve(strict=False),
                    destination=(target_memory / entry.name).resolve(
                        strict=False
                    ),
                )
            )
    return ops


def _directory_ops(
    install_paths: dict[str, Path], target_root: Path
) -> list[MigrationOp]:
    """Build directory-level move ops for the canonical profile dirs.

    Emits one ``MigrationOp`` per (key, source) where the source dir
    exists on disk. The destination follows the standard per-profile
    layout (``<target_root>/<key>/`` for top-level keys; ``memory`` is
    handled separately to preserve the per-file granularity for top-level
    identity files).
    """
    ops: list[MigrationOp] = []
    # Top-level dirs that map 1:1 onto the per-profile layout.
    keys = (
        "data",
        "state",
        "logs",
        "run",
        "archon",
        "home",
        "cron",
        "sessions",
        "skills",
    )
    for key in keys:
        src = install_paths.get(key)
        if src is None:
            continue
        if not src.exists():
            continue
        ops.append(
            MigrationOp(
                op_type="move",
                source=src.resolve(strict=False),
                destination=(target_root / key).resolve(strict=False),
            )
        )
    # ``credentials`` lives at ``<scripts>/integrations`` in the install
    # layout but should land at ``<profile>/credentials/`` per the
    # canonical per-profile shape.
    credentials_src = install_paths.get("credentials")
    if credentials_src is not None and credentials_src.exists():
        ops.append(
            MigrationOp(
                op_type="move",
                source=credentials_src.resolve(strict=False),
                destination=(target_root / "credentials").resolve(
                    strict=False
                ),
            )
        )
    # ``memory`` directory itself — destination is the per-profile memory
    # dir. Subdirectory contents are NOT enumerated here (Phase 8 owns
    # the recursive walk if it's needed); this op surfaces the intent.
    memory_src = install_paths.get("memory")
    if memory_src is not None and memory_src.is_dir():
        ops.append(
            MigrationOp(
                op_type="move",
                source=memory_src.resolve(strict=False),
                destination=(target_root / "memory").resolve(strict=False),
            )
        )
    return ops


def _env_file_op(install_paths: dict[str, Path]) -> Optional[MigrationOp]:
    """Build the ``.env`` move op if the install-dir ``.env`` exists."""
    env_src = install_paths.get("env_file")
    if env_src is None:
        return None
    if not env_src.exists():
        return None
    target_root = _profile_default_target_root()
    return MigrationOp(
        op_type="move",
        source=env_src.resolve(strict=False),
        destination=(target_root / ".env").resolve(strict=False),
    )


def migrate_default_dry_run() -> list[MigrationOp]:
    """Return the list of ``MigrationOp`` operations the migrate-default
    flow would execute on ``--apply``.

    Pure inspection — does NOT write anything to disk.
    Idempotent — calling twice returns equal op lists.

    Reads physical state from ``get_default_paths()`` (Rule 2 — no
    sidecar manifest). Sources that do not exist on disk are simply
    omitted from the op list, so a fresh install (no install-dir vault)
    returns ``[]``.
    """
    install_paths = get_default_paths()
    target_root = _profile_default_target_root()
    target_memory = target_root / "memory"

    ops: list[MigrationOp] = []
    # Identity files first (top-level *.md inside install-dir memory).
    ops.extend(
        _identity_file_ops(install_paths.get("memory", Path()), target_memory)
    )
    # Then directories.
    ops.extend(_directory_ops(install_paths, target_root))
    # Then .env.
    env_op = _env_file_op(install_paths)
    if env_op is not None:
        ops.append(env_op)
    return ops


# =============================================================================
# JOURNAL WRITER (atomic — Rule 2 physical-state guarantee)
# =============================================================================


def _serialize_ops(ops: list[MigrationOp]) -> list[dict[str, object]]:
    """Convert a list of ``MigrationOp`` to JSON-friendly dicts.

    ``MigrationOp.source`` / ``destination`` are ``Path`` instances —
    JSON serialization requires strings. ``asdict`` would surface the
    Paths as ``PosixPath('/...')`` repr inside ``json.dumps``, so we
    walk the dict ourselves.
    """
    out: list[dict[str, object]] = []
    for op in ops:
        d: dict[str, object] = asdict(op)
        d["source"] = str(op.source)
        d["destination"] = str(op.destination)
        out.append(d)
    return out


def _default_journal_path() -> Path:
    """Return the default journal path.

    ``~/.homie/migration-journal.json`` — resolved via
    ``get_default_homie_root()`` so the env-driven root is honored.
    """
    return (
        get_default_homie_root() / "migration-journal.json"
    ).resolve(strict=False)


def _write_migration_journal(
    ops: list[MigrationOp],
    *,
    journal_path: Optional[Path] = None,
) -> Path:
    """Write the migration journal atomically (tmp + ``os.replace``).

    Args:
        ops: List of ops to record.
        journal_path: None-sentinel — defaults to
            ``~/.homie/migration-journal.json`` resolved at call time.

    Returns:
        Path to the written journal file.

    Atomic-write contract: the file is written into a sibling tempfile
    in the same directory, then ``os.replace``'d into the target name.
    A reader observing the file at any moment sees either the old
    content (if any) or the new content — never partial bytes.
    """
    if journal_path is None:
        journal_path = _default_journal_path()
    journal_path = Path(journal_path).expanduser().resolve(strict=False)
    journal_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "version": _JOURNAL_VERSION,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "operations": _serialize_ops(ops),
    }
    body = json.dumps(payload, indent=2, sort_keys=False)

    # Atomic write: NamedTemporaryFile in the same dir + os.replace.
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(journal_path.parent),
        delete=False,
        prefix=".migration-journal.",
        suffix=".tmp",
    ) as tmp:
        tmp.write(body)
        tmp_path = tmp.name
    os.replace(tmp_path, journal_path)
    return journal_path


def _rollback_from_journal(journal_path: Path) -> None:
    """Phase 2 stub — Phase 8 will read the journal and reverse completed
    operations.

    Returns ``None`` unconditionally. Reads the journal if present (so
    a smoke test can verify the path resolves) but performs no
    filesystem changes.
    """
    journal_path = Path(journal_path).expanduser().resolve(strict=False)
    if not journal_path.exists():
        # Nothing to rollback. No-op.
        return None
    try:
        # Touch the file path to confirm readability — but make no
        # decisions based on the contents (Phase 8 owns the rollback
        # algorithm).
        journal_path.read_text(encoding="utf-8")
    except OSError:
        # If we can't even read the journal, the rollback can't proceed.
        # Phase 2 stub: silently return.
        return None
    return None


# =============================================================================
# APPLY (Phase 2 STUB)
# =============================================================================


def migrate_default_apply() -> None:
    """Write the migration journal and print the documented stub message.

    Phase 2 STUB CONTRACT (R1 minor — tested verbatim):
        - Returns ``None``.
        - Prints exactly: ``"migrate-default --apply is a Phase 8
          follow-up; this is a stub. Journal infrastructure is in
          place. Use --dry-run to inspect what would move."``
        - Writes the journal at ``~/.homie/migration-journal.json``
          atomically (tmp + ``os.replace``).
        - Idempotent: re-running keeps journal contents stable
          (modulo ``started_at`` which changes per run — tests assert
          shape, not byte-equality).
        - Does NOT raise.
        - Does NOT move files. (Phase 2 ships journal infrastructure
          only; Phase 8 owns the file-move logic.)
    """
    ops = migrate_default_dry_run()
    try:
        _write_migration_journal(ops)
    except OSError:
        # Phase 2 contract: NEVER raises. If the journal can't be
        # written (disk full, permission denied, etc.) we still print
        # the documented stub message so the user knows the apply
        # didn't actually do anything.
        pass
    print(_APPLY_STUB_MESSAGE, file=sys.stdout)
    return None
