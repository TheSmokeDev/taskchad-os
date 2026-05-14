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
