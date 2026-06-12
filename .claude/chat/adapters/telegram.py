"""Telegram adapter using python-telegram-bot with long-polling."""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from models import (
    Attachment,
    Channel,
    IncomingMessage,
    MessageComponent,
    OutgoingMessage,
    Platform,
    Thread,
    User,
)

# Phase 4 (PRD-8) — voice cascade + marker dispatch.
import voice as voice_mod
from voice_markers import parse_send_markers, strip_send_markers

# PRD-8 Phase 7b WS2 (codex post-build F2) — operator kill-switch handling.
# Module-attribute lookup so monkeypatch propagates (Rule 3). Adapter catches
# KillSwitchDisabled before generic Exception so refusals get the friendly
# degraded reply, not a generic "Transcription failed: <ex>" message.
from security import kill_switches as _kill_switches


class TelegramDeliveryError(RuntimeError):
    """Raised when Telegram cannot deliver non-empty response text."""


@dataclass(frozen=True)
class _TelegramMediaRef:
    """A local file or remote URL that should be sent as Telegram media."""

    source: str
    mimetype: str | None = None
    filename: str | None = None


class TelegramAdapter:
    """Telegram platform adapter using python-telegram-bot.

    Connects via long-polling (no webhook/public URL needed). Handles
    DMs and group messages. Each Telegram chat is a conversation;
    reply-to creates threaded sessions.
    """

    def __init__(
        self,
        bot_token: str,
        allowed_user_ids: list[int],
        *,
        openai_api_key: str = "",
        voice_stt_model: str = "whisper-1",
        voice_tts_engine: str = "edge",
        voice_tts_voice_edge: str = "en-US-GuyNeural",
        voice_tts_voice_openai: str = "alloy",
    ) -> None:
        from telegram.ext import ApplicationBuilder

        self.bot_token = bot_token
        self.allowed_user_ids = allowed_user_ids
        self._queue: asyncio.Queue[IncomingMessage] = asyncio.Queue()
        self._app = ApplicationBuilder().token(bot_token).build()
        self._sent_messages: dict[str, int] = {}  # key -> message_id for updates
        self._bot_username: str | None = None
        self._pending_document_groups: dict[str, list[IncomingMessage]] = {}
        self._pending_document_tasks: dict[str, asyncio.Task[Any]] = {}
        self._document_group_delay_seconds = 1.0
        # Hashed callback_data → original custom_id. Telegram's callback_data
        # limit is 64 bytes; longer IDs are hashed and resolved on tap.
        self._callback_id_map: dict[str, str] = {}

        self.configure_voice(
            openai_api_key=openai_api_key,
            voice_stt_model=voice_stt_model,
            voice_tts_engine=voice_tts_engine,
            voice_tts_voice_edge=voice_tts_voice_edge,
            voice_tts_voice_openai=voice_tts_voice_openai,
        )
        self._voice_reply_threads: set[str] = set()

    def configure_voice(
        self,
        *,
        openai_api_key: str,
        voice_stt_model: str,
        voice_tts_engine: str,
        voice_tts_voice_edge: str,
        voice_tts_voice_openai: str,
    ) -> None:
        """Refresh voice provider selection without rebuilding the adapter."""

        import voice as voice_mod

        self._openai_api_key = openai_api_key
        self._voice_stt_model = voice_stt_model
        self._tts_engine = voice_tts_engine
        self._tts_voice_edge = voice_tts_voice_edge
        self._tts_voice_openai = voice_tts_voice_openai
        self._voice_providers = voice_mod.build_voice_provider_set(
            openai_api_key=openai_api_key,
            stt_model=voice_stt_model,
            tts_engine=voice_tts_engine,
            tts_voice_edge=voice_tts_voice_edge,
            tts_voice_openai=voice_tts_voice_openai,
        )

    @property
    def platform(self) -> Platform:
        return Platform.TELEGRAM

    async def connect(self) -> None:
        """Start polling for updates."""
        from commands import get_telegram_bot_commands
        from telegram import BotCommand
        from telegram.ext import CallbackQueryHandler, MessageHandler, filters

        # All text messages (including /commands) pass through — the router
        # decides what's a command vs. regular text. No more manual regex sync.
        self._app.add_handler(MessageHandler(filters.TEXT, self._on_message))

        # Register handler for voice messages
        self._app.add_handler(MessageHandler(filters.VOICE, self._on_voice))

        # Register handler for photo uploads (images for Claude to analyze)
        self._app.add_handler(MessageHandler(filters.PHOTO, self._on_photo))

        # Register handler for document uploads (xlsx cancellation reports)
        self._app.add_handler(MessageHandler(filters.Document.ALL, self._on_document))

        # Register handler for inline button taps
        self._app.add_handler(CallbackQueryHandler(self._on_callback))

        # Initialize and start polling
        await self._app.initialize()

        # Register slash commands with Telegram so users see a dropdown on "/"
        tg_commands = [BotCommand(name, desc) for name, desc in get_telegram_bot_commands()]
        await self._app.bot.set_my_commands(tg_commands)

        await self._app.start()
        await self._app.updater.start_polling(
            drop_pending_updates=False,
            error_callback=lambda e: print(f"[{datetime.now()}] [TG-POLL-ERR] {e}", flush=True),
        )

        # Get bot info
        bot = await self._app.bot.get_me()
        self._bot_username = bot.username
        print(f"[{datetime.now()}] Telegram adapter connected (bot: @{bot.username})")
        print(f"[{datetime.now()}] Registered {len(tg_commands)} slash commands with Telegram")

    async def disconnect(self) -> None:
        """Stop polling and shut down."""
        if self._app.updater and self._app.updater.running:
            await self._app.updater.stop()
        if self._app.running:
            await self._app.stop()
        await self._app.shutdown()
        print(f"[{datetime.now()}] Telegram adapter disconnected")

    async def listen(self) -> Any:
        """Yield incoming messages from the queue (infinite loop)."""
        while True:
            message = await self._queue.get()
            yield message

    async def send(self, message: OutgoingMessage) -> str | None:
        """Send a message to Telegram. Returns message_id as string for updates.

        Footer (gap-6 concept draft hint) is appended below the message
        body with a blank-line separator. Inline-keyboard buttons still
        ride on the last chunk so the footer sits above the buttons,
        matching the §I8 contract for Telegram.
        """
        chat_id = int(message.channel.platform_id)
        thread_id = message.thread.thread_id if message.thread else message.channel.platform_id
        body_text = message.text

        # Phase 4: parse [SEND_FILE]/[SEND_PHOTO] markers and dispatch via
        # bot.send_document / bot.send_photo (kind == 'document' | 'photo'
        # maps directly to Telegram's send method names — R1 M5).
        await self._dispatch_send_markers(chat_id, body_text)
        body_text = strip_send_markers(body_text)

        footer = getattr(message, "footer", None)
        if footer:
            body_text = f"{body_text}\n\n{footer}" if body_text else footer
        raw_text, directive_media = self._extract_media_directives(body_text)
        media_refs = self._collect_media_refs(message.attachments, directive_media)

        # Voice reply: when the engine's final response arrives for a voice thread,
        # delete the "Thinking..." placeholder and send a voice bubble instead.
        # Skip progress ticks ("Thinking...", "Working...") — only trigger on the final response.
        _is_progress_tick = message.text.startswith(("Thinking...", "Working..."))
        if message.is_update and thread_id in self._voice_reply_threads and not _is_progress_tick:
            self._voice_reply_threads.discard(thread_id)
            # Delete the placeholder message
            if message.update_message_id:
                try:
                    await self._app.bot.delete_message(
                        chat_id=chat_id,
                        message_id=int(message.update_message_id),
                    )
                except Exception as e:
                    print(f"[{datetime.now()}] Failed to delete placeholder: {e}")

            # Decide voice reply strategy based on marker-stripped body_text
            # (R-runtime-chat F1: must use marker-stripped body_text, NOT raw
            # message.text — otherwise TTS would speak [SEND_FILE:...] aloud
            # and text fallback would echo marker syntax in chat bubble).
            tier = self._classify_voice_tier(body_text)
            if tier == "voice_only":
                # Short & conversational — voice bubble, no text
                await self._send_voice_response(chat_id, body_text)
            elif tier == "voice_and_text":
                # Medium length — voice summary + full formatted text
                await self._send_voice_response(chat_id, body_text)
                text = self._format_for_telegram(body_text)
                await self._app.bot.send_message(
                    chat_id=chat_id, text=text,
                    parse_mode="HTML",
                )
            else:
                # Long/technical — text only, voice would be painful
                text = self._format_for_telegram(body_text)
                for chunk in self._split_message(text):
                    try:
                        await self._app.bot.send_message(
                            chat_id=chat_id, text=chunk,
                            parse_mode="Markdown",
                        )
                    except Exception as e:
                        print(f"[{datetime.now()}] Voice-thread text send failed: {e}")
                        try:
                            await self._app.bot.send_message(chat_id=chat_id, text=chunk)
                        except Exception as e2:
                            print(f"[{datetime.now()}] Voice-thread text fallback failed: {e2}")
            return None

        text = self._format_for_telegram(raw_text)

        # Reply to specific message if in a thread
        reply_to = None
        if message.thread and message.thread.parent_message_id:
            try:
                reply_to = int(message.thread.parent_message_id)
            except (ValueError, TypeError):
                pass

        # Update existing message — only attempt edit if content fits in one message.
        # If too long, fall through to chunked new-message send to avoid silent truncation.
        if (
            message.is_update
            and message.update_message_id
            and len(text) <= 4096
            and not media_refs
        ):
            try:
                msg_id = int(message.update_message_id)
                await self._app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=text,
                    parse_mode="Markdown",
                )
                print(
                    f"[{datetime.now()}] Telegram edit delivered "
                    f"message_id={message.update_message_id}",
                    flush=True,
                )
                return message.update_message_id
            except Exception as e:
                err_msg = str(e)
                # "Message is not modified" is harmless — same content sent twice
                if "is not modified" in err_msg:
                    return message.update_message_id
                # Other edit failures — fall through to send new message
                print(f"[{datetime.now()}] Telegram edit failed, sending new: {e}", flush=True)

        # Send media natively when the runtime provides attachments or a Hermes-style
        # MEDIA:/path/to/file.png directive. This avoids echoing local paths into chat.
        if media_refs:
            return await self._send_media_reply(
                chat_id=chat_id,
                text=text,
                media_refs=media_refs,
                reply_to=reply_to,
                components=message.components,
            )

        # Send new message(s) — split if over 4096 chars
        chunks = self._split_message(text)
        first_id: str | None = None
        failed_chunks = 0

        # Buttons ride on the LAST chunk so they sit under the final content
        # the user reads (matches Discord adapter behavior).
        reply_markup = self._build_reply_markup(message.components) if message.components else None

        for idx, chunk in enumerate(chunks):
            is_last = idx == len(chunks) - 1
            chunk_markup = reply_markup if is_last else None
            try:
                sent = await self._app.bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    reply_to_message_id=reply_to,
                    parse_mode="Markdown",
                    reply_markup=chunk_markup,
                )
                if first_id is None:
                    first_id = str(sent.message_id)
            except Exception as e:
                # Fallback: send without markdown if parsing fails
                print(f"[{datetime.now()}] Telegram Markdown send failed, retrying plain: {e}", flush=True)
                try:
                    sent = await self._app.bot.send_message(
                        chat_id=chat_id,
                        text=chunk,
                        reply_to_message_id=reply_to,
                        reply_markup=chunk_markup,
                    )
                    if first_id is None:
                        first_id = str(sent.message_id)
                    print(
                        f"[{datetime.now()}] Telegram plain send fallback succeeded "
                        f"message_id={sent.message_id}",
                        flush=True,
                    )
                except Exception as e2:
                    failed_chunks += 1
                    print(f"[{datetime.now()}] Telegram send failed after fallback: {e2}", flush=True)

        if failed_chunks:
            print(
                f"[{datetime.now()}] Telegram delivery failed: "
                f"{failed_chunks} text chunk(s) were not delivered",
                flush=True,
            )
            raise TelegramDeliveryError(
                f"Telegram failed to deliver {failed_chunks} text chunk(s)"
            )
        if text.strip() and first_id is None:
            print(
                f"[{datetime.now()}] Telegram delivery failed: "
                "non-empty text produced no message id",
                flush=True,
            )
            raise TelegramDeliveryError("Telegram returned no message id for non-empty text")

        return first_id

    async def _send_media_reply(
        self,
        *,
        chat_id: int,
        text: str,
        media_refs: list[_TelegramMediaRef],
        reply_to: int | None,
        components: list[MessageComponent],
    ) -> str | None:
        """Send one or more media refs, using text as a caption when practical."""

        first_id: str | None = None
        reply_markup = self._build_reply_markup(components) if components else None
        caption = text.strip()

        # Telegram photo/document captions are capped at 1024 chars. For longer
        # responses, send the text first and attach media afterward.
        if caption and len(caption) > 1024:
            for chunk in self._split_message(caption):
                try:
                    sent = await self._app.bot.send_message(
                        chat_id=chat_id,
                        text=chunk,
                        reply_to_message_id=reply_to,
                        parse_mode="Markdown",
                    )
                    if first_id is None:
                        first_id = str(sent.message_id)
                except Exception as e:
                    print(f"[{datetime.now()}] Media preface send failed: {e}")
                    sent = await self._app.bot.send_message(
                        chat_id=chat_id,
                        text=chunk,
                        reply_to_message_id=reply_to,
                    )
                    if first_id is None:
                        first_id = str(sent.message_id)
            caption = ""

        for idx, media in enumerate(media_refs):
            media_caption = caption if idx == 0 and caption else None
            media_markup = reply_markup if idx == len(media_refs) - 1 else None
            try:
                sent = await self._send_one_media(
                    chat_id=chat_id,
                    media=media,
                    caption=media_caption,
                    reply_to=reply_to if first_id is None else None,
                    reply_markup=media_markup,
                )
                if first_id is None:
                    first_id = str(sent.message_id)
            except Exception as e:
                print(f"[{datetime.now()}] Telegram media send failed: {e}")
                fallback = f"Could not send media: {media.filename or media.source}"
                sent = await self._app.bot.send_message(
                    chat_id=chat_id,
                    text=fallback,
                    reply_to_message_id=reply_to if first_id is None else None,
                    reply_markup=media_markup,
                )
                if first_id is None:
                    first_id = str(sent.message_id)

        return first_id

    async def _send_one_media(
        self,
        *,
        chat_id: int,
        media: _TelegramMediaRef,
        caption: str | None,
        reply_to: int | None,
        reply_markup: Any,
    ) -> Any:
        """Send a single media item as photo when image-like, else document."""

        source = media.source
        is_image = self._is_image_media(media)
        kwargs: dict[str, Any] = {
            "chat_id": chat_id,
            "caption": caption,
            "reply_to_message_id": reply_to,
            "reply_markup": reply_markup,
        }

        if self._is_remote_url(source):
            if is_image:
                return await self._app.bot.send_photo(photo=source, **kwargs)
            return await self._app.bot.send_document(document=source, **kwargs)

        path = Path(source).expanduser()
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"Local media file not found: {path}")

        with path.open("rb") as handle:
            if is_image:
                return await self._app.bot.send_photo(photo=handle, **kwargs)
            return await self._app.bot.send_document(document=handle, **kwargs)

    @staticmethod
    def _extract_media_directives(text: str) -> tuple[str, list[_TelegramMediaRef]]:
        """Parse Hermes-style MEDIA:/path directives out of assistant text."""

        media: list[_TelegramMediaRef] = []
        kept_lines: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            upper = stripped.upper()
            if upper.startswith("MEDIA:") or upper.startswith("IMAGE:"):
                _, value = stripped.split(":", 1)
                source = value.strip().strip("<>").strip()
                if source:
                    media.append(_TelegramMediaRef(source=source))
                continue
            kept_lines.append(line)
        return "\n".join(kept_lines).strip(), media

    @staticmethod
    def _collect_media_refs(
        attachments: list[Attachment],
        directive_media: list[_TelegramMediaRef],
    ) -> list[_TelegramMediaRef]:
        media_refs = list(directive_media)
        for attachment in attachments:
            if not attachment.url:
                continue
            media_refs.append(
                _TelegramMediaRef(
                    source=attachment.url,
                    mimetype=attachment.mimetype,
                    filename=attachment.filename,
                )
            )
        return media_refs

    @staticmethod
    def _is_remote_url(source: str) -> bool:
        return source.startswith(("http://", "https://"))

    @staticmethod
    def _is_image_media(media: _TelegramMediaRef) -> bool:
        if media.mimetype and media.mimetype.startswith("image/"):
            return True
        path = media.source.split("?", 1)[0].lower()
        return path.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))

    async def update(self, message: OutgoingMessage) -> str | None:
        """Edit an existing message."""
        return await self.send(message)

    async def send_typing(self, channel: Channel) -> None:
        """Send typing indicator."""
        try:
            chat_id = int(channel.platform_id)
            await self._app.bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass

    # ── Event Handler ──────────────────────────────────────────────

    async def _on_message(self, update: Any, context: Any) -> None:
        """Handle incoming text messages."""
        msg = update.message
        if not msg or not msg.text:
            return

        user_id = msg.from_user.id

        # Auth check
        if self.allowed_user_ids and user_id not in self.allowed_user_ids:
            await msg.reply_text("Not authorized.")
            return

        # Strip bot @mentions from group messages
        text = msg.text
        if self._bot_username:
            text = text.replace(f"@{self._bot_username}", "").strip()

        # Build thread ID — use reply_to_message for threading, else chat_id
        chat_id = str(msg.chat_id)
        thread_id = chat_id  # Default: whole chat is one conversation
        parent_msg_id = None

        if msg.reply_to_message:
            # Replying to a specific message creates a sub-thread
            thread_id = f"{chat_id}:{msg.reply_to_message.message_id}"
            parent_msg_id = str(msg.reply_to_message.message_id)

        user = User(
            platform=Platform.TELEGRAM,
            platform_id=str(user_id),
            display_name=msg.from_user.first_name,
        )
        channel = Channel(
            platform=Platform.TELEGRAM,
            platform_id=chat_id,
            is_dm=msg.chat.type == "private",
        )
        thread = Thread(
            thread_id=thread_id,
            parent_message_id=parent_msg_id,
        )

        incoming = IncomingMessage(
            text=text,
            user=user,
            channel=channel,
            platform=Platform.TELEGRAM,
            thread=thread,
            platform_message_id=str(msg.message_id),
            raw_event=msg.to_dict(),
        )

        await self._queue.put(incoming)

    async def _on_voice(self, update: Any, context: Any) -> None:
        """Handle incoming voice messages — transcribe and queue as text."""
        msg = update.message
        if not msg or not msg.voice:
            return

        user_id = msg.from_user.id

        # Auth check
        if self.allowed_user_ids and user_id not in self.allowed_user_ids:
            await msg.reply_text("Not authorized.")
            return

        # Phase 4: prefer the voice cascade if any provider is configured.
        # Falls back to legacy single-provider STT (preserves existing back-compat).
        capabilities = voice_mod.voice_capabilities()
        if not capabilities.get("stt") and self._voice_providers.stt is None:
            await msg.reply_text(
                "Voice notes require a configured STT provider. "
                "Set GROQ_API_KEY, OPENAI_API_KEY, MISTRAL_API_KEY, or "
                "WHISPER_MODEL_PATH in .env to enable."
            )
            return

        # Download voice file to disk so cascade providers can stream from it.
        local_path: str | None = None
        try:
            voice_file = await self._app.bot.get_file(msg.voice.file_id)
            fd, local_path = tempfile.mkstemp(suffix=".ogg", prefix="homie_tg_voice_")
            os.close(fd)
            await voice_file.download_to_drive(local_path)
        except Exception as e:
            print(f"[{datetime.now()}] Voice download failed: {e}")
            if local_path:
                try:
                    os.unlink(local_path)
                except OSError:
                    pass
            await msg.reply_text("Failed to download voice note.")
            return

        # Transcribe — prefer cascade, fall back to legacy single-provider.
        # PRD-8 Phase 7b WS2 (codex post-build F2): both paths route through
        # voice_mod entry points so the kill-switch ("voice") gates all
        # variants. The legacy fallback now goes through voice_mod.transcribe()
        # (which is gated at voice.py:585-589) instead of direct
        # _voice_providers.stt.transcribe(audio_bytes). Catches
        # KillSwitchDisabled BEFORE generic Exception so the operator
        # message is the documented degraded reply, not a generic error.
        transcript = ""
        try:
            if capabilities.get("stt"):
                transcript = await voice_mod.transcribe_audio_file(local_path)
            elif self._voice_providers.stt is not None:
                # Route through voice_mod.transcribe — which IS gated by the
                # voice kill-switch — instead of calling the provider directly
                # so HOMIE_KILLSWITCH_VOICE=disabled refuses cleanly.
                with open(local_path, "rb") as f:
                    audio_bytes = f.read()
                # Use the legacy entrypoint signature (bytes, key, model).
                # We pass an empty api_key — the gated cascade refuses BEFORE
                # the provider dispatch reads the key. If the kill-switch is
                # NOT disabled, the provider lookup happens in voice_mod and
                # uses the configured _voice_providers state via voice.py
                # internals. R4 NM4 closed this Telegram bypass.
                api_key = (
                    getattr(self._voice_providers.stt, "api_key", "")
                    if self._voice_providers.stt is not None
                    else ""
                )
                transcript = await voice_mod.transcribe(audio_bytes, api_key)
        except _kill_switches.KillSwitchDisabled as ks_exc:
            # Operator-toggleable refusal — friendly degraded message
            # (NOT a generic error). Refusal counter already incremented
            # inside voice_mod.requireEnabled() call.
            print(f"[{datetime.now()}] Voice cascade refused: {ks_exc}")
            await msg.reply_text(
                f"[killswitch:{ks_exc.switch_name}] Voice transcription is "
                f"disabled by the operator. To re-enable, unset "
                f"HOMIE_KILLSWITCH_{ks_exc.switch_name.upper()}."
            )
            return
        except Exception as e:
            print(f"[{datetime.now()}] Transcription failed: {e}")
            user_message = (
                e.user_message()
                if isinstance(e, getattr(voice_mod, "VoiceTranscriptionError", ()))
                else "Voice transcription failed. Check the bot logs for provider details."
            )
            await msg.reply_text(user_message)
            return
        finally:
            try:
                os.unlink(local_path)
            except OSError:
                pass

        if not transcript.strip():
            await msg.reply_text("Couldn't make out any speech in that voice note.")
            return

        # Build thread — same logic as _on_message
        chat_id = str(msg.chat_id)
        thread_id = chat_id
        parent_msg_id = None

        if msg.reply_to_message:
            thread_id = f"{chat_id}:{msg.reply_to_message.message_id}"
            parent_msg_id = str(msg.reply_to_message.message_id)

        user = User(
            platform=Platform.TELEGRAM,
            platform_id=str(user_id),
            display_name=msg.from_user.first_name,
        )
        channel = Channel(
            platform=Platform.TELEGRAM,
            platform_id=chat_id,
            is_dm=msg.chat.type == "private",
        )
        thread = Thread(thread_id=thread_id, parent_message_id=parent_msg_id)

        incoming = IncomingMessage(
            text=transcript,
            user=user,
            channel=channel,
            platform=Platform.TELEGRAM,
            thread=thread,
            platform_message_id=str(msg.message_id),
            raw_event=msg.to_dict(),
        )

        # Mark this thread for voice reply
        self._voice_reply_threads.add(thread_id)

        await self._queue.put(incoming)

    async def _on_document(self, update: Any, context: Any) -> None:
        """Handle incoming document uploads and queue them as attachments."""
        msg = update.message
        if not msg or not msg.document:
            return

        user_id = msg.from_user.id

        if self.allowed_user_ids and user_id not in self.allowed_user_ids:
            await msg.reply_text("Not authorized.")
            return

        document = msg.document
        filename = document.file_name or f"{document.file_unique_id or document.file_id}.bin"

        try:
            tg_file = await self._app.bot.get_file(document.file_id)
            tmp_dir = Path(tempfile.gettempdir()) / "thehomie_telegram_documents"
            tmp_dir.mkdir(exist_ok=True)
            unique_id = self._safe_document_filename(
                document.file_unique_id or str(msg.message_id)
            )
            file_path = tmp_dir / f"{unique_id}_{self._safe_document_filename(filename)}"
            await tg_file.download_to_drive(str(file_path))
            print(
                f"[{datetime.now()}] Document saved: {file_path} "
                f"({document.file_size or 0} bytes)",
                flush=True,
            )
        except Exception as e:
            print(f"[{datetime.now()}] Document download failed: {e}", flush=True)
            await msg.reply_text(f"Failed to download document: {e}")
            return

        caption = msg.caption or ""
        text = self._document_turn_text(
            filename=filename,
            file_path=str(file_path),
            mime_type=document.mime_type,
            file_size=document.file_size,
            caption=caption,
        )

        chat_id = str(msg.chat_id)
        thread_id = chat_id
        parent_msg_id = None

        if msg.reply_to_message:
            thread_id = f"{chat_id}:{msg.reply_to_message.message_id}"
            parent_msg_id = str(msg.reply_to_message.message_id)

        user = User(
            platform=Platform.TELEGRAM,
            platform_id=str(user_id),
            display_name=msg.from_user.first_name,
        )
        channel = Channel(
            platform=Platform.TELEGRAM,
            platform_id=chat_id,
            is_dm=msg.chat.type == "private",
        )
        thread = Thread(thread_id=thread_id, parent_message_id=parent_msg_id)

        incoming = IncomingMessage(
            text=text,
            user=user,
            channel=channel,
            platform=Platform.TELEGRAM,
            thread=thread,
            platform_message_id=str(msg.message_id),
            attachments=[
                Attachment(
                    filename=filename,
                    mimetype=document.mime_type,
                    url=str(file_path),
                    size_bytes=document.file_size,
                )
            ],
            raw_event=msg.to_dict(),
        )

        media_group_id = getattr(msg, "media_group_id", None)
        if media_group_id:
            group_key = f"{chat_id}:{media_group_id}"
            self._pending_document_groups.setdefault(group_key, []).append(incoming)
            task = self._pending_document_tasks.get(group_key)
            if task is None or task.done():
                self._pending_document_tasks[group_key] = asyncio.create_task(
                    self._flush_document_group_after_delay(group_key)
                )
            return

        await self._queue.put(incoming)

    @staticmethod
    def _document_turn_text(
        filename: str,
        file_path: str,
        mime_type: str | None,
        file_size: int | None,
        caption: str,
    ) -> str:
        """Build the user-turn text for a document upload.

        Lane-agnostic wording (Phase 2, doc-upload-truthful-reads): the
        document content is delivered to the model via the turn prompt on
        every lane, so the text must not instruct tool use that generic
        (no-tools) lanes cannot perform.
        """
        details = [
            f"[User uploaded a document: {filename}]",
            f"Saved at: {file_path}",
        ]
        if mime_type:
            details.append(f"MIME type: {mime_type}")
        if file_size is not None:
            details.append(f"Size: {file_size} bytes")
        details.append(
            "The document's content is provided to the model along with this "
            "message. If file tools are available, the full original is at the "
            "saved path; otherwise rely on the provided content. If the content "
            "is missing or partial, say so explicitly instead of guessing."
        )
        text = "\n".join(details)
        if caption:
            text += f"\n\nUser's message: {caption}"
        return text

    async def _flush_document_group_after_delay(self, group_key: str) -> None:
        try:
            await asyncio.sleep(self._document_group_delay_seconds)
            batch = self._pending_document_groups.pop(group_key, [])
            self._pending_document_tasks.pop(group_key, None)
            if not batch:
                return
            await self._queue.put(self._merge_document_group(batch, group_key))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(
                f"[{datetime.now()}] Document group flush failed for {group_key}: {e}",
                flush=True,
            )

    @staticmethod
    def _merge_document_group(
        batch: list[IncomingMessage],
        group_key: str,
    ) -> IncomingMessage:
        first = batch[0]
        attachments: list[Attachment] = []
        message_ids: list[str] = []
        raw_events: list[dict[str, Any]] = []
        parts = [
            f"[User uploaded {len(batch)} documents in one Telegram attachment group. "
            "Treat them as one user turn.]"
        ]
        for index, incoming in enumerate(batch, start=1):
            parts.append(f"Document {index}:\n{incoming.text}")
            attachments.extend(incoming.attachments)
            if incoming.platform_message_id:
                message_ids.append(incoming.platform_message_id)
            raw_events.append(incoming.raw_event)

        first.text = "\n\n".join(parts)
        first.attachments = attachments
        if message_ids:
            first.platform_message_id = ",".join(message_ids)
        first.raw_event = {
            "coalesced": True,
            "telegram_media_group": group_key,
            "events": raw_events,
        }
        return first

    @staticmethod
    def _safe_document_filename(filename: str) -> str:
        """Return a filesystem-safe filename segment for Telegram downloads."""
        import re

        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).name).strip("._")
        return safe[:120] or "document"

    # ── Inline buttons ─────────────────────────────────────────────

    def _build_reply_markup(self, components: list[MessageComponent]) -> Any:
        """Build an InlineKeyboardMarkup from MessageComponent list.

        Most controls render one button per row. Turn-control buttons render
        in one compact row so Queue/Steer are both visible while composing.
        Telegram's `callback_data` is capped at 64 bytes — longer custom_ids
        are hashed and the original is stored in `_callback_id_map` so taps
        can be resolved back to the real id without collision risk.
        """
        import hashlib
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        rows: list[list[InlineKeyboardButton]] = []
        turn_row: list[InlineKeyboardButton] = []
        for comp in components:
            cid_bytes = comp.custom_id.encode("utf-8")
            if len(cid_bytes) <= 64:
                callback_data = comp.custom_id
            else:
                digest = hashlib.sha1(cid_bytes).hexdigest()[:16]
                callback_data = f"h:{digest}"
                self._callback_id_map[callback_data] = comp.custom_id
            button = InlineKeyboardButton(text=comp.label, callback_data=callback_data)
            if comp.custom_id.startswith(("turn_queue:", "turn_steer:")):
                turn_row.append(button)
            else:
                rows.append([button])
        if turn_row:
            rows.append(turn_row)
        return InlineKeyboardMarkup(rows)

    async def _on_callback(self, update: Any, context: Any) -> None:
        """Handle inline button taps — ACK, disable buttons, queue as __button:."""
        query = update.callback_query
        if not query:
            return

        user_id = query.from_user.id
        if self.allowed_user_ids and user_id not in self.allowed_user_ids:
            try:
                await query.answer(text="Not authorized.", show_alert=True)
            except Exception:
                pass
            return

        # ACK within 3s to kill the loading spinner
        try:
            await query.answer()
        except Exception as e:
            print(f"[{datetime.now()}] Telegram callback ACK failed: {e}")

        # Resolve hashed callback_data back to the real custom_id
        raw = query.data or ""
        custom_id = self._callback_id_map.get(raw, raw)

        # Disable all buttons on the original message to prevent double-taps
        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            original = query.message
            if original and original.reply_markup:
                disabled_rows = []
                for row in original.reply_markup.inline_keyboard:
                    disabled_rows.append(
                        [
                            InlineKeyboardButton(
                                text=f"✓ {btn.text}" if btn.callback_data == raw else btn.text,
                                callback_data="__disabled__",
                            )
                            for btn in row
                        ]
                    )
                await original.edit_reply_markup(reply_markup=InlineKeyboardMarkup(disabled_rows))
        except Exception as e:
            # Non-fatal — double-taps will just route through again
            print(f"[{datetime.now()}] Telegram disable buttons failed: {e}")

        # Suppress taps on already-disabled buttons (defensive)
        if custom_id == "__disabled__":
            return

        # Route through the same __button: pipeline the router already handles
        chat_id = str(query.message.chat_id) if query.message else ""
        user = User(
            platform=Platform.TELEGRAM,
            platform_id=str(user_id),
            display_name=query.from_user.first_name,
        )
        channel = Channel(
            platform=Platform.TELEGRAM,
            platform_id=chat_id,
            is_dm=(query.message.chat.type == "private") if query.message else True,
        )
        thread = Thread(thread_id=chat_id)

        incoming = IncomingMessage(
            text=f"__button:{custom_id}",
            user=user,
            channel=channel,
            platform=Platform.TELEGRAM,
            thread=thread,
            raw_event={
                "interaction_type": "button",
                "custom_id": custom_id,
                "callback_data": raw,
            },
        )
        await self._queue.put(incoming)

    async def _on_photo(self, update: Any, context: Any) -> None:
        """Handle incoming photos — download and queue with image path for Claude."""
        msg = update.message
        if not msg or not msg.photo:
            return

        user_id = msg.from_user.id

        # Auth check
        if self.allowed_user_ids and user_id not in self.allowed_user_ids:
            await msg.reply_text("Not authorized.")
            return

        # Telegram sends multiple sizes — grab the largest (last in list)
        photo = msg.photo[-1]

        # Download to a temp file that persists for the session
        try:
            tg_file = await self._app.bot.get_file(photo.file_id)
            # Create a persistent temp file (not auto-deleted)
            tmp_dir = Path(tempfile.gettempdir()) / "thehomie_photos"
            tmp_dir.mkdir(exist_ok=True)
            file_path = tmp_dir / f"{photo.file_unique_id}.jpg"
            await tg_file.download_to_drive(str(file_path))
            print(f"[{datetime.now()}] Photo saved: {file_path} ({photo.width}x{photo.height})")
        except Exception as e:
            print(f"[{datetime.now()}] Photo download failed: {e}")
            await msg.reply_text(f"Failed to download photo: {e}")
            return

        # Use caption as text, or default prompt
        caption = msg.caption or ""
        text = (
            f"[User sent a photo: {file_path}]\n"
            f"Use the Read tool to view the image at the path above, then respond.\n"
        )
        if caption:
            text += f"\nUser's message: {caption}"

        # Build thread — same logic as _on_message
        chat_id_str = str(msg.chat_id)
        thread_id = chat_id_str
        parent_msg_id = None

        if msg.reply_to_message:
            thread_id = f"{chat_id_str}:{msg.reply_to_message.message_id}"
            parent_msg_id = str(msg.reply_to_message.message_id)

        user = User(
            platform=Platform.TELEGRAM,
            platform_id=str(user_id),
            display_name=msg.from_user.first_name,
        )
        channel = Channel(
            platform=Platform.TELEGRAM,
            platform_id=chat_id_str,
            is_dm=msg.chat.type == "private",
        )
        thread = Thread(thread_id=thread_id, parent_message_id=parent_msg_id)

        incoming = IncomingMessage(
            text=text,
            user=user,
            channel=channel,
            platform=Platform.TELEGRAM,
            thread=thread,
            platform_message_id=str(msg.message_id),
            attachments=[
                Attachment(
                    filename=file_path.name,
                    mimetype="image/jpeg",
                    url=str(file_path),
                    size_bytes=photo.file_size,
                )
            ],
            raw_event=msg.to_dict(),
        )

        await self._queue.put(incoming)

    # ── Voice reply strategy ────────────────────────────────────────

    # Thresholds (chars of cleaned text)
    _VOICE_ONLY_MAX = 300      # ~20s of speech
    _VOICE_AND_TEXT_MAX = 1500  # ~90s of speech

    @staticmethod
    def _classify_voice_tier(raw_text: str) -> str:
        """Classify response into voice reply tier.

        Returns: "voice_only", "voice_and_text", or "text_only"
        """
        import re
        has_code = bool(re.search(r"```", raw_text))
        has_table = bool(re.search(r"\|.*\|.*\|", raw_text))

        # Code blocks or tables → always text-only (voice can't convey these)
        if has_code or has_table:
            return "text_only"

        # Use cleaned length for thresholds (no markdown noise)
        cleaned = TelegramAdapter._clean_for_tts(raw_text)
        length = len(cleaned)

        if length <= TelegramAdapter._VOICE_ONLY_MAX:
            return "voice_only"
        elif length <= TelegramAdapter._VOICE_AND_TEXT_MAX:
            return "voice_and_text"
        else:
            return "text_only"

    @staticmethod
    def _clean_for_tts(text: str) -> str:
        """Strip markdown/code/noise so TTS reads clean natural language."""
        import re
        # Remove code blocks entirely (they sound terrible read aloud)
        text = re.sub(r"```[\s\S]*?```", "", text)
        # Remove inline code backticks
        text = re.sub(r"`([^`]+)`", r"\1", text)
        # Remove markdown bold/italic markers
        text = re.sub(r"\*{1,2}(.+?)\*{1,2}", r"\1", text)
        text = re.sub(r"_{1,2}(.+?)_{1,2}", r"\1", text)
        # Remove markdown headers
        text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
        # Remove markdown links — keep display text
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        # Remove bullet markers
        text = re.sub(r"^[\-\*]\s+", "", text, flags=re.MULTILINE)
        # Collapse multiple newlines
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    async def _send_voice_response(self, chat_id: int, text: str) -> None:
        """Synthesize text to speech and send as a voice bubble.

        R-runtime-chat F2: uses voice_mod.synthesize() cascade — full 9-provider
        cascade (ElevenLabs → Gradium → Mistral → Gemini → OpenAI → Kokoro →
        KittenTTS → Edge → macOS-say) with per-provider char-cap truncation,
        matching the other 5 adapters. Pre-Phase-4 path used legacy single-
        provider self._voice_providers.tts.synthesize() which never tried
        Mistral/Gemini/Kokoro/KittenTTS even when configured.
        """
        text = self._clean_for_tts(text)

        try:
            audio = await voice_mod.synthesize(text)
            buf = BytesIO(audio)
            buf.name = "response.ogg"
            await self._app.bot.send_voice(chat_id=chat_id, voice=buf)
        except _kill_switches.KillSwitchDisabled as ks_exc:
            # Operator-toggleable refusal — degrade to text-only reply with
            # explicit operator-facing context instead of silent text fallback.
            print(f"[{datetime.now()}] TTS refused by kill-switch: {ks_exc}")
            degraded_text = (
                f"[killswitch:{ks_exc.switch_name}] Voice synthesis is "
                f"disabled by the operator. Falling back to text.\n\n{text}"
            )
            try:
                await self._app.bot.send_message(chat_id=chat_id, text=degraded_text)
            except Exception as e2:
                print(f"[{datetime.now()}] TTS killswitch text fallback failed: {e2}")
        except Exception as e:
            print(f"[{datetime.now()}] TTS failed, falling back to text: {e}")
            # Fallback to text if TTS fails
            try:
                await self._app.bot.send_message(chat_id=chat_id, text=text)
            except Exception as e2:
                print(f"[{datetime.now()}] Text fallback also failed: {e2}")

    async def _dispatch_send_markers(self, chat_id: int, text: str) -> None:
        """Phase 4 (PRD-8): parse [SEND_FILE]/[SEND_PHOTO] markers, dispatch as media.

        kind == 'document' → bot.send_document; kind == 'photo' → bot.send_photo.
        URLs are passed through; local paths are opened and sent as InputFile.
        """
        markers = parse_send_markers(text)
        if not markers:
            return
        try:
            from telegram import InputFile  # type: ignore[import-not-found]
        except ImportError:
            return
        for m in markers:
            try:
                if m.path.startswith(("http://", "https://")):
                    payload: Any = m.path
                else:
                    payload = InputFile(open(m.path, "rb"))

                if m.kind == "photo":
                    await self._app.bot.send_photo(
                        chat_id=chat_id,
                        photo=payload,
                        caption=m.caption,
                    )
                else:
                    await self._app.bot.send_document(
                        chat_id=chat_id,
                        document=payload,
                        caption=m.caption,
                    )
            except Exception as e:
                print(f"[{datetime.now()}] Telegram marker dispatch failed ({m.path}): {e}")

    # ── Formatting ─────────────────────────────────────────────────

    def _format_for_telegram(self, text: str) -> str:
        """Light cleanup for Telegram's Markdown format.

        Telegram uses a simpler markdown: *bold*, _italic_, `code`, ```pre```.
        Standard **bold** needs to become *bold*.
        """
        import re

        # Convert **bold** to *bold*
        text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)

        return text

    def _split_message(self, text: str, max_length: int = 4000) -> list[str]:
        """Split messages to fit Telegram's 4096 char limit."""
        if len(text) <= max_length:
            return [text]

        chunks: list[str] = []
        remaining = text

        while remaining:
            if len(remaining) <= max_length:
                chunks.append(remaining)
                break

            split_at = max_length

            # Don't split inside code blocks
            open_fence = remaining[:split_at].rfind("```")
            if open_fence != -1:
                close_fence = remaining[open_fence + 3 : split_at].find("```")
                if close_fence == -1:
                    split_at = open_fence

            # Try natural boundaries
            double_nl = remaining[:split_at].rfind("\n\n")
            if double_nl > max_length // 2:
                split_at = double_nl + 2
            else:
                single_nl = remaining[:split_at].rfind("\n")
                if single_nl > max_length // 2:
                    split_at = single_nl + 1

            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:]

        return chunks
