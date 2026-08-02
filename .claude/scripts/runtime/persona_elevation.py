"""Bounded operator-approved capability elevation for persona turns.

This module owns the authority boundary for issue #262.  A persona can ask for
one registered tool with one exact argument payload.  The request is durable;
the grant is deliberately process-local and can be consumed only by one retry
for the same persona and channel.  Nothing here mutates persona configuration.

Dedicated-gate tools are never eligible.  The operator decision must arrive
through an authenticated chat event; this module exposes decision primitives
but never calls them from a model tool handler.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

KILL_SWITCH_NAME = "persona_elevation"
REQUEST_TOOL_NAME = "request_tool"
_REQUEST_TTL_S = 600
_GRANT_TTL_S = 300
_MAX_REASON_CHARS = 600
# Approval is only meaningful when the operator can see the whole payload on
# the originating chat card. Larger operations belong in an assigned toolset
# or a dedicated reviewed workflow, not a truncated one-tap exception.
_MAX_ARGUMENT_CHARS = 900
_MAX_ORIGINAL_TEXT_CHARS = 24_000

_TERMINAL_DEDICATED_GATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(?:^|\s)git\s+push\b"),
    re.compile(r"(?i)(?:^|\s)gh\s+(?:pr\s+merge|release\s+create)\b"),
    re.compile(r"(?i)(?:^|\s)(?:npm|pnpm|yarn|uv|twine)\s+publish\b"),
    re.compile(r"(?i)(?:^|\s)(?:vercel|netlify)\b"),
    re.compile(r"(?i)(?:^|\s)docker\s+push\b"),
    re.compile(r"(?i)(?:^|\s)kubectl\s+(?:apply|delete|patch|replace|scale)\b"),
    re.compile(r"(?i)(?:^|\s)terraform\s+(?:apply|destroy)\b"),
    re.compile(r"(?i)\bcurl\b[^\r\n]*(?:-X|--request)\s*(?:POST|PUT|PATCH|DELETE)\b"),
    re.compile(r"(?i)\bInvoke-(?:WebRequest|RestMethod)\b[^\r\n]*-Method\s+(?:POST|PUT|PATCH|DELETE)\b"),
    # Profile/authority mutation keeps its own explicit provisioner and kill
    # switches.  A generic shell grant must not become a second write path.
    re.compile(r"(?i)(?:\.homie|HOMIE_HOME|persona-capability-matrix|discord-channel-bindings)"),
    re.compile(r"(?i)HOMIE_KILLSWITCH_[A-Z0-9_]+"),
)


@dataclass(frozen=True)
class ElevationRequest:
    request_id: str
    short_code: str
    persona_id: str
    tool_name: str
    reason: str
    intended_arguments: dict[str, Any]
    status: str
    created_at: float
    expires_at: float
    platform: str
    channel_id: str
    thread_id: str
    guild_id: str
    session_key: str
    turn_id: str
    original_user_id: str
    original_user_name: str
    original_user_role: str
    original_text: str
    decision_operator_id: str = ""
    decided_at: float | None = None
    consumed_at: float | None = None
    status_detail: str = ""


@dataclass(frozen=True)
class ClaimedGrant:
    request_id: str
    persona_id: str
    tool_name: str
    intended_arguments: dict[str, Any]
    platform: str
    channel_id: str
    expires_at: float


@dataclass(frozen=True)
class DecisionResult:
    outcome: str
    request: ElevationRequest | None
    message: str


_GRANTS: dict[str, ClaimedGrant] = {}
_GRANT_LOCK = threading.RLock()


def _bounded_env_seconds(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _resolve_db_path(db_path: Path | str | None = None) -> Path:
    if db_path is not None:
        return Path(db_path)
    import config

    return Path(config.DATA_DIR) / "persona_elevation.db"


def _resolve_audit_path(audit_path: Path | str | None = None) -> Path:
    if audit_path is not None:
        return Path(audit_path)
    import config

    return Path(config.DATA_DIR) / "persona_elevation.jsonl"


def _connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = _resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS persona_elevation_requests (
            request_id TEXT PRIMARY KEY,
            short_code TEXT NOT NULL UNIQUE,
            persona_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            reason TEXT NOT NULL,
            intended_arguments_json TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            platform TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            guild_id TEXT NOT NULL,
            session_key TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            original_user_id TEXT NOT NULL,
            original_user_name TEXT NOT NULL,
            original_user_role TEXT NOT NULL,
            original_text TEXT NOT NULL,
            decision_operator_id TEXT NOT NULL DEFAULT '',
            decided_at REAL,
            consumed_at REAL,
            status_detail TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_persona_elevation_turn "
        "ON persona_elevation_requests(persona_id, turn_id, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_persona_elevation_status "
        "ON persona_elevation_requests(status, expires_at)"
    )
    conn.commit()
    return conn


def _row_to_request(row: sqlite3.Row | None) -> ElevationRequest | None:
    if row is None:
        return None
    try:
        intended = json.loads(row["intended_arguments_json"])
    except (TypeError, json.JSONDecodeError):
        intended = {}
    if not isinstance(intended, dict):
        intended = {}
    return ElevationRequest(
        request_id=str(row["request_id"]),
        short_code=str(row["short_code"]),
        persona_id=str(row["persona_id"]),
        tool_name=str(row["tool_name"]),
        reason=str(row["reason"]),
        intended_arguments=intended,
        status=str(row["status"]),
        created_at=float(row["created_at"]),
        expires_at=float(row["expires_at"]),
        platform=str(row["platform"]),
        channel_id=str(row["channel_id"]),
        thread_id=str(row["thread_id"]),
        guild_id=str(row["guild_id"]),
        session_key=str(row["session_key"]),
        turn_id=str(row["turn_id"]),
        original_user_id=str(row["original_user_id"]),
        original_user_name=str(row["original_user_name"]),
        original_user_role=str(row["original_user_role"]),
        original_text=str(row["original_text"]),
        decision_operator_id=str(row["decision_operator_id"] or ""),
        decided_at=(float(row["decided_at"]) if row["decided_at"] is not None else None),
        consumed_at=(float(row["consumed_at"]) if row["consumed_at"] is not None else None),
        status_detail=str(row["status_detail"] or ""),
    )


def canonical_arguments(arguments: dict[str, Any]) -> str:
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _arguments_digest(arguments: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_arguments(arguments).encode("utf-8")).hexdigest()


def _audit(
    event: str,
    request: ElevationRequest | None,
    *,
    outcome: str,
    operator_id: str = "",
    detail: str = "",
    persona_id: str = "",
    tool_name: str = "",
    reason: str = "",
    intended_arguments: dict[str, Any] | None = None,
    audit_path: Path | str | None = None,
) -> None:
    effective_persona_id = request.persona_id if request else persona_id
    effective_tool_name = request.tool_name if request else tool_name
    effective_reason = request.reason if request else reason
    effective_arguments = request.intended_arguments if request else intended_arguments
    arguments_digest = ""
    if isinstance(effective_arguments, dict):
        try:
            arguments_digest = _arguments_digest(effective_arguments)
        except (TypeError, ValueError):
            arguments_digest = "unserializable"
    record = {
        "timestamp": time.time(),
        "event": event,
        "outcome": outcome,
        "operator_id": operator_id,
        "request_id": request.request_id if request else "",
        "short_code": request.short_code if request else "",
        "persona_id": effective_persona_id,
        "tool_name": effective_tool_name,
        "reason_preview": effective_reason[:160],
        "arguments_sha256": arguments_digest,
        "detail": detail[:300],
    }
    try:
        from shared import file_lock

        path = _resolve_audit_path(audit_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path, timeout=5):
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception as exc:  # noqa: BLE001
        _logger.error("persona elevation audit append failed: %s", exc)

    try:
        from dashboard_api import _audit_write

        _audit_write(
            operator_id=operator_id or "persona_elevation",
            action=f"persona_elevation_{event}",
            target_persona_id=effective_persona_id,
            outcome=outcome,
            detail=record,
            blocked=outcome in {"denied", "expired", "refused", "invalid"},
        )
    except Exception:
        # The append-only private ledger above is the canonical receipt.  The
        # dashboard row is an additive observer surface.
        pass


def _refused_request(
    error: str,
    *,
    persona_id: str,
    tool_name: str,
    reason: str,
    intended_arguments: dict[str, Any] | None,
    audit_path: Path | str | None,
) -> str:
    _audit(
        "request",
        None,
        outcome="refused",
        detail=error,
        persona_id=persona_id,
        tool_name=tool_name,
        reason=reason,
        intended_arguments=intended_arguments,
        audit_path=audit_path,
    )
    return json.dumps({"status": "refused", "error": error})


def _expire_pending(
    *,
    now: float | None = None,
    db_path: Path | str | None = None,
    audit_path: Path | str | None = None,
) -> list[ElevationRequest]:
    current = time.time() if now is None else float(now)
    expired: list[ElevationRequest] = []
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT * FROM persona_elevation_requests "
            "WHERE status = 'pending' AND expires_at <= ?",
            (current,),
        ).fetchall()
        ids = [str(row["request_id"]) for row in rows]
        if ids:
            conn.executemany(
                "UPDATE persona_elevation_requests SET status = 'expired', "
                "decided_at = ?, status_detail = 'request TTL elapsed' "
                "WHERE request_id = ? AND status = 'pending'",
                [(current, request_id) for request_id in ids],
            )
        conn.commit()
        expired = [req for row in rows if (req := _row_to_request(row)) is not None]
    finally:
        conn.close()
    for req in expired:
        _audit(
            "expiry",
            req,
            outcome="expired",
            detail="request TTL elapsed",
            audit_path=audit_path,
        )
    return expired


def build_turn_context(
    persona_id: str,
    incoming: Any,
    *,
    session_key: str,
    turn_id: str | None = None,
    project_root: Path | str | None = None,
) -> dict[str, Any]:
    """Copy only bounded primitive origin data into the tool dispatcher."""

    platform = getattr(getattr(incoming, "platform", None), "value", None) or str(
        getattr(incoming, "platform", "")
    )
    channel = getattr(incoming, "channel", None)
    thread = getattr(incoming, "thread", None)
    user = getattr(incoming, "user", None)
    raw_event = getattr(incoming, "raw_event", None)
    raw_event = raw_event if isinstance(raw_event, dict) else {}
    stable_turn_id = (
        str(turn_id or "").strip()
        or str(raw_event.get("elevation_original_turn_id") or "").strip()
        or str(getattr(incoming, "platform_message_id", "") or "").strip()
        or uuid.uuid4().hex
    )
    return {
        "persona_id": persona_id,
        "platform": str(platform),
        "channel_id": str(getattr(channel, "platform_id", "") or ""),
        "channel_name": str(getattr(channel, "name", "") or ""),
        "channel_is_dm": bool(getattr(channel, "is_dm", False)),
        "thread_id": str(getattr(thread, "thread_id", "") or ""),
        "thread_parent_message_id": str(
            getattr(thread, "parent_message_id", "") or ""
        ),
        "guild_id": str(raw_event.get("guild") or ""),
        "session_key": str(session_key),
        "turn_id": stable_turn_id,
        "original_user_id": str(getattr(user, "platform_id", "") or ""),
        "original_user_name": str(getattr(user, "display_name", "") or ""),
        "original_user_role": str(getattr(incoming, "user_role", "admin") or "admin"),
        "original_text": str(getattr(incoming, "text", "") or "")[:_MAX_ORIGINAL_TEXT_CHARS],
        "has_attachments": bool(getattr(incoming, "attachments", None)),
        "project_root": str(Path(project_root).resolve()) if project_root else "",
    }


def _validate_elevation_arguments(
    tool_name: str,
    arguments: dict[str, Any],
    context: dict[str, Any],
) -> str:
    if tool_name in {"write_file", "patch"}:
        raw_path = str(arguments.get("path") or "").strip()
        if not raw_path:
            return "path is required"
        project_root = str(context.get("project_root") or "").strip()
        try:
            candidate = Path(raw_path).expanduser()
            if not candidate.is_absolute():
                if not project_root:
                    return "project root is unavailable"
                candidate = Path(project_root) / candidate
            candidate = candidate.resolve(strict=False)
            root = Path(project_root).resolve(strict=False)
            candidate.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            return "one-time file writes are confined to the current project"

    if tool_name == "terminal":
        command = str(arguments.get("command") or "").strip()
        if not command:
            return "command is required"
        for pattern in _TERMINAL_DEDICATED_GATE_PATTERNS:
            if pattern.search(command):
                return "command crosses a dedicated external/profile authority gate"

    return ""


def request_tool(
    tool: str = "",
    reason: str = "",
    arguments: dict[str, Any] | None = None,
    *,
    _persona_id: str | None = None,
    _dispatch_context: dict[str, Any] | None = None,
    db_path: Path | str | None = None,
    audit_path: Path | str | None = None,
    now: float | None = None,
    **_: Any,
) -> str:
    """Model-facing handler.  It creates a request; it never creates a grant."""

    context = dict(_dispatch_context or {})
    persona_id = str(_persona_id or context.get("persona_id") or "").strip()
    tool_name = str(tool or "").strip()
    rationale = str(reason or "").strip()[:_MAX_REASON_CHARS]
    intended = dict(arguments or {}) if isinstance(arguments or {}, dict) else None
    current = time.time() if now is None else float(now)

    try:
        from security import kill_switches

        if kill_switches.is_disabled(KILL_SWITCH_NAME):
            return _refused_request(
                "capability elevation is disabled by operator",
                persona_id=persona_id,
                tool_name=tool_name,
                reason=rationale,
                intended_arguments=intended,
                audit_path=audit_path,
            )
    except Exception:
        pass

    if not persona_id or not context.get("turn_id") or not context.get("channel_id"):
        return _refused_request(
            "capability requests require an authenticated direct chat turn",
            persona_id=persona_id,
            tool_name=tool_name,
            reason=rationale,
            intended_arguments=intended,
            audit_path=audit_path,
        )
    if context.get("has_attachments"):
        return _refused_request(
            "attachment turns cannot be retried safely; ask again with a text-only task",
            persona_id=persona_id,
            tool_name=tool_name,
            reason=rationale,
            intended_arguments=intended,
            audit_path=audit_path,
        )
    if not tool_name or not rationale or intended is None:
        return _refused_request(
            "tool, reason, and arguments are required",
            persona_id=persona_id,
            tool_name=tool_name,
            reason=rationale,
            intended_arguments=intended,
            audit_path=audit_path,
        )
    try:
        encoded_args = canonical_arguments(intended)
    except (TypeError, ValueError) as exc:
        return _refused_request(
            f"arguments are not JSON-safe: {exc}",
            persona_id=persona_id,
            tool_name=tool_name,
            reason=rationale,
            intended_arguments=intended,
            audit_path=audit_path,
        )
    if len(encoded_args) > _MAX_ARGUMENT_CHARS:
        return _refused_request(
            "arguments exceed the request limit",
            persona_id=persona_id,
            tool_name=tool_name,
            reason=rationale,
            intended_arguments=intended,
            audit_path=audit_path,
        )

    from runtime import tool_registry

    entry = tool_registry.get_entry(tool_name)
    if entry is None or entry.handler is None:
        return _refused_request(
            f"tool {tool_name!r} is not callable",
            persona_id=persona_id,
            tool_name=tool_name,
            reason=rationale,
            intended_arguments=intended,
            audit_path=audit_path,
        )
    if tool_name in set(context.get("granted_tools") or ()):
        return _refused_request(
            f"tool {tool_name!r} is already in this persona's scope",
            persona_id=persona_id,
            tool_name=tool_name,
            reason=rationale,
            intended_arguments=intended,
            audit_path=audit_path,
        )
    if not entry.elevatable or entry.dedicated_gate:
        return _refused_request(
            f"tool {tool_name!r} cannot use one-time elevation",
            persona_id=persona_id,
            tool_name=tool_name,
            reason=rationale,
            intended_arguments=intended,
            audit_path=audit_path,
        )
    policy_error = _validate_elevation_arguments(tool_name, intended, context)
    if policy_error:
        return _refused_request(
            policy_error,
            persona_id=persona_id,
            tool_name=tool_name,
            reason=rationale,
            intended_arguments=intended,
            audit_path=audit_path,
        )

    _expire_pending(now=current, db_path=db_path, audit_path=audit_path)
    conn = _connect(db_path)
    created: ElevationRequest | None = None
    existing: ElevationRequest | None = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM persona_elevation_requests "
            "WHERE persona_id = ? AND turn_id = ? ORDER BY created_at DESC LIMIT 1",
            (persona_id, str(context["turn_id"])),
        ).fetchone()
        existing = _row_to_request(row)
        if existing is None:
            request_id = uuid.uuid4().hex
            short_code = request_id[:10].upper()
            ttl = _bounded_env_seconds(
                "HOMIE_CAPABILITY_REQUEST_TTL_SECONDS",
                _REQUEST_TTL_S,
                minimum=30,
                maximum=3600,
            )
            values = {
                "request_id": request_id,
                "short_code": short_code,
                "persona_id": persona_id,
                "tool_name": tool_name,
                "reason": rationale,
                "intended_arguments_json": encoded_args,
                "status": "pending",
                "created_at": current,
                "expires_at": current + ttl,
                "platform": str(context.get("platform") or ""),
                "channel_id": str(context.get("channel_id") or ""),
                "thread_id": str(context.get("thread_id") or ""),
                "guild_id": str(context.get("guild_id") or ""),
                "session_key": str(context.get("session_key") or ""),
                "turn_id": str(context.get("turn_id") or ""),
                "original_user_id": str(context.get("original_user_id") or ""),
                "original_user_name": str(context.get("original_user_name") or ""),
                "original_user_role": str(context.get("original_user_role") or "admin"),
                "original_text": str(context.get("original_text") or ""),
            }
            conn.execute(
                """
                INSERT INTO persona_elevation_requests (
                    request_id, short_code, persona_id, tool_name, reason,
                    intended_arguments_json, status, created_at, expires_at,
                    platform, channel_id, thread_id, guild_id, session_key,
                    turn_id, original_user_id, original_user_name,
                    original_user_role, original_text
                ) VALUES (
                    :request_id, :short_code, :persona_id, :tool_name, :reason,
                    :intended_arguments_json, :status, :created_at, :expires_at,
                    :platform, :channel_id, :thread_id, :guild_id, :session_key,
                    :turn_id, :original_user_id, :original_user_name,
                    :original_user_role, :original_text
                )
                """,
                values,
            )
            created = _row_to_request(
                conn.execute(
                    "SELECT * FROM persona_elevation_requests WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
            )
        conn.commit()
    finally:
        conn.close()

    if existing is not None:
        if existing.status == "pending":
            existing_error = (
                "this turn already created a capability request; "
                "wait for the operator decision"
            )
        else:
            existing_error = (
                "this turn already used its one capability request; "
                f"the recorded decision is {existing.status}"
            )
        return json.dumps(
            {
                "status": existing.status,
                "request_id": existing.request_id,
                "short_code": existing.short_code,
                "error": existing_error,
            }
        )
    assert created is not None
    _audit("request", created, outcome="pending", audit_path=audit_path)
    return json.dumps(
        {
            "status": "approval_required",
            "request_id": created.request_id,
            "short_code": created.short_code,
            "tool": created.tool_name,
            "expires_at": created.expires_at,
            "instruction": (
                "Stop and tell the operator approval is required. "
                "Do not claim the tool ran."
            ),
        }
    )


def get_request(
    request_id_or_code: str,
    *,
    db_path: Path | str | None = None,
    audit_path: Path | str | None = None,
    now: float | None = None,
) -> ElevationRequest | None:
    _expire_pending(now=now, db_path=db_path, audit_path=audit_path)
    needle = str(request_id_or_code or "").strip()
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM persona_elevation_requests "
            "WHERE request_id = ? OR upper(short_code) = upper(?)",
            (needle, needle),
        ).fetchone()
        return _row_to_request(row)
    finally:
        conn.close()


def pending_request_for_turn(
    persona_id: str,
    turn_id: str,
    *,
    db_path: Path | str | None = None,
    audit_path: Path | str | None = None,
    now: float | None = None,
) -> ElevationRequest | None:
    _expire_pending(now=now, db_path=db_path, audit_path=audit_path)
    conn = _connect(db_path)
    try:
        return _row_to_request(
            conn.execute(
                "SELECT * FROM persona_elevation_requests "
                "WHERE persona_id = ? AND turn_id = ? AND status = 'pending' "
                "ORDER BY created_at DESC LIMIT 1",
                (persona_id, turn_id),
            ).fetchone()
        )
    finally:
        conn.close()


def decide_request(
    request_id_or_code: str,
    *,
    approve: bool,
    operator_id: str,
    platform: str,
    channel_id: str,
    db_path: Path | str | None = None,
    audit_path: Path | str | None = None,
    now: float | None = None,
) -> DecisionResult:
    """CAS a pending request to approved/denied and mint a process-local grant."""

    current = time.time() if now is None else float(now)
    _expire_pending(now=current, db_path=db_path, audit_path=audit_path)
    needle = str(request_id_or_code or "").strip()
    conn = _connect(db_path)
    request: ElevationRequest | None = None
    outcome = "invalid"
    message = "Capability request not found."
    try:
        conn.execute("BEGIN IMMEDIATE")
        request = _row_to_request(
            conn.execute(
                "SELECT * FROM persona_elevation_requests "
                "WHERE request_id = ? OR upper(short_code) = upper(?)",
                (needle, needle),
            ).fetchone()
        )
        if request is None:
            conn.rollback()
            return DecisionResult(outcome, None, message)
        if request.platform != str(platform) or request.channel_id != str(channel_id):
            conn.rollback()
            _audit(
                "decision",
                request,
                outcome="refused",
                operator_id=operator_id,
                detail="origin surface mismatch",
                audit_path=audit_path,
            )
            return DecisionResult(
                "refused",
                request,
                "Approve this request from its originating channel.",
            )
        if request.status != "pending":
            conn.rollback()
            return DecisionResult(
                "already_decided",
                request,
                f"Capability request is already {request.status}.",
            )

        next_status = "approved" if approve else "denied"
        cursor = conn.execute(
            "UPDATE persona_elevation_requests SET status = ?, "
            "decision_operator_id = ?, decided_at = ?, status_detail = ? "
            "WHERE request_id = ? AND status = 'pending'",
            (
                next_status,
                str(operator_id),
                current,
                "operator approved one exact call" if approve else "operator denied",
                request.request_id,
            ),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return DecisionResult(
                "already_decided",
                request,
                "Capability request was already decided.",
            )
        conn.commit()
        request = get_request(
            request.request_id,
            db_path=db_path,
            audit_path=audit_path,
            now=current,
        )
    finally:
        conn.close()

    assert request is not None
    if approve:
        ttl = _bounded_env_seconds(
            "HOMIE_CAPABILITY_GRANT_TTL_SECONDS",
            _GRANT_TTL_S,
            minimum=15,
            maximum=900,
        )
        grant = ClaimedGrant(
            request_id=request.request_id,
            persona_id=request.persona_id,
            tool_name=request.tool_name,
            intended_arguments=dict(request.intended_arguments),
            platform=request.platform,
            channel_id=request.channel_id,
            expires_at=min(request.expires_at, current + ttl),
        )
        with _GRANT_LOCK:
            _GRANTS[request.request_id] = grant
        outcome = "approved"
        message = (
            f"Approved `{request.tool_name}` once for `{request.persona_id}`. "
            "Retrying the original task now."
        )
        _audit("grant", request, outcome=outcome, operator_id=operator_id, audit_path=audit_path)
    else:
        outcome = "denied"
        message = f"Denied `{request.tool_name}` for `{request.persona_id}`."
        _audit("deny", request, outcome=outcome, operator_id=operator_id, audit_path=audit_path)
    return DecisionResult(outcome, request, message)


def claim_grant(
    request_id: str,
    *,
    persona_id: str,
    platform: str,
    channel_id: str,
    db_path: Path | str | None = None,
    audit_path: Path | str | None = None,
    now: float | None = None,
) -> tuple[ClaimedGrant | None, str]:
    """Consume the process-local grant before assembling the approved retry."""

    current = time.time() if now is None else float(now)
    with _GRANT_LOCK:
        grant = _GRANTS.get(request_id)
        if grant is None:
            request = get_request(request_id, db_path=db_path, audit_path=audit_path, now=current)
            if request and request.status == "approved":
                _mark_expired_after_restart(
                    request,
                    current,
                    db_path=db_path,
                    audit_path=audit_path,
                )
            return None, "the one-time grant is unavailable (expired or process restarted)"
        if (
            grant.persona_id != persona_id
            or grant.platform != platform
            or grant.channel_id != channel_id
        ):
            return None, "the one-time grant does not match this persona/channel"
        if grant.expires_at <= current:
            _GRANTS.pop(request_id, None)
            request = get_request(request_id, db_path=db_path, audit_path=audit_path, now=current)
            if request:
                _mark_expired_after_restart(
                    request,
                    current,
                    db_path=db_path,
                    audit_path=audit_path,
                )
            return None, "the one-time grant expired"
        _GRANTS.pop(request_id, None)

    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            "UPDATE persona_elevation_requests SET status = 'consumed', consumed_at = ?, "
            "status_detail = 'grant claimed for one retry' "
            "WHERE request_id = ? AND status = 'approved'",
            (current, request_id),
        )
        conn.commit()
        if cursor.rowcount != 1:
            return None, "the one-time grant was already consumed"
    finally:
        conn.close()
    request = get_request(request_id, db_path=db_path, audit_path=audit_path, now=current)
    if request:
        _audit("consume", request, outcome="consumed", audit_path=audit_path)
    return grant, ""


def _mark_expired_after_restart(
    request: ElevationRequest,
    now: float,
    *,
    db_path: Path | str | None,
    audit_path: Path | str | None,
) -> None:
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            "UPDATE persona_elevation_requests SET status = 'expired', decided_at = ?, "
            "status_detail = 'process-local grant unavailable' "
            "WHERE request_id = ? AND status = 'approved'",
            (now, request.request_id),
        )
        conn.commit()
    finally:
        conn.close()
    if cursor.rowcount == 1:
        updated = get_request(request.request_id, db_path=db_path, audit_path=audit_path, now=now)
        _audit(
            "expiry",
            updated or request,
            outcome="expired",
            detail="process-local grant unavailable",
            audit_path=audit_path,
        )


def invalidate_grant(
    request_id: str,
    *,
    detail: str,
    db_path: Path | str | None = None,
    audit_path: Path | str | None = None,
    now: float | None = None,
) -> bool:
    """Revoke an approved grant when automatic resumption cannot start safely."""

    current = time.time() if now is None else float(now)
    with _GRANT_LOCK:
        _GRANTS.pop(request_id, None)
    request = get_request(request_id, db_path=db_path, audit_path=audit_path, now=current)
    if request is None:
        return False
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            "UPDATE persona_elevation_requests SET status = 'expired', decided_at = ?, "
            "status_detail = ? WHERE request_id = ? AND status = 'approved'",
            (current, str(detail)[:300], request_id),
        )
        conn.commit()
    finally:
        conn.close()
    if cursor.rowcount != 1:
        return False
    updated = get_request(request_id, db_path=db_path, audit_path=audit_path, now=current)
    _audit(
        "expiry",
        updated or request,
        outcome="expired",
        detail=detail,
        audit_path=audit_path,
    )
    return True


def arguments_match(grant: ClaimedGrant, arguments: Any) -> bool:
    if not isinstance(arguments, dict):
        return False
    try:
        return canonical_arguments(arguments) == canonical_arguments(grant.intended_arguments)
    except (TypeError, ValueError):
        return False


def request_card_text(request: ElevationRequest) -> str:
    args = canonical_arguments(request.intended_arguments)
    return (
        f"Capability request `{request.short_code}`\n"
        f"`{request.persona_id}` needs `{request.tool_name}` once.\n"
        f"Why: {request.reason}\n"
        f"Exact arguments: `{args}`\n"
        f"Approve to retry the original task once. No profile permissions will change."
    )


def request_tool_schema() -> tuple[str, str, str, dict[str, Any], Any]:
    return (
        REQUEST_TOOL_NAME,
        "safe_core",
        "Ask the operator for one exact call to a registered tool outside your scope. "
        "Use only when the task is blocked. Supply the exact arguments you intend to call; "
        "approval never changes your permanent permissions.",
        {
            "type": "object",
            "properties": {
                "tool": {"type": "string", "description": "Exact registered tool name."},
                "reason": {
                    "type": "string",
                    "description": "Why this task needs the tool and what outcome it enables.",
                },
                "arguments": {
                    "type": "object",
                    "description": "The exact JSON arguments for the one approved call.",
                    "additionalProperties": True,
                },
            },
            "required": ["tool", "reason", "arguments"],
        },
        request_tool,
    )


def register_tools() -> int:
    from runtime import tool_registry

    name, toolset, description, parameters, handler = request_tool_schema()
    try:
        tool_registry.register_tool(
            name,
            description,
            toolset=toolset,
            parameters=parameters,
            handler=handler,
            effect="read",
            persona_scoped=True,
            dispatch_context_scoped=True,
            elevatable=False,
        )
        return 1
    except Exception:  # noqa: BLE001
        _logger.warning("failed to register persona elevation tool", exc_info=True)
        return 0


def clear_process_grants_for_tests() -> None:
    with _GRANT_LOCK:
        _GRANTS.clear()


__all__ = [
    "ClaimedGrant",
    "DecisionResult",
    "ElevationRequest",
    "KILL_SWITCH_NAME",
    "REQUEST_TOOL_NAME",
    "arguments_match",
    "build_turn_context",
    "canonical_arguments",
    "claim_grant",
    "clear_process_grants_for_tests",
    "decide_request",
    "get_request",
    "invalidate_grant",
    "pending_request_for_turn",
    "register_tools",
    "request_card_text",
    "request_tool",
]
