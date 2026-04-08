"""Convoy service — create, list, dispatch, complete, fail, status transitions.

All operations work locally without Mission Control.
Parity oracle: mission-control/src/lib/convoy.ts
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

from orchestration.contract import (
    CONVOY_TRANSITIONS,
    DEFAULT_WORKSPACE_ID,
    POST_TERMINAL_FIELDS,
    SUBTASK_TRANSITIONS,
    TERMINAL_CONVOY_STATUSES,
    TERMINAL_SUBTASK_STATUSES,
    UPDATABLE_SUBTASK_FIELDS,
    ConvoyStatus,
    SubtaskStatus,
)
from orchestration.db import OrchestrationDB
from orchestration.models import (
    AddSubtaskInput,
    Convoy,
    ConvoyWithSubtasks,
    CreateConvoyInput,
    DependencyEdge,
    ExecutorReceipt,
    ProgressReport,
    Subtask,
)


class ConvoyService:
    """Framework-owned convoy orchestration service."""

    def __init__(self, db: OrchestrationDB):
        self.db = db

    # ── Create ─────────────────────────────────────────────────────────────
    # Parity: convoy.ts:createConvoy()

    def create_convoy(
        self,
        inp: CreateConvoyInput,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> ConvoyWithSubtasks:
        conn = self.db.conn
        with conn:
            cur = conn.execute(
                """INSERT INTO convoys
                   (workspace_id, title, description, created_by, base_branch,
                    repo_path, merge_strategy, decomposition_mode, total_subtasks)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    workspace_id,
                    inp.title,
                    inp.description,
                    inp.created_by,
                    inp.base_branch,
                    inp.repo_path,
                    inp.merge_strategy,
                    inp.decomposition_mode,
                    len(inp.subtasks),
                ),
            )
            convoy_id = cur.lastrowid

            subtask_ids: list[int] = []
            edges: list[DependencyEdge] = []

            if inp.subtasks:
                # First pass: insert subtasks
                for i, s in enumerate(inp.subtasks):
                    dep_count = len(s.depends_on_subtask_indexes)
                    sc = conn.execute(
                        """INSERT INTO subtasks
                           (convoy_id, workspace_id, title, description,
                            assigned_agent_id, assigned_agent_name,
                            remaining_dependencies, seq, metadata)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            convoy_id,
                            workspace_id,
                            s.title,
                            s.description,
                            s.assigned_agent_id,
                            s.assigned_agent_name,
                            dep_count,
                            i,
                            s.metadata,
                        ),
                    )
                    subtask_ids.append(sc.lastrowid)

                # Second pass: insert edges
                for i, s in enumerate(inp.subtasks):
                    for dep_idx in s.depends_on_subtask_indexes:
                        if dep_idx < 0 or dep_idx >= len(subtask_ids):
                            raise ValueError(
                                f"Invalid dependency index {dep_idx} for subtask '{s.title}'"
                            )
                        from_id = subtask_ids[dep_idx]
                        to_id = subtask_ids[i]
                        ec = conn.execute(
                            """INSERT INTO dependency_edges
                               (workspace_id, convoy_id, from_subtask_id, to_subtask_id)
                               VALUES (?, ?, ?, ?)""",
                            (workspace_id, convoy_id, from_id, to_id),
                        )
                        edges.append(
                            DependencyEdge(
                                id=ec.lastrowid,
                                workspace_id=workspace_id,
                                convoy_id=convoy_id,
                                from_subtask_id=from_id,
                                to_subtask_id=to_id,
                            )
                        )

                # Validate no cycles
                if self._detect_cycle(convoy_id):
                    raise ValueError("Dependency cycle detected in convoy subtasks")

                # Mark zero-dep subtasks as ready
                conn.execute(
                    """UPDATE subtasks SET status = 'ready'
                       WHERE convoy_id = ? AND remaining_dependencies = 0""",
                    (convoy_id,),
                )

            convoy = self._get_convoy_row(convoy_id)
            subtasks = self._get_subtask_rows(convoy_id)
            return ConvoyWithSubtasks(convoy=convoy, subtasks=subtasks, edges=edges)

    # ── Read ───────────────────────────────────────────────────────────────
    # Parity: convoy.ts:getConvoy(), listConvoys(), getReadySubtasks()

    def get_convoy(
        self,
        convoy_id: int,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> ConvoyWithSubtasks | None:
        row = self.db.conn.execute(
            "SELECT * FROM convoys WHERE id = ? AND workspace_id = ?",
            (convoy_id, workspace_id),
        ).fetchone()
        if not row:
            return None
        convoy = self.db.row_to_convoy(row)
        subtasks = self._get_subtask_rows(convoy_id)
        edges = self._get_edge_rows(convoy_id)
        return ConvoyWithSubtasks(convoy=convoy, subtasks=subtasks, edges=edges)

    def list_convoys(
        self,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
        status: ConvoyStatus | None = None,
    ) -> list[Convoy]:
        if status:
            rows = self.db.conn.execute(
                "SELECT * FROM convoys WHERE workspace_id = ? AND status = ? ORDER BY updated_at DESC",
                (workspace_id, status),
            ).fetchall()
        else:
            rows = self.db.conn.execute(
                "SELECT * FROM convoys WHERE workspace_id = ? ORDER BY updated_at DESC",
                (workspace_id,),
            ).fetchall()
        return [self.db.row_to_convoy(r) for r in rows]

    def get_ready_subtasks(self, convoy_id: int) -> list[Subtask]:
        # Parity: convoy.ts:getReadySubtasks()
        rows = self.db.conn.execute(
            """SELECT * FROM subtasks
               WHERE convoy_id = ? AND status = 'ready'
                 AND remaining_dependencies = 0
               ORDER BY seq""",
            (convoy_id,),
        ).fetchall()
        return [self.db.row_to_subtask(r) for r in rows]

    def get_subtask(
        self,
        subtask_id: int,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> Subtask | None:
        """Fetch a single subtask by id."""
        row = self.db.conn.execute(
            "SELECT * FROM subtasks WHERE id = ? AND workspace_id = ?",
            (subtask_id, workspace_id),
        ).fetchone()
        if not row:
            return None
        return self.db.row_to_subtask(row)

    # ── Dispatch ───────────────────────────────────────────────────────────
    # Parity: convoy.ts:dispatchSubtask()
    # Phase 4: executor adapter boundary — optional executor dispatches
    # downstream. Framework DB state is updated regardless of executor result.

    def dispatch_subtask(
        self,
        subtask_id: int,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
        paperclip_issue_id: str | None = None,
        executor: ExecutorAdapter | None = None,
    ) -> ExecutorReceipt:
        """Dispatch a subtask, optionally through an executor adapter.

        Returns an ExecutorReceipt. The framework records an attempt in all
        cases, but only transitions the subtask to `dispatched` when the
        selected executor explicitly accepts the request.
        """
        from orchestration.executor import LocalExecutor

        conn = self.db.conn
        row = conn.execute(
            "SELECT * FROM subtasks WHERE id = ? AND workspace_id = ?",
            (subtask_id, workspace_id),
        ).fetchone()
        if not row:
            raise ValueError(f"Subtask {subtask_id} not found")
        subtask = self.db.row_to_subtask(row)
        if subtask.status != "ready":
            raise ValueError(f"Subtask {subtask_id} is not ready (status: {subtask.status})")

        now = int(time.time())

        # CAS: claim the subtask BEFORE calling the external executor.
        # Concurrent callers that both pass the early status read race here;
        # only one wins the UPDATE. The loser is rejected without triggering
        # an external dispatch — preventing duplicate backend work.
        with conn:
            cur = conn.execute(
                """UPDATE subtasks
                   SET status = 'dispatched', dispatched_at = ?, updated_at = ?
                   WHERE id = ? AND workspace_id = ? AND status = 'ready'""",
                (now, now, subtask_id, workspace_id),
            )
        if cur.rowcount != 1:
            raise ValueError(
                f"Subtask {subtask_id} was already dispatched (concurrent claim)"
            )

        # Count attempts AFTER claiming — no race on next_attempt numbering
        next_attempt = (
            conn.execute(
                """SELECT COUNT(*) as cnt FROM attempts
                   WHERE workspace_id = ? AND convoy_id = ? AND subtask_id = ? AND action = 'dispatch'""",
                (workspace_id, subtask.convoy_id, subtask_id),
            ).fetchone()["cnt"]
            + 1
        )
        attempt_key = f"convoy:{subtask.convoy_id}:subtask:{subtask_id}:attempt:{next_attempt}"

        # Resolve executor — default to LocalExecutor
        if executor is None:
            executor = LocalExecutor()

        # External dispatch — only one caller reaches here (CAS guard above).
        # If executor.dispatch() raises, we must roll back the claim so the
        # subtask is not left stuck in 'dispatched' with no attempt recorded.
        try:
            receipt = executor.dispatch(subtask)
        except Exception:
            rollback_ts = int(time.time())
            with conn:
                conn.execute(
                    """UPDATE subtasks
                       SET status = 'ready', dispatched_at = NULL, updated_at = ?
                       WHERE id = ?""",
                    (rollback_ts, subtask_id),
                )
            raise

        # Determine external ref: explicit paperclip_issue_id takes precedence
        external_ref = paperclip_issue_id or receipt.external_ref

        with conn:
            # Record attempt
            attempt_status = "sent" if receipt.status == "accepted" else "failed"
            conn.execute(
                """INSERT INTO attempts
                   (workspace_id, convoy_id, subtask_id, attempt_key, action, status,
                    paperclip_issue_id, error_message)
                   VALUES (?, ?, ?, ?, 'dispatch', ?, ?, ?)""",
                (
                    workspace_id,
                    subtask.convoy_id,
                    subtask_id,
                    attempt_key,
                    attempt_status,
                    external_ref,
                    receipt.error,
                ),
            )

            if receipt.status == "accepted":
                # Set external ref on the already-claimed subtask
                conn.execute(
                    "UPDATE subtasks SET paperclip_issue_id = ?, updated_at = ? WHERE id = ?",
                    (external_ref, now, subtask_id),
                )
                # Activate convoy if still draft
                convoy_row = conn.execute(
                    "SELECT status FROM convoys WHERE id = ?",
                    (subtask.convoy_id,),
                ).fetchone()
                if convoy_row and convoy_row["status"] == "draft":
                    conn.execute(
                        """UPDATE convoys
                           SET status = 'active', started_at = ?, updated_at = ?
                           WHERE id = ?""",
                        (now, now, subtask.convoy_id),
                    )
            else:
                # Executor rejected — roll back the claim to 'ready'
                conn.execute(
                    """UPDATE subtasks
                       SET status = 'ready', dispatched_at = NULL, updated_at = ?
                       WHERE id = ?""",
                    (now, subtask_id),
                )

        return receipt

    # ── Progress Reporting ────────────────────────────────────────────────
    # Phase 4: executor adapters can report in-flight progress.

    def report_progress(
        self,
        subtask_id: int,
        progress: ProgressReport,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> None:
        """Record an in-flight progress update from an executor.

        Updates the subtask metadata with the latest progress. Does not
        change subtask status — only the executor receipt on completion/
        failure triggers a status transition.
        """
        import json

        conn = self.db.conn
        row = conn.execute(
            "SELECT * FROM subtasks WHERE id = ? AND workspace_id = ?",
            (subtask_id, workspace_id),
        ).fetchone()
        if not row:
            raise ValueError(f"Subtask {subtask_id} not found")

        if row["status"] in TERMINAL_SUBTASK_STATUSES:
            raise ValueError(
                f"Cannot report progress on terminal subtask (status: {row['status']})"
            )

        # Store progress in subtask metadata
        existing_meta = json.loads(row["metadata"]) if row["metadata"] else {}
        existing_meta["last_progress"] = {
            "pct": progress.progress_pct,
            "message": progress.message,
            "executor": progress.executor_name,
            "timestamp": progress.timestamp or int(time.time()),
        }

        now = int(time.time())
        with conn:
            conn.execute(
                """UPDATE subtasks SET metadata = ?, updated_at = ?
                   WHERE id = ? AND workspace_id = ?""",
                (json.dumps(existing_meta), now, subtask_id, workspace_id),
            )

    # ── Subtask Status Transitions ────────────────────────────────────────
    # Parity: MC [subtaskId]/route.ts PATCH status field

    def transition_subtask(
        self,
        subtask_id: int,
        new_status: SubtaskStatus,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> Subtask:
        """Transition a subtask to a new mechanical status.

        Only mechanical state changes are allowed here: running, stalled,
        cancelled. completed and failed MUST go through
        handle_subtask_completion() / handle_subtask_failure() which handle
        downstream dependency release.

        Side effects per target status:
        - running: sets started_at
        - stalled: sets stall_detected_at
        - cancelled: sets completed_at, updates convoy progress
        """
        conn = self.db.conn
        row = conn.execute(
            "SELECT * FROM subtasks WHERE id = ? AND workspace_id = ?",
            (subtask_id, workspace_id),
        ).fetchone()
        if not row:
            raise ValueError(f"Subtask {subtask_id} not found")
        subtask = self.db.row_to_subtask(row)

        allowed = SUBTASK_TRANSITIONS.get(subtask.status, [])
        if new_status not in allowed:
            raise ValueError(f"Cannot transition subtask from '{subtask.status}' to '{new_status}'")

        now = int(time.time())
        with conn:
            conn.execute(
                "UPDATE subtasks SET status = ?, updated_at = ? WHERE id = ?",
                (new_status, now, subtask_id),
            )

            if new_status == "running" and subtask.started_at is None:
                conn.execute(
                    "UPDATE subtasks SET started_at = ? WHERE id = ?",
                    (now, subtask_id),
                )
            elif new_status == "stalled":
                conn.execute(
                    "UPDATE subtasks SET stall_detected_at = ? WHERE id = ?",
                    (now, subtask_id),
                )
            elif new_status == "cancelled":
                conn.execute(
                    "UPDATE subtasks SET completed_at = ? WHERE id = ?",
                    (now, subtask_id),
                )
                self._update_progress(subtask.convoy_id)
                self._check_completion(subtask.convoy_id)

        return self.get_subtask(subtask_id, workspace_id)

    # ── Subtask Field Updates ─────────────────────────────────────────────
    # Parity: MC [subtaskId]/route.ts PATCH metadata fields

    def update_subtask_fields(
        self,
        subtask_id: int,
        fields: dict[str, str | None],
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> Subtask:
        """Update allowed metadata fields on a subtask.

        Most fields are rejected on terminal subtasks. POST_TERMINAL_FIELDS
        (merge_commit, error_message) are allowed after terminal because
        their values are only known after completion/failure.
        """
        conn = self.db.conn
        row = conn.execute(
            "SELECT * FROM subtasks WHERE id = ? AND workspace_id = ?",
            (subtask_id, workspace_id),
        ).fetchone()
        if not row:
            raise ValueError(f"Subtask {subtask_id} not found")
        subtask = self.db.row_to_subtask(row)

        invalid = set(fields.keys()) - UPDATABLE_SUBTASK_FIELDS
        if invalid:
            raise ValueError(f"Cannot update disallowed fields: {', '.join(sorted(invalid))}")

        if subtask.status in TERMINAL_SUBTASK_STATUSES:
            non_seal = set(fields.keys()) - POST_TERMINAL_FIELDS
            if non_seal:
                raise ValueError(
                    f"Cannot update fields on terminal subtask (status: {subtask.status}). "
                    f"Only {', '.join(sorted(POST_TERMINAL_FIELDS))} are allowed post-terminal."
                )

        if not fields:
            return subtask

        now = int(time.time())
        set_clauses = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [now, subtask_id]

        with conn:
            conn.execute(
                f"UPDATE subtasks SET {set_clauses}, updated_at = ? WHERE id = ?",
                values,
            )

        return self.get_subtask(subtask_id, workspace_id)

    # ── Completion ─────────────────────────────────────────────────────────
    # Parity: convoy.ts:handleSubtaskCompletion()

    def handle_subtask_completion(
        self,
        subtask_id: int,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> tuple[list[Subtask], bool]:
        """Returns (newly_unblocked_subtasks, convoy_completed).

        newly_unblocked_subtasks contains only subtasks that were unblocked by
        THIS completion event (their remaining_dependencies just hit 0). Does NOT
        include subtasks that were already in 'ready' state before this call.
        """
        conn = self.db.conn
        row = conn.execute(
            "SELECT * FROM subtasks WHERE id = ? AND workspace_id = ?",
            (subtask_id, workspace_id),
        ).fetchone()
        if not row:
            raise ValueError(f"Subtask {subtask_id} not found")
        subtask = self.db.row_to_subtask(row)

        # Guard: already terminal — idempotent no-op so duplicate callbacks
        # with different idempotency keys don't corrupt convoy state.
        if subtask.status in TERMINAL_SUBTASK_STATUSES:
            return [], False

        now = int(time.time())

        with conn:
            # Mark completed
            conn.execute(
                """UPDATE subtasks
                   SET status = 'completed', completed_at = ?, updated_at = ?
                   WHERE id = ?""",
                (now, now, subtask_id),
            )

            # Decrement downstream dependencies
            downstream = conn.execute(
                "SELECT to_subtask_id FROM dependency_edges WHERE from_subtask_id = ?",
                (subtask_id,),
            ).fetchall()
            downstream_ids = [e["to_subtask_id"] for e in downstream]

            for dep_id in downstream_ids:
                conn.execute(
                    """UPDATE subtasks
                       SET remaining_dependencies = MAX(0, remaining_dependencies - 1),
                           updated_at = ?
                       WHERE id = ? AND status = 'pending'""",
                    (now, dep_id),
                )

            # Mark subtasks that just became unblocked (deps hit 0)
            conn.execute(
                """UPDATE subtasks SET status = 'ready'
                   WHERE convoy_id = ? AND status = 'pending'
                     AND remaining_dependencies = 0""",
                (subtask.convoy_id,),
            )

            # Return only subtasks THIS event unblocked, not pre-existing ready ones.
            if downstream_ids:
                placeholders = ",".join("?" for _ in downstream_ids)
                newly_unblocked = [
                    self.db.row_to_subtask(r)
                    for r in conn.execute(
                        f"SELECT * FROM subtasks WHERE id IN ({placeholders}) AND status = 'ready' ORDER BY seq",
                        downstream_ids,
                    ).fetchall()
                ]
            else:
                newly_unblocked = []

            self._update_progress(subtask.convoy_id)
            convoy_completed = self._check_completion(subtask.convoy_id)

        return newly_unblocked, convoy_completed

    # ── Failure ────────────────────────────────────────────────────────────
    # Parity: convoy.ts:handleSubtaskFailure()

    def handle_subtask_failure(
        self,
        subtask_id: int,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
        error_message: str | None = None,
    ) -> bool:
        """Returns True if the convoy transitioned to a terminal state."""
        conn = self.db.conn
        row = conn.execute(
            "SELECT * FROM subtasks WHERE id = ? AND workspace_id = ?",
            (subtask_id, workspace_id),
        ).fetchone()
        if not row:
            raise ValueError(f"Subtask {subtask_id} not found")
        subtask = self.db.row_to_subtask(row)

        # Guard: already terminal — idempotent no-op so a subtask.failed callback
        # with a new idempotency key can't flip an already-completed subtask back to
        # failed and corrupt the convoy's completion state.
        if subtask.status in TERMINAL_SUBTASK_STATUSES:
            return False

        now = int(time.time())

        with conn:
            conn.execute(
                """UPDATE subtasks
                   SET status = 'failed', error_message = ?,
                       completed_at = ?, updated_at = ?
                   WHERE id = ?""",
                (error_message, now, now, subtask_id),
            )
            self._update_progress(subtask.convoy_id)
            return self._check_completion(subtask.convoy_id)

    # ── Executor Callback Ingress ──────────────────────────────────────────
    # Phase 6b: framework-owned conductor loop. Replaces the need for Mission
    # Control's webhook route when running GUI-off.

    def _auto_dispatch_ready(
        self,
        newly_ready: list[Subtask],
        convoy_completed: bool,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> list[int]:
        """Auto-dispatch all ready subtasks after a completion event.

        Returns list of subtask IDs successfully dispatched.
        Mirrors MC's webhook auto-dispatch loop (webhooks/convoy/route.ts:85-93).
        Individual failures are logged and skipped — the convoy continues.
        """
        if convoy_completed or not newly_ready:
            return []
        dispatched_ids: list[int] = []
        for subtask in newly_ready:
            try:
                receipt = self.dispatch_subtask(subtask.id, workspace_id)
                if receipt.status == "accepted":
                    dispatched_ids.append(subtask.id)
            except Exception as exc:
                logger.warning("Auto-dispatch failed for subtask %d: %s", subtask.id, exc)
        return dispatched_ids

    def handle_executor_callback(
        self,
        event_type: str,
        convoy_id: int,
        subtask_id: int,
        idempotency_key: str,
        payload: dict,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> tuple[str, list[int]]:
        """Process an executor callback with exactly-once semantics.

        Returns (status, newly_dispatched_ids) where status is
        'processed' or 'already_processed'.

        Idempotency: INSERT OR IGNORE on idempotency_key.
        Receipt deleted on processing error so retries can reprocess.
        Parity: MC webhooks/convoy/route.ts idempotency pattern.
        """
        import json as _json

        from orchestration.contract import CALLBACK_EVENT_TYPES

        if event_type not in CALLBACK_EVENT_TYPES:
            raise ValueError(f"Unknown event_type: {event_type!r}")

        conn = self.db.conn

        # 1. Idempotency insert — INSERT OR IGNORE on unique key
        with conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO callback_receipts
                   (idempotency_key, workspace_id, convoy_id, subtask_id, event_type, payload)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    idempotency_key,
                    workspace_id,
                    convoy_id,
                    subtask_id,
                    event_type,
                    _json.dumps(payload),
                ),
            )
        if cur.rowcount == 0:
            return "already_processed", []

        # 2. Process event
        try:
            # Validate subtask belongs to the claimed convoy — reject mismatches
            # before any state mutation so the receipt is cleaned up on error.
            subtask_row = conn.execute(
                "SELECT convoy_id FROM subtasks WHERE id = ? AND workspace_id = ?",
                (subtask_id, workspace_id),
            ).fetchone()
            if not subtask_row:
                raise ValueError(f"Subtask {subtask_id} not found")
            if subtask_row["convoy_id"] != convoy_id:
                raise ValueError(
                    f"Subtask {subtask_id} does not belong to convoy {convoy_id}"
                )

            newly_dispatched: list[int] = []
            if event_type == "subtask.completed":
                newly_ready, convoy_completed = self.handle_subtask_completion(
                    subtask_id, workspace_id
                )
                if merge_commit := payload.get("merge_commit"):
                    self.update_subtask_fields(
                        subtask_id, {"merge_commit": merge_commit}, workspace_id
                    )
                newly_dispatched = self._auto_dispatch_ready(
                    newly_ready, convoy_completed, workspace_id
                )
            elif event_type == "subtask.failed":
                self.handle_subtask_failure(
                    subtask_id,
                    workspace_id,
                    error_message=payload.get("error_message"),
                )
            elif event_type == "subtask.started":
                self.transition_subtask(subtask_id, "running", workspace_id)
            elif event_type == "subtask.stalled":
                self.transition_subtask(subtask_id, "stalled", workspace_id)

            # 3. Mark receipt processed
            with conn:
                conn.execute(
                    """UPDATE callback_receipts
                       SET processed_at = strftime('%s', 'now')
                       WHERE idempotency_key = ?""",
                    (idempotency_key,),
                )
            return "processed", newly_dispatched

        except Exception:
            # Delete receipt on error so retries can reprocess (transient failures
            # should not become idempotency traps).
            with conn:
                conn.execute(
                    "DELETE FROM callback_receipts WHERE idempotency_key = ?",
                    (idempotency_key,),
                )
            raise

    # ── Status Transitions ─────────────────────────────────────────────────
    # Parity: convoy.ts:updateConvoyStatus()

    def update_convoy_status(
        self,
        convoy_id: int,
        new_status: ConvoyStatus,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> Convoy:
        conn = self.db.conn
        row = conn.execute(
            "SELECT * FROM convoys WHERE id = ? AND workspace_id = ?",
            (convoy_id, workspace_id),
        ).fetchone()
        if not row:
            raise ValueError(f"Convoy {convoy_id} not found")
        convoy = self.db.row_to_convoy(row)

        allowed = CONVOY_TRANSITIONS.get(convoy.status, [])
        if new_status not in allowed:
            raise ValueError(f"Cannot transition convoy from '{convoy.status}' to '{new_status}'")

        now = int(time.time())
        with conn:
            conn.execute(
                "UPDATE convoys SET status = ?, updated_at = ? WHERE id = ?",
                (new_status, now, convoy_id),
            )

            # Cancel non-terminal subtasks when cancelling
            if new_status == "cancelled":
                conn.execute(
                    """UPDATE subtasks SET status = 'cancelled', updated_at = ?
                       WHERE convoy_id = ?
                         AND status NOT IN ('completed', 'failed', 'cancelled')""",
                    (now, convoy_id),
                )

        return self._get_convoy_row(convoy_id)

    # ── Add Subtasks ───────────────────────────────────────────────────────
    # Parity: convoy.ts:addSubtasks()

    def add_subtasks(
        self,
        convoy_id: int,
        subtasks: list[AddSubtaskInput],
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> list[Subtask]:
        conn = self.db.conn
        with conn:
            convoy_exists = conn.execute(
                "SELECT 1 FROM convoys WHERE id = ? AND workspace_id = ?",
                (convoy_id, workspace_id),
            ).fetchone()
            if not convoy_exists:
                raise ValueError(f"Convoy {convoy_id} not found")

            existing_ids = {
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM subtasks WHERE convoy_id = ? AND workspace_id = ?",
                    (convoy_id, workspace_id),
                ).fetchall()
            }
            max_seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), -1) as max_seq FROM subtasks WHERE convoy_id = ?",
                (convoy_id,),
            ).fetchone()
            max_seq = max_seq_row["max_seq"]

            new_ids: list[int] = []
            for i, s in enumerate(subtasks):
                dep_count = len(s.depends_on_subtask_ids)
                cur = conn.execute(
                    """INSERT INTO subtasks
                       (convoy_id, workspace_id, title, description,
                        assigned_agent_id, assigned_agent_name,
                        remaining_dependencies, seq, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        convoy_id,
                        workspace_id,
                        s.title,
                        s.description,
                        s.assigned_agent_id,
                        s.assigned_agent_name,
                        dep_count,
                        max_seq + 1 + i,
                        s.metadata,
                    ),
                )
                new_ids.append(cur.lastrowid)

            # Insert edges - depends_on_subtask_ids references existing persisted subtasks
            for i, s in enumerate(subtasks):
                for dep_id in s.depends_on_subtask_ids:
                    if dep_id not in existing_ids:
                        raise ValueError(
                            f"Invalid dependency subtask id {dep_id} for convoy {convoy_id}"
                        )
                    conn.execute(
                        """INSERT INTO dependency_edges
                           (workspace_id, convoy_id, from_subtask_id, to_subtask_id)
                           VALUES (?, ?, ?, ?)""",
                        (workspace_id, convoy_id, dep_id, new_ids[i]),
                    )

            if self._detect_cycle(convoy_id):
                raise ValueError("Adding these subtasks would create a dependency cycle")

            # Mark ready those with 0 deps
            conn.execute(
                """UPDATE subtasks SET status = 'ready'
                   WHERE convoy_id = ? AND status = 'pending'
                     AND remaining_dependencies = 0""",
                (convoy_id,),
            )

            # Update total
            total_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM subtasks WHERE convoy_id = ?",
                (convoy_id,),
            ).fetchone()
            now = int(time.time())
            conn.execute(
                "UPDATE convoys SET total_subtasks = ?, updated_at = ? WHERE id = ?",
                (total_row["cnt"], now, convoy_id),
            )

        placeholders = ",".join("?" for _ in new_ids)
        rows = conn.execute(
            f"SELECT * FROM subtasks WHERE id IN ({placeholders}) ORDER BY seq",
            new_ids,
        ).fetchall()
        return [self.db.row_to_subtask(r) for r in rows]

    # ── Delete ─────────────────────────────────────────────────────────────
    # Parity: convoy.ts:deleteConvoy()

    def delete_convoy(
        self,
        convoy_id: int,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> None:
        conn = self.db.conn
        with conn:
            cur = conn.execute(
                "DELETE FROM convoys WHERE id = ? AND workspace_id = ?",
                (convoy_id, workspace_id),
            )
            if cur.rowcount == 0:
                raise ValueError(f"Convoy {convoy_id} not found")

    # ── Internal helpers ───────────────────────────────────────────────────

    def _get_convoy_row(self, convoy_id: int) -> Convoy:
        row = self.db.conn.execute(
            "SELECT * FROM convoys WHERE id = ?",
            (convoy_id,),
        ).fetchone()
        return self.db.row_to_convoy(row)

    def _get_subtask_rows(self, convoy_id: int) -> list[Subtask]:
        rows = self.db.conn.execute(
            "SELECT * FROM subtasks WHERE convoy_id = ? ORDER BY seq",
            (convoy_id,),
        ).fetchall()
        return [self.db.row_to_subtask(r) for r in rows]

    def _get_edge_rows(self, convoy_id: int) -> list[DependencyEdge]:
        rows = self.db.conn.execute(
            "SELECT * FROM dependency_edges WHERE convoy_id = ?",
            (convoy_id,),
        ).fetchall()
        return [self.db.row_to_edge(r) for r in rows]

    # Parity: convoy.ts:updateConvoyProgress()
    def _update_progress(self, convoy_id: int) -> None:
        conn = self.db.conn
        row = conn.execute(
            """SELECT
                 COUNT(*) as total,
                 SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
                 SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
               FROM subtasks WHERE convoy_id = ?""",
            (convoy_id,),
        ).fetchone()
        now = int(time.time())
        conn.execute(
            """UPDATE convoys
               SET total_subtasks = ?, completed_subtasks = ?,
                   failed_subtasks = ?, updated_at = ?
               WHERE id = ?""",
            (row["total"], row["completed"], row["failed"], now, convoy_id),
        )

    # Parity: convoy.ts:checkConvoyCompletion()
    def _check_completion(self, convoy_id: int) -> bool:
        conn = self.db.conn
        convoy_row = conn.execute(
            "SELECT * FROM convoys WHERE id = ?",
            (convoy_id,),
        ).fetchone()
        if not convoy_row:
            return False
        if convoy_row["status"] in TERMINAL_CONVOY_STATUSES:
            return False

        stats = conn.execute(
            """SELECT
                 COUNT(*) as total,
                 SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
                 SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
               FROM subtasks WHERE convoy_id = ?""",
            (convoy_id,),
        ).fetchone()

        if stats["total"] == 0:
            return False

        now = int(time.time())

        # All completed
        if stats["completed"] >= stats["total"]:
            conn.execute(
                """UPDATE convoys
                   SET status = 'completed', completed_at = ?, updated_at = ?
                   WHERE id = ?""",
                (now, now, convoy_id),
            )
            return True

        # More than half failed
        if stats["failed"] > stats["total"] / 2:
            conn.execute(
                """UPDATE convoys
                   SET status = 'failed', completed_at = ?, updated_at = ?
                   WHERE id = ?""",
                (now, now, convoy_id),
            )
            return True

        # All terminal
        terminal_stats = conn.execute(
            """SELECT
                 COUNT(*) as cnt,
                 SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) as cancelled
               FROM subtasks
               WHERE convoy_id = ?
                 AND status IN ('completed', 'failed', 'cancelled')""",
            (convoy_id,),
        ).fetchone()

        if terminal_stats["cnt"] >= stats["total"]:
            if stats["failed"] > 0:
                final_status = "failed"
            elif terminal_stats["cancelled"] >= stats["total"]:
                final_status = "cancelled"
            else:
                final_status = "completed"
            conn.execute(
                """UPDATE convoys
                   SET status = ?, completed_at = ?, updated_at = ?
                   WHERE id = ?""",
                (final_status, now, now, convoy_id),
            )
            return True

        return False

    # Parity: convoy.ts:detectCycleInConvoy()
    def _detect_cycle(self, convoy_id: int) -> bool:
        edges = self.db.conn.execute(
            "SELECT from_subtask_id, to_subtask_id FROM dependency_edges WHERE convoy_id = ?",
            (convoy_id,),
        ).fetchall()

        adj: dict[int, list[int]] = {}
        nodes: set[int] = set()
        for e in edges:
            f, t = e["from_subtask_id"], e["to_subtask_id"]
            nodes.add(f)
            nodes.add(t)
            adj.setdefault(f, []).append(t)

        # DFS with coloring: 0=white, 1=gray, 2=black
        color: dict[int, int] = {n: 0 for n in nodes}

        def dfs(node: int) -> bool:
            color[node] = 1
            for neighbor in adj.get(node, []):
                c = color.get(neighbor, 0)
                if c == 1:
                    return True  # back edge = cycle
                if c == 0 and dfs(neighbor):
                    return True
            color[node] = 2
            return False

        for n in nodes:
            if color[n] == 0 and dfs(n):
                return True
        return False
