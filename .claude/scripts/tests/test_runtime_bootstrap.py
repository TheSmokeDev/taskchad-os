from __future__ import annotations

from pathlib import Path

from config import now_local
from runtime.bootstrap import build_session_start_context


def test_build_session_start_context_includes_core_memory(tmp_path: Path) -> None:
    memory_dir = tmp_path / "Memory"
    daily_dir = memory_dir / "daily"
    daily_dir.mkdir(parents=True)

    (memory_dir / "BOOTSTRAP.md").write_text("bootstrap", encoding="utf-8")
    (memory_dir / "SOUL.md").write_text("soul", encoding="utf-8")
    (memory_dir / "USER.md").write_text("user", encoding="utf-8")
    (memory_dir / "MEMORY.md").write_text("memory", encoding="utf-8")
    (memory_dir / "GOALS.md").write_text("goals", encoding="utf-8")
    today = now_local().strftime("%Y-%m-%d")
    (daily_dir / f"{today}.md").write_text("line-1\nline-2", encoding="utf-8")

    context = build_session_start_context("startup", memory_dir=memory_dir, daily_dir=daily_dir)

    assert "## BOOTSTRAP" in context
    assert "## Soul" in context
    assert "## User" in context
    assert "## Long-Term Memory" in context
    assert "## Goals" in context
    assert "## Recent Daily Log" in context
    assert "line-2" in context


def test_build_session_start_context_truncates_cleanly(tmp_path: Path) -> None:
    memory_dir = tmp_path / "Memory"
    daily_dir = memory_dir / "daily"
    daily_dir.mkdir(parents=True)

    (memory_dir / "MEMORY.md").write_text("A" * 1000, encoding="utf-8")

    context = build_session_start_context(
        "startup",
        memory_dir=memory_dir,
        daily_dir=daily_dir,
        max_context_chars=120,
    )

    assert len(context) <= 120


def test_no_primo_builder_exported() -> None:
    """Regression: build_primo_identity_context must not exist after identity unification."""
    import runtime.bootstrap as mod
    assert not hasattr(mod, "build_primo_identity_context")
