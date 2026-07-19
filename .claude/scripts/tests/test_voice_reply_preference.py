from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import adapters.discord as discord_adapter_module
import adapters.telegram as telegram_adapter_module
import core_handlers
import voice_preferences
from adapters.discord import DiscordAdapter
from adapters.telegram import TelegramAdapter
from models import Channel, OutgoingMessage, Platform


def test_voice_preference_persists_and_on_aliases_always(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "voice.json"
    monkeypatch.setattr(voice_preferences, "VOICE_REPLY_STATE_PATH", state_path)
    monkeypatch.setattr(
        voice_preferences,
        "_VOICE_REPLY_LOCK_PATH",
        tmp_path / "voice.lock",
    )

    assert voice_preferences.get_voice_reply_mode() == "auto"
    assert voice_preferences.set_voice_reply_mode("on") == "always"
    assert voice_preferences.get_voice_reply_mode() == "always"


@pytest.mark.asyncio
async def test_voice_command_sets_shared_mode(monkeypatch) -> None:
    setter = MagicMock(return_value="always")
    monkeypatch.setattr(voice_preferences, "set_voice_reply_mode", setter)

    reply = await core_handlers.handle_voice(None, SimpleNamespace(), "always")

    setter.assert_called_once_with("always")
    assert "every Telegram and Discord reply includes voice + text" in reply
    assert "survives restarts" in reply


@pytest.mark.asyncio
async def test_telegram_always_mode_sends_voice_and_text(monkeypatch) -> None:
    monkeypatch.setattr(telegram_adapter_module, "get_voice_reply_mode", lambda: "always")
    adapter = TelegramAdapter.__new__(TelegramAdapter)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=7)))
    adapter._app = SimpleNamespace(bot=bot)
    adapter._voice_reply_threads = set()
    adapter._send_voice_response = AsyncMock()
    adapter._sent_messages = {}

    result = await adapter.send(
        OutgoingMessage(
            text="Voice and text, every time.",
            channel=Channel(Platform.TELEGRAM, "123", is_dm=True),
        )
    )

    adapter._send_voice_response.assert_awaited_once_with(
        123, "Voice and text, every time.", fallback_to_text=False
    )
    bot.send_message.assert_awaited_once()
    assert result == "7"


@pytest.mark.asyncio
async def test_discord_always_mode_sends_voice_and_text(monkeypatch) -> None:
    monkeypatch.setattr(discord_adapter_module, "get_voice_reply_mode", lambda: "always")
    adapter = DiscordAdapter.__new__(DiscordAdapter)
    channel = MagicMock()
    channel.send = AsyncMock(return_value=SimpleNamespace(id=8))
    adapter._client = SimpleNamespace(get_channel=lambda _channel_id: channel)
    adapter._voice_reply_channels = set()
    adapter._send_voice_response = AsyncMock()

    result = await adapter.send(
        OutgoingMessage(
            text="Voice and text, every time.",
            channel=Channel(Platform.DISCORD, "456", is_dm=True),
        )
    )

    adapter._send_voice_response.assert_awaited_once_with(
        channel, "Voice and text, every time.", fallback_to_text=False
    )
    channel.send.assert_awaited_once()
    assert result == "8"


@pytest.mark.asyncio
async def test_always_mode_never_voices_progress_updates(monkeypatch) -> None:
    monkeypatch.setattr(telegram_adapter_module, "get_voice_reply_mode", lambda: "always")
    adapter = TelegramAdapter.__new__(TelegramAdapter)
    bot = SimpleNamespace(
        edit_message_text=AsyncMock(return_value=SimpleNamespace(message_id=7))
    )
    adapter._app = SimpleNamespace(bot=bot)
    adapter._send_voice_response = AsyncMock()

    result = await adapter.update(
        OutgoingMessage(
            text="⏳ Working — 8s",
            channel=Channel(Platform.TELEGRAM, "123", is_dm=True),
            is_update=True,
            update_message_id="7",
        )
    )

    assert result == "7"
    adapter._send_voice_response.assert_not_awaited()
