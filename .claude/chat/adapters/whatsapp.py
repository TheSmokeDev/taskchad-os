"""WhatsApp adapter using Meta Cloud API."""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime
from typing import Any

from models import Channel, IncomingMessage, OutgoingMessage, Platform, Thread, User

# Phase 4 (PRD-8) — voice cascade + marker dispatch.
import voice as voice_mod
from voice_markers import parse_send_markers, strip_send_markers

# PRD-8 Phase 7b WS2 (codex post-build F2) — operator kill-switch handling.
from security import kill_switches as _kill_switches


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
        """Send a text message via WhatsApp Cloud API.

        Phase 4: parses [SEND_FILE]/[SEND_PHOTO] markers and dispatches each
        via Cloud-API media upload + send. Markers stripped from the text reply.
        """
        import httpx

        recipient = message.channel.platform_id  # Phone number

        # Phase 4: marker dispatch (before text)
        await self._dispatch_send_markers(recipient, message.text)

        text_body = strip_send_markers(message.text)
        if not text_body:
            return None

        url = f"{self.GRAPH_API_BASE}/{self.phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient,
            "type": "text",
            "text": {"body": text_body[:4096]},  # WA limit
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

    async def _download_media(self, media_id: str, suffix: str = ".ogg") -> str | None:
        """Phase 4: WhatsApp Cloud-API media-receive.

        GET /v21.0/{media_id} → fetch URL → bearer-token GET → write to temp file.
        Returns local path on success, None on failure.
        """
        import httpx

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Step 1: get media URL
                meta_resp = await client.get(
                    f"{self.GRAPH_API_BASE}/{media_id}",
                    headers={"Authorization": f"Bearer {self.access_token}"},
                )
                meta_resp.raise_for_status()
                media_url = meta_resp.json().get("url")
                if not media_url:
                    return None

                # Step 2: download bytes
                file_resp = await client.get(
                    media_url,
                    headers={"Authorization": f"Bearer {self.access_token}"},
                )
                file_resp.raise_for_status()
                fd, local_path = tempfile.mkstemp(suffix=suffix, prefix="homie_wa_voice_")
                os.close(fd)
                with open(local_path, "wb") as f:
                    f.write(file_resp.content)
                return local_path
        except Exception as e:
            print(f"[{datetime.now()}] WhatsApp media download failed: {e}")
            return None

    async def _upload_media(self, file_path: str, mime: str) -> str | None:
        """Phase 4: WhatsApp Cloud-API media-send (upload step).

        POST /{phone_id}/media → returns media_id. Returns None on failure.
        """
        import httpx

        url = f"{self.GRAPH_API_BASE}/{self.phone_number_id}/media"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                with open(file_path, "rb") as f:
                    files = {
                        "file": (os.path.basename(file_path), f, mime),
                        "type": (None, mime),
                        "messaging_product": (None, "whatsapp"),
                    }
                    resp = await client.post(
                        url,
                        headers={"Authorization": f"Bearer {self.access_token}"},
                        files=files,
                    )
                    resp.raise_for_status()
                    return resp.json().get("id")
        except Exception as e:
            print(f"[{datetime.now()}] WhatsApp media upload failed: {e}")
            return None

    async def _send_audio_message(self, recipient: str, media_id: str) -> str | None:
        """Phase 4: send audio message after media upload."""
        import httpx

        url = f"{self.GRAPH_API_BASE}/{self.phone_number_id}/messages"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.access_token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "messaging_product": "whatsapp",
                        "to": recipient,
                        "type": "audio",
                        "audio": {"id": media_id},
                    },
                )
                if resp.status_code == 200:
                    return resp.json().get("messages", [{}])[0].get("id")
                print(
                    f"[{datetime.now()}] WhatsApp audio send failed: "
                    f"{resp.status_code} {resp.text}"
                )
                return None
        except Exception as e:
            print(f"[{datetime.now()}] WhatsApp audio send failed: {e}")
            return None

    async def _send_voice_response(self, recipient: str, text: str) -> None:
        """Phase 4: synthesize text via voice cascade, upload + send as audio."""
        try:
            audio = await voice_mod.synthesize(text)
            fd, local_path = tempfile.mkstemp(suffix=".ogg", prefix="homie_wa_tts_")
            os.close(fd)
            try:
                with open(local_path, "wb") as f:
                    f.write(audio)
                media_id = await self._upload_media(local_path, "audio/ogg")
                if media_id:
                    await self._send_audio_message(recipient, media_id)
            finally:
                try:
                    os.unlink(local_path)
                except OSError:
                    pass
        except _kill_switches.KillSwitchDisabled as ks_exc:
            # PRD-8 Phase 7b WS2 (codex post-build F2) — operator kill-switch
            # refusal. Send degraded text reply with operator-facing context.
            print(f"[{datetime.now()}] WhatsApp TTS refused by kill-switch: {ks_exc}")
            try:
                await self._send_text_message(
                    recipient,
                    (
                        f"[killswitch:{ks_exc.switch_name}] Voice synthesis "
                        f"disabled by operator. Falling back to text.\n\n{text}"
                    ),
                )
            except Exception as e2:
                print(f"[{datetime.now()}] WhatsApp killswitch text fallback failed: {e2}")
        except Exception as e:
            print(f"[{datetime.now()}] WhatsApp TTS failed: {e}")

    async def _dispatch_send_markers(self, recipient: str, text: str) -> None:
        """Phase 4: parse markers and dispatch via WhatsApp Cloud-API media upload+send."""
        markers = parse_send_markers(text)
        if not markers:
            return
        import httpx
        from pathlib import Path as _P

        for m in markers:
            local_path: str | None = None
            cleanup_path: str | None = None
            try:
                if m.path.startswith(("http://", "https://")):
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        resp = await client.get(m.path)
                        resp.raise_for_status()
                        fd, dl_path = tempfile.mkstemp(
                            suffix=_P(m.path).suffix or ".bin",
                            prefix="homie_wa_marker_",
                        )
                        os.close(fd)
                        with open(dl_path, "wb") as fh:
                            fh.write(resp.content)
                        local_path = dl_path
                        cleanup_path = dl_path
                else:
                    local_path = m.path

                # WhatsApp media types: image vs document/audio
                msg_type = "image" if m.kind == "photo" else "document"
                mime = "image/png" if m.kind == "photo" else "application/octet-stream"
                media_id = await self._upload_media(local_path, mime)
                if not media_id:
                    continue
                url = f"{self.GRAPH_API_BASE}/{self.phone_number_id}/messages"
                payload: dict[str, Any] = {
                    "messaging_product": "whatsapp",
                    "to": recipient,
                    "type": msg_type,
                    msg_type: {"id": media_id},
                }
                if m.caption:
                    payload[msg_type]["caption"] = m.caption
                async with httpx.AsyncClient(timeout=30.0) as client:
                    await client.post(
                        url,
                        headers={
                            "Authorization": f"Bearer {self.access_token}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
            except Exception as e:
                print(f"[{datetime.now()}] WhatsApp marker dispatch failed ({m.path}): {e}")
            finally:
                if cleanup_path:
                    try:
                        os.unlink(cleanup_path)
                    except OSError:
                        pass

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
                    msg_type = msg.get("type")
                    phone = msg.get("from", "")
                    if self.allowed_numbers and phone not in self.allowed_numbers:
                        continue

                    # Phase 4: voice ingress — Cloud-API media-receive path.
                    text_payload = ""
                    if msg_type == "text":
                        text_payload = msg.get("text", {}).get("body", "")
                    elif msg_type in ("audio", "voice"):
                        media_obj = msg.get(msg_type, {}) or msg.get("audio", {})
                        media_id = media_obj.get("id")
                        if not media_id:
                            continue
                        local_path = await self._download_media(media_id)
                        if not local_path:
                            continue
                        try:
                            text_payload = (
                                await voice_mod.transcribe_audio_file(local_path)
                            ).strip()
                        except _kill_switches.KillSwitchDisabled as ks_exc:
                            # PRD-8 Phase 7b WS2 (codex post-build F2) — explicit catch.
                            print(
                                f"[{datetime.now()}] WhatsApp voice cascade refused: "
                                f"{ks_exc}"
                            )
                            continue
                        except Exception as e:
                            print(
                                f"[{datetime.now()}] WhatsApp voice transcribe failed: {e}"
                            )
                            continue
                        finally:
                            try:
                                os.unlink(local_path)
                            except OSError:
                                pass
                        if not text_payload:
                            continue
                    else:
                        continue  # Other media types not yet handled

                    # Find contact name
                    name = phone
                    for c in contacts:
                        if c.get("wa_id") == phone:
                            name = c.get("profile", {}).get("name", phone)
                            break

                    incoming = IncomingMessage(
                        text=text_payload,
                        user=User(Platform.WHATSAPP, phone, name),
                        channel=Channel(Platform.WHATSAPP, phone, is_dm=True),
                        platform=Platform.WHATSAPP,
                        thread=Thread(thread_id=phone),
                        platform_message_id=msg.get("id", ""),
                        raw_event=msg,
                    )
                    await self._queue.put(incoming)

        return web.Response(status=200)
