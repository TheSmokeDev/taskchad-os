"""Discord adapter using discord.py gateway."""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime
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
from attachment_context import is_supported_document_attachment
from voice_markers import parse_send_markers, strip_send_markers

# PRD-8 Phase 7b WS2 (codex post-build F2) — operator kill-switch handling.
# Module-attribute lookup (Rule 3); adapter catches KillSwitchDisabled
# BEFORE generic Exception so the refusal gets a friendly degraded reply.
from security import kill_switches as _kill_switches

# Audio MIME types Discord clients commonly send for voice messages /
# audio attachments (M4A from voice-message recorder, OGG/Opus from
# bots, WebM from web clients).
_DISCORD_AUDIO_MIMES: tuple[str, ...] = (
    "audio/ogg",
    "audio/mp4",
    "audio/mpeg",
    "audio/m4a",
    "audio/x-m4a",
    "audio/webm",
    "audio/wav",
    "audio/flac",
)


class DiscordAdapter:
    """Discord platform adapter using discord.py gateway.

    Connects via WebSocket gateway (no public URL needed for receiving).
    Handles DMs, @mentions, and button interactions in allowed guilds.
    """

    def __init__(
        self,
        bot_token: str,
        allowed_guilds: list[str],
        allowed_users: list[str],
        watched_channels: list[str] | None = None,
    ) -> None:
        import discord

        intents = discord.Intents.default()
        intents.message_content = True  # CRITICAL: required for message text
        intents.dm_messages = True

        self.bot_token = bot_token
        self.allowed_guilds = allowed_guilds
        self.allowed_users = allowed_users
        self._queue: asyncio.Queue[IncomingMessage] = asyncio.Queue()
        self._client = discord.Client(intents=intents)
        self._bot_user_id: int | None = None
        # Channels where bot listens without @mention
        # Reads from DISCORD_WATCHED_CHANNELS env var (comma-separated IDs)
        if watched_channels is None:
            env_val = os.getenv("DISCORD_WATCHED_CHANNELS", "")
            watched_channels = [c.strip() for c in env_val.split(",") if c.strip()] if env_val else []
        self._watched_channels: set[str] = set(watched_channels)

        # Register event handlers
        @self._client.event
        async def on_ready() -> None:
            self._bot_user_id = self._client.user.id
            print(f"[{datetime.now()}] Discord adapter connected ({self._client.user})")

        @self._client.event
        async def on_message(msg: Any) -> None:
            # Skip own messages
            if msg.author.id == self._bot_user_id:
                return
            # Auth check
            if not self._is_allowed(msg):
                return
            # Only process DMs, @mentions, or watched channels
            is_dm = isinstance(msg.channel, discord.DMChannel)
            is_watched = str(msg.channel.id) in self._watched_channels
            if not is_dm and not is_watched and self._client.user not in msg.mentions:
                return

            # Phase 4: voice ingress — transcribe audio attachments first.
            voice_text = await self._on_voice_message(msg)
            if voice_text:
                incoming = self._normalize_message(msg, is_dm, voice_text, [])
                await self._queue.put(incoming)
                return

            # Download image/document attachments to local disk.
            img_text, img_attachments = await self._download_image_attachments(msg)
            doc_text, doc_attachments = await self._download_document_attachments(msg)
            context_text = "\n".join(part for part in (img_text, doc_text) if part)
            attachments = [*img_attachments, *doc_attachments]
            incoming = self._normalize_message(msg, is_dm, context_text, attachments)
            await self._queue.put(incoming)

        @self._client.event
        async def on_interaction(interaction: discord.Interaction) -> None:
            """Handle button clicks and other component interactions."""
            if interaction.type != discord.InteractionType.component:
                return
            # Auth check
            if self.allowed_users and str(interaction.user.id) not in self.allowed_users:
                try:
                    await interaction.response.send_message(
                        "You don't have permission.", ephemeral=True
                    )
                except Exception:
                    pass
                return
            custom_id = interaction.data.get("custom_id", "")
            if not custom_id:
                return

            # Acknowledge within 3 seconds
            try:
                await interaction.response.defer()
            except Exception:
                pass

            # Disable buttons on original message (prevents double-click)
            try:
                if interaction.message:
                    disabled_view = discord.ui.View(timeout=1)
                    for comp in (interaction.message.components or []):
                        for child in comp.children:
                            btn = discord.ui.Button(
                                label=child.label,
                                style=child.style,
                                custom_id=child.custom_id,
                                disabled=True,
                            )
                            disabled_view.add_item(btn)
                    await interaction.message.edit(view=disabled_view)
            except Exception as e:
                print(f"[{datetime.now()}] Discord disable buttons failed: {e}")

            # Build channel info
            ch_id = str(interaction.channel_id)
            is_dm = interaction.guild_id is None
            channel = Channel(Platform.DISCORD, ch_id, is_dm=is_dm)

            # Route as IncomingMessage with __button: prefix
            incoming = IncomingMessage(
                text=f"__button:{custom_id}",
                user=User(
                    Platform.DISCORD,
                    str(interaction.user.id),
                    interaction.user.display_name,
                ),
                channel=channel,
                platform=Platform.DISCORD,
                thread=Thread(thread_id=ch_id),
                raw_event={
                    "interaction_id": str(interaction.id),
                    "interaction_type": "button",
                    "custom_id": custom_id,
                    "guild": str(interaction.guild_id or ""),
                },
            )
            await self._queue.put(incoming)

    @property
    def platform(self) -> Platform:
        return Platform.DISCORD

    async def connect(self) -> None:
        """Start the Discord gateway connection as a background task."""
        self._task = asyncio.create_task(self._client.start(self.bot_token))

    async def disconnect(self) -> None:
        """Close the Discord connection."""
        await self._client.close()
        if hasattr(self, "_task") and not self._task.done():
            self._task.cancel()

    async def listen(self) -> Any:
        """Yield incoming messages from the queue."""
        while True:
            message = await self._queue.get()
            yield message

    async def send(self, message: OutgoingMessage) -> str | None:
        """Send a message to a Discord channel, optionally with buttons and embeds.

        Phase 4: parses [SEND_FILE]/[SEND_PHOTO] markers from message.text and
        dispatches each as a discord.File attachment via channel.send(file=).
        Markers are stripped from the text reply before send.
        """
        channel = self._client.get_channel(int(message.channel.platform_id))
        if not channel:
            return None

        # Phase 4: marker dispatch BEFORE text send (so attachments arrive first).
        await self._dispatch_send_markers(channel, message.text)

        # Build View from components if present
        view = self._build_view(message.components) if message.components else None

        # Build embed if present
        embed = self._build_embed(message.embed) if message.embed else None

        text = strip_send_markers(message.text)
        if not text:
            # All-marker reply — nothing left to send as text.
            return None
        sent = None
        chunks = self._split_message(text, max_length=1900)
        for i, chunk in enumerate(chunks):
            kwargs: dict[str, Any] = {"content": chunk}
            # Attach view + embed only to the last chunk
            if i == len(chunks) - 1:
                if view:
                    kwargs["view"] = view
                if embed:
                    kwargs["embed"] = embed
            sent = await channel.send(**kwargs)
        return str(sent.id) if sent else None

    async def update(self, message: OutgoingMessage) -> str | None:
        """Edit an existing Discord message."""
        if not message.update_message_id:
            return
        channel = self._client.get_channel(int(message.channel.platform_id))
        if not channel:
            return
        try:
            msg = await channel.fetch_message(int(message.update_message_id))
            kwargs: dict[str, Any] = {"content": message.text[:2000]}
            if message.components:
                kwargs["view"] = self._build_view(message.components)
            await msg.edit(**kwargs)
            return message.update_message_id
        except Exception as e:
            print(f"[{datetime.now()}] Discord edit failed: {e}")
            return None

    async def send_typing(self, channel: Channel) -> None:
        """Send typing indicator."""
        ch = self._client.get_channel(int(channel.platform_id))
        if ch:
            await ch.typing()

    def _build_view(self, components: list) -> Any:
        """Build a discord.ui.View from components.

        Supports both:
        - MessageComponent objects (new style from router/extensions)
        - Raw dict format (old style: [{components: [{type, label, style, custom_id}]}])
        """
        import discord

        STYLE_MAP_STR = {
            "primary": discord.ButtonStyle.primary,
            "secondary": discord.ButtonStyle.secondary,
            "success": discord.ButtonStyle.success,
            "danger": discord.ButtonStyle.danger,
        }
        STYLE_MAP_INT = {
            1: discord.ButtonStyle.primary,
            2: discord.ButtonStyle.secondary,
            3: discord.ButtonStyle.success,
            4: discord.ButtonStyle.danger,
        }

        view = discord.ui.View(timeout=600)

        for item in components:
            # New style: MessageComponent dataclass
            if isinstance(item, MessageComponent):
                btn = discord.ui.Button(
                    label=item.label,
                    custom_id=item.custom_id,
                    style=STYLE_MAP_STR.get(item.style, discord.ButtonStyle.secondary),
                    disabled=item.disabled,
                )
                view.add_item(btn)
            # Old style: row dict with nested components array
            elif isinstance(item, dict):
                for comp in item.get("components", []):
                    if comp.get("type") == 2:  # Button
                        btn = discord.ui.Button(
                            label=comp.get("label", "Button"),
                            style=STYLE_MAP_INT.get(
                                comp.get("style", 2), discord.ButtonStyle.secondary
                            ),
                            custom_id=comp.get("custom_id"),
                        )
                        view.add_item(btn)

        return view

    def _build_embed(self, embed_data: Any) -> Any:
        """Convert a MessageEmbed to a discord.Embed."""
        import discord

        embed = discord.Embed(
            title=embed_data.title or discord.utils.MISSING,
            description=embed_data.description or discord.utils.MISSING,
            color=embed_data.color,
        )
        for f in embed_data.fields:
            embed.add_field(
                name=f.get("name", ""),
                value=f.get("value", ""),
                inline=f.get("inline", True),
            )
        if embed_data.footer:
            embed.set_footer(text=embed_data.footer)
        if embed_data.image_url:
            embed.set_image(url=embed_data.image_url)
        return embed

    def _is_allowed(self, msg: Any) -> bool:
        """Check guild and user allowlists."""
        import discord

        if self.allowed_users and str(msg.author.id) not in self.allowed_users:
            return False
        if not isinstance(msg.channel, discord.DMChannel):
            if self.allowed_guilds and str(msg.guild.id) not in self.allowed_guilds:
                return False
        return True

    def _normalize_message(
        self, msg: Any, is_dm: bool,
        img_text: str = "", img_attachments: list | None = None,
    ) -> IncomingMessage:
        """Convert Discord message to IncomingMessage."""
        text = msg.content
        # Strip bot mentions
        if self._bot_user_id:
            text = re.sub(rf"<@!?{self._bot_user_id}>\s*", "", text).strip()

        # Prepend image Read-tool instructions if images were downloaded
        if img_text:
            text = f"{img_text}\n\n{text}" if text else img_text

        user = User(Platform.DISCORD, str(msg.author.id), msg.author.display_name)
        channel = Channel(Platform.DISCORD, str(msg.channel.id), is_dm=is_dm)
        thread_id = (
            str(msg.thread.id)
            if hasattr(msg, "thread") and msg.thread
            else str(msg.channel.id)
        )
        thread = Thread(thread_id=thread_id)

        # Use pre-downloaded local attachments
        attachments = img_attachments if img_attachments is not None else []

        return IncomingMessage(
            text=text,
            user=user,
            channel=channel,
            platform=Platform.DISCORD,
            thread=thread,
            platform_message_id=str(msg.id),
            attachments=attachments,
            raw_event={
                "author": str(msg.author),
                "guild": str(getattr(msg.guild, "id", "")),
            },
        )

    async def _on_voice_message(self, msg: Any) -> str:
        """Phase 4: detect audio attachments, transcribe via voice cascade.

        Returns the transcript text if an audio attachment was present and
        transcribed, otherwise empty string.
        """
        if not msg.attachments:
            return ""
        for att in msg.attachments:
            ct = (att.content_type or "").lower()
            if not any(ct.startswith(prefix) for prefix in _DISCORD_AUDIO_MIMES):
                continue
            if att.size and att.size > 25 * 1024 * 1024:
                continue
            # Download to temp file
            import tempfile
            from pathlib import Path
            import httpx

            ext = Path(att.filename).suffix or ".ogg"
            fd, local_path = tempfile.mkstemp(suffix=ext, prefix="homie_discord_voice_")
            os.close(fd)
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.get(str(att.url))
                    resp.raise_for_status()
                    with open(local_path, "wb") as f:
                        f.write(resp.content)
                transcript = await voice_mod.transcribe_audio_file(local_path)
                return transcript.strip()
            except _kill_switches.KillSwitchDisabled as ks_exc:
                # PRD-8 Phase 7b WS2 (codex post-build F2) — explicit catch
                # before generic Exception so refusals get a degraded reply.
                print(f"[{datetime.now()}] Discord voice cascade refused: {ks_exc}")
                return ""
            except Exception as e:
                print(f"[{datetime.now()}] Discord voice transcribe failed: {e}")
                return ""
            finally:
                try:
                    os.unlink(local_path)
                except OSError:
                    pass
        return ""

    async def _send_voice_response(self, channel: Any, text: str) -> None:
        """Phase 4: synthesize text via voice cascade, send as audio attachment."""
        try:
            import discord  # type: ignore[import-not-found]
            from io import BytesIO

            audio = await voice_mod.synthesize(text)
            # discord.File accepts a file-like object
            buf = BytesIO(audio)
            file = discord.File(buf, filename="response.ogg")
            await channel.send(file=file)
        except _kill_switches.KillSwitchDisabled as ks_exc:
            # PRD-8 Phase 7b WS2 (codex post-build F2) — degraded text reply.
            print(f"[{datetime.now()}] Discord TTS refused by kill-switch: {ks_exc}")
            try:
                await channel.send(
                    content=(
                        f"[killswitch:{ks_exc.switch_name}] Voice synthesis disabled "
                        f"by operator. Falling back to text.\n\n{text[:1850]}"
                    )
                )
            except Exception as e2:
                print(f"[{datetime.now()}] Discord killswitch text fallback failed: {e2}")
        except Exception as e:
            print(f"[{datetime.now()}] Discord TTS failed, falling back to text: {e}")
            try:
                await channel.send(content=text[:1900])
            except Exception as e2:
                print(f"[{datetime.now()}] Discord text fallback failed: {e2}")

    async def _dispatch_send_markers(self, channel: Any, text: str) -> None:
        """Phase 4: parse [SEND_FILE]/[SEND_PHOTO] markers, send as files."""
        markers = parse_send_markers(text)
        if not markers:
            return
        try:
            import discord  # type: ignore[import-not-found]
        except ImportError:
            return
        for m in markers:
            try:
                # discord.File accepts a path string OR a URL via httpx fetch.
                if m.path.startswith(("http://", "https://")):
                    import httpx
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        resp = await client.get(m.path)
                        resp.raise_for_status()
                        from io import BytesIO
                        buf = BytesIO(resp.content)
                        from pathlib import Path as _P
                        fname = _P(m.path).name or "attachment"
                        file = discord.File(buf, filename=fname)
                else:
                    file = discord.File(m.path)
                await channel.send(file=file, content=m.caption or None)
            except Exception as e:
                print(f"[{datetime.now()}] Discord marker dispatch failed ({m.path}): {e}")

    async def _download_image_attachments(self, msg: Any) -> tuple[str, list]:
        """Download image attachments from Discord CDN to local temp files.

        Returns (text_injection, attachment_list).
        """
        if not msg.attachments:
            return "", []

        import tempfile
        from pathlib import Path
        import httpx

        tmp_dir = Path(tempfile.gettempdir()) / "thehomie_discord"
        tmp_dir.mkdir(exist_ok=True)

        text_parts: list[str] = []
        downloaded: list[Attachment] = []

        for att in msg.attachments:
            ct = att.content_type or ""
            if not ct.startswith("image/"):
                continue
            if att.size and att.size > 25 * 1024 * 1024:
                text_parts.append(f"[Skipped {att.filename}: exceeds 25MB]")
                continue

            ext = Path(att.filename).suffix or ".png"
            local_path = tmp_dir / f"{msg.id}_{att.id}{ext}"

            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.get(str(att.url))
                    resp.raise_for_status()
                    local_path.write_bytes(resp.content)

                text_parts.append(
                    f"[User uploaded image: {att.filename} — saved at {local_path}]\n"
                    f"Use the Read tool to view the image at the path above."
                )
                downloaded.append(Attachment(
                    filename=att.filename,
                    mimetype=ct,
                    url=str(local_path),
                    size_bytes=att.size,
                ))
                print(f"[{datetime.now()}] Discord image saved: {local_path} ({att.size} bytes)")
            except Exception as e:
                print(f"[{datetime.now()}] Discord image download failed ({att.filename}): {e}")
                text_parts.append(f"[Failed to download {att.filename}: {e}]")

        return "\n".join(text_parts), downloaded

    async def _download_document_attachments(self, msg: Any) -> tuple[str, list]:
        """Download supported document attachments for engine-side parsing.

        The returned user text intentionally does not include local filesystem
        paths. The local file path stays internal on Attachment.url so the
        engine can parse bounded document context before runtime dispatch.
        """
        if not msg.attachments:
            return "", []

        import tempfile
        from pathlib import Path
        import httpx

        tmp_dir = Path(tempfile.gettempdir()) / "thehomie_discord_documents"
        tmp_dir.mkdir(exist_ok=True)

        text_parts: list[str] = []
        downloaded: list[Attachment] = []

        for att in msg.attachments:
            ct = att.content_type or ""
            if not is_supported_document_attachment(att.filename, ct):
                continue
            if att.size and att.size > 8 * 1024 * 1024:
                text_parts.append(f"[Skipped document {att.filename}: exceeds 8MB parser limit]")
                continue

            safe_name = self._safe_attachment_filename(att.filename)
            ext = Path(safe_name).suffix or ".bin"
            local_path = tmp_dir / f"{msg.id}_{att.id}{ext}"

            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.get(str(att.url))
                    resp.raise_for_status()
                    local_path.write_bytes(resp.content)

                text_parts.append(
                    f"[User uploaded document: {safe_name}. "
                    "Bounded attachment context will be provided to the runtime.]"
                )
                downloaded.append(Attachment(
                    filename=safe_name,
                    mimetype=ct,
                    url=str(local_path),
                    size_bytes=att.size,
                ))
                print(
                    f"[{datetime.now()}] Discord document saved: "
                    f"{local_path} ({att.size or 0} bytes)"
                )
            except Exception as e:
                print(f"[{datetime.now()}] Discord document download failed ({att.filename}): {e}")
                text_parts.append(f"[Failed to download document {att.filename}: {type(e).__name__}]")

        return "\n".join(text_parts), downloaded

    @staticmethod
    def _safe_attachment_filename(filename: str) -> str:
        name = Path(filename or "attachment").name
        name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
        return name or "attachment"

    def _split_message(self, text: str, max_length: int = 1900) -> list[str]:
        """Split long messages for Discord's 2000 char limit."""
        if len(text) <= max_length:
            return [text]
        chunks: list[str] = []
        remaining = text
        while remaining:
            if len(remaining) <= max_length:
                chunks.append(remaining)
                break
            split_at = max_length
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
