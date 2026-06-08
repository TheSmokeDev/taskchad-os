from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory_flush import build_memory_flush_prompt
from memory_reflect import _assemble_reflect_identity_section
from repository_memory import (
    build_repository_briefing_section,
    validate_repository_memory,
)
from runtime.bootstrap import build_session_briefing


def _write_core_memory(memory_dir: Path, daily_dir: Path) -> None:
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
        "## Active Projects\n\n"
        "- **The Homie** - private runtime work.\n\n"
        "## Global Rules\n\n"
        "- Preserve boundaries.\n\n"
        "## Preferences\n\n"
        "Short updates.\n\n"
        "## Upcoming Events\n\n"
        "- Undated urgent item\n",
        encoding="utf-8",
    )


def _write_repository_memory(memory_dir: Path) -> None:
    pages_dir = memory_dir / "repositories"
    pages_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "REPOSITORIES.md").write_text(
        "# Repository Index\n\n"
        "## Active Repositories\n\n"
        "| Slug | GitHub | Visibility | Default branch | Local path | Archon | Page |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| thehomie | example/thehomie | private | master | "
        "C:\\Repos\\thehomie | yes | "
        "[thehomie](repositories/thehomie.md) |\n\n"
        "## Dispatch Defaults\n\n"
        "- Prefer Archon worktrees for substantive coding work.\n",
        encoding="utf-8",
    )
    (pages_dir / "thehomie.md").write_text(
        "---\n"
        "slug: thehomie\n"
        "github_repo: example/thehomie\n"
        "visibility: private\n"
        "default_branch: master\n"
        f"local_path: {memory_dir.parent}\n"
        "archon_enabled: false\n"
        "---\n"
        "# thehomie\n\n"
        "## Identity\nPrivate runtime.\n\n"
        "## Archon Configuration\nWorktree isolation.\n\n"
        "## Workflow Preferences\nPreserve dirt.\n\n"
        "## Dispatch History\nNone.\n\n"
        "## Recent Activity\nNone.\n\n"
        "## Related\nREPOSITORIES.md\n",
        encoding="utf-8",
    )


def test_session_briefing_includes_repositories_after_projects(tmp_path: Path) -> None:
    memory_dir = tmp_path / "TheHomie" / "Memory"
    daily_dir = memory_dir / "daily"
    _write_core_memory(memory_dir, daily_dir)
    _write_repository_memory(memory_dir)

    briefing = build_session_briefing(memory_dir=memory_dir, daily_dir=daily_dir)

    assert "### Repositories" in briefing
    assert "thehomie" in briefing
    assert "Prefer Archon worktrees" in briefing
    assert briefing.index("### Active Projects") < briefing.index("### Repositories")
    assert briefing.index("### Repositories") < briefing.index("### Urgents")


def test_session_briefing_omits_repositories_when_index_missing(tmp_path: Path) -> None:
    memory_dir = tmp_path / "TheHomie" / "Memory"
    daily_dir = memory_dir / "daily"
    _write_core_memory(memory_dir, daily_dir)

    briefing = build_session_briefing(memory_dir=memory_dir, daily_dir=daily_dir)

    assert "### Repositories" not in briefing


def test_repository_briefing_truncates(tmp_path: Path) -> None:
    memory_dir = tmp_path / "TheHomie" / "Memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "REPOSITORIES.md").write_text(
        "# Repository Index\n\n"
        "## Active Repositories\n\n"
        + "\n".join(f"- repo-{i}: " + ("x" * 80) for i in range(20))
        + "\n\n## Dispatch Defaults\n\n- Prefer worktrees.\n",
        encoding="utf-8",
    )

    section = build_repository_briefing_section(memory_dir, max_chars=220)

    assert section.startswith("### Repositories")
    assert "truncated" in section
    assert len(section) < 320


def test_validate_repository_memory_shape(tmp_path: Path) -> None:
    memory_dir = tmp_path / "TheHomie" / "Memory"
    _write_repository_memory(memory_dir)

    assert validate_repository_memory(memory_dir) == []


def test_memory_flush_prompt_requests_repository_activity_fields() -> None:
    prompt = build_memory_flush_prompt("Worked in thehomie on branch codex/x")

    assert "Repository/codebase activity" in prompt
    assert "repo slug" in prompt
    assert "workflow or dispatch name" in prompt
    assert "branch or worktree path" in prompt
    assert "notable repo-scoped lessons" in prompt


def test_reflection_identity_includes_repositories(tmp_path: Path) -> None:
    memory_dir = tmp_path / "TheHomie" / "Memory"
    daily_dir = memory_dir / "daily"
    _write_core_memory(memory_dir, daily_dir)
    _write_repository_memory(memory_dir)

    section = _assemble_reflect_identity_section(memory_dir)

    assert "## Current REPOSITORIES.md (private repo routing context)" in section
    assert "thehomie" in section
