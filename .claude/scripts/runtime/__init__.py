"""Runtime adapters and shared bootstrap helpers."""

from .base import RuntimeRequest, RuntimeResult
from .bootstrap import (
    build_second_brain_identity_context,
    build_session_start_context,
)
from .registry import run_with_fallback

__all__ = [
    "RuntimeRequest",
    "RuntimeResult",
    "build_second_brain_identity_context",
    "build_session_start_context",
    "run_with_fallback",
]
