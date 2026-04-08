"""WebSocket client connecting to the server relay at ai.your-domain.example.com.

Maintains a persistent connection with automatic reconnection (exponential
backoff). Receives chat_request messages from the relay, routes them through
the ConversationEngine via the WebAdapter, and sends responses back.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import TYPE_CHECKING

import websockets
from models import Channel, IncomingMessage, Platform, Thread, User
from session_keys import build_web_channel_id, build_web_conversation_id
from websockets.asyncio.client import ClientConnection

if TYPE_CHECKING:
    from adapters.web import WebAdapter
    from router import ChatRouter


class RelayWSClient:
    """Persistent WebSocket client connecting to the server relay.

    The relay sits on ai.your-domain.example.com and bridges the MC frontend (browser)
    to this local machine where the Agent SDK actually runs. Messages flow:

        Browser -> MC API -> Relay (SSE/WS) -> this client -> ConversationEngine
        ConversationEngine -> this client -> Relay -> MC API -> Browser (SSE)

    Authentication uses a static RELAY_AUTH_TOKEN passed as a query param.
    """

    # Reconnection backoff config
    INITIAL_BACKOFF_S = 2.0
    MAX_BACKOFF_S = 120.0
    BACKOFF_MULTIPLIER = 2.0

    def __init__(
        self,
        relay_url: str,
        relay_token: str,
        router: ChatRouter,
        adapter: WebAdapter,
    ) -> None:
        self.relay_url = relay_url.rstrip("/")
        self.relay_token = relay_token
        self.router = router
        self.adapter = adapter
        self._ws: ClientConnection | None = None
        self._backoff = self.INITIAL_BACKOFF_S
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect_forever(self) -> None:
        """Connect to relay with auto-reconnect. Run as asyncio.create_task().

        Uses exponential backoff on failures, resets on successful connection.
        Never raises -- logs errors and retries indefinitely.
        """
        while True:
            try:
                url = f"{self.relay_url}?token={self.relay_token}"
                async with websockets.connect(
                    url,
                    # Large max size for long engine responses
                    max_size=10 * 1024 * 1024,  # 10MB
                    # Ping to detect dead connections (supplement server-side ping)
                    ping_interval=30,
                    ping_timeout=10,
                    # Reasonable close timeout
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    self._backoff = self.INITIAL_BACKOFF_S  # Reset on success
                    print(
                        f"[{datetime.now()}] [RelayWS] Connected to {self.relay_url}",
                        flush=True,
                    )
                    await self._listen(ws)
            except websockets.exceptions.InvalidStatus as e:
                status = e.response.status_code if hasattr(e, "response") else 0
                if status == 4001:
                    print(
                        f"[{datetime.now()}] [RelayWS] Auth rejected (4001). "
                        f"Check RELAY_AUTH_TOKEN. Retrying in {self._backoff:.0f}s...",
                        flush=True,
                    )
                else:
                    print(
                        f"[{datetime.now()}] [RelayWS] Connection rejected "
                        f"(status {status}). Retrying in {self._backoff:.0f}s...",
                        flush=True,
                    )
            except Exception as e:
                print(
                    f"[{datetime.now()}] [RelayWS] Disconnected: {e}. "
                    f"Reconnecting in {self._backoff:.0f}s...",
                    flush=True,
                )
            finally:
                self._ws = None
                self._connected = False

            await asyncio.sleep(self._backoff)
            self._backoff = min(
                self._backoff * self.BACKOFF_MULTIPLIER, self.MAX_BACKOFF_S
            )

    async def _listen(self, ws: ClientConnection) -> None:
        """Listen for messages from the relay server."""
        async for raw in ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                print(
                    f"[{datetime.now()}] [RelayWS] Invalid JSON received",
                    flush=True,
                )
                continue

            msg_type = data.get("type", "")

            if msg_type == "chat_request":
                # Spawn a task so we can handle multiple requests concurrently
                asyncio.create_task(self._handle_request(ws, data))
            elif msg_type == "ping":
                await self._send_json(ws, {"type": "pong"})
            else:
                print(
                    f"[{datetime.now()}] [RelayWS] Unknown message type: {msg_type}",
                    flush=True,
                )

    def _build_incoming(
        self, data: dict
    ) -> tuple[str, IncomingMessage]:
        """Build an IncomingMessage from relay request data.

        Returns (request_id, incoming_message).
        """
        request_id = data.get("request_id", "")
        session_key = data.get("session_key", "")
        message_text = data.get("message", "")
        user_data = data.get("user", {})

        user = User(
            platform=Platform.WEB,
            platform_id=user_data.get("user_id", "web-user"),
            display_name=user_data.get("email"),
        )

        channel = Channel(
            platform=Platform.WEB,
            platform_id=build_web_channel_id(
                session_key,
                user_data.get("user_id", "anon"),
            ),
            is_dm=True,  # Web chat is always direct
        )

        # Extract agent type and user role from relay data
        agent_type = data.get("agent_type", "thehomie")
        user_role = user_data.get("role", "admin")

        # Dual-ID: session_key = durable conversation identity,
        # request_id = ephemeral transport correlation for WS routing.
        # Fallback includes agent_type for unique session IDs.
        conversation_id = build_web_conversation_id(
            session_key,
            user_data.get("user_id", "anon"),
            agent_type,
        )

        thread = Thread(
            thread_id=conversation_id,
            parent_message_id=request_id,
        )

        incoming = IncomingMessage(
            text=message_text,
            user=user,
            channel=channel,
            platform=Platform.WEB,
            thread=thread,
            agent_type=agent_type,
            user_role=user_role,
            raw_event={"request_id": request_id},
        )

        return request_id, incoming

    async def _handle_request(self, ws: ClientConnection, data: dict) -> None:
        """Handle a chat_request: delegate to router's canonical ingress."""
        request_id, incoming = self._build_incoming(data)
        user_data = data.get("user", {})

        agent_label = "Homie"
        print(
            f"[{datetime.now()}] [RelayWS] chat_request {request_id[:8]}... "
            f"[{agent_label}] from {user_data.get('email', 'unknown')}: {incoming.text[:60]}...",
            flush=True,
        )

        try:
            # CANONICAL PATH: delegate to router (same as Telegram/Slack/Discord)
            # router._handle() wraps _handle_inner() with error handler:
            #   - Application errors (engine crash, command failure) are caught by
            #     router._handle() and sent as a normal chat_response via adapter.send()
            #   - Only transport/infrastructure failures bubble up here
            await self.router._handle(self.adapter, incoming)

        except Exception as e:
            # Transport/infrastructure failures only — application errors are
            # handled by router._handle() → adapter.send(error_text)
            print(
                f"[{datetime.now()}] [RelayWS] Transport error {request_id[:8]}...: {e}",
                flush=True,
            )
            error_msg = {
                "type": "chat_error",
                "request_id": request_id,
                "error": str(e),
                "is_done": True,
            }
            try:
                await self._send_json(ws, error_msg)
            except Exception:
                pass
            return

        # Extract final metadata and send is_done — guarded so a DB or
        # transient relay failure after content was already sent doesn't
        # leave MC hanging with no completion signal.
        try:
            conversation_id = incoming.thread.thread_id
            session = self.router.engine.session_store.get(
                "web", incoming.channel.platform_id, conversation_id
            )
            cost_usd = session.total_cost_usd if session else None
            tool_count = session.tool_call_count if session else 0
        except Exception:
            cost_usd = None
            tool_count = 0

        done_msg = {
            "type": "chat_response",
            "request_id": request_id,
            "is_done": True,
            "cost_usd": cost_usd,
            "tool_count": tool_count,
        }
        try:
            await self._send_json(ws, done_msg)
        except Exception as e:
            print(
                f"[{datetime.now()}] [RelayWS] Failed to send is_done "
                f"for {request_id[:8]}...: {e}",
                flush=True,
            )
            return

        if cost_usd:
            print(
                f"[{datetime.now()}] [RelayWS] Completed {request_id[:8]}... "
                f"(cost: ${cost_usd:.4f})",
                flush=True,
            )
        else:
            print(
                f"[{datetime.now()}] [RelayWS] Completed {request_id[:8]}...",
                flush=True,
            )

    async def send_response(
        self,
        request_id: str,
        text: str,
        is_update: bool,
        is_done: bool,
        tool_count: int = 0,
        cost_usd: float | None = None,
    ) -> None:
        """Send a response message back to the relay server.

        Called by WebAdapter.send() when the router pushes response chunks.
        """
        if not self._ws:
            return
        msg = {
            "type": "chat_response",
            "request_id": request_id,
            "text": text,
            "is_update": is_update,
            "is_done": is_done,
            "tool_count": tool_count,
            "cost_usd": cost_usd,
        }
        await self._send_json(self._ws, msg)

    async def _send_json(self, ws: ClientConnection, data: dict) -> None:
        """Send JSON to the WebSocket with error handling."""
        try:
            await ws.send(json.dumps(data))
        except websockets.exceptions.ConnectionClosed:
            print(
                f"[{datetime.now()}] [RelayWS] Cannot send -- connection closed",
                flush=True,
            )
