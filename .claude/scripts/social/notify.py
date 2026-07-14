"""Deliver a generated social draft to the operator's Telegram with inline
approve / edit / reject buttons.

Cross-process safe: the cadence cron runs in a SEPARATE process from the
chat bot, so this posts directly to the Telegram Bot API (same pattern as
``example_scout._send_telegram``). The button tap then routes back into the
running bot's existing callback pipeline as ``__button:social:<action>:<id>``.

Fail-open contract: a delivery failure NEVER breaks draft generation. Every
path returns a bool and swallows its own exceptions — the draft is already
persisted in the queue DB before this is ever called.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING

from social.channels import get_channel

if TYPE_CHECKING:
    from social.models import SocialPost

# Telegram hard limits.
_TG_TEXT_LIMIT = 4096
# A photo message's caption is capped far lower than a text message's body.
_TG_CAPTION_LIMIT = 1024
# callback_data is capped at 64 bytes; "social:approve:<id>" is tiny, so the
# bot's hashed-callback map is never engaged and the custom_id arrives intact.

# Local image extensions Telegram accepts as a photo upload.
_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _redact(text: str, token: str) -> str:
    """Strip the bot token from any string before it is printed/logged.
    urllib exceptions embed the request URL (which carries the token)."""
    if token and token in text:
        text = text.replace(token, "***")
    return text


def _telegram_credentials() -> tuple[str, str] | None:
    """Return (token, chat_id) from env, or None when not configured."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    user_ids = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    if not token or not user_ids:
        return None
    chat_id = user_ids.split(",")[0].strip()
    if not chat_id:
        return None
    return token, chat_id


def _build_card_text(post: "SocialPost", limit: int = _TG_TEXT_LIMIT) -> str:
    """Compose the paste-ready draft card. Plain text (no parse_mode) so
    arbitrary generated content can never break Telegram entity parsing.

    ``limit`` is the hard character ceiling: 4096 for a text message body,
    1024 for a photo caption (see ``_build_photo_caption``)."""
    channel_id = post.channel or "social"
    configured = get_channel(channel_id)
    channel = (
        configured.display_name
        if configured is not None and configured.display_name
        else channel_id.upper()
    )
    source = post.topic_source or "manual"
    header = f"📝 New {channel} draft  ·  #{post.id}  ·  {source}"
    body = post.body or "(empty draft)"
    footer = "Tap Approve & Post to publish, Edit to tweak, or Reject."

    # Reserve room for header/footer/separators inside the limit.
    overhead = len(header) + len(footer) + 8
    budget = limit - overhead
    if budget > 0 and len(body) > budget:
        body = body[: budget - 1].rstrip() + "…"

    card = f"{header}\n\n{body}\n\n{footer}"
    # Hard cap — header components (channel/source) are not length-bounded, so
    # guarantee the final string never exceeds the Telegram limit regardless.
    return card[:limit]


def _utf16_len(text: str) -> int:
    """Length in UTF-16 code units — how Telegram counts caption/message length
    (supplementary-plane emoji count as 2 units, not 1)."""
    return len(text.encode("utf-16-le")) // 2


def _utf16_truncate(text: str, max_units: int) -> str:
    """Truncate to at most ``max_units`` UTF-16 code units without splitting a
    surrogate pair. No-op when already within budget."""
    if _utf16_len(text) <= max_units:
        return text
    out: list[str] = []
    units = 0
    for ch in text:
        w = 2 if ord(ch) > 0xFFFF else 1
        if units + w > max_units:
            break
        out.append(ch)
        units += w
    return "".join(out)


def _build_photo_caption(post: "SocialPost") -> str:
    """The draft card sized for a photo caption. Telegram caps captions at 1024
    UTF-16 code units (not code points), so a code-point cap alone can still
    overflow on emoji-heavy text — apply a UTF-16-aware final trim."""
    caption = _build_card_text(post, limit=_TG_CAPTION_LIMIT)
    return _utf16_truncate(caption, _TG_CAPTION_LIMIT)


def _build_reply_markup(post_id: int) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "✅ Approve & Post", "callback_data": f"social:approve:{post_id}"}],
            [
                {"text": "✏️ Edit", "callback_data": f"social:edit:{post_id}"},
                {"text": "❌ Reject", "callback_data": f"social:reject:{post_id}"},
            ],
        ]
    }


def send_text_to_telegram(text: str) -> bool:
    """Send a plain operator notification (no buttons). Fail-open: returns
    False on any failure, never raises — used by the Postiz reconcile pass
    to surface async publish failures."""
    if not text:
        return False
    creds = _telegram_credentials()
    if creds is None:
        return False
    token, chat_id = creds
    try:
        data = urllib.parse.urlencode(
            {
                "chat_id": chat_id,
                "text": text[:_TG_TEXT_LIMIT],
                "disable_web_page_preview": "true",
            }
        ).encode()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as exc:
        safe = _redact(f"{type(exc).__name__}: {exc}", token)
        print(f"[social.notify] Telegram send failed: {safe}")
        return False


def _send_photo(
    token: str,
    chat_id: str,
    image_path: str,
    caption: str,
    reply_markup: dict,
) -> bool:
    """Upload a local image as a Telegram photo with a caption + inline buttons.

    Returns False on any failure (unsupported type, unreadable/empty file,
    network error) so the caller can fall back to the text card. Never raises.
    Builds the multipart/form-data body with the stdlib only (no new deps),
    keeping the cross-process, dependency-light contract of this module."""
    ext = os.path.splitext(image_path)[1].lower()
    mime = _IMAGE_MIME.get(ext)
    if mime is None:
        return False
    try:
        with open(image_path, "rb") as fh:
            photo_bytes = fh.read()
    except OSError:
        return False
    if not photo_bytes:
        return False

    boundary = "----HomieSocialNotify7f3a2b"
    parts: list[bytes] = []
    for name, value in (
        ("chat_id", chat_id),
        ("caption", caption),
        ("reply_markup", json.dumps(reply_markup)),
    ):
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        )
        parts.append(f"{value}\r\n".encode())
    filename = os.path.basename(image_path) or "image.png"
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'.encode()
    )
    parts.append(f"Content-Type: {mime}\r\n\r\n".encode())
    parts.append(photo_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)

    try:
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        req = urllib.request.Request(url, data=body)
        req.add_header(
            "Content-Type", f"multipart/form-data; boundary={boundary}"
        )
        urllib.request.urlopen(req, timeout=30)
        return True
    except Exception as exc:
        safe = _redact(f"{type(exc).__name__}: {exc}", token)
        print(f"[social.notify] Telegram photo send failed: {safe}")
        return False


def deliver_draft_to_telegram(post: "SocialPost") -> bool:
    """Send the draft card with inline buttons to the operator's Telegram.

    When the draft carries a readable local image (``media_type == "image"``),
    send it as a photo card (image + caption + buttons); on any photo failure
    fall through to the plain text card so the operator NEVER loses the card.

    Returns True on success, False on any failure (missing creds, network
    error, bad post). Never raises — delivery is best-effort and additive.
    """
    post_id = getattr(post, "id", 0)
    if post is None or not isinstance(post_id, int) or post_id <= 0:
        return False

    creds = _telegram_credentials()
    if creds is None:
        print("[social.notify] Telegram creds not configured; draft not delivered")
        return False
    token, chat_id = creds

    # Photo card first when a rendered image is attached; fail-open to text.
    media_path = getattr(post, "media_path", None)
    media_type = getattr(post, "media_type", None)
    if media_type == "image" and media_path and os.path.isfile(str(media_path)):
        if _send_photo(
            token,
            chat_id,
            str(media_path),
            _build_photo_caption(post),
            _build_reply_markup(post_id),
        ):
            return True

    try:
        data = urllib.parse.urlencode(
            {
                "chat_id": chat_id,
                "text": _build_card_text(post),
                "reply_markup": json.dumps(_build_reply_markup(post.id)),
                "disable_web_page_preview": "true",
            }
        ).encode()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as exc:
        # urllib exceptions can embed the request URL (which carries the token).
        safe = _redact(f"{type(exc).__name__}: {exc}", token)
        print(f"[social.notify] Telegram delivery failed for post {post_id}: {safe}")
        return False
