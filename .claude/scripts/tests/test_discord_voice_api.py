"""Discord voice endpoint tests — /api/discord/voice/{status,join,leave}.

Mirrors test_talk_api.py: TestClient against a bare FastAPI app mounting
discord_voice_api.router (the orchestration auth middleware is covered
separately). Lifecycle calls are monkeypatched — no sidecar subprocess,
no Discord gateway, no network.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import discord_voice_lifecycle


@pytest.fixture
def voice_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Bare app + discord voice router with a hermetic kill-switch env."""

    monkeypatch.delenv("HOMIE_KILLSWITCH_VOICE", raising=False)

    import discord_voice_api  # noqa: PLC0415

    app = FastAPI()
    app.include_router(discord_voice_api.router)
    return TestClient(app)


# ─── /api/discord/voice/status ────────────────────────────────────────────


def test_status_shape(voice_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        discord_voice_lifecycle,
        "status",
        lambda: {
            "ok": True,
            "status": "stopped",
            "pid": None,
            "channelId": None,
            "sidecarDirExists": True,
            "sidecarPythonExists": True,
            "bridge": None,
        },
    )

    r = voice_client.get("/api/discord/voice/status")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "stopped"
    assert body["sidecarPythonExists"] is True
    assert body["bridge"] is None


# ─── /api/discord/voice/join ──────────────────────────────────────────────


def test_join_happy_path(voice_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict = {}

    def fake_start(
        guild_id: int,
        channel_id: int,
        text_channel_id: int | None = None,
        operator_role: str = "viewer",
    ) -> dict:
        calls.update(
            guild_id=guild_id,
            channel_id=channel_id,
            text_channel_id=text_channel_id,
            operator_role=operator_role,
        )
        return {
            "status": "ready",
            "guildId": guild_id,
            "channelId": channel_id,
            "bridge": {"connected": True, "authSource": "configured"},
        }

    monkeypatch.setattr(discord_voice_lifecycle, "start_session", fake_start)

    r = voice_client.post(
        "/api/discord/voice/join",
        json={"guildId": 11, "channelId": 22, "textChannelId": 33},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "ready"
    assert body["channelId"] == 22
    assert body["bridge"]["authSource"] == "configured"
    # `operatorRole` is absent from the request body here, so the route must
    # forward the fail-closed default rather than minting admin.
    assert calls == {
        "guild_id": 11,
        "channel_id": 22,
        "text_channel_id": 33,
        "operator_role": "viewer",
    }


def test_join_text_channel_id_defaults_to_none(
    voice_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict = {}

    def fake_start(
        guild_id: int,
        channel_id: int,
        text_channel_id: int | None = None,
        operator_role: str = "viewer",
    ) -> dict:
        calls.update(text_channel_id=text_channel_id)
        return {"status": "ready"}

    monkeypatch.setattr(discord_voice_lifecycle, "start_session", fake_start)

    r = voice_client.post("/api/discord/voice/join", json={"guildId": 11, "channelId": 22})

    assert r.status_code == 200, r.text
    assert calls["text_channel_id"] is None


def test_join_lifecycle_error_is_503(
    voice_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_start(*_args, **_kwargs) -> dict:
        raise discord_voice_lifecycle.DiscordVoiceError("sidecar venv missing")

    monkeypatch.setattr(discord_voice_lifecycle, "start_session", fake_start)

    r = voice_client.post("/api/discord/voice/join", json={"guildId": 11, "channelId": 22})

    assert r.status_code == 503
    assert "sidecar venv missing" in r.json()["detail"]["error"]


def test_join_blocked_by_voice_killswitch(
    voice_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOMIE_KILLSWITCH_VOICE", "disabled")

    def fake_start(*_args, **_kwargs) -> dict:  # pragma: no cover - must not run
        raise AssertionError("start_session must not fire under the kill-switch")

    monkeypatch.setattr(discord_voice_lifecycle, "start_session", fake_start)

    r = voice_client.post("/api/discord/voice/join", json={"guildId": 11, "channelId": 22})

    assert r.status_code == 503
    detail = r.json()["detail"]
    assert detail["switch"] == "voice"
    assert "disabled by operator" in detail["error"]


# ─── /api/discord/voice/leave ─────────────────────────────────────────────


def test_leave_returns_stopped_state(
    voice_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        discord_voice_lifecycle,
        "stop_session",
        lambda: {"status": "stopped", "pid": None, "stoppedAt": 123.0},
    )

    r = voice_client.post("/api/discord/voice/leave")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "stopped"
    assert body["pid"] is None
