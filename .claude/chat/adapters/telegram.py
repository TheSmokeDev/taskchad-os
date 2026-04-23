"""Telegram adapter using python-telegram-bot with long-polling."""

from __future__ import annotations

import asyncio
import tempfile
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
        """Send a message to Telegram. Returns message_id as string for updates."""
        chat_id = int(message.channel.platform_id)
        thread_id = message.thread.thread_id if message.thread else message.channel.platform_id

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

            # Decide voice reply strategy based on response complexity
            tier = self._classify_voice_tier(message.text)
            if tier == "voice_only":
                # Short & conversational — voice bubble, no text
                await self._send_voice_response(chat_id, message.text)
            elif tier == "voice_and_text":
                # Medium length — voice summary + full formatted text
                await self._send_voice_response(chat_id, message.text)
                text = self._format_for_telegram(message.text)
                await self._app.bot.send_message(
                    chat_id=chat_id, text=text,
                    parse_mode="HTML",
                )
            else:
                # Long/technical — text only, voice would be painful
                text = self._format_for_telegram(message.text)
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

        text = self._format_for_telegram(message.text)

        # Reply to specific message if in a thread
        reply_to = None
        if message.thread and message.thread.parent_message_id:
            try:
                reply_to = int(message.thread.parent_message_id)
            except (ValueError, TypeError):
                pass

        # Update existing message — only attempt edit if content fits in one message.
        # If too long, fall through to chunked new-message send to avoid silent truncation.
        if message.is_update and message.update_message_id and len(text) <= 4096:
            try:
                msg_id = int(message.update_message_id)
                await self._app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=text,
                    parse_mode="Markdown",
                )
                return message.update_message_id
            except Exception as e:
                err_msg = str(e)
                # "Message is not modified" is harmless — same content sent twice
                if "is not modified" in err_msg:
                    return message.update_message_id
                # Other edit failures — fall through to send new message
                print(f"[{datetime.now()}] Edit failed, sending new: {e}")

        # Send new message(s) — split if over 4096 chars
        chunks = self._split_message(text)
        first_id: str | None = None

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
                print(f"[{datetime.now()}] Markdown send failed, retrying plain: {e}")
                try:
                    sent = await self._app.bot.send_message(
                        chat_id=chat_id,
                        text=chunk,
                        reply_to_message_id=reply_to,
                        reply_markup=chunk_markup,
                    )
                    if first_id is None:
                        first_id = str(sent.message_id)
                except Exception as e2:
                    print(f"[{datetime.now()}] Send failed: {e2}")

        return first_id

    async def update(self, message: OutgoingMessage) -> None:
        """Edit an existing message."""
        await self.send(message)

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

        # Need API key for transcription
        if self._voice_providers.stt is None:
            await msg.reply_text(
                "Voice notes require an OpenAI API key for transcription. "
                "Set OPENAI_API_KEY in .env to enable."
            )
            return

        # Download voice file to memory
        try:
            voice_file = await self._app.bot.get_file(msg.voice.file_id)
            buf = BytesIO()
            await voice_file.download_to_memory(buf)
            audio_bytes = buf.getvalue()
        except Exception as e:
            print(f"[{datetime.now()}] Voice download failed: {e}")
            await msg.reply_text("Failed to download voice note.")
            return

        # Transcribe
        try:
            transcript = await self._voice_providers.stt.transcribe(audio_bytes)
        except Exception as e:
            print(f"[{datetime.now()}] Transcription failed: {e}")
            await msg.reply_text(f"Transcription failed: {e}")
            return

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
        """Handle document uploads. Currently a no-op — extensions can register handlers."""
        pass

    # ── Inline buttons ─────────────────────────────────────────────

    def _build_reply_markup(self, components: list[MessageComponent]) -> Any:
        """Build an InlineKeyboardMarkup from MessageComponent list.

        One button per row (matches the simple vertical stack Discord uses).
        Telegram's `callback_data` is capped at 64 bytes — longer custom_ids
        are hashed and the original is stored in `_callback_id_map` so taps
        can be resolved back to the real id without collision risk.
        """
        import hashlib
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        rows: list[list[InlineKeyboardButton]] = []
        for comp in components:
            cid_bytes = comp.custom_id.encode("utf-8")
            if len(cid_bytes) <= 64:
                callback_data = comp.custom_id
            else:
                digest = hashlib.sha1(cid_bytes).hexdigest()[:16]
                callback_data = f"h:{digest}"
                self._callback_id_map[callback_data] = comp.custom_id
            rows.append([InlineKeyboardButton(text=comp.label, callback_data=callback_data)])
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
        """Synthesize text to speech and send as a voice bubble."""
        text = self._clean_for_tts(text)

        try:
            audio = await self._voice_providers.tts.synthesize(text)
            buf = BytesIO(audio)
            buf.name = "response.ogg"
            await self._app.bot.send_voice(chat_id=chat_id, voice=buf)
        except Exception as e:
            print(f"[{datetime.now()}] TTS failed, falling back to text: {e}")
            # Fallback to text if TTS fails
            try:
                await self._app.bot.send_message(chat_id=chat_id, text=text)
            except Exception as e2:
                print(f"[{datetime.now()}] Text fallback also failed: {e2}")

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
