"""Test PRD-8 Phase 5a / WS1.6 — title generator (M2 contract).

M2 — fires AFTER first user/assistant exchange (NOT first user turn alone),
BOTH messages as inputs, 30s timeout, background async via
asyncio.create_task. NULL on failure.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

import config
from cabinet.title import (
    TITLE_GENERATION_TIMEOUT_S,
    TITLE_MAX_LEN,
    generate_title,
    maybe_set_meeting_title,
    schedule_title_generation,
)
from dashboard_db import get_connection
from runtime.base import RuntimeResult


@pytest.fixture
def tmp_dashboard_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "dashboard.db"
    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", str(db_path))
    conn = get_connection()
    conn.close()
    return db_path


def _make_meeting() -> int:
    conn = get_connection()
    try:
        cur = conn.execute("INSERT INTO cabinet_meetings (mode) VALUES (?)", ("text",))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def test_constants_match_hermes() -> None:
    """M2 — 30s timeout (matches Hermes TITLE_GENERATION_TIMEOUT_S)."""
    assert TITLE_GENERATION_TIMEOUT_S == 30.0
    assert TITLE_MAX_LEN == 80


@pytest.mark.asyncio
async def test_generate_title_uses_both_messages_via_lane_router() -> None:
    captured_prompts: list[str] = []

    async def fake_run(req):
        captured_prompts.append(req.prompt)
        return RuntimeResult(text="SEO content plan Q4", runtime_lane="claude_native", provider="claude", model="haiku")

    with patch("cabinet.title.lane_router.run_with_runtime_lanes", side_effect=fake_run):
        title = await generate_title(
            user_message="What's our SEO content plan for Q4?",
            assistant_response="Here are five themes we should target...",
        )
    assert title == "SEO content plan Q4"
    # Prompt MUST include both messages.
    assert "User: What's our SEO content plan" in captured_prompts[0]
    assert "Assistant: Here are five themes" in captured_prompts[0]


@pytest.mark.asyncio
async def test_generate_title_returns_none_on_timeout() -> None:
    async def fake_run(req):
        await asyncio.sleep(10.0)
        return None

    with patch("cabinet.title.lane_router.run_with_runtime_lanes", side_effect=fake_run):
        title = await generate_title("u", "a", timeout=0.05)
    assert title is None


@pytest.mark.asyncio
async def test_generate_title_returns_none_on_error() -> None:
    async def fake_run(req):
        raise RuntimeError("boom")

    with patch("cabinet.title.lane_router.run_with_runtime_lanes", side_effect=fake_run):
        title = await generate_title("u", "a")
    assert title is None


@pytest.mark.asyncio
async def test_generate_title_strips_quotes_and_prefix() -> None:
    async def fake_run(req):
        return RuntimeResult(text='Title: "SEO Plan Q4"', runtime_lane="claude_native", provider="claude", model="haiku")

    with patch("cabinet.title.lane_router.run_with_runtime_lanes", side_effect=fake_run):
        title = await generate_title("u", "a")
    assert title == "SEO Plan Q4"


@pytest.mark.asyncio
async def test_maybe_set_meeting_title_writes_when_null(tmp_dashboard_db: Path) -> None:
    meeting_id = _make_meeting()

    async def fake_run(req):
        return RuntimeResult(text="A meaningful title", runtime_lane="claude_native", provider="claude", model="haiku")

    with patch("cabinet.title.lane_router.run_with_runtime_lanes", side_effect=fake_run):
        await maybe_set_meeting_title(meeting_id, "user msg", "assistant reply")

    conn = get_connection()
    try:
        row = conn.execute("SELECT title FROM cabinet_meetings WHERE id = ?", (meeting_id,)).fetchone()
    finally:
        conn.close()
    assert row["title"] == "A meaningful title"


@pytest.mark.asyncio
async def test_maybe_set_meeting_title_skips_when_already_set(tmp_dashboard_db: Path) -> None:
    meeting_id = _make_meeting()
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE cabinet_meetings SET title = ? WHERE id = ?",
            ("Operator-set", meeting_id),
        )
        conn.commit()
    finally:
        conn.close()

    called: list[bool] = []

    async def fake_run(req):
        called.append(True)
        return RuntimeResult(text="never", runtime_lane="claude_native", provider="claude", model="haiku")

    with patch("cabinet.title.lane_router.run_with_runtime_lanes", side_effect=fake_run):
        await maybe_set_meeting_title(meeting_id, "u", "a")

    assert called == []  # short-circuited on existing title
    conn = get_connection()
    try:
        row = conn.execute("SELECT title FROM cabinet_meetings WHERE id = ?", (meeting_id,)).fetchone()
    finally:
        conn.close()
    assert row["title"] == "Operator-set"


def test_schedule_title_generation_no_loop_returns_none() -> None:
    """No running loop → schedule returns None silently (sync caller path)."""
    out = schedule_title_generation(1, "u", "a")
    assert out is None


@pytest.mark.asyncio
async def test_schedule_title_generation_returns_task() -> None:
    """In an async context, schedule returns an asyncio.Task that can be awaited."""
    async def fake_run(req):
        return RuntimeResult(text="x", runtime_lane="claude_native", provider="claude", model="haiku")

    with patch("cabinet.title.lane_router.run_with_runtime_lanes", side_effect=fake_run), \
         patch("cabinet.title.maybe_set_meeting_title") as mock_set:
        async def _coro(*args, **kwargs):
            return None
        mock_set.side_effect = _coro
        task = schedule_title_generation(1, "u", "a")
        assert task is not None
        await task
        assert mock_set.called
