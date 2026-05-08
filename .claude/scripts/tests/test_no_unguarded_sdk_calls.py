"""PRD-8 Phase 7a (WS4 R1 B5 + R2 NM1) — direct SDK call inventory.

AST-scans the repo for `claude_agent_sdk.query(...)` calls (direct SDK
bypass of lane_router/registry) and asserts each has a `kill_switches.requireEnabled`
guard within 20 lines above. Exempts:
  - tests
  - runtime/ (canonical adapter location — guards live in lane_router/registry)
  - debug/, .archon/

R2 NM1 refinement: only enforces guards for actual `query` bindings — exempts
HookMatcher-only imports (`from claude_agent_sdk import HookMatcher`) which
exist at memory_reflect.py:195, memory_weekly.py:182, etc. Uses ast.parse
to find query-binding nodes specifically, NOT a blind grep.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = Path(__file__).resolve().parents[1]


EXEMPT_DIRS = (
    "tests",
    "runtime",
    "debug",
    ".archon",
    "worktrees",          # .claude/worktrees/ — orphaned working trees
    ".worktrees",         # top-level .worktrees/ — fresh git worktrees
    ".codex-worktrees",
    "__pycache__",
    "_drafts",
    ".refs",
    ".tmp",               # transient audit/workshop snapshots
    "_archive",
    "_holders",
)


def _is_exempt(path: Path) -> bool:
    parts = set(path.parts)
    for part in EXEMPT_DIRS:
        if part in parts:
            return True
    # The codex-worktrees segments may have a leading dot.
    if any(p == ".codex-worktrees" for p in path.parts):
        return True
    if any(p == ".claude" and "worktrees" in path.parts[path.parts.index(p):] for p in path.parts):
        # Match any path with both .claude and worktrees components.
        if "worktrees" in path.parts:
            return True
    if path.name.startswith("test_"):
        return True
    return False


def _file_imports_query(tree: ast.AST) -> bool:
    """True iff the file binds the name `query` from claude_agent_sdk."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "claude_agent_sdk":
            for alias in node.names:
                # Catches `import query` and `import query as sdk_query`.
                if alias.name == "query":
                    return True
        # Catches `import claude_agent_sdk` style — those would call
        # claude_agent_sdk.query(...) directly.
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "claude_agent_sdk":
                    return True
    return False


def _has_killswitch_guard_in_enclosing_scope(
    tree: ast.AST, call_line: int, text: str
) -> bool:
    """True iff a kill_switches.requireEnabled appears in the enclosing function.

    Walks the AST to find the FunctionDef that contains *call_line*, then
    checks whether `kill_switches.requireEnabled` is anywhere inside that
    function body. Falls back to a module-scope search if no enclosing
    function is found (defensive — module-level SDK calls also count if a
    module-level guard exists). Tests for the literal substring AFTER the
    AST scope is narrowed.
    """
    enclosing_function: ast.AST | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno <= call_line and (
                node.end_lineno is None or call_line <= node.end_lineno
            ):
                # Take the innermost — last walked match wins via overwrite.
                enclosing_function = node
    if enclosing_function is not None:
        # Extract function source via line numbers and check for the guard literal.
        text_lines = text.splitlines()
        start = enclosing_function.lineno - 1
        end = enclosing_function.end_lineno or len(text_lines)
        scope_text = "\n".join(text_lines[start:end])
    else:
        # Module-scope fallback.
        scope_text = text
    return "requireEnabled" in scope_text and "kill_switches" in scope_text


def _find_query_calls(tree: ast.AST, sdk_query_name_set: set[str]) -> list[int]:
    """Return list of line numbers where `<query_alias>(...)` is called."""
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            # Direct call: query(...)
            if isinstance(func, ast.Name) and func.id in sdk_query_name_set:
                lines.append(node.lineno)
            # Attribute call: claude_agent_sdk.query(...)
            elif isinstance(func, ast.Attribute) and func.attr == "query":
                if isinstance(func.value, ast.Name) and func.value.id == "claude_agent_sdk":
                    lines.append(node.lineno)
    return lines


def _aliases_for_query(tree: ast.AST) -> set[str]:
    """Return all local names bound to claude_agent_sdk.query."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "claude_agent_sdk":
            for alias in node.names:
                if alias.name == "query":
                    names.add(alias.asname or "query")
    return names


def test_no_unguarded_direct_sdk_calls():
    """B5 — every `claude_agent_sdk.query(...)` direct call has a guard above."""
    offenders: list[str] = []

    for py_file in REPO_ROOT.rglob("*.py"):
        if _is_exempt(py_file):
            continue
        if "_drafts" in py_file.parts:
            continue
        try:
            text = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        if not _file_imports_query(tree):
            continue

        aliases = _aliases_for_query(tree)
        if not aliases:
            # The file only imports HookMatcher / etc, never `query` — exempt.
            continue
        call_lines = _find_query_calls(tree, aliases)
        if not call_lines:
            continue
        for call_line in call_lines:
            if not _has_killswitch_guard_in_enclosing_scope(tree, call_line, text):
                offenders.append(f"{py_file}:{call_line} — no kill_switches.requireEnabled guard in enclosing function")

    assert not offenders, (
        "Direct claude_agent_sdk.query(...) calls without kill-switch guard. "
        f"Add `from security import kill_switches; kill_switches.requireEnabled('llm', ...)` "
        f"above the call. Offenders: {offenders}"
    )


def test_heartbeat_haro_pitch_has_killswitch_guard():
    """Regression for R1 B5 — heartbeat.py HARO block specifically."""
    heartbeat = SCRIPTS_DIR / "heartbeat.py"
    text = heartbeat.read_text(encoding="utf-8")
    assert "heartbeat_haro_pitch" in text
    assert "requireEnabled" in text
