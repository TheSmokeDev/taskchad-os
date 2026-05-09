"""Test PRD-8 Phase 5a / WS1.5 — text_orchestrator port.

B1 lock — cabinet code MUST NOT invoke any concrete provider client.
Tests patch `runtime.lane_router.run_with_runtime_lanes` (the only
allowed dispatch surface).

Coverage:
  - @-mention parsing (extract_all_at_mentions).
  - Slash-command recognition (parse_slash_command).
  - Greeting/Acknowledgment short-circuit.
  - Sticky-addressee inference.
  - handle_text_turn happy path: re-fetch → dedup → persist → primary →
    intervener loop → turn_complete.
  - cabinet kill-switch (M7 layer 1) raises KillSwitchDisabled at function
    head.
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

import config
from cabinet import meeting_channel as channels_mod
from cabinet.text_orchestrator import (
    HandleTurnOptions,
    RosterAgent,
    extract_all_at_mentions,
    handle_text_turn,
    is_acknowledgment,
    is_greeting,
    is_social_message,
    parse_slash_command,
)
from dashboard_db import get_connection
from runtime.base import RuntimeResult
from security.kill_switches import KillSwitchDisabled


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_dashboard_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Per-test dashboard.db at a tmp path."""
    db_path = tmp_path / "dashboard.db"
    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", str(db_path))
    # Init schema by opening a connection.
    conn = get_connection()
    conn.close()
    return db_path


@pytest.fixture(autouse=True)
def _reset_channels() -> None:
    channels_mod._reset_channels()
    yield
    channels_mod._reset_channels()


def _make_meeting(chat_id: str = "test-chat") -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO cabinet_meetings (mode, chat_id) VALUES (?, ?)",
            ("text", chat_id),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _test_roster() -> list[RosterAgent]:
    return [
        RosterAgent(id="default", name="Main", description="host"),
        RosterAgent(id="seo", name="SEO", description="content + SERP"),
        RosterAgent(id="ops", name="Ops", description="schedules"),
    ]


# ── Pure helpers ────────────────────────────────────────────────────────


def test_extract_at_mentions_orderpreserving_dedup() -> None:
    roster = _test_roster()
    out = extract_all_at_mentions("hey @seo and @ops, also @seo again", roster)
    assert out == ["seo", "ops"]


def test_extract_at_mentions_skips_unknown() -> None:
    roster = _test_roster()
    out = extract_all_at_mentions("@unknown @seo", roster)
    assert out == ["seo"]


def test_extract_at_mentions_punctuation_boundary() -> None:
    roster = _test_roster()
    # Q4 lock — Python receives canonical "default" id post-Hono-translation
    # of the user's "@main" input via /send route's `(^|\s)@main\b/g` regex.
    out = extract_all_at_mentions("(@seo,@ops) [@default]", roster)
    assert set(out) == {"seo", "ops", "default"}


def test_parse_slash_command() -> None:
    assert parse_slash_command("/standup") == {"cmd": "standup", "args": ""}
    assert parse_slash_command("/discuss SEO trends") == {"cmd": "discuss", "args": "SEO trends"}
    assert parse_slash_command("/standup\n") == {"cmd": "standup", "args": ""}
    assert parse_slash_command("hello") is None
    assert parse_slash_command("/unknown") is None


def test_is_greeting_simple() -> None:
    assert is_greeting("hi")
    assert is_greeting("hey there")
    assert is_greeting("how's it going")
    assert is_greeting("hey what's up")
    assert not is_greeting("hey can you write the SEO post?")  # task word
    assert not is_greeting("what's our SEO plan?")


def test_is_acknowledgment_simple() -> None:
    assert is_acknowledgment("thanks")
    assert is_acknowledgment("thanks team")
    assert is_acknowledgment("ok")
    assert is_acknowledgment("got it")
    assert not is_acknowledgment("thanks - one more thing")
    assert not is_acknowledgment("Q2 numbers please")


def test_is_social_message() -> None:
    assert is_social_message("hi")
    assert is_social_message("ok")
    assert not is_social_message("draft a tweet about SEO")


# ── handle_text_turn happy path ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_text_turn_persists_user_row_and_emits_events(tmp_dashboard_db: Path) -> None:
    meeting_id = _make_meeting()

    async def fake_run(req):
        return RuntimeResult(
            text="Here's a thought from the SEO angle.",
            runtime_lane="claude_native",
            provider="claude",
            model="haiku",
        )

    opts = HandleTurnOptions(roster=_test_roster())

    with patch("cabinet.text_orchestrator.lane_router.run_with_runtime_lanes", side_effect=fake_run), \
         patch("cabinet.text_router.lane_router.run_with_runtime_lanes", side_effect=fake_run):
        result = await handle_text_turn(
            meeting_id=meeting_id,
            user_text="@seo what's our plan?",
            client_msg_id="c_unique_1",
            opts=opts,
        )

    assert result.accepted is True
    assert result.turn_id is not None and result.turn_id.startswith("t_")

    # User row + assistant row persisted to cabinet_transcripts.
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT speaker, text FROM cabinet_transcripts WHERE meeting_id = ? ORDER BY id",
            (meeting_id,),
        ).fetchall()
    finally:
        conn.close()
    speakers = [r["speaker"] for r in rows]
    assert "user" in speakers
    assert "seo" in speakers

    # Channel emitted turn_start, agent_selected, agent_done, turn_complete.
    ch = channels_mod.get_channel(meeting_id)
    types = [e.event["type"] for e in ch.since(0)]
    assert "turn_start" in types
    assert "agent_selected" in types
    assert "agent_done" in types
    assert "turn_complete" in types


@pytest.mark.asyncio
async def test_handle_text_turn_dedups_client_msg_id(tmp_dashboard_db: Path) -> None:
    meeting_id = _make_meeting()

    async def fake_run(req):
        return RuntimeResult(text="reply", runtime_lane="claude_native", provider="claude", model="haiku")

    opts = HandleTurnOptions(roster=_test_roster())
    with patch("cabinet.text_orchestrator.lane_router.run_with_runtime_lanes", side_effect=fake_run), \
         patch("cabinet.text_router.lane_router.run_with_runtime_lanes", side_effect=fake_run):
        r1 = await handle_text_turn(meeting_id, "@seo plan?", "dup_id", opts=opts)
        r2 = await handle_text_turn(meeting_id, "@seo plan?", "dup_id", opts=opts)

    assert r1.accepted is True and r1.deduped is None
    assert r2.accepted is True and r2.deduped is True


@pytest.mark.asyncio
async def test_handle_text_turn_acknowledgment_short_circuits_silent(tmp_dashboard_db: Path) -> None:
    meeting_id = _make_meeting()

    async def fake_run(req):
        return RuntimeResult(text="never called", runtime_lane="claude_native", provider="claude", model="haiku")

    opts = HandleTurnOptions(roster=_test_roster())
    with patch("cabinet.text_orchestrator.lane_router.run_with_runtime_lanes", side_effect=fake_run) as mock_run:
        result = await handle_text_turn(meeting_id, "thanks", "ack_id", opts=opts)

    assert result.accepted is True
    # Only router_decision + turn_complete should fire — no agent dispatch.
    assert mock_run.call_count == 0


@pytest.mark.asyncio
async def test_handle_text_turn_greeting_routes_to_default(tmp_dashboard_db: Path) -> None:
    meeting_id = _make_meeting()

    captured_personas: list[str] = []

    async def fake_run(req):
        captured_personas.append(req.metadata.get("persona_id") if req.metadata else "")
        return RuntimeResult(text="hi back", runtime_lane="claude_native", provider="claude", model="haiku")

    opts = HandleTurnOptions(roster=_test_roster())
    with patch("cabinet.text_orchestrator.lane_router.run_with_runtime_lanes", side_effect=fake_run):
        await handle_text_turn(meeting_id, "hey", "greet_id", opts=opts)

    assert "default" in captured_personas


@pytest.mark.asyncio
async def test_handle_text_turn_kill_switch_raises(tmp_dashboard_db: Path, monkeypatch) -> None:
    """M7 layer 1 — cabinet kill-switch disabled at function head raises."""
    monkeypatch.setenv("HOMIE_KILLSWITCH_CABINET", "disabled")
    meeting_id = _make_meeting()
    opts = HandleTurnOptions(roster=_test_roster())
    with pytest.raises(KillSwitchDisabled):
        await handle_text_turn(meeting_id, "@seo plan?", "ks_id", opts=opts)


@pytest.mark.asyncio
async def test_handle_text_turn_meeting_ended_returns_error(tmp_dashboard_db: Path) -> None:
    meeting_id = _make_meeting()
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE cabinet_meetings SET ended_at = strftime('%s','now') WHERE id = ?",
            (meeting_id,),
        )
        conn.commit()
    finally:
        conn.close()

    result = await handle_text_turn(meeting_id, "@seo plan?", "x", opts=HandleTurnOptions(roster=_test_roster()))
    assert result.accepted is False
    assert result.error == "meeting_ended"


@pytest.mark.asyncio
async def test_handle_text_turn_threads_tool_policy(tmp_dashboard_db: Path) -> None:
    """B1 + WS1.0 — every per-persona dispatch carries allowed/disallowed/mcp from cabinet_tool_policy."""
    meeting_id = _make_meeting()
    captured_requests: list = []

    async def fake_run(req):
        captured_requests.append(req)
        return RuntimeResult(text="ok", runtime_lane="claude_native", provider="claude", model="haiku")

    # Use a roster persona that opts into a tool.
    roster = [
        RosterAgent(id="default", name="Main", description="host"),
        RosterAgent(id="ops", name="Ops", description="schedules", tools=["Bash", "mcp:gmail"], mcp_servers={"gmail": {}, "asana": {}}),
    ]
    opts = HandleTurnOptions(roster=roster)
    with patch("cabinet.text_orchestrator.lane_router.run_with_runtime_lanes", side_effect=fake_run), \
         patch("cabinet.text_router.lane_router.run_with_runtime_lanes", side_effect=fake_run):
        await handle_text_turn(meeting_id, "@ops schedule it", "tp_id", opts=opts)

    assert len(captured_requests) >= 1
    # The ops persona dispatch carries allowed_tools (Bash + SAFE_READONLY)
    # AND disallowed_tools (Write/Edit/etc.) AND mcp_servers (filtered to gmail).
    ops_request = next(
        r for r in captured_requests
        if r.metadata and r.metadata.get("persona_id") == "ops"
    )
    assert "Bash" in ops_request.allowed_tools
    assert "Read" in ops_request.allowed_tools  # SAFE_READONLY auto-included.
    assert ops_request.disallowed_tools is not None
    assert "Write" in ops_request.disallowed_tools
    assert ops_request.mcp_servers == ["gmail"]
