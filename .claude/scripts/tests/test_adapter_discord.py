"""Tests for adapters.discord — normalization, allowlists, message splitting."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add chat dir to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "chat"))

from adapters.discord import DiscordAdapter, get_discord_native_command_menu
from models import Attachment, Channel, MessageEmbed, OutgoingMessage, Platform


def _make_adapter(
    allowed_guilds: list[str] | None = None,
    allowed_users: list[str] | None = None,
    watch_all_guild_channels: bool = False,
) -> DiscordAdapter:
    """Create a DiscordAdapter without connecting to Discord."""
    adapter = DiscordAdapter(
        bot_token="fake-token",
        allowed_guilds=allowed_guilds or [],
        allowed_users=allowed_users or [],
        watch_all_guild_channels=watch_all_guild_channels,
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
    msg.attachments = []
    return msg


def _mock_component_interaction(
    *,
    user_id: int = 12345,
    guild_id: int | None = 11111,
    channel_id: int = 67890,
    interaction_id: int = 24680,
    custom_id: str = "approval:preview:7",
    message_id: int = 13579,
    message_author_id: int | None = 999999,
    application_id: int | None = 999999,
) -> SimpleNamespace:
    """Create a component interaction with observable ACK/edit calls."""
    import discord

    message = SimpleNamespace(
        id=message_id,
        author=(
            SimpleNamespace(id=message_author_id)
            if message_author_id is not None
            else None
        ),
        components=[],
        edit=AsyncMock(),
    )
    return SimpleNamespace(
        id=interaction_id,
        type=discord.InteractionType.component,
        channel_id=channel_id,
        guild_id=guild_id,
        application_id=application_id,
        user=SimpleNamespace(id=user_id, display_name="Tester"),
        data={"custom_id": custom_id},
        message=message,
        response=SimpleNamespace(
            defer=AsyncMock(),
            send_message=AsyncMock(),
        ),
    )


# ── Platform property ──────────────────────────────────────


def test_discord_platform():
    adapter = _make_adapter()
    assert adapter.platform == Platform.DISCORD


@pytest.mark.asyncio
async def test_discord_send_fails_when_channel_is_unavailable() -> None:
    adapter = _make_adapter()
    adapter._client.get_channel = MagicMock(return_value=None)

    with pytest.raises(RuntimeError, match="Discord channel 67890 is unavailable"):
        await adapter.send(
            OutgoingMessage(
                text="This must not be reported as delivered.",
                channel=Channel(Platform.DISCORD, "67890"),
            )
        )


@pytest.mark.asyncio
async def test_discord_long_update_defers_before_any_edit() -> None:
    adapter = _make_adapter()
    channel = MagicMock()
    channel.fetch_message = AsyncMock()
    adapter._client.get_channel = MagicMock(return_value=channel)

    result = await adapter.update(
        OutgoingMessage(
            text="x" * 4000,
            channel=Channel(Platform.DISCORD, "67890"),
            is_update=True,
            update_message_id="55",
        )
    )

    assert result is None
    channel.fetch_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_discord_marker_update_defers_before_any_file_or_text_write() -> None:
    adapter = _make_adapter()
    channel = MagicMock()
    channel.fetch_message = AsyncMock()
    channel.send = AsyncMock()
    adapter._client.get_channel = MagicMock(return_value=channel)

    result = await adapter.update(
        OutgoingMessage(
            text="Here it is [SEND_FILE:report.pdf]",
            channel=Channel(Platform.DISCORD, "67890"),
            is_update=True,
            update_message_id="55",
        )
    )

    assert result is None
    channel.fetch_message.assert_not_awaited()
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_discord_voice_input_gets_voice_only_reply() -> None:
    adapter = _make_adapter()
    channel = MagicMock()
    channel.send = AsyncMock()
    adapter._client.get_channel = MagicMock(return_value=channel)
    adapter._send_voice_response = AsyncMock()
    adapter._voice_reply_channels.add("67890")

    result = await adapter.send(
        OutgoingMessage(
            text="Yep, I can hear you.",
            channel=Channel(Platform.DISCORD, "67890"),
        )
    )

    assert result is None
    adapter._send_voice_response.assert_awaited_once_with(channel, "Yep, I can hear you.")
    channel.send.assert_not_awaited()
    assert "67890" not in adapter._voice_reply_channels


@pytest.mark.asyncio
async def test_discord_medium_voice_reply_keeps_text_copy() -> None:
    adapter = _make_adapter()
    channel = MagicMock()
    sent = MagicMock()
    sent.id = 123
    channel.send = AsyncMock(return_value=sent)
    adapter._client.get_channel = MagicMock(return_value=channel)
    adapter._send_voice_response = AsyncMock()
    adapter._voice_reply_channels.add("67890")
    reply = "Got it. " + ("Here is the detail. " * 25)

    result = await adapter.send(
        OutgoingMessage(
            text=reply,
            channel=Channel(Platform.DISCORD, "67890"),
        )
    )

    assert result == "123"
    adapter._send_voice_response.assert_awaited_once_with(channel, reply.strip())
    channel.send.assert_awaited()


@pytest.mark.asyncio
async def test_discord_send_preserves_multiple_embeds() -> None:
    adapter = _make_adapter()
    channel = MagicMock()
    sent = MagicMock(id=321)
    channel.send = AsyncMock(return_value=sent)
    adapter._client.get_channel = MagicMock(return_value=channel)

    result = await adapter.send(
        OutgoingMessage(
            text="Daily Win Desk",
            channel=Channel(Platform.DISCORD, "67890"),
            embeds=[MessageEmbed(title="One"), MessageEmbed(title="Two")],
        )
    )

    assert result == "321"
    kwargs = channel.send.await_args.kwargs
    assert "embed" not in kwargs
    assert len(kwargs["embeds"]) == 2


@pytest.mark.asyncio
async def test_discord_progress_edit_preserves_multiple_embeds() -> None:
    adapter = _make_adapter()
    message = SimpleNamespace(edit=AsyncMock())
    channel = MagicMock(fetch_message=AsyncMock(return_value=message))
    adapter._client.get_channel = MagicMock(return_value=channel)

    result = await adapter.update(
        OutgoingMessage(
            text="Final answer",
            channel=Channel(Platform.DISCORD, "67890"),
            is_update=True,
            update_message_id="55",
            embeds=[MessageEmbed(title="One"), MessageEmbed(title="Two")],
        )
    )

    assert result == "55"
    assert len(message.edit.await_args.kwargs["embeds"]) == 2


@pytest.mark.asyncio
async def test_discord_voice_summary_failure_is_short_and_does_not_touch_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import adapters.discord as discord_adapter_module

    adapter = _make_adapter()
    channel = MagicMock(send=AsyncMock())
    adapter._client.get_channel = MagicMock(return_value=channel)
    monkeypatch.setattr(
        discord_adapter_module.voice_mod,
        "synthesize",
        AsyncMock(side_effect=RuntimeError("tts offline")),
    )

    sent = await adapter.send_voice_summary(
        Channel(Platform.DISCORD, "67890"),
        "Private deal summary",
    )

    assert sent is False
    assert channel.send.await_args.kwargs["content"].startswith("Voice summary is unavailable")


def test_discord_registers_native_vault_group_without_flat_duplicate():
    adapter = _make_adapter()
    commands = adapter._tree.get_commands()
    vault_commands = [cmd for cmd in commands if cmd.name == "vault"]

    assert len(vault_commands) == 1
    assert type(vault_commands[0]).__name__ == "Group"
    assert [cmd.name for cmd in vault_commands[0].commands] == [
        "status",
        "db",
        "search",
        "context",
        "contacts",
        "ingest",
        "ops",
    ]
    assert "vault" not in [name for name, _desc in get_discord_native_command_menu()]


def test_discord_native_vault_text_builds_shared_router_command():
    text = DiscordAdapter._build_native_vault_text(
        "search",
        query="YourProduct demo URLs",
        vault="thehomie",
        mode="hybrid",
        limit=4,
    )

    assert text == (
        "/vault search YourProduct demo URLs --vault thehomie "
        "--mode hybrid --limit 4"
    )


@pytest.mark.asyncio
async def test_discord_native_vault_ingest_attachment_queues_internal_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _make_adapter()
    downloaded = Attachment(
        filename="notes.txt",
        mimetype="text/plain",
        url="C:\\tmp\\notes.txt",
        size_bytes=12,
    )
    download = AsyncMock(return_value=downloaded)
    monkeypatch.setattr(adapter, "_download_interaction_attachment", download)

    interaction = SimpleNamespace(
        id=111,
        channel_id=222,
        guild_id=333,
        user=SimpleNamespace(id=444, display_name="Tester"),
        response=SimpleNamespace(defer=AsyncMock()),
    )
    raw_attachment = SimpleNamespace(filename="notes.txt")

    await adapter._queue_native_slash_command(
        interaction,
        "vault",
        "ingest --vault thehomie",
        interaction_attachment=raw_attachment,
    )

    queued = await adapter._queue.get()
    assert queued.text == "/vault ingest --vault thehomie"
    assert queued.attachments == [downloaded]
    assert queued.raw_event["interaction_type"] == "slash_command"
    download.assert_awaited_once_with(interaction, raw_attachment)
    interaction.response.defer.assert_awaited_once_with(thinking=True)


@pytest.mark.asyncio
async def test_discord_component_allowed_guild_queues_provenance_metadata() -> None:
    adapter = _make_adapter(
        allowed_guilds=["11111"],
        allowed_users=["12345"],
    )
    interaction = _mock_component_interaction()

    await adapter._client.on_interaction(interaction)

    queued = adapter._queue.get_nowait()
    assert queued.text == "__button:approval:preview:7"
    assert queued.raw_event == {
        "interaction_id": "24680",
        "interaction_type": "button",
        "custom_id": "approval:preview:7",
        "guild": "11111",
        "source_message_id": "13579",
        "message_author_id": "999999",
        "application_id": "999999",
        "source_message_is_own": True,
    }
    interaction.response.defer.assert_awaited_once_with()
    interaction.response.send_message.assert_not_awaited()
    interaction.message.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_discord_component_is_queued_before_button_disable_failure() -> None:
    adapter = _make_adapter(
        allowed_guilds=["11111"],
        allowed_users=["12345"],
    )
    interaction = _mock_component_interaction()

    async def fail_after_queue(*_args, **_kwargs) -> None:
        assert not adapter._queue.empty()
        raise asyncio.TimeoutError

    interaction.message.edit.side_effect = fail_after_queue

    await adapter._client.on_interaction(interaction)

    queued = adapter._queue.get_nowait()
    assert queued.text == "__button:approval:preview:7"
    interaction.response.defer.assert_awaited_once_with()
    interaction.message.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_discord_component_wrong_guild_rejected_before_ack_or_disable() -> None:
    adapter = _make_adapter(
        allowed_guilds=["99999"],
        allowed_users=["12345"],
    )
    interaction = _mock_component_interaction(guild_id=11111)

    await adapter._client.on_interaction(interaction)

    interaction.response.send_message.assert_awaited_once_with(
        "This server is not allowed.", ephemeral=True
    )
    interaction.response.defer.assert_not_awaited()
    interaction.message.edit.assert_not_awaited()
    assert adapter._queue.empty()


@pytest.mark.asyncio
async def test_discord_component_missing_source_metadata_is_not_own_message() -> None:
    adapter = _make_adapter(allowed_users=["12345"])
    interaction = _mock_component_interaction(
        message_author_id=None,
        application_id=None,
    )

    await adapter._client.on_interaction(interaction)

    queued = adapter._queue.get_nowait()
    assert queued.raw_event["message_author_id"] == ""
    assert queued.raw_event["application_id"] == ""
    assert queued.raw_event["source_message_is_own"] is False


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


# ── Whole-server auto-listen (DISCORD_WATCH_ALL_GUILD_CHANNELS) ─────


def test_watches_guild_flag_off():
    adapter = _make_adapter(allowed_guilds=["11111"], watch_all_guild_channels=False)
    msg = _mock_message(guild_id=11111)
    assert adapter._watches_guild(msg) is False


def test_watches_guild_flag_on_guild_allowed():
    adapter = _make_adapter(allowed_guilds=["11111"], watch_all_guild_channels=True)
    msg = _mock_message(guild_id=11111)
    assert adapter._watches_guild(msg) is True


def test_watches_guild_flag_on_guild_not_allowed():
    adapter = _make_adapter(allowed_guilds=["99999"], watch_all_guild_channels=True)
    msg = _mock_message(guild_id=11111)
    assert adapter._watches_guild(msg) is False


def test_watches_guild_flag_on_empty_allowlist_any_guild():
    adapter = _make_adapter(allowed_guilds=[], watch_all_guild_channels=True)
    msg = _mock_message(guild_id=11111)
    assert adapter._watches_guild(msg) is True


def test_watches_guild_dm_returns_false():
    adapter = _make_adapter(allowed_guilds=[], watch_all_guild_channels=True)
    msg = _mock_message(is_dm=True, author_id=12345)
    assert adapter._watches_guild(msg) is False


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


@pytest.mark.asyncio
async def test_download_document_attachment_without_exposing_local_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _make_adapter()
    msg = _mock_message(message_id=777)
    msg.attachments = [
        SimpleNamespace(
            filename="report.txt",
            content_type="text/plain",
            size=17,
            id=888,
            url="https://cdn.example/report.txt",
        )
    ]

    class FakeResponse:
        content = b"Discord doc body"

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url: str) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr("httpx.AsyncClient", FakeClient)

    text, attachments = await adapter._download_document_attachments(msg)

    assert "report.txt" in text
    assert str(tmp_path) not in text
    # Phase 2 (2f) full-read contract wording — the stale "bounded" phrasing
    # must be gone.
    assert "The document's content is provided to the model along with this message." in text
    assert "say so explicitly instead of guessing" in text
    assert "Bounded attachment context" not in text
    assert attachments == [
        Attachment(
            filename="report.txt",
            mimetype="text/plain",
            url=str(tmp_path / "thehomie_discord_documents" / "777_888.txt"),
            size_bytes=17,
        )
    ]
    assert Path(attachments[0].url or "").read_text(encoding="utf-8") == "Discord doc body"
