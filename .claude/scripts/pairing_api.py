"""QR device pairing — Homie Mobile M2 (PRD homie consumer app).

Default-deny device pairing over the existing dashboard API surface. Flow:

    operator  POST /api/pair/start        -> bootstrap token + QR payload (audit)
    phone     scans QR, POST /api/pair/claim  -> device row lands PENDING (audit)
    operator  POST /api/pair/approve/{id} -> pending -> approved (audit)
    phone     POST /api/pair/poll         -> one-time credential release (audit)

Invariants (CLAUDE.md default-deny mutation policy):
    - A device NEVER receives a credential without an explicit operator
      approve action. Claim only parks it as ``pending``.
    - Bootstrap tokens are high-entropy, short-lived (10 min), single-use.
    - The credential release is one-time: a second poll after release gets
      ``consumed``, never the token again.
    - Every transition writes an ``audit_log`` row (reuses the dashboard's
      ``_audit_write``).

``/api/pair/claim`` and ``/api/pair/poll`` are reachable WITHOUT a bearer
token (the phone does not have one yet) — they self-authenticate with the
bootstrap token / poll secret in the body. Both secrets are compared by
sha256 hash with ``hmac.compare_digest``; plaintext never touches the DB.

Anti-pattern compliance: no config value bound in default args (Rule 1);
status/expiry checks read the physical row at call time (Rule 2).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import socket
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from dashboard_api import _audit_write
from dashboard_db import get_connection

router = APIRouter()

_PAIR_TTL_SECONDS = 600  # bootstrap lifetime — scan-to-claim window
_QR_PREFIX = "homie-pair:"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hash_matches(provided: str, stored_hash: str) -> bool:
    return hmac.compare_digest(_sha256(provided), stored_hash)


def _lan_ip() -> str:
    """Best-effort LAN IP (UDP connect trick — no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _dashboard_token() -> str:
    """Resolve the credential to release, at CALL time (Rule 1).

    ``DASHBOARD_TOKEN`` and ``ORCHESTRATION_API_TOKEN`` are aliases (the
    Hono 4-branch boot policy); either names the value the proxy accepts.
    Empty string = loopback dev-mode (no token configured anywhere).
    """
    return (os.getenv("DASHBOARD_TOKEN") or os.getenv("ORCHESTRATION_API_TOKEN") or "").strip()


class PairStartBody(BaseModel):
    gateway_url: str | None = Field(default=None, max_length=512)
    remote_url: str | None = Field(default=None, max_length=512)


class PairClaimBody(BaseModel):
    bootstrap: str = Field(min_length=8, max_length=128)
    # device_name/platform are cosmetic labels — accept generously and truncate
    # in the handler. A metadata field must NEVER 422 the security-critical claim
    # (a phone's build fingerprint can be 70+ chars).
    device_name: str = Field(default="", max_length=256)
    platform: str = Field(default="", max_length=256)


class PairPollBody(BaseModel):
    device_id: int
    poll_secret: str = Field(min_length=16, max_length=128)


@router.post("/api/pair/start")
def pair_start(body: PairStartBody) -> dict:
    """Operator action (bearer-gated by the shared middleware)."""
    # 12-char (~72-bit) single-use code — plenty for a 10-min bootstrap, and
    # short enough to keep the QR low-density so a phone decodes it fast.
    bootstrap = secrets.token_urlsafe(9)
    gateway_url = (body.gateway_url or "").strip() or f"http://{_lan_ip()}:3141"
    remote_url = (body.remote_url or "").strip()
    now = int(time.time())

    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT INTO pair_requests
               (bootstrap_hash, gateway_url, remote_url, status, expires_at)
               VALUES (?, ?, ?, 'issued', ?)""",
            (_sha256(bootstrap), gateway_url, remote_url, now + _PAIR_TTL_SECONDS),
        )
        conn.commit()
        pair_id = cur.lastrowid
    finally:
        conn.close()

    payload = {
        "v": 1,
        "gw": gateway_url,
        "tok": bootstrap,
        "src": "lan",
    }
    if remote_url:
        # Only carry the remote URL when set — a null field just bloats the QR.
        payload["rgw"] = remote_url
    _audit_write(
        operator_id="operator",
        action="pair.start",
        target_persona_id="default",
        outcome="issued",
        detail={"pair_id": pair_id, "gateway_url": gateway_url, "ttl_s": _PAIR_TTL_SECONDS},
    )
    return {
        "pair_id": pair_id,
        "payload": payload,
        "qr_text": _QR_PREFIX + json.dumps(payload, separators=(",", ":")),
        "expires_at": now + _PAIR_TTL_SECONDS,
    }


@router.post("/api/pair/claim")
def pair_claim(body: PairClaimBody) -> dict:
    """Phone action — self-authenticated by the scanned bootstrap token."""
    now = int(time.time())
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM pair_requests WHERE bootstrap_hash = ?",
            (_sha256(body.bootstrap),),
        ).fetchone()
        if row is None:
            _audit_write("device", "pair.claim", "default", "rejected", {"reason": "unknown bootstrap"}, blocked=True)
            raise HTTPException(status_code=403, detail="unknown or invalid setup code")
        if row["status"] != "issued":
            _audit_write("device", "pair.claim", "default", "rejected", {"pair_id": row["id"], "reason": "already used"}, blocked=True)
            raise HTTPException(status_code=403, detail="setup code already used")
        if now > int(row["expires_at"]):
            _audit_write("device", "pair.claim", "default", "rejected", {"pair_id": row["id"], "reason": "expired"}, blocked=True)
            raise HTTPException(status_code=403, detail="setup code expired")

        # Cosmetic labels — truncate to the stored width so an over-long
        # fingerprint can't brick pairing.
        device_name = body.device_name.strip()[:64]
        platform = body.platform.strip()[:32]
        poll_secret = secrets.token_urlsafe(32)
        conn.execute(
            """UPDATE pair_requests
               SET status = 'pending', device_name = ?, device_platform = ?,
                   poll_secret_hash = ?, claimed_at = ?
               WHERE id = ?""",
            (device_name, platform, _sha256(poll_secret), now, row["id"]),
        )
        conn.commit()
        pair_id = row["id"]
    finally:
        conn.close()

    _audit_write(
        operator_id="device",
        action="pair.claim",
        target_persona_id="default",
        outcome="pending",
        detail={"pair_id": pair_id, "device_name": device_name, "platform": platform},
    )
    return {"device_id": pair_id, "poll_secret": poll_secret, "status": "pending"}


@router.post("/api/pair/poll")
def pair_poll(body: PairPollBody) -> dict:
    """Phone action — self-authenticated by the claim-time poll secret."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM pair_requests WHERE id = ?", (body.device_id,)
        ).fetchone()
        if row is None or not row["poll_secret_hash"] or not _hash_matches(body.poll_secret, row["poll_secret_hash"]):
            raise HTTPException(status_code=403, detail="unknown device or bad poll secret")

        status = row["status"]
        if status == "pending":
            return {"status": "pending"}
        if status == "denied":
            return {"status": "denied"}
        if status != "approved":
            return {"status": status}
        if int(row["released"]):
            # One-time release invariant — the credential never repeats.
            return {"status": "consumed"}

        conn.execute(
            "UPDATE pair_requests SET released = 1 WHERE id = ?", (row["id"],)
        )
        conn.commit()
        gateway_url = row["gateway_url"]
        pair_id = row["id"]
    finally:
        conn.close()

    _audit_write(
        operator_id="device",
        action="pair.release",
        target_persona_id="default",
        outcome="released",
        detail={"pair_id": pair_id},
    )
    return {"status": "approved", "token": _dashboard_token(), "gateway_url": gateway_url}


@router.get("/api/pair/pending")
def pair_pending() -> dict:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT id, device_name, device_platform, claimed_at, expires_at
               FROM pair_requests WHERE status = 'pending'
               ORDER BY claimed_at DESC"""
        ).fetchall()
        return {"pending": [dict(r) for r in rows]}
    finally:
        conn.close()


def _decide(pair_id: int, decision: str) -> dict:
    now = int(time.time())
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT status FROM pair_requests WHERE id = ?", (pair_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="unknown pair request")
        if row["status"] != "pending":
            raise HTTPException(status_code=409, detail=f"pair request is {row['status']}, not pending")
        conn.execute(
            "UPDATE pair_requests SET status = ?, decided_at = ? WHERE id = ?",
            (decision, now, pair_id),
        )
        conn.commit()
    finally:
        conn.close()

    _audit_write(
        operator_id="operator",
        action=f"pair.{'approve' if decision == 'approved' else 'deny'}",
        target_persona_id="default",
        outcome=decision,
        detail={"pair_id": pair_id},
        blocked=decision == "denied",
    )
    return {"device_id": pair_id, "status": decision}


@router.post("/api/pair/approve/{pair_id}")
def pair_approve(pair_id: int) -> dict:
    """Operator action — the default-deny gate. Pending -> approved."""
    return _decide(pair_id, "approved")


@router.post("/api/pair/deny/{pair_id}")
def pair_deny(pair_id: int) -> dict:
    return _decide(pair_id, "denied")
