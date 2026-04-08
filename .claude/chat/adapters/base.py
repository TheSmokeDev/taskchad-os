"""Base protocol for platform chat adapters."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from models import Channel, IncomingMessage, OutgoingMessage, Platform


@runtime_checkable
class PlatformAdapter(Protocol):
    """Protocol for platform chat adapters.

    Implement this to add a new chat platform. The conversation engine
    and router are platform-agnostic — they only interact through this interface.
    """

    @property
    def platform(self) -> Platform: ...

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def listen(self) -> AsyncIterator[IncomingMessage]: ...

    async def send(self, message: OutgoingMessage) -> str | None:
        """Send a message, return platform message ID for later updates."""
        ...

    async def update(self, message: OutgoingMessage) -> None:
        """Edit/update an existing message (for streaming updates)."""
        ...

    async def send_typing(self, channel: Channel) -> None:
        """Send typing indicator. Optional — default no-op."""
        ...
