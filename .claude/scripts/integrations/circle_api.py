"""
Circle Direct Integration for The Homie (READ-ONLY proof-of-concept).

Uses two APIs:
  - Admin API v2 (Bearer token): community-level reads (posts, spaces, members)
  - Headless Member API (JWT): user-level reads (DMs, notifications)

Usage:
    uv run python -m integrations.circle_api spaces
    uv run python -m integrations.circle_api posts <space_id> [--max 10]
    uv run python -m integrations.circle_api post <post_id>
    uv run python -m integrations.circle_api dms [--max 10]
    uv run python -m integrations.circle_api dm <chat_room_uuid>
    uv run python -m integrations.circle_api notifications [--max 10]
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# Add parent dir for config imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import (  # noqa: E402
    CIRCLE_ADMIN_TOKEN,
    CIRCLE_HEADLESS_TOKEN,
    CIRCLE_MEMBER_EMAIL,
    LOCAL_TZ,
)
from shared import with_retry  # noqa: E402

# === Constants ===

ADMIN_BASE_URL = "https://app.circle.so/api/admin/v2"
HEADLESS_AUTH_URL = "https://app.circle.so/api/v1/headless/auth_token"
HEADLESS_BASE_URL = "https://app.circle.so/api/headless/v1"

# === JWT Token Cache ===

_jwt_cache: dict[str, Any] = {
    "access_token": None,
    "refresh_token": None,
    "expires_at": 0,
}


# === Data Classes ===


@dataclass
class CircleSpace:
    """Represents a Circle space."""

    id: int
    name: str
    slug: str
    space_type: str = ""
    post_count: int = 0


@dataclass
class CirclePost:
    """Represents a Circle post."""

    id: int
    name: str
    body_plain: str = ""
    status: str = ""
    space_name: str = ""
    author_name: str = ""
    created_at: str = ""
    comments_count: int = 0
    likes_count: int = 0
    url: str = ""


@dataclass
class CircleChatRoom:
    """Represents a Circle DM or group chat room."""

    uuid: str
    kind: str  # 'direct' or 'group_chat'
    name: str = ""
    participants: list[str] = field(default_factory=list)
    unread_count: int = 0
    last_message_preview: str = ""
    last_message_sender: str = ""
    last_message_at: str = ""


@dataclass
class CircleMessage:
    """Represents a message in a chat room."""

    id: int
    body_plain: str = ""
    sender_name: str = ""
    sender_id: int = 0
    sent_at: str = ""
    replies_count: int = 0


@dataclass
class CircleNotification:
    """Represents a Circle notification."""

    id: int
    action: str = ""
    display_action: str = ""
    actor_name: str = ""
    notifiable_title: str = ""
    space_title: str = ""
    read: bool = False
    created_at: str = ""
    url: str = ""


# === HTTP Helpers ===


def _get(url: str, headers: dict[str, str], params: dict[str, Any] | None = None) -> Any:
    """Make a GET request with retry logic (uses curl to avoid Cloudflare blocks)."""
    import subprocess
    import urllib.parse

    if params:
        query_string = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}
        )
        url = f"{url}?{query_string}"

    def do_request() -> Any:
        cmd = ["curl", "-s", "-f", "--max-time", "30", url]
        for k, v in headers.items():
            cmd.extend(["-H", f"{k}: {v}"])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
        if result.returncode != 0:
            raise RuntimeError(f"HTTP request failed (curl exit {result.returncode}): {result.stderr.strip()}")
        if not result.stdout.strip():
            return {}
        return json.loads(result.stdout)

    return with_retry(do_request)


def _post(url: str, headers: dict[str, str], data: dict[str, Any]) -> Any:
    """Make a POST request with retry logic (uses curl to avoid Cloudflare blocks)."""
    import subprocess

    body = json.dumps(data)

    def do_request() -> Any:
        cmd = ["curl", "-s", "-f", "--max-time", "30", "-X", "POST", url, "-d", body]
        for k, v in headers.items():
            cmd.extend(["-H", f"{k}: {v}"])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
        if result.returncode != 0:
            raise RuntimeError(f"HTTP request failed (curl exit {result.returncode}): {result.stderr.strip()}")
        return json.loads(result.stdout)

    return with_retry(do_request)


# === Admin API (Bearer token auth) ===


def _admin_headers() -> dict[str, str]:
    """Get headers for Admin API v2 requests."""
    if not CIRCLE_ADMIN_TOKEN:
        raise ValueError(
            "CIRCLE_ADMIN_TOKEN not set in .env\n"
            "Go to your Circle community > Developers > Tokens > create 'Admin V2' token"
        )
    return {
        "Authorization": f"Token {CIRCLE_ADMIN_TOKEN}",
        "Content-Type": "application/json",
    }


def get_spaces() -> list[CircleSpace]:
    """List all spaces in the community."""
    data = _get(f"{ADMIN_BASE_URL}/spaces", _admin_headers())

    spaces: list[CircleSpace] = []
    items = data if isinstance(data, list) else data.get("records", data.get("spaces", []))
    for s in items:
        spaces.append(
            CircleSpace(
                id=s.get("id", 0),
                name=s.get("name", ""),
                slug=s.get("slug", ""),
                space_type=s.get("space_type", s.get("type", "")),
                post_count=s.get("post_count", 0),
            )
        )
    return spaces


def get_posts(space_id: int, max_results: int = 10, status: str = "published") -> list[CirclePost]:
    """List posts in a space via Admin API."""
    params: dict[str, Any] = {
        "space_id": space_id,
        "per_page": max_results,
        "page": 1,
        "status": status,
    }
    data = _get(f"{ADMIN_BASE_URL}/posts", _admin_headers(), params)

    posts: list[CirclePost] = []
    items = data if isinstance(data, list) else data.get("records", [])
    for p in items:
        # Admin API has user_name at top level, body is nested dict
        body = p.get("body", {})
        body_text = ""
        if isinstance(body, dict):
            body_text = _extract_plain_text(body.get("body", {}))
        elif isinstance(body, str):
            body_text = body

        posts.append(
            CirclePost(
                id=p.get("id", 0),
                name=p.get("name", ""),
                body_plain=body_text,
                status=p.get("status", ""),
                space_name=p.get("space_name", ""),
                author_name=p.get("user_name", ""),
                created_at=p.get("created_at", ""),
                comments_count=p.get("comments_count", 0),
                likes_count=p.get("likes_count", 0),
                url=p.get("url", ""),
            )
        )
    return posts


def get_post(post_id: int) -> CirclePost | None:
    """Get a single post by ID via Admin API."""
    try:
        data = _get(f"{ADMIN_BASE_URL}/posts/{post_id}", _admin_headers())
    except Exception as e:
        print(f"Error fetching post {post_id}: {e}")
        return None

    p = data if isinstance(data, dict) else {}
    body = p.get("body", {})
    body_text = ""
    if isinstance(body, dict):
        body_text = _extract_plain_text(body.get("body", {}))
    elif isinstance(body, str):
        body_text = body

    return CirclePost(
        id=p.get("id", 0),
        name=p.get("name", ""),
        body_plain=body_text,
        status=p.get("status", ""),
        space_name=p.get("space_name", ""),
        author_name=p.get("user_name", ""),
        created_at=p.get("created_at", ""),
        comments_count=p.get("comments_count", 0),
        likes_count=p.get("likes_count", 0),
        url=p.get("url", ""),
    )


def search_posts(query: str, max_results: int = 10) -> list[CirclePost]:
    """Search posts via Admin API advanced search."""
    params: dict[str, Any] = {
        "query": query,
        "per_page": max_results,
        "resource_type": "post",
    }
    try:
        data = _get(f"{ADMIN_BASE_URL}/advanced_search", _admin_headers(), params)
    except Exception as e:
        print(f"Search error: {e}")
        return []

    posts: list[CirclePost] = []
    items = data if isinstance(data, list) else data.get("records", [])
    for p in items:
        posts.append(
            CirclePost(
                id=p.get("id", 0),
                name=p.get("name", p.get("title", "")),
                body_plain=_extract_plain_text(p.get("body", {})),
                status=p.get("status", ""),
                space_name=p.get("space_name", ""),
                author_name=p.get("author_name", ""),
                created_at=p.get("created_at", ""),
                url=p.get("url", ""),
            )
        )
    return posts


# === Headless Member API (JWT auth) ===


def _get_member_jwt() -> str:
    """Get a JWT access token for the Member API, using cache when possible."""
    # Return cached token if still valid (with 60s buffer)
    if _jwt_cache["access_token"] and time.time() < _jwt_cache["expires_at"] - 60:
        return _jwt_cache["access_token"]

    if not CIRCLE_HEADLESS_TOKEN:
        raise ValueError(
            "CIRCLE_HEADLESS_TOKEN not set in .env\n"
            "Go to your Circle community > Developers > Tokens > create 'Headless Auth' token"
        )

    headers = {
        "Authorization": f"Bearer {CIRCLE_HEADLESS_TOKEN}",
        "Content-Type": "application/json",
    }
    data = _post(HEADLESS_AUTH_URL, headers, {"email": CIRCLE_MEMBER_EMAIL})

    _jwt_cache["access_token"] = data["access_token"]
    _jwt_cache["refresh_token"] = data.get("refresh_token")

    # Parse expiry or default to 50 minutes from now
    expires_str = data.get("access_token_expires_at", "")
    if expires_str:
        try:
            expires_dt = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
            _jwt_cache["expires_at"] = expires_dt.timestamp()
        except (ValueError, TypeError):
            _jwt_cache["expires_at"] = time.time() + 3000
    else:
        _jwt_cache["expires_at"] = time.time() + 3000

    return _jwt_cache["access_token"]


def _member_headers() -> dict[str, str]:
    """Get headers for Member API requests (JWT auth)."""
    token = _get_member_jwt()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def get_chat_rooms(max_results: int = 20) -> list[CircleChatRoom]:
    """List DM / group chat rooms for the authenticated member."""
    params: dict[str, Any] = {"per_page": max_results}
    try:
        data = _get(f"{HEADLESS_BASE_URL}/messages", _member_headers(), params)
    except Exception as e:
        print(f"Error fetching chat rooms: {e}")
        return []

    rooms: list[CircleChatRoom] = []
    items = data if isinstance(data, list) else data.get("records", [])
    for r in items:
        # Participants from other_participants_preview
        participants = []
        for p in r.get("other_participants_preview", []):
            if isinstance(p, dict):
                participants.append(p.get("name", str(p.get("community_member_id", ""))))

        # Last message preview
        last_msg = r.get("last_message", {})
        last_preview = ""
        last_sender = ""
        last_at = ""
        if isinstance(last_msg, dict):
            last_preview = last_msg.get("body", "")[:100]
            last_at = last_msg.get("sent_at", last_msg.get("created_at", ""))
            sender = last_msg.get("sender", {})
            if isinstance(sender, dict):
                last_sender = sender.get("name", "")

        rooms.append(
            CircleChatRoom(
                uuid=str(r.get("uuid", r.get("id", ""))),
                kind=r.get("chat_room_kind", ""),
                name=r.get("chat_room_name", ""),
                participants=participants,
                unread_count=r.get("unread_messages_count", 0),
                last_message_preview=last_preview,
                last_message_sender=last_sender,
                last_message_at=last_at,
            )
        )
    return rooms


def get_chat_messages(chat_room_uuid: str, max_results: int = 20) -> list[CircleMessage]:
    """Read messages from a specific chat room (DM or group)."""
    params: dict[str, Any] = {"per_page": max_results}
    try:
        data = _get(
            f"{HEADLESS_BASE_URL}/messages/{chat_room_uuid}/chat_room_messages",
            _member_headers(),
            params,
        )
    except Exception as e:
        print(f"Error fetching messages for room {chat_room_uuid}: {e}")
        return []

    messages: list[CircleMessage] = []
    items = data if isinstance(data, list) else data.get("records", [])
    for m in items:
        sender = m.get("sender", {})
        sender_name = ""
        sender_id = 0
        if isinstance(sender, dict):
            sender_name = sender.get("name", "")
            sender_id = sender.get("community_member_id", sender.get("id", 0))

        # body can be plain string or TipTap JSON
        body = m.get("body", "")

        messages.append(
            CircleMessage(
                id=m.get("id", 0),
                body_plain=body if isinstance(body, str) else _extract_plain_text(body),
                sender_name=sender_name,
                sender_id=int(sender_id) if sender_id else 0,
                sent_at=m.get("sent_at", m.get("created_at", "")),
                replies_count=m.get("replies_count", 0),
            )
        )
    return messages


def get_notifications(max_results: int = 20) -> list[CircleNotification]:
    """Get notifications for the authenticated member."""
    params: dict[str, Any] = {"per_page": max_results}
    try:
        data = _get(f"{HEADLESS_BASE_URL}/notifications", _member_headers(), params)
    except Exception as e:
        print(f"Error fetching notifications: {e}")
        return []

    notifications: list[CircleNotification] = []
    items = data if isinstance(data, list) else data.get("records", [])
    for n in items:
        notifications.append(
            CircleNotification(
                id=n.get("id", 0),
                action=n.get("action", ""),
                display_action=n.get("display_action", ""),
                actor_name=n.get("actor_name", ""),
                notifiable_title=n.get("notifiable_title", ""),
                space_title=n.get("space_title", ""),
                read=n.get("read_at") is not None,
                created_at=n.get("created_at", ""),
                url=n.get("action_web_url", ""),
            )
        )
    return notifications


def get_member_posts(max_results: int = 10) -> list[CirclePost]:
    """Get posts from the member's home feed."""
    params: dict[str, Any] = {"per_page": max_results}
    try:
        data = _get(f"{HEADLESS_BASE_URL}/home", _member_headers(), params)
    except Exception as e:
        print(f"Error fetching home feed: {e}")
        return []

    posts: list[CirclePost] = []
    items = data if isinstance(data, list) else data.get("records", [])
    for p in items:
        author = p.get("author", {})
        author_name = ""
        if isinstance(author, dict):
            author_name = author.get("name", "")

        space = p.get("space", {})
        space_name = ""
        if isinstance(space, dict):
            space_name = space.get("name", "")

        posts.append(
            CirclePost(
                id=p.get("id", 0),
                name=p.get("name", p.get("display_title", "")),
                body_plain=p.get("body_plain_text", "") or _extract_plain_text(p.get("body", {})),
                status=p.get("status", ""),
                space_name=space_name,
                author_name=author_name,
                created_at=p.get("created_at", ""),
                comments_count=p.get("comment_count", 0),
                likes_count=p.get("user_likes_count", 0),
                url=p.get("url", ""),
            )
        )
    return posts


# === Draft Support Functions ===


# Cole's community_member_id — imported from config at module level
try:
    from config import CIRCLE_COMMUNITY_MEMBER_ID
except ImportError:
    CIRCLE_COMMUNITY_MEMBER_ID = 36097714


def check_dm_reply(chat_room_uuid: str, after_timestamp: str) -> str | None:
    """
    Check if Cole sent a message in a Circle DM after a given time.

    Args:
        chat_room_uuid: The chat room UUID
        after_timestamp: ISO format timestamp — only look for replies after this time

    Returns:
        Cole's reply text if found, None otherwise.
    """
    messages = get_chat_messages(chat_room_uuid, max_results=10)
    after_dt = datetime.fromisoformat(after_timestamp)

    for msg in messages:
        if msg.sender_id != CIRCLE_COMMUNITY_MEMBER_ID:
            continue

        if msg.sent_at:
            try:
                msg_dt = datetime.fromisoformat(msg.sent_at.replace("Z", "+00:00"))
                if msg_dt.replace(tzinfo=None) > after_dt.replace(tzinfo=None):
                    return msg.body_plain
            except (ValueError, TypeError):
                continue

    return None


def check_post_reply(post_id: int, after_timestamp: str) -> str | None:
    """
    Check if Cole commented on a Circle post after a given time.

    Uses Admin API v2 to fetch comments on a post and look for Cole's.

    Args:
        post_id: The post ID
        after_timestamp: ISO format timestamp

    Returns:
        Cole's comment text if found, None otherwise.
    """
    try:
        data = _get(
            f"{ADMIN_BASE_URL}/comments",
            _admin_headers(),
            {"post_id": post_id, "per_page": 20},
        )
    except Exception as e:
        print(f"Error fetching comments for post {post_id}: {e}")
        return None

    after_dt = datetime.fromisoformat(after_timestamp)
    items = data if isinstance(data, list) else data.get("records", [])

    for c in items:
        # Admin API v2 comments nest author under "user" with a different ID space
        # than the headless API (user.id vs community_member_id).
        # Match by email (CIRCLE_MEMBER_EMAIL) for reliability across API versions.
        user = c.get("user", {})
        user_email = user.get("email", "")
        if user_email != CIRCLE_MEMBER_EMAIL:
            continue

        created = c.get("created_at", "")
        if created:
            try:
                comment_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                if comment_dt.replace(tzinfo=None) > after_dt.replace(tzinfo=None):
                    body = c.get("body", {})
                    return _extract_plain_text(body) if isinstance(body, dict) else str(body)
            except (ValueError, TypeError):
                continue

    return None


def get_unreplied_dms(max_results: int = 20) -> list[CircleChatRoom]:
    """
    Get DM threads where the last message is NOT from Cole.

    Returns chat rooms where Cole needs to respond.
    """
    rooms = get_chat_rooms(max_results=max_results)
    unreplied: list[CircleChatRoom] = []

    for room in rooms:
        if room.kind != "direct":
            continue

        # Get recent messages to check who sent the last one
        messages = get_chat_messages(room.uuid, max_results=3)
        if not messages:
            continue

        # Messages are typically ordered newest first
        last_msg = messages[0]
        if last_msg.sender_id != CIRCLE_COMMUNITY_MEMBER_ID:
            unreplied.append(room)

    return unreplied


def get_unreplied_posts(space_id: int, max_results: int = 10) -> list[CirclePost]:
    """
    Get recent posts in a space that Cole hasn't commented on.

    Args:
        space_id: The space ID to check
        max_results: Maximum posts to check

    Returns:
        List of posts Cole hasn't commented on.
    """
    posts = get_posts(space_id, max_results=max_results)
    unreplied: list[CirclePost] = []

    for post in posts:
        # Skip posts authored by Cole himself
        if post.author_name and "cole" in post.author_name.lower():
            continue

        # Check if Cole has commented
        cole_reply = check_post_reply(post.id, "2000-01-01T00:00:00")
        if cole_reply is None:
            unreplied.append(post)

    return unreplied


# === Formatting ===


def _strip_html(html: str) -> str:
    """Strip HTML tags and decode common entities to get plain text."""
    import re

    # Remove HTML tags
    text = re.sub(r"<[^>]+>", " ", html)
    # Decode common HTML entities
    for entity, char in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                         ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")]:
        text = text.replace(entity, char)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_plain_text(body: Any) -> str:
    """Extract plain text from a TipTap body object, HTML string, or plain string."""
    if isinstance(body, str):
        # Check if it looks like HTML (Admin API returns HTML in body.body)
        if "<" in body and ">" in body:
            return _strip_html(body)
        return body
    if isinstance(body, dict):
        # Admin API v2 comment bodies use a wrapper: {"body": "<html>...", ...}
        # Check for nested HTML body first before trying TipTap walk
        inner_body = body.get("body", "")
        if isinstance(inner_body, str) and inner_body:
            if "<" in inner_body and ">" in inner_body:
                return _strip_html(inner_body)
            return inner_body
        # TipTap JSON format — walk the tree
        text_parts: list[str] = []
        _walk_tiptap(body, text_parts)
        return " ".join(text_parts).strip()
    return ""


def _walk_tiptap(node: dict[str, Any], parts: list[str]) -> None:
    """Recursively extract text from TipTap JSON."""
    if node.get("type") == "text":
        parts.append(node.get("text", ""))
    for child in node.get("content", []):
        if isinstance(child, dict):
            _walk_tiptap(child, parts)


def _extract_message_preview(room: dict[str, Any]) -> str:
    """Extract a preview of the last message in a chat room."""
    last_msg = room.get("last_message", room.get("latest_message", {}))
    if isinstance(last_msg, dict):
        return _extract_plain_text(last_msg.get("body", ""))[:100]
    return ""


def format_spaces_for_context(spaces: list[CircleSpace]) -> str:
    """Format spaces for display."""
    if not spaces:
        return "No spaces found."
    lines: list[str] = []
    for s in spaces:
        type_label = f" ({s.space_type})" if s.space_type else ""
        posts_label = f" — {s.post_count} posts" if s.post_count else ""
        lines.append(f"- **{s.name}**{type_label} (ID: {s.id}){posts_label}")
    return "\n".join(lines)


def format_posts_for_context(posts: list[CirclePost], max_chars: int = 3000) -> str:
    """Format posts for display."""
    if not posts:
        return "No posts found."
    lines: list[str] = []
    chars = 0
    for p in posts:
        author = f" by {p.author_name}" if p.author_name else ""
        space = f" in {p.space_name}" if p.space_name else ""
        date = ""
        if p.created_at:
            try:
                dt = datetime.fromisoformat(p.created_at.replace("Z", "+00:00")).astimezone(LOCAL_TZ)
                date = f" ({dt.strftime('%Y-%m-%d %H:%M')})"
            except (ValueError, TypeError):
                date = f" ({p.created_at})"

        preview = p.body_plain[:200] + "..." if len(p.body_plain) > 200 else p.body_plain
        entry = f"- **{p.name}**{author}{space}{date}\n  {preview}\n  Comments: {p.comments_count} | Likes: {p.likes_count}"
        if p.url:
            entry += f" | {p.url}"

        if chars + len(entry) > max_chars:
            remaining = len(posts) - len(lines)
            lines.append(f"\n... and {remaining} more posts")
            break
        lines.append(entry)
        chars += len(entry)
    return "\n\n".join(lines)


def format_chat_rooms_for_context(rooms: list[CircleChatRoom]) -> str:
    """Format chat rooms (DMs) for display."""
    if not rooms:
        return "No DMs or chat rooms found."
    lines: list[str] = []
    for r in rooms:
        kind_label = "DM" if r.kind == "direct" else "Group Chat"
        name = r.name or ", ".join(r.participants[:3]) or "Unknown"
        unread = f" [{r.unread_count} unread]" if r.unread_count else ""
        preview = ""
        if r.last_message_preview:
            sender = f"**{r.last_message_sender}**: " if r.last_message_sender else ""
            preview = f"\n  Last: {sender}{r.last_message_preview}"
        lines.append(f"- [{kind_label}] **{name}**{unread} (UUID: {r.uuid}){preview}")
    return "\n".join(lines)


def format_messages_for_context(messages: list[CircleMessage], max_chars: int = 3000) -> str:
    """Format chat messages for display."""
    if not messages:
        return "No messages found."
    lines: list[str] = []
    chars = 0
    for m in messages:
        time_str = ""
        if m.sent_at:
            try:
                dt = datetime.fromisoformat(m.sent_at.replace("Z", "+00:00")).astimezone(LOCAL_TZ)
                time_str = f"[{dt.strftime('%Y-%m-%d %H:%M')}] "
            except (ValueError, TypeError):
                time_str = f"[{m.sent_at}] "

        sender = m.sender_name or "Unknown"
        reply_indicator = f" ({m.replies_count} replies)" if m.replies_count else ""
        entry = f"- {time_str}**{sender}**: {m.body_plain}{reply_indicator}"

        if chars + len(entry) > max_chars:
            remaining = len(messages) - len(lines)
            lines.append(f"\n... and {remaining} more messages")
            break
        lines.append(entry)
        chars += len(entry)
    return "\n".join(lines)


def format_notifications_for_context(notifications: list[CircleNotification]) -> str:
    """Format notifications for display."""
    if not notifications:
        return "No notifications found."
    lines: list[str] = []
    for n in notifications:
        read_marker = "" if n.read else " [UNREAD]"
        date = ""
        if n.created_at:
            try:
                dt = datetime.fromisoformat(n.created_at.replace("Z", "+00:00"))
                date = f" ({dt.strftime('%Y-%m-%d %H:%M')})"
            except (ValueError, TypeError):
                date = f" ({n.created_at})"

        # Build: "Stratos commented on your post: Hackathon Winners"
        summary = f"**{n.actor_name}** {n.display_action} {n.notifiable_title}"
        space = f" in {n.space_title}" if n.space_title else ""
        entry = f"- {summary}{space}{read_marker}{date}"
        if n.url:
            entry += f"\n  {n.url}"
        lines.append(entry)
    return "\n".join(lines)


# === CLI ===


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Circle integration (read-only)")
    parser.add_argument(
        "command",
        choices=["spaces", "posts", "post", "search", "dms", "dm", "notifications", "feed"],
    )
    parser.add_argument("target", nargs="?", default=None, help="space_id, post_id, chat_room_uuid, or search query")
    parser.add_argument("--max", type=int, default=10)
    parser.add_argument("--status", default="published", choices=["published", "draft", "all"])

    args = parser.parse_args()

    if args.command == "spaces":
        result = get_spaces()
        print(format_spaces_for_context(result))

    elif args.command == "posts":
        if not args.target:
            print("Error: space_id required. Run 'circle spaces' first to get IDs.")
            sys.exit(1)
        result = get_posts(int(args.target), max_results=args.max, status=args.status)
        print(format_posts_for_context(result))

    elif args.command == "post":
        if not args.target:
            print("Error: post_id required")
            sys.exit(1)
        post = get_post(int(args.target))
        if post:
            print(format_posts_for_context([post]))
        else:
            print("Post not found")

    elif args.command == "search":
        if not args.target:
            print("Error: search query required")
            sys.exit(1)
        result = search_posts(args.target, max_results=args.max)
        print(format_posts_for_context(result))

    elif args.command == "dms":
        result = get_chat_rooms(max_results=args.max)
        print(format_chat_rooms_for_context(result))

    elif args.command == "dm":
        if not args.target:
            print("Error: chat_room_uuid required. Run 'circle dms' first to get UUIDs.")
            sys.exit(1)
        result = get_chat_messages(args.target, max_results=args.max)
        print(format_messages_for_context(result))

    elif args.command == "notifications":
        result = get_notifications(max_results=args.max)
        print(format_notifications_for_context(result))

    elif args.command == "feed":
        result = get_member_posts(max_results=args.max)
        print(format_posts_for_context(result))
