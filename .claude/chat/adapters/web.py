"""Web adapter for chat via the server relay WebSocket."""

from __future__ import annotations

import asyncio
import base64
import os
import tempfile
from collections.abc import AsyncIterator
from datetime import datetime
from typing import TYPE_CHECKING

from models import Channel, IncomingMessage, OutgoingMessage, Platform

# Phase 4 (PRD-8) — voice cascade + marker dispatch.
import voice as voice_mod
from voice_markers import parse_send_markers, strip_send_markers

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

        Footer (gap-6 concept draft hint) is appended with a "\\n\\n--\\n"
        separator per the §I8 contract. HTML styling is deferred — the
        relay-rendered web client treats this as plain text.

        Phase 4: parses [SEND_FILE]/[SEND_PHOTO] markers and dispatches as
        binary frames over the WS pipe.
        """
        request_id = ""
        if message.thread:
            request_id = message.thread.parent_message_id or message.thread.thread_id or ""

        # Phase 4: marker dispatch (before text)
        await self._dispatch_send_markers(request_id, message.text)

        text = strip_send_markers(message.text)
        footer = getattr(message, "footer", None)
        if footer:
            text = f"{text}\n\n--\n{footer}"

        if not text:
            return request_id or None

        await self.ws_client.send_response(
            request_id=request_id,
            text=text,
            is_update=message.is_update,
            is_done=False,
        )
        return request_id or None  # Activates placeholder/update path in _handle_inner

    async def _dispatch_send_markers(self, request_id: str, text: str) -> None:
        """Phase 4: parse markers and emit binary frames on the WS pipe.

        Each marker becomes a {kind: 'audio'|'file', data: <b64>, mime: ...}
        frame. The web client decodes the base64 and renders the media
        appropriately.
        """
        markers = parse_send_markers(text)
        if not markers:
            return
        import httpx
        from pathlib import Path as _P

        for m in markers:
            try:
                if m.path.startswith(("http://", "https://")):
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        resp = await client.get(m.path)
                        resp.raise_for_status()
                        content = resp.content
                else:
                    with open(m.path, "rb") as f:
                        content = f.read()

                mime = (
                    "image/png" if m.kind == "photo"
                    else "application/octet-stream"
                )
                frame = {
                    "kind": "file",
                    "data": base64.b64encode(content).decode("ascii"),
                    "mime": mime,
                    "filename": _P(m.path).name,
                    "caption": m.caption,
                }
                # Reuse send_response with JSON-encoded frame text.
                import json as _json
                await self.ws_client.send_response(
                    request_id=request_id,
                    text=_json.dumps({"binary_frame": frame}),
                    is_update=False,
                    is_done=False,
                )
            except Exception as e:
                print(f"[{datetime.now()}] Web marker dispatch failed ({m.path}): {e}")

    async def _send_voice_response(self, request_id: str, text: str) -> None:
        """Phase 4: synthesize text via voice cascade, emit as audio frame."""
        try:
            audio = await voice_mod.synthesize(text)
            import json as _json

            frame = {
                "kind": "audio",
                "data": base64.b64encode(audio).decode("ascii"),
                "mime": "audio/opus",
            }
            await self.ws_client.send_response(
                request_id=request_id,
                text=_json.dumps({"binary_frame": frame}),
                is_update=False,
                is_done=False,
            )
        except Exception as e:
            print(f"[{datetime.now()}] Web TTS failed: {e}")

    async def transcribe_audio_blob(
        self,
        audio_bytes: bytes,
        audio_mime: str = "audio/opus",
    ) -> str:
        """Phase 4: helper for binary-blob ingress.

        Persists audio to temp file, calls cascade, returns transcript.
        Caller (RelayWSClient) invokes this when an audio frame is received
        and uses the returned text in an IncomingMessage via enqueue().
        """
        suffix_map = {
            "audio/opus": ".ogg",
            "audio/ogg": ".ogg",
            "audio/mp4": ".m4a",
            "audio/m4a": ".m4a",
            "audio/wav": ".wav",
            "audio/webm": ".webm",
        }
        suffix = suffix_map.get(audio_mime.lower(), ".ogg")
        fd, local_path = tempfile.mkstemp(suffix=suffix, prefix="homie_web_voice_")
        os.close(fd)
        try:
            with open(local_path, "wb") as f:
                f.write(audio_bytes)
            return (await voice_mod.transcribe_audio_file(local_path)).strip()
        finally:
            try:
                os.unlink(local_path)
            except OSError:
                pass

    async def update(self, message: OutgoingMessage) -> None:
        """Edit/update an existing message -- same as send for relay."""
        await self.send(message)

    async def send_typing(self, channel: Channel) -> None:
        """No-op -- typing indicators not supported via relay."""
        pass

    def enqueue(
        self,
        message: IncomingMessage | None = None,
        *,
        text: str | None = None,
        audio_bytes: bytes | None = None,
        audio_mime: str | None = None,
    ) -> None:
        """Push an incoming message into the listen queue.

        Phase 4: extended to accept binary-blob audio ingress. When
        audio_bytes is provided, the helper transcribes via voice cascade
        and constructs a text-only IncomingMessage; the caller still passes
        a fully-formed message via the positional `message` arg for
        text-only requests (legacy path preserved).
        """
        if message is not None:
            self._queue.put_nowait(message)
            return

        if audio_bytes is not None:
            # Schedule transcription on the running loop; callers providing
            # binary blobs typically already hold the loop. Errors are
            # swallowed and logged so the WS pipeline doesn't crash.
            async def _transcribe_and_enqueue() -> None:
                try:
                    transcript = await self.transcribe_audio_blob(
                        audio_bytes,
                        audio_mime=audio_mime or "audio/opus",
                    )
                    if not transcript:
                        return
                    # Bare-bones placeholder IncomingMessage. Real callers
                    # should construct their own with full user/channel.
                    placeholder = IncomingMessage(
                        text=transcript,
                        user=__import__("models").User(Platform.WEB, "web", "web"),
                        channel=Channel(Platform.WEB, "web", is_dm=True),
                        platform=Platform.WEB,
                    )
                    self._queue.put_nowait(placeholder)
                except Exception as e:
                    print(f"[{datetime.now()}] Web binary ingress failed: {e}")

            asyncio.create_task(_transcribe_and_enqueue())
            return

        if text is not None:
            placeholder = IncomingMessage(
                text=text,
                user=__import__("models").User(Platform.WEB, "web", "web"),
                channel=Channel(Platform.WEB, "web", is_dm=True),
                platform=Platform.WEB,
            )
            self._queue.put_nowait(placeholder)
