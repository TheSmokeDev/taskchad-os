"""Discord channel -> persona binding loader.

The binding file is local operator configuration, not a secret. It lets one
Discord bot listen in multiple channels while routing each channel to the
correct Homie persona profile.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from models import Platform

from personas import get_default_paths
from personas.discord_bindings import (
    DiscordChannelBinding,
    load_binding_document,
    parse_bindings,
)

DEFAULT_BINDINGS_FILE = (
    get_default_paths()["data"] / "discord-channel-bindings.json"
)


def bindings_file_path() -> Path:
    configured = os.getenv("DISCORD_CHANNEL_BINDINGS_FILE", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_BINDINGS_FILE


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def load_discord_channel_bindings(path: Path | None = None) -> dict[str, DiscordChannelBinding]:
    raw = load_binding_document(path or bindings_file_path(), strict=False)
    return parse_bindings(raw)


def watched_channel_ids(path: Path | None = None) -> list[str]:
    """Return channel IDs that should be auto-listened without @mention."""

    ids = set(_split_csv(os.getenv("DISCORD_WATCHED_CHANNELS", "")))
    ids.update(
        channel_id
        for channel_id, binding in load_discord_channel_bindings(path).items()
        if binding.enabled
    )
    return sorted(ids)


def resolve_discord_channel_binding(incoming: Any) -> DiscordChannelBinding | None:
    platform = getattr(incoming, "platform", None)
    platform_value = getattr(platform, "value", str(platform))
    if platform != Platform.DISCORD and platform_value != Platform.DISCORD.value:
        return None

    channel = getattr(incoming, "channel", None)
    channel_id = str(getattr(channel, "platform_id", "") or "").strip()
    if not channel_id:
        return None

    binding = load_discord_channel_bindings().get(channel_id)
    if binding is None or not binding.enabled:
        return None
    if binding.kind in {"", "default", "normal"}:
        return None

    raw_event = getattr(incoming, "raw_event", None) or {}
    incoming_guild = (
        str(raw_event.get("guild") or "").strip()
        if isinstance(raw_event, dict)
        else ""
    )
    if binding.guild_id and incoming_guild and binding.guild_id != incoming_guild:
        return None

    if binding.kind == "persona" and binding.persona_id:
        return binding
    return None


__all__ = [
    "DEFAULT_BINDINGS_FILE",
    "DiscordChannelBinding",
    "bindings_file_path",
    "load_discord_channel_bindings",
    "resolve_discord_channel_binding",
    "watched_channel_ids",
]
