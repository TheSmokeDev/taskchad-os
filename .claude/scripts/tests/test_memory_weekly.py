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


# =============================================================================
# REGRESSION — MEMORY_DIR must stay a module global inside _run_weekly_inner
# =============================================================================
#
# 2026-06-29 .. 2026-09-10: every Sunday synthesis died before writing a note.
# A ``from config import MEMORY_DIR`` sat inside ``_run_weekly_inner``'s body
# (in the Move-5a emergent-connections block, ~400 lines BELOW first use).
# Python binds names per-function at COMPILE time, so that single late import
# made MEMORY_DIR a function-local for the entire body — and the earlier reads
# at the recall seam and the identity-section seam raised
# ``UnboundLocalError: cannot access local variable 'MEMORY_DIR'``.
# Ten weekly notes (2026-W27..W36) were never written. Same bug class as
# Rule 1 (config bound at the wrong time), but via scoping rather than
# default args.


class TestMemoryDirScoping:
    """Guard the UnboundLocalError that silently killed 10 weekly synthesis runs."""

    def test_memory_dir_is_not_a_function_local(self) -> None:
        """Structural guard: a late in-body import would re-introduce the bug.

        Fails before the fix — the compiler lists MEMORY_DIR in co_varnames
        the moment ANY ``from config import MEMORY_DIR`` appears in the body.
        """
        import memory_weekly

        code = memory_weekly._run_weekly_inner.__code__
        assert "MEMORY_DIR" not in code.co_varnames, (
            "MEMORY_DIR is a function-local of _run_weekly_inner — an in-body "
            "`from config import MEMORY_DIR` re-introduced the UnboundLocalError "
            "that killed weekly synthesis for 10 straight weeks."
        )
        assert "MEMORY_DIR" in code.co_names, (
            "MEMORY_DIR should resolve as a module global inside _run_weekly_inner"
        )

    def test_inner_reaches_identity_assembly_without_unbound_local(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Behavioral guard: drive the real body past both MEMORY_DIR seams.

        Before the fix this raised UnboundLocalError at the recall seam and
        again at the identity seam, so the sentinel was never reached.
        """
        import asyncio
        import types

        import memory_weekly

        # Keep the (slow, real) recall seam out of the test: inject a stub
        # recall_service so the in-body `from recall_service import recall`
        # resolves to a no-op. The seam itself still executes and still
        # dereferences MEMORY_DIR — which is the point.
        class _StubResponse:
            formatted_text = ""

        async def _stub_recall(**_kwargs: object) -> _StubResponse:
            return _StubResponse()

        stub_module = types.ModuleType("recall_service")
        stub_module.recall = _stub_recall  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "recall_service", stub_module)

        monkeypatch.setattr(
            memory_weekly,
            "get_weekly_logs",
            lambda days=7: [("2026-09-01", "seeded log body")],
        )

        class _ReachedIdentitySeam(Exception):
            """Unique sentinel proving execution got past the MEMORY_DIR reads."""

        def _explode(memory_dir: Path) -> str:
            raise _ReachedIdentitySeam(str(memory_dir))

        monkeypatch.setattr(
            memory_weekly, "_assemble_weekly_identity_section", _explode
        )

        with pytest.raises(_ReachedIdentitySeam):
            asyncio.run(memory_weekly._run_weekly_inner(test_mode=True, days=7))
