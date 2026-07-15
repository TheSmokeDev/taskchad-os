"""Tests for github_signal.picks — prompt assembly, validation, fallback.

The LLM boundary is monkeypatched at module-attribute level (Rule 3); no
runtime lane is ever invoked.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from github_signal import picks as picks_mod  # noqa: E402


def _eligible(*specs: tuple[str, str]) -> list[dict]:
    """specs: (full_name, starred_at_date)."""
    return [
        {
            "full_name": name,
            "starred_at": f"{starred}T00:00:00Z",
            "description": f"description of {name} " + "x" * 200,
            "language": "Python",
            "html_url": f"https://github.com/{name}",
        }
        for name, starred in specs
    ]


def _fake_llm(reply_text: str, captured: dict):
    async def fake(req):
        captured["prompt"] = req.prompt
        captured["model"] = req.model
        captured["task_name"] = req.task_name
        return SimpleNamespace(text=reply_text)

    return fake


@pytest.mark.asyncio
async def test_prompt_contains_inventory_and_context(monkeypatch, tmp_path):
    goals = tmp_path / "GOALS.md"
    goals.write_text("Ship the voice pipeline Q3", encoding="utf-8")
    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / "2026-07-13.md").write_text("worked on wf18 canary", encoding="utf-8")
    monkeypatch.setattr(picks_mod._main_config, "GOALS_FILE", goals)
    monkeypatch.setattr(picks_mod._main_config, "DAILY_DIR", daily)
    monkeypatch.setattr(picks_mod._main_config, "PROJECT_ROOT", tmp_path)

    captured: dict = {}
    reply = '[{"full_name": "a/old", "why_now": "bridges wf18 work"}]'
    monkeypatch.setattr(picks_mod, "run_with_runtime_lanes", _fake_llm(reply, captured))

    eligible = _eligible(("a/old", "2025-01-01"), ("b/new", "2026-07-01"))
    picks, used_llm = await picks_mod.pick_backlog(
        eligible, 1, max_budget_usd=0.1, model="sonnet"
    )

    assert used_llm is True
    assert picks == [{"full_name": "a/old", "why_now": "bridges wf18 work"}]
    prompt = captured["prompt"]
    assert "Ship the voice pipeline Q3" in prompt
    assert "worked on wf18 canary" in prompt
    # oldest star first, description truncated to 110 chars per line
    a_line = next(l for l in prompt.splitlines() if "a/old" in l and "|" in l)
    b_line = next(l for l in prompt.splitlines() if "b/new" in l and "|" in l)
    assert prompt.index(a_line) < prompt.index(b_line)
    assert len(a_line.split("|")[-1].strip()) <= 110
    assert captured["model"] == "sonnet"


@pytest.mark.asyncio
async def test_hallucinated_names_dropped_and_topped_up(monkeypatch):
    reply = (
        '[{"full_name": "not/starred", "why_now": "hallucinated"},'
        ' {"full_name": "a/real", "why_now": "legit"}]'
    )
    monkeypatch.setattr(picks_mod, "run_with_runtime_lanes", _fake_llm(reply, {}))
    eligible = _eligible(("a/real", "2025-01-01"), ("b/other", "2026-06-01"))

    picks, used_llm = await picks_mod.pick_backlog(
        eligible, 2, max_budget_usd=0.1, model="sonnet"
    )
    names = [p["full_name"] for p in picks]
    assert "not/starred" not in names
    assert "a/real" in names
    assert "b/other" in names  # topped up from fallback
    assert used_llm is True


@pytest.mark.asyncio
async def test_llm_failure_falls_back_deterministically(monkeypatch):
    async def boom(req):
        raise RuntimeError("lane down")

    monkeypatch.setattr(picks_mod, "run_with_runtime_lanes", boom)
    eligible = _eligible(
        ("a/oldest", "2024-01-01"), ("b/mid", "2025-06-01"), ("c/newest", "2026-07-01")
    )
    picks, used_llm = await picks_mod.pick_backlog(
        eligible, 2, max_budget_usd=0.1, model="sonnet"
    )
    assert used_llm is False
    # fallback = most recently starred first, flagged why_now
    assert [p["full_name"] for p in picks] == ["c/newest", "b/mid"]
    assert all("fallback" in p["why_now"] for p in picks)


@pytest.mark.asyncio
async def test_garbage_llm_reply_falls_back(monkeypatch):
    monkeypatch.setattr(
        picks_mod, "run_with_runtime_lanes", _fake_llm("sure! here are ideas...", {})
    )
    eligible = _eligible(("a/x", "2025-01-01"))
    picks, used_llm = await picks_mod.pick_backlog(
        eligible, 1, max_budget_usd=0.1, model="sonnet"
    )
    assert used_llm is False
    assert picks[0]["full_name"] == "a/x"


@pytest.mark.asyncio
async def test_eligible_smaller_than_n_and_empty(monkeypatch):
    reply = '[{"full_name": "a/x", "why_now": "y"}]'
    monkeypatch.setattr(picks_mod, "run_with_runtime_lanes", _fake_llm(reply, {}))
    picks, _ = await picks_mod.pick_backlog(
        _eligible(("a/x", "2025-01-01")), 5, max_budget_usd=0.1, model="sonnet"
    )
    assert len(picks) == 1

    picks, used_llm = await picks_mod.pick_backlog(
        [], 5, max_budget_usd=0.1, model="sonnet"
    )
    assert picks == [] and used_llm is False


def test_gather_context_degrades_when_files_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        picks_mod._main_config, "GOALS_FILE", tmp_path / "missing.md"
    )
    monkeypatch.setattr(picks_mod._main_config, "DAILY_DIR", tmp_path / "no-dir")
    monkeypatch.setattr(picks_mod._main_config, "PROJECT_ROOT", tmp_path)
    context = picks_mod._gather_context()
    assert context == "(no active-work context found)"


def test_extract_json_array_variants():
    assert picks_mod._extract_json_array('[{"a": 1}]') == [{"a": 1}]
    assert picks_mod._extract_json_array('```json\n[{"a": 1}]\n```') == [{"a": 1}]
    assert picks_mod._extract_json_array('Here you go: [{"a": 1}] hope it helps') == [
        {"a": 1}
    ]
    assert picks_mod._extract_json_array('{"not": "array"}') is None
    assert picks_mod._extract_json_array("") is None
