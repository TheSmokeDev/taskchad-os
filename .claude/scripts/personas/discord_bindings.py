"""Strict Discord binding document primitives for persona provisioning.

The live chat adapter keeps a fail-soft read surface. Mutation paths use this
module's strict parser so malformed operator configuration can never be
silently replaced with an empty document.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class DiscordBindingError(ValueError):
    """Raised when a binding document is malformed or conflicts."""


@dataclass(frozen=True)
class DiscordChannelBinding:
    channel_id: str
    name: str
    kind: str
    persona_id: str = ""
    guild_id: str = ""
    enabled: bool = True


def load_binding_document(
    path: str | Path,
    *,
    strict: bool = True,
) -> dict[str, Any]:
    """Read one binding document while preserving unknown fields."""

    binding_path = Path(path)
    if not binding_path.is_file():
        return {"channels": {}}
    try:
        raw = json.loads(binding_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        if strict:
            raise DiscordBindingError(
                f"invalid Discord binding document {binding_path}: {exc}"
            ) from exc
        return {}
    if not isinstance(raw, dict):
        if strict:
            raise DiscordBindingError(
                f"Discord binding document must be an object: {binding_path}"
            )
        return {}
    channels = raw.get("channels", {})
    if not isinstance(channels, dict):
        if strict:
            raise DiscordBindingError(
                f"Discord binding channels must be an object: {binding_path}"
            )
        return {}
    valid_channels: dict[str, Any] = {}
    for channel_id, row in channels.items():
        if not str(channel_id).strip() or not isinstance(row, dict):
            if strict:
                raise DiscordBindingError(
                    f"Discord channel binding {channel_id!r} must be an object"
                )
            continue
        if "enabled" in row and not isinstance(row["enabled"], bool):
            if strict:
                raise DiscordBindingError(
                    f"Discord channel binding {channel_id!r} enabled must be a boolean"
                )
            continue
        valid_channels[str(channel_id)] = row
    if not strict and len(valid_channels) != len(channels):
        raw = copy.deepcopy(raw)
        raw["channels"] = valid_channels
    return raw


def parse_bindings(
    document: dict[str, Any],
) -> dict[str, DiscordChannelBinding]:
    """Convert a validated document to runtime binding records."""

    guild_id = str(document.get("guild_id") or "").strip()
    channels = document.get("channels", {})
    if not isinstance(channels, dict):
        return {}
    bindings: dict[str, DiscordChannelBinding] = {}
    for channel_id, row in channels.items():
        if not isinstance(row, dict):
            continue
        cid = str(channel_id or "").strip()
        if not cid:
            continue
        kind = str(
            row.get("kind") or ("persona" if row.get("persona") else "default")
        ).strip()
        persona_id = str(row.get("persona") or "").strip()
        bindings[cid] = DiscordChannelBinding(
            channel_id=cid,
            name=str(row.get("name") or persona_id or cid).strip(),
            kind=kind,
            persona_id=persona_id,
            guild_id=str(row.get("guild_id") or guild_id).strip(),
            enabled=row.get("enabled", True),
        )
    return bindings


def reconcile_persona_bindings(
    document: dict[str, Any],
    *,
    persona_id: str,
    channels: Iterable[Any],
) -> dict[str, Any]:
    """Return a copy with exactly the blueprint's Discord rows for a persona."""

    result = copy.deepcopy(document)
    raw_channels = result.setdefault("channels", {})
    if not isinstance(raw_channels, dict):
        raise DiscordBindingError("Discord binding channels must be an object")

    desired: dict[str, Any] = {}
    for channel in channels:
        kind = str(getattr(channel, "kind", "") or "").strip()
        if kind != "discord":
            continue
        channel_id = str(getattr(channel, "channel_id", "") or "").strip()
        name = str(getattr(channel, "name", "") or persona_id).strip()
        if not channel_id.isdigit():
            raise DiscordBindingError(
                f"Discord channel id must contain digits only: {channel_id!r}"
            )
        existing = raw_channels.get(channel_id)
        if isinstance(existing, dict):
            owner = str(existing.get("persona") or "").strip()
            existing_kind = _effective_kind(existing)
            if existing_kind == "persona" and owner and owner != persona_id:
                raise DiscordBindingError(
                    f"Discord channel {channel_id} is already bound to {owner}"
                )
            row = copy.deepcopy(existing)
            preserve_activation = (
                existing_kind == "persona" and owner == persona_id
            )
        else:
            row = {}
            preserve_activation = False
        row.update({"kind": "persona", "persona": persona_id, "name": name})
        if not preserve_activation:
            row["enabled"] = False
        desired[channel_id] = row

    for channel_id, row in list(raw_channels.items()):
        if (
            isinstance(row, dict)
            and _effective_kind(row) == "persona"
            and str(row.get("persona") or "").strip() == persona_id
            and channel_id not in desired
        ):
            del raw_channels[channel_id]
    raw_channels.update(desired)
    return result


def _effective_kind(row: dict[str, Any]) -> str:
    return str(
        row.get("kind") or ("persona" if row.get("persona") else "default")
    ).strip()


def dump_binding_document(document: dict[str, Any]) -> str:
    """Serialize a validated document with stable, reviewable formatting."""

    channels = document.get("channels", {})
    if not isinstance(channels, dict):
        raise DiscordBindingError("Discord binding channels must be an object")
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


__all__ = [
    "DiscordBindingError",
    "DiscordChannelBinding",
    "dump_binding_document",
    "load_binding_document",
    "parse_bindings",
    "reconcile_persona_bindings",
]
