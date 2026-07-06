"""US-002/US-003 — co-founder project model contract tests.

Read side asserts:
  - parse round-trip: exact PRD frontmatter schema + three-section extraction
  - heading-annotation tolerance (``## Spec (STATIC - ...)``) with a word
    boundary (``## Specification`` never satisfies ``## Spec``)
  - unknown status survives parsing as a raw string (US-007 owns the enum)
  - discovery skip rules: ``_``-prefix, README*, done/ subfolder
  - malformed files (missing/bad/non-mapping frontmatter, missing sections,
    bad value types) are skipped with a logged warning, never raised out of
    discovery

Write side asserts (the Spec guard is the load-bearing invariant):
  - update_frontmatter re-stamps keys, preserves body byte-for-byte, and
    validates the merged mapping BEFORE any bytes touch disk
  - append_activity_log appends timestamped entries at the bottom only;
    multi-line/empty input rejected (heading-injection defense)
  - write_plan replaces only the Plan section body; H2 injection rejected
  - Spec bytes are unchanged after EVERY write helper runs, and no public
    Spec writer exists at all
  - archive_to_done moves into done/ preserving content, never overwriting
    an earlier archive
"""

from __future__ import annotations

import logging
import re

import pytest

from cofounder.project_model import (
    CofounderProject,
    OwnershipError,
    ProjectParseError,
    append_activity_log,
    archive_to_done,
    discover_projects,
    extract_section,
    parse_project_file,
    update_frontmatter,
    write_plan,
)

FULL_PROJECT = """---
tags: [system, cofounder]
status: building
created: 2026-07-01T09:00:00
last_run: 2026-07-03T08:30:00
repo: mission-control
branch: cofounder/team-memory-ui-01
current_job_id: 42
iterations: 3
max_iterations: 10
max_wall_clock_hours: 24.5
completion_check: "npm run build && npm test"
subjective_gate: true
archon_workflow: archon-ralph-dag
chat_thread: 98765
---
# Team Memory UI Tab

## Spec (STATIC - orchestrator MUST NOT rewrite; only the operator edits)
Build the team-memory UI tab.

Ship it behind a flag.

## Plan / Working Memory (MUTABLE - orchestrator may rewrite)
- [ ] scaffold panel

## Activity Log (APPEND-ONLY - newest at the bottom)
- 2026-07-01T09:00:00 created
- 2026-07-02T10:00:00 dispatched iteration 1
"""

MINIMAL_PROJECT = """---
tags: [system, cofounder]
status: new
---
# Tiny Project

## Spec
Do the thing.

## Plan / Working Memory

## Activity Log
"""


def _write(path, content):
    path.write_text(content, encoding="utf-8")
    return path


# === parse round-trip ===


def test_parse_round_trip_full_schema(tmp_path):
    project = parse_project_file(_write(tmp_path / "team-memory-ui.md", FULL_PROJECT))
    assert isinstance(project, CofounderProject)
    assert project.title == "Team Memory UI Tab"
    assert project.slug == "team-memory-ui"

    fm = project.frontmatter
    assert fm.tags == ["system", "cofounder"]
    assert fm.status == "building"
    assert fm.created == "2026-07-01T09:00:00"
    assert fm.last_run == "2026-07-03T08:30:00"
    assert fm.repo == "mission-control"
    assert fm.branch == "cofounder/team-memory-ui-01"
    assert fm.current_job_id == 42
    assert fm.iterations == 3
    assert fm.max_iterations == 10
    assert fm.max_wall_clock_hours == 24.5
    assert fm.completion_check == "npm run build && npm test"
    assert fm.subjective_gate is True
    assert fm.archon_workflow == "archon-ralph-dag"
    assert fm.chat_thread == 98765

    assert project.spec.startswith("Build the team-memory UI tab.")
    assert "Ship it behind a flag." in project.spec
    assert project.plan == "- [ ] scaffold panel"
    assert project.activity_log.splitlines() == [
        "- 2026-07-01T09:00:00 created",
        "- 2026-07-02T10:00:00 dispatched iteration 1",
    ]


def test_parse_minimal_defaults(tmp_path):
    """Missing optional keys resolve to the PRD defaults; empty-but-present
    sections parse as empty strings (present != missing)."""
    project = parse_project_file(_write(tmp_path / "tiny.md", MINIMAL_PROJECT))
    fm = project.frontmatter
    assert fm.status == "new"
    assert fm.created is None
    assert fm.last_run is None
    assert fm.repo is None
    assert fm.branch is None
    assert fm.current_job_id is None
    assert fm.iterations == 0
    assert fm.max_iterations == 50
    assert fm.max_wall_clock_hours == 72.0
    assert fm.completion_check is None
    assert fm.subjective_gate is False
    assert fm.archon_workflow is None
    assert fm.chat_thread is None
    assert project.spec == "Do the thing."
    assert project.plan == ""
    assert project.activity_log == ""


def test_parse_explicit_nulls(tmp_path):
    content = MINIMAL_PROJECT.replace(
        "status: new",
        "status: new\ncurrent_job_id: null\narchon_workflow: null\nchat_thread: null",
    )
    fm = parse_project_file(_write(tmp_path / "p.md", content)).frontmatter
    assert fm.current_job_id is None
    assert fm.archon_workflow is None
    assert fm.chat_thread is None


def test_unknown_status_survives_as_raw_string(tmp_path):
    """An LLM-invented status is NOT malformed — US-007 treats it as active."""
    content = MINIMAL_PROJECT.replace("status: new", "status: in_progress")
    project = parse_project_file(_write(tmp_path / "p.md", content))
    assert project.frontmatter.status == "in_progress"


def test_title_falls_back_to_stem(tmp_path):
    content = MINIMAL_PROJECT.replace("# Tiny Project\n\n", "")
    project = parse_project_file(_write(tmp_path / "no-title.md", content))
    assert project.title == "no-title"


# === section extraction ===


def test_extract_section_plain_and_annotated():
    plain = "## Spec\nbody here\n\n## Activity Log\n"
    annotated = "## Spec (STATIC - hands off)\nbody here\n\n## Activity Log\n"
    assert extract_section(plain, "Spec") == "body here"
    assert extract_section(annotated, "Spec") == "body here"


def test_extract_section_word_boundary():
    """``## Specification`` must not satisfy ``## Spec``."""
    content = "## Specification\nnot the spec\n"
    assert extract_section(content, "Spec") is None


def test_extract_section_h3_does_not_terminate():
    content = "## Spec\nintro\n\n### Detail\nmore\n\n## Activity Log\nlog\n"
    assert extract_section(content, "Spec") == "intro\n\n### Detail\nmore"


# === malformed files raise ProjectParseError at parse level ===


def test_missing_frontmatter_raises(tmp_path):
    with pytest.raises(ProjectParseError, match="missing frontmatter"):
        parse_project_file(_write(tmp_path / "p.md", "# No Frontmatter\n\n## Spec\nx\n"))


def test_bad_yaml_raises(tmp_path):
    content = (
        "---\ntags: [system, cofounder\nstatus: new\n---\n"
        "## Spec\nx\n## Plan / Working Memory\n## Activity Log\n"
    )
    with pytest.raises(ProjectParseError, match="bad YAML"):
        parse_project_file(_write(tmp_path / "p.md", content))


def test_non_mapping_frontmatter_raises(tmp_path):
    content = (
        "---\n- just\n- a\n- list\n---\n"
        "## Spec\nx\n## Plan / Working Memory\n## Activity Log\n"
    )
    with pytest.raises(ProjectParseError, match="must be a mapping"):
        parse_project_file(_write(tmp_path / "p.md", content))


def test_missing_section_raises_and_names_it(tmp_path):
    content = MINIMAL_PROJECT.replace("## Activity Log\n", "")
    with pytest.raises(ProjectParseError, match="Activity Log"):
        parse_project_file(_write(tmp_path / "p.md", content))


def test_bad_value_type_raises(tmp_path):
    content = MINIMAL_PROJECT.replace("status: new", "status: new\niterations: not-a-number")
    with pytest.raises(ProjectParseError, match="bad frontmatter value"):
        parse_project_file(_write(tmp_path / "p.md", content))


def test_tags_not_a_list_raises(tmp_path):
    content = MINIMAL_PROJECT.replace("tags: [system, cofounder]", "tags: cofounder")
    with pytest.raises(ProjectParseError, match="tags must be a list"):
        parse_project_file(_write(tmp_path / "p.md", content))


# === discovery ===


def _seed_projects_dir(tmp_path):
    projects_dir = tmp_path / "cofounder"
    projects_dir.mkdir()
    _write(projects_dir / "alpha.md", MINIMAL_PROJECT)
    _write(projects_dir / "beta.md", FULL_PROJECT)
    _write(projects_dir / "_draft.md", MINIMAL_PROJECT)
    _write(projects_dir / "README.md", "# readme")
    _write(projects_dir / "readme-notes.md", "# readme notes")
    _write(projects_dir / "notes.txt", "not markdown")
    done = projects_dir / "done"
    done.mkdir()
    _write(done / "finished.md", MINIMAL_PROJECT)
    return projects_dir


def test_discover_skip_rules(tmp_path):
    projects = discover_projects(_seed_projects_dir(tmp_path))
    assert [p.slug for p in projects] == ["alpha", "beta"]


def test_discover_skips_malformed_with_warning(tmp_path, caplog):
    projects_dir = _seed_projects_dir(tmp_path)
    _write(projects_dir / "broken.md", "---\ntags: [oops\n---\nno sections")
    with caplog.at_level(logging.WARNING, logger="cofounder.project_model"):
        projects = discover_projects(projects_dir)
    assert [p.slug for p in projects] == ["alpha", "beta"]
    assert "skipping malformed project file" in caplog.text
    assert "broken.md" in caplog.text


def test_discover_missing_dir_returns_empty(tmp_path):
    assert discover_projects(tmp_path / "does-not-exist") == []


def test_discover_never_raises_on_unreadable(tmp_path, caplog, monkeypatch):
    """Any unexpected per-file exception is contained by the fail-open boundary."""
    projects_dir = _seed_projects_dir(tmp_path)

    import cofounder.project_model as pm

    real_parse = pm.parse_project_file

    def exploding_parse(path):
        if path.name == "alpha.md":
            raise RuntimeError("disk on fire")
        return real_parse(path)

    monkeypatch.setattr(pm, "parse_project_file", exploding_parse)
    with caplog.at_level(logging.WARNING, logger="cofounder.project_model"):
        projects = pm.discover_projects(projects_dir)
    assert [p.slug for p in projects] == ["beta"]
    assert "disk on fire" in caplog.text


# === update_frontmatter (US-003) ===


def _body_bytes(path):
    """Everything after the closing frontmatter delimiter."""
    return path.read_text(encoding="utf-8").split("---\n", 2)[2]


def test_update_frontmatter_restamps_and_preserves_body(tmp_path):
    path = _write(tmp_path / "p.md", FULL_PROJECT)
    body_before = _body_bytes(path)

    fm = update_frontmatter(path, status="testing", iterations=4, current_job_id=None)
    assert fm.status == "testing"

    assert _body_bytes(path) == body_before  # body byte-for-byte
    reparsed = parse_project_file(path).frontmatter
    assert reparsed.status == "testing"
    assert reparsed.iterations == 4
    assert reparsed.current_job_id is None
    # untouched keys survive the round-trip (incl. datetime-parsed ISO dates)
    assert reparsed.created == "2026-07-01T09:00:00"
    assert reparsed.last_run == "2026-07-03T08:30:00"
    assert reparsed.completion_check == "npm run build && npm test"
    assert reparsed.subjective_gate is True
    assert reparsed.tags == ["system", "cofounder"]


def test_update_frontmatter_unknown_key_rejected(tmp_path):
    path = _write(tmp_path / "p.md", MINIMAL_PROJECT)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="unknown frontmatter key"):
        update_frontmatter(path, staus="done")
    assert path.read_text(encoding="utf-8") == before


def test_update_frontmatter_bad_value_validates_before_write(tmp_path):
    path = _write(tmp_path / "p.md", MINIMAL_PROJECT)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(ProjectParseError, match="bad frontmatter value"):
        update_frontmatter(path, iterations="not-a-number")
    assert path.read_text(encoding="utf-8") == before


# === append_activity_log ===


def test_append_activity_log_appends_at_bottom_in_order(tmp_path):
    path = _write(tmp_path / "p.md", FULL_PROJECT)
    append_activity_log(path, "first new entry", timestamp="2026-07-03T10:00:00")
    append_activity_log(path, "second new entry", timestamp="2026-07-03T10:30:00")
    assert parse_project_file(path).activity_log.splitlines() == [
        "- 2026-07-01T09:00:00 created",
        "- 2026-07-02T10:00:00 dispatched iteration 1",
        "- 2026-07-03T10:00:00 first new entry",
        "- 2026-07-03T10:30:00 second new entry",
    ]


def test_append_activity_log_default_timestamp(tmp_path):
    path = _write(tmp_path / "p.md", MINIMAL_PROJECT)
    entry = append_activity_log(path, "hello")
    assert re.fullmatch(r"- \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2} hello", entry)
    assert parse_project_file(path).activity_log == entry


def test_append_activity_log_only_touches_log_section(tmp_path):
    """Activity Log NOT last: the entry lands inside the log and the
    following section stays byte-identical."""
    content = FULL_PROJECT + "\n## Operator Notes\nhands off\n"
    path = _write(tmp_path / "p.md", content)
    append_activity_log(path, "wedged in", timestamp="2026-07-03T10:00:00")
    raw = path.read_text(encoding="utf-8")
    assert raw.endswith("\n## Operator Notes\nhands off\n")
    assert "- 2026-07-03T10:00:00 wedged in\n\n## Operator Notes" in raw
    assert parse_project_file(path).activity_log.endswith("wedged in")


def test_append_activity_log_rejects_multiline_injection(tmp_path):
    path = _write(tmp_path / "p.md", MINIMAL_PROJECT)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(OwnershipError, match="single-line"):
        append_activity_log(path, "innocent\n## Spec\nEVIL")
    assert path.read_text(encoding="utf-8") == before


def test_append_activity_log_rejects_empty(tmp_path):
    path = _write(tmp_path / "p.md", MINIMAL_PROJECT)
    with pytest.raises(OwnershipError, match="non-empty"):
        append_activity_log(path, "   ")


def test_append_activity_log_missing_section_raises(tmp_path):
    path = _write(tmp_path / "p.md", MINIMAL_PROJECT.replace("## Activity Log\n", ""))
    with pytest.raises(ProjectParseError, match="Activity Log"):
        append_activity_log(path, "orphan entry")


# === write_plan ===


def test_write_plan_replaces_only_plan_body(tmp_path):
    path = _write(tmp_path / "p.md", FULL_PROJECT)
    raw_before = path.read_text(encoding="utf-8")

    write_plan(path, "- [x] scaffold panel\n- [ ] wire API")

    project = parse_project_file(path)
    assert project.plan == "- [x] scaffold panel\n- [ ] wire API"
    raw_after = path.read_text(encoding="utf-8")
    # heading annotation preserved; everything outside the Plan span identical
    assert "## Plan / Working Memory (MUTABLE - orchestrator may rewrite)\n" in raw_after
    assert raw_after.split("## Plan / Working Memory")[0] == (
        raw_before.split("## Plan / Working Memory")[0]
    )
    assert raw_after.split("## Activity Log")[1] == raw_before.split("## Activity Log")[1]


def test_write_plan_empty_plan(tmp_path):
    path = _write(tmp_path / "p.md", FULL_PROJECT)
    write_plan(path, "")
    project = parse_project_file(path)
    assert project.plan == ""
    assert project.spec.startswith("Build the team-memory UI tab.")
    assert project.activity_log.endswith("dispatched iteration 1")


def test_write_plan_h2_injection_raises(tmp_path):
    path = _write(tmp_path / "p.md", FULL_PROJECT)
    before = path.read_text(encoding="utf-8")
    with pytest.raises(OwnershipError, match="H2 heading"):
        write_plan(path, "## Spec\nEVIL replacement spec")
    assert path.read_text(encoding="utf-8") == before


# === Spec ownership guard (the load-bearing invariant) ===


def _spec_bytes(path):
    raw = path.read_text(encoding="utf-8")
    return raw[raw.index("## Spec"):raw.index("## Plan / Working Memory")]


def test_spec_bytes_unchanged_after_every_writer(tmp_path):
    path = _write(tmp_path / "p.md", FULL_PROJECT)
    spec_before = _spec_bytes(path)

    update_frontmatter(path, status="testing", iterations=9)
    append_activity_log(path, "poked the log", timestamp="2026-07-03T11:00:00")
    write_plan(path, "totally new plan")

    assert _spec_bytes(path) == spec_before
    assert parse_project_file(path).spec == (
        "Build the team-memory UI tab.\n\nShip it behind a flag."
    )


def test_no_public_spec_writer_exists():
    """The Spec is operator-owned: the module must not export any callable
    that names the spec (write_spec, update_spec, ...)."""
    import cofounder.project_model as pm

    offenders = [
        name
        for name in dir(pm)
        if "spec" in name.lower() and callable(getattr(pm, name))
    ]
    assert offenders == []


# === archive_to_done ===


def test_archive_to_done_moves_preserving_content(tmp_path):
    projects_dir = tmp_path / "cofounder"
    projects_dir.mkdir()
    path = _write(projects_dir / "shipped.md", FULL_PROJECT)

    target = archive_to_done(path)

    assert not path.exists()
    assert target == projects_dir / "done" / "shipped.md"
    assert target.read_text(encoding="utf-8") == FULL_PROJECT
    assert discover_projects(projects_dir) == []  # archived = out of discovery


def test_archive_to_done_collision_gets_suffix(tmp_path):
    projects_dir = tmp_path / "cofounder"
    (projects_dir / "done").mkdir(parents=True)
    _write(projects_dir / "done" / "shipped.md", "earlier archive")
    path = _write(projects_dir / "shipped.md", FULL_PROJECT)

    target = archive_to_done(path)

    assert target.name == "shipped-1.md"
    assert (projects_dir / "done" / "shipped.md").read_text(encoding="utf-8") == (
        "earlier archive"
    )
    assert target.read_text(encoding="utf-8") == FULL_PROJECT
