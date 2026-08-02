from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from adapters.base import ProgressCapabilities
from adapters.buzz import BuzzAdapter
from buzz_lock import BuzzIdentityLock
from buzz_nostr import public_key_from_private, sign_event
from coincurve import PrivateKey
from extension_manager import CommandSpec, ExtensionManager
from models import Attachment, Channel, OutgoingMessage, Platform, Thread

from buzz_config import BuzzSettings, get_buzz_settings


def _settings(private_key: bytes, allowed: str, *, require_mention: bool = True) -> BuzzSettings:
    return BuzzSettings(
        relay_url="ws://127.0.0.1:3000",
        private_key=private_key.hex(),
        allowed_pubkeys=(allowed,),
        channels=("room-1",),
        home_channel="room-1",
        signal_channel="signal-1",
        cli_path="buzz",
        transport="auto",
        require_mention=require_mention,
        desktop_path="",
    )


def _chat_event(private_key: bytes, content: str, *, tags=None, created_at=None):
    return sign_event(
        {
            "pubkey": public_key_from_private(private_key),
            "created_at": created_at or int(time.time()),
            "kind": 9,
            "tags": [["h", "room-1"]] if tags is None else tags,
            "content": content,
        },
        private_key,
    )


def _ready_adapter(tmp_path, *, require_mention=True):
    self_key = PrivateKey().secret
    user_key = PrivateKey().secret
    user_pubkey = public_key_from_private(user_key)
    adapter = BuzzAdapter(
        _settings(self_key, user_pubkey, require_mention=require_mention),
        state_path=tmp_path / "buzz.db",
        status_path=tmp_path / "status.json",
    )
    adapter._private_key = self_key
    adapter._self_pubkey = public_key_from_private(self_key)
    adapter._self_npub = "npub-self"
    adapter._display_name = "Homie"
    adapter._allowed_pubkeys = {user_pubkey}
    adapter._user_roles = {user_pubkey: "viewer"}
    adapter._channel_types = {"room-1": "group", "dm-1": "dm"}
    adapter._channel_names = {"room-1": "general", "dm-1": "DM"}
    adapter._send_reaction = AsyncMock(return_value=True)
    adapter._resolve_user_name = AsyncMock(return_value="Alice")
    return adapter, self_key, user_key


@pytest.mark.asyncio
async def test_invalid_identity_fails_closed_without_persisting_secret(tmp_path) -> None:
    secret = "not-a-valid-private-key"
    settings = BuzzSettings(
        relay_url="ws://127.0.0.1:3000",
        private_key=secret,
        allowed_pubkeys=("also-invalid",),
        channels=("room-1",),
        home_channel="room-1",
        signal_channel="signal-1",
        cli_path="buzz",
        transport="auto",
        require_mention=True,
        desktop_path="",
    )
    status_path = tmp_path / "status.json"
    adapter = BuzzAdapter(
        settings,
        state_path=tmp_path / "buzz.db",
        status_path=status_path,
        lock_root=tmp_path / "locks",
    )
    adapter.cli_path = "buzz"

    with pytest.raises(RuntimeError, match="invalid Buzz identity configuration"):
        await adapter.connect()

    assert adapter._state == "failed"
    assert secret not in status_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_room_requires_mention_and_strips_it_before_command_detection(tmp_path) -> None:
    adapter, _self_key, user_key = _ready_adapter(tmp_path)
    await adapter._handle_event("room-1", _chat_event(user_key, "broadcast"))
    assert adapter._queue.empty()

    event = _chat_event(user_key, "@Homie /status")
    await adapter._handle_event("room-1", event)
    message = adapter._queue.get_nowait()

    assert message.text == "/status"
    assert message.platform is Platform.BUZZ
    assert message.user.platform_id == public_key_from_private(user_key)
    assert message.user_role == "viewer"
    assert message.channel.platform_id == "room-1"
    assert message.thread.parent_message_id == event["id"]
    adapter._send_reaction.assert_awaited_once_with(event["id"])


@pytest.mark.asyncio
async def test_dm_needs_no_mention_and_preserves_nostr_thread_tags(tmp_path) -> None:
    adapter, _self_key, user_key = _ready_adapter(tmp_path)
    root = "a" * 64
    parent = "b" * 64
    event = _chat_event(
        user_key,
        "hello privately",
        tags=[["h", "dm-1"], ["e", root, "", "root"], ["e", parent, "", "reply"]],
    )
    await adapter._handle_event("dm-1", event)
    message = adapter._queue.get_nowait()

    assert message.channel.is_dm is True
    assert message.thread == Thread(thread_id=root, parent_message_id=parent)


@pytest.mark.asyncio
async def test_unauthorized_self_echo_invalid_and_oversized_events_do_not_dispatch(
    tmp_path,
) -> None:
    adapter, self_key, _user_key = _ready_adapter(tmp_path, require_mention=False)
    unauthorized = PrivateKey().secret
    cases = [
        _chat_event(unauthorized, "unauthorized"),
        _chat_event(self_key, "self echo"),
        dict(_chat_event(unauthorized, "tampered"), sig="00" * 64),
        _chat_event(unauthorized, "x" * 65_537),
    ]
    for event in cases:
        await adapter._handle_event("room-1", event)

    assert adapter._queue.empty()
    adapter._send_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_unmapped_allowlisted_sender_cannot_run_admin_command(tmp_path) -> None:
    adapter, _self_key, user_key = _ready_adapter(tmp_path)
    await adapter._handle_event("room-1", _chat_event(user_key, "@Homie /admin-only"))
    incoming = adapter._queue.get_nowait()
    invoked = False

    async def admin_handler(*_args, **_kwargs):
        nonlocal invoked
        invoked = True
        return "unsafe"

    manager = ExtensionManager()
    manager.register_command(
        CommandSpec(
            name="admin-only",
            description="test",
            type="router",
            min_role="admin",
            handler=admin_handler,
        )
    )

    result = await manager.dispatch("admin-only", adapter, incoming, "")

    assert result == "Permission denied: /admin-only requires admin role."
    assert invoked is False


def test_pubkey_role_mapping_is_parsed_without_exposing_secret() -> None:
    pubkey = "a" * 64
    settings = get_buzz_settings(
        {
            "BUZZ_RELAY_URL": "ws://127.0.0.1:3000",
            "BUZZ_PRIVATE_KEY": "11" * 32,
            "BUZZ_ALLOWED_PUBKEYS": pubkey,
            "BUZZ_PUBKEY_ROLES": f"{pubkey}=operator",
        }
    )

    assert settings.pubkey_roles == ((pubkey, "operator"),)
    assert settings.private_key not in repr(settings)


def test_reply_target_and_progress_are_bounded(tmp_path) -> None:
    adapter, _self_key, _user_key = _ready_adapter(tmp_path)
    assert adapter._reply_target(Thread("a" * 64)) == "a" * 64
    assert adapter._reply_target(Thread("room-uuid")) is None
    assert adapter.progress_capabilities == ProgressCapabilities()


def test_buzz_v052_imeta_media_and_protocol_mention_are_normalized(tmp_path) -> None:
    adapter, _self_key, _user_key = _ready_adapter(tmp_path)
    event = {
        "tags": [
            ["p", adapter._self_pubkey],
            [
                "imeta",
                "url http://127.0.0.1:3000/media/hash",
                "m image/png",
                "x deadbeef",
                "size 321",
            ],
        ]
    }

    assert adapter._is_mentioned("please inspect this", event) is True
    assert adapter._attachments_from_event(event) == [
        Attachment(
            filename="hash.png",
            mimetype="image/png",
            url="http://127.0.0.1:3000/media/hash",
            size_bytes=321,
        )
    ]


@pytest.mark.asyncio
async def test_cli_invocation_uses_argument_array_stdin_and_secret_only_in_env(
    tmp_path, monkeypatch
) -> None:
    adapter, _self_key, _user_key = _ready_adapter(tmp_path)
    adapter.cli_path = "C:/Program Files/Buzz/buzz.exe"
    captured = {}

    class Process:
        returncode = 0

        async def communicate(self, value):
            captured["stdin"] = value
            return b"{}", b""

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await adapter._run_cli(
        ["messages", "send", "--channel", "room;not-a-shell", "--content", "-"],
        input_text="hello $(whoami)",
    )

    assert captured["args"][0] == adapter.cli_path
    assert captured["args"][1:] == (
        "messages",
        "send",
        "--channel",
        "room;not-a-shell",
        "--content",
        "-",
    )
    assert adapter.settings.private_key not in captured["args"]
    assert captured["env"]["BUZZ_PRIVATE_KEY"] == adapter.settings.private_key
    assert captured["stdin"] == b"hello $(whoami)"


def test_identity_lock_conflict_is_scoped_by_relay_and_pubkey(tmp_path) -> None:
    first = BuzzIdentityLock("ws://relay", "a" * 64, root=tmp_path)
    duplicate = BuzzIdentityLock("ws://relay", "a" * 64, root=tmp_path)
    different = BuzzIdentityLock("ws://relay", "b" * 64, root=tmp_path)

    assert first.acquire() is True
    try:
        assert duplicate.acquire() is False
        assert different.acquire() is True
        different.release()
    finally:
        first.release()


@pytest.mark.asyncio
async def test_send_uses_thread_reply_and_local_media_arguments(tmp_path) -> None:
    adapter, _self_key, _user_key = _ready_adapter(tmp_path)
    local = tmp_path / "proof.txt"
    local.write_text("proof", encoding="utf-8")
    adapter._run_cli = AsyncMock(return_value=(0, '{"accepted":true,"event_id":"abc"}', ""))
    message = OutgoingMessage(
        text="done",
        channel=Channel(Platform.BUZZ, "room-1"),
        thread=Thread("a" * 64, "b" * 64),
        attachments=[Attachment(filename="proof.txt", url=str(local))],
    )

    assert await adapter.send(message) == "abc"
    args = adapter._run_cli.await_args.args[0]
    assert args[:6] == ["messages", "send", "--channel", "room-1", "--content", "-"]
    assert ["--reply-to", "b" * 64] == args[6:8]
    assert args[8:] == ["--file", str(local)]


@pytest.mark.asyncio
async def test_scheduled_delivery_uses_only_profile_home_channel(tmp_path) -> None:
    adapter, _self_key, _user_key = _ready_adapter(tmp_path)
    adapter._send_text = AsyncMock(return_value="scheduled-event")

    assert await adapter.deliver_scheduled("daily result") == "scheduled-event"
    adapter._send_text.assert_awaited_once_with("room-1", "daily result", attachments=None)


@pytest.mark.asyncio
async def test_channel_named_dm_remains_mention_gated_without_relay_confirmation(
    tmp_path,
) -> None:
    adapter, _self_key, user_key = _ready_adapter(tmp_path)
    dm_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    adapter.settings = BuzzSettings(
        relay_url=adapter.settings.relay_url,
        private_key=adapter.settings.private_key,
        allowed_pubkeys=adapter.settings.allowed_pubkeys,
        channels=(dm_id,),
        home_channel=adapter.settings.home_channel,
        signal_channel=adapter.settings.signal_channel,
        cli_path=adapter.settings.cli_path,
        transport=adapter.settings.transport,
        require_mention=True,
        desktop_path="",
    )
    adapter._run_cli = AsyncMock(
        side_effect=[
            (
                0,
                '[{"channel_id":"aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee","name":"DM"}]',
                "",
            ),
            (0, "[]", ""),
            (
                0,
                f"[{json.dumps({'channel_id': dm_id, 'name': 'DM', 'channel_type': 'stream'})}]",
                "",
            ),
        ]
    )

    await adapter._discover_channels(seed=False)
    await adapter._handle_event(
        dm_id,
        _chat_event(user_key, "hello without a mention", tags=[["h", dm_id]]),
    )

    assert adapter._channel_types[dm_id] == "group"
    assert adapter._queue.empty()


@pytest.mark.asyncio
async def test_relay_confirmed_dm_bypasses_room_mention_gate(tmp_path) -> None:
    adapter, _self_key, user_key = _ready_adapter(tmp_path)
    dm_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    adapter.settings = BuzzSettings(**{**adapter.settings.__dict__, "channels": (dm_id,)})
    adapter._run_cli = AsyncMock(
        side_effect=[
            (0, f"[{json.dumps({'channel_id': dm_id, 'name': 'DM'})}]", ""),
            (
                0,
                f"[{json.dumps({'dm_id': dm_id, 'participants': [adapter._self_pubkey]})}]",
                "",
            ),
        ]
    )

    await adapter._discover_channels(seed=False)
    await adapter._handle_event(
        dm_id,
        _chat_event(user_key, "hello without a mention", tags=[["h", dm_id]]),
    )

    assert adapter._channel_types[dm_id] == "dm"
    assert adapter._queue.get_nowait().text == "hello without a mention"


@pytest.mark.asyncio
async def test_v052_relay_only_channel_type_confirms_dm_when_dms_list_is_empty(
    tmp_path,
) -> None:
    adapter, _self_key, user_key = _ready_adapter(tmp_path)
    dm_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    adapter.settings = BuzzSettings(**{**adapter.settings.__dict__, "channels": (dm_id,)})
    adapter._run_cli = AsyncMock(
        side_effect=[
            (0, f"[{json.dumps({'channel_id': dm_id, 'name': 'DM'})}]", ""),
            (0, "[]", ""),
            (
                0,
                f"[{json.dumps({'channel_id': dm_id, 'name': 'DM', 'channel_type': 'dm'})}]",
                "",
            ),
        ]
    )

    await adapter._discover_channels(seed=False)
    await adapter._handle_event(
        dm_id,
        _chat_event(user_key, "hello without a mention", tags=[["h", dm_id]]),
    )

    assert adapter._channel_types[dm_id] == "dm"
    assert adapter._queue.get_nowait().text == "hello without a mention"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tags",
    [
        [],
        [["h", "different-room"]],
        [["h", "room-1"], ["h", "room-1"]],
        [["h", "room-1"], ["h", "different-room"]],
    ],
)
async def test_signed_event_must_bind_exactly_once_to_subscription_channel(tmp_path, tags) -> None:
    adapter, _self_key, user_key = _ready_adapter(tmp_path, require_mention=False)
    event = _chat_event(user_key, "cross-channel", tags=tags)

    await adapter._handle_event("room-1", event)

    assert adapter._queue.empty()
    adapter._send_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_inbound_media_is_materialized_from_bounded_relay_origin(tmp_path) -> None:
    body = b"proof contents"

    async def serve(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(body)).encode("ascii")
            + b"\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    adapter, _self_key, _user_key = _ready_adapter(tmp_path)
    adapter.settings = BuzzSettings(
        **{**adapter.settings.__dict__, "relay_url": f"ws://127.0.0.1:{port}"}
    )
    adapter.media_root = tmp_path / "media"
    try:
        materialized = await adapter._materialize_attachments(
            "a" * 64,
            [
                Attachment(
                    filename="../proof.txt",
                    mimetype="text/plain",
                    url=f"http://127.0.0.1:{port}/media/proof",
                    size_bytes=len(body),
                )
            ],
        )
    finally:
        server.close()
        await server.wait_closed()

    assert materialized[0].filename == "proof.txt"
    assert materialized[0].size_bytes == len(body)
    assert materialized[0].url is not None
    assert Path(materialized[0].url).read_bytes() == body


@pytest.mark.asyncio
async def test_inbound_media_rejects_cross_origin_and_oversized_refs(tmp_path) -> None:
    adapter, _self_key, _user_key = _ready_adapter(tmp_path)
    refs = [
        Attachment(filename="private.txt", url="http://169.254.169.254/latest"),
        Attachment(
            filename="large.bin",
            url="http://127.0.0.1:3000/large",
            size_bytes=9 * 1024 * 1024,
        ),
    ]

    materialized = await adapter._materialize_attachments("b" * 64, refs)

    assert [item.url for item in materialized] == [None, None]
