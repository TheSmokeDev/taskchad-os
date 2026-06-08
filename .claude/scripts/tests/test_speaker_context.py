from __future__ import annotations

from models import Channel, IncomingMessage, Platform, Thread, User
from speaker_context import render_speaker_context, resolve_speaker_context


def _message(
    *,
    platform_id: str = "111",
    display_name: str | None = "Alice",
    is_dm: bool = False,
) -> IncomingMessage:
    return IncomingMessage(
        text="what should I do next?",
        user=User(
            platform=Platform.DISCORD,
            platform_id=platform_id,
            display_name=display_name,
        ),
        channel=Channel(platform=Platform.DISCORD, platform_id="chan-1", is_dm=is_dm),
        platform=Platform.DISCORD,
        thread=Thread(thread_id="chan-1"),
    )


def test_resolves_known_transport_speaker() -> None:
    context = resolve_speaker_context(_message(platform_id="222", display_name="the operator"))

    assert context.status == "verified_transport_metadata"
    assert context.platform == "discord"
    assert context.platform_user_id == "222"
    assert context.display_name == "the operator"
    assert context.channel_scope == "shared_channel"

    rendered = render_speaker_context(context)
    assert "Current Speaker" not in rendered
    assert "display_name: the operator" in rendered
    assert "owner/default identity" in rendered


def test_unknown_speaker_is_loud_and_unverified() -> None:
    context = resolve_speaker_context(_message(platform_id="", display_name=None))

    assert context.status == "unknown_unverified"
    assert context.platform_user_id == "unknown"
    assert context.display_name == "unknown"

    rendered = render_speaker_context(context)
    assert "warning: Active speaker identity is incomplete" in rendered
    assert "ask the speaker to verify who they are" in rendered
    assert "owner/default identity" in rendered


def test_speaker_context_ignores_raw_event_paths_and_profile_dumps() -> None:
    message = _message(platform_id="333", display_name="Bob")
    message.raw_event = {
        "profile_file": r"C:\Users\YourUser\TheHomie\Memory\USER.md",
        "secret": "<REDACTED-openai>",
        "raw_profile": "private memory dump",
    }

    rendered = render_speaker_context(resolve_speaker_context(message))

    assert "Bob" in rendered
    assert "C:\\Users" not in rendered
    assert "<REDACTED-openai>" not in rendered
    assert "private memory dump" not in rendered
