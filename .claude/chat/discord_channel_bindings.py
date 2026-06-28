"""Discord channel -> persona binding loader.

The binding file is local operator configuration, not a secret. It lets one
Discord bot listen in multiple channels while routing each channel to the
correct Homie persona profile.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from models import Platform


DEFAULT_BINDINGS_FILE = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "discord-channel-bindings.json"
)


@dataclass(frozen=True)
class DiscordChannelBinding:
    channel_id: str
    name: str
    kind: str
    persona_id: str = ""
    guild_id: str = ""


def bindings_file_path() -> Path:
    configured = os.getenv("DISCORD_CHANNEL_BINDINGS_FILE", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_BINDINGS_FILE


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _load_raw(path: Path | None = None) -> dict[str, Any]:
    binding_path = path or bindings_file_path()
    if not binding_path.is_file():
        return {}
    try:
        data = json.loads(binding_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_discord_channel_bindings(path: Path | None = None) -> dict[str, DiscordChannelBinding]:
    raw = _load_raw(path)
    guild_id = str(raw.get("guild_id") or "").strip()
    channels = raw.get("channels")
    if not isinstance(channels, dict):
        return {}

    bindings: dict[str, DiscordChannelBinding] = {}
    for channel_id, row in channels.items():
        cid = str(channel_id or "").strip()
        if not cid or not isinstance(row, dict):
            continue
        kind = str(row.get("kind") or ("persona" if row.get("persona") else "default")).strip()
        persona_id = str(row.get("persona") or "").strip()
        name = str(row.get("name") or persona_id or cid).strip()
        bindings[cid] = DiscordChannelBinding(
            channel_id=cid,
            name=name,
            kind=kind,
            persona_id=persona_id,
            guild_id=str(row.get("guild_id") or guild_id).strip(),
        )
    return bindings


def watched_channel_ids(path: Path | None = None) -> list[str]:
    """Return channel IDs that should be auto-listened without @mention."""

    ids = set(_split_csv(os.getenv("DISCORD_WATCHED_CHANNELS", "")))
    ids.update(load_discord_channel_bindings(path).keys())
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
    if binding is None:
        return None
    if binding.kind in {"", "default", "normal"}:
        return None

    raw_event = getattr(incoming, "raw_event", None) or {}
    incoming_guild = str(raw_event.get("guild") or "").strip() if isinstance(raw_event, dict) else ""
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
