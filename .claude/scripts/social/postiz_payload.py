"""Postiz per-platform payload settings — pure functions, no I/O.

Every Postiz create-post carries a ``settings`` object whose ``__type``
names the platform contract. Shapes come from the published Postiz API
docs (no AGPL source consulted). v1 publishes TEXT+IMAGE platforms only —
video-required platforms are refused by the executor with a clear error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from social.channels import SocialChannel
    from social.models import SocialPost

# Platforms whose settings need only {"__type": ...}.
MINIMAL_SETTINGS_PLATFORMS = frozenset(
    ["mastodon", "bluesky", "threads", "telegram", "nostr", "vk", "facebook"]
)

# Platforms Postiz cannot publish without a video attachment. The executor
# refuses these ONLY when the draft has no rendered video; a youtube draft
# WITH a video now publishes (Shorts), while tiktok stays effectively refused
# (no video path built for it on this instance).
VIDEO_REQUIRED_PLATFORMS = frozenset(["youtube", "tiktok"])

# channel_id → Postiz settings __type, where they differ from the raw id.
_CHANNEL_TYPE_MAP = {
    "fb": "facebook",
    "ig": "instagram",
    "li": "linkedin",
    "twitter": "x",
}


def resolve_platform_type(channel: "SocialChannel") -> str:
    """The Postiz ``settings.__type`` for a channel.

    Explicit ``postiz_settings.__type`` in channels.yaml wins; otherwise the
    channel id maps through the alias table (falls back to itself).
    """
    override = str(channel.postiz_settings.get("__type", "") or "")
    if override:
        return override
    cid = channel.channel_id.lower()
    return _CHANNEL_TYPE_MAP.get(cid, cid)


def build_platform_settings(
    platform_type: str,
    post: "SocialPost",
    channel: "SocialChannel",
) -> dict[str, Any]:
    """Build the per-platform ``settings`` object for one post.

    Unknown platforms get the minimal shape — Postiz rejects anything that
    genuinely needs more, and that error surfaces on the queue row.
    """
    settings: dict[str, Any] = {"__type": platform_type}

    if platform_type in ("instagram", "instagram-standalone"):
        settings["post_type"] = str(
            channel.postiz_settings.get("post_type", "post") or "post"
        )
    elif platform_type == "x":
        settings["who_can_reply_post"] = str(
            channel.postiz_settings.get("who_can_reply_post", "everyone")
            or "everyone"
        )
    elif platform_type == "youtube":
        # Built for completeness; the v1 executor refuses video platforms
        # before this payload ever reaches Postiz.
        settings["title"] = (post.title or "")[:100]
        settings["type"] = str(
            channel.postiz_settings.get("visibility", "public") or "public"
        )

    # Operator-provided extras pass through (never overriding __type).
    for key, value in channel.postiz_settings.items():
        if key in ("__type", "visibility", "post_type", "who_can_reply_post"):
            continue
        settings.setdefault(key, value)

    return settings
