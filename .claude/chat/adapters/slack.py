"""Slack adapter using Bolt AsyncApp with Socket Mode."""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from datetime import datetime
from typing import Any

from models import Channel, IncomingMessage, OutgoingMessage, Platform, Thread, User

# Phase 4 (PRD-8) — voice cascade + marker dispatch.
import voice as voice_mod
from voice_markers import parse_send_markers, strip_send_markers

_SLACK_AUDIO_MIMES: tuple[str, ...] = (
    "audio/ogg",
    "audio/mp4",
    "audio/mpeg",
    "audio/m4a",
    "audio/x-m4a",
    "audio/webm",
    "audio/wav",
    "audio/flac",
)


class SlackAdapter:
    """Slack platform adapter using Bolt AsyncApp + Socket Mode.

    Connects via outbound WebSocket (no public URL needed). Handles
    @mentions in channels, direct messages, and thread replies to
    heartbeat notifications. Each Slack thread maps to a separate conversation.
    """

    def __init__(
        self,
        bot_token: str,
        app_token: str,
        allowed_users: list[str],
        session_store: Any | None = None,
    ) -> None:
        from slack_bolt.async_app import AsyncApp

        self.bot_token = bot_token
        self.app_token = app_token
        self.allowed_users = [u.strip() for u in allowed_users if u.strip()]
        self.session_store = session_store  # For heartbeat thread lookups
        self._queue: asyncio.Queue[IncomingMessage] = asyncio.Queue()
        self._bot_user_id: str | None = None

        # Create the Bolt async app
        self.app = AsyncApp(token=bot_token)

        # Register event handlers
        self.app.event("app_mention")(self._on_app_mention)
        self.app.event("message")(self._on_message)

        # Socket mode handler (created on connect)
        self._handler: Any = None

    @property
    def platform(self) -> Platform:
        return Platform.SLACK

    async def _get_bot_user_id(self) -> str:
        """Lazily fetch the bot's own user ID via auth.test()."""
        if self._bot_user_id is None:
            result = await self.app.client.auth_test()
            self._bot_user_id = result["user_id"]
        return self._bot_user_id

    async def connect(self) -> None:
        """Start the Socket Mode connection."""
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

        self._handler = AsyncSocketModeHandler(self.app, self.app_token)
        await self._handler.connect_async()
        bot_id = await self._get_bot_user_id()
        print(f"[{datetime.now()}] Slack adapter connected (bot user: {bot_id})")

    async def disconnect(self) -> None:
        """Close the Socket Mode connection."""
        if self._handler:
            await self._handler.close_async()
            print(f"[{datetime.now()}] Slack adapter disconnected")

    async def listen(self) -> Any:
        """Yield incoming messages from the queue (infinite loop)."""
        while True:
            message = await self._queue.get()
            yield message

    async def send(self, message: OutgoingMessage) -> str | None:
        """Send or update a message in Slack. Returns the message ts for updates.

        Phase 4: parses [SEND_FILE]/[SEND_PHOTO] markers and dispatches each
        via files_upload_v2 BEFORE the text reply. Markers are stripped from
        the text before send.
        """
        channel_id = message.channel.platform_id
        thread_ts = message.thread.thread_id if message.thread else None

        # Phase 4: marker dispatch (before text)
        await self._dispatch_send_markers(channel_id, message.text, thread_ts)

        text = self._markdown_to_mrkdwn(strip_send_markers(message.text))
        if not text:
            return None

        # Update an existing message
        if message.is_update and message.update_message_id:
            for chunk in self._split_message(text):
                try:
                    await self.app.client.chat_update(
                        channel=channel_id,
                        ts=message.update_message_id,
                        text=chunk,
                    )
                except Exception as e:
                    print(f"[{datetime.now()}] Error updating message: {e}")
            result_ts: str | None = message.update_message_id
            return result_ts

        # Send new message(s)
        chunks = self._split_message(text)
        first_ts: str | None = None
        for chunk in chunks:
            try:
                kwargs: dict[str, Any] = {"channel": channel_id, "text": chunk}
                if thread_ts:
                    kwargs["thread_ts"] = thread_ts
                result = await self.app.client.chat_postMessage(**kwargs)
                if first_ts is None:
                    first_ts = result["ts"]
            except Exception as e:
                print(f"[{datetime.now()}] Error sending message: {e}")
        return first_ts

    async def _dispatch_send_markers(
        self,
        channel_id: str,
        text: str,
        thread_ts: str | None = None,
    ) -> None:
        """Phase 4: parse markers and upload via files_upload_v2."""
        markers = parse_send_markers(text)
        if not markers:
            return
        for m in markers:
            try:
                if m.path.startswith(("http://", "https://")):
                    import httpx
                    from pathlib import Path as _P
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        resp = await client.get(m.path)
                        resp.raise_for_status()
                        fd, local_path = tempfile.mkstemp(
                            suffix=_P(m.path).suffix or ".bin",
                            prefix="homie_slack_marker_",
                        )
                        os.close(fd)
                        try:
                            with open(local_path, "wb") as fh:
                                fh.write(resp.content)
                            await self.app.client.files_upload_v2(
                                channel=channel_id,
                                file=local_path,
                                initial_comment=m.caption or "",
                                thread_ts=thread_ts,
                            )
                        finally:
                            try:
                                os.unlink(local_path)
                            except OSError:
                                pass
                else:
                    await self.app.client.files_upload_v2(
                        channel=channel_id,
                        file=m.path,
                        initial_comment=m.caption or "",
                        thread_ts=thread_ts,
                    )
            except Exception as e:
                print(f"[{datetime.now()}] Slack marker dispatch failed ({m.path}): {e}")

    async def _send_voice_response(
        self,
        channel_id: str,
        text: str,
        thread_ts: str | None = None,
    ) -> None:
        """Phase 4: synthesize text via voice cascade, upload audio via files_upload_v2."""
        try:
            audio = await voice_mod.synthesize(text)
            fd, local_path = tempfile.mkstemp(suffix=".ogg", prefix="homie_slack_tts_")
            os.close(fd)
            try:
                with open(local_path, "wb") as fh:
                    fh.write(audio)
                await self.app.client.files_upload_v2(
                    channel=channel_id,
                    file=local_path,
                    filename="response.ogg",
                    thread_ts=thread_ts,
                )
            finally:
                try:
                    os.unlink(local_path)
                except OSError:
                    pass
        except Exception as e:
            print(f"[{datetime.now()}] Slack TTS failed, falling back to text: {e}")
            try:
                await self.app.client.chat_postMessage(
                    channel=channel_id,
                    text=text,
                    thread_ts=thread_ts,
                )
            except Exception as e2:
                print(f"[{datetime.now()}] Slack text fallback failed: {e2}")

    async def update(self, message: OutgoingMessage) -> None:
        """Edit an existing message (convenience wrapper around send)."""
        await self.send(message)

    async def send_typing(self, channel: Channel) -> None:
        """No-op — Slack doesn't support outbound typing indicators for bots."""

    # ── Event Handlers ──────────────────────────────────────────────

    async def _on_app_mention(self, event: dict[str, Any], say: Any, client: Any) -> None:
        """Handle @bot mentions in channels."""
        user_id = event.get("user", "")
        if not self._is_allowed(user_id):
            return

        incoming = self._normalize_event(event, is_dm=False)
        await self._queue.put(incoming)

    async def _on_message(self, event: dict[str, Any], say: Any, client: Any) -> None:
        """Handle direct messages and thread replies to heartbeat notifications.

        Phase 4: detect audio file uploads and replace event text with the
        transcript before normalising. R4 NB2: uses canonical
        voice.transcribe_audio_file (NOT legacy 3-arg transcribe).
        """
        # Skip bot messages and subtypes — but keep "file_share" subtype so
        # voice notes (uploaded files) still flow through.
        if event.get("bot_id"):
            return
        subtype = event.get("subtype")
        if subtype and subtype not in ("file_share",):
            return

        user_id = event.get("user", "")
        if not self._is_allowed(user_id):
            return

        is_dm = event.get("channel_type") == "im"

        if not is_dm:
            # Channel message — only process if it's a thread reply to a heartbeat notification
            thread_ts = event.get("thread_ts")
            if not thread_ts:
                return  # Not a thread reply, ignore
            channel_id = event.get("channel", "")
            if not self._is_heartbeat_thread(channel_id, thread_ts):
                return  # Not a heartbeat thread, ignore

        # Phase 4 voice ingress — transcribe audio uploads.
        transcript = await self._transcribe_audio_files(event)
        if transcript:
            event = dict(event)
            event["text"] = transcript

        incoming = self._normalize_event(event, is_dm=is_dm)
        await self._queue.put(incoming)

    async def _transcribe_audio_files(self, event: dict[str, Any]) -> str:
        """Phase 4: detect audio file uploads on the event, transcribe via cascade.

        Returns transcript text if an audio file was found and transcribed;
        otherwise empty string. R4 NB2: uses canonical
        voice.transcribe_audio_file.
        """
        files = event.get("files") or []
        if not files:
            return ""
        for f in files:
            mime = (f.get("mimetype") or "").lower()
            if not any(mime.startswith(prefix) for prefix in _SLACK_AUDIO_MIMES):
                continue
            # Phase 4 post-build NM1: use files.info to fetch private URL
            # rather than reading url_private[_download] off the event payload.
            # The event payload omits these for some file types; files.info
            # is the canonical Slack API path. Bot token is already in scope.
            file_id = f.get("id")
            url = None
            if file_id:
                try:
                    info = await self.app.client.files_info(file=file_id)
                    info_file = info.get("file", {}) if isinstance(info, dict) else getattr(info, "data", {}).get("file", {})
                    url = info_file.get("url_private_download") or info_file.get("url_private")
                except Exception as e:
                    print(f"[{datetime.now()}] Slack files.info failed for {file_id}: {e}")
            # Fallback to event-payload URL if files.info is unavailable.
            if not url:
                url = f.get("url_private_download") or f.get("url_private")
            if not url:
                continue
            import httpx

            from pathlib import Path as _P
            ext = _P(f.get("name") or "voice.ogg").suffix or ".ogg"
            fd, local_path = tempfile.mkstemp(suffix=ext, prefix="homie_slack_voice_")
            os.close(fd)
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.get(
                        url,
                        headers={"Authorization": f"Bearer {self.bot_token}"},
                    )
                    resp.raise_for_status()
                    with open(local_path, "wb") as fh:
                        fh.write(resp.content)
                return (await voice_mod.transcribe_audio_file(local_path)).strip()
            except Exception as e:
                print(f"[{datetime.now()}] Slack voice transcribe failed: {e}")
                return ""
            finally:
                try:
                    os.unlink(local_path)
                except OSError:
                    pass
        return ""

    # ── Private Helpers ─────────────────────────────────────────────

    def _is_allowed(self, user_id: str) -> bool:
        """Check if a user is in the allowlist."""
        if not self.allowed_users:
            return True  # No allowlist = allow all
        return user_id in self.allowed_users

    def _is_heartbeat_thread(self, channel_id: str, thread_ts: str) -> bool:
        """Check if a thread_ts corresponds to a heartbeat notification."""
        if not self.session_store:
            return False
        try:
            return self.session_store.get_heartbeat_thread(channel_id, thread_ts) is not None
        except Exception:
            return False

    def _normalize_event(self, event: dict[str, Any], is_dm: bool) -> IncomingMessage:
        """Convert a Slack event into a platform-agnostic IncomingMessage."""
        user_id = event.get("user", "")
        channel_id = event.get("channel", "")
        text = event.get("text", "")
        ts = event.get("ts", "")

        # Always thread — use thread_ts if replying, otherwise start a new thread on ts
        thread_ts = event.get("thread_ts") or ts

        # Strip bot mentions from text
        text = re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()

        user = User(Platform.SLACK, user_id)
        channel = Channel(Platform.SLACK, channel_id, is_dm=is_dm)
        thread = Thread(thread_id=thread_ts)

        return IncomingMessage(
            text=text,
            user=user,
            channel=channel,
            platform=Platform.SLACK,
            thread=thread,
            platform_message_id=ts,
            raw_event=event,
        )

    def _markdown_to_mrkdwn(self, text: str) -> str:
        """Convert standard markdown to Slack's mrkdwn format.

        Key differences:
        - **bold** → *bold* (single asterisk)
        - [text](url) → <url|text>
        - ## Heading → *Heading* (bold, no heading support)
        - Code blocks and inline code are compatible as-is
        """
        # Protect code blocks from conversion
        code_blocks: list[str] = []

        def _save_code_block(match: re.Match[str]) -> str:
            code_blocks.append(match.group(0))
            return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

        # Save fenced code blocks
        result = re.sub(r"```[\s\S]*?```", _save_code_block, text)
        # Save inline code
        result = re.sub(r"`[^`]+`", _save_code_block, result)

        # Convert **bold** to *bold* (but not inside code)
        result = re.sub(r"\*\*(.+?)\*\*", r"*\1*", result)

        # Convert [text](url) to <url|text>
        result = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", result)

        # Convert headings to bold
        result = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", result, flags=re.MULTILINE)

        # Restore code blocks
        for i, block in enumerate(code_blocks):
            result = result.replace(f"\x00CODEBLOCK{i}\x00", block)

        return result

    def _split_message(self, text: str, max_length: int = 3900) -> list[str]:
        """Split long messages at natural boundaries.

        Respects code blocks — never splits inside a fenced block.
        """
        if len(text) <= max_length:
            return [text]

        chunks: list[str] = []
        remaining = text

        while remaining:
            if len(remaining) <= max_length:
                chunks.append(remaining)
                break

            # Find a good split point
            split_at = max_length

            # Don't split inside a code block
            open_fence = remaining[:split_at].rfind("```")
            if open_fence != -1:
                # Check if there's a closing fence after the open
                close_fence = remaining[open_fence + 3 : split_at].find("```")
                if close_fence == -1:
                    # Open code block — split before it
                    split_at = open_fence

            # Try to split at double newline
            double_nl = remaining[:split_at].rfind("\n\n")
            if double_nl > max_length // 2:
                split_at = double_nl + 2
            else:
                # Try single newline
                single_nl = remaining[:split_at].rfind("\n")
                if single_nl > max_length // 2:
                    split_at = single_nl + 1
                else:
                    # Try space
                    space = remaining[:split_at].rfind(" ")
                    if space > max_length // 2:
                        split_at = space + 1

            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:]

        return chunks
