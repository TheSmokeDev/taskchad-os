from __future__ import annotations

import asyncio
import json
import time

import pytest
import websockets
from adapters.buzz import BuzzAdapter
from buzz_nostr import public_key_from_private, sign_event, verify_event
from coincurve import PrivateKey

from buzz_config import BuzzSettings


class FakeCliBuzzAdapter(BuzzAdapter):
    def __init__(self, *args, self_pubkey: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.cli_path = "fake-buzz"
        self.self_pubkey = self_pubkey
        self.calls: list[list[str]] = []

    async def _run_cli(self, args, *, input_text=None, timeout=30):
        self.calls.append(list(args))
        if args == ["--version"]:
            return 0, "buzz 0.5.2", ""
        if args == ["users", "get"]:
            return 0, json.dumps([{"pubkey": self.self_pubkey, "display_name": "Homie"}]), ""
        if args[:2] == ["users", "get"]:
            return 0, json.dumps([{"pubkey": args[-1], "display_name": "Alice"}]), ""
        if args == ["channels", "list"]:
            return 0, json.dumps([{"channel_id": "room-1", "name": "general"}]), ""
        if args == ["dms", "list"]:
            return 0, "[]", ""
        if args[:2] == ["messages", "get"]:
            return 0, "[]", ""
        if args[:2] == ["reactions", "add"]:
            return 0, '{"accepted":true}', ""
        if args[:2] == ["messages", "send"]:
            return 0, '{"accepted":true,"event_id":"sent-id"}', ""
        return 1, "", "unexpected fake CLI command"


def _settings(relay_url: str, self_key: bytes, allowed_pubkey: str) -> BuzzSettings:
    return BuzzSettings(
        relay_url=relay_url,
        private_key=self_key.hex(),
        allowed_pubkeys=(allowed_pubkey,),
        channels=("room-1",),
        home_channel="room-1",
        signal_channel="",
        cli_path="fake-buzz",
        transport="auto",
        require_mention=True,
        desktop_path="",
    )


async def _wait_for(predicate, *, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached before timeout")


@pytest.mark.asyncio
async def test_fake_relay_authenticates_and_dispatches_signed_event(tmp_path) -> None:
    self_key = PrivateKey().secret
    user_key = PrivateKey().secret
    user_pubkey = public_key_from_private(user_key)
    auth_events = []
    incoming = sign_event(
        {
            "pubkey": user_pubkey,
            "created_at": int(time.time()) + 1,
            "kind": 9,
            "tags": [["h", "room-1"]],
            "content": "@Homie /status",
        },
        user_key,
    )

    async def relay(connection):
        await connection.send(json.dumps(["AUTH", "challenge-1"]))
        auth_frame = json.loads(await connection.recv())
        auth_events.append(auth_frame[1])
        await connection.send(json.dumps(["OK", auth_frame[1]["id"], True, "authenticated"]))
        while True:
            request = json.loads(await connection.recv())
            if request[0] == "REQ" and request[2].get("#h") == ["room-1"]:
                await connection.send(json.dumps(["EVENT", request[1], incoming]))
                await connection.wait_closed()
                return

    server = await websockets.serve(relay, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    adapter = FakeCliBuzzAdapter(
        _settings(f"ws://127.0.0.1:{port}", self_key, user_pubkey),
        self_pubkey=public_key_from_private(self_key),
        state_path=tmp_path / "buzz.db",
        status_path=tmp_path / "status.json",
        lock_root=tmp_path / "locks",
        poll_interval=0.05,
        recovery_interval=0.1,
    )
    try:
        await adapter.connect()
        message = await asyncio.wait_for(anext(adapter.listen()), timeout=3)
        assert message.text == "/status"
        assert message.platform_message_id == incoming["id"]
        assert auth_events and verify_event(auth_events[0])
        assert auth_events[0]["kind"] == 22242
        assert any(call == ["dms", "list"] for call in adapter.calls)
        assert adapter._state == "connected"
        assert adapter._active_transport == "websocket"
    finally:
        await adapter.disconnect()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_disconnect_degrades_to_polling_then_recovers_websocket(tmp_path) -> None:
    self_key = PrivateKey().secret
    user_key = PrivateKey().secret
    user_pubkey = public_key_from_private(user_key)
    connection_count = 0

    async def relay(connection):
        nonlocal connection_count
        connection_count += 1
        current = connection_count
        await connection.send(json.dumps(["AUTH", f"challenge-{current}"]))
        auth_frame = json.loads(await connection.recv())
        await connection.send(json.dumps(["OK", auth_frame[1]["id"], True, "authenticated"]))
        await connection.recv()  # room subscription
        if current == 1:
            await connection.close(code=1012, reason="test disconnect")
            return
        await connection.wait_closed()

    server = await websockets.serve(relay, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    adapter = FakeCliBuzzAdapter(
        _settings(f"ws://127.0.0.1:{port}", self_key, user_pubkey),
        self_pubkey=public_key_from_private(self_key),
        state_path=tmp_path / "buzz.db",
        status_path=tmp_path / "status.json",
        lock_root=tmp_path / "locks",
        poll_interval=0.03,
        recovery_interval=0.08,
    )
    try:
        await adapter.connect()
        await _wait_for(lambda: adapter._state == "degraded")
        assert adapter._active_transport == "polling"
        await _wait_for(lambda: connection_count >= 2 and adapter._state == "connected")
        assert any(call[:2] == ["messages", "get"] and "--since" in call for call in adapter.calls)
    finally:
        await adapter.disconnect()
        server.close()
        await server.wait_closed()
