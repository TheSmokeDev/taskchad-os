"""Smoke tests for Repositories System SessionStart injection.

Verifies that build_repository_briefing_section() and
build_repository_config_briefing() produce correct output and degrade
gracefully when sources are missing or empty.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repository_memory import build_repository_briefing_section


VALID_REPOS_MD = (
    "# Repository Index\n\n"
    "## Active Repositories\n\n"
    "| Slug | GitHub | Visibility | Default branch | Local path | Archon | Page |\n"
    "| --- | --- | --- | --- | --- | --- | --- |\n"
    "| thehomie | example/thehomie | private | master | C:\\Repos\\sb | yes | "
    "[thehomie](repositories/thehomie.md) |\n"
    "| YourBusiness | example/YourBusiness | private | main | C:\\Repos\\qm | yes | "
    "[YourBusiness](repositories/YourBusiness.md) |\n\n"
    "## Dispatch Defaults\n\n"
    "- Prefer Archon worktrees for substantive coding work.\n"
    "- Work in-session for trivial edits or planning.\n"
)


def _make_repos_md(memory_dir: Path, content: str = VALID_REPOS_MD) -> None:
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "REPOSITORIES.md").write_text(content, encoding="utf-8")


def test_briefing_contains_repositories_heading(tmp_path: Path) -> None:
    memory_dir = tmp_path / "Memory"
    _make_repos_md(memory_dir)
    section = build_repository_briefing_section(memory_dir)
    assert section.startswith("### Repositories")


def test_briefing_contains_repo_slug(tmp_path: Path) -> None:
    memory_dir = tmp_path / "Memory"
    _make_repos_md(memory_dir)
    section = build_repository_briefing_section(memory_dir)
    assert "thehomie" in section
    assert "YourBusiness" in section


def test_briefing_contains_dispatch_defaults(tmp_path: Path) -> None:
    memory_dir = tmp_path / "Memory"
    _make_repos_md(memory_dir)
    section = build_repository_briefing_section(memory_dir)
    assert "Dispatch defaults:" in section
    assert "Prefer Archon worktrees" in section


def test_missing_repositories_md_returns_empty(tmp_path: Path) -> None:
    memory_dir = tmp_path / "Memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    section = build_repository_briefing_section(memory_dir)
    assert section == ""


def test_empty_repositories_md_returns_empty(tmp_path: Path) -> None:
    memory_dir = tmp_path / "Memory"
    _make_repos_md(memory_dir, content="")
    section = build_repository_briefing_section(memory_dir)
    assert section == ""


def test_whitespace_only_repositories_md_returns_empty(tmp_path: Path) -> None:
    memory_dir = tmp_path / "Memory"
    _make_repos_md(memory_dir, content="   \n  \n  ")
    section = build_repository_briefing_section(memory_dir)
    assert section == ""


def test_config_briefing_with_valid_config(tmp_path: Path, monkeypatch: object) -> None:
    from repository_config import (
        RepositoryConfigReport,
        RepositoryItem,
        build_repository_config_briefing,
        load_repository_config,
    )

    fake_report = RepositoryConfigReport(
        profile="main",
        config_path=tmp_path / "config.yaml",
        config_exists=True,
        enabled=True,
        items=(
            RepositoryItem(
                slug="thehomie",
                github_repo="example/thehomie",
                default_branch="master",
                local_path=str(tmp_path),
                archon_enabled=True,
                dispatch_mode="archon-preferred",
            ),
        ),
    )
    monkeypatch.setattr(
        "repository_config.load_repository_config",
        lambda: fake_report,
    )
    briefing = build_repository_config_briefing()
    assert "### Configured Repositories" in briefing
    assert "thehomie" in briefing


def test_config_briefing_empty_when_disabled(tmp_path: Path, monkeypatch: object) -> None:
    from repository_config import (
        RepositoryConfigReport,
        build_repository_config_briefing,
    )

    fake_report = RepositoryConfigReport(
        profile="main",
        config_path=tmp_path / "config.yaml",
        config_exists=True,
        enabled=False,
    )
    monkeypatch.setattr(
        "repository_config.load_repository_config",
        lambda: fake_report,
    )
    briefing = build_repository_config_briefing()
    assert briefing == ""
