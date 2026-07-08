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

if TYPE_CHECKING:
    from social.models import SocialPost

# Telegram hard limits.
_TG_TEXT_LIMIT = 4096
# callback_data is capped at 64 bytes; "social:approve:<id>" is tiny, so the
# bot's hashed-callback map is never engaged and the custom_id arrives intact.


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


def _build_card_text(post: "SocialPost") -> str:
    """Compose the paste-ready draft card. Plain text (no parse_mode) so
    arbitrary generated content can never break Telegram entity parsing."""
    channel = (post.channel or "social").upper()
    source = post.topic_source or "manual"
    header = f"📝 New {channel} draft  ·  #{post.id}  ·  {source}"
    body = post.body or "(empty draft)"
    footer = "Tap Approve & Post to publish, Edit to tweak, or Reject."

    # Reserve room for header/footer/separators inside the 4096 limit.
    overhead = len(header) + len(footer) + 8
    budget = _TG_TEXT_LIMIT - overhead
    if budget > 0 and len(body) > budget:
        body = body[: budget - 1].rstrip() + "…"

    card = f"{header}\n\n{body}\n\n{footer}"
    # Hard cap — header components (channel/source) are not length-bounded, so
    # guarantee the final string never exceeds the Telegram limit regardless.
    return card[:_TG_TEXT_LIMIT]


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


def deliver_draft_to_telegram(post: "SocialPost") -> bool:
    """Send the draft card with inline buttons to the operator's Telegram.

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
