"""Private AST audit helpers used by Phase 1 acceptance-gate tests.

This module is INTERNAL. The leading underscore in the filename and the
public API contract documented in PRP-7a §"API Surface" + R2 NM3 mark it
as private — it MUST NOT be re-exported through ``personas/__init__.py``
and is NOT in ``personas.__all__``.

Whitelist (PRP-7a R2 NM3) — only one production-tree consumer is allowed:
    - ``.claude/scripts/tests/test_no_install_dir_paths.py`` may import
      ``personas._audit._assert_no_install_dir_paths`` so the AST logic
      lives in one place.

Production code under ``.claude/scripts/``, ``.claude/chat/``,
``.claude/hooks/`` (excluding ``tests/``) is BANNED from importing any
underscore-prefixed name from ``personas/``. The ban is enforced by
``tests/test_personas_public_api.py`` (Workstream 4a).

The helper exists so PRP-7a R1 M2 / R2 NM3's "single AST audit shape" can
be reused without duplicating the AST walk in every consumer.

Phase 1 status: the helper signature is locked in (returns
``list[str]`` of violation messages) so Workstream 4b's
``test_no_install_dir_paths.py`` can wire to it. Phase 1 ships the helper
shell; Workstream 4b lands the matching test that exercises it. Keeping
the helper here (instead of inline in the test file) lets the public-API
test in 4a verify that nothing in production tries to import it.
"""

from __future__ import annotations

import ast
from pathlib import Path


def _is_dotenv_arg(node: ast.AST) -> bool:
    """Return True if *node* is a Path expression that ends in ``".env"``.

    Catches the two forbidden parent-path env-file shapes:
        - ``Path(__file__).parent[.parent...] / ".env"``
        - ``_SCRIPTS_DIR / ".env"`` (or any module-level constant / ``".env"``)

    These are the two parent-path math anti-patterns that PRP-7a R1 B3
    targets — they bypass the persona resolver and bind to the install
    dir at import time.
    """
    if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
        return False
    right = node.right
    if isinstance(right, ast.Constant) and right.value == ".env":
        return True
    return False


def _is_path_subdir(node: ast.AST, *, names: tuple[str, ...]) -> bool:
    """Return True if *node* is a Path expression ending in one of *names*.

    Used to catch ``Path(...) / "data"``, ``Path(...) / "state"``, etc.,
    where the right-hand side bypasses ``personas.get_persona_paths``.
    """
    if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
        return False
    right = node.right
    if isinstance(right, ast.Constant) and right.value in names:
        return True
    return False


def _is_bare_load_dotenv(node: ast.AST) -> bool:
    """Return True if *node* is a bare ``load_dotenv()`` call (no args).

    PRP-7a R1 B3 — bare ``load_dotenv()`` calls bypass ``config.ENV_FILE``
    entirely and load from cwd / default search paths. The migration
    requires every call site to pass an explicit env path.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name) and func.id == "load_dotenv":
        return not node.args and not node.keywords
    if isinstance(func, ast.Attribute) and func.attr == "load_dotenv":
        return not node.args and not node.keywords
    return False


def _assert_no_install_dir_paths(file: Path) -> list[str]:
    """Return a list of violation messages found in *file*.

    AST-walks *file* and flags:
        1. ``Path(__file__).parent[.parent...] / ".env"`` (parent-path math)
        2. ``<NAME> / ".env"`` (e.g. ``_SCRIPTS_DIR / ".env"``)
        3. ``Path(...) / "data"`` and ``Path(...) / "state"`` (install-dir
           binding for state/data dirs that should route through the
           persona resolver)
        4. Bare ``load_dotenv()`` (no env-path arg)

    Returns an empty list when the file is clean. Each entry in the
    returned list is a human-readable string of the form
    ``"<file>:<lineno> <description>"`` so the consuming test can print
    a useful failure message.

    Whitelist enforcement (PRP-7a R1 M2): the consuming test
    (``test_no_install_dir_paths.py``) restricts which files this helper
    runs against — ``config.py``, ``personas/``, templates, and ``tests/``
    are exempt because they are the legitimate owners of these patterns.
    This helper does NOT contain the whitelist itself; the test does.
    """
    violations: list[str] = []
    try:
        source = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"{file}: failed to read source ({exc})"]

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"{file}:{exc.lineno or 0} syntax error: {exc.msg}"]

    for node in ast.walk(tree):
        # Every AST node yielded from a parsed source file carries ``lineno``
        # at runtime, but ``ast.AST`` (the static base type) does not expose
        # the attribute. ``getattr(..., 0)`` keeps mypy happy without a cast.
        line = getattr(node, "lineno", 0)
        if _is_dotenv_arg(node):
            violations.append(
                f"{file}:{line} forbidden parent-path env-file "
                f"construction (use config.ENV_FILE)"
            )
            continue
        if _is_path_subdir(node, names=("data", "state")):
            violations.append(
                f"{file}:{line} forbidden install-dir subdir join "
                f"(use personas.get_persona_paths)"
            )
            continue
        if _is_bare_load_dotenv(node):
            violations.append(
                f"{file}:{line} bare load_dotenv() — pass "
                f"config.ENV_FILE explicitly"
            )

    return violations
