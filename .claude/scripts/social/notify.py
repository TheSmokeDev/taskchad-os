"""Deliver a generated social draft to the operator's Telegram with inline
approve / edit / reject buttons.

Cross-process safe: the cadence cron runs in a SEPARATE process from the
chat bot, so this posts directly to the Telegram Bot API (same pattern as
``upstream_watch._send_telegram``). The button tap then routes back into the
running bot's existing callback pipeline as ``__button:social:<action>:<id>``.

Fail-open contract: a delivery failure NEVER breaks draft generation. Every
path returns a bool and swallows its own exceptions — the draft is already
persisted in the queue DB before this is ever called.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from social.channels import get_channel
from social.models import SocialPost, approval_binding_digest


class ReviewTransportError(RuntimeError):
    def __init__(self, message: str, *, uncertain: bool = True) -> None:
        super().__init__(message)
        self.uncertain = uncertain


def _message_receipt(response) -> str:
    payload = json.loads(response.read())
    if payload.get("ok") is not True:
        raise ReviewTransportError("Telegram rejected the review message", uncertain=False)
    message_id = (payload.get("result") or {}).get("message_id")
    if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
        raise ReviewTransportError("Telegram returned no message ID")
    return str(message_id)

# Telegram hard limits.
_TG_TEXT_LIMIT = 4096
# A photo message's caption is capped far lower than a text message's body.
_TG_CAPTION_LIMIT = 1024
# Discord caps message content at 2000 characters.
_DISCORD_TEXT_LIMIT = 2000
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


def _build_card_text(post: SocialPost, limit: int = _TG_TEXT_LIMIT) -> str:
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
    footer = (
        "Tap Approve & Post to publish, Revise Copy or Redo Image to tweak, "
        "or Reject."
    )

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


def _build_photo_caption(post: SocialPost) -> str:
    """The draft card sized for a photo caption. Telegram caps captions at 1024
    UTF-16 code units (not code points), so a code-point cap alone can still
    overflow on emoji-heavy text — apply a UTF-16-aware final trim."""
    caption = _build_card_text(post, limit=_TG_CAPTION_LIMIT)
    return _utf16_truncate(caption, _TG_CAPTION_LIMIT)


def _build_reply_markup(post: SocialPost | int) -> dict:
    if isinstance(post, int):
        # Test/compatibility callers should pass the row.  Keep a bounded
        # fail-closed placeholder for integer-only construction; live delivery
        # always supplies the full SocialPost below.
        post_id = post
        revision = 1
        digest = "0" * 12
    else:
        post_id = post.id
        revision = post.revision
        digest = approval_binding_digest(post)

    def callback(action: str) -> str:
        return f"social:{action}:{post_id}:{revision}:{digest}"

    return {
        "inline_keyboard": [
            [{"text": "✅ Approve & Post", "callback_data": callback("approve")}],
            [
                {"text": "✏️ Revise Copy", "callback_data": callback("edit")},
                {"text": "🖼️ Redo Image", "callback_data": callback("image")},
            ],
            [
                {"text": "❌ Reject", "callback_data": callback("reject")},
            ],
        ]
    }


def _full_copy_messages(post: SocialPost) -> list[str]:
    """Return complete, UTF-16-safe review messages without truncating copy."""

    header = f"📝 FULL COPY · #{post.id} · revision {post.revision}"
    text = f"{header}\n\n{post.body or '(empty draft)'}"
    messages: list[str] = []
    remaining = text
    while remaining:
        chunk = _utf16_truncate(remaining, _TG_TEXT_LIMIT)
        if not chunk:
            break
        messages.append(chunk)
        remaining = remaining[len(chunk) :]
    return messages or [header]


def _review_messages(text: str) -> list[str]:
    """Split operator review text without silently losing evidence or copy."""

    messages: list[str] = []
    while text:
        chunk = _utf16_truncate(text, _TG_TEXT_LIMIT)
        if not chunk:
            break
        messages.append(chunk)
        text = text[len(chunk) :]
    return messages


def _authority_evidence_messages(post: SocialPost, package: dict) -> list[str]:
    """Render only the review fields, never the raw prompt or private context."""

    lines = [
        f"INTERNAL EVIDENCE · #{post.id} · revision {post.revision}",
        "Operator review only. This note is NOT part of the LinkedIn caption.",
        f"Format: {package.get('format', 'unconfigured')}",
        "Independent copy review: accepted. Image/caption review: accepted.",
    ]
    expires = package.get("source_expires_at")
    if expires:
        lines.append(f"Evidence expires: {expires}")
    for source in package.get("evidence_sources", []):
        # Explicit field selection keeps delivery separate from research and
        # model prompts, even if the stored package gains more internal fields.
        if not isinstance(source, dict):
            continue
        lines.extend(
            [
                "",
                f"Evidence {source.get('claim_index', '?')}: "
                f"{source.get('source_title', 'Untitled source')}",
                str(source.get("text", "")),
                str(source.get("source_url", "")),
                f"Date: {source.get('source_date') or 'not supplied'} · "
                f"Class: {source.get('source_class', 'unknown')} · "
                f"Confidence: {source.get('confidence', 'unknown')}",
            ]
        )
    cta = package.get("cta") or {}
    if cta.get("kind") == "resource_drop":
        lines.extend(
            [
                "",
                f"Resource: {cta.get('title', '')}",
                f"Artifact: {cta.get('resource_id', '')}",
                f"SHA-256: {cta.get('digest', '')}",
                "Delivery is manual; no automated DM was promised or sent.",
            ]
        )
    return _review_messages("\n".join(lines))


def _deliver_authority_review(
    post: SocialPost,
    *,
    token: str,
    chat_id: str,
    db_path: str | Path | None,
) -> bool:
    """Fail closed: evidence, complete public copy, image, then bound buttons."""

    from social.service import SocialPostService

    controls_issued = False
    try:
        svc = SocialPostService(db_path=db_path)

        def current_package() -> dict:
            current = svc.get_post(post.id)
            if (
                current is None
                or current.status != "draft"
                or current.revision != post.revision
                or approval_binding_digest(current) != approval_binding_digest(post)
                or current.body != post.body
                or current.media_path != post.media_path
            ):
                raise ValueError("Draft revision changed before review delivery")
            svc.assert_editorial_integrity(current)
            package = svc.get_editorial_package(post.id, post.revision)
            if not package:
                raise ValueError("Editorial package is missing")
            if (
                current.media_type != "image"
                or not current.media_path
                or not os.path.isfile(current.media_path)
            ):
                raise ValueError("Reviewed image is missing")
            return package

        package = current_package()
        for message in _authority_evidence_messages(post, package):
            if not _send_message(token, chat_id, message):
                return False
        for message in _review_messages(
            f"PUBLIC LINKEDIN CAPTION · #{post.id} · revision {post.revision}"
            f"\n\n{post.body}"
        ):
            if not _send_message(token, chat_id, message):
                return False
        if not _send_photo(
            token,
            chat_id,
            str(post.media_path),
            f"PUBLIC IMAGE · #{post.id} · revision {post.revision}",
            None,
        ):
            _send_media_review_blocked(token, chat_id, post, full_copy_sent=True)
            return False
        # A workshop revision or a changed file during Telegram upload must
        # never gain approval controls from the earlier snapshot.
        current_package()
        if not _send_message(
            token,
            chat_id,
            _review_control_text(post),
            reply_markup=_build_reply_markup(post),
        ):
            return False
        controls_issued = True
        return bool(svc.mark_editorial_delivered(post.id, post.revision))
    except Exception as exc:
        safe = _redact(f"{type(exc).__name__}: {exc}", token)
        print(f"[social.notify] Authority review blocked for post {post.id}: {safe}")
        notice = (
            f"Draft #{post.id} revision {post.revision} needs a fresh editorial "
            "and image review before approval. "
        )
        notice += (
            "The revision changed during delivery; reopen the current preview."
            if controls_issued
            else "No new approval controls were issued."
        )
        try:
            _send_message(token, chat_id, notice)
        except Exception:
            pass  # Best-effort notification may not mask the failed review.
        return False


def _review_control_text(post: SocialPost) -> str:
    return (
        f"Review controls for draft #{post.id} · revision {post.revision} · "
        f"{approval_binding_digest(post)}"
    )


def linkedin_review_messages(post: SocialPost, package: dict | None) -> list[str]:
    """One public-caption presentation for scheduler and conversational reviews."""
    from social.publishers import parse_publisher

    publisher = parse_publisher(getattr(post, "publisher_json", None))
    destination = (
        f"Publishing as {publisher.name} company page\n{publisher.url}"
        if publisher and publisher.kind == "organization"
        else "Publishing as your LinkedIn personal profile"
    )
    messages = []
    if package:
        if package.get("schema_version") == "company-editorial/v1":
            messages.extend(_review_messages(
                f"INTERNAL REVIEW · #{post.id} · revision {post.revision}\n"
                "Operator context only; not part of the public caption.\n"
                "Company copy and image reviewed together. No research statistics "
                "or private client results are being presented as company receipts."
            ))
        else:
            messages.extend(_authority_evidence_messages(post, package))
    messages.extend(_review_messages(
        f"{destination}\nPUBLIC LINKEDIN CAPTION · #{post.id} · revision {post.revision}"
        f"\n\n{post.body}"
    ))
    return messages


def _deliver_linkedin_review(
    post: SocialPost, *, token: str, chat_id: str,
    db_path: str | Path | None = None,
    reply_to_message_id: str | None = None,
    delivery_request_id: str | None = None,
) -> bool:
    from social.publishers import assert_publisher_matches_channel
    from social.review_delivery import ReviewDeliveryStore
    from social.service import SocialPostService

    try:
        svc = SocialPostService(db_path=db_path)
        binding = approval_binding_digest(post)

        def current_review():
            current = svc.get_post(post.id)
            if (not current or current.status != "draft"
                    or current.revision != post.revision
                    or current.channel != post.channel
                    or current.publisher_json != post.publisher_json
                    or current.body != post.body
                    or current.media_path != post.media_path
                    or approval_binding_digest(current) != binding):
                raise ValueError("Draft changed during review delivery")
            svc.assert_integrity(current.id)
            assert_publisher_matches_channel(current, get_channel(current.channel))
            if (current.media_type != "image" or not current.media_path
                    or not Path(current.media_path).is_file()):
                raise ValueError("LinkedIn approval requires a visible image")
            package = svc.get_editorial_package(current.id, current.revision)
            if package:
                svc.assert_editorial_integrity(current.id)
            return package

        package = current_review()
        store = ReviewDeliveryStore(svc.db_path)
        # Bot identity scopes receipts without ever persisting a credential.
        import hashlib

        bot_id = hashlib.sha256(token.encode()).hexdigest()[:16]
        # Scheduled retries share the empty request scope. A genuine explicit
        # reopen has a stable inbound message/callback ID and may reissue a
        # disabled old card, while replaying that same request remains a no-op.
        request_key = hashlib.sha256((delivery_request_id or "").encode()).hexdigest()[:16]
        recipient = f"telegram:{bot_id}:{chat_id}:{reply_to_message_id or ''}:{request_key}"
        key = (post.id, post.revision, binding, recipient)

        def send_step(step, send):
            state = store.claim(key, step)
            if state == "delivered":
                return
            if state != "send":
                raise ValueError("Review delivery is in flight or needs receipt reconciliation")
            try:
                message_id = send()
                if not isinstance(message_id, str) or not message_id.isdigit():
                    raise ReviewTransportError("Telegram returned no verified message ID")
                store.finish(key, step, message_id=message_id, status="delivered")
            except Exception as exc:
                status = (
                    "rejected" if isinstance(exc, ReviewTransportError)
                    and not exc.uncertain else "uncertain"
                )
                store.finish(key, step, message_id=None, status=status,
                             error=_redact(str(exc), token)[:500])
                raise

        for index, text in enumerate(linkedin_review_messages(post, package)):
            send_step(f"text:{index}", lambda text=text: _send_message(
                token, chat_id, text, require_receipt=True,
                reply_to_message_id=reply_to_message_id,
            ))
        send_step("image", lambda: _send_photo(
            token, chat_id, str(post.media_path),
            f"PUBLIC IMAGE · #{post.id} · revision {post.revision}", None,
            require_receipt=True, reply_to_message_id=reply_to_message_id,
        ))
        # Re-read row AND actual file bytes after upload, immediately before
        # issuing controls; stored hashes alone do not establish integrity.
        current_review()
        send_step("controls", lambda: _send_message(
            token, chat_id, _review_control_text(post),
            reply_markup=_build_reply_markup(post), require_receipt=True,
            reply_to_message_id=reply_to_message_id,
        ))
        if package:
            return bool(svc.mark_editorial_delivered(post.id, post.revision))
        return True
    except Exception as exc:
        safe = _redact(f"{type(exc).__name__}: {exc}", token)
        print(f"[social.notify] LinkedIn review blocked for post {post.id}: {safe}")
        return False


def _send_media_review_blocked(
    token: str,
    chat_id: str,
    post: SocialPost,
    *,
    full_copy_sent: bool,
) -> bool:
    """Show the copy but never attach approval controls to unseen media."""

    if not full_copy_sent:
        for message in _full_copy_messages(post):
            if not _send_message(token, chat_id, message):
                return False
    return _send_message(
        token,
        chat_id,
        (
            f"Media for draft #{post.id} revision {post.revision} could not be "
            "delivered. No approval control was issued. Redo the image or reopen "
            "the current revision after the media is visible."
        ),
        reply_markup=None,
    )


def _send_message(
    token: str,
    chat_id: str,
    text: str,
    *,
    reply_markup: dict | None = None,
    require_receipt: bool = False,
    reply_to_message_id: str | None = None,
) -> bool | str:
    fields: dict[str, str] = {
        "chat_id": chat_id,
        "text": _utf16_truncate(text, _TG_TEXT_LIMIT),
        "disable_web_page_preview": "true",
    }
    if reply_markup is not None:
        fields["reply_markup"] = json.dumps(reply_markup)
    if reply_to_message_id:
        fields["reply_to_message_id"] = str(reply_to_message_id)
    try:
        data = urllib.parse.urlencode(fields).encode()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        req = urllib.request.Request(url, data=data)
        response = urllib.request.urlopen(req, timeout=10)
        if require_receipt:
            return _message_receipt(response)
        return True
    except Exception as exc:
        safe = _redact(f"{type(exc).__name__}: {exc}", token)
        if require_receipt:
            if isinstance(exc, ReviewTransportError):
                raise
            raise ReviewTransportError(
                safe, uncertain=not (
                    isinstance(exc, urllib.error.HTTPError) and 400 <= exc.code < 500
                ),
            ) from None
        print(f"[social.notify] Telegram delivery failed: {safe}")
        return False


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


def send_text_to_discord(text: str, channel_id: str) -> bool:
    """Post a plain text message to a Discord channel via the REST API.

    Cross-process safe — the cron process has no gateway connection, so this
    hits ``POST /channels/{id}/messages`` directly with the bot token.
    Fail-open: returns False on any failure, never raises. The token is
    redacted from every error path before printing."""
    if not text or not channel_id:
        return False
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        return False
    try:
        data = json.dumps({"content": text[:_DISCORD_TEXT_LIMIT]}).encode("utf-8")
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        req = urllib.request.Request(url, data=data)
        req.add_header("Authorization", f"Bot {token}")
        req.add_header("Content-Type", "application/json")
        # Discord rejects UA-less requests.
        req.add_header("User-Agent", "DiscordBot (thehomie, 1.0)")
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as exc:
        safe = _redact(f"{type(exc).__name__}: {exc}", token)
        print(f"[social.notify] Discord send failed: {safe}")
        return False


def _send_photo(
    token: str,
    chat_id: str,
    image_path: str,
    caption: str,
    reply_markup: dict | None,
    *,
    require_receipt: bool = False,
    reply_to_message_id: str | None = None,
) -> bool | str:
    """Upload a local image as a Telegram photo with a caption + inline buttons.

    Returns False on any failure (unsupported type, unreadable/empty file,
    network error) so the caller can fall back to the text card. Never raises.
    Builds the multipart/form-data body with the stdlib only (no new deps),
    keeping the cross-process, dependency-light contract of this module."""
    ext = os.path.splitext(image_path)[1].lower()
    mime = _IMAGE_MIME.get(ext)
    if mime is None:
        if require_receipt:
            raise ReviewTransportError("Unsupported image type", uncertain=False)
        return False
    try:
        with open(image_path, "rb") as fh:
            photo_bytes = fh.read()
    except OSError:
        if require_receipt:
            raise ReviewTransportError("Image is unreadable", uncertain=False) from None
        return False
    if not photo_bytes:
        if require_receipt:
            raise ReviewTransportError("Image is empty", uncertain=False)
        return False

    boundary = "----HomieSocialNotify7f3a2b"
    parts: list[bytes] = []
    fields = [("chat_id", chat_id), ("caption", caption)]
    if reply_to_message_id:
        fields.append(("reply_to_message_id", str(reply_to_message_id)))
    if reply_markup is not None:
        fields.append(("reply_markup", json.dumps(reply_markup)))
    for name, value in fields:
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
        response = urllib.request.urlopen(req, timeout=30)
        if require_receipt:
            return _message_receipt(response)
        return True
    except Exception as exc:
        safe = _redact(f"{type(exc).__name__}: {exc}", token)
        if require_receipt:
            if isinstance(exc, ReviewTransportError):
                raise
            raise ReviewTransportError(
                safe, uncertain=not (
                    isinstance(exc, urllib.error.HTTPError) and 400 <= exc.code < 500
                ),
            ) from None
        print(f"[social.notify] Telegram photo send failed: {safe}")
        return False


def deliver_draft_to_telegram(
    post: SocialPost, *, db_path: str | Path | None = None,
    token: str | None = None, chat_id: str | None = None,
    reply_to_message_id: str | None = None,
    delivery_request_id: str | None = None,
) -> bool:
    """Send the draft card with inline buttons to the operator's Telegram.

    Authority drafts require their current, independently reviewed editorial
    package. They always send internal evidence, full public copy, public image,
    and controls separately. Legacy non-authority delivery is unchanged.

    Returns True on success, False on any failure (missing creds, network
    error, bad post). Never raises — delivery is best-effort and additive.
    """
    post_id = getattr(post, "id", 0)
    if post is None or not isinstance(post_id, int) or post_id <= 0:
        return False

    creds = (token, chat_id) if token and chat_id else _telegram_credentials()
    if creds is None:
        print("[social.notify] Telegram creds not configured; draft not delivered")
        return False
    token, chat_id = creds

    from social.publishers import is_linkedin_channel

    if is_linkedin_channel(post.channel):
        return _deliver_linkedin_review(
            post, token=token, chat_id=chat_id, db_path=db_path,
            reply_to_message_id=reply_to_message_id,
            delivery_request_id=delivery_request_id,
        )

    if post.topic_source == "authority_signal":
        return _deliver_authority_review(
            post, token=token, chat_id=chat_id, db_path=db_path
        )

    # Photo card first when a rendered image is attached; fail-open to text.
    media_path = getattr(post, "media_path", None)
    media_type = getattr(post, "media_type", None)
    if media_type == "image" and media_path:
        if not os.path.isfile(str(media_path)):
            return _send_media_review_blocked(
                token, chat_id, post, full_copy_sent=False
            )
        caption = _build_photo_caption(post)
        complete_card = _build_card_text(post, limit=10_000_000)
        caption_truncates = _utf16_len(complete_card) > _TG_CAPTION_LIMIT
        if caption_truncates:
            # Exact review order: complete copy, then the image, then the
            # revision/hash-bound controls.  The approval button is never
            # attached to a truncated caption.
            for message in _full_copy_messages(post):
                if not _send_message(token, chat_id, message):
                    return False
            media_caption = f"🖼️ Media for draft #{post.id} · revision {post.revision}"
            if _send_photo(
                token,
                chat_id,
                str(media_path),
                media_caption,
                None,
            ):
                return _send_message(
                    token,
                    chat_id,
                    _review_control_text(post),
                    reply_markup=_build_reply_markup(post),
                )
            return _send_media_review_blocked(
                token, chat_id, post, full_copy_sent=True
            )
        elif _send_photo(
            token,
            chat_id,
            str(media_path),
            caption,
            _build_reply_markup(post),
        ):
            return True
        return _send_media_review_blocked(
            token, chat_id, post, full_copy_sent=False
        )

    try:
        return _send_message(
            token,
            chat_id,
            _build_card_text(post),
            reply_markup=_build_reply_markup(post),
        )
    except Exception as exc:
        # urllib exceptions can embed the request URL (which carries the token).
        safe = _redact(f"{type(exc).__name__}: {exc}", token)
        print(f"[social.notify] Telegram delivery failed for post {post_id}: {safe}")
        return False
