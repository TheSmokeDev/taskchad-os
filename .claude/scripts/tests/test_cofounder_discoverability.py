"""US-018 — discoverability: index doc refresh + always-loaded references.

Path map (one test per distinct path):

_refresh_index_doc (run_pass):
  - non-dry pass + index doc present: the Active Projects section body is
    replaced with the current list + refresh stamp; every byte outside the
    section is preserved
  - the list reflects POST-pass disk state (Rule 2): a project archived this
    pass vanishes from the list now, not next pass
  - --test (dry run) skips the refresh: index doc bytes + mtime untouched
  - missing index doc: quiet skip, pass completes, nothing created
  - index doc without the Active Projects section: fail-open, doc unchanged
  - index doc without frontmatter: fail-open, doc unchanged

vault_lint frontmatter validation:
  - cofounder/ project files (incl. done/) are excluded: zero findings
  - control: the SAME file outside cofounder/ IS flagged (the check runs —
    Testing Principle: a pass that never exercises the target proves nothing)

Shipped artifacts (repo-level registration locks, US-015 precedent):
  - vault/memory/COFOUNDER-PROJECTS.md carries every required element
  - the shipped index doc is refresh-compatible (frontmatter + section
    resolvable by the very helpers the refresher uses)
  - the shipped project template parses as a valid project when activated,
    and its underscore name is discovery-skipped
  - CLAUDE.md carries the Co-Founder Projects section
  - vault/memory/MEMORY.md Reference carries the pointer line
"""

from __future__ import annotations

from pathlib import Path

import pytest

import config
from cofounder import notify as cofounder_notify
from cofounder import orchestrate as cofounder_orchestrate
from cofounder import project_model
from cofounder.run_pass import (
    INDEX_ACTIVE_PROJECTS_SECTION,
    INDEX_DOC_NAME,
    OUTCOME_COMPLETED,
    PROJECT_ARCHIVED,
    run_pass,
)
from orchestration import observability
from security import kill_switches
from vault_lint import check_frontmatter_validation

REPO_ROOT = Path(__file__).resolve().parents[3]

COFOUNDER_ENV_KEYS = (
    "COFOUNDER_ENABLED",
    "COFOUNDER_PROJECTS_DIR",
    "COFOUNDER_MAX_ITERATIONS",
    "COFOUNDER_MAX_WALL_CLOCK_HOURS",
    "COFOUNDER_MAX_CONCURRENT",
    "COFOUNDER_NOTIFY_LEVELS",
    "COFOUNDER_ZOMBIE_STALE_MINUTES",
    "COFOUNDER_ARCHON_DB",
    "COFOUNDER_WORKFLOW_PROVIDER",
    "COFOUNDER_WORKFLOW_MODEL",
)

INDEX_TEMPLATE = """---
tags: [system, cofounder]
status: current
date: 2026-07-04
---
# Co-Founder Projects

## The Orchestrator-Only Rule

Operator-owned prose the refresher must never touch.

## Active Projects

_No active projects._

## Pointers

- PRD: `.archon/ralph/autonomous-cofounder/prd.md`
"""


@pytest.fixture(autouse=True)
def clear_cofounder_env(monkeypatch, tmp_path):
    """No COFOUNDER_*/kill-switch env leakage; Langfuse pinned OFF (the
    cofounder_pass span costs ~4s of OTEL retries against a dead server when
    the operator .env says otherwise); the default-wired Telegram notify is
    stubbed so no test can ever reach real HTTP (live creds ride the .env)."""
    for key in COFOUNDER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("HOMIE_KILLSWITCH_COFOUNDER", raising=False)
    monkeypatch.setenv("LANGFUSE_ENABLED", "false")
    monkeypatch.setattr(observability, "_OBS_LOG", tmp_path / "obs" / "obs.jsonl")
    monkeypatch.setattr(
        cofounder_notify, "notify", lambda project, text, level: False
    )
    # US-020 wired orchestrate.decide as the run_pass default decider; pin it
    # back to None so no discoverability test can ever reach a live LLM.
    monkeypatch.setattr(cofounder_orchestrate, "decide", None)
    yield


@pytest.fixture(autouse=True)
def reset_counters():
    kill_switches._REFUSAL_COUNTERS.clear()
    kill_switches._AUDIT_WRITE_FAILURES.clear()
    yield
    kill_switches._REFUSAL_COUNTERS.clear()
    kill_switches._AUDIT_WRITE_FAILURES.clear()


@pytest.fixture()
def projects_dir(tmp_path):
    pdir = tmp_path / "cofounder"
    pdir.mkdir()
    return pdir


def make_project(directory: Path, slug: str, *, status: str = "new") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    text = (
        "---\n"
        "tags: [system, cofounder]\n"
        f"status: {status}\n"
        "created: 2026-07-04T00:00:00\n"
        "---\n"
        f"# {slug}\n\n"
        "## Spec (STATIC - orchestrator MUST NOT rewrite; only the operator edits)\n"
        f"Build {slug}.\n\n"
        "## Plan / Working Memory (MUTABLE - orchestrator may rewrite)\n"
        f"- [ ] plan {slug}\n\n"
        "## Activity Log (APPEND-ONLY - newest at the bottom)\n"
        "- 2026-07-04T00:00:00 created\n"
    )
    path = directory / f"{slug}.md"
    path.write_text(text, encoding="utf-8")
    return path


def write_index_doc(tmp_path: Path, content: str = INDEX_TEMPLATE) -> Path:
    path = tmp_path / INDEX_DOC_NAME
    path.write_text(content, encoding="utf-8")
    return path


def enabled_settings(projects_dir: Path):
    return config.get_cofounder_settings(enabled=True, projects_dir=projects_dir)


# === index refresh: run_pass paths ===


def test_index_refresh_writes_active_projects_list(tmp_path, projects_dir):
    index = write_index_doc(tmp_path)
    make_project(projects_dir, "alpha")

    result = run_pass(
        settings=enabled_settings(projects_dir), state_file=tmp_path / "state.json"
    )

    assert result.outcome == OUTCOME_COMPLETED
    content = index.read_text(encoding="utf-8")
    assert "- **alpha** - new (iterations 0/50, job none)" in content
    assert "_Auto-refreshed by the co-founder pass at " in content
    assert "_No active projects._" not in content
    # Every byte outside the Active Projects section is preserved.
    head = INDEX_TEMPLATE.split("## Active Projects", 1)[0] + "## Active Projects\n"
    assert content.startswith(head)
    assert (
        content.split("## Pointers", 1)[1]
        == INDEX_TEMPLATE.split("## Pointers", 1)[1]
    )


def test_index_refresh_reflects_post_pass_disk_state(tmp_path, projects_dir):
    """Rule 2: a project archived THIS pass is gone from the list already."""
    index = write_index_doc(tmp_path)
    make_project(projects_dir, "shipped", status="done")

    result = run_pass(
        settings=enabled_settings(projects_dir), state_file=tmp_path / "state.json"
    )

    assert result.project_outcomes["shipped"] == PROJECT_ARCHIVED
    content = index.read_text(encoding="utf-8")
    active_section = content.split("## Active Projects", 1)[1].split("## Pointers", 1)[0]
    assert "shipped" not in active_section
    assert "_No active projects._" in active_section
    assert "_Auto-refreshed by the co-founder pass at " in active_section


def test_dry_run_skips_index_refresh(tmp_path, projects_dir):
    index = write_index_doc(tmp_path)
    make_project(projects_dir, "alpha")
    before_bytes = index.read_text(encoding="utf-8")
    before_mtime = index.stat().st_mtime_ns

    result = run_pass(
        dry_run=True,
        settings=enabled_settings(projects_dir),
        state_file=tmp_path / "state.json",
    )

    assert result.outcome == OUTCOME_COMPLETED
    assert index.read_text(encoding="utf-8") == before_bytes
    assert index.stat().st_mtime_ns == before_mtime


def test_missing_index_doc_is_a_quiet_skip(tmp_path, projects_dir):
    make_project(projects_dir, "alpha")

    result = run_pass(
        settings=enabled_settings(projects_dir), state_file=tmp_path / "state.json"
    )

    assert result.outcome == OUTCOME_COMPLETED
    assert not (tmp_path / INDEX_DOC_NAME).exists()


def test_index_doc_without_section_fails_open(tmp_path, projects_dir):
    broken = (
        "---\ntags: [system, cofounder]\ndate: 2026-07-04\n---\n"
        "# Doc\n\n## Something Else\n\ntext\n"
    )
    index = write_index_doc(tmp_path, broken)
    make_project(projects_dir, "alpha")

    result = run_pass(
        settings=enabled_settings(projects_dir), state_file=tmp_path / "state.json"
    )

    assert result.outcome == OUTCOME_COMPLETED
    assert index.read_text(encoding="utf-8") == broken


def test_index_doc_without_frontmatter_fails_open(tmp_path, projects_dir):
    broken = "# Doc\n\n## Active Projects\n\nstale\n"
    index = write_index_doc(tmp_path, broken)
    make_project(projects_dir, "alpha")

    result = run_pass(
        settings=enabled_settings(projects_dir), state_file=tmp_path / "state.json"
    )

    assert result.outcome == OUTCOME_COMPLETED
    assert index.read_text(encoding="utf-8") == broken


# === vault_lint: frontmatter validation exclusion ===


def test_vault_lint_frontmatter_clean_on_cofounder_project(tmp_path):
    make_project(tmp_path / "cofounder", "alpha")
    make_project(tmp_path / "cofounder" / "done", "shipped", status="done")

    issues = check_frontmatter_validation(tmp_path)

    assert issues == []


def test_vault_lint_exclusion_is_directory_scoped(tmp_path):
    """Control: the same file OUTSIDE cofounder/ is still flagged — proof the
    clean test above exercised the check, not a schema change."""
    make_project(tmp_path, "alpha")

    issues = check_frontmatter_validation(tmp_path)

    assert any(i.file == "alpha.md" and "date" in i.message for i in issues)


# === shipped artifacts: repo-level locks ===


def _shipped_index_doc() -> str:
    return (REPO_ROOT / "TheHomie" / "Memory" / INDEX_DOC_NAME).read_text(
        encoding="utf-8"
    )


def test_shipped_index_doc_carries_every_required_element():
    doc = _shipped_index_doc()
    assert "## Active Projects" in doc
    # Ownership table
    for token in ("STATIC", "MUTABLE", "APPEND-ONLY"):
        assert token in doc
    # Status enum
    assert "`new | building | testing | blocked | awaiting-human | done`" in doc
    # Orchestrator-only rule + worked example
    assert "INSTRUCTION to" in doc
    assert "## How To Update A Project Doc" in doc
    # Pointers: the PRD and one live project file
    assert ".archon/ralph/autonomous-cofounder/prd.md" in doc
    assert "cofounder/_template.md" in doc


def test_shipped_index_doc_is_refresh_compatible():
    """The refresher's own helpers must resolve the shipped doc — otherwise
    the auto-refresh silently fail-opens forever."""
    doc = _shipped_index_doc()
    head, body = project_model._split_raw(doc)
    start, end = project_model._section_span(body, INDEX_ACTIVE_PROJECTS_SECTION)
    assert 0 <= start <= end


def test_shipped_template_parses_when_activated(tmp_path):
    template = REPO_ROOT / "TheHomie" / "Memory" / "cofounder" / "_template.md"
    target = tmp_path / "first-project.md"
    target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")

    project = project_model.parse_project_file(target)

    assert project.frontmatter.status == "new"
    assert project.frontmatter.repo == "greenfield"
    assert project.spec and project.plan and project.activity_log


def test_shipped_template_name_is_discovery_skipped(tmp_path, projects_dir):
    template = REPO_ROOT / "TheHomie" / "Memory" / "cofounder" / "_template.md"
    (projects_dir / "_template.md").write_text(
        template.read_text(encoding="utf-8"), encoding="utf-8"
    )

    assert project_model.discover_projects(projects_dir) == []


def test_claude_md_carries_the_cofounder_section():
    text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    assert "## Co-Founder Projects" in text
    assert "COFOUNDER-PROJECTS.md" in text


def test_memory_md_reference_carries_the_pointer_line():
    text = (REPO_ROOT / "TheHomie" / "Memory" / "MEMORY.md").read_text(
        encoding="utf-8"
    )
    assert "COFOUNDER-PROJECTS.md" in text
