"""Web adapter for chat via the server relay WebSocket."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from typing import TYPE_CHECKING

from models import Channel, IncomingMessage, OutgoingMessage, Platform

if TYPE_CHECKING:
    from ws_client import RelayWSClient


class WebAdapter:
    """Adapter for web chat messages arriving via the relay WebSocket.

    Unlike TelegramAdapter which polls a platform API, WebAdapter receives
    messages pushed by the RelayWSClient and sends responses back through
    the same WebSocket connection. The adapter's listen() queue is fed
    externally by the ws_client when it receives a chat_request.
    """

    def __init__(self, ws_client: RelayWSClient) -> None:
        self.ws_client = ws_client
        self._queue: asyncio.Queue[IncomingMessage] = asyncio.Queue()

    @property
    def platform(self) -> Platform:
        return Platform.WEB

    async def connect(self) -> None:
        """No-op -- connection is managed by RelayWSClient."""
        print(f"[{datetime.now()}] Web adapter registered (relay-backed)")

    async def disconnect(self) -> None:
        """No-op -- disconnection is managed by RelayWSClient."""
        print(f"[{datetime.now()}] Web adapter disconnected")

    async def listen(self) -> AsyncIterator[IncomingMessage]:
        """Yield incoming messages pushed by the relay client."""
        while True:
            message = await self._queue.get()
            yield message

    async def send(self, message: OutgoingMessage) -> str | None:
        """Send response back through the relay WebSocket.

        parent_message_id carries the relay request_id for WS response
        correlation, while thread_id holds the durable conversation_id
        for session persistence. Falls back to thread_id for backward
        compat with non-web callers.
        """
        request_id = ""
        if message.thread:
            request_id = message.thread.parent_message_id or message.thread.thread_id or ""

        await self.ws_client.send_response(
            request_id=request_id,
            text=message.text,
            is_update=message.is_update,
            is_done=False,
        )
        return request_id or None  # Activates placeholder/update path in _handle_inner

    async def update(self, message: OutgoingMessage) -> None:
        """Edit/update an existing message -- same as send for relay."""
        await self.send(message)

    async def send_typing(self, channel: Channel) -> None:
        """No-op -- typing indicators not supported via relay."""
        pass

    def enqueue(self, message: IncomingMessage) -> None:
        """Push an incoming message into the listen queue.

        Called by RelayWSClient when it receives a chat_request from the server.
        """
        self._queue.put_nowait(message)
