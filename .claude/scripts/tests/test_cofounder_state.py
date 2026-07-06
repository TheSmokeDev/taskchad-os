"""US-004 — repo slug resolution + per-machine state file.

resolve_repo asserts:
  - known slug resolves to local path + default branch from the
    ``## Active Repositories`` table (repository_memory readers)
  - ``greenfield`` sentinel resolves WITHOUT reading the index
  - unknown/empty slug, missing index, missing section, and blank
    branch/path cells all raise RepoResolutionError — which IS a
    ProjectParseError, so the pass's existing skip-and-warn boundary
    covers repo resolution with no new catch logic
  - default memory_dir resolves config.MEMORY_DIR at call time (Rule 1)

state asserts:
  - round-trip save/load; missing file = clean empty state
  - corrupt / non-mapping file degrades to empty WITH a warning
  - per-project defaults (reply cursor, fail streak, wall-clock start,
    last dispatch) filled; unknown keys and other projects preserved
  - update_project_state is a single-lock read-modify-write; atomic
    writes leave no .tmp behind
  - default state path resolves config.COFOUNDER_STATE_FILE at call time
"""

from __future__ import annotations

import json
import logging

import pytest

from cofounder.project_model import ProjectParseError
from cofounder.repos import (
    GREENFIELD_SLUG,
    RepoResolution,
    RepoResolutionError,
    resolve_repo,
)
from cofounder.state import (
    PROJECT_STATE_DEFAULTS,
    get_project_state,
    load_state,
    save_state,
    update_project_state,
)

REPO_INDEX = """---
tags: [system, repositories, private]
status: active
---
# Repository Index

## Active Repositories

| Slug | GitHub | Visibility | Default branch | Local path | Archon | Page |
| --- | --- | --- | --- | --- | --- | --- |
| thehomie | gh/sb | private | master | C:\\repos\\thehomie | yes | p |
| mission-control | gh/mc | private | YourBusiness-fork | C:\\repos\\mission-control | yes | p |
| broken-repo | gh/broken | private |  | C:\\repos\\broken | no | p |

## Dispatch Defaults

- Resolve the repo slug before coding work.
"""


@pytest.fixture()
def memory_dir(tmp_path):
    md = tmp_path / "Memory"
    md.mkdir()
    (md / "REPOSITORIES.md").write_text(REPO_INDEX, encoding="utf-8")
    return md


# === resolve_repo ===


def test_known_slug_resolves_path_and_branch(memory_dir):
    resolution = resolve_repo("mission-control", memory_dir=memory_dir)
    assert isinstance(resolution, RepoResolution)
    assert resolution.slug == "mission-control"
    assert str(resolution.local_path) == "C:\\repos\\mission-control"
    assert resolution.default_branch == "YourBusiness-fork"
    assert resolution.greenfield is False


def test_slug_whitespace_is_stripped(memory_dir):
    resolution = resolve_repo("  thehomie  ", memory_dir=memory_dir)
    assert resolution.slug == "thehomie"
    assert resolution.default_branch == "master"


def test_greenfield_sentinel_never_reads_index(tmp_path):
    # memory_dir does not even exist — the sentinel must short-circuit.
    resolution = resolve_repo("greenfield", memory_dir=tmp_path / "nope")
    assert resolution.greenfield is True
    assert resolution.slug == GREENFIELD_SLUG
    assert resolution.local_path is None
    assert resolution.default_branch is None


def test_greenfield_sentinel_case_insensitive(tmp_path):
    assert resolve_repo("Greenfield", memory_dir=tmp_path / "nope").greenfield is True


def test_unknown_slug_raises_resolution_error(memory_dir):
    with pytest.raises(RepoResolutionError, match="unknown repo slug: no-such-repo"):
        resolve_repo("no-such-repo", memory_dir=memory_dir)


def test_resolution_error_is_a_parse_error(memory_dir):
    """The pass's existing ProjectParseError boundary must cover repo errors."""
    with pytest.raises(ProjectParseError):
        resolve_repo("no-such-repo", memory_dir=memory_dir)


def test_empty_slug_rejected(memory_dir):
    with pytest.raises(RepoResolutionError, match="empty repo slug"):
        resolve_repo("   ", memory_dir=memory_dir)


def test_header_row_is_not_a_slug(memory_dir):
    with pytest.raises(RepoResolutionError, match="unknown repo slug"):
        resolve_repo("Slug", memory_dir=memory_dir)


def test_missing_index_raises(tmp_path):
    empty = tmp_path / "Memory"
    empty.mkdir()
    with pytest.raises(RepoResolutionError, match="missing or empty repo index"):
        resolve_repo("thehomie", memory_dir=empty)


def test_missing_active_repositories_section_raises(tmp_path):
    md = tmp_path / "Memory"
    md.mkdir()
    (md / "REPOSITORIES.md").write_text(
        "# Repository Index\n\n## Something Else\n\ntext\n", encoding="utf-8"
    )
    with pytest.raises(RepoResolutionError, match="Active Repositories"):
        resolve_repo("thehomie", memory_dir=md)


def test_blank_branch_cell_raises(memory_dir):
    with pytest.raises(RepoResolutionError, match="missing default branch or local path"):
        resolve_repo("broken-repo", memory_dir=memory_dir)


def test_default_memory_dir_resolves_config_at_call_time(memory_dir, monkeypatch):
    """Rule 1: no def-time binding — config.MEMORY_DIR is read per call."""
    import config

    monkeypatch.setattr(config, "MEMORY_DIR", memory_dir)
    resolution = resolve_repo("thehomie")
    assert resolution.default_branch == "master"


# === state file ===


def test_load_missing_file_is_clean_empty(tmp_path):
    assert load_state(tmp_path / "cofounder-state.json") == {}


def test_state_round_trip(tmp_path):
    state_file = tmp_path / "state" / "cofounder-state.json"
    state = {
        "projects": {
            "team-memory-ui": {
                "reply_cursor": 4,
                "fail_streak": 1,
                "wall_clock_start": "2026-07-01T09:00:00",
                "last_dispatch_at": "2026-07-03T08:30:00",
            }
        }
    }
    save_state(state, state_file)
    assert load_state(state_file) == state
    # atomic write leaves no tmp file behind
    assert not state_file.with_suffix(state_file.suffix + ".tmp").exists()


def test_corrupt_state_file_degrades_with_warning(tmp_path, caplog):
    state_file = tmp_path / "cofounder-state.json"
    state_file.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="cofounder.state"):
        assert load_state(state_file) == {}
    assert any("degrading to empty state" in r.message for r in caplog.records)


def test_non_mapping_state_file_degrades_with_warning(tmp_path, caplog):
    state_file = tmp_path / "cofounder-state.json"
    state_file.write_text("[1, 2, 3]", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="cofounder.state"):
        assert load_state(state_file) == {}
    assert any("not a JSON object" in r.message for r in caplog.records)


def test_get_project_state_fills_defaults():
    assert get_project_state({}, "anything") == PROJECT_STATE_DEFAULTS
    partial = {"projects": {"p1": {"fail_streak": 2, "custom_key": "kept"}}}
    entry = get_project_state(partial, "p1")
    assert entry["fail_streak"] == 2
    assert entry["reply_cursor"] == 0
    assert entry["wall_clock_start"] is None
    assert entry["last_dispatch_at"] is None
    assert entry["custom_key"] == "kept"


def test_get_project_state_returns_a_copy():
    state: dict = {}
    entry = get_project_state(state, "p1")
    entry["fail_streak"] = 99
    assert state == {}
    assert PROJECT_STATE_DEFAULTS["fail_streak"] == 0


def test_update_project_state_creates_and_persists(tmp_path):
    state_file = tmp_path / "nested" / "cofounder-state.json"
    entry = update_project_state(
        "team-memory-ui", state_file, last_dispatch_at="2026-07-03T09:00:00"
    )
    assert entry["last_dispatch_at"] == "2026-07-03T09:00:00"
    assert entry["fail_streak"] == 0
    on_disk = json.loads(state_file.read_text(encoding="utf-8"))
    assert on_disk["projects"]["team-memory-ui"]["last_dispatch_at"] == (
        "2026-07-03T09:00:00"
    )


def test_update_preserves_other_projects_and_unknown_keys(tmp_path):
    state_file = tmp_path / "cofounder-state.json"
    save_state(
        {
            "projects": {
                "alpha": {"fail_streak": 3, "mtime_snapshot": 12345.0},
                "beta": {"reply_cursor": 7},
            }
        },
        state_file,
    )
    update_project_state("alpha", state_file, fail_streak=0)
    reloaded = load_state(state_file)
    alpha = reloaded["projects"]["alpha"]
    assert alpha["fail_streak"] == 0
    assert alpha["mtime_snapshot"] == 12345.0  # unknown key preserved
    assert reloaded["projects"]["beta"]["reply_cursor"] == 7


def test_update_on_corrupt_file_degrades_not_crashes(tmp_path, caplog):
    state_file = tmp_path / "cofounder-state.json"
    state_file.write_text('{"projects": {"alpha": ', encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="cofounder.state"):
        entry = update_project_state("alpha", state_file, reply_cursor=2)
    assert entry["reply_cursor"] == 2
    reloaded = load_state(state_file)
    assert reloaded["projects"]["alpha"]["reply_cursor"] == 2


def test_default_state_file_resolves_config_at_call_time(tmp_path, monkeypatch):
    """Rule 1: default path derives from config.STATE_DIR, read per call
    (US-001's lock test forbids any COFOUNDER_* module constant in config)."""
    import config

    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    save_state({"projects": {"p": {"fail_streak": 1}}})
    assert load_state()["projects"]["p"]["fail_streak"] == 1
    assert (tmp_path / "cofounder-state.json").exists()
