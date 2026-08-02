"""Runtime-layer error types."""

from __future__ import annotations


class RuntimeLayerError(Exception):
    """Base runtime-layer error."""


class RuntimeConfigError(RuntimeLayerError):
    """The runtime is misconfigured or missing credentials."""


class RuntimeUnsupportedCapabilityError(RuntimeLayerError):
    """The runtime does not support the requested capability."""


class RuntimeRetryableError(RuntimeLayerError):
    """The runtime failed in a way that may be recoverable via fallback."""


class RuntimeExecutionError(RuntimeLayerError):
    """The runtime failed and no valid fallback succeeded."""


class RuntimeCallerToolTransportError(RuntimeExecutionError):
    """No selected runtime safely carried the caller's scoped tool snapshot.

    Channel adapters may use this narrow signal to retry a conversational turn
    without tools. They must not treat a generic runtime failure as permission
    to drop capability silently.
    """
