"""LiveKit Cabinet voice transport spike tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import config  # noqa: E402
from cabinet.voice import livekit_agent, livekit_session  # noqa: E402


@pytest.fixture
def dash_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db_path = tmp_path / "dashboard.db"
    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", str(db_path))
    from cabinet import meeting_channel as channels_mod  # noqa: PLC0415

    channels_mod._reset_channels()
    from dashboard_db import get_connection as _get_conn  # noqa: PLC0415

    _get_conn().close()
    import dashboard_api  # noqa: PLC0415

    app = FastAPI()
    app.include_router(dashboard_api.router)
    return TestClient(app)


def _create_meeting(client: TestClient, chat_id: str = "cabinet-browser") -> int:
    r = client.post("/api/cabinet/new", json={"chatId": chat_id})
    assert r.status_code == 200, r.text
    return r.json()["meetingId"]


def test_create_browser_session_uses_room_scoped_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CABINET_LIVEKIT_URL", "ws://127.0.0.1:7880")
    monkeypatch.setenv("CABINET_LIVEKIT_TOKEN_TTL_S", "600")
    monkeypatch.setenv("LIVEKIT_API_KEY", "devkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "devsecret")
    captured: dict = {}

    def fake_token_factory(**kwargs):
        captured.update(kwargs)
        return "room.jwt"

    session = livekit_session.create_browser_session(
        meeting_id=16,
        chat_id="cabinet-browser",
        token_factory=fake_token_factory,
    )

    assert session.room_name == "cabinet-16"
    assert session.server_url == "ws://127.0.0.1:7880"
    assert session.participant_token == "room.jwt"
    assert captured == {
        "room_name": "cabinet-16",
        "identity": "cabinet-browser-16-cabinet-browser",
        "name": "Cabinet Browser 16",
        "ttl_seconds": 600,
    }
    wire = json.dumps(session.to_wire())
    assert "devkey" not in wire
    assert "devsecret" not in wire


def test_livekit_session_endpoint_returns_token_without_secrets(
    dash_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import dashboard_api  # noqa: PLC0415

    meeting_id = _create_meeting(dash_client, "cabinet-browser")
    monkeypatch.setenv("LIVEKIT_API_KEY", "devkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "devsecret")

    def fake_create_browser_session(*, meeting_id: int, chat_id: str):
        return livekit_session.LiveKitSessionDescriptor(
            meeting_id=meeting_id,
            chat_id=chat_id,
            room_name=f"cabinet-{meeting_id}",
            server_url="ws://127.0.0.1:7880",
            participant_identity=f"cabinet-browser-{meeting_id}",
            participant_name=f"Cabinet Browser {meeting_id}",
            participant_token="browser.jwt",
            agent_identity=f"cabinet-livekit-agent-{meeting_id}",
            agent_name="cabinet-livekit-agent",
            expires_in_s=1800,
        )

    monkeypatch.setattr(
        dashboard_api._cabinet_livekit_session,
        "create_browser_session",
        fake_create_browser_session,
    )

    r = dash_client.get(
        "/api/cabinet/voice/livekit/session",
        params={"meetingId": meeting_id, "chatId": "cabinet-browser"},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["transport"] == "livekit"
    assert body["mode"] == "local_oss_spike"
    assert body["roomName"] == f"cabinet-{meeting_id}"
    assert body["participantToken"] == "browser.jwt"
    assert "devkey" not in r.text
    assert "devsecret" not in r.text


def test_livekit_session_endpoint_refuses_missing_wrong_chat_and_ended(
    dash_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import dashboard_api  # noqa: PLC0415

    monkeypatch.setattr(
        dashboard_api._cabinet_livekit_session,
        "create_browser_session",
        lambda **_: pytest.fail("should validate meeting before token mint"),
    )

    missing = dash_client.get(
        "/api/cabinet/voice/livekit/session",
        params={"meetingId": 99999, "chatId": "cabinet-browser"},
    )
    assert missing.status_code == 404

    meeting_id = _create_meeting(dash_client, "chat-a")
    mismatch = dash_client.get(
        "/api/cabinet/voice/livekit/session",
        params={"meetingId": meeting_id, "chatId": "chat-b"},
    )
    assert mismatch.status_code == 403

    end = dash_client.post("/api/cabinet/end", json={"meetingId": meeting_id, "chatId": "chat-a"})
    assert end.status_code == 200
    ended = dash_client.get(
        "/api/cabinet/voice/livekit/session",
        params={"meetingId": meeting_id, "chatId": "chat-a"},
    )
    assert ended.status_code == 410


@pytest.mark.asyncio
async def test_livekit_transcript_handoff_posts_to_cabinet_auto_route() -> None:
    captured: dict = {}

    async def fake_send_message(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "queued": True}

    fake_api = SimpleNamespace(send_message=fake_send_message)
    result = await livekit_agent.handoff_transcript_to_cabinet(
        meeting_id=16,
        chat_id="cabinet-browser",
        transcript="  what should we do next  ",
        client_msg_id="lk_test",
        cabinet_api_module=fake_api,
    )

    assert result == {"ok": True, "queued": True}
    assert captured["meeting_id"] == 16
    assert captured["text"] == "what should we do next"
    assert captured["client_msg_id"] == "lk_test"
    assert captured["chat_id"] == "cabinet-browser"
    assert captured["is_voice"] is True
    assert captured["audience"] == "auto"
    assert captured["target_agent_id"] is None


@pytest.mark.asyncio
async def test_livekit_transcript_handoff_ignores_empty_transcript() -> None:
    result = await livekit_agent.handoff_transcript_to_cabinet(
        meeting_id=16,
        chat_id="cabinet-browser",
        transcript="   ",
        cabinet_api_module=SimpleNamespace(send_message=None),
    )

    assert result == {"ok": True, "ignored": "empty_transcript"}
