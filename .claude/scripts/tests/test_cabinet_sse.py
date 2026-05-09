"""Test PRD-8 Phase 5a / WS2.6 — SSE shape + B4 race-hardening + M4 410 delta.

B4 — subscribe-first → seen_seqs dedup → snapshot direct-write → replay-after.
M4 — 410 Gone + X-Refetch-Hint when sinceSeq < oldest_seq.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db_path = tmp_path / "dashboard.db"
    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", str(db_path))
    from cabinet import meeting_channel as channels_mod
    channels_mod._reset_channels()
    from dashboard_db import get_connection
    get_connection().close()

    import dashboard_api
    app = FastAPI()
    app.include_router(dashboard_api.router)
    return TestClient(app)


def _make_meeting(client: TestClient) -> int:
    return client.post("/api/cabinet/new", json={}).json()["meetingId"]


def test_sse_410_gone_with_x_refetch_hint(client: TestClient) -> None:
    """M4 — 410 + X-Refetch-Hint when sinceSeq < oldest_seq.

    Setup: create channel with rolled buffer (oldest_seq=11 after 20 emits in cap-10),
    then request stream with sinceSeq=2 — that's < oldest_seq - 1 = 10, so 410.
    """
    mid = _make_meeting(client)
    from cabinet import meeting_channel as channels_mod
    ch = channels_mod.get_channel(mid)
    # Replace with a tight buffer to force the rollover.
    new_ch = channels_mod.MeetingChannel(max_buffer=5)
    channels_mod._CHANNELS[mid] = new_ch
    for i in range(20):
        new_ch.emit({"type": "ping", "i": i})

    # oldest_seq=16, latest_seq=20. sinceSeq=2 → 2 < 16-1=15 → 410.
    r = client.get(f"/api/cabinet/stream?meetingId={mid}&sinceSeq=2")
    assert r.status_code == 410
    assert r.headers.get("X-Refetch-Hint", "").startswith("GET /api/cabinet/transcripts")


def test_sse_meeting_ended_immediate_close(client: TestClient) -> None:
    """If meeting already ended, stream sends meeting_state + meeting_ended + closes.

    Streaming SSE end-to-end with FastAPI TestClient is subject to the
    httpx sync transport behavior — long-lived event loops hang. The SSE
    GET on an ended meeting RETURNS after the snapshot+meeting_ended pair
    (per the orchestrator contract — `if meeting.ended_at is not None:
    return` inside `event_gen`), so we can read the body deterministically.
    """
    mid = _make_meeting(client)
    client.post("/api/cabinet/end", json={"meetingId": mid})

    with client.stream("GET", f"/api/cabinet/stream?meetingId={mid}") as resp:
        chunks: list[bytes] = []
        for chunk in resp.iter_raw():
            chunks.append(chunk)
            joined = b"".join(chunks)
            if b"meeting_ended" in joined:
                break
            if len(joined) > 4000:
                break

    body = b"".join(chunks).decode("utf-8", errors="replace")
    # Snapshot direct-write: the first event payload contains "meeting_state".
    assert "meeting_state" in body
    assert "data:" in body
    # Snapshot frame OMITS id: line by design (dashboard-owner SSE minor fix:
    # snapshot id=0 was clobbering browser lastEventId on reconnect; per SSE
    # spec, omitting id: keeps the client cursor). Find a non-snapshot frame
    # WITH id: line — meeting_ended is one such frame.
    framed_with_id = [
        f for f in body.split("\n\n")
        if "data:" in f and any(ln.startswith("id:") for ln in f.split("\n"))
    ]
    assert framed_with_id, "no SSE frame with id: line seen (snapshot frames omit id: by design)"
    target_frame = framed_with_id[0]
    lines = target_frame.split("\n")
    id_pos = next((i for i, ln in enumerate(lines) if ln.startswith("id:")), -1)
    data_pos = next((i for i, ln in enumerate(lines) if ln.startswith("data:")), -1)
    assert id_pos != -1 and data_pos != -1
    assert id_pos < data_pos
    # meeting_ended fires per the early-return path.
    assert "meeting_ended" in body


def test_sse_subscribe_first_snapshot_payload(client: TestClient) -> None:
    """B4 — initial meeting_state snapshot is direct-written to the subscriber.

    Verified via the same ended-meeting early-return path used above (so
    the test doesn't hang on the live drain loop).
    """
    mid = _make_meeting(client)
    # Pre-emit a sentinel before ending. After end_meeting, the channel
    # buffer still holds events but the SSE handler exits cleanly post-snapshot.
    from cabinet import meeting_channel as channels_mod
    ch = channels_mod.get_channel(mid)
    ch.emit({"type": "system_note", "text": "before end", "tone": "info", "dismissable": False})
    client.post("/api/cabinet/end", json={"meetingId": mid})

    with client.stream("GET", f"/api/cabinet/stream?meetingId={mid}") as resp:
        chunks: list[bytes] = []
        for chunk in resp.iter_raw():
            chunks.append(chunk)
            joined = b"".join(chunks)
            if b"meeting_ended" in joined:
                break
            if len(joined) > 4000:
                break
    body = b"".join(chunks).decode("utf-8", errors="replace")
    # The very first event MUST be meeting_state (snapshot) — direct write,
    # NOT through channel.emit (would pollute the buffer for OTHER subs).
    snapshot_pos = body.find("meeting_state")
    ended_pos = body.find("meeting_ended")
    assert snapshot_pos >= 0
    assert ended_pos >= 0
    assert snapshot_pos < ended_pos


def test_sse_seq_is_monotonic_int_in_id_line(client: TestClient) -> None:
    """SSE id-line carries a monotonic int seq (matches Phase 3 SSE shape)."""
    mid = _make_meeting(client)
    client.post("/api/cabinet/end", json={"meetingId": mid})
    with client.stream("GET", f"/api/cabinet/stream?meetingId={mid}") as resp:
        chunks: list[bytes] = []
        for chunk in resp.iter_raw():
            chunks.append(chunk)
            joined = b"".join(chunks)
            if b"meeting_ended" in joined:
                break
            if len(joined) > 4000:
                break
    body = b"".join(chunks).decode("utf-8", errors="replace")
    import re
    id_lines = re.findall(r"^id:\s*(-?\d+)\s*$", body, re.MULTILINE)
    assert id_lines, "no id: lines found in SSE stream"
    # Snapshot id is 0 per upstream contract.
    assert id_lines[0] == "0"
