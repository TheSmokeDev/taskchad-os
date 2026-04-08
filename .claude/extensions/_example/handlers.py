"""Example extension handlers.

Each handler must be an async function with this signature:

    async def handle_X(adapter, incoming, args, *, collect_only=False) -> str

Arguments:
    adapter: Platform adapter (TelegramAdapter, SlackAdapter, etc.)
    incoming: IncomingMessage with .text, .user, .channel, .thread, etc.
    args: Command arguments after the command name (e.g. "/hello world" -> "world")
    collect_only: If True, return the text but don't send via adapter.
                  Used for multi-command chaining and /brief.

Returns:
    Reply text to send to the user.

Handler references in extension.json use "module:function" format, e.g.:
    "handler": "handlers:handle_hello"

The module name is relative to the extension directory.
"""

from __future__ import annotations

from typing import Any


async def handle_hello(
    adapter: Any, incoming: Any, args: str, *, collect_only: bool = False,
) -> str:
    """Say hello from the example extension."""
    name = args.strip() if args.strip() else "world"
    return f"Hello, {name}! This is the example extension."
