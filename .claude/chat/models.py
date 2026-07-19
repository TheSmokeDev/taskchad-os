"""Platform-agnostic message models for the chat interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Platform(Enum):
    """Supported chat platforms."""

    SLACK = "slack"
    DISCORD = "discord"
    TELEGRAM = "telegram"
    WEB = "web"
    CLI = "cli"
    WHATSAPP = "whatsapp"
    WEBHOOK = "webhook"  # Phase 4 (hermes-v18 Tier-1) — webhook event ingress


class MessageType(Enum):
    """Types of chat messages."""

    TEXT = "text"
    FILE = "file"
    REACTION = "reaction"


@dataclass
class User:
    """Platform-agnostic user representation."""

    platform: Platform
    platform_id: str
    display_name: str | None = None

    @property
    def unified_id(self) -> str:
        return f"{self.platform.value}:{self.platform_id}"


@dataclass
class Channel:
    """Platform-agnostic channel representation."""

    platform: Platform
    platform_id: str
    name: str | None = None
    is_dm: bool = False

    @property
    def unified_id(self) -> str:
        return f"{self.platform.value}:{self.platform_id}"


@dataclass
class Thread:
    """Thread identifier within a channel."""

    thread_id: str
    parent_message_id: str | None = None


@dataclass
class Attachment:
    """File attachment on a message."""

    filename: str
    mimetype: str | None = None
    url: str | None = None
    size_bytes: int | None = None


@dataclass
class IncomingMessage:
    """Normalized incoming message from any platform."""

    text: str
    user: User
    channel: Channel
    platform: Platform
    thread: Thread | None = None
    platform_message_id: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    # Platform caption attached to an upload (e.g. Telegram document caption).
    # Distinct from `text` (which folds the caption into the rendered turn);
    # routers match explicit commands (e.g. /vault-ingest) against this field.
    caption: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    is_piv: bool = False           # True when message contains PIV instruction content
    piv_command: str = ""          # Original command name (e.g., "planning", "clutch")
    prefetched_context: str = ""   # Pre-fetched data from router (skip tools in engine)
    agent_type: str = "thehomie"
    user_role: str = "admin"          # "admin", "operator", or "viewer"
    raw_event: dict[str, Any] = field(default_factory=dict)
    source: str = "interactive"       # PRD-7 §7.10 / Phase 4 (PRP-7d): "interactive"|"tool"|"cron"|"hook"
    # True when this turn originated as a transcribed voice message. The router
    # skips the "Thinking..." text placeholder for these turns so the adapter's
    # one-shot voice-reply flag is consumed by the FINAL send() (the real
    # answer), not by the placeholder.
    voice_origin: bool = False


@dataclass
class MessageComponent:
    """An interactive button component (platform-agnostic)."""

    label: str
    custom_id: str
    style: str = "primary"  # "primary", "secondary", "success", "danger"
    disabled: bool = False


@dataclass
class MessageEmbed:
    """Rich embed for platforms that support it (Discord, Slack)."""

    title: str = ""
    description: str = ""
    color: int = 0x5865F2  # Discord blurple default
    fields: list[dict[str, Any]] = field(default_factory=list)
    footer: str = ""
    image_url: str = ""
    url: str = ""
    thumbnail_url: str = ""


@dataclass
class OutgoingMessage:
    """Message to send back to a platform."""

    text: str
    channel: Channel
    thread: Thread | None = None
    is_update: bool = False
    update_message_id: str | None = None
    is_error: bool = False  # True when engine/router sends an error response
    attachments: list[Attachment] = field(default_factory=list)
    components: list[MessageComponent] = field(default_factory=list)
    embed: MessageEmbed | None = None
    # Multiple rich cards. ``embed`` remains supported for older adapters and
    # extensions; adapters prefer this collection when it is non-empty.
    embeds: list[MessageEmbed] = field(default_factory=list)
    # Per-adapter rendered hint (e.g. concept-draft footer). Never persisted to
    # chat_history — adapters render it appropriate to the medium.
    footer: str | None = None
