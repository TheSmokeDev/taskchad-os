"""Team service — DB-backed team session tracking on top of convoy + mailbox.

Team sessions LINK to convoys (the work graph) but don't replace them.
Convoy owns subtask ownership and dependency edges. TeamService tracks
which agents are actively in a team, their role (leader/worker), and the
team's runtime lifecycle (active/idle/shutdown_requested/closed).
"""

from __future__ import annotations

import sqlite3
import time

from orchestration.contract import (
    DEFAULT_WORKSPACE_ID,
    TERMINAL_TEAM_STATUSES,
)
from orchestration.db import OrchestrationDB
from orchestration.models import (
    AddTeamMemberInput,
    CreateTeamSessionInput,
    ExecutorReceipt,
    TeamMember,
    TeamSession,
    TeamSessionWithMembers,
)
from orchestration.observability import orchestration_span, update_observation


class TeamService:
    """Framework-owned team session service."""

    def __init__(self, db: OrchestrationDB):
        self.db = db

    # ── Create ─────────────────────────────────────────────────────────────

    def create_team_session(
        self,
        inp: CreateTeamSessionInput,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> TeamSessionWithMembers:
        with orchestration_span(
            "team_service.create_team_session",
            metadata={
                "team_name": inp.team_name,
                "lead_agent_id": inp.lead_agent_id,
                "convoy_id": inp.convoy_id,
                "requested_backend": inp.backend_type,
            },
            trace_metadata={"feature_phase": 4},
            expected_exceptions=(ValueError,),
        ):
            if not inp.team_name:
                raise ValueError("team_name is required")
            if not inp.lead_agent_id:
                raise ValueError("lead_agent_id is required")

            conn = self.db.conn
            now = int(time.time())
            try:
                with conn:
                    cur = conn.execute(
                        """INSERT INTO team_sessions
                           (workspace_id, convoy_id, team_name, lead_agent_id,
                            lead_agent_name, backend_type, last_activity_at, metadata)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            workspace_id,
                            inp.convoy_id,
                            inp.team_name,
                            inp.lead_agent_id,
                            inp.lead_agent_name,
                            inp.backend_type,
                            now,
                            inp.metadata,
                        ),
                    )
                    team_id = cur.lastrowid

                    conn.execute(
                        """INSERT INTO team_members
                           (workspace_id, team_session_id, agent_id, agent_name,
                            role, status, last_activity_at)
                           VALUES (?, ?, ?, ?, 'leader', 'active', ?)""",
                        (
                            workspace_id,
                            team_id,
                            inp.lead_agent_id,
                            inp.lead_agent_name,
                            now,
                        ),
                    )
            except sqlite3.IntegrityError as e:
                raise ValueError(f"Invalid input - constraint violation: {e}") from e

            result = self.get_team_session(team_id, workspace_id=workspace_id)
            if result is None:
                raise RuntimeError(f"Failed to read back team session {team_id}")
            update_observation(
                metadata={
                    "team_id": result.session.id,
                    "team_name": result.session.team_name,
                    "convoy_id": result.session.convoy_id,
                    "actual_backend": result.session.backend_type,
                }
            )
            return result

    # ── Read ───────────────────────────────────────────────────────────────

    def get_team_session(
        self, team_id: int, workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> TeamSessionWithMembers | None:
        with orchestration_span(
            "team_service.get_team_session",
            metadata={"team_id": team_id},
            trace_metadata={"feature_phase": 5, "team_id": team_id},
        ):
            conn = self.db.conn
            row = conn.execute(
                "SELECT * FROM team_sessions WHERE id = ? AND workspace_id = ?",
                (team_id, workspace_id),
            ).fetchone()
            if not row:
                update_observation(metadata={"team_id": team_id, "found": False})
                return None
            session = self.db.row_to_team_session(row)
            member_rows = conn.execute(
                """SELECT * FROM team_members
                   WHERE team_session_id = ? AND workspace_id = ?
                   ORDER BY id""",
                (team_id, workspace_id),
            ).fetchall()
            members = [self.db.row_to_team_member(r) for r in member_rows]
            update_observation(
                metadata={
                    "team_id": team_id,
                    "team_name": session.team_name,
                    "convoy_id": session.convoy_id,
                    "member_count": len(members),
                    "found": True,
                }
            )
            return TeamSessionWithMembers(session=session, members=members)

    def list_team_sessions(
        self,
        status: str | None = None,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> list[TeamSession]:
        conn = self.db.conn
        if status:
            rows = conn.execute(
                """SELECT * FROM team_sessions
                   WHERE workspace_id = ? AND status = ?
                   ORDER BY id DESC""",
                (workspace_id, status),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM team_sessions
                   WHERE workspace_id = ?
                   ORDER BY id DESC""",
                (workspace_id,),
            ).fetchall()
        return [self.db.row_to_team_session(r) for r in rows]

    # ── Members ────────────────────────────────────────────────────────────

    def add_member(
        self,
        team_id: int,
        inp: AddTeamMemberInput,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> TeamMember:
        with orchestration_span(
            "team_service.add_member",
            metadata={
                "team_id": team_id,
                "agent_id": inp.agent_id,
                "role": inp.role,
                "subtask_id": inp.subtask_id,
            },
            trace_metadata={"feature_phase": 4, "team_id": team_id},
            expected_exceptions=(ValueError,),
        ):
            if not inp.agent_id:
                raise ValueError("agent_id is required")
            existing = self.get_team_session(team_id, workspace_id=workspace_id)
            if existing is None:
                raise ValueError(f"Team session {team_id} not found")
            if existing.session.status in TERMINAL_TEAM_STATUSES:
                raise ValueError(
                    f"Cannot add member to team in terminal status '{existing.session.status}'"
                )

            conn = self.db.conn
            now = int(time.time())
            try:
                with conn:
                    cur = conn.execute(
                        """INSERT INTO team_members
                           (workspace_id, team_session_id, agent_id, agent_name,
                            role, subtask_id, status, last_activity_at)
                           VALUES (?, ?, ?, ?, ?, ?, 'active', ?)""",
                        (
                            workspace_id,
                            team_id,
                            inp.agent_id,
                            inp.agent_name,
                            inp.role,
                            inp.subtask_id,
                            now,
                        ),
                    )
                    member_id = cur.lastrowid
            except sqlite3.IntegrityError as e:
                err = str(e).upper()
                if "UNIQUE" in err:
                    raise ValueError(
                        f"Agent '{inp.agent_id}' is already a member of team {team_id}"
                    ) from e
                raise ValueError(f"Invalid input - constraint violation: {e}") from e

            row = conn.execute(
                "SELECT * FROM team_members WHERE id = ?", (member_id,),
            ).fetchone()
            member = self.db.row_to_team_member(row)
            update_observation(
                metadata={
                    "team_id": team_id,
                    "agent_id": member.agent_id,
                    "role": member.role,
                    "subtask_id": member.subtask_id,
                }
            )
            return member

    def update_member_status(
        self,
        team_id: int,
        agent_id: str,
        status: str,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> TeamMember:
        if status not in ("active", "idle", "closed"):
            raise ValueError(f"Invalid member status '{status}'")
        conn = self.db.conn
        now = int(time.time())
        with conn:
            cur = conn.execute(
                """UPDATE team_members
                   SET status = ?, last_activity_at = ?
                   WHERE team_session_id = ? AND agent_id = ? AND workspace_id = ?""",
                (status, now, team_id, agent_id, workspace_id),
            )
            if cur.rowcount == 0:
                raise ValueError(
                    f"Member '{agent_id}' not found in team {team_id}"
                )
        row = conn.execute(
            """SELECT * FROM team_members
               WHERE team_session_id = ? AND agent_id = ? AND workspace_id = ?""",
            (team_id, agent_id, workspace_id),
        ).fetchone()
        return self.db.row_to_team_member(row)

    # ── Activity ───────────────────────────────────────────────────────────

    def ping_activity(
        self,
        team_id: int,
        agent_id: str | None = None,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> None:
        with orchestration_span(
            "team_service.ping_activity",
            metadata={"team_id": team_id, "agent_id": agent_id},
            trace_metadata={"feature_phase": 4, "team_id": team_id},
            expected_exceptions=(ValueError,),
        ):
            conn = self.db.conn
            now = int(time.time())
            with conn:
                cur = conn.execute(
                    """UPDATE team_sessions
                       SET last_activity_at = ?, updated_at = ?
                       WHERE id = ? AND workspace_id = ?""",
                    (now, now, team_id, workspace_id),
                )
                if cur.rowcount == 0:
                    raise ValueError(f"Team session {team_id} not found")
                if agent_id is not None:
                    conn.execute(
                        """UPDATE team_members
                           SET last_activity_at = ?
                           WHERE team_session_id = ? AND agent_id = ? AND workspace_id = ?""",
                        (now, team_id, agent_id, workspace_id),
                    )
            update_observation(metadata={"team_id": team_id, "agent_id": agent_id})

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def request_shutdown(
        self, team_id: int, workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> TeamSession:
        with orchestration_span(
            "team_service.request_shutdown",
            metadata={"team_id": team_id, "msg_type": "shutdown_request"},
            trace_metadata={"feature_phase": 5, "team_id": team_id},
            expected_exceptions=(ValueError,),
        ):
            conn = self.db.conn
            now = int(time.time())
            with conn:
                cur = conn.execute(
                    """UPDATE team_sessions
                       SET status = 'shutdown_requested',
                           shutdown_requested_at = ?,
                           updated_at = ?
                       WHERE id = ? AND workspace_id = ?
                         AND status NOT IN ('shutdown_requested', 'closed')""",
                    (now, now, team_id, workspace_id),
                )
                if cur.rowcount == 0:
                    existing_row = conn.execute(
                        "SELECT * FROM team_sessions WHERE id = ? AND workspace_id = ?",
                        (team_id, workspace_id),
                    ).fetchone()
                    if not existing_row:
                        raise ValueError(f"Team session {team_id} not found")
                    result = self.db.row_to_team_session(existing_row)
                    update_observation(metadata={"team_id": team_id, "final_status": result.status})
                    return result
            row = conn.execute(
                "SELECT * FROM team_sessions WHERE id = ? AND workspace_id = ?",
                (team_id, workspace_id),
            ).fetchone()
            result = self.db.row_to_team_session(row)
            update_observation(metadata={"team_id": team_id, "final_status": result.status})
            return result

    # ── Dispatch (Phase 6) ────────────────────────────────────────────────

    def dispatch_to_executor(
        self,
        team_id: int,
        subtask_id: int,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> tuple[ExecutorReceipt, str]:
        """Dispatch a subtask via the team's configured backend, with fallback.

        Returns (receipt, actual_backend) where actual_backend may differ
        from team.backend_type when the fallback chain kicked in.

        Raises ValueError if the subtask does not belong to the team's convoy.
        """
        from orchestration.convoy_service import ConvoyService
        from orchestration.executor import ExecutorRegistry

        with orchestration_span(
            "team_service.dispatch_to_executor",
            metadata={"team_id": team_id, "subtask_id": subtask_id},
            trace_metadata={"feature_phase": 6, "team_id": team_id, "subtask_id": subtask_id},
            expected_exceptions=(ValueError,),
        ):
            session = self.get_team_session(team_id, workspace_id=workspace_id)
            if session is None:
                raise ValueError(f"Team {team_id} not found")

            if session.session.convoy_id is None:
                raise ValueError(
                    f"Team {team_id} has no convoy_id set - "
                    "dispatch_to_executor requires the team to be bound to a convoy"
                )
            subtask_row = self.db.conn.execute(
                "SELECT convoy_id FROM subtasks WHERE id = ?", (subtask_id,)
            ).fetchone()
            if subtask_row is None:
                raise ValueError(f"Subtask {subtask_id} not found")
            if subtask_row["convoy_id"] != session.session.convoy_id:
                raise ValueError(
                    f"Subtask {subtask_id} belongs to convoy "
                    f"{subtask_row['convoy_id']}, not team {team_id}'s "
                    f"convoy {session.session.convoy_id}"
                )

            registry = ExecutorRegistry.default()
            executor, actual_backend = registry.backend_selector.select(
                session.session.backend_type,
                workspace_id=workspace_id,
            )

            convoy_svc = ConvoyService(self.db)
            receipt = convoy_svc.dispatch_subtask(subtask_id, workspace_id=workspace_id, executor=executor)
            update_observation(
                metadata={
                    "team_id": team_id,
                    "convoy_id": session.session.convoy_id,
                    "subtask_id": subtask_id,
                    "requested_backend": session.session.backend_type,
                    "actual_backend": actual_backend,
                    "fallback_used": actual_backend != session.session.backend_type,
                },
                output={"status": receipt.status},
            )
            return receipt, actual_backend

    def close_team_session(
        self, team_id: int, workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> TeamSession:
        with orchestration_span(
            "team_service.close_team_session",
            metadata={"team_id": team_id},
            trace_metadata={"feature_phase": 5, "team_id": team_id},
            expected_exceptions=(ValueError,),
        ):
            conn = self.db.conn
            now = int(time.time())
            with conn:
                cur = conn.execute(
                    """UPDATE team_sessions
                       SET status = 'closed', closed_at = ?, updated_at = ?
                       WHERE id = ? AND workspace_id = ? AND status != 'closed'""",
                    (now, now, team_id, workspace_id),
                )
                if cur.rowcount == 0:
                    existing_row = conn.execute(
                        "SELECT * FROM team_sessions WHERE id = ? AND workspace_id = ?",
                        (team_id, workspace_id),
                    ).fetchone()
                    if not existing_row:
                        raise ValueError(f"Team session {team_id} not found")
                    result = self.db.row_to_team_session(existing_row)
                    update_observation(metadata={"team_id": team_id, "final_status": result.status})
                    return result
                conn.execute(
                    """UPDATE team_members
                       SET status = 'closed', last_activity_at = ?
                       WHERE team_session_id = ? AND workspace_id = ?
                         AND status != 'closed'""",
                    (now, team_id, workspace_id),
                )
            row = conn.execute(
                "SELECT * FROM team_sessions WHERE id = ? AND workspace_id = ?",
                (team_id, workspace_id),
            ).fetchone()
            result = self.db.row_to_team_session(row)
            update_observation(metadata={"team_id": team_id, "final_status": result.status})
            return result
