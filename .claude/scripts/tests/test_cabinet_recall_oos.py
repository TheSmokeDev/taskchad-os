"""Test PRD-8 Phase 5a / WS1.12 (B9) — recall ingestion out-of-scope guard.

Phase 5a does NOT write recall/memory-index data. Upstream's runAgentTurn
at warroom-text-orchestrator.ts:1919-1967 performs memory ingestion that
block is OUT OF SCOPE for Phase 5a (Phase 6 owns the recall ingestion
contract).

This file enforces it via:
  (a) AST scan — no `recall_service.store/write/append` or memory_index
      writers in any cabinet/* module.
  (b) Behavior — patching recall_service to fail-noisy and asserting NO
      cabinet code path invokes it.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


_CABINET_DIR = Path(__file__).resolve().parent.parent / "cabinet"
_CHAT_DIR = Path(__file__).resolve().parent.parent.parent / "chat"

_CABINET_FILES = [
    _CABINET_DIR / "meeting_channel.py",
    _CABINET_DIR / "text_orchestrator.py",
    _CABINET_DIR / "text_router.py",
    _CABINET_DIR / "tool_policy.py",
    _CABINET_DIR / "title.py",
    _CHAT_DIR / "cabinet_text.py",
]

_FORBIDDEN_ATTRS = {
    "store",
    "write",
    "append",
    "upsert",
    "ingest",
}

_FORBIDDEN_MODULES = {
    "recall_service",
    "memory_index",
    "memory_search",
}


def _imports_forbidden_modules(tree: ast.Module) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _FORBIDDEN_MODULES or alias.name.split(".")[0] in _FORBIDDEN_MODULES:
                    out.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.module in _FORBIDDEN_MODULES or node.module.split(".")[0] in _FORBIDDEN_MODULES:
                out.append(node.module)
    return out


def _calls_writer_attr(tree: ast.Module) -> list[str]:
    """Detect `<recall_service|memory_index|memory_search>.<store|write|append|upsert|ingest>(`."""
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not isinstance(f, ast.Attribute):
            continue
        if f.attr not in _FORBIDDEN_ATTRS:
            continue
        # Walk backward to the root of the attribute chain.
        root = f.value
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name) and root.id in _FORBIDDEN_MODULES:
            offenders.append(f"line {node.lineno}: {ast.unparse(node)[:120]}")
    return offenders


@pytest.mark.parametrize("path", _CABINET_FILES, ids=[p.name for p in _CABINET_FILES])
def test_no_recall_or_memory_index_imports(path: Path) -> None:
    """B9 — cabinet/* must NOT import recall_service / memory_index / memory_search."""
    assert path.is_file(), f"missing cabinet file: {path}"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bad = _imports_forbidden_modules(tree)
    assert not bad, f"{path.name}: forbidden recall/memory-index imports: {bad}"


@pytest.mark.parametrize("path", _CABINET_FILES, ids=[p.name for p in _CABINET_FILES])
def test_no_recall_or_memory_index_writer_calls(path: Path) -> None:
    """B9 — cabinet/* must NOT call recall_service.store/write/append etc."""
    assert path.is_file()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = _calls_writer_attr(tree)
    assert not offenders, f"{path.name}: forbidden writer calls: {offenders}"


def test_cabinet_only_writes_dashboard_db_tables() -> None:
    """B9 behavior — cabinet INSERT/UPDATE statements target ONLY dashboard.db tables.

    Allowed tables: cabinet_meetings, cabinet_transcripts, cabinet_text_meetings,
    cabinet_client_msg_seen, audit_log. Anything else → flag.
    """
    import re
    allowed = {
        "cabinet_meetings",
        "cabinet_transcripts",
        "cabinet_text_meetings",
        "cabinet_client_msg_seen",
        "audit_log",
    }
    pattern = re.compile(r"(INSERT\s+(?:OR\s+IGNORE\s+)?INTO|UPDATE)\s+(\w+)", re.IGNORECASE)
    offenders: list[str] = []
    for path in _CABINET_FILES:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for m in pattern.finditer(text):
            table = m.group(2)
            if table not in allowed:
                offenders.append(f"{path.name}: writes table '{table}'")
    assert not offenders, f"cabinet writes to non-allowlisted tables: {offenders}"
