"""Cabinet in-room slash command parser.

Commands are handled by the API layer before LLM dispatch. The parser is pure:
it only returns the requested action and arguments.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

CommandName = Literal[
    "help",
    "all",
    "add",
    "remove",
    "pin",
    "unpin",
    "voice",
    "end",
]

_COMMAND_RE = re.compile(r"^/([a-z][a-z0-9_-]*)(?:\s+([\s\S]*))?$", re.IGNORECASE)
_AGENT_RE = re.compile(r"@?([a-z][a-z0-9_-]{0,30})\b", re.IGNORECASE)


@dataclass(frozen=True)
class RoomCommand:
    name: CommandName
    args: str = ""
    agent_id: str | None = None
    message: str = ""


def parse_room_command(text: str) -> RoomCommand | None:
    """Parse a Cabinet slash command, or return ``None`` for normal text."""
    m = _COMMAND_RE.match((text or "").strip())
    if not m:
        return None
    name = m.group(1).lower()
    args = (m.group(2) or "").strip()
    if name == "help":
        return RoomCommand(name="help", args=args)
    if name == "all":
        return RoomCommand(name="all", args=args, message=args)
    if name in {"add", "remove", "pin"}:
        agent_id = _parse_agent_arg(args)
        return RoomCommand(name=name, args=args, agent_id=agent_id)
    if name == "unpin":
        return RoomCommand(name="unpin", args=args)
    if name == "voice":
        return RoomCommand(name="voice", args=args)
    if name == "end":
        return RoomCommand(name="end", args=args)
    return None


def _parse_agent_arg(args: str) -> str | None:
    m = _AGENT_RE.search(args or "")
    if not m:
        return None
    agent_id = m.group(1).lower()
    return "default" if agent_id == "main" else agent_id


__all__ = ["RoomCommand", "parse_room_command"]
