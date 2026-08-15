"""Server-side parser for the persona-addressed learn drop.

`@<persona> learn <url>` is a COMMAND, not conversation. The router resolves it
deterministically and the command text never reaches an LLM prompt — the
cabinet `room_commands.py` precedent.

The shape is deliberately strict and default-deny: the message must be exactly
`@name learn <http(s) url>` with nothing before or after it. `@sales learn
kubernetes` is prose and falls through to the engine untouched.

Pure module: regex + dataclass, no imports from the chat runtime.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_LEARN_DROP_RE = re.compile(
    r"^@([A-Za-z0-9][A-Za-z0-9_-]{0,63})\s+learn\s+(https?://\S+)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class LearnDrop:
    persona_id: str
    url: str


def parse_learn_drop(text: str) -> LearnDrop | None:
    """Parse a persona-addressed learn drop, or return ``None`` for normal text."""
    match = _LEARN_DROP_RE.match((text or "").strip())
    if not match:
        return None
    return LearnDrop(persona_id=match.group(1).casefold(), url=match.group(2))


__all__ = ["LearnDrop", "parse_learn_drop"]
