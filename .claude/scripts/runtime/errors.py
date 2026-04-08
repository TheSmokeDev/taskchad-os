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
