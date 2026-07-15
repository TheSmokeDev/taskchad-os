"""Tests for github_signal.engine — pipeline outcomes and digest rendering.

Every boundary (fetch, trending, LLM picks, daily log, Telegram) is
monkeypatched at engine-module level; state + digest land in tmp dirs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from github_signal import engine as engine_mod  # noqa: E402
from github_signal import output as output_mod  # noqa: E402
from github_signal import state as state_mod  # noqa: E402
from github_signal.fetch import FetchError  # noqa: E402


def _inventory() -> list[dict]:
    return [
        {
            "full_name": "a/old",
            "starred_at": "2025-01-01T00:00:00Z",
            "description": "old repo",
            "language": "Python",
            "html_url": "https://github.com/a/old",
        },
        {
            "full_name": "b/new",
            "starred_at": "2026-07-10T00:00:00Z",
            "description": "new repo",
            "language": "Rust",
            "html_url": "https://github.com/b/new",
        },
    ]


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    """Wire state + digest to tmp, mock all boundaries, record calls."""
    state_file = tmp_path / "github-signal-state.json"
    digest_dir = tmp_path / "github-signal"
    monkeypatch.setattr(state_mod, "GITHUB_SIGNAL_STATE_FILE", state_file)
    monkeypatch.setattr(output_mod, "GITHUB_SIGNAL_DIR", digest_dir)
    monkeypatch.setenv("GITHUB_SIGNAL_ENABLED", "true")

    calls = {"picks": 0, "notify": 0, "log": 0}

    monkeypatch.setattr(engine_mod, "fetch_starred", lambda: _inventory())
    monkeypatch.setattr(
        engine_mod, "_gather_trending", lambda kw, persist=True: []
    )

    async def fake_picks(eligible, n):
        calls["picks"] += 1
        return (
            [{"full_name": eligible[0]["full_name"], "why_now": "matches active work"}],
            True,
        )

    monkeypatch.setattr(engine_mod, "pick_backlog", fake_picks)
    monkeypatch.setattr(
        engine_mod, "append_log", lambda d, p: calls.__setitem__("log", calls["log"] + 1)
    )

    def fake_notify(d, p):
        calls["notify"] += 1
        return (True, True)

    monkeypatch.setattr(engine_mod, "notify", fake_notify)
    return {"state_file": state_file, "digest_dir": digest_dir, "calls": calls}


def _state(harness) -> dict:
    return json.loads(harness["state_file"].read_text(encoding="utf-8"))


def _digest_text(harness) -> str:
    # [0-9]*.md: dated digests only — GITHUB-SIGNAL-INDEX.md lives alongside.
    files = list(harness["digest_dir"].glob("[0-9]*.md"))
    assert len(files) == 1
    return files[0].read_text(encoding="utf-8")


# ── pipeline outcomes ──────────────────────────────────────


@pytest.mark.asyncio
async def test_first_run_baselines_watermark_but_still_picks(harness):
    result = await engine_mod.run_github_signal()
    assert result == "success"
    state = _state(harness)
    assert state["starred_watermark"] == "2026-07-10T00:00:00Z"
    assert state["new_stars_last_run"] == 0
    digest = _digest_text(harness)
    assert "_None since last run._" in digest  # no new-star replay
    assert "matches active work" in digest  # picks ran on run 1
    assert harness["calls"]["picks"] == 1


@pytest.mark.asyncio
async def test_success_path_writes_digest_state_log_and_ping(harness, monkeypatch):
    harness["state_file"].write_text(
        json.dumps({"starred_watermark": "2026-01-01T00:00:00Z"}), encoding="utf-8"
    )
    monkeypatch.setattr(
        engine_mod,
        "_gather_trending",
        lambda kw, persist=True: [
            {"full_name": "hot/repo", "stars": "9000", "description": "an agent"}
        ],
    )
    result = await engine_mod.run_github_signal()
    assert result == "success"

    digest = _digest_text(harness)
    assert digest.startswith("---\n")
    assert "picks_via_llm: true" in digest
    assert "## New stars this week" in digest
    assert "b/new" in digest  # starred after watermark
    assert "## Backlog picks — why now" in digest
    assert "## Trending this week" in digest and "hot/repo" in digest
    assert "/stars used a/old" in digest

    state = _state(harness)
    assert state["last_result"] == "success"
    assert state["starred_watermark"] == "2026-07-10T00:00:00Z"
    assert state["new_stars_last_run"] == 1
    assert state["repos"]["a/old"]["status"] == "surfaced"
    assert state["last_picks"][0]["full_name"] == "a/old"
    assert harness["calls"]["log"] == 1
    assert harness["calls"]["notify"] == 1


@pytest.mark.asyncio
async def test_silent_path_skips_llm_ping_and_digest(harness, monkeypatch):
    # No new stars (watermark current), nothing eligible (all used), no trending.
    harness["state_file"].write_text(
        json.dumps(
            {
                "starred_watermark": "2026-07-10T00:00:00Z",
                "repos": {
                    "a/old": {"status": "used", "used_at": "2026-07-01"},
                    "b/new": {"status": "used", "used_at": "2026-07-01"},
                },
            }
        ),
        encoding="utf-8",
    )
    result = await engine_mod.run_github_signal()
    assert result == "GITHUB_SIGNAL_SILENT"
    assert harness["calls"]["picks"] == 0
    assert harness["calls"]["notify"] == 0
    assert not harness["digest_dir"].exists()
    assert _state(harness)["last_result"] == "silent"


@pytest.mark.asyncio
async def test_fetch_failure_leaves_watermark_untouched(harness, monkeypatch):
    harness["state_file"].write_text(
        json.dumps({"starred_watermark": "2026-01-01T00:00:00Z"}), encoding="utf-8"
    )

    def boom():
        raise FetchError("GitHub API HTTP 500 on page 1")

    monkeypatch.setattr(engine_mod, "fetch_starred", boom)
    result = await engine_mod.run_github_signal()
    assert result == "failed"
    state = _state(harness)
    assert state["starred_watermark"] == "2026-01-01T00:00:00Z"
    assert state["last_result"] == "failed"
    assert not harness["digest_dir"].exists()


@pytest.mark.asyncio
async def test_disabled_runs_nothing(harness, monkeypatch):
    monkeypatch.setenv("GITHUB_SIGNAL_ENABLED", "false")
    result = await engine_mod.run_github_signal()
    assert result == "disabled"
    assert not harness["state_file"].exists()


@pytest.mark.asyncio
async def test_trending_failure_is_non_fatal(harness, monkeypatch):
    def boom(kw, persist=True):
        raise RuntimeError("github changed their HTML again")

    monkeypatch.setattr(engine_mod, "_gather_trending", boom)
    result = await engine_mod.run_github_signal()
    assert result == "success"
    assert "_No trending hits matched" in _digest_text(harness)


@pytest.mark.asyncio
async def test_telegram_failure_still_success(harness, monkeypatch):
    monkeypatch.setattr(engine_mod, "notify", lambda d, p: (False, False))
    result = await engine_mod.run_github_signal()
    assert result == "success"  # digest is the durable artifact
    assert _state(harness)["last_result"] == "success"


@pytest.mark.asyncio
async def test_fallback_picks_flagged_in_frontmatter(harness, monkeypatch):
    async def fallback_picks(eligible, n):
        return [{"full_name": "a/old", "why_now": "(fallback)"}], False

    monkeypatch.setattr(engine_mod, "pick_backlog", fallback_picks)
    result = await engine_mod.run_github_signal()
    assert result == "success"
    assert "picks_via_llm: false" in _digest_text(harness)


@pytest.mark.asyncio
async def test_test_mode_makes_no_writes(harness):
    result = await engine_mod.run_github_signal(test_mode=True)
    assert result == "success"
    assert harness["calls"]["picks"] == 0
    assert not harness["state_file"].exists()
    assert not harness["digest_dir"].exists()


@pytest.mark.asyncio
async def test_lane_index_written_alongside_digest(harness):
    result = await engine_mod.run_github_signal()
    assert result == "success"
    index = harness["digest_dir"] / "GITHUB-SIGNAL-INDEX.md"
    assert index.exists()
    text = index.read_text(encoding="utf-8")
    digest_stem = next(harness["digest_dir"].glob("[0-9]*.md")).stem
    assert f"[[{digest_stem}]]" in text
    assert "[[MOC-thehomie]]" in text


# ── status display ─────────────────────────────────────────


def test_status_before_first_run_and_after(harness):
    assert "has not run yet" in engine_mod.get_latest_status()
    harness["state_file"].write_text(
        json.dumps(
            {
                "last_run": "2026-07-14T09:00:00+00:00",
                "last_result": "success",
                "inventory_count": 392,
                "new_stars_last_run": 4,
                "repos": {
                    "a/old": {"status": "used", "used_at": "2026-07-14"},
                    "c/z": {"status": "snoozed", "snooze_until": "2026-08-01"},
                },
                "last_picks": [{"full_name": "a/old", "why_now": "relevant"}],
            }
        ),
        encoding="utf-8",
    )
    status = engine_mod.get_latest_status()
    assert "392" in status
    assert "1 used · 1 snoozed" in status
    assert "a/old ✅ — relevant" in status


# ── dual-lane notify (Repo Scout build) ────────────────────


class TestNotifyDualLane:
    def _data(self):
        return {
            "week": "2026-W29",
            "date": "2026-07-14",
            "new_stars": [],
            "picks": [{"full_name": "a/b", "why_now": "x"}],
            "trending": [],
            "picks_via_llm": True,
            "inventory_count": 1,
        }

    def test_both_lanes_called_with_same_card(self, tmp_path, monkeypatch):
        import social.notify as social_notify_mod

        monkeypatch.setenv("GITHUB_SIGNAL_DISCORD_CHANNEL_ID", "42424242")
        sent = {}
        def fake_tg(text):
            sent["tg"] = text
            return True

        def fake_dc(text, cid):
            sent["dc"] = (text, cid)
            return True

        monkeypatch.setattr(social_notify_mod, "send_text_to_telegram", fake_tg)
        monkeypatch.setattr(social_notify_mod, "send_text_to_discord", fake_dc)
        result = output_mod.notify(self._data(), tmp_path / "2026-W29.md")
        assert result == (True, True)
        assert sent["tg"] == sent["dc"][0]  # same card both lanes
        assert sent["dc"][1] == "42424242"

    def test_discord_failure_does_not_affect_telegram(self, tmp_path, monkeypatch):
        import social.notify as social_notify_mod

        monkeypatch.setenv("GITHUB_SIGNAL_DISCORD_CHANNEL_ID", "42424242")
        monkeypatch.setattr(
            social_notify_mod, "send_text_to_telegram", lambda text: True
        )

        def dc_boom(text, cid):
            raise RuntimeError("discord down")

        monkeypatch.setattr(social_notify_mod, "send_text_to_discord", dc_boom)
        assert output_mod.notify(self._data(), tmp_path / "d.md") == (True, False)

    def test_empty_knob_never_calls_discord(self, tmp_path, monkeypatch):
        import social.notify as social_notify_mod

        monkeypatch.setenv("GITHUB_SIGNAL_DISCORD_CHANNEL_ID", "")
        monkeypatch.setattr(
            social_notify_mod, "send_text_to_telegram", lambda text: True
        )

        def dc_forbidden(text, cid):
            raise AssertionError("discord lane must not fire with empty knob")

        monkeypatch.setattr(social_notify_mod, "send_text_to_discord", dc_forbidden)
        assert output_mod.notify(self._data(), tmp_path / "d.md") == (True, False)

    def test_config_knob_defaults_and_overrides(self, monkeypatch):
        from github_signal.config import get_github_signal_settings

        monkeypatch.delenv("GITHUB_SIGNAL_DISCORD_CHANNEL_ID", raising=False)
        monkeypatch.delenv("GITHUB_SIGNAL_SCOUT_PROFILE", raising=False)
        settings = get_github_signal_settings()
        assert settings.discord_channel_id == ""
        assert settings.scout_profile == "repo-scout"

        monkeypatch.setenv("GITHUB_SIGNAL_DISCORD_CHANNEL_ID", "777")
        monkeypatch.setenv("GITHUB_SIGNAL_SCOUT_PROFILE", "")
        settings = get_github_signal_settings()
        assert settings.discord_channel_id == "777"
        assert settings.scout_profile == ""


# ── scout sync hook (Repo Scout build) ─────────────────────


@pytest.mark.asyncio
async def test_engine_success_syncs_digest_to_scout(harness, monkeypatch):
    from github_signal import scout_sync as sync_mod

    synced = {}
    monkeypatch.setattr(
        sync_mod, "sync_to_scout", lambda paths: synced.setdefault("paths", paths)
    )
    result = await engine_mod.run_github_signal()
    assert result == "success"
    assert len(synced["paths"]) == 1
    assert synced["paths"][0].name.endswith(".md")


@pytest.mark.asyncio
async def test_scout_sync_failure_is_non_fatal(harness, monkeypatch):
    from github_signal import scout_sync as sync_mod

    def boom(paths):
        raise RuntimeError("profile exploded")

    monkeypatch.setattr(sync_mod, "sync_to_scout", boom)
    result = await engine_mod.run_github_signal()
    assert result == "success"
    assert _state(harness)["last_result"] == "success"
