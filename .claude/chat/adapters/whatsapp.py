"""WhatsApp adapter using Meta Cloud API."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from models import Channel, IncomingMessage, OutgoingMessage, Platform, Thread, User


class WhatsAppAdapter:
    """WhatsApp platform adapter using Meta Cloud API.

    Runs a lightweight aiohttp webhook server to receive inbound messages.
    Sends responses via the WhatsApp Cloud API REST endpoint.
    """

    GRAPH_API_BASE = "https://graph.facebook.com/v21.0"

    def __init__(
        self,
        access_token: str,
        phone_number_id: str,
        verify_token: str,
        webhook_port: int = 8443,
        allowed_numbers: list[str] | None = None,
    ) -> None:
        self.access_token = access_token
        self.phone_number_id = phone_number_id
        self.verify_token = verify_token
        self.webhook_port = webhook_port
        self.allowed_numbers = allowed_numbers or []
        self._queue: asyncio.Queue[IncomingMessage] = asyncio.Queue()
        self._server: Any = None

    @property
    def platform(self) -> Platform:
        return Platform.WHATSAPP

    async def connect(self) -> None:
        """Start the webhook HTTP server."""
        from aiohttp import web

        app = web.Application()
        app.router.add_get("/webhook", self._handle_verify)
        app.router.add_post("/webhook", self._handle_webhook)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.webhook_port)
        await site.start()
        self._server = runner
        print(f"[{datetime.now()}] WhatsApp webhook server on port {self.webhook_port}")

    async def disconnect(self) -> None:
        """Stop the webhook server."""
        if self._server:
            await self._server.cleanup()

    async def listen(self) -> Any:
        """Yield incoming messages from the queue."""
        while True:
            message = await self._queue.get()
            yield message

    async def send(self, message: OutgoingMessage) -> str | None:
        """Send a text message via WhatsApp Cloud API."""
        import httpx

        recipient = message.channel.platform_id  # Phone number
        url = f"{self.GRAPH_API_BASE}/{self.phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient,
            "type": "text",
            "text": {"body": message.text[:4096]},  # WA limit
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("messages", [{}])[0].get("id")
            else:
                print(
                    f"[{datetime.now()}] WhatsApp send failed: "
                    f"{resp.status_code} {resp.text}"
                )
                return None

    async def update(self, message: OutgoingMessage) -> None:
        """WhatsApp doesn't support message editing — send new message."""
        await self.send(message)

    async def send_typing(self, channel: Channel) -> None:
        """No-op — WhatsApp typing via API is not well-supported."""

    async def _handle_verify(self, request: Any) -> Any:
        """Handle webhook verification GET request."""
        from aiohttp import web

        mode = request.query.get("hub.mode")
        token = request.query.get("hub.verify_token")
        challenge = request.query.get("hub.challenge")

        if mode == "subscribe" and token == self.verify_token:
            return web.Response(text=challenge)  # CRITICAL: plain text, not JSON
        return web.Response(status=403)

    async def _handle_webhook(self, request: Any) -> Any:
        """Handle inbound webhook POST with message data."""
        from aiohttp import web

        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400)

        # Extract messages from nested structure
        # GOTCHA: entry[0].changes[0].value.messages[0]
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])
                contacts = value.get("contacts", [])

                for msg in messages:
                    if msg.get("type") != "text":
                        continue  # Only handle text for now

                    phone = msg.get("from", "")
                    if self.allowed_numbers and phone not in self.allowed_numbers:
                        continue

                    # Find contact name
                    name = phone
                    for c in contacts:
                        if c.get("wa_id") == phone:
                            name = c.get("profile", {}).get("name", phone)
                            break

                    incoming = IncomingMessage(
                        text=msg.get("text", {}).get("body", ""),
                        user=User(Platform.WHATSAPP, phone, name),
                        channel=Channel(Platform.WHATSAPP, phone, is_dm=True),
                        platform=Platform.WHATSAPP,
                        thread=Thread(thread_id=phone),
                        platform_message_id=msg.get("id", ""),
                        raw_event=msg,
                    )
                    await self._queue.put(incoming)

        return web.Response(status=200)
