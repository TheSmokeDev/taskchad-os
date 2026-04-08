"""Tests for the relay WebSocket client and web adapter."""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from adapters.web import WebAdapter
from models import Channel, IncomingMessage, OutgoingMessage, Platform, Thread, User
from session import Session


class AsyncIteratorFromList:
    """Helper to create an async iterator from a list of items."""

    def __init__(self, items):
        self._items = iter(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration


# ── WebAdapter tests ──────────────────────────────────────────────


class TestWebAdapter:
    """Test the WebAdapter class."""

    def test_platform(self):
        adapter = WebAdapter(ws_client=None)
        assert adapter.platform == Platform.WEB

    @pytest.mark.asyncio
    async def test_connect_is_noop(self):
        adapter = WebAdapter(ws_client=None)
        await adapter.connect()  # Should not raise

    @pytest.mark.asyncio
    async def test_disconnect_is_noop(self):
        adapter = WebAdapter(ws_client=None)
        await adapter.disconnect()  # Should not raise

    @pytest.mark.asyncio
    async def test_send_calls_ws_client(self):
        mock_ws = MagicMock()
        mock_ws.send_response = AsyncMock()
        adapter = WebAdapter(ws_client=mock_ws)

        msg = OutgoingMessage(
            text="Hello from engine",
            channel=Channel(platform=Platform.WEB, platform_id="session-key"),
            thread=Thread(thread_id="conv-id", parent_message_id="req-123"),
            is_update=False,
        )

        await adapter.send(msg)

        mock_ws.send_response.assert_called_once_with(
            request_id="req-123",  # reads from parent_message_id
            text="Hello from engine",
            is_update=False,
            is_done=False,
        )

    @pytest.mark.asyncio
    async def test_send_returns_request_id_when_thread_exists(self):
        """send() returns request_id from parent_message_id."""
        mock_ws = MagicMock()
        mock_ws.send_response = AsyncMock()
        adapter = WebAdapter(ws_client=mock_ws)

        msg = OutgoingMessage(
            text="Hello",
            channel=Channel(platform=Platform.WEB, platform_id="session-key"),
            thread=Thread(thread_id="conv-id", parent_message_id="req-123"),
        )

        result = await adapter.send(msg)
        assert result == "req-123"

    @pytest.mark.asyncio
    async def test_send_returns_none_when_no_thread(self):
        """send() returns None when no thread — no placeholder path."""
        mock_ws = MagicMock()
        mock_ws.send_response = AsyncMock()
        adapter = WebAdapter(ws_client=mock_ws)

        msg = OutgoingMessage(
            text="No thread",
            channel=Channel(platform=Platform.WEB, platform_id="session-key"),
        )

        result = await adapter.send(msg)
        assert result is None

    @pytest.mark.asyncio
    async def test_send_returns_none_when_empty_thread_id(self):
        """send() returns None when thread_id is empty string."""
        mock_ws = MagicMock()
        mock_ws.send_response = AsyncMock()
        adapter = WebAdapter(ws_client=mock_ws)

        msg = OutgoingMessage(
            text="Empty thread",
            channel=Channel(platform=Platform.WEB, platform_id="session-key"),
            thread=Thread(thread_id=""),
        )

        result = await adapter.send(msg)
        assert result is None

    @pytest.mark.asyncio
    async def test_update_delegates_to_send(self):
        mock_ws = MagicMock()
        mock_ws.send_response = AsyncMock()
        adapter = WebAdapter(ws_client=mock_ws)

        msg = OutgoingMessage(
            text="Updated text",
            channel=Channel(platform=Platform.WEB, platform_id="session-key"),
            thread=Thread(thread_id="conv-id", parent_message_id="req-456"),
            is_update=True,
        )

        await adapter.update(msg)
        mock_ws.send_response.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_typing_is_noop(self):
        adapter = WebAdapter(ws_client=None)
        channel = Channel(platform=Platform.WEB, platform_id="test")
        await adapter.send_typing(channel)  # Should not raise

    def test_enqueue(self):
        adapter = WebAdapter(ws_client=None)
        msg = IncomingMessage(
            text="test",
            user=User(platform=Platform.WEB, platform_id="user1"),
            channel=Channel(platform=Platform.WEB, platform_id="ch1"),
            platform=Platform.WEB,
        )
        adapter.enqueue(msg)
        assert adapter._queue.qsize() == 1


# ── RelayWSClient tests ──────────────────────────────────────────


class TestRelayWSClient:
    """Test the RelayWSClient class."""

    def _make_client(self):
        from ws_client import RelayWSClient

        mock_router = MagicMock()
        mock_adapter = MagicMock()
        client = RelayWSClient(
            relay_url="wss://test.example.com/api/chat-relay/ws/local",
            relay_token="test-token",
            router=mock_router,
            adapter=mock_adapter,
        )
        return client, mock_router, mock_adapter

    def test_initial_state(self):
        client, _, _ = self._make_client()
        assert client.is_connected is False
        assert client._ws is None
        assert client.relay_url == "wss://test.example.com/api/chat-relay/ws/local"
        assert client.relay_token == "test-token"

    def test_relay_url_strips_trailing_slash(self):
        from ws_client import RelayWSClient

        mock_router = MagicMock()
        mock_adapter = MagicMock()
        client = RelayWSClient(
            relay_url="wss://test.example.com/api/chat-relay/ws/local/",
            relay_token="tok",
            router=mock_router,
            adapter=mock_adapter,
        )
        assert not client.relay_url.endswith("/")

    def test_build_incoming(self):
        client, _, _ = self._make_client()

        data = {
            "type": "chat_request",
            "request_id": "abc-123",
            "session_key": "web:user1:thread1",
            "message": "Hello world",
            "user": {
                "user_id": "user-uuid",
                "email": "test@example.com",
                "role": "admin",
            },
        }

        request_id, incoming = client._build_incoming(data)

        assert request_id == "abc-123"
        assert incoming.text == "Hello world"
        assert incoming.platform == Platform.WEB
        assert incoming.user.platform_id == "user-uuid"
        assert incoming.user.display_name == "test@example.com"
        assert incoming.channel.platform_id == "web:user1:thread1"
        assert incoming.channel.is_dm is True
        assert incoming.thread.thread_id == "web:user1:thread1"  # durable conversation_id
        assert incoming.thread.parent_message_id == "abc-123"    # transport correlation
        assert incoming.raw_event == {"request_id": "abc-123"}

    def test_build_incoming_defaults(self):
        client, _, _ = self._make_client()

        data = {
            "type": "chat_request",
            "request_id": "xyz",
            "message": "hi",
        }

        request_id, incoming = client._build_incoming(data)
        assert request_id == "xyz"
        assert incoming.user.platform_id == "web-user"
        assert incoming.channel.platform_id == "web:anon"
        assert incoming.thread.thread_id == "web:thehomie:anon"  # fallback includes agent_type
        assert incoming.thread.parent_message_id == "xyz"

    @pytest.mark.asyncio
    async def test_send_response(self):
        client, _, _ = self._make_client()

        # Mock the WebSocket
        mock_ws = AsyncMock()
        client._ws = mock_ws

        await client.send_response(
            request_id="req-1",
            text="Response chunk",
            is_update=False,
            is_done=False,
            tool_count=2,
            cost_usd=0.0042,
        )

        mock_ws.send.assert_called_once()
        sent_data = json.loads(mock_ws.send.call_args[0][0])
        assert sent_data["type"] == "chat_response"
        assert sent_data["request_id"] == "req-1"
        assert sent_data["text"] == "Response chunk"
        assert sent_data["is_done"] is False
        assert sent_data["tool_count"] == 2
        assert sent_data["cost_usd"] == 0.0042

    @pytest.mark.asyncio
    async def test_send_response_no_connection(self):
        client, _, _ = self._make_client()
        client._ws = None

        # Should not raise when no connection
        await client.send_response(
            request_id="req-1", text="test", is_update=False, is_done=False,
        )

    @pytest.mark.asyncio
    async def test_listen_handles_ping(self):
        client, _, _ = self._make_client()

        mock_ws = AsyncMock()
        ping_msg = json.dumps({"type": "ping"})

        # Create a proper async iterator for the WebSocket
        mock_ws.__aiter__ = lambda self: AsyncIteratorFromList([ping_msg])

        await client._listen(mock_ws)

        # Should have sent pong
        mock_ws.send.assert_called_once()
        sent = json.loads(mock_ws.send.call_args[0][0])
        assert sent["type"] == "pong"

    @pytest.mark.asyncio
    async def test_listen_handles_invalid_json(self, capsys):
        client, _, _ = self._make_client()

        mock_ws = AsyncMock()
        mock_ws.__aiter__ = lambda self: AsyncIteratorFromList(["not valid json"])

        await client._listen(mock_ws)

        captured = capsys.readouterr()
        assert "Invalid JSON" in captured.out

    @pytest.mark.asyncio
    async def test_listen_handles_unknown_type(self, capsys):
        client, _, _ = self._make_client()

        mock_ws = AsyncMock()
        mock_ws.__aiter__ = lambda self: AsyncIteratorFromList(
            [json.dumps({"type": "unknown_thing"})]
        )

        await client._listen(mock_ws)

        captured = capsys.readouterr()
        assert "Unknown message type" in captured.out

    # ── New canonical routing tests ──────────────────────────────

    @pytest.mark.asyncio
    async def test_handle_request_delegates_to_router(self):
        """_handle_request calls router._handle(adapter, incoming)."""
        client, mock_router, mock_adapter = self._make_client()
        mock_router._handle = AsyncMock()
        mock_router.engine.session_store.get.return_value = None

        mock_ws = AsyncMock()
        client._ws = mock_ws

        data = {
            "type": "chat_request",
            "request_id": "req-001",
            "message": "hello",
            "user": {"user_id": "u1", "email": "test@test.com"},
        }

        await client._handle_request(mock_ws, data)

        mock_router._handle.assert_called_once()
        call_args = mock_router._handle.call_args
        assert call_args[0][0] is mock_adapter  # adapter
        assert call_args[0][1].text == "hello"  # incoming

    @pytest.mark.asyncio
    async def test_handle_request_sends_is_done(self):
        """_handle_request sends is_done after router completes."""
        client, mock_router, _ = self._make_client()
        mock_router._handle = AsyncMock()
        mock_router.engine.session_store.get.return_value = None

        mock_ws = AsyncMock()
        client._ws = mock_ws

        data = {
            "type": "chat_request",
            "request_id": "req-002",
            "message": "test",
            "user": {"user_id": "u1"},
        }

        await client._handle_request(mock_ws, data)

        # Last send call should be is_done
        sent_calls = mock_ws.send.call_args_list
        assert len(sent_calls) >= 1
        last_payload = json.loads(sent_calls[-1][0][0])
        assert last_payload["is_done"] is True
        assert last_payload["request_id"] == "req-002"

    @pytest.mark.asyncio
    async def test_handle_request_sends_chat_error_on_transport_exception(self):
        """Transport failures produce chat_error (not application errors)."""
        client, mock_router, _ = self._make_client()
        mock_router._handle = AsyncMock(side_effect=Exception("WS transport died"))

        mock_ws = AsyncMock()
        client._ws = mock_ws

        data = {
            "type": "chat_request",
            "request_id": "req-003",
            "message": "test",
            "user": {"user_id": "u1"},
        }

        await client._handle_request(mock_ws, data)

        sent_calls = mock_ws.send.call_args_list
        assert len(sent_calls) >= 1
        error_payload = json.loads(sent_calls[-1][0][0])
        assert error_payload["type"] == "chat_error"
        assert error_payload["is_done"] is True
        assert "WS transport died" in error_payload["error"]

    @pytest.mark.asyncio
    async def test_handle_request_extracts_cost_and_tool_count(self):
        """is_done payload includes cost_usd and tool_count from session.
        Also asserts session_store.get() is called with conversation_id, not request_id.
        """
        client, mock_router, _ = self._make_client()
        mock_router._handle = AsyncMock()

        mock_session = Session(
            session_id="web:ch:req",
            agent_session_id="sid",
            platform="web",
            channel_id="ch",
            thread_id="req-004",
            user_id="u1",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            total_cost_usd=0.05,
            tool_call_count=7,
        )
        mock_router.engine.session_store.get.return_value = mock_session

        mock_ws = AsyncMock()
        client._ws = mock_ws

        data = {
            "type": "chat_request",
            "request_id": "req-004",
            "session_key": "web:sb:u1:agent_sb",
            "message": "test",
            "user": {"user_id": "u1"},
        }

        await client._handle_request(mock_ws, data)

        # CRITICAL: session lookup must use conversation_id (session_key), NOT request_id
        mock_router.engine.session_store.get.assert_called_once_with(
            "web", "web:sb:u1:agent_sb", "web:sb:u1:agent_sb"
        )

        sent_calls = mock_ws.send.call_args_list
        done_payload = json.loads(sent_calls[-1][0][0])
        assert done_payload["is_done"] is True
        assert done_payload["cost_usd"] == 0.05
        assert done_payload["tool_count"] == 7

    @pytest.mark.asyncio
    async def test_handle_request_handles_missing_session(self):
        """Missing session → cost_usd=None, tool_count=0 in is_done."""
        client, mock_router, _ = self._make_client()
        mock_router._handle = AsyncMock()
        mock_router.engine.session_store.get.return_value = None

        mock_ws = AsyncMock()
        client._ws = mock_ws

        data = {
            "type": "chat_request",
            "request_id": "req-005",
            "message": "test",
            "user": {"user_id": "u1"},
        }

        await client._handle_request(mock_ws, data)

        sent_calls = mock_ws.send.call_args_list
        done_payload = json.loads(sent_calls[-1][0][0])
        assert done_payload["cost_usd"] is None
        assert done_payload["tool_count"] == 0


class TestBypassRemoval:
    """Verify the bypass code has been fully removed from ws_client."""

    def test_router_commands_not_in_module(self):
        """_ROUTER_COMMANDS set no longer exists in ws_client."""
        import ws_client as ws_mod
        assert not hasattr(ws_mod, "_ROUTER_COMMANDS")

    def test_commands_module_not_imported(self):
        """commands module functions no longer imported."""
        import ws_client as ws_mod
        assert not hasattr(ws_mod, "get_piv_instruction")
        assert not hasattr(ws_mod, "get_engine_command_description")
        assert not hasattr(ws_mod, "get_router_commands")

    def test_handle_router_command_removed(self):
        """_handle_router_command method no longer exists."""
        from ws_client import RelayWSClient
        assert not hasattr(RelayWSClient, "_handle_router_command")


# ── Integration test: canonical path ─────────────────────────────


class TestRelayCanonicalPath:
    """Integration test: relay → router → adapter → WS (real wiring, mocked transport)."""

    @pytest.mark.asyncio
    async def test_message_flows_through_canonical_path(self):
        """Full wiring: relay → _build_incoming → router._handle → adapter.send → WS."""
        from router import ChatRouter
        from ws_client import RelayWSClient

        # Build real components with mocked engine boundary
        mock_engine = MagicMock()

        async def fake_handle(msg, progress=None):
            yield OutgoingMessage(
                text="Hello back",
                channel=msg.channel,
                thread=msg.thread,
            )

        mock_engine.handle_message = fake_handle
        mock_engine.session_store = MagicMock()
        mock_engine.session_store.get.return_value = None

        from extension_manager import ExtensionManager
        manager = ExtensionManager()
        router = ChatRouter(mock_engine, manager)
        web_adapter = WebAdapter(None)
        relay_client = RelayWSClient(
            relay_url="wss://test/ws",
            relay_token="tok",
            router=router,
            adapter=web_adapter,
        )
        web_adapter.ws_client = relay_client
        router.register(web_adapter)

        # Mock the WS connection
        mock_ws = AsyncMock()
        relay_client._ws = mock_ws

        data = {
            "type": "chat_request",
            "request_id": "test-req-001",
            "message": "hello",
            "user": {"user_id": "u1", "email": "test@test.com"},
        }
        await relay_client._handle_request(mock_ws, data)

        # Verify WS received messages
        sent_calls = mock_ws.send.call_args_list
        sent_payloads = [json.loads(c[0][0]) for c in sent_calls]

        non_done = [p for p in sent_payloads if not p.get("is_done")]
        done = [p for p in sent_payloads if p.get("is_done")]

        assert len(done) >= 1, "Missing is_done signal"

        # First non-done frame should be "Thinking..." placeholder
        assert non_done[0]["text"] == "Thinking..."
        assert non_done[0].get("is_update") is not True, (
            "Placeholder should not be is_update"
        )

        # Final content should be the response with is_update=true
        content_frames = [p for p in non_done if p["text"] != "Thinking..."]
        assert any("Hello back" in p["text"] for p in content_frames), (
            "Missing response"
        )
        assert content_frames[-1].get("is_update") is True, (
            "Final response should be is_update=true (via adapter.update)"
        )

    @pytest.mark.asyncio
    async def test_router_error_sent_via_adapter_not_chat_error(self):
        """Application errors are caught by router._handle → adapter.send,
        NOT surfaced as chat_error."""
        from router import ChatRouter
        from ws_client import RelayWSClient

        mock_engine = MagicMock()

        async def exploding_handle(msg, progress=None):
            raise RuntimeError("engine exploded")
            yield  # make it a generator  # noqa: E501

        mock_engine.handle_message = exploding_handle
        mock_engine.session_store = MagicMock()
        mock_engine.session_store.get.return_value = None

        from extension_manager import ExtensionManager
        manager = ExtensionManager()
        router = ChatRouter(mock_engine, manager)
        web_adapter = WebAdapter(None)
        relay_client = RelayWSClient(
            relay_url="wss://test/ws",
            relay_token="tok",
            router=router,
            adapter=web_adapter,
        )
        web_adapter.ws_client = relay_client
        router.register(web_adapter)

        mock_ws = AsyncMock()
        relay_client._ws = mock_ws

        data = {
            "type": "chat_request",
            "request_id": "err-req",
            "message": "hello",
            "user": {"user_id": "u1"},
        }
        await relay_client._handle_request(mock_ws, data)

        sent_payloads = [json.loads(c[0][0]) for c in mock_ws.send.call_args_list]
        # router._handle catches the error and sends via adapter.send
        # → appears as chat_response, NOT chat_error
        error_types = [p["type"] for p in sent_payloads]
        assert "chat_error" not in error_types, (
            "Application errors should be handled by router._handle, not surface as chat_error"
        )
        # Should still get is_done
        assert any(p.get("is_done") for p in sent_payloads)


# ── Durable identity tests ──────────────────────────────────────


class TestDurableIdentity:
    """Tests for Phase 2: durable conversation identity via session_key."""

    def _make_client(self):
        from ws_client import RelayWSClient

        mock_router = MagicMock()
        mock_adapter = MagicMock()
        client = RelayWSClient(
            relay_url="wss://test.example.com/ws",
            relay_token="tok",
            router=mock_router,
            adapter=mock_adapter,
        )
        return client, mock_router, mock_adapter

    def test_session_key_becomes_thread_id(self):
        """session_key maps to thread_id (durable), request_id to parent_message_id (transport)."""
        client, _, _ = self._make_client()
        data = {
            "request_id": "ephemeral-uuid",
            "session_key": "web:thehomie:user1:agent_homie",
            "message": "hello",
            "user": {"user_id": "user1"},
        }
        request_id, incoming = client._build_incoming(data)
        assert incoming.thread.thread_id == "web:thehomie:user1:agent_homie"
        assert incoming.thread.parent_message_id == "ephemeral-uuid"
        assert request_id == "ephemeral-uuid"

    def test_same_session_key_same_thread_id(self):
        """Two messages with same session_key produce same thread_id → engine finds same session."""
        client, _, _ = self._make_client()
        base = {
            "session_key": "web:thehomie:user1:agent_homie",
            "message": "msg",
            "user": {"user_id": "user1"},
        }

        _, incoming1 = client._build_incoming({**base, "request_id": "req-aaa"})
        _, incoming2 = client._build_incoming({**base, "request_id": "req-bbb"})

        assert incoming1.thread.thread_id == incoming2.thread.thread_id
        assert incoming1.thread.parent_message_id != incoming2.thread.parent_message_id

    def test_different_session_keys_different_thread_ids(self):
        """Different session_keys → different thread_ids → separate sessions."""
        client, _, _ = self._make_client()
        base = {"request_id": "req-1", "message": "msg", "user": {"user_id": "u1"}}

        _, inc1 = client._build_incoming({**base, "session_key": "web:thehomie:u1:agent_a"})
        _, inc2 = client._build_incoming({**base, "session_key": "web:thehomie:u1:agent_b"})

        assert inc1.thread.thread_id != inc2.thread.thread_id

    def test_orchestration_bar_session_key_is_unique(self):
        """Orchestration bar uses timestamp-based session_key → one-off sessions."""
        client, _, _ = self._make_client()
        data = {
            "request_id": "req-orch",
            "session_key": "web:operator:mc:1711234567",
            "message": "/status",
            "user": {"user_id": "admin"},
        }
        _, incoming = client._build_incoming(data)
        assert incoming.thread.thread_id == "web:operator:mc:1711234567"

    def test_fallback_includes_agent_type(self):
        """No session_key → fallback includes agent_type for unique session IDs."""
        client, _, _ = self._make_client()
        data = {
            "request_id": "req-fb",
            "message": "msg",
            "user": {"user_id": "user1"},
            "agent_type": "thehomie",
        }
        _, incoming = client._build_incoming(data)
        assert incoming.thread.thread_id == "web:thehomie:user1"

    def test_fallback_thehomie_default(self):
        """No session_key + no agent_type → default 'thehomie' in fallback."""
        client, _, _ = self._make_client()
        data = {"request_id": "req-x", "message": "msg", "user": {"user_id": "u2"}}
        _, incoming = client._build_incoming(data)
        assert incoming.thread.thread_id == "web:thehomie:u2"

    def test_session_reuse_with_real_store(self, tmp_path):
        """End-to-end: two messages with same session_key reuse the same persisted session."""
        from session import SQLiteSessionStore, Session

        store = SQLiteSessionStore(tmp_path / "e2e_chat.db")
        client, _, _ = self._make_client()

        session_key = "web:thehomie:user1:agent_homie"
        now = datetime.now()

        # First message — creates session
        _, inc1 = client._build_incoming({
            "request_id": "req-aaa",
            "session_key": session_key,
            "message": "first",
            "user": {"user_id": "user1"},
        })
        conv_id = inc1.thread.thread_id
        channel_id = inc1.channel.platform_id
        store.create(Session(
            session_id=f"web:{channel_id}:{conv_id}",
            agent_session_id="agent-1",
            platform="web",
            channel_id=channel_id,
            thread_id=conv_id,
            user_id="user1",
            created_at=now,
            updated_at=now,
            message_count=1,
        ))

        # Second message — different request_id, same session_key
        _, inc2 = client._build_incoming({
            "request_id": "req-bbb",
            "session_key": session_key,
            "message": "second",
            "user": {"user_id": "user1"},
        })
        # Lookup with second message's thread_id should find the SAME session
        found = store.get("web", inc2.channel.platform_id, inc2.thread.thread_id)
        assert found is not None, "Session not found — session_key didn't produce same lookup key"
        assert found.message_count == 1  # Same session, count from first create
        assert found.session_id == f"web:{channel_id}:{conv_id}"

        # Simulate engine incrementing message_count
        found.message_count = 2
        store.update(found)

        # Third message — still finds updated session
        _, inc3 = client._build_incoming({
            "request_id": "req-ccc",
            "session_key": session_key,
            "message": "third",
            "user": {"user_id": "user1"},
        })
        found2 = store.get("web", inc3.channel.platform_id, inc3.thread.thread_id)
        assert found2 is not None
        assert found2.message_count == 2  # Persisted increment


class TestWebAdapterDualId:
    """Test WebAdapter reads request_id from parent_message_id."""

    @pytest.mark.asyncio
    async def test_send_reads_parent_message_id(self):
        """send() uses parent_message_id for WS request_id correlation."""
        mock_ws = MagicMock()
        mock_ws.send_response = AsyncMock()
        adapter = WebAdapter(ws_client=mock_ws)

        msg = OutgoingMessage(
            text="response",
            channel=Channel(platform=Platform.WEB, platform_id="ch"),
            thread=Thread(thread_id="durable-conv-id", parent_message_id="req-transport-123"),
        )
        result = await adapter.send(msg)

        mock_ws.send_response.assert_called_once_with(
            request_id="req-transport-123",
            text="response",
            is_update=False,
            is_done=False,
        )
        assert result == "req-transport-123"

    @pytest.mark.asyncio
    async def test_send_falls_back_to_thread_id(self):
        """When parent_message_id is None, falls back to thread_id."""
        mock_ws = MagicMock()
        mock_ws.send_response = AsyncMock()
        adapter = WebAdapter(ws_client=mock_ws)

        msg = OutgoingMessage(
            text="fallback",
            channel=Channel(platform=Platform.WEB, platform_id="ch"),
            thread=Thread(thread_id="fallback-id", parent_message_id=None),
        )
        result = await adapter.send(msg)

        mock_ws.send_response.assert_called_once_with(
            request_id="fallback-id",
            text="fallback",
            is_update=False,
            is_done=False,
        )
        assert result == "fallback-id"


class TestSessionStoreGetByUser:
    """Test get_by_user() on SQLiteSessionStore."""

    def _make_store(self, tmp_path):
        from session import SQLiteSessionStore
        return SQLiteSessionStore(tmp_path / "test_chat.db")

    def test_get_by_user_returns_matching_sessions(self, tmp_path):
        from session import SQLiteSessionStore
        store = SQLiteSessionStore(tmp_path / "test_chat.db")
        now = datetime.now()

        # Create sessions for same user on different platforms
        for platform in ("telegram", "web", "slack"):
            store.create(Session(
                session_id=f"{platform}:ch:{platform}-thread",
                agent_session_id="agent-1",
                platform=platform,
                channel_id="ch",
                thread_id=f"{platform}-thread",
                user_id="user-42",
                created_at=now,
                updated_at=now,
            ))

        # Create a session for a different user
        store.create(Session(
            session_id="web:ch:other-thread",
            agent_session_id="agent-2",
            platform="web",
            channel_id="ch",
            thread_id="other-thread",
            user_id="user-99",
            created_at=now,
            updated_at=now,
        ))

        results = store.get_by_user("user-42")
        assert len(results) == 3
        assert all(s.user_id == "user-42" for s in results)

    def test_get_by_user_empty(self, tmp_path):
        from session import SQLiteSessionStore
        store = SQLiteSessionStore(tmp_path / "test_chat.db")
        results = store.get_by_user("nonexistent-user")
        assert results == []
