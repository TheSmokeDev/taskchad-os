"""Mailbox service — send, claim, ack, inbox for inter-agent messaging.

All operations work locally without Mission Control.
Parity oracle: mission-control/src/lib/mailbox.ts
"""

from __future__ import annotations

import dataclasses
import json
import time
import uuid
from datetime import UTC, datetime

from orchestration.contract import DEFAULT_WORKSPACE_ID
from orchestration.db import OrchestrationDB
from orchestration.models import (
    AgentMessage,
    BlockedRequestPayload,
    IdleReadyPayload,
    MessageWithDeliveries,
    SendMessageInput,
    ShutdownRequestPayload,
    TaskAssignmentPayload,
    VerifierFeedbackPayload,
    WorkHandoffPayload,
)
from orchestration.observability import orchestration_span, update_observation


class MailboxService:
    """Framework-owned inter-agent mailbox service."""

    def __init__(self, db: OrchestrationDB):
        self.db = db

    # ── Send ───────────────────────────────────────────────────────────────
    # Parity: mailbox.ts:sendMessage()

    def send_message(
        self,
        inp: SendMessageInput,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> AgentMessage:
        with orchestration_span(
            "mailbox.send_message",
            metadata={
                "from_agent": inp.from_agent,
                "recipient_count": len(inp.recipients),
                "convoy_id": inp.convoy_id,
                "msg_type": inp.msg_type or "direct",
            },
            trace_metadata={"feature_phase": 4},
            expected_exceptions=(ValueError,),
        ):
            if not inp.recipients:
                raise ValueError("At least one recipient is required")

            conn = self.db.conn
            artifact_json = json.dumps(inp.artifact_refs) if inp.artifact_refs else None

            with conn:
                cur = conn.execute(
                    """INSERT INTO agent_messages
                       (workspace_id, convoy_id, thread_id, correlation_id,
                        causation_id, reply_to_message_id, from_agent,
                        message_type, subject, body, artifact_refs, dedupe_key,
                        msg_type)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        workspace_id,
                        inp.convoy_id,
                        inp.thread_id,
                        inp.correlation_id,
                        inp.causation_id,
                        inp.reply_to_message_id,
                        inp.from_agent,
                        inp.message_type,
                        inp.subject,
                        inp.body,
                        artifact_json,
                        inp.dedupe_key,
                        inp.msg_type,
                    ),
                )
                message_id = cur.lastrowid

                for recipient in inp.recipients:
                    conn.execute(
                        """INSERT INTO agent_deliveries
                           (workspace_id, message_id, recipient_agent)
                           VALUES (?, ?, ?)""",
                        (workspace_id, message_id, recipient),
                    )

            row = conn.execute(
                "SELECT * FROM agent_messages WHERE id = ?",
                (message_id,),
            ).fetchone()
            update_observation(
                metadata={
                    "message_id": message_id,
                    "from_agent": inp.from_agent,
                    "convoy_id": inp.convoy_id,
                    "msg_type": inp.msg_type or "direct",
                }
            )
            return self.db.row_to_message(row)

    # ── Claim ──────────────────────────────────────────────────────────────
    # Parity: mailbox.ts:claimDeliveries()

    def claim_deliveries(
        self,
        agent_id: str,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
        convoy_id: int | None = None,
        limit: int = 10,
    ) -> list[MessageWithDeliveries]:
        conn = self.db.conn
        claim_token = str(uuid.uuid4())
        now = int(time.time())

        query = """
            SELECT d.id as delivery_id, d.message_id
            FROM agent_deliveries d
            JOIN agent_messages m ON m.id = d.message_id
            WHERE d.workspace_id = ? AND d.recipient_agent = ?
              AND d.status = 'pending'
        """
        params: list[int | str] = [workspace_id, agent_id]

        if convoy_id is not None:
            query += " AND m.convoy_id = ?"
            params.append(convoy_id)
        query += " ORDER BY m.created_at ASC LIMIT ?"
        params.append(limit)

        with conn:
            pending = conn.execute(query, params).fetchall()
            if not pending:
                return []

            # Claim deliveries
            claimed_ids: list[int] = []
            for d in pending:
                cur = conn.execute(
                    """UPDATE agent_deliveries
                       SET status = 'claimed', claim_token = ?, claimed_at = ?
                       WHERE id = ? AND status = 'pending'""",
                    (claim_token, now, d["delivery_id"]),
                )
                if cur.rowcount > 0:
                    claimed_ids.append(d["delivery_id"])

            if not claimed_ids:
                return []

            # Fetch claimed messages
            claimed_deliveries = conn.execute(
                """SELECT DISTINCT message_id FROM agent_deliveries
                   WHERE claim_token = ? AND workspace_id = ?""",
                (claim_token, workspace_id),
            ).fetchall()

            messages: list[MessageWithDeliveries] = []
            for cd in claimed_deliveries:
                msg_row = conn.execute(
                    "SELECT * FROM agent_messages WHERE id = ?",
                    (cd["message_id"],),
                ).fetchone()
                del_rows = conn.execute(
                    "SELECT * FROM agent_deliveries WHERE message_id = ?",
                    (cd["message_id"],),
                ).fetchall()
                messages.append(
                    MessageWithDeliveries(
                        message=self.db.row_to_message(msg_row),
                        deliveries=[self.db.row_to_delivery(r) for r in del_rows],
                    )
                )

        return messages

    # ── Ack ────────────────────────────────────────────────────────────────
    # Parity: mailbox.ts:ackDelivery()

    def ack_delivery(
        self,
        delivery_id: int,
        recipient_agent: str,
        claim_token: str,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> None:
        now = int(time.time())
        row = self.db.conn.execute(
            """SELECT * FROM agent_deliveries
               WHERE id = ? AND workspace_id = ?""",
            (delivery_id, workspace_id),
        ).fetchone()
        if not row:
            raise ValueError(f"Delivery {delivery_id} not found")

        delivery = self.db.row_to_delivery(row)
        if delivery.recipient_agent != recipient_agent:
            raise ValueError(
                f"Delivery {delivery_id} is not owned by recipient '{recipient_agent}'"
            )
        if delivery.status != "claimed":
            raise ValueError(
                f"Delivery {delivery_id} must be claimed before it can be acknowledged"
            )
        if not delivery.claim_token or delivery.claim_token != claim_token:
            raise ValueError(f"Delivery {delivery_id} claim token does not match the active claim")

        with self.db.conn:
            self.db.conn.execute(
                """UPDATE agent_deliveries
                   SET status = 'acked', acked_at = ?
                   WHERE id = ? AND workspace_id = ?""",
                (now, delivery_id, workspace_id),
            )

    # ── Inbox (read-only, no claim) ────────────────────────────────────────

    def get_inbox(
        self,
        agent_id: str,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
        convoy_id: int | None = None,
        msg_type: str | None = None,
    ) -> list[MessageWithDeliveries]:
        with orchestration_span(
            "mailbox.get_inbox",
            metadata={"agent_id": agent_id, "convoy_id": convoy_id, "msg_type": msg_type},
            trace_metadata={"feature_phase": 4},
        ):
            conn = self.db.conn

            query = """
                SELECT m.*, d.id as delivery_id
                FROM agent_messages m
                JOIN agent_deliveries d ON d.message_id = m.id
                WHERE d.workspace_id = ? AND d.recipient_agent = ?
                  AND d.status IN ('pending', 'claimed')
            """
            params: list[int | str] = [workspace_id, agent_id]

            if convoy_id is not None:
                query += " AND m.convoy_id = ?"
                params.append(convoy_id)
            if msg_type is not None:
                query += " AND m.msg_type = ?"
                params.append(msg_type)
            query += " ORDER BY m.created_at ASC"

            rows = conn.execute(query, params).fetchall()
            if not rows:
                update_observation(metadata={"agent_id": agent_id, "unread_count": 0, "convoy_id": convoy_id, "msg_type": msg_type})
                return []

            seen_msgs: dict[int, MessageWithDeliveries] = {}
            for row in rows:
                msg_id = row["id"]
                if msg_id not in seen_msgs:
                    msg_row = conn.execute(
                        "SELECT * FROM agent_messages WHERE id = ?",
                        (msg_id,),
                    ).fetchone()
                    del_rows = conn.execute(
                        "SELECT * FROM agent_deliveries WHERE message_id = ?",
                        (msg_id,),
                    ).fetchall()
                    seen_msgs[msg_id] = MessageWithDeliveries(
                        message=self.db.row_to_message(msg_row),
                        deliveries=[self.db.row_to_delivery(r) for r in del_rows],
                    )

            messages = list(seen_msgs.values())
            update_observation(metadata={"agent_id": agent_id, "unread_count": len(messages), "convoy_id": convoy_id, "msg_type": msg_type})
            return messages

    # ── Convoy messages ────────────────────────────────────────────────────
    # Parity: mailbox.ts:getConvoyMessages()

    def get_convoy_messages(
        self,
        convoy_id: int,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> list[MessageWithDeliveries]:
        conn = self.db.conn
        msg_rows = conn.execute(
            """SELECT * FROM agent_messages
               WHERE convoy_id = ? AND workspace_id = ?
               ORDER BY created_at ASC, id ASC""",
            (convoy_id, workspace_id),
        ).fetchall()

        messages: list[MessageWithDeliveries] = []
        for msg_row in msg_rows:
            del_rows = conn.execute(
                "SELECT * FROM agent_deliveries WHERE message_id = ?",
                (msg_row["id"],),
            ).fetchall()
            messages.append(
                MessageWithDeliveries(
                    message=self.db.row_to_message(msg_row),
                    deliveries=[self.db.row_to_delivery(r) for r in del_rows],
                )
            )
        return messages

    # ── Format for dispatch ────────────────────────────────────────────────
    # Parity: mailbox.ts:formatMailForDispatch()

    def format_mail_for_dispatch(
        self,
        agent_id: str,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
        convoy_id: int | None = None,
    ) -> str | None:
        conn = self.db.conn

        query = """
            SELECT m.*, d.id as delivery_id
            FROM agent_messages m
            JOIN agent_deliveries d ON d.message_id = m.id
            WHERE d.workspace_id = ? AND d.recipient_agent = ?
              AND d.status IN ('pending', 'claimed')
        """
        params: list[int | str] = [workspace_id, agent_id]

        if convoy_id is not None:
            query += " AND m.convoy_id = ?"
            params.append(convoy_id)
        query += " ORDER BY m.created_at ASC"

        rows = conn.execute(query, params).fetchall()
        if not rows:
            return None

        lines: list[str] = ["## Unread Messages", ""]
        seen_delivery_ids: list[int] = []
        for row in rows:
            lines.append(f"**From**: {row['from_agent']}")
            if row["subject"]:
                lines.append(f"**Subject**: {row['subject']}")
            lines.append(f"**Type**: {row['message_type']}")
            ts = datetime.fromtimestamp(row["created_at"], tz=UTC).isoformat()
            lines.append(f"**Time**: {ts}")
            lines.append("")
            lines.append(row["body"])
            lines.append("")
            lines.append("---")
            lines.append("")
            seen_delivery_ids.append(row["delivery_id"])

        # Mark as seen
        with conn:
            for did in seen_delivery_ids:
                conn.execute(
                    """UPDATE agent_deliveries SET status = 'seen'
                       WHERE id = ? AND status IN ('pending', 'claimed')""",
                    (did,),
                )

        return "\n".join(lines)

    # ── Typed send helpers (Phase 3) ───────────────────────────────────────
    # Each helper wraps send_message() with a typed msg_type and a JSON body
    # serialized from the typed payload dataclass.

    def _send_typed(
        self,
        from_agent: str,
        recipients: list[str],
        msg_type: str,
        payload: object,
        convoy_id: int | None,
        subject: str | None,
        workspace_id: int,
    ) -> AgentMessage:
        body = json.dumps(dataclasses.asdict(payload))
        return self.send_message(
            SendMessageInput(
                from_agent=from_agent,
                recipients=recipients,
                body=body,
                convoy_id=convoy_id,
                msg_type=msg_type,
                subject=subject,
            ),
            workspace_id=workspace_id,
        )

    def send_task_assignment(
        self,
        from_agent: str,
        to_agent: str,
        payload: TaskAssignmentPayload,
        convoy_id: int | None = None,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> AgentMessage:
        return self._send_typed(
            from_agent, [to_agent], "task_assignment", payload,
            convoy_id, f"task_assignment: {payload.title}", workspace_id,
        )

    def send_work_handoff(
        self,
        from_agent: str,
        to_agent: str,
        payload: WorkHandoffPayload,
        convoy_id: int | None = None,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> AgentMessage:
        return self._send_typed(
            from_agent, [to_agent], "work_handoff", payload,
            convoy_id, f"work_handoff: subtask {payload.subtask_id}", workspace_id,
        )

    def send_blocked_request(
        self,
        from_agent: str,
        to_agent: str,
        payload: BlockedRequestPayload,
        convoy_id: int | None = None,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> AgentMessage:
        return self._send_typed(
            from_agent, [to_agent], "blocked_request", payload,
            convoy_id, "blocked_request", workspace_id,
        )

    def send_shutdown_request(
        self,
        from_agent: str,
        to_agent: str,
        payload: ShutdownRequestPayload | None = None,
        convoy_id: int | None = None,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> AgentMessage:
        return self._send_typed(
            from_agent, [to_agent], "shutdown_request",
            payload or ShutdownRequestPayload(),
            convoy_id, "shutdown_request", workspace_id,
        )

    def send_shutdown_ack(
        self,
        from_agent: str,
        to_agent: str,
        convoy_id: int | None = None,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> AgentMessage:
        return self.send_message(
            SendMessageInput(
                from_agent=from_agent,
                recipients=[to_agent],
                body="{}",
                convoy_id=convoy_id,
                msg_type="shutdown_ack",
                subject="shutdown_ack",
            ),
            workspace_id=workspace_id,
        )

    def send_idle_ready(
        self,
        from_agent: str,
        to_agent: str,
        payload: IdleReadyPayload | None = None,
        convoy_id: int | None = None,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> AgentMessage:
        return self._send_typed(
            from_agent, [to_agent], "idle_ready",
            payload or IdleReadyPayload(),
            convoy_id, "idle_ready", workspace_id,
        )

    def send_verifier_feedback(
        self,
        from_agent: str,
        to_agent: str,
        payload: VerifierFeedbackPayload,
        convoy_id: int | None = None,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> AgentMessage:
        return self._send_typed(
            from_agent, [to_agent], "verifier_feedback", payload,
            convoy_id, f"verifier_feedback: {payload.verdict}", workspace_id,
        )
