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


def _discord_sync_timeout_seconds() -> float:
    try:
        return max(1.0, float(os.getenv("DISCORD_SLASH_SYNC_TIMEOUT_SECONDS", "20")))
    except (TypeError, ValueError):
        return 20.0


def get_discord_native_command_menu() -> list[tuple[str, str]]:
    """Return Discord-native slash commands from the curated chat menu."""

    from commands import get_telegram_command_menu

    menu, _hidden_count = get_telegram_command_menu()
    return [
        (name, _discord_description(desc))
        for name, desc in menu
        if name != "vault"
    ]


def _discord_description(description: str) -> str:
    """Discord application command descriptions are capped at 100 chars."""

    clean = " ".join(str(description or "").split())
    if len(clean) <= 100:
        return clean
    return clean[:97].rstrip() + "..."


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
        watch_all_guild_channels: bool = False,
    ) -> None:
        import discord

        intents = discord.Intents.default()
        intents.message_content = True  # CRITICAL: required for message text
        intents.dm_messages = True

        self.bot_token = bot_token
        self.allowed_guilds = allowed_guilds
        self.allowed_users = allowed_users
        # When true, auto-listen to every channel in the allowed guild(s)
        # without an @mention (scoped by allowed_guilds).
        self._watch_all_guild_channels = watch_all_guild_channels
        self._queue: asyncio.Queue[IncomingMessage] = asyncio.Queue()
        self._client = discord.Client(intents=intents)
        self._tree = discord.app_commands.CommandTree(self._client)
        self._slash_commands_synced = False
        self._bot_user_id: int | None = None
        self._voice_reply_channels: set[str] = set()
        # Strong refs for fire-and-forget tasks (CPython only weak-refs
        # running tasks — unreferenced ones can be GC'd mid-await).
        self._bg_tasks: set[Any] = set()
        # Channels where bot listens without @mention
        # Reads from DISCORD_WATCHED_CHANNELS env var (comma-separated IDs)
        if watched_channels is None:
            try:
                from discord_channel_bindings import watched_channel_ids

                watched_channels = watched_channel_ids()
            except Exception:
                env_val = os.getenv("DISCORD_WATCHED_CHANNELS", "")
                watched_channels = [c.strip() for c in env_val.split(",") if c.strip()] if env_val else []
        self._watched_channels: set[str] = set(watched_channels)
        # Channels EXCLUDED from whole-guild watch — still reachable via
        # @mention or DM. Lets one guild stay whole-watched while a single
        # channel (e.g. a dedicated bot's channel) remains @mention-only.
        _excl = os.getenv("DISCORD_WATCH_EXCLUDE_CHANNELS", "")
        self._watch_exclude_channels: set[str] = {
            c.strip() for c in _excl.split(",") if c.strip()
        }

        # Register event handlers
        @self._client.event
        async def on_ready() -> None:
            self._bot_user_id = self._client.user.id
            print(f"[{datetime.now()}] Discord adapter connected ({self._client.user})")
            _sync_task = asyncio.create_task(self._sync_native_slash_commands(discord))
            self._bg_tasks.add(_sync_task)
            _sync_task.add_done_callback(self._bg_tasks.discard)

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
            if (
                not is_dm
                and not is_watched
                and not self._watches_guild(msg)
                and self._client.user not in msg.mentions
            ):
                return

            # Phase 4: voice ingress — transcribe audio attachments first.
            voice_text = await self._on_voice_message(msg)
            if voice_text:
                self._voice_reply_channels.add(str(msg.channel.id))
                incoming = self._normalize_message(msg, is_dm, voice_text, [])
                incoming.voice_origin = True
                await self._queue.put(incoming)
                return

            # Download image/document attachments to local disk.
            img_text, img_attachments = await self._download_image_attachments(msg)
            doc_text, doc_attachments = await self._download_document_attachments(msg)
            context_text = "\n".join(part for part in (img_text, doc_text) if part)
            attachments = [*img_attachments, *doc_attachments]
            incoming = self._normalize_message(msg, is_dm, context_text, attachments)
            await self._queue.put(incoming)

        self._register_native_slash_commands(discord)

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

    def _register_native_slash_commands(self, discord: Any) -> None:
        """Expose the curated Homie command menu as Discord slash commands."""

        for command_name, description in get_discord_native_command_menu():

            def make_callback(name: str) -> Any:
                async def callback(interaction: Any, args: str = "") -> None:
                    await self._queue_native_slash_command(interaction, name, args)

                callback.__name__ = f"slash_{name}"
                return discord.app_commands.describe(
                    args="Arguments after the slash command"
                )(callback)

            self._tree.add_command(
                discord.app_commands.Command(
                    name=command_name,
                    description=description,
                    callback=make_callback(command_name),
                )
            )
        self._register_native_vault_group(discord)

    def _register_native_vault_group(self, discord: Any) -> None:
        """Expose /vault as a typed Discord command group.

        Telegram and text surfaces use the same /vault command name with
        freeform args. Discord gets typed options but every callback converts
        back into the shared router text path.
        """

        app_commands = discord.app_commands
        vault_choices = [
            app_commands.Choice(name="thehomie", value="thehomie"),
            app_commands.Choice(name="coding-vault", value="coding-vault"),
        ]
        mode_choices = [
            app_commands.Choice(name="hybrid", value="hybrid"),
            app_commands.Choice(name="keyword", value="keyword"),
            app_commands.Choice(name="auto", value="auto"),
        ]
        routine_choices = [
            app_commands.Choice(name=name, value=name)
            for name in (
                "orient",
                "debrief",
                "weekly",
                "capture",
                "ingest",
                "compile",
                "research",
                "maintain",
                "context",
                "status",
                "think",
            )
        ]

        group = app_commands.Group(
            name="vault",
            description="Vault operations across the thehomie and coding vaults",
        )

        async def status(interaction: Any, vault: str = "thehomie") -> None:
            text = self._build_native_vault_text("status", vault=vault)
            await self._queue_native_slash_command(interaction, "vault", text[7:])

        async def db(interaction: Any, vault: str = "thehomie") -> None:
            text = self._build_native_vault_text("db", vault=vault)
            await self._queue_native_slash_command(interaction, "vault", text[7:])

        async def search(
            interaction: Any,
            query: str,
            vault: str = "thehomie",
            mode: str = "hybrid",
            limit: int = 5,
        ) -> None:
            text = self._build_native_vault_text(
                "search",
                query=query,
                vault=vault,
                mode=mode,
                limit=limit,
            )
            await self._queue_native_slash_command(interaction, "vault", text[7:])

        async def context(
            interaction: Any,
            topic: str,
            vault: str = "thehomie",
        ) -> None:
            text = self._build_native_vault_text("context", query=topic, vault=vault)
            await self._queue_native_slash_command(interaction, "vault", text[7:])

        async def contacts(
            interaction: Any,
            query: str = "",
            vault: str = "thehomie",
        ) -> None:
            text = self._build_native_vault_text("contacts", query=query, vault=vault)
            await self._queue_native_slash_command(interaction, "vault", text[7:])

        async def ingest(
            interaction: Any,
            url: str = "",
            attachment: Any = None,
            vault: str = "thehomie",
        ) -> None:
            text = self._build_native_vault_text("ingest", url=url, vault=vault)
            await self._queue_native_slash_command(
                interaction,
                "vault",
                text[7:],
                interaction_attachment=attachment,
            )

        async def ops(
            interaction: Any,
            routine: str,
            args: str = "",
            vault: str = "thehomie",
        ) -> None:
            text = self._build_native_vault_text(
                "ops",
                routine=routine,
                args=args,
                vault=vault,
            )
            await self._queue_native_slash_command(interaction, "vault", text[7:])

        status.__name__ = "slash_vault_status"
        db.__name__ = "slash_vault_db"
        search.__name__ = "slash_vault_search"
        context.__name__ = "slash_vault_context"
        contacts.__name__ = "slash_vault_contacts"
        ingest.__name__ = "slash_vault_ingest"
        ops.__name__ = "slash_vault_ops"
        ingest.__annotations__["attachment"] = discord.Attachment | None

        group.add_command(
            app_commands.Command(
                name="status",
                description="Show vault configuration and recall index status",
                callback=app_commands.describe(vault="Vault to inspect")(
                    app_commands.choices(vault=vault_choices)(status)
                ),
            )
        )
        group.add_command(
            app_commands.Command(
                name="db",
                description="Show the selected vault memory and recall DB paths",
                callback=app_commands.describe(vault="Vault to inspect")(
                    app_commands.choices(vault=vault_choices)(db)
                ),
            )
        )
        group.add_command(
            app_commands.Command(
                name="search",
                description="Search a vault with Homie recall",
                callback=app_commands.describe(
                    query="Search query",
                    vault="Vault to search",
                    mode="Recall mode",
                    limit="Maximum matches, 1-10",
                )(app_commands.choices(vault=vault_choices, mode=mode_choices)(search)),
            )
        )
        group.add_command(
            app_commands.Command(
                name="context",
                description="Pull context briefing for a topic",
                callback=app_commands.describe(
                    topic="Topic to brief",
                    vault="Vault to search",
                )(app_commands.choices(vault=vault_choices)(context)),
            )
        )
        group.add_command(
            app_commands.Command(
                name="contacts",
                description="Search contact-related vault knowledge",
                callback=app_commands.describe(
                    query="Optional person or company query",
                    vault="Vault to search",
                )(app_commands.choices(vault=vault_choices)(contacts)),
            )
        )
        group.add_command(
            app_commands.Command(
                name="ingest",
                description="Ingest a URL or attached document into a vault",
                callback=app_commands.describe(
                    url="URL to ingest",
                    attachment="Document attachment to ingest",
                    vault="Vault to write into",
                )(app_commands.choices(vault=vault_choices)(ingest)),
            )
        )
        group.add_command(
            app_commands.Command(
                name="ops",
                description="Run a vault-ops routine",
                callback=app_commands.describe(
                    routine="Vault-ops routine",
                    args="Arguments for the routine",
                    vault="Vault context",
                )(app_commands.choices(vault=vault_choices, routine=routine_choices)(ops)),
            )
        )
        self._tree.add_command(group)

    async def _sync_native_slash_commands(self, discord: Any) -> None:
        """Sync once per process. Guild syncs update immediately; global can lag."""

        if self._slash_commands_synced:
            return
        try:
            timeout_s = _discord_sync_timeout_seconds()
            if self.allowed_guilds:
                total = 0
                for guild_id in self.allowed_guilds:
                    guild = discord.Object(id=int(guild_id))
                    self._tree.copy_global_to(guild=guild)
                    synced = await asyncio.wait_for(
                        self._tree.sync(guild=guild),
                        timeout=timeout_s,
                    )
                    total += len(synced)
                print(f"[{datetime.now()}] Registered {total} Discord slash commands")
            else:
                synced = await asyncio.wait_for(
                    self._tree.sync(),
                    timeout=timeout_s,
                )
                print(f"[{datetime.now()}] Registered {len(synced)} Discord slash commands")
            self._slash_commands_synced = True
        except asyncio.TimeoutError:
            print(
                f"[{datetime.now()}] Discord slash command sync timed out after {timeout_s:g}s",
                flush=True,
            )
        except Exception as e:
            print(f"[{datetime.now()}] Discord slash command sync failed: {e}", flush=True)

    async def _queue_native_slash_command(
        self,
        interaction: Any,
        command_name: str,
        args: str = "",
        *,
        attachments: list[Attachment] | None = None,
        interaction_attachment: Any = None,
    ) -> None:
        """Convert a native Discord slash invocation into the shared router path."""

        if self.allowed_users and str(interaction.user.id) not in self.allowed_users:
            try:
                await interaction.response.send_message(
                    "You don't have permission.", ephemeral=True
                )
            except Exception:
                pass
            return
        if self.allowed_guilds and interaction.guild_id is not None:
            if str(interaction.guild_id) not in self.allowed_guilds:
                try:
                    await interaction.response.send_message(
                        "This server is not allowed.", ephemeral=True
                    )
                except Exception:
                    pass
                return

        try:
            await interaction.response.defer(thinking=True)
        except Exception:
            pass

        queued_attachments = list(attachments or [])
        if interaction_attachment is not None:
            downloaded = await self._download_interaction_attachment(
                interaction,
                interaction_attachment,
            )
            if downloaded is None:
                return
            queued_attachments.append(downloaded)

        ch_id = str(interaction.channel_id)
        text = f"/{command_name}"
        args = str(args or "").strip()
        if args:
            text = f"{text} {args}"
        incoming = IncomingMessage(
            text=text,
            user=User(
                Platform.DISCORD,
                str(interaction.user.id),
                interaction.user.display_name,
            ),
            channel=Channel(Platform.DISCORD, ch_id, is_dm=interaction.guild_id is None),
            platform=Platform.DISCORD,
            thread=Thread(thread_id=ch_id),
            attachments=queued_attachments,
            raw_event={
                "interaction_id": str(interaction.id),
                "interaction_type": "slash_command",
                "command": command_name,
                "guild": str(interaction.guild_id or ""),
                "display_text": text,
            },
        )
        await self._queue.put(incoming)

    @staticmethod
    def _build_native_vault_text(
        subcommand: str,
        *,
        query: str = "",
        url: str = "",
        routine: str = "",
        args: str = "",
        vault: str = "thehomie",
        mode: str = "hybrid",
        limit: int = 5,
    ) -> str:
        parts = ["/vault", subcommand]
        if subcommand in {"search", "context", "contacts"} and query.strip():
            parts.append(query.strip())
        elif subcommand == "ingest" and url.strip():
            parts.append(url.strip())
        elif subcommand == "ops":
            parts.append(routine.strip())
            if args.strip():
                parts.append(args.strip())
        parts.extend(["--vault", vault])
        if subcommand == "search":
            parts.extend(["--mode", mode, "--limit", str(limit)])
        return " ".join(part for part in parts if part)

    async def _download_interaction_attachment(
        self,
        interaction: Any,
        attachment: Any,
    ) -> Attachment | None:
        """Download a Discord slash-command attachment to the internal model."""

        import tempfile
        import httpx

        filename = self._safe_attachment_filename(getattr(attachment, "filename", "attachment"))
        content_type = getattr(attachment, "content_type", None) or ""
        size = getattr(attachment, "size", None)
        if size and size > 8 * 1024 * 1024:
            try:
                await interaction.followup.send(
                    f"Skipped document {filename}: exceeds 8MB parser limit.",
                    ephemeral=True,
                )
            except Exception:
                pass
            return None

        tmp_dir = Path(tempfile.gettempdir()) / "thehomie_discord_documents"
        tmp_dir.mkdir(exist_ok=True)
        ext = Path(filename).suffix or ".bin"
        attachment_id = getattr(attachment, "id", "interaction")
        local_path = tmp_dir / f"{interaction.id}_{attachment_id}{ext}"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(str(getattr(attachment, "url", "")))
                resp.raise_for_status()
                local_path.write_bytes(resp.content)
        except Exception as e:
            print(f"[{datetime.now()}] Discord slash attachment download failed ({filename}): {e}")
            try:
                await interaction.followup.send(
                    f"Failed to download document {filename}: {type(e).__name__}.",
                    ephemeral=True,
                )
            except Exception:
                pass
            return None

        print(
            f"[{datetime.now()}] Discord slash document saved: "
            f"{local_path} ({size or 0} bytes)"
        )
        return Attachment(
            filename=filename,
            mimetype=content_type,
            url=str(local_path),
            size_bytes=size,
        )

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

        wants_voice_reply = (
            not message.is_update
            and not message.is_error
            and message.channel.platform_id in self._voice_reply_channels
        )
        if wants_voice_reply:
            self._voice_reply_channels.discard(message.channel.platform_id)
            tier = self._classify_voice_tier(text)
            if tier == "voice_only":
                await self._send_voice_response(channel, text)
                return None
            if tier == "voice_and_text":
                await self._send_voice_response(channel, text)

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

    def _watches_guild(self, msg: Any) -> bool:
        """True if the whole guild is auto-listened (no @mention needed).

        Only fires when DISCORD_WATCH_ALL_GUILD_CHANNELS is on, the message
        comes from a guild, and that guild is in allowed_guilds (empty = any).
        DMs return False (they are always handled via the is_dm path).
        Channels in DISCORD_WATCH_EXCLUDE_CHANNELS are skipped here so they
        stay @mention-only even when the rest of the guild is whole-watched.
        """
        if not self._watch_all_guild_channels:
            return False
        guild = getattr(msg, "guild", None)
        if guild is None:
            return False
        if self.allowed_guilds and str(guild.id) not in self.allowed_guilds:
            return False
        if str(msg.channel.id) in self._watch_exclude_channels:
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

    _VOICE_ONLY_MAX = 300
    _VOICE_AND_TEXT_MAX = 1500

    @staticmethod
    def _classify_voice_tier(raw_text: str) -> str:
        """Classify whether a reply should be voice-only, voice+text, or text-only."""
        text = raw_text.strip()
        if not text:
            return "text_only"
        if "```" in text or "|" in text:
            return "text_only"
        length = len(text)
        if length <= DiscordAdapter._VOICE_ONLY_MAX:
            return "voice_only"
        if length <= DiscordAdapter._VOICE_AND_TEXT_MAX:
            return "voice_and_text"
        return "text_only"

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
        engine can parse document context before runtime dispatch.
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
                    "The document's content is provided to the model along with "
                    "this message. If the content is missing or partial, say so "
                    "explicitly instead of guessing.]"
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
