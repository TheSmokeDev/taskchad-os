"""Bounded current-speaker context for shared chat runtime turns."""

from __future__ import annotations

import re
from dataclasses import dataclass

from models import IncomingMessage

_MAX_FIELD_CHARS = 96
_BLANKISH = {"", "unknown", "none", "null", "anonymous", "user"}


@dataclass(frozen=True)
class SpeakerContext:
    """Prompt-safe active-speaker summary derived from ingress metadata."""

    status: str
    platform: str
    platform_user_id: str
    display_name: str
    channel_scope: str
    warning: str = ""

    @property
    def is_known(self) -> bool:
        return self.status == "verified_transport_metadata"


def _clean_field(value: object, *, max_chars: int = _MAX_FIELD_CHARS) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    text = text.replace("<", "").replace(">", "")
    if len(text) > max_chars:
        return text[: max_chars - 12].rstrip() + " [truncated]"
    return text


def _is_blankish(value: str) -> bool:
    return value.strip().lower() in _BLANKISH


def resolve_speaker_context(message: IncomingMessage) -> SpeakerContext:
    """Resolve active speaker from normalized ingress metadata.

    This intentionally uses the platform/user fields already normalized by
    adapters. It does not read profile files, vault memory, local paths, or raw
    event dumps, so unknown speakers degrade loudly instead of inheriting owner
    identity.
    """

    platform = _clean_field(message.platform.value)
    platform_user_id = _clean_field(message.user.platform_id)
    display_name = _clean_field(message.user.display_name)
    channel_scope = "direct_message" if message.channel.is_dm else "shared_channel"

    if _is_blankish(platform_user_id) or _is_blankish(display_name):
        return SpeakerContext(
            status="unknown_unverified",
            platform=platform,
            platform_user_id=platform_user_id if not _is_blankish(platform_user_id) else "unknown",
            display_name=display_name if not _is_blankish(display_name) else "unknown",
            channel_scope=channel_scope,
            warning=(
                "Active speaker identity is incomplete. Do not assume this is the "
                "owner or any remembered person."
            ),
        )

    return SpeakerContext(
        status="verified_transport_metadata",
        platform=platform,
        platform_user_id=platform_user_id,
        display_name=display_name,
        channel_scope=channel_scope,
    )


def render_speaker_context(context: SpeakerContext) -> str:
    """Render a compact prompt block with explicit unknown-speaker behavior."""

    lines = [
        "status: " + context.status,
        "platform: " + context.platform,
        "platform_user_id: " + context.platform_user_id,
        "display_name: " + context.display_name,
        "channel_scope: " + context.channel_scope,
    ]
    if context.warning:
        lines.append("warning: " + context.warning)
    lines.extend(
        [
            "instruction: Treat first-person pronouns in the current user turn as "
            "coming from this active speaker only.",
            "instruction: Do not use owner/default identity unless the active speaker "
            "is verified as that person.",
        ]
    )
    if not context.is_known:
        lines.append(
            "instruction: If identity-specific memory or profile facts are needed, "
            "ask the speaker to verify who they are."
        )
    return "\n".join(lines)


def speaker_context_metadata(context: SpeakerContext) -> dict[str, str | bool]:
    """Return trace/runtime metadata without raw names or profile content."""

    return {
        "status": context.status,
        "platform": context.platform,
        "channel_scope": context.channel_scope,
        "has_display_name": context.display_name != "unknown",
        "has_platform_user_id": context.platform_user_id != "unknown",
    }


__all__ = [
    "SpeakerContext",
    "render_speaker_context",
    "resolve_speaker_context",
    "speaker_context_metadata",
]
