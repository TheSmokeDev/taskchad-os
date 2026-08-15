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


def test_build_skill_index_allowlist_filters_central_skills(tmp_path):
    for name in ("sales-skill", "social-skill"):
        d = tmp_path / name
        d.mkdir()
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} desc\n---\n",
            encoding="utf-8",
        )

    result = build_skill_index(tmp_path, allowlist={"sales-skill"})

    assert "sales-skill" in result
    assert "social-skill" not in result


def test_build_skill_index_max_cap_reserves_a_linked_local_skill(tmp_path):
    """M1 (#429 round-2 MAJOR): a persona-local (explicitly linked) skill must
    survive the cap even when the central pool alone already fills it and the
    linked skill's name sorts alphabetically LAST — the old behavior sorted
    the COMBINED pool then capped, which could silently drop exactly the
    skill ``skill_intake`` just told the operator was "live on the homie's
    next turn"."""
    central = tmp_path / "central"
    profile = tmp_path / "profile"
    central.mkdir()
    profile.mkdir()
    for i in range(5):
        d = central / f"skill-{i:02d}"
        d.mkdir()
        (d / "SKILL.md").write_text(
            f"---\nname: skill-{i:02d}\ndescription: Desc {i}\n---\n",
            encoding="utf-8",
        )
    (profile / "zz-linked-skill").mkdir()
    (profile / "zz-linked-skill" / "SKILL.md").write_text(
        "---\nname: zz-linked-skill\ndescription: Just linked\n---\n",
        encoding="utf-8",
    )

    result = build_skill_index(
        central, max_entries=3, extra_skill_dirs=[profile]
    )

    assert "zz-linked-skill" in result
    # The cap still bounds the TOTAL: 1 reserved local + 2 of the 5 central.
    assert result.count("- **") == 3


def test_build_skill_index_cap_binds_even_when_local_skills_alone_exceed_it(tmp_path):
    """#429 codex R3 MINOR: the persona-local reservation is PRIORITY, not an
    exemption — 21 persona-local skills under ``max_entries=20`` used to emit
    21 entries because the local names were unioned in after ``remaining`` hit
    zero. The cap bounds the TOTAL across local + central."""
    central = tmp_path / "central"
    profile = tmp_path / "profile"
    central.mkdir()
    profile.mkdir()
    for i in range(21):
        d = profile / f"local-{i:02d}"
        d.mkdir()
        (d / "SKILL.md").write_text(
            f"---\nname: local-{i:02d}\ndescription: Local {i}\n---\n",
            encoding="utf-8",
        )
    (central / "central-skill").mkdir()
    (central / "central-skill" / "SKILL.md").write_text(
        "---\nname: central-skill\ndescription: Central desc\n---\n",
        encoding="utf-8",
    )

    result = build_skill_index(central, max_entries=20, extra_skill_dirs=[profile])

    # The cap holds even when the local set alone overflows it.
    assert result.count("- **") == 20
    # Locals keep priority — the whole budget goes to them, none to central...
    assert "central-skill" not in result
    # ...and the overflow local (sorts last) is the one dropped.
    assert "local-20" not in result
    assert "local-00" in result


def test_build_skill_index_includes_profile_local_extra_skills(tmp_path):
    central = tmp_path / "central"
    profile = tmp_path / "profile"
    central.mkdir()
    profile.mkdir()
    (central / "central-skill").mkdir()
    (central / "central-skill" / "SKILL.md").write_text(
        "---\nname: central-skill\ndescription: Central desc\n---\n",
        encoding="utf-8",
    )
    (profile / "local-skill").mkdir()
    (profile / "local-skill" / "SKILL.md").write_text(
        "---\nname: local-skill\ndescription: Local desc\n---\n",
        encoding="utf-8",
    )

    result = build_skill_index(
        central,
        allowlist={"missing-central-skill"},
        extra_skill_dirs=[profile],
    )

    assert "central-skill" not in result
    assert "local-skill" in result


# === persona-scoped central promotion (#429 round-2 BLOCKER 3) ===
#
# A skill promoted through linked-skill intake physically lands in the
# SHARED central `promoted/` tree (nothing else vets a skill). The `default`
# profile is the ONE profile whose allowlist is unrestricted (`None`), so it
# reads that whole shared pool unfiltered — a skill scoped to `sales` was
# visible to `default` the instant it was promoted. `skill_usage`'s
# `assigned_personas` records that scope; `build_skill_index`'s central scan
# must honor it, and a persona's own `extra_skill_dirs` copy must NEVER be
# affected by it (that scan is already scoped by construction).


def _write_central_skill(central: Path, name: str) -> None:
    d = central / name
    d.mkdir()
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} desc\n---\n", encoding="utf-8",
    )


def test_build_skill_index_excludes_a_persona_scoped_skill_from_unrestricted_scan(
    tmp_path, monkeypatch
):
    import config
    from cognition import skill_usage

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data", raising=False)
    central = tmp_path / "central"
    central.mkdir()
    # Scope rows only exist for skills that came through the promotion gate —
    # Rule 2: the promoted/ path is what makes a row enforceable (#429 codex
    # R4: hand-authored central skills are never scope-gated, so the shadowing
    # half of the finding can't recur).
    _write_promoted_skill(central, "sales-only-skill")

    skill_usage.record_recurrence(
        "sales-only-skill", path=str(central / "promoted" / "sales-only-skill")
    )
    skill_usage.record_persona_assignment("sales-only-skill", "sales")

    # `default`'s scan is exactly this shape: no allowlist, no extra dirs
    # (engine.py:_build_profile_skill_index).
    result = build_skill_index(central, allowlist=None)
    assert "sales-only-skill" not in result


def test_build_skill_index_includes_a_skill_explicitly_assigned_to_default(
    tmp_path, monkeypatch
):
    import config
    from cognition import skill_usage

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data", raising=False)
    central = tmp_path / "central"
    central.mkdir()
    _write_central_skill(central, "shared-skill")

    skill_usage.record_recurrence("shared-skill", path=str(central / "shared-skill"))
    skill_usage.record_persona_assignment("shared-skill", "sales")
    skill_usage.record_persona_assignment("shared-skill", "default")

    result = build_skill_index(central, allowlist=None)
    assert "shared-skill" in result


def test_a_named_persona_reader_only_sees_its_own_scoped_promoted_skills(
    tmp_path, monkeypatch
):
    """#429 codex R4 BLOCKER: scoping used to apply only when the allowlist was
    None, so every NAMED persona (concrete allowlist) skipped it — a skill
    scoped to sales leaked into marketing's index whenever marketing's
    allowlist happened to name it. The gate is keyed on the READER now: the
    allowlist decides what a persona may ask for, the scope row decides whose
    it is.

    Non-vacuity: on the pre-fix code the marketing assertion PASSES (the
    leak), so this test fails there.
    """
    import config
    from cognition import skill_usage

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data", raising=False)
    central = tmp_path / "central"
    central.mkdir()
    _write_promoted_skill(central, "sales-only-skill")

    skill_usage.record_recurrence(
        "sales-only-skill", path=str(central / "promoted" / "sales-only-skill")
    )
    skill_usage.record_persona_assignment("sales-only-skill", "sales")

    # marketing's allowlist names the skill — that alone must NOT reveal it.
    marketing = build_skill_index(
        central, allowlist={"sales-only-skill"}, reader_persona="marketing"
    )
    assert "sales-only-skill" not in marketing
    # sales — the persona the skill was linked for — sees its own skill.
    sales = build_skill_index(
        central, allowlist={"sales-only-skill"}, reader_persona="sales"
    )
    assert "sales-only-skill" in sales
    # The unrestricted default scan stays fenced as well.
    default = build_skill_index(central, allowlist=None)
    assert "sales-only-skill" not in default


def test_a_hand_authored_skill_is_never_shadowed_by_a_same_name_scope_row(
    tmp_path, monkeypatch
):
    """#429 codex R4 BLOCKER (second half): the scope map is name-keyed, so a
    row belonging to a promoted persona-scoped skill used to hide an UNRELATED
    hand-authored central skill that merely shares the name. A hand-authored
    skill never went through the promotion gate, so no scope row can restrict
    it (Rule 2 — the path is what says a skill came through the gate)."""
    import config
    from cognition import skill_usage

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data", raising=False)
    central = tmp_path / "central"
    central.mkdir()
    _write_central_skill(central, "research")  # hand-authored twin
    _write_promoted_skill(central, "research")  # promoted twin, scoped to sales

    skill_usage.record_recurrence("research", path=str(central / "promoted" / "research"))
    skill_usage.record_persona_assignment("research", "sales")

    # default sees the hand-authored skill (never gated); the promoted twin
    # stays hidden. The index de-dupes by name, so exactly one entry survives.
    result = build_skill_index(central, allowlist=None)
    assert "research" in result


def test_build_skill_index_scoping_never_touches_a_personas_own_extra_dir(
    tmp_path, monkeypatch
):
    """A persona's own installed copy (`extra_skill_dirs`) must stay visible
    to that persona even though its sidecar row scopes it to a DIFFERENT
    persona name — the marker only gates the unrestricted CENTRAL scan."""
    import config
    from cognition import skill_usage

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data", raising=False)
    central = tmp_path / "central"
    profile = tmp_path / "profile"
    central.mkdir()
    profile.mkdir()
    _write_central_skill(central, "sales-only-skill")
    (profile / "sales-only-skill").mkdir()
    (profile / "sales-only-skill" / "SKILL.md").write_text(
        "---\nname: sales-only-skill\ndescription: sales-only-skill desc\n---\n",
        encoding="utf-8",
    )

    skill_usage.record_recurrence("sales-only-skill", path=str(central / "sales-only-skill"))
    skill_usage.record_persona_assignment("sales-only-skill", "sales")

    # Scanned as "sales" would scan itself: concrete allowlist over central
    # (excludes it there — sales's own allowlist never named it) + its own
    # extra dir (must include it).
    result = build_skill_index(
        central, allowlist=frozenset(), extra_skill_dirs=[profile]
    )
    assert "sales-only-skill" in result


# === scoping fails CLOSED (#429 design gate B2) ===
#
# The mechanism above is only worth what its FAILURE modes are worth. A skill
# that came through the promotion gate lives under `promoted/` (Rule 2 — the
# path is what says it was promoted), so the unrestricted reader must be able
# to point at a positive permission before indexing one. Missing permission —
# an unreadable sidecar, or a promoted skill with no row at all — hides the
# skill rather than exposing it to a homie nobody named.


def _write_promoted_skill(central: Path, name: str) -> None:
    d = central / "promoted" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} desc\n---\n", encoding="utf-8",
    )


def test_build_skill_index_hides_a_promoted_skill_with_no_scope_row(
    tmp_path, monkeypatch
):
    """No row means no recorded permission. Under the fixed write ordering a
    scope is committed BEFORE the artifact is published, so a promoted skill
    with no row is an anomaly — and the safe reading of an anomaly is "not
    mine to show"."""
    import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data", raising=False)
    central = tmp_path / "central"
    central.mkdir()
    _write_promoted_skill(central, "orphan-skill")

    assert "orphan-skill" not in build_skill_index(central, allowlist=None)


def test_build_skill_index_hides_promoted_skills_when_the_sidecar_is_unreadable(
    tmp_path, monkeypatch
):
    """Seam 3: one blanket ``except: return {}`` used to unscope EVERY
    persona-scoped skill at once. A read error now hides what it cannot vouch
    for — and only that: a hand-authored central skill never went through the
    promotion gate, so it is unaffected."""
    import config
    from cognition import skill_usage

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data", raising=False)
    central = tmp_path / "central"
    central.mkdir()
    _write_promoted_skill(central, "scoped-skill")
    _write_central_skill(central, "hand-authored-skill")

    skill_usage.record_recurrence("scoped-skill")
    skill_usage.record_persona_assignment("scoped-skill", "sales")

    def _boom(*_a, **_k):
        raise OSError("sidecar locked")

    monkeypatch.setattr(skill_usage, "list_all_usage", _boom)

    result = build_skill_index(central, allowlist=None)
    assert "scoped-skill" not in result
    assert "hand-authored-skill" in result


def test_build_skill_index_includes_a_promoted_skill_marked_unrestricted(
    tmp_path, monkeypatch
):
    """The positive marker a global ``/skills promote`` stamps."""
    import config
    from cognition import skill_usage

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data", raising=False)
    central = tmp_path / "central"
    central.mkdir()
    _write_promoted_skill(central, "global-skill")

    skill_usage.record_recurrence("global-skill")
    skill_usage.mark_scope_unrestricted("global-skill")

    assert "global-skill" in build_skill_index(central, allowlist=None)


def test_build_skill_index_treats_a_pre_sentinel_promoted_row_as_unrestricted(
    tmp_path, monkeypatch
):
    """The one migration read: a row promoted before the sentinel existed
    carries an empty scope, which means "never persona-scoped" — those stay
    visible instead of vanishing from the main homie on upgrade."""
    import config
    from cognition import skill_usage

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data", raising=False)
    central = tmp_path / "central"
    central.mkdir()
    _write_promoted_skill(central, "legacy-skill")

    skill_usage.record_recurrence("legacy-skill")  # row exists, scope empty
    assert skill_usage.get_usage("legacy-skill").assigned_personas == []

    assert "legacy-skill" in build_skill_index(central, allowlist=None)


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
