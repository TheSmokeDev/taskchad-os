"""QR pairing lifecycle tests (Homie Mobile M2).

Paths exercised (one test per distinct path, per the testing principle):
  happy: start -> claim -> approve -> poll releases token ONCE
  deny:  start -> claim -> deny -> poll reports denied, no token
  expiry: claim after TTL -> 403
  single-use bootstrap: second claim -> 403
  one-time release: second poll after release -> consumed, no token
  bad poll secret -> 403
  audit rows written per transition
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import config  # noqa: E402
import pairing_api  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", tmp_path / "dashboard.db")
    monkeypatch.setenv("DASHBOARD_TOKEN", "release-me-123")
    app = FastAPI()
    app.include_router(pairing_api.router)
    return TestClient(app)


def _start_and_claim(client):
    start = client.post("/api/pair/start", json={}).json()
    claim = client.post(
        "/api/pair/claim",
        json={"bootstrap": start["payload"]["tok"], "device_name": "pixel", "platform": "android"},
    ).json()
    return start, claim


def test_happy_path_start_claim_approve_release(client):
    start = client.post("/api/pair/start", json={"gateway_url": "http://10.0.0.5:3141"})
    assert start.status_code == 200
    body = start.json()
    assert body["qr_text"].startswith("homie-pair:{")
    assert body["payload"]["gw"] == "http://10.0.0.5:3141"

    claim = client.post(
        "/api/pair/claim",
        json={"bootstrap": body["payload"]["tok"], "device_name": "pixel", "platform": "android"},
    )
    assert claim.status_code == 200
    claimed = claim.json()
    assert claimed["status"] == "pending"

    # Default-deny: poll BEFORE approval must not release anything.
    pre = client.post(
        "/api/pair/poll",
        json={"device_id": claimed["device_id"], "poll_secret": claimed["poll_secret"]},
    ).json()
    assert pre == {"status": "pending"}

    approve = client.post(f"/api/pair/approve/{claimed['device_id']}")
    assert approve.status_code == 200

    poll = client.post(
        "/api/pair/poll",
        json={"device_id": claimed["device_id"], "poll_secret": claimed["poll_secret"]},
    ).json()
    assert poll["status"] == "approved"
    assert poll["token"] == "release-me-123"
    assert poll["gateway_url"] == "http://10.0.0.5:3141"


def test_deny_path_reports_denied_and_never_releases(client):
    _, claimed = _start_and_claim(client)
    assert client.post(f"/api/pair/deny/{claimed['device_id']}").status_code == 200
    poll = client.post(
        "/api/pair/poll",
        json={"device_id": claimed["device_id"], "poll_secret": claimed["poll_secret"]},
    ).json()
    assert poll == {"status": "denied"}
    assert "token" not in poll


def test_expired_bootstrap_rejected(client, monkeypatch):
    start = client.post("/api/pair/start", json={}).json()
    real_time = pairing_api.time.time
    monkeypatch.setattr(pairing_api.time, "time", lambda: real_time() + 601)
    claim = client.post("/api/pair/claim", json={"bootstrap": start["payload"]["tok"]})
    assert claim.status_code == 403
    assert "expired" in claim.json()["detail"]


def test_bootstrap_is_single_use(client):
    start, _ = _start_and_claim(client)
    second = client.post("/api/pair/claim", json={"bootstrap": start["payload"]["tok"]})
    assert second.status_code == 403
    assert "already used" in second.json()["detail"]


def test_release_is_one_time(client):
    _, claimed = _start_and_claim(client)
    client.post(f"/api/pair/approve/{claimed['device_id']}")
    body = {"device_id": claimed["device_id"], "poll_secret": claimed["poll_secret"]}
    first = client.post("/api/pair/poll", json=body).json()
    assert first["status"] == "approved" and first["token"]
    second = client.post("/api/pair/poll", json=body).json()
    assert second == {"status": "consumed"}


def test_long_platform_is_truncated_not_rejected(client):
    # Regression: a real phone sent its 70-char Android build fingerprint as
    # `platform`; the old max_length=32 422'd and bricked pairing. Now it must
    # succeed (200) and store a truncated label.
    from dashboard_db import get_connection

    start = client.post("/api/pair/start", json={}).json()
    fingerprint = "samsung/e2qsqw/e2q:16/bp4a.251205.006/s926usqu5dzdr:user/release-keys"
    assert len(fingerprint) > 32
    claim = client.post(
        "/api/pair/claim",
        json={"bootstrap": start["payload"]["tok"], "device_name": "x" * 200, "platform": fingerprint},
    )
    assert claim.status_code == 200
    assert claim.json()["status"] == "pending"
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT device_name, device_platform FROM pair_requests WHERE id = ?",
            (claim.json()["device_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert len(row["device_platform"]) <= 32
    assert len(row["device_name"]) <= 64


def test_bad_poll_secret_rejected(client):
    _, claimed = _start_and_claim(client)
    resp = client.post(
        "/api/pair/poll",
        json={"device_id": claimed["device_id"], "poll_secret": "x" * 32},
    )
    assert resp.status_code == 403


def test_unknown_bootstrap_rejected(client):
    resp = client.post("/api/pair/claim", json={"bootstrap": "y" * 32})
    assert resp.status_code == 403


def test_approve_requires_pending(client):
    _, claimed = _start_and_claim(client)
    client.post(f"/api/pair/approve/{claimed['device_id']}")
    again = client.post(f"/api/pair/approve/{claimed['device_id']}")
    assert again.status_code == 409


def test_audit_rows_written_per_transition(client):
    from dashboard_db import get_connection

    _, claimed = _start_and_claim(client)
    client.post(f"/api/pair/approve/{claimed['device_id']}")
    client.post(
        "/api/pair/poll",
        json={"device_id": claimed["device_id"], "poll_secret": claimed["poll_secret"]},
    )
    conn = get_connection()
    try:
        actions = [
            r["action"]
            for r in conn.execute(
                "SELECT action FROM audit_log ORDER BY id"
            ).fetchall()
        ]
    finally:
        conn.close()
    assert actions == ["pair.start", "pair.claim", "pair.approve", "pair.release"]


def test_pending_listing(client):
    _, claimed = _start_and_claim(client)
    pending = client.get("/api/pair/pending").json()["pending"]
    assert [p["id"] for p in pending] == [claimed["device_id"]]
    assert pending[0]["device_name"] == "pixel"
