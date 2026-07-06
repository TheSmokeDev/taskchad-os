"""US-015 — /cofounder command family: handler behavior + 4-point registration.

One test per distinct path (Testing Principle):

* registration completeness across all FOUR surfaces (COMMANDS row,
  CORE_HANDLERS entry, TELEGRAM_NATIVE_COMMANDS, CATEGORIES group)
* usage / status / list / show rendering
* steer appends exactly one single-line timestamped ``[steer]`` Activity Log
  entry (multi-line operator input collapsed; Spec bytes untouched)
* pause/resume/approve status flips asserted on DISK (re-parsed frontmatter,
  archive move) plus the state-json ``paused_from`` stash — Rule 4 spirit
* unknown slug, traversal-shaped slug, malformed file -> friendly errors
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Ensure both .claude/scripts and .claude/chat are importable.
_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS.parent / "chat"))

import commands  # type: ignore[import-not-found]  # noqa: E402
import core_handlers  # type: ignore[import-not-found]  # noqa: E402

import config  # noqa: E402
from cofounder import project_model  # noqa: E402
from cofounder import state as state_mod  # noqa: E402

PROJECT_TEMPLATE = """---
tags: [system, cofounder]
status: {status}
created: 2026-07-01T09:00:00
last_run: null
repo: greenfield
branch: null
current_job_id: null
iterations: 1
max_iterations: 5
max_wall_clock_hours: 72
completion_check: "echo ok"
subjective_gate: {subjective_gate}
archon_workflow: null
chat_thread: null
---
# Widget Factory

## Spec (STATIC - orchestrator MUST NOT rewrite)
Build the widget factory.

## Plan / Working Memory
- [ ] first step

## Activity Log
- 2026-07-01T09:00:00 [note] project created
"""


def _write_project(
    projects_dir: Path,
    slug: str = "alpha",
    status: str = "building",
    subjective_gate: bool = False,
) -> Path:
    path = projects_dir / f"{slug}.md"
    path.write_text(
        PROJECT_TEMPLATE.format(
            status=status,
            subjective_gate="true" if subjective_gate else "false",
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def cofounder_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point projects dir + state dir at tmp; return the projects dir.

    ``get_cofounder_settings`` resolves COFOUNDER_PROJECTS_DIR at call time
    (Rule 1) and ``state._resolve_state_file`` reads ``config.STATE_DIR`` at
    call time, so per-test monkeypatching is enough — no reload needed.
    """
    projects_dir = tmp_path / "cofounder"
    projects_dir.mkdir()
    monkeypatch.setenv("COFOUNDER_PROJECTS_DIR", str(projects_dir))
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    return projects_dir


def _log_lines(path: Path) -> list[str]:
    project = project_model.parse_project_file(path)
    return [ln for ln in project.activity_log.splitlines() if ln.strip()]


async def _run(args: str) -> str:
    incoming = SimpleNamespace(chat_id=42)
    return await core_handlers.handle_cofounder(object(), incoming, args)


# ---------------------------------------------------------------------------
# Registration completeness — all FOUR surfaces contain 'cofounder'
# ---------------------------------------------------------------------------


def test_commands_row_registered_router_admin() -> None:
    row = next((r for r in commands.COMMANDS if r[0] == "cofounder"), None)
    assert row is not None, "missing COMMANDS row for /cofounder"
    assert row[2] == "router", "/cofounder must be router-type, not engine"
    assert row[3] == "admin", "/cofounder must be admin-only role"


def test_core_handlers_entry_registered() -> None:
    assert "cofounder" in core_handlers.CORE_HANDLERS
    assert (
        core_handlers.CORE_HANDLERS["cofounder"]
        is core_handlers.handle_cofounder
    )
    assert "/" not in "cofounder"  # slashless key convention


def test_telegram_native_commands_registered() -> None:
    assert "cofounder" in commands.TELEGRAM_NATIVE_COMMANDS


def test_categories_group_registered() -> None:
    categorized = {name for _cat, names in commands.CATEGORIES for name in names}
    assert "cofounder" in categorized


# ---------------------------------------------------------------------------
# Usage / read-only subcommands
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_args_returns_usage(cofounder_env: Path) -> None:
    out = await _run("")
    assert "/cofounder steer" in out
    assert "/cofounder approve" in out


@pytest.mark.asyncio
async def test_unknown_subcommand_returns_usage(cofounder_env: Path) -> None:
    out = await _run("bogus")
    assert "/cofounder status" in out


@pytest.mark.asyncio
async def test_status_overview(cofounder_env: Path) -> None:
    _write_project(cofounder_env, "alpha", status="building")
    out = await _run("status")
    assert "Enabled" in out
    assert "alpha" in out
    assert "building" in out


@pytest.mark.asyncio
async def test_list_shows_projects_with_status(cofounder_env: Path) -> None:
    _write_project(cofounder_env, "alpha", status="building")
    _write_project(cofounder_env, "beta", status="testing")
    out = await _run("list")
    assert "alpha" in out and "building" in out
    assert "beta" in out and "testing" in out


@pytest.mark.asyncio
async def test_list_empty_dir_is_friendly(cofounder_env: Path) -> None:
    out = await _run("list")
    assert "No co-founder projects" in out


@pytest.mark.asyncio
async def test_show_renders_detail(cofounder_env: Path) -> None:
    _write_project(cofounder_env, "alpha", status="testing")
    out = await _run("show alpha")
    assert "Widget Factory" in out
    assert "testing" in out
    assert "first step" in out
    assert "[note] project created" in out


@pytest.mark.asyncio
async def test_missing_slug_arg_shows_usage(cofounder_env: Path) -> None:
    out = await _run("pause")
    assert "Usage: /cofounder pause <slug>" in out


# ---------------------------------------------------------------------------
# steer — appends one timestamped [steer] line, ownership respected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_steer_appends_timestamped_steer_line(cofounder_env: Path) -> None:
    path = _write_project(cofounder_env, "alpha")
    spec_before = project_model.parse_project_file(path).spec
    before = _log_lines(path)

    out = await _run("steer alpha focus on the API tests first")

    lines = _log_lines(path)
    assert len(lines) == len(before) + 1, "steer must append exactly one line"
    assert lines[-1].endswith("[steer] focus on the API tests first")
    assert lines[-1].startswith("- 20"), "steer line must be timestamped"
    assert lines[:-1] == before, "append-only: earlier entries untouched"
    assert project_model.parse_project_file(path).spec == spec_before
    assert "next pass" in out


@pytest.mark.asyncio
async def test_steer_collapses_multiline_input(cofounder_env: Path) -> None:
    path = _write_project(cofounder_env, "alpha")
    await _run("steer alpha line one\nline two")
    lines = _log_lines(path)
    assert lines[-1].endswith("[steer] line one line two")


@pytest.mark.asyncio
async def test_steer_without_text_shows_usage(cofounder_env: Path) -> None:
    path = _write_project(cofounder_env, "alpha")
    before = _log_lines(path)
    out = await _run("steer alpha")
    assert "Usage: /cofounder steer <slug> <text>" in out
    assert _log_lines(path) == before


# ---------------------------------------------------------------------------
# pause / resume / approve — status flips (disk + state-json assertions)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_flips_to_awaiting_human_and_stashes_prior(
    cofounder_env: Path,
) -> None:
    path = _write_project(cofounder_env, "alpha", status="building")
    out = await _run("pause alpha")
    fm = project_model.parse_project_file(path).frontmatter
    assert fm.status == "awaiting-human"
    entry = state_mod.get_project_state(state_mod.load_state(), "alpha")
    assert entry.get("paused_from") == "building"
    assert any("[pause]" in ln for ln in _log_lines(path))
    assert "paused" in out


@pytest.mark.asyncio
async def test_pause_already_parked_is_friendly(cofounder_env: Path) -> None:
    path = _write_project(cofounder_env, "alpha", status="awaiting-human")
    out = await _run("pause alpha")
    assert "already" in out
    fm = project_model.parse_project_file(path).frontmatter
    assert fm.status == "awaiting-human"


@pytest.mark.asyncio
async def test_pause_done_is_friendly(cofounder_env: Path) -> None:
    path = _write_project(cofounder_env, "alpha", status="done")
    out = await _run("pause alpha")
    assert "nothing to pause" in out
    fm = project_model.parse_project_file(path).frontmatter
    assert fm.status == "done"


@pytest.mark.asyncio
async def test_resume_restores_prior_active_status(cofounder_env: Path) -> None:
    path = _write_project(cofounder_env, "alpha", status="testing")
    await _run("pause alpha")
    out = await _run("resume alpha")
    fm = project_model.parse_project_file(path).frontmatter
    assert fm.status == "testing", "resume must restore the PRIOR active status"
    entry = state_mod.get_project_state(state_mod.load_state(), "alpha")
    assert entry.get("paused_from") is None, "stash cleared on resume"
    assert any("[resume]" in ln for ln in _log_lines(path))
    assert "resumed" in out


@pytest.mark.asyncio
async def test_resume_without_stash_falls_back_to_new(cofounder_env: Path) -> None:
    path = _write_project(cofounder_env, "alpha", status="awaiting-human")
    await _run("resume alpha")
    fm = project_model.parse_project_file(path).frontmatter
    assert fm.status == "new"


@pytest.mark.asyncio
async def test_resume_active_project_is_friendly(cofounder_env: Path) -> None:
    path = _write_project(cofounder_env, "alpha", status="building")
    out = await _run("resume alpha")
    assert "not paused" in out
    fm = project_model.parse_project_file(path).frontmatter
    assert fm.status == "building"


@pytest.mark.asyncio
async def test_approve_flips_done_and_archives_on_disk(cofounder_env: Path) -> None:
    path = _write_project(
        cofounder_env, "alpha", status="awaiting-human", subjective_gate=True
    )
    out = await _run("approve alpha")
    assert not path.exists(), "approve must move the file (Rule 4: disk proof)"
    archived = cofounder_env / "done" / "alpha.md"
    assert archived.exists()
    archived_project = project_model.parse_project_file(archived)
    assert archived_project.frontmatter.status == "done"
    assert any("[approve]" in ln for ln in _log_lines(archived))
    assert "archived" in out


@pytest.mark.asyncio
async def test_approve_non_parked_is_friendly(cofounder_env: Path) -> None:
    path = _write_project(cofounder_env, "alpha", status="testing")
    out = await _run("approve alpha")
    assert "not awaiting" in out
    assert path.exists(), "refused approve must not archive"
    fm = project_model.parse_project_file(path).frontmatter
    assert fm.status == "testing"


# ---------------------------------------------------------------------------
# Friendly errors — unknown slug, traversal-shaped slug, malformed file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_slug_friendly_error_lists_known(cofounder_env: Path) -> None:
    _write_project(cofounder_env, "alpha")
    out = await _run("show ghost")
    assert "Unknown co-founder project" in out
    assert "alpha" in out  # lists known slugs so the operator can retry


@pytest.mark.asyncio
async def test_traversal_slug_rejected(
    cofounder_env: Path, tmp_path: Path
) -> None:
    # A file OUTSIDE the projects dir must be unreachable via slug.
    (tmp_path / "secret.md").write_text("---\nstatus: new\n---\n", encoding="utf-8")
    out = await _run("show ../secret")
    assert "Unknown co-founder project" in out


@pytest.mark.asyncio
async def test_malformed_project_file_is_friendly(cofounder_env: Path) -> None:
    (cofounder_env / "broken.md").write_text("no frontmatter here\n", encoding="utf-8")
    out = await _run("show broken")
    assert "could not be read" in out
    assert "Traceback" not in out
