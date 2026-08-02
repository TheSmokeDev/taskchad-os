"""Test /talk chat wiring — platform gate, voice-channel resolution, API stubs.

Mirrors test_core_handlers_cabinet.py: SimpleNamespace incoming + adapter
doubles, HTTP layer monkeypatched at ``core_handlers._talk_api_get`` /
``core_handlers._talk_api_post`` — no orchestration API, no discord.py client.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Ensure both .claude/scripts and .claude/chat are importable.
_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS.parent / "chat"))

import core_handlers  # type: ignore[import-not-found]  # noqa: E402
from models import Platform  # type: ignore[import-not-found]  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers — incoming + adapter doubles
# ---------------------------------------------------------------------------


def _incoming(
    *,
    platform: Platform = Platform.DISCORD,
    guild: str = "123",
    user_id: str = "42",
    channel_id: str = "777",
) -> SimpleNamespace:
    """Minimal IncomingMessage-shaped double for the /talk handler."""
    return SimpleNamespace(
        platform=platform,
        user=SimpleNamespace(platform_id=user_id),
        channel=SimpleNamespace(platform_id=channel_id),
        raw_event={"guild": guild},
    )


class _VoiceChannel:
    def __init__(self, channel_id: int = 555, name: str = "war-room") -> None:
        self.id = channel_id
        self.name = name


class _Guild:
    def __init__(self, member: SimpleNamespace | None) -> None:
        self._member = member

    def get_member(self, user_id: int) -> SimpleNamespace | None:
        return self._member if user_id == 42 else None


class _Client:
    def __init__(self, guild: _Guild | None) -> None:
        self._guild = guild

    def get_guild(self, guild_id: int) -> _Guild | None:
        return self._guild if guild_id == 123 else None


def _discord_adapter(member: SimpleNamespace | None) -> SimpleNamespace:
    """Adapter double carrying a fake discord.py client like the real one."""
    return SimpleNamespace(_client=_Client(_Guild(member)))


def _member_in_voice() -> SimpleNamespace:
    return SimpleNamespace(voice=SimpleNamespace(channel=_VoiceChannel()))


# ---------------------------------------------------------------------------
# Registration + usage
# ---------------------------------------------------------------------------


def test_handle_talk_in_core_handlers() -> None:
    assert "talk" in core_handlers.CORE_HANDLERS
    assert core_handlers.CORE_HANDLERS["talk"] is core_handlers.handle_talk


def test_talk_command_registered_as_router_admin() -> None:
    import commands  # noqa: PLC0415

    rows = [r for r in commands.COMMANDS if r[0] == "talk"]
    name, desc, cmd_type, min_role = rows[0]
    assert name == "talk"
    assert desc == "Live voice in your Discord voice channel - join | leave | status"
    assert (cmd_type, min_role) == ("router", "admin")
    assert commands.get_command_min_role("talk") == "admin"


@pytest.mark.asyncio
async def test_talk_usage_on_help_and_empty_subcommand() -> None:
    reply = await core_handlers.handle_talk(None, _incoming(), "help")
    assert "Usage: /talk" in reply

    reply = await core_handlers.handle_talk(
        _discord_adapter(_member_in_voice()), _incoming(), "bogus"
    )
    assert "Usage: /talk" in reply


# ---------------------------------------------------------------------------
# Platform gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_talk_non_discord_adapter_gets_dashboard_pointer() -> None:
    reply = await core_handlers.handle_talk(
        None, _incoming(platform=Platform.TELEGRAM), "status"
    )
    assert "Discord-only" in reply
    assert "/talk" in reply


@pytest.mark.asyncio
async def test_talk_discord_without_client_gets_dashboard_pointer() -> None:
    reply = await core_handlers.handle_talk(SimpleNamespace(), _incoming(), "join")
    assert "Discord-only" in reply


# ---------------------------------------------------------------------------
# join
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_talk_join_without_voice_channel() -> None:
    adapter = _discord_adapter(SimpleNamespace(voice=None))

    reply = await core_handlers.handle_talk(adapter, _incoming(), "join")

    assert reply == "Join a voice channel first, then run /talk join."


@pytest.mark.asyncio
async def test_talk_join_in_dm_has_no_guild() -> None:
    adapter = _discord_adapter(_member_in_voice())

    reply = await core_handlers.handle_talk(adapter, _incoming(guild=""), "join")

    assert reply == "Join a voice channel first, then run /talk join."


@pytest.mark.asyncio
async def test_talk_join_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _discord_adapter(_member_in_voice())
    posts: list[tuple[str, dict]] = []

    async def fake_post(path: str, payload: dict):
        posts.append((path, payload))
        return {
            "ok": True,
            "status": "ready",
            "channelId": 555,
            "bridge": {"connected": True, "authSource": "configured"},
        }, None

    monkeypatch.setattr(core_handlers, "_talk_api_post", fake_post)

    reply = await core_handlers.handle_talk(adapter, _incoming(), "join")

    assert posts == [
        ("/api/discord/voice/join", {"guildId": 123, "channelId": 555, "textChannelId": 777})
    ]
    assert "#war-room" in reply
    assert "configured" in reply


@pytest.mark.asyncio
async def test_talk_join_already_joined(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _discord_adapter(_member_in_voice())

    async def fake_post(path: str, payload: dict):
        return {"ok": True, "status": "ready", "alreadyJoined": True, "bridge": {}}, None

    monkeypatch.setattr(core_handlers, "_talk_api_post", fake_post)

    reply = await core_handlers.handle_talk(adapter, _incoming(), "join")

    assert "Already live" in reply
    assert "#war-room" in reply


@pytest.mark.asyncio
async def test_talk_join_surfaces_error_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _discord_adapter(_member_in_voice())

    async def fake_post(path: str, payload: dict):
        return None, "voice features are disabled by operator"

    monkeypatch.setattr(core_handlers, "_talk_api_post", fake_post)

    reply = await core_handlers.handle_talk(adapter, _incoming(), "join")

    assert reply == "voice features are disabled by operator"


@pytest.mark.asyncio
async def test_talk_join_collect_only_refuses_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _discord_adapter(_member_in_voice())

    async def fake_post(path: str, payload: dict):  # pragma: no cover - must not run
        raise AssertionError("chained /talk join must not touch the API")

    monkeypatch.setattr(core_handlers, "_talk_api_post", fake_post)

    reply = await core_handlers.handle_talk(adapter, _incoming(), "join", collect_only=True)

    assert "Cannot chain /talk" in reply


# ---------------------------------------------------------------------------
# leave
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_talk_leave_confirms(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _discord_adapter(_member_in_voice())
    posts: list[tuple[str, dict]] = []

    async def fake_post(path: str, payload: dict):
        posts.append((path, payload))
        return {"ok": True, "status": "stopped"}, None

    monkeypatch.setattr(core_handlers, "_talk_api_post", fake_post)

    reply = await core_handlers.handle_talk(adapter, _incoming(), "leave")

    assert posts == [("/api/discord/voice/leave", {})]
    assert "Left the voice channel" in reply


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_talk_status_renders_bridge_line(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _discord_adapter(_member_in_voice())

    async def fake_get(path: str):
        assert path == "/api/discord/voice/status"
        return {
            "ok": True,
            "status": "ready",
            "channelId": 555,
            "logPath": "~/.claude/data/logs/discord-voice/discord-voice.log",
            "bridge": {
                "connected": True,
                "channelId": 555,
                "authSource": "codex-oauth",
                "uptimeS": 12.5,
            },
        }, None

    monkeypatch.setattr(core_handlers, "_talk_api_get", fake_get)

    reply = await core_handlers.handle_talk(adapter, _incoming(), "")

    assert "ready" in reply
    assert "555" in reply
    assert "12.5s" in reply
    assert "codex-oauth" in reply
    assert "discord-voice.log" in reply


@pytest.mark.asyncio
async def test_talk_status_api_down_surfaces_friendly_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _discord_adapter(_member_in_voice())

    async def fake_get(path: str):
        return None, (
            "Orchestration API is not running. "
            "Start it with `uv run python -m orchestration.run_api`."
        )

    monkeypatch.setattr(core_handlers, "_talk_api_get", fake_get)

    reply = await core_handlers.handle_talk(adapter, _incoming(), "status")

    assert "Orchestration API is not running" in reply
