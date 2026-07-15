"""Tests for github_signal.eval_runner — degradation ladder, read-only contract.

Every boundary (GitHub API, git clone, LLM, notify lanes) is monkeypatched at
module level; sandbox + digest + state land in tmp dirs.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from github_signal import eval_runner as eval_mod  # noqa: E402
from github_signal import state as state_mod  # noqa: E402


def _meta(size_kb: int = 1024) -> dict:
    return {
        "full_name": "owner/repo",
        "description": "a test repo",
        "language": "Python",
        "stargazers_count": 123,
        "pushed_at": "2026-07-01T00:00:00Z",
        "archived": False,
        "topics": ["ai"],
        "size": size_kb,
        "license": {"spdx_id": "MIT"},
    }


_VERDICT_JSON = json.dumps(
    {
        "what_it_is": "An agent framework",
        "fit_with_active_work": "Plugs into the voice pipeline",
        "recommendation": "adopt",
        "why": "Actively maintained, small dep surface",
        "effort_estimate": "30 min spike",
    }
)


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    """Wire all boundaries to fakes; record calls."""
    monkeypatch.setattr(eval_mod, "GITHUB_SIGNAL_DIR", tmp_path / "github-signal")
    monkeypatch.setattr(eval_mod, "REPO_EVAL_SANDBOX_DIR", tmp_path / "repo-eval")
    monkeypatch.setattr(
        state_mod, "GITHUB_SIGNAL_STATE_FILE", tmp_path / "state.json"
    )
    monkeypatch.setattr(eval_mod, "_gather_context", lambda: "(active work ctx)")

    calls = {"clone": 0, "llm": 0, "tg": [], "dc": []}
    monkeypatch.setattr(eval_mod, "_api_get", lambda url, **kw: _meta())

    def fake_clone(full_name, sandbox, timeout=180.0):
        calls["clone"] += 1
        sandbox.mkdir(parents=True, exist_ok=True)
        (sandbox / "README.md").write_text("# Test repo readme", encoding="utf-8")
        (sandbox / "pyproject.toml").write_text("[project]", encoding="utf-8")
        return True

    monkeypatch.setattr(eval_mod, "_clone", fake_clone)

    async def fake_llm(req):
        calls["llm"] += 1
        return SimpleNamespace(text=_VERDICT_JSON)

    monkeypatch.setattr(eval_mod, "run_with_runtime_lanes", fake_llm)

    import social.notify as social_notify_mod

    monkeypatch.setattr(
        social_notify_mod,
        "send_text_to_telegram",
        lambda text: calls["tg"].append(text) or True,
    )
    monkeypatch.setattr(
        social_notify_mod,
        "send_text_to_discord",
        lambda text, cid: calls["dc"].append((text, cid)) or True,
    )
    monkeypatch.setenv("GITHUB_SIGNAL_DISCORD_CHANNEL_ID", "42424242")
    monkeypatch.delenv("GITHUB_SIGNAL_EVAL_KEEP_CLONE", raising=False)
    return {"tmp": tmp_path, "calls": calls}


def _note_text(harness) -> str:
    notes = list((harness["tmp"] / "github-signal" / "evals").glob("*.md"))
    assert len(notes) == 1
    return notes[0].read_text(encoding="utf-8")


# ── degradation ladder ─────────────────────────────────────


@pytest.mark.asyncio
async def test_invalid_name_exits_without_any_work(harness, monkeypatch):
    def forbidden(url, **kw):
        raise AssertionError("API must not be touched on invalid name")

    monkeypatch.setattr(eval_mod, "_api_get", forbidden)
    assert await eval_mod.run_eval("not-a-repo") == "invalid"
    assert await eval_mod.run_eval("a/b/c") == "invalid"


@pytest.mark.asyncio
async def test_oversize_repo_skips_clone_but_ships_card(harness, monkeypatch):
    monkeypatch.setattr(
        eval_mod,
        "_api_get",
        lambda url, **kw: (
            "README VIA API" if "readme" in url else _meta(size_kb=500 * 1024)
        ),
    )
    result = await eval_mod.run_eval("owner/repo")
    assert result == "done"
    assert harness["calls"]["clone"] == 0
    card = harness["calls"]["tg"][0]
    assert "clone skipped: size" in card
    assert "ADOPT" in card  # LLM still ran on API-only evidence


@pytest.mark.asyncio
async def test_clone_failure_degrades_to_api_evidence(harness, monkeypatch):
    monkeypatch.setattr(eval_mod, "_clone", lambda fn, sb, timeout=180.0: False)
    result = await eval_mod.run_eval("owner/repo")
    assert result == "done"
    assert "clone skipped: clone failed" in harness["calls"]["tg"][0]


@pytest.mark.asyncio
async def test_llm_failure_ships_facts_card_and_note(harness, monkeypatch):
    async def boom(req):
        raise RuntimeError("lane down")

    monkeypatch.setattr(eval_mod, "run_with_runtime_lanes", boom)
    result = await eval_mod.run_eval("owner/repo")
    assert result == "done"
    card = harness["calls"]["tg"][0]
    assert "Verdict: unavailable" in card
    note = _note_text(harness)
    assert "recommendation: unavailable" in note


@pytest.mark.asyncio
async def test_success_path_card_note_state_and_both_lanes(harness):
    result = await eval_mod.run_eval("owner/repo")
    assert result == "done"
    card = harness["calls"]["tg"][0]
    assert "Verdict: ADOPT" in card
    assert "voice pipeline" in card
    assert harness["calls"]["dc"][0][1] == "42424242"
    assert harness["calls"]["llm"] == 1

    note = _note_text(harness)
    assert "recommendation: adopt" in note

    persisted = json.loads(
        (harness["tmp"] / "state.json").read_text(encoding="utf-8")
    )
    entry = persisted["repos"]["owner/repo"]
    assert entry["eval_recommendation"] == "adopt"
    assert "status" not in entry  # eval never touches lifecycle


# ── cleanup ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lane_index_written_with_eval_row(harness):
    result = await eval_mod.run_eval("owner/repo")
    assert result == "done"
    index = harness["tmp"] / "github-signal" / "GITHUB-SIGNAL-INDEX.md"
    assert index.exists()
    text = index.read_text(encoding="utf-8")
    note_stem = next((harness["tmp"] / "github-signal" / "evals").glob("*.md")).stem
    assert f"[[{note_stem}]]" in text
    assert "owner/repo" in text


@pytest.mark.asyncio
async def test_sandbox_deleted_by_default_even_with_readonly_files(
    harness, monkeypatch
):
    def clone_with_readonly(full_name, sandbox, timeout=180.0):
        sandbox.mkdir(parents=True, exist_ok=True)
        locked = sandbox / "pack.idx"
        locked.write_text("x", encoding="utf-8")
        os.chmod(locked, stat.S_IREAD)  # mimic .git read-only objects
        return True

    monkeypatch.setattr(eval_mod, "_clone", clone_with_readonly)
    await eval_mod.run_eval("owner/repo")
    assert not (harness["tmp"] / "repo-eval" / "owner__repo").exists()


@pytest.mark.asyncio
async def test_sandbox_kept_with_knob(harness, monkeypatch):
    monkeypatch.setenv("GITHUB_SIGNAL_EVAL_KEEP_CLONE", "true")
    await eval_mod.run_eval("owner/repo")
    assert (harness["tmp"] / "repo-eval" / "owner__repo").is_dir()


# ── json extraction ────────────────────────────────────────


def test_extract_json_object_variants():
    assert eval_mod._extract_json_object('{"a": 1}') == {"a": 1}
    assert eval_mod._extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert eval_mod._extract_json_object('Sure: {"a": 1} enjoy') == {"a": 1}
    assert eval_mod._extract_json_object('[{"not": "object"}]') is None
    assert eval_mod._extract_json_object("") is None


def test_validate_verdict_rejects_bad_recommendation():
    assert eval_mod._validate_verdict({"recommendation": "maybe"}) is None
    assert eval_mod._validate_verdict(None) is None
    ok = eval_mod._validate_verdict(
        {"recommendation": "SKIP", "why": "x" * 500, "what_it_is": "y"}
    )
    assert ok["recommendation"] == "skip"
    assert len(ok["why"]) == 260  # capped
