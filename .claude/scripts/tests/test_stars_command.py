"""Tests for the /stars router command — registration + handler behavior.

State goes to a tmp file; the detached refresh spawn is monkeypatched.
Registration completeness across the menu surfaces is additionally enforced
by test_command_menu.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import commands  # noqa: E402
import core_handlers  # noqa: E402
from github_signal import state as state_mod  # noqa: E402


@pytest.fixture()
def state_file(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "github-signal-state.json"
    monkeypatch.setattr(state_mod, "GITHUB_SIGNAL_STATE_FILE", path)
    return path


def test_stars_is_registered_on_all_router_surfaces() -> None:
    assert any(row[0] == "stars" and row[2] == "router" for row in commands.COMMANDS)
    assert "stars" in commands.TELEGRAM_NATIVE_COMMANDS
    assert any("stars" in cmds for _, cmds in commands.CATEGORIES)
    assert core_handlers.CORE_HANDLERS["stars"] is core_handlers.handle_stars


@pytest.mark.asyncio
async def test_status_before_first_run(state_file) -> None:
    result = await core_handlers.handle_stars(None, None, "")
    assert "has not run yet" in result


@pytest.mark.asyncio
async def test_used_writes_state_on_disk(state_file) -> None:
    result = await core_handlers.handle_stars(None, None, "used astral-sh/uv")
    assert "Marked used: astral-sh/uv" in result
    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert persisted["repos"]["astral-sh/uv"]["status"] == "used"


@pytest.mark.asyncio
async def test_snooze_with_explicit_weeks(state_file) -> None:
    result = await core_handlers.handle_stars(None, None, "snooze owner/repo 6")
    assert "Snoozed owner/repo for 6 weeks" in result
    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert persisted["repos"]["owner/repo"]["status"] == "snoozed"

    result = await core_handlers.handle_stars(None, None, "snooze owner/repo nope")
    assert "weeks must be a number" in result


@pytest.mark.asyncio
async def test_unknown_bare_repo_lists_current_picks(state_file) -> None:
    state_file.write_text(
        json.dumps({"last_picks": [{"full_name": "a/b", "why_now": "x"}]}),
        encoding="utf-8",
    )
    result = await core_handlers.handle_stars(None, None, "used nonexistent")
    assert "Couldn't match" in result
    assert "a/b" in result


@pytest.mark.asyncio
async def test_refresh_spawns_detached_never_inline(state_file, monkeypatch) -> None:
    import shared

    spawned: dict = {}

    def fake_spawn(cmd, **kwargs):
        spawned["cmd"] = cmd
        spawned["kwargs"] = kwargs
        return 12345

    monkeypatch.setattr(shared, "spawn_detached", fake_spawn)
    result = await core_handlers.handle_stars(None, None, "refresh")
    assert "12345" in result
    assert spawned["cmd"][-1] == "github_signal.engine"
    assert "log_path" in spawned["kwargs"]


@pytest.mark.asyncio
async def test_trending_renders_stored_hits_and_empty_case(state_file) -> None:
    result = await core_handlers.handle_stars(None, None, "trending")
    assert "No trending hits stored yet" in result

    state_file.write_text(
        json.dumps(
            {
                "last_trending": [
                    {"full_name": "hot/repo", "stars": "9000", "description": "agents"}
                ]
            }
        ),
        encoding="utf-8",
    )
    result = await core_handlers.handle_stars(None, None, "trending")
    assert "hot/repo" in result and "9000" in result


@pytest.mark.asyncio
async def test_lock_timeout_returns_busy_message(state_file, monkeypatch) -> None:
    def locked(*args, **kwargs):
        raise TimeoutError("Could not acquire lock")

    monkeypatch.setattr(state_mod, "mark_used", locked)
    result = await core_handlers.handle_stars(None, None, "used a/b")
    assert "try again in a moment" in result


@pytest.mark.asyncio
async def test_unknown_subcommand_shows_usage(state_file) -> None:
    result = await core_handlers.handle_stars(None, None, "bogus")
    assert "Usage:" in result or "Unknown subcommand" in result


# ── /stars eval (Repo Scout build) ─────────────────────────


@pytest.mark.asyncio
async def test_eval_no_arg_shows_usage(state_file) -> None:
    result = await core_handlers.handle_stars(None, None, "eval")
    assert "Usage: /stars eval" in result


@pytest.mark.asyncio
async def test_eval_spawns_detached_runner(state_file, monkeypatch) -> None:
    import shared

    spawned: dict = {}

    def fake_spawn(cmd, **kwargs):
        spawned["cmd"] = cmd
        spawned["kwargs"] = kwargs
        return 777

    monkeypatch.setattr(shared, "spawn_detached", fake_spawn)
    result = await core_handlers.handle_stars(None, None, "eval owner/repo")
    assert "777" in result and "read-only" in result
    assert spawned["cmd"][-2:] == ["github_signal.eval_runner", "owner/repo"]
    assert "owner__repo" in str(spawned["kwargs"]["log_path"])


@pytest.mark.asyncio
async def test_eval_bare_name_resolves_and_unknown_errors(
    state_file, monkeypatch
) -> None:
    import shared

    state_file.write_text(
        json.dumps({"last_picks": [{"full_name": "astral-sh/uv"}]}),
        encoding="utf-8",
    )
    spawned: dict = {}
    monkeypatch.setattr(
        shared,
        "spawn_detached",
        lambda cmd, **kw: spawned.setdefault("cmd", cmd) and 1 or 1,
    )
    result = await core_handlers.handle_stars(None, None, "eval uv")
    assert "astral-sh/uv" in result
    assert spawned["cmd"][-1] == "astral-sh/uv"

    result = await core_handlers.handle_stars(None, None, "eval nonexistent")
    assert "Couldn't match" in result
