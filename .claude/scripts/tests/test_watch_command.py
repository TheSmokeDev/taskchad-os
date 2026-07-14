from __future__ import annotations

from types import SimpleNamespace

import pytest

import commands
import core_handlers
from models import Channel, IncomingMessage, Platform, User


def _incoming(platform: Platform = Platform.TELEGRAM) -> IncomingMessage:
    return IncomingMessage(
        text="",
        user=User(platform, "operator"),
        channel=Channel(platform, "channel-1"),
        platform=platform,
        user_role="admin",
    )


def test_watch_is_registered_on_all_router_surfaces() -> None:
    assert any(row[0] == "watch" and row[2] == "router" for row in commands.COMMANDS)
    assert "watch" in commands.TELEGRAM_NATIVE_COMMANDS
    assert core_handlers.CORE_HANDLERS["watch"] is core_handlers.handle_watch


def test_parse_watch_request_defaults_to_smart_and_save() -> None:
    source, question, detail, save = core_handlers._parse_watch_request(
        "https://youtu.be/abc What should we apply?"
    )
    assert source == "https://youtu.be/abc"
    assert question == "What should we apply?"
    assert detail == "smart"
    assert save is True


def test_parse_watch_request_accepts_detail_and_no_save() -> None:
    source, question, detail, save = core_handlers._parse_watch_request(
        "https://youtu.be/abc --detail deep focus on positioning --no-save"
    )
    assert source == "https://youtu.be/abc"
    assert question == "focus on positioning"
    assert detail == "deep"
    assert save is False


@pytest.mark.asyncio
async def test_remote_channel_rejects_local_path_before_job_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = SimpleNamespace(dependency_report=lambda: [])
    monkeypatch.setattr(core_handlers, "_watch_service", lambda: fake)
    result = await core_handlers.handle_watch(
        None,
        _incoming(),
        r"C:\videos\strategy.mp4",
    )
    assert "public http(s)" in result


@pytest.mark.asyncio
async def test_watch_status_is_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = SimpleNamespace(
        status=lambda _job="": {
            "job_id": "abc123",
            "status": "ready",
            "stage_detail": "Ready",
            "request": {"source": "https://youtu.be/abc"},
            "result": {"title": "Strategy"},
        }
    )
    monkeypatch.setattr(core_handlers, "_watch_service", lambda: fake)
    result = await core_handlers.handle_watch(None, _incoming(), "status abc123")
    assert "abc123" in result
    assert "ready" in result
