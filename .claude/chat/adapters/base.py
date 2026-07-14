"""Base protocol for platform chat adapters."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from models import Channel, IncomingMessage, OutgoingMessage, Platform


@dataclass(frozen=True)
class ProgressCapabilities:
    """Progress UX an adapter can deliver truthfully.

    Every flag defaults off so an unknown or third-party adapter cannot start
    emitting permanent progress messages merely by gaining a new router
    dependency. Adapters opt into only the surfaces their platform supports.
    """

    enabled: bool = False
    typing: bool = False
    editable: bool = False
    recover_failed_status: bool = False


_DISABLED_PROGRESS_CAPABILITIES = ProgressCapabilities()


def resolve_progress_capabilities(adapter: object) -> ProgressCapabilities:
    """Return a valid adapter declaration, otherwise fail quiet.

    Attribute access is guarded because third-party adapters may expose a
    dynamic property that raises. Invalid declarations are treated exactly
    like missing declarations: progress is disabled.
    """

    try:
        capabilities = getattr(adapter, "progress_capabilities", None)
    except Exception:
        return _DISABLED_PROGRESS_CAPABILITIES
    if isinstance(capabilities, ProgressCapabilities):
        return capabilities
    return _DISABLED_PROGRESS_CAPABILITIES


@runtime_checkable
class PlatformAdapter(Protocol):
    """Protocol for platform chat adapters.

    Implement this to add a new chat platform. The conversation engine
    and router are platform-agnostic — they only interact through this interface.
    """

    @property
    def platform(self) -> Platform: ...

    @property
    def progress_capabilities(self) -> ProgressCapabilities: ...

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def listen(self) -> AsyncIterator[IncomingMessage]: ...

    async def send(self, message: OutgoingMessage) -> str | None:
        """Send a message, return platform message ID for later updates."""
        ...

    async def update(self, message: OutgoingMessage) -> str | None:
        """Perform one in-place edit and return its message ID.

        An update must not send a new message or dispatch media. If the content
        cannot be represented by one safe edit, return ``None`` before any
        side effect so the router can own the single fresh-send fallback.
        """
        ...

    async def send_typing(self, channel: Channel) -> None:
        """Send typing indicator. Optional — default no-op."""
        ...
