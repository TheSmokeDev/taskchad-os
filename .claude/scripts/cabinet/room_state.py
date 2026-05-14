"""Cabinet room state helpers.

The per-meeting roster snapshot is membership/order/display truth. Runtime
execution metadata is still rehydrated from the live Homie profile by the text
orchestrator when an agent actually speaks.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from dashboard_db import get_connection
from security import redact as _redact_mod

if TYPE_CHECKING:
    from .text_orchestrator import RosterAgent

logger = logging.getLogger(__name__)
_redact = _redact_mod.redact


class CabinetRoomStateError(Exception):
    """Base error for room state mutations."""


class CabinetMeetingNotFound(CabinetRoomStateError):
    """The requested meeting row does not exist."""


class CabinetMeetingEnded(CabinetRoomStateError):
    """The requested meeting is already ended."""


class CabinetUnknownAgent(CabinetRoomStateError):
    """The requested agent is not cabinet-eligible in the live roster."""


class CabinetDefaultRemovalRejected(CabinetRoomStateError):
    """The default/main participant cannot be removed from a room."""


def load_meeting_roster(meeting_id: int) -> list[RosterAgent]:
    """Return the meeting roster snapshot, or live roster if unavailable."""
    snapshot = load_meeting_roster_snapshot(meeting_id)
    if snapshot:
        return snapshot
    return _text_orchestrator().get_roster()


def load_meeting_roster_snapshot(meeting_id: int) -> list[RosterAgent] | None:
    """Load ``cabinet_text_meetings.roster_json`` for ``meeting_id``.

    Returns ``None`` when the snapshot row is missing, empty, malformed, or
    contains invalid roster rows. Callers can then use the live roster fallback.
    """
    raw = _load_roster_json(meeting_id)
    if raw is None:
        return None
    roster = _parse_roster_json(raw)
    if not roster:
        logger.debug("cabinet room_state: invalid roster snapshot for meeting_id=%s", meeting_id)
        return None
    return roster


def _load_roster_json(meeting_id: int) -> str | None:
    try:
        conn = get_connection()
        try:
            row = conn.execute(
                """SELECT roster_json
                   FROM cabinet_text_meetings
                   WHERE meeting_id = ?""",
                (meeting_id,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.debug(
            "cabinet room_state: roster snapshot lookup failed for meeting_id=%s: %s",
            meeting_id,
            _redact(str(exc)),
        )
        return None
    if row is None:
        return None
    return row["roster_json"] if isinstance(row, sqlite3.Row) else row[0]


def list_available_agents(meeting_id: int) -> list[RosterAgent]:
    """Return live cabinet-eligible agents not currently in the room."""
    current_ids = {agent.id for agent in load_meeting_roster(meeting_id)}
    return [
        agent
        for agent in _text_orchestrator().get_roster()
        if agent.id not in current_ids
    ]


def add_meeting_participant(meeting_id: int, agent_id: str) -> list[RosterAgent]:
    """Add ``agent_id`` to a meeting roster snapshot and return the roster."""
    canonical_id = _canonical_agent_id(agent_id)
    live_by_id = {agent.id: agent for agent in _text_orchestrator().get_roster()}
    candidate = live_by_id.get(canonical_id)
    if candidate is None:
        raise CabinetUnknownAgent(canonical_id)

    with _meeting_transaction(meeting_id) as conn:
        roster = _load_snapshot_for_update(conn, meeting_id) or list(live_by_id.values())
        if canonical_id not in {agent.id for agent in roster}:
            roster.append(candidate)
        _write_roster_snapshot(conn, meeting_id, roster)
        return roster


def remove_meeting_participant(meeting_id: int, agent_id: str) -> list[RosterAgent]:
    """Remove ``agent_id`` from a meeting roster snapshot and return it."""
    canonical_id = _canonical_agent_id(agent_id)
    if canonical_id == "default":
        raise CabinetDefaultRemovalRejected(canonical_id)

    with _meeting_transaction(meeting_id) as conn:
        roster = _load_snapshot_for_update(conn, meeting_id)
        if roster is None:
            roster = _text_orchestrator().get_roster()
        roster = [agent for agent in roster if agent.id != canonical_id]
        _write_roster_snapshot(conn, meeting_id, roster)
        conn.execute(
            """UPDATE cabinet_meetings
               SET pinned_persona = NULL
               WHERE id = ? AND pinned_persona = ?""",
            (meeting_id, canonical_id),
        )
        conn.execute(
            """UPDATE cabinet_text_meetings
               SET pinned_agent = NULL
               WHERE meeting_id = ? AND pinned_agent = ?""",
            (meeting_id, canonical_id),
        )
        return roster


def roster_to_wire(roster: list[RosterAgent]) -> list[dict[str, str]]:
    """Return display-safe REST/SSE roster rows."""
    return [
        {
            "id": agent.id,
            "name": agent.name,
            "description": agent.description,
        }
        for agent in roster
    ]


def broadcast_order(roster: list[RosterAgent]) -> list[str]:
    """Return stable broadcast ids for ``cabinet_meetings.broadcast_order``."""
    return [agent.id for agent in roster if agent.id]


def _canonical_agent_id(agent_id: str) -> str:
    value = (agent_id or "").strip()
    if value == "main":
        return "default"
    return value


class _meeting_transaction:
    def __init__(self, meeting_id: int):
        self.meeting_id = meeting_id
        self.conn: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        conn = get_connection()
        self.conn = conn
        row = conn.execute(
            "SELECT id, ended_at FROM cabinet_meetings WHERE id = ?",
            (self.meeting_id,),
        ).fetchone()
        if row is None:
            conn.close()
            self.conn = None
            raise CabinetMeetingNotFound(self.meeting_id)
        if row["ended_at"] is not None:
            conn.close()
            self.conn = None
            raise CabinetMeetingEnded(self.meeting_id)
        conn.execute("BEGIN IMMEDIATE")
        return conn

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.conn is None:
            return
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()


def _load_snapshot_for_update(
    conn: sqlite3.Connection,
    meeting_id: int,
) -> list[RosterAgent] | None:
    row = conn.execute(
        """SELECT roster_json
           FROM cabinet_text_meetings
           WHERE meeting_id = ?""",
        (meeting_id,),
    ).fetchone()
    if row is None:
        return None
    raw = row["roster_json"] if isinstance(row, sqlite3.Row) else row[0]
    return _parse_roster_json(raw)


def _write_roster_snapshot(
    conn: sqlite3.Connection,
    meeting_id: int,
    roster: list[RosterAgent],
) -> None:
    roster_json = json.dumps([_snapshot_row(agent) for agent in roster])
    order_json = json.dumps(broadcast_order(roster))
    conn.execute(
        """INSERT INTO cabinet_text_meetings (meeting_id, roster_json)
           VALUES (?, ?)
           ON CONFLICT(meeting_id) DO UPDATE SET roster_json = excluded.roster_json""",
        (meeting_id, roster_json),
    )
    conn.execute(
        "UPDATE cabinet_meetings SET broadcast_order = ? WHERE id = ?",
        (order_json, meeting_id),
    )


def _snapshot_row(agent: RosterAgent) -> dict[str, Any]:
    data = asdict(agent)
    return {
        "id": data["id"],
        "name": data["name"],
        "description": data.get("description") or "",
    }


def _parse_roster_json(raw: str) -> list[RosterAgent] | None:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, list) or not payload:
        return None

    roster: list[RosterAgent] = []
    for item in payload:
        agent = _agent_from_snapshot_row(item)
        if agent is None:
            return None
        roster.append(agent)
    return roster or None


def _agent_from_snapshot_row(item: Any) -> RosterAgent | None:
    if not isinstance(item, dict):
        return None
    agent_id = item.get("id")
    if not isinstance(agent_id, str) or not agent_id.strip():
        return None
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        name = agent_id
    description = item.get("description")
    if not isinstance(description, str):
        description = ""
    tools_raw = item.get("tools")
    tools = list(tools_raw) if isinstance(tools_raw, list) else []
    mcp_servers_raw = item.get("mcp_servers", item.get("mcpServers"))
    mcp_servers = dict(mcp_servers_raw) if isinstance(mcp_servers_raw, dict) else {}
    auth_profile = item.get("auth_profile", item.get("authProfile"))
    if not isinstance(auth_profile, str):
        auth_profile = None

    return _text_orchestrator().RosterAgent(
        id=agent_id.strip(),
        name=name.strip(),
        description=description,
        tools=tools,
        mcp_servers=mcp_servers,
        auth_profile=auth_profile,
    )


def _text_orchestrator():
    from . import text_orchestrator as _text_orch

    return _text_orch


__all__ = [
    "CabinetDefaultRemovalRejected",
    "CabinetMeetingEnded",
    "CabinetMeetingNotFound",
    "CabinetRoomStateError",
    "CabinetUnknownAgent",
    "add_meeting_participant",
    "broadcast_order",
    "list_available_agents",
    "load_meeting_roster",
    "load_meeting_roster_snapshot",
    "remove_meeting_participant",
    "roster_to_wire",
]
