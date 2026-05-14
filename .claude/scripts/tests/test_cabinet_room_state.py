"""Focused tests for Cabinet meeting roster snapshots."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import config
from cabinet import meeting_channel as channels_mod
from cabinet.room_state import (
    add_meeting_participant,
    list_available_agents,
    load_meeting_roster,
    remove_meeting_participant,
)
from cabinet.text_orchestrator import HandleTurnOptions, RosterAgent, handle_text_turn
from dashboard_db import get_connection
from runtime.base import RuntimeResult


@pytest.fixture
def tmp_dashboard_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "dashboard.db"
    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", str(db_path))
    conn = get_connection()
    conn.close()
    return db_path


@pytest.fixture(autouse=True)
def _reset_channels() -> None:
    channels_mod._reset_channels()
    yield
    channels_mod._reset_channels()


def _make_meeting(roster_snapshot: list[dict] | str | None = None) -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO cabinet_meetings (mode, chat_id) VALUES (?, ?)",
            ("text", "test-chat"),
        )
        meeting_id = cur.lastrowid
        if roster_snapshot is not None:
            raw = (
                roster_snapshot
                if isinstance(roster_snapshot, str)
                else json.dumps(roster_snapshot)
            )
            conn.execute(
                """INSERT INTO cabinet_text_meetings (meeting_id, roster_json)
                   VALUES (?, ?)""",
                (meeting_id, raw),
            )
        conn.commit()
        return meeting_id
    finally:
        conn.close()


def _snapshot_roster() -> list[dict]:
    return [
        {"id": "default", "name": "Main", "description": "host"},
        {"id": "sales", "name": "Sales", "description": "pipeline"},
    ]


def _live_roster() -> list[RosterAgent]:
    return [
        RosterAgent(id="default", name="Main", description="host"),
        RosterAgent(id="ops", name="Ops", description="ops"),
    ]


def _explicit_roster() -> list[RosterAgent]:
    return [
        RosterAgent(id="default", name="Main", description="host"),
        RosterAgent(id="ops", name="Ops", description="explicit"),
    ]


def test_load_meeting_roster_prefers_snapshot_over_live(tmp_dashboard_db: Path) -> None:
    meeting_id = _make_meeting(_snapshot_roster())

    with patch("cabinet.text_orchestrator.get_roster", return_value=_live_roster()) as live:
        roster = load_meeting_roster(meeting_id)

    assert [agent.id for agent in roster] == ["default", "sales"]
    live.assert_not_called()


@pytest.mark.parametrize("snapshot", [None, "not-json", "[]", "[{}]"])
def test_load_meeting_roster_falls_back_for_missing_or_malformed_snapshot(
    tmp_dashboard_db: Path,
    snapshot: str | None,
) -> None:
    meeting_id = _make_meeting(snapshot)

    with patch("cabinet.text_orchestrator.get_roster", return_value=_live_roster()) as live:
        roster = load_meeting_roster(meeting_id)

    assert [agent.id for agent in roster] == ["default", "ops"]
    live.assert_called_once_with()


def test_participant_add_remove_updates_snapshot_and_broadcast_order(
    tmp_dashboard_db: Path,
) -> None:
    meeting_id = _make_meeting(_snapshot_roster())

    with patch("cabinet.text_orchestrator.get_roster", return_value=_live_roster()):
        available = list_available_agents(meeting_id)
        assert [agent.id for agent in available] == ["ops"]

        roster = add_meeting_participant(meeting_id, "ops")
        assert [agent.id for agent in roster] == ["default", "sales", "ops"]

        roster = remove_meeting_participant(meeting_id, "sales")
        assert [agent.id for agent in roster] == ["default", "ops"]

    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT ctm.roster_json, cm.broadcast_order
               FROM cabinet_text_meetings ctm
               JOIN cabinet_meetings cm ON cm.id = ctm.meeting_id
               WHERE ctm.meeting_id = ?""",
            (meeting_id,),
        ).fetchone()
    finally:
        conn.close()

    assert [item["id"] for item in json.loads(row["roster_json"])] == ["default", "ops"]
    assert json.loads(row["broadcast_order"]) == ["default", "ops"]


@pytest.mark.asyncio
async def test_handle_text_turn_uses_meeting_snapshot_when_opts_roster_absent(
    tmp_dashboard_db: Path,
) -> None:
    meeting_id = _make_meeting(_snapshot_roster())
    captured_personas: list[str] = []

    async def fake_run(req):
        if req.metadata:
            captured_personas.append(req.metadata.get("persona_id", ""))
        return RuntimeResult(
            text="Pipeline is healthy.",
            runtime_lane="claude_native",
            provider="claude",
            model="haiku",
        )

    with patch("cabinet.text_orchestrator.get_roster", return_value=_live_roster()), \
         patch("cabinet.text_orchestrator.lane_router.run_with_runtime_lanes", side_effect=fake_run), \
         patch("cabinet.text_router.lane_router.run_with_runtime_lanes", side_effect=fake_run):
        result = await handle_text_turn(
            meeting_id=meeting_id,
            user_text="@sales what is pipeline state?",
            client_msg_id="snapshot_turn",
        )

    assert result.accepted is True
    assert "sales" in captured_personas
    assert "ops" not in captured_personas


@pytest.mark.asyncio
async def test_handle_text_turn_explicit_roster_still_wins(
    tmp_dashboard_db: Path,
) -> None:
    meeting_id = _make_meeting(_snapshot_roster())
    captured_personas: list[str] = []

    async def fake_run(req):
        if req.metadata:
            captured_personas.append(req.metadata.get("persona_id", ""))
        return RuntimeResult(
            text="Ops has it.",
            runtime_lane="claude_native",
            provider="claude",
            model="haiku",
        )

    with patch(
        "cabinet.text_orchestrator._room_state.load_meeting_roster",
        side_effect=AssertionError("snapshot loader should not run"),
    ), patch("cabinet.text_orchestrator.lane_router.run_with_runtime_lanes", side_effect=fake_run), \
         patch("cabinet.text_router.lane_router.run_with_runtime_lanes", side_effect=fake_run):
        result = await handle_text_turn(
            meeting_id=meeting_id,
            user_text="@ops take this",
            client_msg_id="explicit_roster_turn",
            opts=HandleTurnOptions(roster=_explicit_roster()),
        )

    assert result.accepted is True
    assert "ops" in captured_personas
    assert "sales" not in captured_personas
