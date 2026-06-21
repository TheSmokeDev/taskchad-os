"""Tests for cognition.skills — skill index, writing, patching, validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cognition.skills import (
    ConflictMatch,
    SkillSpec,
    _find_conflict,
    _has_conflict,
    build_skill_index,
    patch_skill,
    validate_skill,
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


def test_build_skill_index_excludes_generated(tmp_path):
    """Default-deny: auto-drafted skills under generated/ are NOT surfaced.

    They are unscanned + ungated, so build_skill_index must keep them out of the
    procedural_memory region until the skill rails promote them out of generated/.
    A hand-authored skill alongside them must still be surfaced.
    """
    gen_dir = tmp_path / "generated" / "test-cat" / "auto-skill"
    gen_dir.mkdir(parents=True)
    (gen_dir / "SKILL.md").write_text(
        "---\nname: auto-skill\ndescription: Auto-generated\ngenerated: true\n---\n",
        encoding="utf-8",
    )
    hand = tmp_path / "hand-skill"
    hand.mkdir()
    (hand / "SKILL.md").write_text(
        "---\nname: hand-skill\ndescription: Hand authored\n---\n",
        encoding="utf-8",
    )
    result = build_skill_index(tmp_path)
    assert "auto-skill" not in result
    assert "hand-skill" in result


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


# === validate_skill tests ===


def test_validate_skill_valid(tmp_path):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text(
        "---\nname: test\ndescription: A test skill\n---\n\n# Body\nContent here.\n",
        encoding="utf-8",
    )
    assert validate_skill(skill_md) == []


def test_validate_skill_missing_file(tmp_path):
    errs = validate_skill(tmp_path / "nope.md")
    assert len(errs) == 1
    assert "not found" in errs[0].lower()


def test_validate_skill_no_frontmatter(tmp_path):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("# Just a heading\nNo frontmatter.\n", encoding="utf-8")
    errs = validate_skill(skill_md)
    assert any("frontmatter" in e.lower() for e in errs)


def test_validate_skill_missing_name(tmp_path):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("---\ndescription: Has desc\n---\n\nBody.\n", encoding="utf-8")
    errs = validate_skill(skill_md)
    assert any("name" in e.lower() for e in errs)


def test_validate_skill_missing_description(tmp_path):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("---\nname: test\n---\n\nBody.\n", encoding="utf-8")
    errs = validate_skill(skill_md)
    assert any("description" in e.lower() for e in errs)


def test_validate_skill_empty_body(tmp_path):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("---\nname: test\ndescription: d\n---\n", encoding="utf-8")
    errs = validate_skill(skill_md)
    assert any("body" in e.lower() for e in errs)


def test_validate_skill_oversized(tmp_path):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text(
        "---\nname: big\ndescription: huge\n---\n\n" + "x" * 30000,
        encoding="utf-8",
    )
    errs = validate_skill(skill_md)
    assert any("large" in e.lower() for e in errs)


# === _find_conflict tests (WS4 / B2) ===


def _write_generated_skill(skills_dir: Path, category: str, name: str) -> Path:
    """Plant a generated draft at skills_dir/generated/<category>/<name>/SKILL.md."""
    d = skills_dir / "generated" / category / name
    d.mkdir(parents=True, exist_ok=True)
    md = d / "SKILL.md"
    md.write_text(
        f"---\nname: {name}\ndescription: auto\ngenerated: true\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return md


def test_find_conflict_returns_none_when_no_match(tmp_path):
    _write_manual_skill(tmp_path, "email-check", "Check inbox")
    spec = SkillSpec(name="calendar-sync", description="d", category="c")
    assert _find_conflict(spec, tmp_path) is None


def test_find_conflict_returns_match_with_name_and_path(tmp_path):
    """A hand-authored collision returns name + path + is_generated=False."""
    _write_manual_skill(tmp_path, "turborater-quote", "ITC quotes")
    spec = SkillSpec(name="quote", description="auto", category="ops")
    match = _find_conflict(spec, tmp_path)
    assert isinstance(match, ConflictMatch)
    assert match.name == "turborater-quote"  # MATCHED skill's name (B2), not spec.name
    assert match.path.name == "SKILL.md"
    assert match.path.parent.name == "turborater-quote"
    assert match.is_generated is False


def test_find_conflict_flags_generated_match(tmp_path):
    """A collision against a generated draft sets is_generated=True (path segment)."""
    _write_generated_skill(tmp_path, "data-queries", "daily-spend-query")
    spec = SkillSpec(name="daily-spend-query", description="auto", category="x")
    match = _find_conflict(spec, tmp_path)
    assert match is not None
    assert match.name == "daily-spend-query"
    assert match.is_generated is True
    # path segment is the source of truth — it lives under generated/
    assert "generated" in match.path.parts


def test_find_conflict_empty_name_returns_none(tmp_path):
    _write_manual_skill(tmp_path, "any-skill", "x")
    spec = SkillSpec(name="", description="d", category="c")
    assert _find_conflict(spec, tmp_path) is None


def test_has_conflict_is_thin_wrapper(tmp_path):
    """_has_conflict must agree with (_find_conflict is not None) — back-compat."""
    _write_manual_skill(tmp_path, "turborater-quote", "ITC quotes")
    spec_hit = SkillSpec(name="quote", description="d", category="c")
    spec_miss = SkillSpec(name="calendar-sync", description="d", category="c")
    assert _has_conflict(spec_hit, tmp_path) == (_find_conflict(spec_hit, tmp_path) is not None)
    assert _has_conflict(spec_miss, tmp_path) == (_find_conflict(spec_miss, tmp_path) is not None)


# === propose_skill recurrence (WS4 / B2) ===


@pytest.fixture
def _sidecar_data_dir(tmp_path, monkeypatch):
    """Point the call-time DATA_DIR resolver at a tmp dir (mirrors WS2 fixture)."""
    import config

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data_dir, raising=False)
    return data_dir


def _patch_reasoning(monkeypatch, parsed: dict) -> None:
    from cognition import steps

    class _FakeResult:
        pass

    fake = _FakeResult()
    fake.parsed = parsed

    async def _fake_reasoning_step(**_kwargs):
        return fake

    monkeypatch.setattr(steps, "reasoning_step", _fake_reasoning_step)


def test_propose_skill_records_recurrence_on_generated_match(
    tmp_path, monkeypatch, _sidecar_data_dir,
):
    """A proposal colliding with a GENERATED draft records recurrence (keyed on
    the matched draft's name) and returns None — recurrence, not a new draft."""
    import asyncio

    from cognition import observability, skill_usage, skills
    from cognition.skills import propose_skill

    skills_dir = tmp_path / "skills"
    _write_generated_skill(skills_dir, "data-queries", "daily-spend-query")

    # Proposal whose token set matches the generated draft.
    _patch_reasoning(monkeypatch, {
        "name": "daily-spend-query",
        "description": "auto gen",
        "category": "data-queries",
    })

    logged: list[observability.SkillLog] = []
    monkeypatch.setattr(observability, "log_skill_event", lambda e: logged.append(e))
    _ = skills  # silence linters

    result = asyncio.run(propose_skill(
        tool_calls=["Read", "Grep", "Bash", "Edit", "Write"],
        session_summary="spend check",
        skills_dir=skills_dir,
        cwd=tmp_path,
    ))

    assert result is None  # recurrence, not a new draft
    # recurrence recorded against the MATCHED draft name in the physical sidecar
    usage = skill_usage.get_usage("daily-spend-query")
    assert usage is not None
    assert usage.recurrence_count == 1
    # a `reused` event was logged, keyed on the matched draft name (B2)
    assert any(e.action == "reused" and e.skill_name == "daily-spend-query" for e in logged)


def test_propose_skill_skips_recurrence_on_manual_match(tmp_path, monkeypatch, _sidecar_data_dir):
    """A proposal colliding with a HAND-authored skill keeps conflict_skipped —
    no recurrence row is written (a hand-authored skill is not a draft)."""
    import asyncio

    from cognition import observability, skill_usage, skills
    from cognition.skills import propose_skill

    skills_dir = tmp_path / "skills"
    _write_manual_skill(skills_dir, "turborater-quote", "ITC quotes")

    _patch_reasoning(monkeypatch, {
        "name": "quote",
        "description": "auto gen",
        "category": "ops",
    })

    logged: list[observability.SkillLog] = []
    monkeypatch.setattr(observability, "log_skill_event", lambda e: logged.append(e))
    _ = skills

    result = asyncio.run(propose_skill(
        tool_calls=["Read", "Grep", "Bash", "Edit", "Write"],
        session_summary="quote",
        skills_dir=skills_dir,
        cwd=tmp_path,
    ))

    assert result is None
    # NO recurrence row for the matched hand-authored skill
    assert skill_usage.get_usage("turborater-quote") is None
    # the event is conflict_skipped (keyed on the PROPOSAL name, existing behavior)
    assert any(e.action == "conflict_skipped" for e in logged)
    assert not any(e.action == "reused" for e in logged)


# === write_skill B4 path-traversal enforcement (WS4) ===


def test_write_skill_rejects_dotdot_category(tmp_path):
    """category='../escaped' must raise — never write outside generated/."""
    spec = SkillSpec(name="x", description="y", category="../escaped")
    with pytest.raises(ValueError):
        write_skill(spec, tmp_path)
    # nothing escaped: no SKILL.md outside generated/
    escaped = list(tmp_path.glob("escaped/**/SKILL.md"))
    assert escaped == []


def test_write_skill_rejects_forward_slash_name(tmp_path):
    spec = SkillSpec(name="a/b", description="y", category="ops")
    with pytest.raises(ValueError):
        write_skill(spec, tmp_path)


def test_write_skill_rejects_backslash_category(tmp_path):
    spec = SkillSpec(name="x", description="y", category="a\\b")
    with pytest.raises(ValueError):
        write_skill(spec, tmp_path)


def test_write_skill_rejects_absolute_name(tmp_path):
    spec = SkillSpec(name="/etc/passwd", description="y", category="ops")
    with pytest.raises(ValueError):
        write_skill(spec, tmp_path)


def test_write_skill_happy_path_stays_under_generated(tmp_path):
    """A clean spec writes under generated/ and the resolved path is contained."""
    spec = SkillSpec(name="clean-name", description="d", category="ops")
    path = write_skill(spec, tmp_path)
    generated_root = (tmp_path / "generated").resolve()
    assert path.resolve().is_relative_to(generated_root)
    assert path.parent.parent.parent.name == "generated"


def test_write_skill_slugs_spaces_in_components(tmp_path):
    """Spaces/uppercase in model-authored components are slugged for the PATH."""
    spec = SkillSpec(name="Daily Spend", description="d", category="Data Queries")
    path = write_skill(spec, tmp_path)
    assert path.parent.name == "daily-spend"
    assert path.parent.parent.name == "data-queries"
    # frontmatter keeps the original display name (only the path is sanitized)
    content = path.read_text(encoding="utf-8")
    assert "name: Daily Spend" in content


# === write_skill F2 YAML field-injection enforcement ===


def test_write_skill_rejects_newline_in_description(tmp_path):
    """F2: a description carrying a newline that forges a frontmatter key must
    raise — never write the injected YAML."""
    spec = SkillSpec(
        name="x", description="line1\nmalicious: true", category="ops",
    )
    with pytest.raises(ValueError):
        write_skill(spec, tmp_path)
    # nothing written: no SKILL.md anywhere under tmp_path
    assert list(tmp_path.rglob("SKILL.md")) == []


def test_write_skill_rejects_newline_in_name(tmp_path):
    """F2: a name with a newline (would forge frontmatter) must raise."""
    spec = SkillSpec(name="x\ngenerated: false", description="d", category="ops")
    with pytest.raises(ValueError):
        write_skill(spec, tmp_path)
    assert list(tmp_path.rglob("SKILL.md")) == []


def test_write_skill_rejects_carriage_return_in_category(tmp_path):
    """F2: a category with a carriage return must raise."""
    spec = SkillSpec(name="x", description="d", category="ops\rinjected: 1")
    with pytest.raises(ValueError):
        write_skill(spec, tmp_path)
    assert list(tmp_path.rglob("SKILL.md")) == []


def test_write_skill_rejects_control_char_in_description(tmp_path):
    """F2: a non-newline C0 control character is also rejected."""
    spec = SkillSpec(name="x", description="bad\x00value", category="ops")
    with pytest.raises(ValueError):
        write_skill(spec, tmp_path)
    assert list(tmp_path.rglob("SKILL.md")) == []


def test_write_skill_allows_clean_multiword_values(tmp_path):
    """F2 is not over-broad: clean spaced values (no control chars) still write,
    and the forged key never appears as a real frontmatter line."""
    spec = SkillSpec(
        name="Daily Spend",
        description="Summarize the day's spend by category.",
        category="Data Queries",
    )
    path = write_skill(spec, tmp_path)
    content = path.read_text(encoding="utf-8")
    assert "name: Daily Spend" in content
    assert "description: Summarize the day's spend by category." in content
