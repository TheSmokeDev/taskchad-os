"""Tests for adapters.discord — normalization, allowlists, message splitting."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Add chat dir to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "chat"))

from adapters.discord import DiscordAdapter
from models import Platform


def _make_adapter(
    allowed_guilds: list[str] | None = None,
    allowed_users: list[str] | None = None,
) -> DiscordAdapter:
    """Create a DiscordAdapter without connecting to Discord."""
    adapter = DiscordAdapter(
        bot_token="fake-token",
        allowed_guilds=allowed_guilds or [],
        allowed_users=allowed_users or [],
    )
    adapter._bot_user_id = 999999
    return adapter


def _mock_message(
    *,
    author_id: int = 12345,
    author_name: str = "TestUser",
    channel_id: int = 67890,
    guild_id: int | None = 11111,
    content: str = "Hello bot",
    message_id: int = 54321,
    is_dm: bool = False,
) -> MagicMock:
    """Create a mock Discord message object."""
    import discord

    msg = MagicMock()
    msg.author.id = author_id
    msg.author.display_name = author_name
    msg.author.__str__ = lambda self: author_name
    msg.channel.id = channel_id
    msg.id = message_id
    msg.content = content

    if is_dm:
        msg.channel.__class__ = discord.DMChannel
        # isinstance check needs special handling
        msg.channel = MagicMock(spec=discord.DMChannel)
        msg.channel.id = channel_id
        msg.guild = None
    else:
        msg.channel = MagicMock()
        msg.channel.id = channel_id
        msg.guild = MagicMock()
        msg.guild.id = guild_id

    msg.thread = None
    return msg


# ── Platform property ──────────────────────────────────────


def test_discord_platform():
    adapter = _make_adapter()
    assert adapter.platform == Platform.DISCORD


# ── Message normalization ──────────────────────────────────


def test_normalize_message_dm():
    adapter = _make_adapter()
    msg = _mock_message(is_dm=True, content="Hello from DM")
    result = adapter._normalize_message(msg, is_dm=True)

    assert result.text == "Hello from DM"
    assert result.platform == Platform.DISCORD
    assert result.user.platform_id == str(msg.author.id)
    assert result.user.display_name == "TestUser"
    assert result.channel.is_dm is True
    assert result.platform_message_id == str(msg.id)


def test_normalize_message_guild():
    adapter = _make_adapter()
    msg = _mock_message(content="<@999999> help me")
    result = adapter._normalize_message(msg, is_dm=False)

    assert result.text == "help me"
    assert result.channel.is_dm is False


def test_normalize_message_strips_mention():
    adapter = _make_adapter()
    msg = _mock_message(content="<@999999> what's up")
    result = adapter._normalize_message(msg, is_dm=False)
    assert result.text == "what's up"


def test_normalize_message_strips_mention_with_bang():
    adapter = _make_adapter()
    msg = _mock_message(content="<@!999999> help me")
    result = adapter._normalize_message(msg, is_dm=False)
    assert result.text == "help me"


def test_normalize_message_no_mention():
    adapter = _make_adapter()
    msg = _mock_message(content="just a message")
    result = adapter._normalize_message(msg, is_dm=False)
    assert result.text == "just a message"


def test_normalize_message_thread_id_from_channel():
    adapter = _make_adapter()
    msg = _mock_message(channel_id=67890)
    msg.thread = None
    result = adapter._normalize_message(msg, is_dm=False)
    assert result.thread.thread_id == "67890"


# ── Allowlist filtering ────────────────────────────────────


def test_is_allowed_user_in_list():
    adapter = _make_adapter(allowed_users=["12345"])
    msg = _mock_message(author_id=12345)
    assert adapter._is_allowed(msg) is True


def test_is_allowed_user_not_in_list():
    adapter = _make_adapter(allowed_users=["99999"])
    msg = _mock_message(author_id=12345)
    assert adapter._is_allowed(msg) is False


def test_is_allowed_user_empty_list():
    adapter = _make_adapter(allowed_users=[])
    msg = _mock_message(author_id=12345)
    assert adapter._is_allowed(msg) is True


def test_is_allowed_guild_in_list():
    adapter = _make_adapter(allowed_guilds=["11111"])
    msg = _mock_message(guild_id=11111)
    assert adapter._is_allowed(msg) is True


def test_is_allowed_guild_not_in_list():
    adapter = _make_adapter(allowed_guilds=["99999"])
    msg = _mock_message(guild_id=11111)
    assert adapter._is_allowed(msg) is False


def test_is_allowed_guild_empty_list():
    adapter = _make_adapter(allowed_guilds=[])
    msg = _mock_message(guild_id=11111)
    assert adapter._is_allowed(msg) is True


def test_is_allowed_dm_bypasses_guild_check():
    adapter = _make_adapter(allowed_guilds=["99999"])
    msg = _mock_message(is_dm=True, author_id=12345)
    assert adapter._is_allowed(msg) is True


# ── Message splitting ──────────────────────────────────────


def test_split_message_short():
    adapter = _make_adapter()
    assert adapter._split_message("hello", max_length=1900) == ["hello"]


def test_split_message_exact_limit():
    adapter = _make_adapter()
    text = "x" * 1900
    assert adapter._split_message(text, max_length=1900) == [text]


def test_split_message_over_limit():
    adapter = _make_adapter()
    text = "x" * 3000
    chunks = adapter._split_message(text, max_length=1900)
    assert all(len(c) <= 1900 for c in chunks)
    assert "".join(chunks) == text


def test_split_message_at_newline():
    adapter = _make_adapter()
    # Build text where a newline falls in the second half
    text = "a" * 1200 + "\n" + "b" * 1200
    chunks = adapter._split_message(text, max_length=1900)
    assert len(chunks) == 2
    assert chunks[0].endswith("a\n") or chunks[0].endswith("a")


def test_split_message_empty():
    adapter = _make_adapter()
    assert adapter._split_message("", max_length=1900) == [""]
