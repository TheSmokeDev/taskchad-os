"""Web adapter for chat via the server relay WebSocket."""

from __future__ import annotations

import asyncio
import base64
import os
import tempfile
import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import TYPE_CHECKING, Any

from models import Channel, IncomingMessage, OutgoingMessage, Platform

# Phase 4 (PRD-8) — voice cascade + marker dispatch.
import voice as voice_mod
from voice_markers import parse_send_markers, strip_send_markers

# PRD-8 Phase 7b WS2 (codex post-build F2) — operator kill-switch handling.
from security import kill_switches as _kill_switches

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
        # Strong refs for fire-and-forget tasks (CPython only weak-refs
        # running tasks — unreferenced ones can be GC'd mid-await).
        self._bg_tasks: set[asyncio.Task] = set()
        # Liveness bookkeeping — read by the /health snapshot.
        self._last_update_at: float | None = None

    # Not a gateway the operator talks THROUGH — it is an outbound link to an
    # EXTERNAL service (the Mission Control relay). Its death is reported, never
    # restarted over: a bot restart cannot fix an MC outage, and RelayWSClient
    # already reconnects itself with backoff. Marking this critical would turn
    # every MC blip into a bot restart loop.
    liveness_critical = False

    @property
    def platform(self) -> Platform:
        return Platform.WEB

    async def connect(self) -> None:
        """No-op -- connection is managed by RelayWSClient."""
        print(f"[{datetime.now()}] Web adapter registered (relay-backed)")

    async def disconnect(self) -> None:
        """No-op -- disconnection is managed by RelayWSClient."""
        print(f"[{datetime.now()}] Web adapter disconnected")

    def liveness_ready(self) -> bool:
        """Whether the relay socket is actually up.

        This adapter's ``connect()`` is a no-op — the real connection lives in
        RelayWSClient — so it has no ``_connected_at`` of its own to stamp. The
        supervisor calls this instead to tell "still dialling" apart from "was
        connected and dropped".
        """
        return bool(self.ws_client is not None and self.ws_client.is_connected)

    async def probe_liveness(self) -> Any:
        """Prove the relay websocket is PHYSICALLY connected (Rule 2).

        Same blind spot as the other adapters: ``listen()`` here is an await on a
        queue that RelayWSClient fills. If the relay drops, the queue simply goes
        quiet — the adapter never notices, and before this probe /health happily
        reported ``web: true`` off registration presence alone. Dashboard chat
        would be dead with nothing saying so.
        """
        from liveness import ProbeResult

        client = self.ws_client
        if client is None:
            return ProbeResult(False, "no relay client attached")
        if not client.is_connected:
            return ProbeResult(False, "relay websocket not connected")
        return ProbeResult(True, f"relay connected to {client.relay_url}")

    async def reconnect(self) -> None:
        """Nothing to do — and saying so honestly matters.

        RelayWSClient.connect_forever() is an infinite retry loop with backoff
        that never raises, so the socket is already being redialled continuously.
        A "reconnect" here would be theatre. The supervisor re-probes after this
        returns and keeps reporting the adapter down until the relay is genuinely
        back, which is the truthful outcome.
        """
        print(
            f"[{datetime.now()}] Web adapter: relay reconnect is owned by "
            f"RelayWSClient (auto-retrying) — nothing to restart here"
        )

    def _enqueue(self, message: IncomingMessage) -> None:
        """Queue an inbound relay message and stamp the last-update clock.

        Forensics only — a quiet relay is not a dead relay; probe_liveness()
        decides that.
        """
        self._last_update_at = time.time()
        self._queue.put_nowait(message)

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
        except _kill_switches.KillSwitchDisabled as ks_exc:
            # PRD-8 Phase 7b WS2 (codex post-build F2) — operator kill-switch
            # refusal. Send degraded text frame instead of generic error.
            print(f"[{datetime.now()}] Web TTS refused by kill-switch: {ks_exc}")
            try:
                await self.ws_client.send_response(
                    request_id=request_id,
                    text=(
                        f"[killswitch:{ks_exc.switch_name}] Voice synthesis "
                        f"disabled by operator. Falling back to text.\n\n{text}"
                    ),
                    is_update=False,
                    is_done=False,
                )
            except Exception as e2:
                print(f"[{datetime.now()}] Web killswitch text fallback failed: {e2}")
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
        except _kill_switches.KillSwitchDisabled as ks_exc:
            # PRD-8 Phase 7b WS2 (codex post-build F2) — refusal returns empty
            # transcript; relay caller treats empty transcript as "no speech".
            print(f"[{datetime.now()}] Web voice cascade refused: {ks_exc}")
            return ""
        finally:
            try:
                os.unlink(local_path)
            except OSError:
                pass

    async def update(self, message: OutgoingMessage) -> str | None:
        """Edit/update an existing message -- same as send for relay."""
        return await self.send(message)

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
            self._enqueue(message)
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
                    self._enqueue(placeholder)
                except Exception as e:
                    print(f"[{datetime.now()}] Web binary ingress failed: {e}")

            _task = asyncio.create_task(_transcribe_and_enqueue())
            self._bg_tasks.add(_task)
            _task.add_done_callback(self._bg_tasks.discard)
            return

        if text is not None:
            placeholder = IncomingMessage(
                text=text,
                user=__import__("models").User(Platform.WEB, "web", "web"),
                channel=Channel(Platform.WEB, "web", is_dm=True),
                platform=Platform.WEB,
            )
            self._enqueue(placeholder)
