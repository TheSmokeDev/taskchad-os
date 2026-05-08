"""PRD-8 Phase 7a (WS4) — security/kill_switches contract tests.

Asserts the operator-toggleable kill-switch contract:
  - is_disabled reads HOMIE_KILLSWITCH_<NAME> env var on every call (Rule 2)
  - requireEnabled raises KillSwitchDisabled when disabled
  - refusal counter increments per switch
  - get_refusal_counters returns a COPY (Rule 2)
  - audit-write failure does NOT block the raise (security action priority)
  - Rule 3 module-attribute imports — no top-level `from security.kill_switches
    import requireEnabled` outside test files
  - Thread safety — concurrent requireEnabled calls produce correct counter
  - Caller-side IMPL — engine + memory_reflect/weekly/dream catch KillSwitchDisabled
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path

import pytest

from security import kill_switches


@pytest.fixture(autouse=True)
def reset_counters():
    """Each test starts with empty counters."""
    kill_switches._REFUSAL_COUNTERS.clear()
    kill_switches._AUDIT_WRITE_FAILURES.clear()
    yield
    kill_switches._REFUSAL_COUNTERS.clear()
    kill_switches._AUDIT_WRITE_FAILURES.clear()


def test_require_enabled_returns_none_when_not_disabled(monkeypatch):
    monkeypatch.delenv("HOMIE_KILLSWITCH_LLM", raising=False)
    # No raise — switch is enabled.
    assert kill_switches.requireEnabled("llm") is None


def test_require_enabled_raises_when_disabled(monkeypatch):
    monkeypatch.setenv("HOMIE_KILLSWITCH_LLM", "disabled")
    with pytest.raises(kill_switches.KillSwitchDisabled) as exc_info:
        kill_switches.requireEnabled("llm")
    assert exc_info.value.switch_name == "llm"


def test_require_enabled_case_insensitive(monkeypatch):
    monkeypatch.setenv("HOMIE_KILLSWITCH_LLM", "DISABLED")
    with pytest.raises(kill_switches.KillSwitchDisabled):
        kill_switches.requireEnabled("llm")


def test_require_enabled_lowercase_switch_name_uppercased(monkeypatch):
    """Switch name is uppercased to form env var key."""
    monkeypatch.setenv("HOMIE_KILLSWITCH_RECALL", "disabled")
    with pytest.raises(kill_switches.KillSwitchDisabled):
        kill_switches.requireEnabled("recall")


def test_refusal_counter_increments_on_raise(monkeypatch):
    monkeypatch.setenv("HOMIE_KILLSWITCH_LLM", "disabled")
    for _ in range(3):
        with pytest.raises(kill_switches.KillSwitchDisabled):
            kill_switches.requireEnabled("llm")
    counters = kill_switches.get_refusal_counters()
    assert counters["llm"] == 3


def test_refusal_counter_per_switch(monkeypatch):
    monkeypatch.setenv("HOMIE_KILLSWITCH_LLM", "disabled")
    monkeypatch.setenv("HOMIE_KILLSWITCH_RECALL", "disabled")
    for _ in range(2):
        with pytest.raises(kill_switches.KillSwitchDisabled):
            kill_switches.requireEnabled("llm")
    with pytest.raises(kill_switches.KillSwitchDisabled):
        kill_switches.requireEnabled("recall")
    counters = kill_switches.get_refusal_counters()
    assert counters["llm"] == 2
    assert counters["recall"] == 1


def test_get_refusal_counters_returns_copy_not_internal(monkeypatch):
    """Rule 2 — caller cannot mutate internal state via the snapshot."""
    monkeypatch.setenv("HOMIE_KILLSWITCH_LLM", "disabled")
    with pytest.raises(kill_switches.KillSwitchDisabled):
        kill_switches.requireEnabled("llm")
    snap = kill_switches.get_refusal_counters()
    snap["llm"] = 99
    snap["bogus"] = 5
    # Internal state unchanged.
    assert kill_switches.get_refusal_counters()["llm"] == 1
    assert "bogus" not in kill_switches.get_refusal_counters()


def test_get_health_snapshot_shape(monkeypatch):
    """Rich snapshot — counters + audit_write_failures + process_started_at."""
    monkeypatch.setenv("HOMIE_KILLSWITCH_LLM", "disabled")
    with pytest.raises(kill_switches.KillSwitchDisabled):
        kill_switches.requireEnabled("llm", caller="test")
    snap = kill_switches.get_health_snapshot()
    assert "counters" in snap
    assert "audit_write_failures" in snap
    assert "process_started_at" in snap
    assert snap["counters"]["llm"] == 1
    assert isinstance(snap["process_started_at"], float)


def test_audit_log_row_written_on_refusal(monkeypatch):
    """Audit write is attempted via dashboard_api._audit_write."""
    monkeypatch.setenv("HOMIE_KILLSWITCH_LLM", "disabled")
    write_calls: list[dict] = []

    def fake_audit_write(**kwargs):
        write_calls.append(kwargs)

    # Late-bind: patch the _audit_write that kill_switches imports lazily.
    import dashboard_api
    monkeypatch.setattr(dashboard_api, "_audit_write", fake_audit_write)

    with pytest.raises(kill_switches.KillSwitchDisabled):
        kill_switches.requireEnabled("llm", caller="testcaller")
    assert len(write_calls) == 1
    assert write_calls[0]["action"] == "killswitch_refusal"
    assert write_calls[0]["target_persona_id"] == "llm"
    assert write_calls[0]["outcome"] == "disabled"
    assert write_calls[0]["blocked"] is True


def test_audit_failure_does_not_block_raise(monkeypatch):
    """If _audit_write raises, kill-switch refusal STILL raises (priority)."""
    monkeypatch.setenv("HOMIE_KILLSWITCH_LLM", "disabled")

    def boom(**kwargs):
        raise RuntimeError("audit write failed")

    import dashboard_api
    monkeypatch.setattr(dashboard_api, "_audit_write", boom)

    # The raise still happens.
    with pytest.raises(kill_switches.KillSwitchDisabled):
        kill_switches.requireEnabled("llm")
    # Audit failure counter incremented.
    snap = kill_switches.get_health_snapshot()
    assert snap["audit_write_failures"]["llm"] >= 1


def test_thread_safety_concurrent_refusals(monkeypatch):
    """Concurrent requireEnabled calls produce correct counter (Lock-protected)."""
    monkeypatch.setenv("HOMIE_KILLSWITCH_LLM", "disabled")
    n_threads = 20
    iterations_per_thread = 5

    def worker():
        for _ in range(iterations_per_thread):
            try:
                kill_switches.requireEnabled("llm")
            except kill_switches.KillSwitchDisabled:
                pass

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    counters = kill_switches.get_refusal_counters()
    assert counters["llm"] == n_threads * iterations_per_thread


# === Rule 3 module-attribute discipline (production grep) ===


def test_rule3_no_top_level_imports_in_production():
    """No `from security.kill_switches import requireEnabled` outside tests.

    Rule 3 — top-level function imports defeat monkeypatch propagation.
    Production code must use `from security import kill_switches` then
    `kill_switches.requireEnabled(...)`. Tests are exempt.

    Uses ast.parse so docstrings and comments are ignored — only real
    `ImportFrom` nodes count.
    """
    import ast

    repo_root = Path(__file__).resolve().parents[3]
    forbidden_callable_names = {
        "requireEnabled",
        "is_disabled",
        "get_refusal_counters",
        "get_health_snapshot",
    }
    # KillSwitchDisabled IS allowed in caller IMPL late-binds (engine.py,
    # memory_*.py) — those use `from security.kill_switches import
    # KillSwitchDisabled` inside an except clause for type-narrowing, which
    # does NOT defeat monkeypatch (the function `requireEnabled` is what
    # gets monkeypatched, not the exception class).

    skip_parts = {
        "__pycache__",
        ".archon",
        "worktrees",
        ".worktrees",
        ".codex-worktrees",
        ".refs",
        "_drafts",
        ".tmp",
        "_archive",
        "_holders",
    }

    offenders: list[str] = []
    for py_file in repo_root.rglob("*.py"):
        if any(part in skip_parts for part in py_file.parts):
            continue
        if py_file.name.startswith("test_") or py_file.name == "conftest.py":
            continue
        # Allow the canonical sources themselves.
        if py_file.name == "kill_switches.py":
            continue
        if py_file.name == "__init__.py" and "security" in py_file.parts:
            continue
        try:
            text = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module not in {"security", "security.kill_switches"}:
                    continue
                for alias in node.names:
                    if alias.name in forbidden_callable_names:
                        offenders.append(
                            f"{py_file}:{node.lineno}: "
                            f"from {node.module} import {alias.name}"
                        )
    assert not offenders, (
        "Rule 3 violation — production code imports kill-switch callables "
        f"directly. Use `from security import kill_switches` instead. "
        f"Offenders: {offenders}"
    )


# === Caller-side IMPL — engine.py / memory_reflect / memory_weekly / memory_dream ===


def test_engine_catches_killswitch_disabled():
    """engine.py has explicit isinstance(e, KillSwitchDisabled) catch (R2 NM2)."""
    engine = Path(__file__).resolve().parents[2] / "chat" / "engine.py"
    text = engine.read_text(encoding="utf-8")
    assert "KillSwitchDisabled" in text
    assert "killswitch:" in text  # documented degraded reply marker


def test_memory_reflect_catches_killswitch_disabled():
    reflect = Path(__file__).resolve().parents[1] / "memory_reflect.py"
    text = reflect.read_text(encoding="utf-8")
    assert "KillSwitchDisabled" in text
    assert "skipped" in text.lower()


def test_memory_weekly_catches_killswitch_disabled():
    weekly = Path(__file__).resolve().parents[1] / "memory_weekly.py"
    text = weekly.read_text(encoding="utf-8")
    assert "KillSwitchDisabled" in text
    assert "skipped" in text.lower()


def test_memory_dream_marks_skipped_killswitch_not_failed():
    dream = Path(__file__).resolve().parents[1] / "memory_dream.py"
    text = dream.read_text(encoding="utf-8")
    assert "skipped_killswitch" in text
    assert "KillSwitchDisabled" in text
