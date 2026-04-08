"""
Slack Direct Integration for The Homie.

Read channel messages and send notifications via Slack Bot Token.

Usage:
    uv run python -m integrations.slack_api channels
    uv run python -m integrations.slack_api messages general --hours 2
    uv run python -m integrations.slack_api send thehomie "Test message"
    uv run python -m integrations.slack_api check
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# Add parent dir for config imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import SLACK_BOT_TOKEN, SLACK_MONITORED_CHANNELS, SLACK_OWNER_USER_ID  # noqa: E402
from shared import with_retry  # noqa: E402

# Cache for user name resolution (user_id -> display_name)
_user_name_cache: dict[str, str] = {}


@dataclass
class SlackMessage:
    """Represents a Slack message."""

    ts: str
    user_id: str
    user_name: str | None
    text: str
    channel: str
    thread_ts: str | None = None


def get_slack_client() -> Any:
    """Create authenticated Slack WebClient."""
    from slack_sdk import WebClient

    if not SLACK_BOT_TOKEN:
        raise ValueError(
            "SLACK_BOT_TOKEN not set in .env\n"
            "Create a Slack app at https://api.slack.com/apps and add Bot Token"
        )

    client: Any = WebClient(token=SLACK_BOT_TOKEN)
    return client


def get_channel_id(channel_name: str) -> str | None:
    """Resolve a channel name (without #) to its ID."""
    client = get_slack_client()
    name = channel_name.lstrip("#")

    try:
        cursor: str | None = None
        while True:
            kwargs: dict[str, Any] = {"types": "public_channel,private_channel", "limit": 200}
            if cursor:
                kwargs["cursor"] = cursor

            result = with_retry(lambda kw=kwargs: client.conversations_list(**kw))
            channels: list[dict[str, Any]] = result.get("channels", [])

            for ch in channels:
                if ch.get("name") == name:
                    channel_id: str = ch["id"]
                    return channel_id

            # Check pagination
            metadata = result.get("response_metadata", {})
            cursor = metadata.get("next_cursor")
            if not cursor:
                break

    except Exception as e:
        print(f"Error listing channels: {e}")

    return None


def resolve_user_name(user_id: str) -> str:
    """Resolve a Slack user ID to display name (with caching)."""
    if user_id in _user_name_cache:
        return _user_name_cache[user_id]

    try:
        client = get_slack_client()
        result = with_retry(lambda: client.users_info(user=user_id))
        user_data: dict[str, Any] = result.get("user", {})
        profile: dict[str, str] = user_data.get("profile", {})
        name = profile.get("display_name") or profile.get("real_name") or user_id
        _user_name_cache[user_id] = name
        return name
    except Exception:
        _user_name_cache[user_id] = user_id
        return user_id


def get_recent_messages(
    channel_id: str,
    hours_ago: int = 2,
    limit: int = 20,
) -> list[SlackMessage]:
    """
    Get recent messages from a channel.

    Args:
        channel_id: Channel ID (not name — use get_channel_id() to resolve)
        hours_ago: How far back to look
        limit: Max messages to return
    """
    client = get_slack_client()

    oldest = str(time.time() - (hours_ago * 3600))

    try:
        result = with_retry(lambda: client.conversations_history(channel=channel_id, oldest=oldest, limit=limit))
        raw_messages: list[dict[str, Any]] = result.get("messages", [])

        messages: list[SlackMessage] = []
        for msg in raw_messages:
            # Skip bot messages and system messages
            if msg.get("subtype") in ("bot_message", "channel_join", "channel_leave"):
                continue

            user_id = msg.get("user", "")
            user_name = resolve_user_name(user_id) if user_id else None

            messages.append(
                SlackMessage(
                    ts=msg.get("ts", ""),
                    user_id=user_id,
                    user_name=user_name,
                    text=msg.get("text", ""),
                    channel=channel_id,
                    thread_ts=msg.get("thread_ts"),
                )
            )

        return messages
    except Exception as e:
        print(f"Error fetching messages: {e}")
        return []


def send_notification(
    channel: str, text: str, thread_ts: str | None = None
) -> dict[str, Any] | None:
    """
    Send a message to a Slack channel.

    Args:
        channel: Channel name (with or without #) or channel ID
        text: Message text (supports Slack markdown)
        thread_ts: Optional thread timestamp to reply in a thread

    Returns:
        Dict with 'channel' (resolved ID) and 'ts' (message timestamp), or None on failure.
    """
    client = get_slack_client()

    # Resolve channel name to ID if needed
    target = channel
    if not target.startswith("C") and not target.startswith("D"):
        resolved = get_channel_id(target)
        if not resolved:
            print(f"Channel not found: {channel}")
            return None
        target = resolved

    try:
        kwargs: dict[str, Any] = {"channel": target, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts

        response = with_retry(lambda: client.chat_postMessage(**kwargs))
        return {"channel": response.get("channel", target), "ts": response.get("ts", "")}
    except Exception as e:
        print(f"Error sending message: {e}")
        return None


def check_for_important_messages(
    channels: list[str] | None = None,
    hours_ago: int = 2,
) -> list[SlackMessage]:
    """
    Check monitored channels for important messages.

    Args:
        channels: Channel names to check (defaults to SLACK_MONITORED_CHANNELS)
        hours_ago: How far back to look
    """
    channel_names = channels or SLACK_MONITORED_CHANNELS
    all_messages: list[SlackMessage] = []

    for ch_name in channel_names:
        ch_name = ch_name.strip()
        ch_id = get_channel_id(ch_name)
        if not ch_id:
            print(f"Warning: Could not find channel #{ch_name}")
            continue

        messages = get_recent_messages(ch_id, hours_ago=hours_ago, limit=10)

        # Filter for potentially important messages (mentions owner or keywords)
        for msg in messages:
            is_important = False

            # Direct mentions of owner
            if SLACK_OWNER_USER_ID and f"<@{SLACK_OWNER_USER_ID}>" in msg.text:
                is_important = True

            # @here or @channel
            if "<!here>" in msg.text or "<!channel>" in msg.text:
                is_important = True

            if is_important:
                all_messages.append(msg)

    return all_messages


def format_messages_for_context(messages: list[SlackMessage], max_chars: int = 2000) -> str:
    """Format messages for inclusion in Claude's context prompt."""
    if not messages:
        return "No messages found."

    output: list[str] = []
    chars = 0

    for msg in messages:
        dt = datetime.fromtimestamp(float(msg.ts))
        time_str = dt.strftime("%H:%M")
        name = msg.user_name or msg.user_id

        entry = f"- [{time_str}] **{name}**: {msg.text[:200]}"

        if chars + len(entry) > max_chars:
            remaining = len(messages) - len(output)
            output.append(f"\n... and {remaining} more messages")
            break

        output.append(entry)
        chars += len(entry)

    return "\n\n".join(output)


# CLI for testing
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Slack integration")
    parser.add_argument("command", choices=["messages", "channels", "send", "check"])
    parser.add_argument("channel", nargs="?", default=None, help="Channel name for messages/send")
    parser.add_argument("message", nargs="?", default=None, help="Message text for send")
    parser.add_argument("--hours", type=int, default=2)
    parser.add_argument("--limit", type=int, default=20)

    args = parser.parse_args()

    if args.command == "channels":
        slack_client = get_slack_client()
        result_data = slack_client.conversations_list(types="public_channel", limit=100)
        for ch in result_data.get("channels", []):
            print(f"  #{ch['name']} ({ch['id']})")

    elif args.command == "messages":
        if not args.channel:
            print("Channel name required: slack_api.py messages <channel> [--hours N]")
            sys.exit(1)
        ch_id = get_channel_id(args.channel)
        if not ch_id:
            print(f"Channel not found: {args.channel}")
            sys.exit(1)
        msgs = get_recent_messages(ch_id, hours_ago=args.hours, limit=args.limit)
        print(format_messages_for_context(msgs))

    elif args.command == "send":
        if not args.channel or not args.message:
            print("Usage: slack_api.py send <channel> <message>")
            sys.exit(1)
        result = send_notification(args.channel, args.message)
        print(f"Sent! (ts={result['ts']})" if result else "Failed to send")

    elif args.command == "check":
        important = check_for_important_messages(hours_ago=args.hours)
        if important:
            print(f"Found {len(important)} important messages:")
            print(format_messages_for_context(important))
        else:
            print("No important messages found")
