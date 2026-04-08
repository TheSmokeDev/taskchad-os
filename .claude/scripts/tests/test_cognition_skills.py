"""Tests for cognition.skills — skill index, writing, patching."""

from __future__ import annotations

import json
from pathlib import Path

from cognition.skills import SkillSpec, build_skill_index, patch_skill, write_skill


# === SkillSpec dataclass tests ===


def test_skill_spec_defaults():
    s = SkillSpec(name="test", description="A test", category="cat")
    assert s.version == "1.0.0"
    assert s.tools_used == []
    assert s.trigger_patterns == []
    assert s.workflow_steps == []
    assert s.source_session == ""
    assert s.created_at == ""


def test_skill_spec_custom():
    s = SkillSpec(
        name="email-check",
        description="Check inbox",
        category="data-queries",
        tools_used=["Read", "Bash"],
        trigger_patterns=["check email"],
    )
    assert s.name == "email-check"
    assert len(s.tools_used) == 2


# === build_skill_index tests ===


def test_build_skill_index_empty(tmp_path):
    assert build_skill_index(tmp_path) == ""


def test_build_skill_index_nonexistent():
    assert build_skill_index(Path("/nonexistent/path")) == ""


def test_build_skill_index_with_skills(tmp_path):
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: A test skill\n---\n\n# Test\n",
        encoding="utf-8",
    )
    result = build_skill_index(tmp_path)
    assert "test-skill" in result
    assert "A test skill" in result


def test_build_skill_index_multiple(tmp_path):
    for i in range(3):
        d = tmp_path / f"skill-{i}"
        d.mkdir()
        (d / "SKILL.md").write_text(
            f"---\nname: skill-{i}\ndescription: Skill number {i}\n---\n",
            encoding="utf-8",
        )
    result = build_skill_index(tmp_path)
    assert result.count("- **") == 3


def test_build_skill_index_max_cap(tmp_path):
    for i in range(25):
        d = tmp_path / f"skill-{i:02d}"
        d.mkdir()
        (d / "SKILL.md").write_text(
            f"---\nname: skill-{i:02d}\ndescription: Desc {i}\n---\n",
            encoding="utf-8",
        )
    result = build_skill_index(tmp_path, max_entries=5)
    assert result.count("- **") == 5


def test_build_skill_index_malformed_skip(tmp_path):
    """Malformed SKILL.md files are skipped gracefully."""
    d = tmp_path / "bad"
    d.mkdir()
    (d / "SKILL.md").write_text("no frontmatter here", encoding="utf-8")
    d2 = tmp_path / "good"
    d2.mkdir()
    (d2 / "SKILL.md").write_text(
        "---\nname: good\ndescription: Works fine\n---\n", encoding="utf-8"
    )
    result = build_skill_index(tmp_path)
    assert "good" in result
    assert result.count("- **") == 1


def test_build_skill_index_scans_generated(tmp_path):
    """Index scans both top-level and generated/ subdirectory."""
    gen_dir = tmp_path / "generated" / "test-cat" / "auto-skill"
    gen_dir.mkdir(parents=True)
    (gen_dir / "SKILL.md").write_text(
        "---\nname: auto-skill\ndescription: Auto-generated\ngenerated: true\n---\n",
        encoding="utf-8",
    )
    result = build_skill_index(tmp_path)
    assert "auto-skill" in result


# === write_skill tests ===


def test_write_skill_creates_file(tmp_path):
    spec = SkillSpec(
        name="test-skill",
        description="A test",
        category="test-cat",
        tools_used=["Read", "Bash"],
        workflow_steps=["Step 1", "Step 2"],
    )
    path = write_skill(spec, tmp_path)
    assert path.exists()
    assert path.name == "SKILL.md"
    assert path.parent.name == "test-skill"
    assert path.parent.parent.name == "test-cat"
    assert path.parent.parent.parent.name == "generated"


def test_write_skill_content(tmp_path):
    spec = SkillSpec(
        name="my-skill",
        description="Does things",
        category="ops",
        version="2.0.0",
        tools_used=["Grep"],
        workflow_steps=["Find files", "Process them"],
    )
    path = write_skill(spec, tmp_path)
    content = path.read_text(encoding="utf-8")
    assert "name: my-skill" in content
    assert "generated: true" in content
    assert "version: 2.0.0" in content
    assert "1. Find files" in content
    assert "- Grep" in content


def test_write_skill_tools_json(tmp_path):
    spec = SkillSpec(
        name="x", description="y", category="z",
        tools_used=["A", "B"],
    )
    path = write_skill(spec, tmp_path)
    content = path.read_text(encoding="utf-8")
    assert json.dumps(["A", "B"]) in content


# === patch_skill tests ===


def test_patch_skill_generated(tmp_path):
    spec = SkillSpec(name="patchable", description="Old desc", category="cat")
    path = write_skill(spec, tmp_path)
    ok = patch_skill(path, {"version": "2.0.0"})
    assert ok is True
    content = path.read_text(encoding="utf-8")
    assert "version: 2.0.0" in content


def test_patch_skill_manual_rejected(tmp_path):
    """Only patches generated skills."""
    manual = tmp_path / "manual" / "SKILL.md"
    manual.parent.mkdir(parents=True)
    manual.write_text(
        "---\nname: manual\ndescription: Hand-made\n---\n", encoding="utf-8"
    )
    ok = patch_skill(manual, {"version": "9.0.0"})
    assert ok is False


def test_patch_skill_nonexistent(tmp_path):
    ok = patch_skill(tmp_path / "nope.md", {"version": "1.0"})
    assert ok is False
