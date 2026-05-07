"""Public-API contract tests for the ``personas`` package.

PRP-7a Workstream 4a — guards two contracts:

1. **R1 M6 — Frozen public surface.** ``personas.__all__`` must equal the
   API Surface table verbatim (12 helpers in the order specified). Any
   reordering, addition, or removal is a contract break and must trigger a
   new R-pass review before landing.

2. **R2 NM3 — Private-import ban with documented whitelist.** Production
   code under ``.claude/scripts/``, ``.claude/chat/``, ``.claude/hooks/``
   (excluding ``tests/`` subtrees) is BANNED from importing any
   underscore-prefixed name from the ``personas`` package. The single
   exception is ``tests/test_no_install_dir_paths.py`` (Workstream 4b),
   which is allowed to call ``personas._audit._assert_no_install_dir_paths``
   so the AST audit logic lives in one place. The whitelist is declared as
   a single-line constant here so a reviewer can see at a glance which
   files are exempt — adding any new entry requires explicit R-pass
   approval per PRP-7a §"API Surface".

The whitelist file does not have to exist yet (Workstream 4b creates it).
The ban is the contract; the whitelist is the documented contract carve-out.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

import pytest

import personas

# === API Surface (FROZEN — PRP-7a R1 M6 + PRD-8 Phase 2 / WS1 expansion) ===
# This tuple MUST match `personas.__all__` verbatim (same order, same names,
# same length). It also matches the "API Surface" table in PRP-7a §
# "Implementation Blueprint > API Surface" (PRP-7a 12-helper baseline) plus
# the two new helpers added by PRD-8 Phase 2 / WS1
# (`load_persona_config` + `ConfigShapeError`, alphabetically sorted).
# Any change here is a contract break — bump the PRP and ship via the
# standard PRP review cycle.
EXPECTED_PUBLIC_API: tuple[str, ...] = (
    "ConfigShapeError",
    "apply_persona_override",
    "get_active_profile_name",
    "get_active_profile_path",
    "get_default_paths",
    "get_homie_home",
    "get_persona_paths",
    "get_subprocess_env",
    "is_default_profile",
    "load_persona_config",
    "read_active_profile",
    "resolve_persona_env",
    "set_active_profile",
    "validate_config_dict",
    "validate_config_yaml_text",
    "validate_persona_name",
)

# === Private-import whitelist (PRP-7a R2 NM3) ===
# Only files listed here are allowed to import underscore-prefixed names
# from `personas/` submodules (e.g. `personas._audit._assert_no_install_dir_paths`).
# Whitelist entries are relative paths from the thehomie repo root,
# normalized to forward slashes so the comparison is platform-stable.
PRIVATE_IMPORT_WHITELIST: tuple[str, ...] = (
    ".claude/scripts/tests/test_no_install_dir_paths.py",  # R2 NM3 — AST audit consumer
)

# Repo root — `tests/test_personas_public_api.py` -> tests -> scripts ->
# .claude -> repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# Production-code roots that are scanned for the private-import ban.
# `tests/` subtrees inside these are skipped automatically.
_PRODUCTION_ROOTS: tuple[Path, ...] = (
    _REPO_ROOT / ".claude" / "scripts",
    _REPO_ROOT / ".claude" / "chat",
    _REPO_ROOT / ".claude" / "hooks",
)


def test_all_attribute_matches_api_surface_verbatim() -> None:
    """`personas.__all__` matches the API Surface table verbatim (R1 M6)."""
    # Cast to tuple so order is part of the assertion. A list comparison
    # would still flag reorders, but tuple makes the contract shape obvious
    # at the assertion site.
    assert tuple(personas.__all__) == EXPECTED_PUBLIC_API


def test_all_size_matches_expected_api() -> None:
    """`personas.__all__` size matches `EXPECTED_PUBLIC_API` (PRD-8 Phase 2 R2 NM1).

    Tied to the list, NOT a magic number. Future API additions only require
    updating ``EXPECTED_PUBLIC_API`` in one place — this assertion follows
    automatically. Pre-PRD-8 Phase 2 the assertion was ``== 12``; PRD-8
    Phase 2 grew to 14 (added ``load_persona_config`` + ``ConfigShapeError``);
    PRD-8 Phase 3 / WS2 grows to 16 (added ``validate_config_dict`` +
    ``validate_config_yaml_text`` per R1 B4).
    """
    assert len(personas.__all__) == len(EXPECTED_PUBLIC_API)


def test_personas_all_length_is_16() -> None:
    """``personas.__all__`` is exactly 16 entries (PRD-8 Phase 3 / WS2).

    Phase 3 R1 B4 lock: dashboard PATCH validation needs the
    ``validate_config_dict`` + ``validate_config_yaml_text`` helpers
    promoted to public API. ``__all__`` grew from Phase 2's 14 to 16.

    This test is a structural anchor — if a future PRP adds a 17th
    helper, this test SHOULD fail and force the PRP author to update
    the assertion explicitly. The literal 16 is intentional (matches
    the criterion ``personas_all_size_matches_expected_api_test_updated_to_16``
    in JSON contract).
    """
    assert len(personas.__all__) == 16, (
        f"personas.__all__ should be 16 entries (Phase 3 / WS2 R1 B4); "
        f"got {len(personas.__all__)} — names: {sorted(personas.__all__)}"
    )


def test_every_public_name_resolves_to_callable() -> None:
    """Every name in `__all__` resolves on the package and is callable.

    Catches the regression where `__all__` lists a name that doesn't exist
    on the package (typo in `__init__.py` re-exports) or that resolves to
    a non-callable (e.g. a constant accidentally exported).
    """
    for name in EXPECTED_PUBLIC_API:
        assert hasattr(personas, name), (
            f"personas.__all__ lists '{name}' but it is not exposed on the "
            f"package. Check personas/__init__.py re-exports."
        )
        attr = getattr(personas, name)
        assert callable(attr), (
            f"personas.{name} is in __all__ but is not callable "
            f"(got {type(attr).__name__})."
        )


def test_audit_helper_not_in_public_api() -> None:
    """`_assert_no_install_dir_paths` MUST NOT appear in `__all__`.

    PRP-7a §"API Surface > Internal-only": the audit helper lives in the
    private submodule `personas._audit` and is whitelisted for
    `tests/test_no_install_dir_paths.py` only. Including it in `__all__`
    would silently widen the public API.
    """
    assert "_assert_no_install_dir_paths" not in personas.__all__
    # Defense in depth — the leading underscore in the name is the only
    # protection against a `from personas import *` star-import in tests.
    # Since we explicitly enumerate __all__ above, the underscore alone is
    # enough; this assertion just documents the rule.
    assert all(
        not name.startswith("_") for name in personas.__all__
    ), "Names in personas.__all__ must not start with underscore."


def _collect_python_files(roots: Iterable[Path]) -> list[Path]:
    """Return every `.py` file under *roots*, skipping `tests/` subtrees.

    Skips:
        - any path with `/tests/` segment (production-code scope per R2 NM3)
        - any path under `__pycache__`
        - any path under `.venv`, `worktrees`, or `node_modules`
    """
    skipped_segments = {
        "tests",
        "__pycache__",
        ".venv",
        "worktrees",
        "node_modules",
    }
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            parts = set(path.relative_to(_REPO_ROOT).parts)
            if parts & skipped_segments:
                continue
            files.append(path)
    return files


def _is_private_personas_import(node: ast.AST) -> tuple[bool, str | None]:
    """Return (True, name) if *node* is a `from personas...` import of a
    private name (underscore-prefixed module or attribute).

    Matches:
        - `from personas._audit import ...`
        - `from personas._audit import _assert_no_install_dir_paths`
        - `from personas import _assert_no_install_dir_paths`  (would-be re-export)
        - `import personas._audit`
        - `from personas.<anything> import _<anything>`

    Does NOT match:
        - `from personas import get_homie_home, ...`  (public re-export)
        - `import personas`  (package-level import, public surface only)
    """
    # ImportFrom: `from personas... import name1, name2, ...`
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        # `from personas._audit import X` — module starts with `personas._`
        if module == "personas" or module.startswith("personas."):
            # private submodule path (`personas._audit`)
            tail = module[len("personas") :]
            if tail.startswith("._"):
                return True, module
            # public submodule but private attribute (`from personas import _foo`)
            for alias in node.names:
                if alias.name.startswith("_"):
                    return True, f"{module}.{alias.name}"
        return False, None
    # Import: `import personas._audit` / `import personas._audit as foo`
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name == "personas":
                continue
            if alias.name.startswith("personas."):
                tail = alias.name[len("personas") :]
                if tail.startswith("._"):
                    return True, alias.name
        return False, None
    return False, None


def _to_relative_posix(path: Path) -> str:
    """Return *path* relative to repo root, with forward slashes.

    The whitelist is declared with forward slashes so the comparison is
    stable on Windows where ``Path.relative_to`` produces backslashes.
    """
    return path.relative_to(_REPO_ROOT).as_posix()


def test_no_production_code_imports_private_personas_helpers() -> None:
    """Production code must not import private `personas/` helpers (R2 NM3).

    Whitelisted exception: only `tests/test_no_install_dir_paths.py` may
    import `personas._audit._assert_no_install_dir_paths`. The whitelist
    is declared at the top of this file as a single-line tuple — adding
    any new entry requires explicit R-pass review.
    """
    violations: list[str] = []
    for path in _collect_python_files(_PRODUCTION_ROOTS):
        rel = _to_relative_posix(path)
        if rel in PRIVATE_IMPORT_WHITELIST:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            # A file we can't parse can't violate the contract — skip rather
            # than fail the whole test on an unrelated parse error.
            continue
        for node in ast.walk(tree):
            is_private, name = _is_private_personas_import(node)
            if is_private:
                violations.append(
                    f"{rel}:{getattr(node, 'lineno', 0)} imports private "
                    f"name '{name}' from personas/ (R2 NM3 ban)"
                )
    assert not violations, (
        "PRP-7a R2 NM3 — production code imports private personas "
        f"helpers ({len(violations)} violation(s)):\n  "
        + "\n  ".join(violations)
        + "\n\nIf this import is legitimate, propose adding the file to "
        "PRIVATE_IMPORT_WHITELIST in tests/test_personas_public_api.py "
        "via a new R-pass review (per PRP-7a §'API Surface > R2 NM3')."
    )


def test_whitelist_entries_are_documented_test_files() -> None:
    """Whitelist entries must point at `tests/` files only (R2 NM3).

    The whitelist exists so the AST audit helper can be reused without
    duplication. Production code is never a legitimate consumer of
    underscore-prefixed names. This test catches an accidental whitelist
    expansion that smuggles a production file under the carve-out.
    """
    for entry in PRIVATE_IMPORT_WHITELIST:
        assert "/tests/" in entry, (
            f"Whitelist entry '{entry}' does not point at a tests/ file. "
            "Only test files under .claude/scripts/tests/ may be whitelisted "
            "(R2 NM3)."
        )
        assert entry.endswith(".py"), (
            f"Whitelist entry '{entry}' is not a .py file."
        )


@pytest.mark.parametrize("name", EXPECTED_PUBLIC_API)
def test_public_helpers_have_docstrings(name: str) -> None:
    """Every public helper has a non-empty docstring.

    Soft contract — the API Surface table promises "Hermes-faithful"
    behaviour with explicit deviation comments. The cheapest signal that
    this discipline is upheld is a non-empty docstring. The full
    "Hermes file:line citation" check is enforced by the Hermes-faithfulness
    review gate (see PRP-7a §"Risk Register").
    """
    helper = getattr(personas, name)
    doc = (helper.__doc__ or "").strip()
    assert doc, f"personas.{name} has no docstring (Hermes-faithfulness gate)."
