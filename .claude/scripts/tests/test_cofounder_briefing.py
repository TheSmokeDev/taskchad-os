"""US-019 - session injection + reflection routing.

Path map (one test per distinct path):

build_cofounder_briefing_section:
  - projects present: section renders one line per project (slug + status +
    iterations + job), the orchestrator-only rule, and the index-doc pointer
  - empty projects dir -> "" (no briefing noise)
  - missing projects dir -> ""
  - malformed project file is skipped by discover_projects' fail-open
    boundary; the good project still renders
  - every file malformed -> "" (discover returns nothing)
  - catastrophic reader failure (discover_projects raises) -> "" (the
    builder's own broad-except fail-open)
  - overlong list truncates with a pointer to the index doc

bootstrap seam (runtime/bootstrap.py):
  - build_session_briefing includes the section, positioned after the
    repositories briefing (ONE builder feeds every surface)
  - no projects dir -> section absent from the briefing
  - a raising briefing builder never breaks bootstrap (guarded lazy import)

reflection routing (memory_reflect.py):
  - _assemble_reflect_repo_routing_section carries the co-founder bullet
    (cofounder dir + repo: frontmatter + Dispatch History) AND the original
    Archon dispatch bullet
  - the helper is wired into the production reflection prompt
    (_run_reflection_inner source consumes it - not a dead helper)
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import memory_reflect
from cofounder import project_model
from cofounder.briefing import (
    INDEX_DOC_NAME,
    ORCHESTRATOR_ONLY_RULE,
    build_cofounder_briefing_section,
)
from runtime.bootstrap import build_session_briefing

SECTION_HEADER = "### Co-Founder Projects"


def make_project(
    projects_dir: Path,
    slug: str,
    *,
    status: str = "new",
    iterations: int = 0,
    current_job_id: str | None = None,
) -> Path:
    projects_dir.mkdir(parents=True, exist_ok=True)
    job_line = f"current_job_id: {current_job_id}\n" if current_job_id else ""
    text = (
        "---\n"
        "tags: [system, cofounder]\n"
        f"status: {status}\n"
        "created: 2026-07-04T00:00:00\n"
        f"iterations: {iterations}\n"
        "max_iterations: 50\n"
        f"{job_line}"
        "---\n"
        f"# {slug}\n\n"
        "## Spec (STATIC - orchestrator MUST NOT rewrite; only the operator edits)\n"
        f"Build {slug}.\n\n"
        "## Plan / Working Memory (MUTABLE - orchestrator may rewrite)\n"
        f"- [ ] plan {slug}\n\n"
        "## Activity Log (APPEND-ONLY - newest at the bottom)\n"
        "- 2026-07-04T00:00:00 created\n"
    )
    path = projects_dir / f"{slug}.md"
    path.write_text(text, encoding="utf-8")
    return path


def _write_core_memory(memory_dir: Path, daily_dir: Path) -> None:
    """Minimum identity files so build_session_briefing takes the structured
    path (identity + capabilities + rules present) instead of the full dump."""
    memory_dir.mkdir(parents=True, exist_ok=True)
    daily_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "SOUL.md").write_text(
        "# SOUL\n\n## Core Identity\nAgent.\n\n## Core Values\nDirect.\n",
        encoding="utf-8",
    )
    (memory_dir / "SELF.md").write_text(
        "# SELF\n\n## Capabilities\nRuntime.\n\n## Patterns\nSmall diffs.\n",
        encoding="utf-8",
    )
    (memory_dir / "USER.md").write_text(
        "# USER\n\n## Profile\nOperator.\n\n## Preferences\nDirect.\n",
        encoding="utf-8",
    )
    (memory_dir / "MEMORY.md").write_text(
        "# MEMORY\n\n"
        "## Active Projects\n\n- **The Homie** - private runtime work.\n\n"
        "## Global Rules\n\n- Preserve boundaries.\n\n"
        "## Preferences\n\nShort updates.\n",
        encoding="utf-8",
    )


def _write_repository_index(memory_dir: Path) -> None:
    (memory_dir / "REPOSITORIES.md").write_text(
        "# Repository Index\n\n"
        "## Active Repositories\n\n- thehomie\n\n"
        "## Dispatch Defaults\n\n- Prefer Archon worktrees.\n",
        encoding="utf-8",
    )


# =============================================================================
# build_cofounder_briefing_section
# =============================================================================


def test_briefing_renders_active_projects(tmp_path: Path) -> None:
    memory_dir = tmp_path / "Memory"
    pdir = memory_dir / "cofounder"
    make_project(pdir, "alpha", status="building", iterations=3, current_job_id="abc123")
    make_project(pdir, "beta", status="awaiting-human")

    section = build_cofounder_briefing_section(memory_dir)

    assert section.startswith(SECTION_HEADER)
    assert "- **alpha** - building (iterations 3/50, job abc123)" in section
    assert "- **beta** - awaiting-human (iterations 0/50, job none)" in section
    assert ORCHESTRATOR_ONLY_RULE in section
    assert INDEX_DOC_NAME in section


def test_briefing_empty_projects_dir_is_empty_string(tmp_path: Path) -> None:
    memory_dir = tmp_path / "Memory"
    (memory_dir / "cofounder").mkdir(parents=True)

    assert build_cofounder_briefing_section(memory_dir) == ""


def test_briefing_missing_projects_dir_is_empty_string(tmp_path: Path) -> None:
    memory_dir = tmp_path / "Memory"
    memory_dir.mkdir(parents=True)

    assert build_cofounder_briefing_section(memory_dir) == ""


def test_briefing_skips_malformed_project_keeps_good(tmp_path: Path) -> None:
    memory_dir = tmp_path / "Memory"
    pdir = memory_dir / "cofounder"
    make_project(pdir, "good", status="testing")
    (pdir / "broken.md").write_text(
        "---\ntags: [system\nstatus: :::\n---\nno sections\n", encoding="utf-8"
    )

    section = build_cofounder_briefing_section(memory_dir)

    assert "- **good** - testing" in section
    assert "broken" not in section


def test_briefing_all_malformed_is_empty_string(tmp_path: Path) -> None:
    memory_dir = tmp_path / "Memory"
    pdir = memory_dir / "cofounder"
    pdir.mkdir(parents=True)
    (pdir / "broken.md").write_text("no frontmatter at all\n", encoding="utf-8")

    assert build_cofounder_briefing_section(memory_dir) == ""


def test_briefing_fails_open_when_reader_raises(tmp_path: Path, monkeypatch) -> None:
    memory_dir = tmp_path / "Memory"
    make_project(memory_dir / "cofounder", "alpha")

    def _boom(_dir):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(project_model, "discover_projects", _boom)

    assert build_cofounder_briefing_section(memory_dir) == ""


def test_briefing_truncates_overlong_list(tmp_path: Path) -> None:
    memory_dir = tmp_path / "Memory"
    pdir = memory_dir / "cofounder"
    for i in range(12):
        make_project(pdir, f"project-with-a-fairly-long-slug-{i:02d}")

    section = build_cofounder_briefing_section(memory_dir, max_chars=200)

    assert section.startswith(SECTION_HEADER)
    assert "truncated" in section
    assert INDEX_DOC_NAME in section
    # The rule survives truncation (it is appended after the capped body).
    assert ORCHESTRATOR_ONLY_RULE in section


# =============================================================================
# bootstrap seam
# =============================================================================


def test_session_briefing_includes_cofounder_after_repositories(tmp_path: Path) -> None:
    memory_dir = tmp_path / "TheHomie" / "Memory"
    daily_dir = memory_dir / "daily"
    _write_core_memory(memory_dir, daily_dir)
    _write_repository_index(memory_dir)
    make_project(memory_dir / "cofounder", "alpha", status="building")

    briefing = build_session_briefing(memory_dir=memory_dir, daily_dir=daily_dir)

    assert SECTION_HEADER in briefing
    assert "- **alpha** - building" in briefing
    assert briefing.index("### Repositories") < briefing.index(SECTION_HEADER)


def test_session_briefing_omits_cofounder_when_no_projects(tmp_path: Path) -> None:
    memory_dir = tmp_path / "TheHomie" / "Memory"
    daily_dir = memory_dir / "daily"
    _write_core_memory(memory_dir, daily_dir)

    briefing = build_session_briefing(memory_dir=memory_dir, daily_dir=daily_dir)

    assert SECTION_HEADER not in briefing


def test_session_briefing_survives_raising_briefing_builder(
    tmp_path: Path, monkeypatch
) -> None:
    """The bootstrap wrapper is fail-open: a briefing crash costs the section,
    never the session briefing (the never-breaks-bootstrap AC)."""
    memory_dir = tmp_path / "TheHomie" / "Memory"
    daily_dir = memory_dir / "daily"
    _write_core_memory(memory_dir, daily_dir)
    make_project(memory_dir / "cofounder", "alpha")

    import cofounder.briefing as briefing_mod

    def _boom(_memory_dir, **_kwargs):
        raise RuntimeError("briefing exploded")

    monkeypatch.setattr(briefing_mod, "build_cofounder_briefing_section", _boom)

    briefing = build_session_briefing(memory_dir=memory_dir, daily_dir=daily_dir)

    assert briefing.startswith("## The Homie")
    assert SECTION_HEADER not in briefing


# =============================================================================
# reflection routing
# =============================================================================


def test_reflection_routing_covers_cofounder_activity() -> None:
    routing = memory_reflect._assemble_reflect_repo_routing_section()

    # New co-founder bullet: activity routes to the owning repo page's
    # Dispatch History, repo resolved from the project file's frontmatter.
    assert "co-founder" in routing
    assert "cofounder" in routing
    assert "`repo:` frontmatter" in routing
    # The original Archon routing bullet is untouched.
    assert "Append Archon/Codex dispatches" in routing
    assert routing.count("`## Dispatch History`") == 2


def test_reflection_prompt_consumes_routing_helper() -> None:
    """Wiring lock: the production prompt assembles section 5 through the
    helper (a dead helper with a green content test proves nothing)."""
    source = inspect.getsource(memory_reflect._run_reflection_inner)

    assert "_assemble_reflect_repo_routing_section()" in source
    # The inline block is gone; the helper is the single source of truth.
    assert "Append Archon/Codex dispatches" not in source
