"""Conversation identity helpers for persisted chat sessions."""

from __future__ import annotations


def resolve_thread_id(channel_id: str, thread_id: str | None = None) -> str:
    """Return the durable thread id, defaulting to the channel id."""

    return thread_id or channel_id


def build_session_key(platform: str, channel_id: str, thread_id: str | None = None) -> str:
    """Return the persisted session key used by the session store."""

    resolved_thread_id = resolve_thread_id(channel_id, thread_id)
    return f"{platform}:{channel_id}:{resolved_thread_id}"


def build_web_channel_id(session_key: str | None, user_id: str | None) -> str:
    """Return the channel identifier used by the web adapter path."""

    if session_key:
        return session_key
    return f"web:{user_id or 'anon'}"


def build_web_conversation_id(
    session_key: str | None,
    user_id: str | None,
    agent_type: str = "thehomie",
) -> str:
    """Return the durable conversation id for the relay/web path."""

    if session_key:
        return session_key
    return f"web:{agent_type}:{user_id or 'anon'}"
