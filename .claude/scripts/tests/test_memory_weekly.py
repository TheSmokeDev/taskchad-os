"""Tests for memory_weekly.py — weekly synthesis pipeline.

PRD-8 Phase 2 WS3 parity tests: prove that swapping the inline
``load_file_safe(...)`` reads + identity-section assembly to the consolidated
``build_identity_payload()`` shim is a behavior-preserving refactor — same
ordering (MEMORY/GOALS/USER/SOUL/SELF), same headers, byte-identical output
for the identity slice of the synthesis prompt.

Pattern matches the canonical fixture style at
``tests/test_memory_dream.py:25-65`` — seed ``tmp_path / "TheHomie" / "Memory"``,
NEVER read the real ``vault/memory/`` (sanitizer-denied via
``scripts/sanitize.py:32-39``, non-reproducible, may contain private content).

All tests are pure Python — no LLM calls, no network, no real file system
writes beyond ``tmp_path`` fixtures.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure scripts dir is on path (defensive — conftest.py also injects it).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# =============================================================================
# FIXTURE — minimal vault/memory/ tree under tmp_path
# =============================================================================


@pytest.fixture
def weekly_memory_dir(tmp_path: Path) -> Path:
    """Build a deterministic ``<tmp>/vault/memory/`` tree for weekly tests.

    Five identity files (MEMORY, GOALS, USER, SOUL, SELF) — note: weekly's
    assembly order is MEMORY/GOALS/USER/SOUL/SELF, distinct from reflect's
    MEMORY/USER/SOUL/SELF/GOALS. The fixture seeds files with distinct
    sentinel content so byte-equality assertions catch ordering bugs.
    """
    memory_dir = tmp_path / "TheHomie" / "Memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "weekly").mkdir()
    (memory_dir / "daily").mkdir()

    (memory_dir / "MEMORY.md").write_text(
        "# MEMORY\n\n## Decisions\n\n- weekly decision A\n",
        encoding="utf-8",
    )
    (memory_dir / "GOALS.md").write_text(
        "# GOALS\n\n## Q2 2026\n\n- ship phase 2\n- ship phase 3\n",
        encoding="utf-8",
    )
    (memory_dir / "USER.md").write_text(
        "# USER\n\nname: TestUser\n",
        encoding="utf-8",
    )
    (memory_dir / "SOUL.md").write_text(
        "# SOUL\n\ntone: weekly\n",
        encoding="utf-8",
    )
    (memory_dir / "SELF.md").write_text(
        "# SELF\n\n## Patterns\n\n- weekly synthesis pattern\n",
        encoding="utf-8",
    )
    return memory_dir


# =============================================================================
# LEGACY ASSEMBLY — pre-refactor logic, preserved as a private test helper
# =============================================================================


def _legacy_load_file_safe(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def _legacy_load_self_file(self_file: Path) -> str:
    return _legacy_load_file_safe(self_file)


def _legacy_build_weekly_identity_section(memory_dir: Path) -> str:
    """Pre-refactor identity-section assembly for memory_weekly.py.

    Mirrors the prompt body at memory_weekly.py:198-219 verbatim — same
    ordering (MEMORY/GOALS/USER/SOUL/SELF) and same headers. This helper
    is the parity baseline.
    """
    current_memory = _legacy_load_file_safe(memory_dir / "MEMORY.md")
    current_goals = _legacy_load_file_safe(memory_dir / "GOALS.md")
    current_soul = _legacy_load_file_safe(memory_dir / "SOUL.md")
    current_user = _legacy_load_file_safe(memory_dir / "USER.md")
    current_self = _legacy_load_self_file(memory_dir / "SELF.md")

    return f"""## Current MEMORY.md

{current_memory}

## Current GOALS.md

{current_goals}

## Current USER.md

{current_user}

## Current SOUL.md

{current_soul}

## Current SELF.md

{current_self}"""


# F2 post-build fix: production helper IS the test target.
from memory_weekly import _assemble_weekly_identity_section


# =============================================================================
# PARITY TESTS
# =============================================================================


class TestPromptParityWithShim:
    def test_prompt_parity_with_shim(self, weekly_memory_dir: Path) -> None:
        """Legacy + shim-based identity section are byte-identical.

        WS3 acceptance criterion ``memory_weekly_refactor_parity_preserved``
        (verification: ``pytest tests/test_memory_weekly.py::test_prompt_parity_with_shim``).
        """
        legacy = _legacy_build_weekly_identity_section(weekly_memory_dir)
        new = _assemble_weekly_identity_section(weekly_memory_dir)

        assert legacy == new, (
            "Identity-section parity broken between legacy reads + shim. "
            "Refactor introduced a behavior change. Diff first 200 chars:\n"
            f"  legacy[:200]={legacy[:200]!r}\n"
            f"  new[:200]={new[:200]!r}"
        )

    def test_prompt_parity_with_shim_missing_files(self, tmp_path: Path) -> None:
        """Missing identity files → both paths return empty strings, no exception."""
        empty_dir = tmp_path / "TheHomie" / "Memory"
        empty_dir.mkdir(parents=True)

        legacy = _legacy_build_weekly_identity_section(empty_dir)
        new = _assemble_weekly_identity_section(empty_dir)

        assert legacy == new
        # Confirm the order header sequence matches expectations even when empty.
        assert "## Current MEMORY.md\n\n\n\n## Current GOALS.md" in new
        assert "## Current SOUL.md\n\n\n\n## Current SELF.md" in new

    def test_prompt_parity_with_shim_partial_files(self, tmp_path: Path) -> None:
        """Mixed presence (some files exist, some missing) preserves parity."""
        memory_dir = tmp_path / "TheHomie" / "Memory"
        memory_dir.mkdir(parents=True)

        # Only seed GOALS.md and USER.md.
        (memory_dir / "GOALS.md").write_text(
            "# GOALS\nrun X\n", encoding="utf-8"
        )
        (memory_dir / "USER.md").write_text(
            "# USER\nrole: weekly\n", encoding="utf-8"
        )

        legacy = _legacy_build_weekly_identity_section(memory_dir)
        new = _assemble_weekly_identity_section(memory_dir)

        assert legacy == new
        assert "run X" in new
        assert "role: weekly" in new
