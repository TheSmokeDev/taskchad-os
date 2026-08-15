"""Tests for Cabinet in-room slash command parsing."""
from __future__ import annotations

import pytest

from cabinet.room_commands import parse_room_command


@pytest.mark.parametrize(
    ("text", "name", "agent_id", "message"),
    [
        ("/help", "help", None, ""),
        ("/all what is everyone seeing?", "all", None, "what is everyone seeing?"),
        ("/add @finance", "add", "finance", ""),
        ("/remove finance", "remove", "finance", ""),
        ("/pin @main", "pin", "default", ""),
        ("/unpin", "unpin", None, ""),
        ("/voice", "voice", None, ""),
        ("/end", "end", None, ""),
    ],
)
def test_parse_room_command(text: str, name: str, agent_id: str | None, message: str) -> None:
    command = parse_room_command(text)

    assert command is not None
    assert command.name == name
    assert command.agent_id == agent_id
    assert command.message == message


def test_parse_room_command_ignores_normal_text_and_unknown_commands() -> None:
    assert parse_room_command("hello @sales") is None
    assert parse_room_command("/doesnotexist @sales") is None


def test_a_pasted_grant_approval_is_recognized_and_never_reaches_the_llm() -> None:
    """#428 R3 MAJOR 3: the room prints `/grant approve …` but cannot run it.

    Returning ``None`` here is what made that a false-success surface — the
    text fell through as ordinary meeting text, a persona answered as though
    the toolset had landed, and the proposal expired untouched. The parser
    now claims the command so the API layer can answer it honestly; the
    decision itself still only happens on the admin-gated chat adapters.
    """
    command = parse_room_command("/grant approve sales ABC123")

    assert command is not None
    assert command.name == "grant"
    # Claimed as a command means the raw text is never handed to a persona
    # prompt — the room-command contract for every other name here.
    assert parse_room_command("/grant").name == "grant"
