"""Acceptance gate: no production file binds install-dir paths or .env (R1 M2).

PRP-7a Workstream 4b — enforced acceptance gate (was deferred, now in
scope per parent PRD §40-44).

This file is the SOLE production-non-test file allowed to import
``personas._audit._assert_no_install_dir_paths``. The whitelist in
``test_personas_public_api.py`` carves out exactly one entry pointing at
this file (``.claude/scripts/tests/test_no_install_dir_paths.py``); any
addition to that whitelist requires explicit R-pass review per
PRP-7a §"API Surface > R2 NM3".

Why the carve-out exists: the AST audit logic lives in one place
(``personas/_audit.py``) so consumers do not duplicate the walk. Production
code never has a legitimate reason to consume a private helper, so the ban
on private imports applies everywhere except this specific test module
which IS the audit consumer.

What this test enforces (PRP-7a R1 M2 acceptance gate):
    - Walks every ``.py`` file under ``.claude/scripts/``,
      ``.claude/chat/``, ``.claude/hooks/``.
    - Excludes ``tests/`` subtrees, ``personas/``, ``config.py``,
      ``__pycache__``, ``.venv``, ``worktrees``, ``node_modules``.
    - For each file in scope, calls
      ``personas._audit._assert_no_install_dir_paths(file)`` and asserts
      the returned list is empty. The helper detects:
        1. ``Path(__file__).parent[.parent...] / ".env"`` (parent-path math)
        2. ``<NAME> / ".env"`` (e.g. ``_SCRIPTS_DIR / ".env"``)
        3. ``Path(...) / "data"`` and ``Path(...) / "state"`` (install-dir
           subdir joins that should route through the persona resolver)
        4. Bare ``load_dotenv()`` (no env-path arg)

The whitelist below mirrors PRIVATE_IMPORT_WHITELIST in
``test_personas_public_api.py`` — both files document the same R2 NM3
contract from different angles (the public-API test asserts the ban,
this test consumes the carve-out).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

# === R2 NM3 whitelist mirror ===
# This file is the SOLE production-non-test consumer allowed to import
# ``personas._audit._assert_no_install_dir_paths``. The matching tuple in
# ``tests/test_personas_public_api.py::PRIVATE_IMPORT_WHITELIST`` keeps
# this entry locked in via a separate enforcement test. Adding a new
# whitelist entry requires updating both files AND a fresh R-pass review.
WHITELIST_DOC = (
    ".claude/scripts/tests/test_no_install_dir_paths.py",
)

# Carve-out import — fully-qualified path, no top-level ``from personas``
# re-export (the public ``__all__`` table deliberately excludes this name).
# E402 is acknowledged: the import lives below the WHITELIST_DOC constant
# so the contract documentation appears at file top-of-mind.
from personas._audit import _assert_no_install_dir_paths  # noqa: E402

# Repo root — `tests/test_no_install_dir_paths.py` -> tests -> scripts ->
# .claude -> repo root. Matches the resolution shape used by
# `test_personas_public_api.py`.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# Production-code roots that are scanned for the audit. Mirrors the same
# scope as `test_personas_public_api.py::_PRODUCTION_ROOTS`.
_PRODUCTION_ROOTS: tuple[Path, ...] = (
    _REPO_ROOT / ".claude" / "scripts",
    _REPO_ROOT / ".claude" / "chat",
    _REPO_ROOT / ".claude" / "hooks",
)

# Files / directory segments exempt from the audit per PRP-7a R1 M2
# whitelist:
#     - ``config.py``  -> the legitimate owner of the persona-resolved
#                         constants. It calls ``load_dotenv(ENV_FILE, ...)``
#                         with an explicit arg, but the helper would
#                         flag the install-dir derivation patterns it
#                         legitimately uses.
#     - ``personas/``  -> contains the resolver itself. ``core.py`` builds
#                         install-dir paths from ``Path(__file__)`` math —
#                         that is the WHOLE POINT of the resolver.
#     - ``tests/``     -> already excluded by directory-skip below; listed
#                         here for documentation completeness.
#     - templates      -> no production templates dir exists today; left
#                         in the doc string for future contributors.
#
# IMPORTANT: ``service.py`` is NOT in this whitelist. Per post-build F2
# review (PRP-7a §"Out-of-Scope" only defers the SPECIFIC ``STOP_FILE``
# pattern, not the whole file), an entire-file whitelist is too broad —
# it would let a NEW hardcoded ``.env``, ``data``, or ``state`` path slip
# in and pass the gate silently. Instead, ``service.py`` is scanned and
# its violations are matched against ``_DEFERRED_VIOLATIONS`` below.
_WHITELIST_FILES: frozenset[str] = frozenset({
    "config.py",
})
_WHITELIST_DIR_SEGMENTS: frozenset[str] = frozenset({
    "personas",
    "tests",
    "__pycache__",
    ".venv",
    "worktrees",
    "node_modules",
    "templates",
})

# Documented Phase-3 deferrals — NARROW per-line exceptions, not file-level
# bypasses. Keys are relative-posix paths from repo root. Values are the
# EXACT violation strings the audit should emit for that file. The main
# audit test asserts the actual violations are a subset of the expected
# set; ANY new violation in a deferred file fails the gate.
#
# Adding a new deferral requires:
#   1. PRP scope explicitly defers the migration (cite the §Out-of-Scope row)
#   2. R-pass review approves the narrow carve-out
#   3. Both the expected violation strings AND a comment justifying each
#
# When a deferred phase lands and removes the underlying pattern, the entry
# below should also be removed (the gate will still pass without it — the
# allowlist becomes a no-op once actual violations drop to zero).
# Format: violation strings are stored as ``<line> <message>`` — the
# absolute-path prefix is stripped at comparison time so this dict is
# portable across machines / OSes (the audit helper emits
# ``<abs_path>:<line> <message>`` which contains a Windows drive letter
# colon, so a naive ``split(":", 1)`` would corrupt the message).
_DEFERRED_VIOLATIONS: dict[str, frozenset[str]] = {
    # PRP-7a §"Out-of-Scope" — Bot pid/lock/mutex consolidation deferred to
    # PRP-7c (Phase 3). STOP_FILE = SCRIPTS_DIR.parent / "data" / "state" /
    # "service-stop". Phase 3 reroutes via personas.get_persona_paths(...).
    # The audit flags the "/ data" and "/ state" subdir joins as identical
    # messages; frozenset dedupes the duplicate to a single expected entry.
    ".claude/scripts/service.py": frozenset({
        "27 forbidden install-dir subdir join (use personas.get_persona_paths)",
    }),
}


def _strip_abs_path_prefix(violation: str, abs_path: Path) -> str:
    """Strip ``<abs_path>:`` prefix from a violation string.

    The audit helper emits ``<abs_path>:<line> <message>``. We compare
    against ``<line> <message>`` to keep ``_DEFERRED_VIOLATIONS`` portable
    across machines (Windows drive letters and POSIX absolute paths look
    very different).
    """
    abs_str = str(abs_path.resolve())
    prefix = f"{abs_str}:"
    if violation.startswith(prefix):
        return violation[len(prefix):]
    return violation


def _collect_python_files(roots: Iterable[Path]) -> list[Path]:
    """Return every ``.py`` file under *roots* outside the whitelist.

    Skips:
        - any path with a whitelisted directory segment (e.g.
          ``personas``, ``tests``, ``__pycache__``)
        - any whitelisted filename (``config.py``)
    """
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            rel_parts = set(path.relative_to(_REPO_ROOT).parts)
            if rel_parts & _WHITELIST_DIR_SEGMENTS:
                continue
            if path.name in _WHITELIST_FILES:
                continue
            files.append(path)
    return files


def _to_relative_posix(path: Path) -> str:
    """Return *path* relative to repo root, with forward slashes."""
    return path.relative_to(_REPO_ROOT).as_posix()


def test_audit_helper_is_importable() -> None:
    """Sanity check — the private helper is reachable via the documented path.

    Catches regressions where someone moves ``_audit.py`` or renames the
    helper without updating the whitelist in
    ``test_personas_public_api.py``.
    """
    assert callable(_assert_no_install_dir_paths)
    # Helper signature contract (PRP-7a §"API Surface > Internal-only"):
    # takes a single Path arg, returns list[str]. We exercise it on this
    # very test file so the type contract is observable from the smoke.
    result = _assert_no_install_dir_paths(Path(__file__))
    assert isinstance(result, list)
    # This test file itself uses string ``".env"`` / ``"data"`` etc. as
    # documentation strings only (not in Path / Path expressions), so it
    # should pass the audit cleanly.
    assert all(isinstance(msg, str) for msg in result)


def test_whitelist_doc_matches_private_import_whitelist() -> None:
    """Local WHITELIST_DOC must match `PRIVATE_IMPORT_WHITELIST` exactly.

    PRP-7a R2 NM3 — both files document the SAME contract. If they
    diverge a future R-pass reviewer cannot tell which list is canonical.
    """
    from tests.test_personas_public_api import PRIVATE_IMPORT_WHITELIST

    assert tuple(WHITELIST_DOC) == tuple(PRIVATE_IMPORT_WHITELIST), (
        "PRP-7a R2 NM3 — WHITELIST_DOC in this file disagrees with "
        "PRIVATE_IMPORT_WHITELIST in test_personas_public_api.py. "
        "Both must mirror the single carve-out."
    )


def test_no_install_dir_paths_in_production_code() -> None:
    """R1 M2 — every production file outside the whitelist passes the audit.

    Scope (PRP-7a §"Cross-Cutting Concerns > Anti-pattern AST scan"):
        - ``.claude/scripts/`` (excluding ``personas/``, ``tests/``,
          ``config.py``)
        - ``.claude/chat/``    (excluding ``tests/`` if present)
        - ``.claude/hooks/``   (no test subtree expected)

    Whitelist:
        - ``config.py``  -> owner of the persona-resolved constants
        - ``personas/``  -> contains the resolver itself
        - ``tests/``     -> fixtures + this file (the carve-out consumer)
        - templates      -> generated content; no production templates today

    Each violation is reported with the source file and line number so
    a future regression points the reviewer straight at the offending
    construct.
    """
    files = _collect_python_files(_PRODUCTION_ROOTS)
    # Sanity check — we expect to walk a non-trivial number of files.
    # If this drops to 0 the test is silently passing despite scope drift.
    assert len(files) >= 30, (
        f"Production scope shrank to {len(files)} files — verify "
        f"_PRODUCTION_ROOTS still resolves. Whitelist drift is a likely "
        f"cause if you recently added a directory."
    )

    violations: list[str] = []
    scanned = 0
    deferred_files_actually_scanned: set[str] = set()
    for path in files:
        scanned += 1
        rel = _to_relative_posix(path)
        per_file = _assert_no_install_dir_paths(path)
        if rel in _DEFERRED_VIOLATIONS:
            deferred_files_actually_scanned.add(rel)
            expected = _DEFERRED_VIOLATIONS[rel]
            # Normalize each audit string by stripping the abs-path prefix
            # so comparison is portable; see ``_strip_abs_path_prefix``.
            actual = frozenset(
                _strip_abs_path_prefix(v, path) for v in per_file
            )
            # NEW violations (not in the deferred set) fail the gate.
            # Missing-expected violations are tolerated — if Phase 3 lands
            # and removes the underlying pattern early, that's fine; the
            # deferred entry simply becomes a no-op.
            new_violations = actual - expected
            for msg in sorted(new_violations):
                violations.append(
                    f"{rel}: NEW violation not in deferred set: {msg}"
                )
        elif per_file:
            for msg in per_file:
                violations.append(f"{rel}: {msg}")
    # Catch scope drift — if a deferred file disappears from the scan
    # (e.g. someone moves it into the excluded ``personas/`` directory or
    # whitelists it whole), the entry stops protecting the codebase.
    unscanned_deferred = (
        set(_DEFERRED_VIOLATIONS.keys()) - deferred_files_actually_scanned
    )
    assert not unscanned_deferred, (
        f"PRP-7a R1 M2 — deferred files were not scanned: "
        f"{sorted(unscanned_deferred)}. Check directory exclusions and "
        f"_WHITELIST_FILES."
    )

    assert not violations, (
        f"PRP-7a R1 M2 — anti-pattern AST audit found "
        f"{len(violations)} violation(s) across {scanned} production file(s):"
        + "\n  "
        + "\n  ".join(violations)
        + "\n\nFixes:\n"
        "  - parent-path .env math      -> import ENV_FILE from config\n"
        "  - Path(...) / 'data'/'state' -> personas.get_persona_paths(...)\n"
        "  - bare load_dotenv()         -> load_dotenv(ENV_FILE, override=True)"
    )


@pytest.mark.parametrize(
    "case_name,fragment",
    [
        ("parent_path_env", 'Path(__file__).parent / ".env"'),
        ("scripts_dir_env", '_SCRIPTS_DIR / ".env"'),
        ("path_data_subdir", 'Path("/tmp") / "data"'),
        ("path_state_subdir", 'Path("/tmp") / "state"'),
        ("bare_load_dotenv", 'load_dotenv()'),
    ],
)
def test_audit_detects_each_anti_pattern(
    tmp_path: Path, case_name: str, fragment: str
) -> None:
    """The audit helper actually flags each forbidden construct.

    Exercises the private helper against synthesized snippets so the
    enforcement is provable, not just observed-empty against a clean
    codebase. If a future contributor accidentally weakens
    ``_assert_no_install_dir_paths`` (e.g. drops a clause), this test
    catches it before the codebase-wide audit goes silently dark.
    """
    # Each fragment lives in its own tmp file. We give the snippet a
    # shape the AST parser will accept: ``from pathlib import Path``
    # plus an assignment that uses the fragment.
    src = "from pathlib import Path\n"
    if case_name == "bare_load_dotenv":
        src += "from dotenv import load_dotenv\n"
        src += f"{fragment}\n"
    else:
        src += f"_X = {fragment}\n"
    target = tmp_path / f"{case_name}.py"
    target.write_text(src, encoding="utf-8")

    violations = _assert_no_install_dir_paths(target)
    assert violations, (
        f"Audit should have flagged '{case_name}' fragment {fragment!r} "
        f"but returned an empty list. Audit weakened?"
    )


def test_audit_returns_empty_for_legitimate_patterns(tmp_path: Path) -> None:
    """The audit MUST NOT false-positive on legitimate code shapes.

    Covers:
        - ``load_dotenv(ENV_FILE, override=True)`` — the migration target.
        - ``Path("/some/abs/path") / "memory"`` — keys other than data/state
          (covered by ``personas.get_persona_paths``, NOT by this audit).
        - String literal ``".env"`` not used in a Path division.
    """
    src = (
        "from pathlib import Path\n"
        "from dotenv import load_dotenv\n"
        "ENV_FILE = Path('/x/.env')\n"
        "load_dotenv(ENV_FILE, override=True)\n"
        "MEMORY = Path('/x') / 'memory'\n"
        "DESC = '.env file path'\n"
    )
    target = tmp_path / "clean.py"
    target.write_text(src, encoding="utf-8")
    violations = _assert_no_install_dir_paths(target)
    # ``load_dotenv(ENV_FILE, override=True)`` has args, so it should NOT
    # be flagged. ``Path('/x/.env')`` uses a literal string Path constructor,
    # NOT a parent-path division — also clean. ``Path('/x') / 'memory'`` is
    # the ``memory`` key (resolved elsewhere), not data/state.
    assert violations == [], (
        f"False positive on legitimate code shapes: {violations!r}"
    )
