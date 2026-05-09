"""Cross-file AST anti-pattern enforcement — PRD-8 Phase 2 / criterion 9 + 16.

Mandatory acceptance test (per PRP §criterion 9: "Mandatory acceptance
criterion, not optional polish (per R1 M3)"). The localized AST checks
inside ``test_identity_payload.py`` only cover one file; this file covers
the full Phase 2 surface — all 6 files touched by the agent identity
reconciliation refactor.

Rule 1: no FunctionDef has a default arg whose unparsed expression is a
single uppercase identifier (the canonical "binds tunable config at def
time" trap). Use ``param=None`` sentinel + body-side resolution instead.

Rule 2: no module-level ``.read_text()`` / ``.read_bytes()`` / ``open()``
calls. File I/O happens inside function bodies, so reads are repeatable
and respect runtime state — not cached at import time.

The 6 files covered:
- ``.claude/chat/cognition/identity_payload.py``
- ``.claude/chat/engine.py``
- ``.claude/scripts/memory_reflect.py``
- ``.claude/scripts/memory_weekly.py``
- ``.claude/scripts/memory_dream.py``
- ``.claude/scripts/personas/services.py``
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# File set under audit (resolved relative to repo layout, not cwd)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_CHAT_DIR = _REPO_ROOT / ".claude" / "chat"
_SCRIPTS_DIR = _REPO_ROOT / ".claude" / "scripts"

AUDITED_FILES: list[Path] = [
    _CHAT_DIR / "cognition" / "identity_payload.py",
    _CHAT_DIR / "engine.py",
    _SCRIPTS_DIR / "memory_reflect.py",
    _SCRIPTS_DIR / "memory_weekly.py",
    _SCRIPTS_DIR / "memory_dream.py",
    _SCRIPTS_DIR / "personas" / "services.py",
    # PRD-8 Phase 5a / WS1.11 (B7) — cabinet modules join the canonical
    # AUDITED_FILES list (NOT a separate test file).
    _SCRIPTS_DIR / "cabinet" / "meeting_channel.py",
    _SCRIPTS_DIR / "cabinet" / "text_orchestrator.py",
    _SCRIPTS_DIR / "cabinet" / "text_router.py",
    _SCRIPTS_DIR / "cabinet" / "tool_policy.py",
    _SCRIPTS_DIR / "cabinet" / "title.py",
    _CHAT_DIR / "cabinet_text.py",
]

# Stable parametrize ids — relative to repo for legible failure output.
AUDITED_IDS: list[str] = [
    str(p.relative_to(_REPO_ROOT)).replace("\\", "/") for p in AUDITED_FILES
]

_UPPERCASE_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def _parse(path: Path) -> ast.Module:
    """Parse *path* into an AST module — fails the test if file missing."""
    assert path.is_file(), f"Phase 2 audit file missing: {path}"
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _is_main_guard(test: ast.expr) -> bool:
    """Return True iff *test* is the canonical ``__name__ == "__main__"`` check.

    Code inside such a guard body never runs at import time, so module-level
    file reads there are Rule-2-safe (no caching of state into module).
    """
    if not isinstance(test, ast.Compare):
        return False
    if len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return False
    if len(test.comparators) != 1:
        return False
    left = test.left
    right = test.comparators[0]
    # Either side may hold the Name vs the Constant.
    name_node, const_node = (None, None)
    if isinstance(left, ast.Name) and isinstance(right, ast.Constant):
        name_node, const_node = left, right
    elif isinstance(right, ast.Name) and isinstance(left, ast.Constant):
        name_node, const_node = right, left
    else:
        return False
    return name_node.id == "__name__" and const_node.value == "__main__"


# ---------------------------------------------------------------------------
# Rule 1 — no default arg binds an UPPERCASE module-level constant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", AUDITED_FILES, ids=AUDITED_IDS)
def test_rule1_no_default_arg_bind_config(path: Path) -> None:
    """Rule 1: function defaults must not unparse to a bare uppercase name.

    e.g. ``def f(x=DEFAULT_INCLUDE)`` would silently cache the value at
    ``def`` time — runtime overrides via monkeypatch / config reload would
    be ignored. The canonical fix is ``def f(x=None)`` then resolve in the
    body. AST-walks every FunctionDef/AsyncFunctionDef in *path*.
    """
    tree = _parse(path)

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        defaults = list(node.args.defaults) + list(node.args.kw_defaults)
        for default in defaults:
            if default is None:
                continue
            try:
                src = ast.unparse(default).strip()
            except Exception:
                continue
            if _UPPERCASE_NAME.match(src):
                offenders.append(
                    f"{path.name}:{node.lineno} — def {node.name}(... ={src})"
                )

    assert not offenders, (
        f"Rule 1 violation — default arg binds UPPERCASE constant: {offenders}"
    )


# ---------------------------------------------------------------------------
# Rule 2 — no module-level file reads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", AUDITED_FILES, ids=AUDITED_IDS)
def test_rule2_no_module_level_file_reads(path: Path) -> None:
    """Rule 2: no ``.read_text()`` / ``.read_bytes()`` / ``open()`` at module top level.

    Reads at module top run once at import time and cache content into
    module state — making subsequent runtime changes invisible. All file
    I/O for these 6 files must happen inside function bodies. ``Path(...)``
    construction at module top is fine (no I/O); the rule targets
    actual call sites.
    """
    tree = _parse(path)

    forbidden_attrs = {"read_text", "read_bytes"}
    forbidden_funcs = {"open"}

    offenders: list[str] = []
    # Iterate only TOP-LEVEL statements; skip into FunctionDef/AsyncFunctionDef
    # /ClassDef bodies because reads inside those are allowed. Also skip
    # ``if __name__ == "__main__":`` guard bodies — those run only on script
    # invocation, never at import time, so they don't cache module state.
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.If) and _is_main_guard(node.test):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            if isinstance(func, ast.Attribute) and func.attr in forbidden_attrs:
                offenders.append(
                    f"{path.name}:{sub.lineno} — .{func.attr}() at module level"
                )
            elif isinstance(func, ast.Name) and func.id in forbidden_funcs:
                offenders.append(
                    f"{path.name}:{sub.lineno} — {func.id}() at module level"
                )

    assert not offenders, (
        f"Rule 2 violation — module-level file reads: {offenders}"
    )


# ---------------------------------------------------------------------------
# Coverage sanity — the parametrize set isn't accidentally trimmed
# ---------------------------------------------------------------------------


def test_audit_covers_phase_2_six_files_plus_phase_5a_cabinet() -> None:
    """Phase 2 locks 6 identity-reconciliation files; Phase 5a (B7) adds 6 cabinet files."""
    expected_names = {
        # Phase 2 (6).
        "identity_payload.py",
        "engine.py",
        "memory_reflect.py",
        "memory_weekly.py",
        "memory_dream.py",
        "services.py",
        # Phase 5a (B7) — 5 cabinet modules + 1 chat-side shim.
        "meeting_channel.py",
        "text_orchestrator.py",
        "text_router.py",
        "tool_policy.py",
        "title.py",
        "cabinet_text.py",
    }
    actual_names = {p.name for p in AUDITED_FILES}
    assert actual_names == expected_names, (
        f"Audit file set drifted; expected {expected_names}, got {actual_names}"
    )


# ---------------------------------------------------------------------------
# PRD-8 Phase 3 / WS1 — dashboard_db.py anti-pattern enforcement
# ---------------------------------------------------------------------------
#
# WS1 ships ``.claude/scripts/dashboard_db.py``. WS2 (next workstream) ships
# ``.claude/scripts/dashboard_api.py`` + ``.claude/scripts/dashboard_bot_lifecycle.py``.
# This block enforces Rule 1 + Rule 2 on the WS1 surface NOW, and leaves
# placeholder slots for the WS2 files. When WS2 lands, swap the
# ``pytest.skip`` calls below for real path bindings.
#
# Why grep + AST over the dedicated dashboard files (not just adding them to
# AUDITED_FILES): the dashboard slice has its own owner charter (dashboard-owner)
# and may grow separately from the Phase 2 identity-reconciliation surface.
# Keeping a dedicated test bucket makes drift visible to the right reviewer.

_DASHBOARD_DB_PATH = _SCRIPTS_DIR / "dashboard_db.py"

# WS2 placeholders — populated when WS2 ships dashboard_api + lifecycle modules.
# TODO(WS2): replace these with real path bindings once
# ``.claude/scripts/dashboard_api.py`` and
# ``.claude/scripts/dashboard_bot_lifecycle.py`` are created. Until then,
# the placeholder tests skip cleanly so WS1 can land green without WS2 work.
_DASHBOARD_API_PATH = _SCRIPTS_DIR / "dashboard_api.py"
_DASHBOARD_BOT_LIFECYCLE_PATH = _SCRIPTS_DIR / "dashboard_bot_lifecycle.py"


# Pattern matching what Rule 1 considers a violation: a default arg whose
# unparsed source is ``config.SOMETHING`` (any dotted reference into config).
# The Phase 2 helper above only catches BARE uppercase identifiers (e.g.
# ``=DEFAULT_INCLUDE``); this Phase 3 helper additionally catches the
# qualified pattern ``=config.X`` which is the more idiomatic WS1/WS2 trap.
_CONFIG_DOTTED = re.compile(r"^config\.[A-Za-z_][A-Za-z0-9_]*$")


def _scan_default_arg_violations(path: Path) -> list[str]:
    """Return Rule 1 offenders in ``path`` — both bare-uppercase + ``config.X``.

    The Phase 2 ``test_rule1_no_default_arg_bind_config`` helper only catches
    bare uppercase identifier defaults. Dashboard endpoint handlers are far
    more likely to write ``def f(x=config.DASHBOARD_DB_PATH)`` (qualified)
    than ``def f(x=DASHBOARD_DB_PATH)`` (unqualified import). Catch both.
    """
    tree = _parse(path)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        defaults = list(node.args.defaults) + list(node.args.kw_defaults)
        for default in defaults:
            if default is None:
                continue
            try:
                src = ast.unparse(default).strip()
            except Exception:
                continue
            if _UPPERCASE_NAME.match(src) or _CONFIG_DOTTED.match(src):
                offenders.append(
                    f"{path.name}:{node.lineno} — def {node.name}(... ={src})"
                )
    return offenders


def test_dashboard_db_rule_1_no_default_arg_bind_config() -> None:
    """Rule 1 on ``dashboard_db.py``: no default arg binds ``config.X`` or
    a bare uppercase constant.

    The canonical Rule 1 trap for this slice is
    ``def __init__(self, db_path=config.DASHBOARD_DB_PATH)`` — that caches
    the path at ``def`` time and ignores test monkeypatches. The fix is
    ``db_path: Path | None = None`` then resolve in the body via
    ``_resolve_db_path()``. This test grep+ASTs the file and rejects either
    pattern.
    """
    assert _DASHBOARD_DB_PATH.is_file(), (
        f"dashboard_db.py missing at {_DASHBOARD_DB_PATH} — WS1 incomplete"
    )
    offenders = _scan_default_arg_violations(_DASHBOARD_DB_PATH)
    assert not offenders, (
        f"Rule 1 violation in dashboard_db.py — default arg binds tunable "
        f"config: {offenders}"
    )


def test_dashboard_db_rule_2_no_module_level_state() -> None:
    """Rule 2 on ``dashboard_db.py``: no module-level mutable state caching
    the resolved path, the connection, or schema-applied flag.

    The slice's correctness depends on every call to ``get_connection`` /
    ``DashboardDB.connect`` re-resolving the path and re-opening the
    connection (FastAPI threadpool compatibility). Module-level state
    (``_RESOLVED = config.DASHBOARD_DB_PATH``, ``_CONN = None``,
    ``_SCHEMA_APPLIED = False``) would silently break test isolation and
    runtime config swaps.

    This test allows: imports, function/class defs, ``__all__``, ``__future__``
    annotations, and module-scope CONSTANT strings (the DDL is one such
    constant — it's safe because it's immutable). It rejects any other
    module-level assignment that builds runtime state.
    """
    assert _DASHBOARD_DB_PATH.is_file(), (
        f"dashboard_db.py missing at {_DASHBOARD_DB_PATH} — WS1 incomplete"
    )
    tree = _parse(_DASHBOARD_DB_PATH)

    forbidden_attrs = {"read_text", "read_bytes"}
    forbidden_funcs = {"open"}

    # Reject Rule-2-shaped reads at module level (same logic as the Phase 2
    # helper but scoped to this single file).
    read_offenders: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.If) and _is_main_guard(node.test):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            if isinstance(func, ast.Attribute) and func.attr in forbidden_attrs:
                read_offenders.append(
                    f"dashboard_db.py:{sub.lineno} — .{func.attr}() at module level"
                )
            elif isinstance(func, ast.Name) and func.id in forbidden_funcs:
                read_offenders.append(
                    f"dashboard_db.py:{sub.lineno} — {func.id}() at module level"
                )
    assert not read_offenders, (
        f"Rule 2 violation in dashboard_db.py — module-level file reads: "
        f"{read_offenders}"
    )

    # Reject calls into config / sqlite3 at module top — those would cache
    # state. ``import`` statements are fine; assignments like
    # ``_RESOLVED = config.DASHBOARD_DB_PATH`` or ``_CONN = sqlite3.connect(...)``
    # are NOT.
    cache_offenders: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        # Only flag PRIVATE-or-public assignments to an actual call into
        # config or sqlite3 — these are the canonical cache shapes.
        for tgt in node.targets:
            if not isinstance(tgt, ast.Name):
                continue
            if not isinstance(node.value, (ast.Call, ast.Attribute)):
                continue
            try:
                src = ast.unparse(node.value).strip()
            except Exception:
                continue
            # Catch anything that calls into config or sqlite3 at module top.
            if src.startswith(("config.", "sqlite3.")) and "(" in src:
                cache_offenders.append(
                    f"dashboard_db.py:{node.lineno} — "
                    f"module-level cache: {tgt.id} = {src}"
                )
    assert not cache_offenders, (
        f"Rule 2 violation in dashboard_db.py — module-level cache of "
        f"resolved state: {cache_offenders}"
    )


# ---------------------------------------------------------------------------
# WS2 placeholders — filled when dashboard_api.py + dashboard_bot_lifecycle.py
# ship. They skip cleanly so the dashboard_db -k filter in CI passes today
# and the WS2 PR is the one that flips them on.
# ---------------------------------------------------------------------------


def _scan_module_level_file_reads(path: Path) -> list[str]:
    """Return Rule 2 offenders in ``path`` — module-level read_text/read_bytes/open."""
    tree = _parse(path)

    forbidden_attrs = {"read_text", "read_bytes"}
    forbidden_funcs = {"open"}

    offenders: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.If) and _is_main_guard(node.test):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            if isinstance(func, ast.Attribute) and func.attr in forbidden_attrs:
                offenders.append(
                    f"{path.name}:{sub.lineno} — .{func.attr}() at module level"
                )
            elif isinstance(func, ast.Name) and func.id in forbidden_funcs:
                offenders.append(
                    f"{path.name}:{sub.lineno} — {func.id}() at module level"
                )
    return offenders


def test_dashboard_api_rule_1_no_default_arg_bind_config() -> None:
    """Rule 1 on ``dashboard_api.py``: no def-time bind to ``config.X`` or
    bare uppercase constants.

    The high-risk pattern for endpoint handlers is
    ``def get_tokens(range=config.DEFAULT_TOKEN_RANGE)`` — caches at def
    time and ignores test monkeypatches. The fix is ``range: str | None = None``
    + body-side resolution.
    """
    assert _DASHBOARD_API_PATH.is_file(), (
        f"dashboard_api.py missing at {_DASHBOARD_API_PATH} — WS2 incomplete"
    )
    offenders = _scan_default_arg_violations(_DASHBOARD_API_PATH)
    assert not offenders, (
        f"Rule 1 violation in dashboard_api.py — default arg binds tunable "
        f"config: {offenders}"
    )


def test_dashboard_api_rule_2_no_module_level_state() -> None:
    """Rule 2 on ``dashboard_api.py``: no module-level file reads or
    config/sqlite3 caches.

    Module-level imports + immutable constants (regex, frozenset,
    ``_VALID_PERSONA_ID``) are fine. ``_RESOLVED = config.X`` or
    ``_PERSONA_LIST = walk_personas()`` is NOT. SSE replay buffer is an
    ephemeral runtime cache, not resolved-config state — it gets a
    narrow allow.
    """
    assert _DASHBOARD_API_PATH.is_file(), (
        f"dashboard_api.py missing at {_DASHBOARD_API_PATH} — WS2 incomplete"
    )
    read_offenders = _scan_module_level_file_reads(_DASHBOARD_API_PATH)
    assert not read_offenders, (
        f"Rule 2 violation in dashboard_api.py — module-level file reads: "
        f"{read_offenders}"
    )

    tree = _parse(_DASHBOARD_API_PATH)
    cache_offenders: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if not isinstance(tgt, ast.Name):
                continue
            if not isinstance(node.value, (ast.Call, ast.Attribute)):
                continue
            try:
                src = ast.unparse(node.value).strip()
            except Exception:
                continue
            if src.startswith(("config.", "sqlite3.")) and "(" in src:
                cache_offenders.append(
                    f"dashboard_api.py:{node.lineno} — "
                    f"module-level cache: {tgt.id} = {src}"
                )
    assert not cache_offenders, (
        f"Rule 2 violation in dashboard_api.py — module-level cache of "
        f"resolved state: {cache_offenders}"
    )


def test_dashboard_bot_lifecycle_rule_1_no_default_arg_bind_config() -> None:
    """Rule 1 on ``dashboard_bot_lifecycle.py``: no def-time bind to
    ``config.DASHBOARD_BOT_GRACE_SECONDS`` or any uppercase constant.

    Every public function (activate / deactivate / restart) must use
    ``grace_seconds: int | None = None`` + body-side resolution.
    """
    assert _DASHBOARD_BOT_LIFECYCLE_PATH.is_file(), (
        f"dashboard_bot_lifecycle.py missing at {_DASHBOARD_BOT_LIFECYCLE_PATH} "
        f"— WS2 incomplete"
    )
    offenders = _scan_default_arg_violations(_DASHBOARD_BOT_LIFECYCLE_PATH)
    assert not offenders, (
        f"Rule 1 violation in dashboard_bot_lifecycle.py — default arg "
        f"binds tunable config: {offenders}"
    )


def test_dashboard_bot_lifecycle_rule_2_no_module_level_state() -> None:
    """Rule 2 on ``dashboard_bot_lifecycle.py``: no module-level file
    reads or config/shared caches.

    Constants like ``_DASHBOARD_ONLY_KEYS`` (frozenset) and
    ``_BOT_CREDS_PREFIXES`` (tuple) are fine — they're literal data
    structures, not call results. ``_PROFILE_ROOTS = walk_personas()``
    would be NOT.
    """
    assert _DASHBOARD_BOT_LIFECYCLE_PATH.is_file(), (
        f"dashboard_bot_lifecycle.py missing at {_DASHBOARD_BOT_LIFECYCLE_PATH} "
        f"— WS2 incomplete"
    )
    read_offenders = _scan_module_level_file_reads(_DASHBOARD_BOT_LIFECYCLE_PATH)
    assert not read_offenders, (
        f"Rule 2 violation in dashboard_bot_lifecycle.py — module-level "
        f"file reads: {read_offenders}"
    )

    tree = _parse(_DASHBOARD_BOT_LIFECYCLE_PATH)
    cache_offenders: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if not isinstance(tgt, ast.Name):
                continue
            if not isinstance(node.value, (ast.Call, ast.Attribute)):
                continue
            try:
                src = ast.unparse(node.value).strip()
            except Exception:
                continue
            if src.startswith(("config.", "shared.", "sqlite3.")) and "(" in src:
                cache_offenders.append(
                    f"dashboard_bot_lifecycle.py:{node.lineno} — "
                    f"module-level cache: {tgt.id} = {src}"
                )
    assert not cache_offenders, (
        f"Rule 2 violation in dashboard_bot_lifecycle.py — module-level "
        f"cache: {cache_offenders}"
    )
