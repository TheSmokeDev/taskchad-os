"""Tests for cognition.skills — skill index, writing, patching."""

from __future__ import annotations

import json
from pathlib import Path

from cognition.skills import (
    SkillSpec,
    _has_conflict,
    build_skill_index,
    patch_skill,
    write_skill,
)


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


# === _has_conflict tests ===


def _write_manual_skill(skills_dir: Path, name: str, description: str) -> None:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n",
        encoding="utf-8",
    )


def test_conflict_exact_name_match(tmp_path):
    _write_manual_skill(tmp_path, "turborater-quote", "ITC TurboRater quotes")
    spec = SkillSpec(name="turborater-quote", description="auto gen", category="ops")
    assert _has_conflict(spec, tmp_path) is True


def test_conflict_substring_match(tmp_path):
    _write_manual_skill(tmp_path, "email-check-inbox", "Check inbox")
    # Proposed name is a substring of existing → conflict
    spec = SkillSpec(name="email-check", description="auto gen", category="data")
    assert _has_conflict(spec, tmp_path) is True


def test_no_conflict_allows_generation(tmp_path):
    _write_manual_skill(tmp_path, "email-check", "Check inbox")
    spec = SkillSpec(name="calendar-sync", description="sync cal", category="data")
    assert _has_conflict(spec, tmp_path) is False


def test_no_conflict_on_empty_skills_dir(tmp_path):
    spec = SkillSpec(name="whatever", description="d", category="c")
    assert _has_conflict(spec, tmp_path) is False


def test_no_conflict_on_empty_name(tmp_path):
    _write_manual_skill(tmp_path, "any-skill", "x")
    spec = SkillSpec(name="", description="d", category="c")
    assert _has_conflict(spec, tmp_path) is False


# === Token-set conflict regression tests (Codex P2 findings) ===


def test_conflict_token_set_email_family_no_collision(tmp_path):
    """{email, inbox} is not a subset of {email, check} — legit sibling skills."""
    _write_manual_skill(tmp_path, "email-check", "Check inbox status")
    spec = SkillSpec(name="email-inbox", description="List inbox", category="data")
    assert _has_conflict(spec, tmp_path) is False


def test_conflict_token_set_quote_shadows_turborater(tmp_path):
    """{quote} IS a subset of {turborater, quote} — proposed would shadow."""
    _write_manual_skill(tmp_path, "turborater-quote", "ITC TurboRater quotes")
    spec = SkillSpec(name="quote", description="auto gen", category="ops")
    assert _has_conflict(spec, tmp_path) is True


def test_conflict_scans_beyond_50_skills(tmp_path):
    """Guard must walk every SKILL.md — not a rendered-index cap."""
    for i in range(60):
        _write_manual_skill(tmp_path, f"manual-skill-{i:02d}", f"Skill {i}")
    # Skill #55 matches proposed via token-set subset
    spec = SkillSpec(
        name="manual-skill-55", description="auto gen", category="ops",
    )
    assert _has_conflict(spec, tmp_path) is True


def test_conflict_matches_skill_without_description(tmp_path):
    """SKILL.md missing `description:` field must still block collisions."""
    skill_dir = tmp_path / "legacy-skill"
    skill_dir.mkdir()
    # No description field at all — older manual skills sometimes omit it
    (skill_dir / "SKILL.md").write_text(
        "---\nname: legacy-skill\n---\n\n# Legacy\n",
        encoding="utf-8",
    )
    spec = SkillSpec(name="legacy-skill", description="auto gen", category="ops")
    assert _has_conflict(spec, tmp_path) is True


def test_propose_skill_logs_conflict_skipped(tmp_path, monkeypatch):
    """Colliding proposal returns None AND logs action=conflict_skipped."""
    import asyncio

    from cognition import observability, skills, steps
    from cognition.skills import propose_skill

    _write_manual_skill(tmp_path, "turborater-quote", "ITC TurboRater quotes")

    class _FakeResult:
        parsed = {
            "name": "turborater",
            "description": "auto gen",
            "category": "ops",
        }

    async def _fake_reasoning_step(**_kwargs):
        return _FakeResult()

    logged: list[observability.SkillLog] = []

    def _fake_log(event):
        logged.append(event)

    monkeypatch.setattr(steps, "reasoning_step", _fake_reasoning_step)
    monkeypatch.setattr(observability, "log_skill_event", _fake_log)
    # skills.py does `from cognition.steps import reasoning_step` inside fn;
    # that lookup resolves at call time via sys.modules, so patching the
    # module attribute is sufficient.
    _ = skills  # silence unused-import warnings from linters

    result = asyncio.run(propose_skill(
        tool_calls=["Read", "Grep", "Bash", "Edit", "Write"],
        session_summary="test session",
        skills_dir=tmp_path,
        cwd=tmp_path,
    ))

    assert result is None
    assert len(logged) == 1
    assert logged[0].action == "conflict_skipped"
    assert logged[0].skill_name == "turborater"
