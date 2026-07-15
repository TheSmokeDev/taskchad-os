"""Tests for github_signal.state — lifecycle map, eligibility, run-end merge.

All tests point the state module at a tmp file; no real state is touched.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from github_signal import state as state_mod  # noqa: E402


@pytest.fixture()
def state_file(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "github-signal-state.json"
    monkeypatch.setattr(state_mod, "GITHUB_SIGNAL_STATE_FILE", path)
    return path


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


# ── load ───────────────────────────────────────────────────


def test_missing_state_file_loads_empty(state_file):
    assert state_mod.load() == {}


def test_corrupt_state_file_fails_open(state_file):
    state_file.write_text("{not json", encoding="utf-8")
    assert state_mod.load() == {}


# ── mark_used / mark_snoozed ───────────────────────────────


def test_mark_used_roundtrip_with_exact_full_name(state_file):
    resolved = state_mod.mark_used("astral-sh/uv")
    assert resolved == "astral-sh/uv"
    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    entry = persisted["repos"]["astral-sh/uv"]
    assert entry["status"] == "used"
    assert entry["used_at"] == date.today().isoformat()


def test_mark_used_bare_name_suffix_matches_last_picks(state_file):
    _write(
        state_file,
        {"last_picks": [{"full_name": "astral-sh/uv", "why_now": "x"}]},
    )
    assert state_mod.mark_used("uv") == "astral-sh/uv"
    assert state_mod.mark_used("UV") == "astral-sh/uv"  # case-insensitive


def test_mark_used_unknown_bare_name_returns_none(state_file):
    _write(state_file, {"last_picks": [{"full_name": "a/b"}]})
    assert state_mod.mark_used("nonexistent") is None
    assert state_mod.mark_used("") is None
    assert state_mod.mark_used("bad/shape/extra") is None


def test_mark_snoozed_computes_snooze_until(state_file):
    resolved = state_mod.mark_snoozed("owner/repo", weeks=6)
    assert resolved == "owner/repo"
    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    entry = persisted["repos"]["owner/repo"]
    assert entry["status"] == "snoozed"
    expected = (date.today() + timedelta(weeks=6)).isoformat()
    assert entry["snooze_until"] == expected


# ── eligible_backlog ───────────────────────────────────────


def _inventory(*names: str) -> list[dict]:
    return [{"full_name": n} for n in names]


def test_eligibility_matrix(state_file):
    today = date.today()
    state = {
        "repos": {
            "a/used": {"status": "used", "used_at": "2026-01-01"},
            "b/snoozed-active": {
                "status": "snoozed",
                "snooze_until": (today + timedelta(days=7)).isoformat(),
            },
            "c/snoozed-expired": {
                "status": "snoozed",
                "snooze_until": (today - timedelta(days=1)).isoformat(),
            },
            "d/surfaced-recent": {
                "status": "surfaced",
                "surfaced_at": (today - timedelta(weeks=2)).isoformat(),
            },
            "e/surfaced-stale": {
                "status": "surfaced",
                "surfaced_at": (today - timedelta(weeks=12)).isoformat(),
            },
        }
    }
    inventory = _inventory(
        "a/used", "b/snoozed-active", "c/snoozed-expired",
        "d/surfaced-recent", "e/surfaced-stale", "f/fresh",
    )
    eligible = state_mod.eligible_backlog(state, inventory, cooldown_weeks=8)
    names = [i["full_name"] for i in eligible]
    assert names == ["c/snoozed-expired", "e/surfaced-stale", "f/fresh"]


def test_eligibility_malformed_dates_fail_open_to_eligible(state_file):
    state = {
        "repos": {
            "a/bad-snooze": {"status": "snoozed", "snooze_until": "not-a-date"},
            "b/bad-surface": {"status": "surfaced", "surfaced_at": None},
        }
    }
    eligible = state_mod.eligible_backlog(
        state, _inventory("a/bad-snooze", "b/bad-surface"), cooldown_weeks=8
    )
    assert len(eligible) == 2


# ── finalize_run ───────────────────────────────────────────


def test_finalize_run_marks_surfaced_and_saves_picks(state_file):
    state_mod.finalize_run(
        result="success",
        watermark="2026-07-12T03:11:09Z",
        inventory_names={"a/pick", "b/other"},
        inventory_count=2,
        new_stars_count=1,
        picked=[{"full_name": "a/pick", "why_now": "relevant now"}],
        trending=[{"full_name": "hot/repo", "stars": "9000"}],
        run_time="2026-07-14T09:00:00+00:00",
    )
    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert persisted["starred_watermark"] == "2026-07-12T03:11:09Z"
    assert persisted["last_result"] == "success"
    assert persisted["repos"]["a/pick"]["status"] == "surfaced"
    assert persisted["last_picks"][0]["why_now"] == "relevant now"
    assert persisted["last_trending"][0]["full_name"] == "hot/repo"


def test_finalize_run_never_downgrades_operator_status(state_file):
    # Operator marked used mid-run (while the engine was in its LLM call).
    _write(
        state_file,
        {"repos": {"a/pick": {"status": "used", "used_at": "2026-07-14"}}},
    )
    state_mod.finalize_run(
        result="success",
        inventory_names={"a/pick"},
        picked=[{"full_name": "a/pick", "why_now": "x"}],
    )
    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert persisted["repos"]["a/pick"]["status"] == "used"


def test_finalize_run_prunes_unstarred_and_keeps_watermark_on_failure(state_file):
    _write(
        state_file,
        {
            "starred_watermark": "2026-07-01T00:00:00Z",
            "repos": {
                "gone/repo": {"status": "surfaced", "surfaced_at": "2026-06-01"},
                "kept/repo": {"status": "used", "used_at": "2026-06-01"},
            },
        },
    )
    # Failed run: no watermark passed → untouched; no inventory → no prune.
    state_mod.finalize_run(result="failed")
    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert persisted["starred_watermark"] == "2026-07-01T00:00:00Z"
    assert persisted["last_result"] == "failed"
    assert "gone/repo" in persisted["repos"]

    # Successful run with inventory that no longer has gone/repo → pruned.
    state_mod.finalize_run(
        result="success",
        watermark="2026-07-12T00:00:00Z",
        inventory_names={"kept/repo"},
    )
    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert "gone/repo" not in persisted["repos"]
    assert "kept/repo" in persisted["repos"]
    assert persisted["starred_watermark"] == "2026-07-12T00:00:00Z"


# ── record_eval (Repo Scout build) ─────────────────────────


def test_record_eval_fresh_repo_stays_eligible(state_file):
    state_mod.record_eval("a/fresh", "try")
    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    entry = persisted["repos"]["a/fresh"]
    assert entry["eval_recommendation"] == "try"
    assert "status" not in entry
    eligible = state_mod.eligible_backlog(
        persisted, _inventory("a/fresh"), cooldown_weeks=8
    )
    assert [i["full_name"] for i in eligible] == ["a/fresh"]


def test_record_eval_never_downgrades_used(state_file):
    _write(
        state_file,
        {"repos": {"a/used": {"status": "used", "used_at": "2026-07-01"}}},
    )
    state_mod.record_eval("a/used", "adopt")
    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    entry = persisted["repos"]["a/used"]
    assert entry["status"] == "used"
    assert entry["eval_recommendation"] == "adopt"


def test_finalize_run_surfacing_preserves_eval_keys(state_file):
    state_mod.record_eval("a/pick", "try")
    state_mod.finalize_run(
        result="success",
        inventory_names={"a/pick"},
        picked=[{"full_name": "a/pick", "why_now": "x"}],
    )
    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    entry = persisted["repos"]["a/pick"]
    assert entry["status"] == "surfaced"
    assert entry["eval_recommendation"] == "try"  # merge, not replace


def test_resolve_name_public_wrapper(state_file):
    _write(state_file, {"last_picks": [{"full_name": "astral-sh/uv"}]})
    assert state_mod.resolve_name("uv") == "astral-sh/uv"
    assert state_mod.resolve_name("any/valid") == "any/valid"
    assert state_mod.resolve_name("unknown-bare") is None
