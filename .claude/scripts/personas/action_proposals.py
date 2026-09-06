"""Pending-action proposals — the operator-approval gate for persona WRITE tools.

Epic #465 ticket 1a. The doctrine (``runtime/toolsets.py``): every write tool
gets its own dedicated operator-approval gate; writes are NEVER reachable from
a bare toolset grant. Grant expands reach; the gate authorizes action. This
module is that gate. A ``dedicated_gate=True`` tool handler never executes —
it calls :func:`propose_action`, which writes one row to a sqlite store and
returns; the tool result the model sees is a CARD. Only :func:`decide_action`,
reached from an authenticated operator action (``/act approve``), gated on the
admin role and an interactive source, runs anything — and what it runs is the
STORED payload through the tool's registered executor, never caller-supplied
arguments.

**Sibling of ``personas/grant_proposals.py``, one step further along the same
rail.** That module gates what a persona can REACH (toolset grants); this one
gates what a granted persona can DO (one concrete write, one approval, one
execution). Same mechanics, deliberately: per-persona sqlite store, short
human code, 30-minute TTL with lazy audited expiry, CAS pending -> decided so
a double tap executes once, audited refusals, kill switch.

**Storage grain matches authorization grain (Rule 4).** The store and the
ledger live in the TARGET persona's own ``<profile>/data/`` — resolved through
``get_persona_paths(persona_id)``, never through the ambient
``config.DATA_DIR``, for the same reason the grant rail documents: persona
bots are separate processes with their own ``HOMIE_HOME``, and keying off the
ambient constant files the proposal where the approving process cannot find
it.

**The executor registry is part of the security boundary.** A tool with no
registered executor cannot be "approved" into a no-op: ``decide_action``
refuses loudly and audits the refusal. Approval without execution would be
approval theater — a receipt claiming an action happened that nothing
performed.

**The executor/driver boundary requires a real approval artifact.** A winning
CAS mints a one-use execution token bound to the action id AND a hash of the
stored payload (Codex R1 BLOCKER): the token rides the executor call into the
driver, and the driver's first act is
:func:`consume_execution_token` — an atomic verify-and-consume, so a replay,
a caller without a token, or a token presented for a different payload all
fail closed. An in-process caller that never touched the gate (a persona with
a shell, say) cannot mint a valid token: minting exists only inside the
CAS-winning approve path.

**KillSwitchDisabled PROPAGATES.** The kill switch is the operator's OFF
control for this entire surface; catching it here would let a decision half
complete and report itself as refused. It is audited best-effort and then
re-raised so the chat surface answers honestly.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

# Operator OFF control for the action-gate surface specifically. Ships ON: an
# unset env var is enabled, and the switch can only ever turn it off.
KILL_SWITCH_NAME = "persona_action_proposals"

STORE_FILENAME = "persona_action_proposals.db"
LEDGER_FILENAME = "persona_action_proposals.jsonl"
LEDGER_INTEGRATION = "personas"
LEDGER_ACTION = "persona_action"

ADMIN_ROLE = "admin"
# A decision is an operator's tap. Anything stamped tool/cron/hook (the
# IncomingMessage.source taxonomy, PRP-7d) is automation wearing an approval
# surface, and this gate exists precisely to keep automation out of it.
REQUIRED_SOURCE = "interactive"

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"
STATUS_EXPIRED = "expired"

DECISION_UNKNOWN = "unknown"
DECISION_REFUSED = "refused"
DECISION_EXPIRED = "expired"
DECISION_ALREADY_DECIDED = "already_decided"
DECISION_DENIED = "denied"
DECISION_EXECUTED = "executed"
# Every handle landed differently than "all succeeded" but at least one did:
# the batch ran, and the receipt says which parts did not.
DECISION_PARTIAL = "partial"
DECISION_FAILED = "failed"
# The chat seam itself failed before ``decide_action`` could answer.
DECISION_ERROR = "error"
# A refusal happened (nothing mutated) but its REQUIRED ledger row could not
# be written — never let a caller read an unaudited refusal as recorded
# (mirrors grant_proposals.DECISION_AUDIT_FAILED).
DECISION_AUDIT_FAILED = "audit_failed"

REASON_UNKNOWN_PROPOSAL = "unknown_proposal"
REASON_PROPOSAL_EXPIRED = "proposal_expired"
REASON_ALREADY_DECIDED = "already_decided"
REASON_NOT_AUTHORIZED = "not_authorized"
REASON_NON_INTERACTIVE_SOURCE = "non_interactive_source"
REASON_KILL_SWITCH = "kill_switch"
REASON_NO_EXECUTOR = "no_executor"
REASON_INVALID_PERSONA = "invalid_persona"
REASON_INVALID_TOOL = "invalid_tool"
REASON_INVALID_ARGUMENTS = "invalid_arguments"

OUTCOME_PROPOSED = "proposed"
OUTCOME_APPROVED = "approved"
OUTCOME_DENIED = "denied"
OUTCOME_REFUSED = "refused"
OUTCOME_EXPIRED = "expired"
OUTCOME_EXECUTED = "executed"
OUTCOME_PARTIAL = "partial"
OUTCOME_FAILED = "failed"

# Tool names are registry keys, not free text. Checked before anything stores
# one so a hostile handler payload never reaches the executor registry.
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_CODE_RE = re.compile(r"^[A-Z0-9]{6}$")

# Long enough for an operator to come back to a card after a meeting, short
# enough that a stale approval cannot execute an action nobody remembers.
_PROPOSAL_TTL_S = 1800
_TTL_ENV = "HOMIE_ACTION_PROPOSAL_TTL_SECONDS"
_TTL_MIN_S = 60
_TTL_MAX_S = 86_400

_MAX_SUMMARY_CHARS = 300
_MAX_ARGUMENTS_CHARS = 8_000
_MAX_OUTCOME_CHARS = 4_000
_MAX_LEDGER_FIELD_CHARS = 512

# tool_name -> executor. Populated by tool-impl modules at registration time
# (``register_action_executor``). The executor contract:
#
#     fn(*, persona_id: str, action_id: str, execution_token: str,
#        arguments: dict) -> dict
#
# ``arguments`` is the STORED payload, deep-copied. ``execution_token`` is the
# one-use artifact minted by the winning CAS; the executor passes it (with the
# action id, persona id, and the untouched payload) to whatever performs the
# write, which consumes it via :func:`consume_execution_token` before moving.
# The executor never sees the deciding turn.
_EXECUTORS: dict[str, Callable[..., dict[str, Any]]] = {}


@dataclass(frozen=True)
class ActionProposal:
    """One persona's un-actioned write. Nothing here has touched the world."""

    action_id: str
    short_code: str
    persona_id: str
    tool_name: str
    arguments: dict[str, Any]
    summary: str
    status: str
    created_at: float
    expires_at: float
    decided_by: str = ""
    decided_at: float | None = None
    status_detail: str = ""
    outcome_json: str = ""


@dataclass(frozen=True)
class ActionDecision:
    """What an approve/deny did, in terms a chat surface can speak."""

    outcome: str
    proposal: ActionProposal | None
    message: str
    result: Any = None


# ── Executor registry ────────────────────────────────────────────────────


def register_action_executor(tool_name: str, fn: Callable[..., dict[str, Any]]) -> None:
    """Bind the executor an approval of *tool_name* will run.

    Called by tool-impl modules at registration time, next to the matching
    ``register_tool(...)`` — the tool and its executor are one unit, and a
    tool registered without its executor is a loud refusal at decide time.
    Re-registering under the same name replaces (test override, reload).
    """
    name = str(tool_name or "").strip()
    if not _TOOL_NAME_RE.match(name):
        raise ValueError(f"executor tool name {tool_name!r} is not a registry key shape")
    if not callable(fn):
        raise ValueError(f"executor for {name!r} must be callable")
    _EXECUTORS[name] = fn


def get_action_executor(tool_name: str) -> Callable[..., dict[str, Any]] | None:
    """The executor bound to *tool_name*, or None. Read at call time (Rule 3)."""
    return _EXECUTORS.get(str(tool_name or "").strip())


# ── Path resolution (Rule 4: storage grain == authorization grain) ────────


def resolve_store_path(
    persona_id: str,
    db_path: Path | str | None = None,
) -> Path:
    """Resolve the proposal store for *persona_id* at call time.

    Same invariant as ``grant_proposals.resolve_store_path``: an explicit
    *db_path* wins (tests inject one); a named persona resolves through
    ``get_persona_paths``; only with neither does this fall back to the
    ambient ``config.DATA_DIR``. Both imports are lazy — cycle safety and
    test monkeypatching both depend on call-time resolution.
    """
    if db_path is not None:
        return Path(db_path)
    persona = str(persona_id or "").strip()
    if persona:
        from personas.core import get_persona_paths  # noqa: PLC0415 — cycle-safe

        return Path(get_persona_paths(persona)["data"]) / STORE_FILENAME
    import config  # noqa: PLC0415 — cycle-safe + test-monkeypatched

    return Path(config.DATA_DIR) / STORE_FILENAME


def resolve_ledger_path(
    persona_id: str,
    audit_path: Path | str | None = None,
) -> Path:
    """Resolve the action ledger — the same discipline as the store above."""
    if audit_path is not None:
        return Path(audit_path)
    persona = str(persona_id or "").strip()
    if persona:
        from personas.core import get_persona_paths  # noqa: PLC0415 — cycle-safe

        return Path(get_persona_paths(persona)["data"]) / LEDGER_FILENAME
    import config  # noqa: PLC0415 — cycle-safe + test-monkeypatched

    return Path(config.DATA_DIR) / LEDGER_FILENAME


def proposal_ttl_seconds() -> int:
    """TTL for an un-actioned proposal, read from the environment per call.

    Rule 1: no module-load snapshot, so an operator (or a test) changing the
    knob takes effect on the next proposal. Bad values clamp instead of
    raising — a broken env var must not disable the gate.
    """
    raw = os.getenv(_TTL_ENV, "").strip()
    try:
        value = int(raw) if raw else _PROPOSAL_TTL_S
    except ValueError:
        value = _PROPOSAL_TTL_S
    return max(_TTL_MIN_S, min(_TTL_MAX_S, value))


# ── Ledger ────────────────────────────────────────────────────────────────


def _ledger_field(value: Any) -> str:
    """A bounded ledger field — identifiers and short reasons, never prose."""
    return " ".join(str(value or "").split())[:_MAX_LEDGER_FIELD_CHARS]


def _append_ledger_row(
    *,
    persona_id: str,
    tool_name: str,
    outcome: str,
    operation: str,
    actor: str = "",
    actor_role: str = "",
    surface: str = "",
    channel_id: str = "",
    source: str = "",
    summary: str = "",
    reason: str = "",
    error: str = "",
    correlation_id: str = "",
    audit_path: Path | str | None = None,
) -> str:
    """Append one ledger row and return its id. Raises on an IO failure.

    Kept strict so the best-effort wrapper below has something to report and
    so REFUSALS — which a caller reads back as recorded — can turn a write
    failure into an honest audit-failed decision instead of a polished lie.
    """
    path = resolve_ledger_path(persona_id, audit_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    row_id = f"{datetime.now(UTC).isoformat(timespec='seconds')}-{uuid.uuid4().hex[:8]}"
    record = {
        "id": row_id,
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "integration": LEDGER_INTEGRATION,
        "action": LEDGER_ACTION,
        "operation": operation,
        "persona_id": _ledger_field(persona_id),
        "tool_name": _ledger_field(tool_name),
        "outcome": _ledger_field(outcome),
        "reason": _ledger_field(reason),
        "actor": _ledger_field(actor),
        "actor_role": _ledger_field(actor_role),
        "surface": _ledger_field(surface),
        "channel_id": _ledger_field(channel_id),
        "source": _ledger_field(source),
        "summary": _ledger_field(summary)[:_MAX_SUMMARY_CHARS],
        "error": _ledger_field(error),
        "correlation_id": _ledger_field(correlation_id),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")
    return row_id


def _audit(**fields: Any) -> str:
    """Best-effort ledger append — a failed row never changes the outcome.

    Used ONLY for rows whose branch already surfaces its own outcome to the
    caller (proposed/approved/denied/executed/failed). Refusals go through
    :func:`_append_ledger_row` directly; see ``decide_action``.
    """
    try:
        return _append_ledger_row(**fields)
    except Exception as exc:  # noqa: BLE001 — audit is a record, not the action
        _logger.warning(
            "action proposals: ledger write failed: %s: %s",
            type(exc).__name__,
            exc,
        )
        return ""


# ── Store ─────────────────────────────────────────────────────────────────


def _payload_hash(arguments: dict[str, Any]) -> str:
    """sha256 of the canonical stored payload. The token's binding target."""
    canonical = json.dumps(arguments, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _token_hash(token: str) -> str:
    """The stored form of an execution token — the raw token never rests."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _safe_text(value: Any, *, limit: int = 300) -> str:
    """A bounded string for ANY value, that cannot itself raise (Codex R1).

    The post-CAS failure path records whatever the executor threw; a
    pathological exception whose ``__str__``/``__repr__`` raises must still
    leave a complete audited row. Nested fallbacks end at the TYPE NAME,
    which Python guarantees.
    """
    try:
        text = str(value)
    except Exception:  # noqa: BLE001 — the whole point of this helper
        try:
            text = repr(value)
        except Exception:  # noqa: BLE001
            text = ""
    if not text:
        text = f"<unprintable {type(value).__name__}>"
    return " ".join(text.split())[:limit]


def _connect(persona_id: str, db_path: Path | str | None) -> sqlite3.Connection:
    path = resolve_store_path(persona_id, db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS persona_action_proposals (
            action_id TEXT PRIMARY KEY,
            short_code TEXT NOT NULL UNIQUE,
            persona_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            arguments_json TEXT NOT NULL,
            summary TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            decided_by TEXT NOT NULL DEFAULT '',
            decided_at REAL,
            status_detail TEXT NOT NULL DEFAULT '',
            outcome_json TEXT NOT NULL DEFAULT '',
            payload_hash TEXT NOT NULL DEFAULT '',
            execution_token_hash TEXT NOT NULL DEFAULT ''
        )
        """
    )
    # In-place migration for stores created before the execution-token
    # columns existed (Codex R1): a duplicate-column error just means the
    # column is already there.
    for column in ("payload_hash", "execution_token_hash"):
        try:
            conn.execute(
                f"ALTER TABLE persona_action_proposals ADD COLUMN {column} "
                "TEXT NOT NULL DEFAULT ''"
            )
        except sqlite3.OperationalError:
            pass
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_action_proposals_status "
        "ON persona_action_proposals(persona_id, status, expires_at)"
    )
    conn.commit()
    return conn


def _row_to_proposal(row: sqlite3.Row | None) -> ActionProposal | None:
    if row is None:
        return None
    try:
        arguments = json.loads(str(row["arguments_json"]))
    except (json.JSONDecodeError, TypeError):
        arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    return ActionProposal(
        action_id=str(row["action_id"]),
        short_code=str(row["short_code"]),
        persona_id=str(row["persona_id"]),
        tool_name=str(row["tool_name"]),
        arguments=arguments,
        summary=str(row["summary"]),
        status=str(row["status"]),
        created_at=float(row["created_at"]),
        expires_at=float(row["expires_at"]),
        decided_by=str(row["decided_by"] or ""),
        decided_at=(float(row["decided_at"]) if row["decided_at"] is not None else None),
        status_detail=str(row["status_detail"] or ""),
        outcome_json=str(row["outcome_json"] or ""),
    )


def _normalize_summary(summary: Any) -> str:
    """One line, bounded — a card field, never a paragraph of model output."""
    return " ".join(str(summary or "").split())[:_MAX_SUMMARY_CHARS]


def expire_pending(
    persona_id: str,
    *,
    now: float | None = None,
    db_path: Path | str | None = None,
    audit_path: Path | str | None = None,
) -> list[ActionProposal]:
    """Quietly expire *persona_id*'s stale proposals; audit each one.

    Called at the top of every read and of the decision path, so an expired
    proposal can never be approved — the honest answer is produced from the
    row this flipped, not from a timestamp compared somewhere else.
    """
    persona = str(persona_id or "").strip()
    if not persona:
        return []
    current = time.time() if now is None else float(now)
    conn = _connect(persona, db_path)
    expired: list[ActionProposal] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT * FROM persona_action_proposals "
            "WHERE persona_id = ? AND status = ? AND expires_at <= ?",
            (persona, STATUS_PENDING, current),
        ).fetchall()
        if rows:
            conn.executemany(
                "UPDATE persona_action_proposals SET status = ?, decided_at = ?, "
                "status_detail = 'proposal TTL elapsed' "
                "WHERE action_id = ? AND status = ?",
                [
                    (STATUS_EXPIRED, current, str(row["action_id"]), STATUS_PENDING)
                    for row in rows
                ],
            )
        conn.commit()
        expired = [p for row in rows if (p := _row_to_proposal(row)) is not None]
    finally:
        conn.close()
    for proposal in expired:
        _audit(
            persona_id=proposal.persona_id,
            tool_name=proposal.tool_name,
            operation="propose",
            outcome=OUTCOME_EXPIRED,
            reason=REASON_PROPOSAL_EXPIRED,
            summary=proposal.summary,
            correlation_id=proposal.action_id,
            audit_path=audit_path,
        )
    return expired


def get_action(
    persona_id: str,
    code_or_id: str,
    *,
    now: float | None = None,
    db_path: Path | str | None = None,
    audit_path: Path | str | None = None,
) -> ActionProposal | None:
    """One proposal by short code or id, with stale rows expired first."""
    persona = str(persona_id or "").strip()
    needle = str(code_or_id or "").strip()
    if not persona or not needle:
        return None
    expire_pending(persona, now=now, db_path=db_path, audit_path=audit_path)
    conn = _connect(persona, db_path)
    try:
        return _row_to_proposal(
            conn.execute(
                "SELECT * FROM persona_action_proposals WHERE persona_id = ? "
                "AND (action_id = ? OR upper(short_code) = upper(?))",
                (persona, needle, needle),
            ).fetchone()
        )
    finally:
        conn.close()


def list_pending(
    persona_id: str,
    *,
    now: float | None = None,
    db_path: Path | str | None = None,
    audit_path: Path | str | None = None,
) -> list[ActionProposal]:
    """Un-actioned proposals for *persona_id*, newest first."""
    persona = str(persona_id or "").strip()
    if not persona:
        return []
    expire_pending(persona, now=now, db_path=db_path, audit_path=audit_path)
    conn = _connect(persona, db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM persona_action_proposals WHERE persona_id = ? AND status = ? "
            "ORDER BY created_at DESC LIMIT 20",
            (persona, STATUS_PENDING),
        ).fetchall()
    finally:
        conn.close()
    return [p for row in rows if (p := _row_to_proposal(row)) is not None]


def consume_execution_token(
    persona_id: str,
    action_id: str,
    token: str,
    arguments: Any,
    *,
    db_path: Path | str | None = None,
) -> bool:
    """Atomically verify AND consume a one-use execution token.

    This is the only door between "an approval happened" and "a driver may
    move". The token was minted by the CAS-winning approve inside
    :func:`decide_action`; consumption is a single guarded UPDATE, so:

    * a caller with no token (or a wrong one) fails — the hash never matches;
    * a REPLAY fails — the first consume blanks the stored hash;
    * a token presented beside a DIFFERENT payload fails — the payload hash
      is part of the guard, binding the token to the exact stored arguments
      the operator approved.

    Fails closed on every anomaly, including a store error: an exception here
    returns False (logged), because the cost of a wrongly refused execution
    is an operator retry, and the cost of a wrongly allowed one is a write
    nobody approved.
    """
    persona = str(persona_id or "").strip()
    aid = str(action_id or "").strip()
    raw_token = str(token or "").strip()
    if not persona or not aid or not raw_token or not isinstance(arguments, dict):
        return False
    try:
        payload_digest = _payload_hash(arguments)
    except (TypeError, ValueError):
        return False
    try:
        conn = _connect(persona, db_path)
        try:
            cursor = conn.execute(
                "UPDATE persona_action_proposals SET execution_token_hash = 'consumed' "
                "WHERE action_id = ? AND persona_id = ? AND status = ? "
                "AND execution_token_hash = ? AND payload_hash = ?",
                (
                    aid,
                    persona,
                    STATUS_APPROVED,
                    _token_hash(raw_token),
                    payload_digest,
                ),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — fail closed, never crash the driver
        _logger.error(
            "action proposals: token consume failed for %s/%s: %s: %s",
            persona,
            aid,
            type(exc).__name__,
            exc,
        )
        return False


def propose_action(
    persona_id: str,
    tool_name: str,
    arguments: Any,
    summary: Any,
    *,
    now: float | None = None,
    db_path: Path | str | None = None,
    audit_path: Path | str | None = None,
) -> ActionProposal | None:
    """Record one persona's intended write. Returns None when it cannot be asked.

    This function CANNOT execute anything — it writes one row to a sqlite
    store and nothing else. Refusals are audited and return None: a proposal
    that cannot be made is an honest error string from the calling handler,
    never a broken turn.

    KillSwitchDisabled PROPAGATES: the kill switch is the operator's OFF
    control for the whole surface, and a handler must be able to say
    "disabled", not "failed".
    """
    persona = str(persona_id or "").strip()
    tool = str(tool_name or "").strip()
    summary_text = _normalize_summary(summary)

    def _refuse(reason: str, error: str) -> None:
        _audit(
            persona_id=persona,
            tool_name=tool[:64],
            operation="propose",
            outcome=OUTCOME_REFUSED,
            reason=reason,
            error=error,
            audit_path=audit_path,
        )

    # Propagates by design — see the module docstring. The unavailable-module
    # case only logs: the switch is an OFF control, never the thing that
    # grants the feature.
    try:
        from security import kill_switches  # noqa: PLC0415 — Rule 3 module attr
    except Exception as exc:  # noqa: BLE001 — see comment
        _logger.warning(
            "action proposals: kill-switch module unavailable (%s: %s)",
            type(exc).__name__,
            exc,
        )
    else:
        kill_switches.requireEnabled(KILL_SWITCH_NAME, caller="personas.propose_action")

    if not persona:
        return None
    try:
        from personas.core import validate_persona_name  # noqa: PLC0415 — cycle-safe

        validate_persona_name(persona)
    except Exception as exc:  # noqa: BLE001 — an unnameable persona has no store
        _refuse(REASON_INVALID_PERSONA, str(exc))
        return None

    if not _TOOL_NAME_RE.match(tool):
        _refuse(REASON_INVALID_TOOL, "tool name is not a registry key shape")
        return None

    if not isinstance(arguments, dict):
        _refuse(REASON_INVALID_ARGUMENTS, "arguments must be a dict")
        return None
    try:
        arguments_json = json.dumps(arguments, sort_keys=True)
    except (TypeError, ValueError) as exc:
        _refuse(REASON_INVALID_ARGUMENTS, f"arguments are not JSON-serializable: {exc}")
        return None
    if len(arguments_json) > _MAX_ARGUMENTS_CHARS:
        _refuse(REASON_INVALID_ARGUMENTS, "arguments exceed the proposal limit")
        return None

    current = time.time() if now is None else float(now)
    expires_at = current + proposal_ttl_seconds()

    conn = _connect(persona, db_path)
    created: ActionProposal | None = None
    try:
        # Short codes must be unique in this store; collisions are rare but
        # cheap to retry, and the UNIQUE index is the only thing that proves it.
        for _ in range(6):
            action_id = uuid.uuid4().hex
            short_code = uuid.uuid4().hex[:6].upper()
            try:
                conn.execute(
                    "INSERT INTO persona_action_proposals ("
                    "action_id, short_code, persona_id, tool_name, arguments_json, "
                    "summary, status, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        action_id,
                        short_code,
                        persona,
                        tool,
                        arguments_json,
                        summary_text,
                        STATUS_PENDING,
                        current,
                        expires_at,
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()
                continue
            created = _row_to_proposal(
                conn.execute(
                    "SELECT * FROM persona_action_proposals WHERE action_id = ?",
                    (action_id,),
                ).fetchone()
            )
            break
    finally:
        conn.close()

    if created is None:
        _logger.warning(
            "action proposals: could not allocate a short code for %s/%s", persona, tool
        )
        return None

    _audit(
        persona_id=created.persona_id,
        tool_name=created.tool_name,
        operation="propose",
        outcome=OUTCOME_PROPOSED,
        summary=created.summary,
        correlation_id=created.action_id,
        audit_path=audit_path,
    )
    return created


def card_text(proposal: ActionProposal) -> str:
    """The operator-facing approval card.

    Nothing attacker-controlled reaches it: the validated persona, the
    validated tool name, the bounded summary the handler built from
    ALREADY-VALIDATED arguments, the code, and the TTL. The raw arguments
    JSON is never rendered — the summary is the human shape of the payload.
    """
    minutes = max(1, int(round((proposal.expires_at - proposal.created_at) / 60)))
    approve_cmd = f"`/act approve {proposal.persona_id} {proposal.short_code}`"
    deny_cmd = f"`/act deny {proposal.persona_id} {proposal.short_code}`"
    lines = [
        f"**Action `{proposal.short_code}`** — `{proposal.persona_id}` wants to run "
        f"`{proposal.tool_name}` (operator approval required).",
        f"What it does: {proposal.summary or '(no summary)'}",
        "Approving EXECUTES it once, exactly as proposed — the stored payload "
        "runs, never a rewritten one.",
        f"Approve: {approve_cmd} · Deny: {deny_cmd}",
        f"Expires in ~{minutes}m if untouched.",
    ]
    if proposal.tool_name == "outlook_send_email":
        # Reopened cards must show the same exact email as the initial card;
        # a shortened summary is insufficient authority for hidden mail text.
        args = proposal.arguments
        preview = (f"From: {args.get('mailbox_id', '')}\nTo: {args.get('to_email', '')}\n"
                   f"Subject: {args.get('subject', '')}\n\n{args.get('body', '')}")
        lines.extend(["", "Exact email content:",
                      "\n".join("> " + line for line in preview.split("\n"))])
    return "\n".join(lines)


# ── The decision path ──────────────────────────────────────────────────────


# Driver receipt statuses that mean "this handle landed". Anything else
# (error, refused, skipped) counts against the batch verdict. The GA4 fleet
# tools (#465 1a PR 2) land single-brand rows rather than per-handle batches;
# their statuses live here too so one classifier serves both drivers —
# including the deploy ladder (linked < env_synced < deployed), where the
# deepest landed stage is the row's status.
_SUCCESS_STATUSES = frozenset(
    {
        "followed",
        "already_following",
        "notifications_on",
        "provisioned",
        "linked",
        "env_synced",
        "deployed",
        # A failed `vercel link` that still changed physical linkage: the row
        # carries env_sync=failed, so it classifies PARTIAL — state changed,
        # and the receipt must say so (Codex R3).
        "link_mutated",
    }
)

# Requested sub-actions and the value that means they landed. When a row
# carries one of these fields at all, the field was REQUESTED — anything but
# the success value makes the row partial (Codex R2's lesson, generalized for
# the GA4 deploy's post-sync verification and the reconcile's two resources).
_SUBACTION_EXPECTED = {
    "notifications": "notifications_on",
    "verification": "verified",
    "property": "ok",
    "stream": "ok",
    "env_sync": "ok",
    "deploy": "ok",
}
# Where each sub-action's failure detail lives on the row.
_SUBACTION_DETAIL_FIELD = {
    "notifications": "notification_detail",
    "verification": "verification_detail",
    "property": "property_detail",
    "stream": "stream_detail",
    "env_sync": "env_detail",
    "deploy": "deploy_detail",
}

# Experience-note bound on per-handle lines — a receipt is a summary, not a dump.
_MAX_HANDLE_ROWS = 25


def _row_landed(row: dict[str, Any]) -> str:
    """One receipt row's verdict: ``full`` | ``partial`` | ``none``.

    Sub-action aware (Codex R2): a row whose primary status landed but whose
    requested notification toggle failed is PARTIAL — the operator approved
    both sub-actions, and "followed" alone must not read as full success.
    Same for a GA4 deploy whose tag verification failed.
    """
    if str(row.get("status", "")) not in _SUCCESS_STATUSES:
        return "none"
    for field, expected in _SUBACTION_EXPECTED.items():
        if field in row and row.get(field) != expected:
            return "partial"
    return "full"


def _classify_receipt(result: Any) -> tuple[str, list[dict[str, Any]]]:
    """``(verdict, per_handle_rows)`` from an executor receipt (Codex R1/R2).

    The gate records what the driver SAYS happened, never a blanket
    "executed": every row fully landed -> ``executed``; at least one row
    landed something -> ``partial``; nothing landed (or an ok:false receipt
    without rows) -> ``failed``. A receipt that is not the contracted dict
    shape reads as ``executed`` only when it does not claim failure — the
    executor contract returns a dict.
    """
    if not isinstance(result, dict):
        return OUTCOME_EXECUTED, []
    rows = result.get("results")
    if isinstance(rows, list) and rows:
        per_handle = [row for row in rows if isinstance(row, dict)]
        verdicts = [_row_landed(row) for row in per_handle]
        if all(v == "full" for v in verdicts):
            return OUTCOME_EXECUTED, per_handle
        if any(v in {"full", "partial"} for v in verdicts):
            return OUTCOME_PARTIAL, per_handle
        return OUTCOME_FAILED, per_handle
    if result.get("ok") is False:
        return OUTCOME_FAILED, []
    return OUTCOME_EXECUTED, []


def _record_outcome(
    persona_id: str,
    action_id: str,
    *,
    status_detail: str,
    outcome_json: str = "",
    db_path: Path | str | None = None,
) -> None:
    """Attach the executor's verdict to the decided row. Never raises.

    The decision already happened and was audited; this is the durable
    breadcrumb (Rule 2 — physical DB state, not a sidecar flag) an operator
    reads on a repeat tap or a later list.
    """
    try:
        conn = _connect(persona_id, db_path)
        try:
            conn.execute(
                "UPDATE persona_action_proposals SET status_detail = ?, "
                "outcome_json = ? WHERE action_id = ?",
                (str(status_detail)[:300], str(outcome_json)[:_MAX_OUTCOME_CHARS], action_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — breadcrumb only
        _logger.warning(
            "action proposals: outcome not recorded for %s: %s: %s",
            action_id,
            type(exc).__name__,
            exc,
        )


def _write_experience_receipt(
    proposal: ActionProposal,
    *,
    outcome: str,
    detail: str,
    per_handle: list[dict[str, Any]] | None = None,
) -> None:
    """One zero-LLM receipt in the persona's own experience notes. Never raises.

    The persona learns from its executed AND its failed actions; a receipt
    failure must never turn a landed action into a reported failure, so this
    rides the fail-open ``append_experience_section`` contract and logs.

    ``per_handle`` carries the driver receipt's rows, so the note says WHICH
    handles landed and which did not (Codex R1) — a batch that half-failed
    must read as half-failed in the persona's memory, not as "executed".
    """
    try:
        from personas import experience  # noqa: PLC0415 — cycle-safe, test-patched

        def _field(value: Any, cap: int = 160) -> str:
            return _safe_text(value, limit=cap)

        lines = [
            f"## action: {_field(proposal.tool_name, 64)} "
            f"(operator-approved -> {_field(outcome, 40)})",
            "",
            # The dedup marker, same shape experience.py renders — the key
            # is a server-side hex id, never model text.
            f"<!-- experience-key: action|{proposal.action_id} -->",
            "",
            f"- Action: {_field(proposal.summary, 300)}",
            f"- Tool: {_field(proposal.tool_name, 64)}",
            f"- Code: {_field(proposal.short_code, 32)}",
            f"- Approved by: {_field(proposal.decided_by, 80)}",
            f"- Outcome: {_field(detail, 300)}",
        ]
        for row in list(per_handle or [])[:_MAX_HANDLE_ROWS]:
            if not isinstance(row, dict):
                continue
            handle = _field(row.get("handle"), 40)
            status = _field(row.get("status"), 40)
            # Every requested sub-action is part of the verdict (Codex R2):
            # the note must show it, or a failed toggle/verification reads as
            # a full success in the persona's memory.
            failed_detail = ""
            for field, expected in _SUBACTION_EXPECTED.items():
                if field not in row:
                    continue
                status = f"{status}; {field}: {_field(row.get(field), 40)}"
                if row.get(field) != expected:
                    failed_detail = str(row.get(_SUBACTION_DETAIL_FIELD[field]) or "")
            # A failed sub-action's reason is the evidence that matters; a
            # success-side screenshot alone would hide it.
            evidence_source = (
                failed_detail
                if failed_detail
                else row.get("screenshot") or row.get("detail")
            )
            evidence = _field(evidence_source, 200)
            lines.append(
                f"- @{handle}: {status}{f' ({evidence})' if evidence else ''}"
            )
        section = "\n".join(lines)
        receipt = experience.append_experience_section(
            persona_id=proposal.persona_id,
            section=section,
            dedup_key=f"action|{proposal.action_id}",
        )
        if receipt.get("status") not in {"written", "duplicate"}:
            _logger.warning(
                "action proposals: experience receipt for %s was %s",
                proposal.action_id,
                receipt.get("status"),
            )
    except Exception as exc:  # noqa: BLE001 — receipt only, never the action
        _logger.warning(
            "action proposals: experience receipt failed for %s: %s: %s",
            proposal.action_id,
            type(exc).__name__,
            exc,
        )


def decide_action(
    persona_id: str,
    code_or_id: str,
    approved: bool,
    *,
    user_role: str,
    source: str,
    actor: str = "",
    surface: str = "",
    channel_id: str = "",
    now: float | None = None,
    db_path: Path | str | None = None,
    audit_path: Path | str | None = None,
) -> ActionDecision:
    """The ONLY way a proposal becomes an executed action. Admin-gated, CAS'd.

    ``user_role`` and ``source`` are CHECKED here; neither is ESTABLISHED
    here. The caller must resolve both server-side from the authenticated
    surface — a role forwarded from a payload reduces the gate to a
    formality (#426/#427), and a non-interactive source is automation
    reaching for the approval surface.

    Order is deliberate and mirrors the grant rail: READ first (mutates
    nothing, so every refusal row names the real persona and tool), then the
    kill switch (PROPAGATES — a proposal made before the switch flipped off
    must not still be executable), then the role and source gates, then the
    honest status answers, and only then the CAS. Only a winning CAS reaches
    the executor, so a double tap executes once.

    The executor runs the STORED payload — the row's ``arguments_json``,
    deep-copied — never anything the deciding caller supplied. A tool with
    no registered executor is refused loudly and audited: approval without
    execution is approval theater.
    """
    persona = str(persona_id or "").strip()
    needle = str(code_or_id or "").strip()
    role = str(user_role or "").strip().lower()
    src = str(source or "").strip().lower()
    who = str(actor or "").strip()
    surface = str(surface or "").strip()
    channel = str(channel_id or "").strip()
    current = time.time() if now is None else float(now)

    if not persona or not needle:
        return ActionDecision(
            DECISION_UNKNOWN, None, "That action proposal could not be identified."
        )

    proposal = get_action(persona, needle, now=current, db_path=db_path, audit_path=audit_path)
    if proposal is None:
        return ActionDecision(
            DECISION_UNKNOWN,
            None,
            f"No action proposal `{needle[:32]}` for `{persona}`.",
        )

    def _refuse_audited(reason: str, refused_message: str, *, error: str) -> ActionDecision:
        """One REQUIRED refusal row, or an honest audit-failed decision."""
        try:
            _append_ledger_row(
                persona_id=proposal.persona_id,
                tool_name=proposal.tool_name,
                operation="decide",
                outcome=OUTCOME_REFUSED,
                reason=reason,
                actor=who,
                actor_role=role,
                surface=surface,
                channel_id=channel,
                source=src,
                summary=proposal.summary,
                error=error,
                correlation_id=proposal.action_id,
                audit_path=audit_path,
            )
        except Exception as exc:  # noqa: BLE001 — refuse honestly, never a phantom receipt
            _logger.error(
                "action proposals: refusal (%s) for %s/%s could not be audited: %s: %s",
                reason,
                proposal.persona_id,
                proposal.tool_name,
                type(exc).__name__,
                exc,
            )
            return ActionDecision(
                DECISION_AUDIT_FAILED,
                proposal,
                refused_message + " The refusal itself could not be recorded — try again.",
            )
        return ActionDecision(DECISION_REFUSED, proposal, refused_message)

    try:
        from security import kill_switches  # noqa: PLC0415 — Rule 3 module attr
    except Exception as exc:  # noqa: BLE001 — same precedent as propose_action
        _logger.warning(
            "action proposals: kill-switch module unavailable (%s: %s)",
            type(exc).__name__,
            exc,
        )
    else:
        try:
            kill_switches.requireEnabled(KILL_SWITCH_NAME, caller="personas.decide_action")
        except kill_switches.KillSwitchDisabled:
            # Audited best-effort, then PROPAGATED. The surface is off; the
            # chat handler answers "disabled" from the exception, and no
            # half-decision is ever recorded as one.
            _audit(
                persona_id=proposal.persona_id,
                tool_name=proposal.tool_name,
                operation="decide",
                outcome=OUTCOME_REFUSED,
                reason=REASON_KILL_SWITCH,
                actor=who,
                actor_role=role,
                surface=surface,
                channel_id=channel,
                source=src,
                summary=proposal.summary,
                correlation_id=proposal.action_id,
                audit_path=audit_path,
            )
            raise

    if role != ADMIN_ROLE:
        return _refuse_audited(
            REASON_NOT_AUTHORIZED,
            f"Refused: deciding a persona action requires the {ADMIN_ROLE} role, "
            f"got {role or 'none'!r}. Nothing changed.",
            error=f"decision requires the {ADMIN_ROLE} role",
        )

    if src != REQUIRED_SOURCE:
        return _refuse_audited(
            REASON_NON_INTERACTIVE_SOURCE,
            "Refused: persona actions can only be decided from an interactive "
            f"operator turn, got source {src or 'none'!r}. Nothing changed.",
            error=f"decision requires source {REQUIRED_SOURCE!r}",
        )

    if proposal.status == STATUS_EXPIRED:
        return ActionDecision(
            DECISION_EXPIRED,
            proposal,
            f"Action `{proposal.short_code}` expired and can no longer be approved. "
            f"Ask `{proposal.persona_id}` to propose it again if it still matters.",
        )
    if proposal.status != STATUS_PENDING:
        return ActionDecision(
            DECISION_ALREADY_DECIDED,
            proposal,
            f"Action `{proposal.short_code}` is already "
            f"{proposal.status}{': ' + proposal.status_detail if proposal.status_detail else ''}.",
        )

    if approved:
        # Mint the one-use execution token INSIDE the winning CAS: the token,
        # and the payload hash it is bound to, land atomically with the
        # decision they authorize. A double tap loses the CAS and mints
        # nothing; a token presented for any other payload or after
        # consumption fails closed in ``consume_execution_token``.
        execution_token = uuid.uuid4().hex
        extra_sql = ", payload_hash = ?, execution_token_hash = ?"
        extra_params: tuple[str, ...] = (
            _payload_hash(proposal.arguments),
            _token_hash(execution_token),
        )
    else:
        execution_token = ""
        extra_sql = ""
        extra_params = ()
    next_status = STATUS_APPROVED if approved else STATUS_DENIED
    conn = _connect(persona, db_path)
    try:
        cursor = conn.execute(
            "UPDATE persona_action_proposals SET status = ?, decided_by = ?, "
            f"decided_at = ?, status_detail = ?{extra_sql} "
            "WHERE action_id = ? AND status = ?",
            (
                next_status,
                who,
                current,
                "operator approved the action" if approved else "operator denied",
                *extra_params,
                proposal.action_id,
                STATUS_PENDING,
            ),
        )
        conn.commit()
        won = cursor.rowcount == 1
    finally:
        conn.close()

    if not won:
        return ActionDecision(
            DECISION_ALREADY_DECIDED,
            proposal,
            f"Action `{proposal.short_code}` was already decided.",
        )

    if not approved:
        _audit(
            persona_id=proposal.persona_id,
            tool_name=proposal.tool_name,
            operation="decide",
            outcome=OUTCOME_DENIED,
            actor=who,
            actor_role=role,
            surface=surface,
            channel_id=channel,
            source=src,
            summary=proposal.summary,
            correlation_id=proposal.action_id,
            audit_path=audit_path,
        )
        return ActionDecision(
            DECISION_DENIED,
            proposal,
            f"Denied `{proposal.tool_name}` for `{proposal.persona_id}`. Nothing executed.",
        )

    _audit(
        persona_id=proposal.persona_id,
        tool_name=proposal.tool_name,
        operation="decide",
        outcome=OUTCOME_APPROVED,
        actor=who,
        actor_role=role,
        surface=surface,
        channel_id=channel,
        source=src,
        summary=proposal.summary,
        correlation_id=proposal.action_id,
        audit_path=audit_path,
    )

    executor = get_action_executor(proposal.tool_name)
    if executor is None:
        # LOUD, audited, and recorded on the row: an approval that cannot
        # execute is not a success with softer wording. The approval stands
        # (re-deciding would be worse), but nobody is told anything ran.
        detail = f"no executor registered for {proposal.tool_name!r}"
        _record_outcome(persona, proposal.action_id, status_detail=detail, db_path=db_path)
        _audit(
            persona_id=proposal.persona_id,
            tool_name=proposal.tool_name,
            operation="execute",
            outcome=OUTCOME_FAILED,
            reason=REASON_NO_EXECUTOR,
            actor=who,
            actor_role=role,
            surface=surface,
            channel_id=channel,
            source=src,
            summary=proposal.summary,
            error=detail,
            correlation_id=proposal.action_id,
            audit_path=audit_path,
        )
        _write_experience_receipt(proposal, outcome=OUTCOME_FAILED, detail=detail)
        return ActionDecision(
            DECISION_FAILED,
            proposal,
            f"Approved, but `{proposal.tool_name}` has no registered executor — "
            "nothing ran. This is a wiring bug; it was audited.",
        )

    decided = ActionProposal(
        **{**proposal.__dict__, "status": STATUS_APPROVED, "decided_by": who, "decided_at": current}
    )
    try:
        # The STORED payload, deep-copied — the deciding turn's arguments are
        # never consulted, because there is no path for them to arrive by.
        # The execution token is the only proof this call passed the gate.
        result = executor(
            persona_id=proposal.persona_id,
            action_id=proposal.action_id,
            execution_token=execution_token,
            arguments=copy.deepcopy(proposal.arguments),
        )
    except Exception as exc:  # noqa: BLE001 — every failure class reports the same way
        detail = f"{type(exc).__name__}: {_safe_text(exc)}"
        _record_outcome(
            persona, proposal.action_id, status_detail=f"failed: {detail}", db_path=db_path
        )
        _audit(
            persona_id=proposal.persona_id,
            tool_name=proposal.tool_name,
            operation="execute",
            outcome=OUTCOME_FAILED,
            actor=who,
            actor_role=role,
            surface=surface,
            channel_id=channel,
            source=src,
            summary=proposal.summary,
            error=detail,
            correlation_id=proposal.action_id,
            audit_path=audit_path,
        )
        _write_experience_receipt(decided, outcome=OUTCOME_FAILED, detail=detail)
        return ActionDecision(
            DECISION_FAILED,
            decided,
            f"Approved, but the action failed: {detail}",
        )

    try:
        outcome_json = json.dumps(result, default=str, sort_keys=True)
    except Exception:  # noqa: BLE001 — a pathological receipt still gets recorded
        outcome_json = json.dumps(_safe_text(result, limit=_MAX_OUTCOME_CHARS))
    outcome_json = outcome_json[:_MAX_OUTCOME_CHARS]

    # Receipt truthfulness (Codex R1): the driver's own verdict decides what
    # the row, the ledger, and the persona's memory claim — never a blanket
    # "executed" over an ok:false receipt.
    verdict, per_handle = _classify_receipt(result)
    _record_outcome(
        persona,
        proposal.action_id,
        status_detail=verdict,
        outcome_json=outcome_json,
        db_path=db_path,
    )
    _audit(
        persona_id=proposal.persona_id,
        tool_name=proposal.tool_name,
        operation="execute",
        outcome=verdict,
        actor=who,
        actor_role=role,
        surface=surface,
        channel_id=channel,
        source=src,
        summary=proposal.summary,
        correlation_id=proposal.action_id,
        audit_path=audit_path,
    )
    _write_experience_receipt(
        decided, outcome=verdict, detail=verdict, per_handle=per_handle
    )
    if verdict == OUTCOME_FAILED:
        return ActionDecision(
            DECISION_FAILED,
            decided,
            f"Approved, but `{proposal.tool_name}` reported failure — "
            "check the per-handle receipt in the ledger.",
            result=result,
        )
    if verdict == OUTCOME_PARTIAL:
        landed = sum(1 for row in per_handle if _row_landed(row) == "full")
        return ActionDecision(
            DECISION_PARTIAL,
            decided,
            f"Partially executed `{proposal.tool_name}` for "
            f"`{proposal.persona_id}` — {landed} of {len(per_handle)} handle(s) "
            "landed; the experience note lists each outcome.",
            result=result,
        )
    return ActionDecision(
        DECISION_EXECUTED,
        decided,
        f"Executed `{proposal.tool_name}` for `{proposal.persona_id}` — "
        f"{proposal.summary or 'done'}.",
        result=result,
    )


__all__ = [
    "ADMIN_ROLE",
    "DECISION_ALREADY_DECIDED",
    "DECISION_AUDIT_FAILED",
    "DECISION_DENIED",
    "DECISION_ERROR",
    "DECISION_EXECUTED",
    "DECISION_EXPIRED",
    "DECISION_FAILED",
    "DECISION_PARTIAL",
    "DECISION_REFUSED",
    "DECISION_UNKNOWN",
    "KILL_SWITCH_NAME",
    "LEDGER_FILENAME",
    "OUTCOME_APPROVED",
    "OUTCOME_DENIED",
    "OUTCOME_EXECUTED",
    "OUTCOME_EXPIRED",
    "OUTCOME_FAILED",
    "OUTCOME_PARTIAL",
    "OUTCOME_PROPOSED",
    "OUTCOME_REFUSED",
    "REASON_ALREADY_DECIDED",
    "REASON_KILL_SWITCH",
    "REASON_NO_EXECUTOR",
    "REASON_NON_INTERACTIVE_SOURCE",
    "REASON_NOT_AUTHORIZED",
    "REASON_PROPOSAL_EXPIRED",
    "REASON_UNKNOWN_PROPOSAL",
    "REQUIRED_SOURCE",
    "STATUS_APPROVED",
    "STATUS_DENIED",
    "STATUS_EXPIRED",
    "STATUS_PENDING",
    "STORE_FILENAME",
    "ActionDecision",
    "ActionProposal",
    "card_text",
    "consume_execution_token",
    "decide_action",
    "expire_pending",
    "get_action",
    "get_action_executor",
    "list_pending",
    "proposal_ttl_seconds",
    "propose_action",
    "register_action_executor",
    "resolve_ledger_path",
    "resolve_store_path",
]
