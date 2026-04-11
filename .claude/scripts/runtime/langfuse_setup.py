"""Langfuse observability integration for The Homie runtime layer.

One-time initialization - call init_langfuse() at startup. All runtime calls
through run_with_runtime_lanes(), plus any legacy calls that still reach the
run_with_fallback() compatibility shim, are automatically traced with provider,
model, tokens, cost, and session context.

Env vars (in .claude/scripts/.env):
    LANGFUSE_PUBLIC_KEY   — from Langfuse project settings
    LANGFUSE_SECRET_KEY   — from Langfuse project settings
    LANGFUSE_BASE_URL     — self-hosted: http://localhost:3000
    LANGFUSE_ENABLED      — set to "false" to disable (default: true if keys present)
"""

from __future__ import annotations

import logging
import os

_logger = logging.getLogger(__name__)
_initialized = False


def is_langfuse_enabled() -> bool:
    """Check if Langfuse is configured and not explicitly disabled."""
    if os.getenv("LANGFUSE_ENABLED", "true").lower() == "false":
        return False
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def init_langfuse() -> bool:
    """Initialize Langfuse client. Returns True if successful, False otherwise.

    Safe to call multiple times — no-ops after first successful init.
    Safe to call without keys — returns False silently.
    """
    global _initialized
    if _initialized:
        return True

    if not is_langfuse_enabled():
        return False

    try:
        # Set OTEL service.name so traces show "thehomie" instead of "unknown_service"
        try:
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry import trace as ot

            if not isinstance(ot.get_tracer_provider(), TracerProvider):
                resource = Resource.create({"service.name": "thehomie"})
                tp = TracerProvider(resource=resource)
                ot.set_tracer_provider(tp)
        except Exception as exc:
            _logger.debug("OTEL resource setup skipped: %s", exc)

        from langfuse import get_client

        client = get_client()
        if client.auth_check():
            _initialized = True
            _logger.info("Langfuse initialized (host: %s)", os.getenv("LANGFUSE_BASE_URL", "cloud"))

            # Claude Agent SDK auto-instrumentation via community instrumentor.
            # Uses wrapt to wrap query() + SDK hooks for tool-level spans.
            # Works with Windows monkey-patched transport (hooks at query level, not transport).
            try:
                from opentelemetry.instrumentation.claude_agent_sdk import (
                    ClaudeAgentSdkInstrumentor,
                )
                ClaudeAgentSdkInstrumentor().instrument()
                _logger.info("Claude Agent SDK auto-instrumentation enabled")
            except Exception as exc:
                _logger.warning("Claude SDK auto-instrumentation failed: %s", exc)

            return True
        else:
            _logger.warning("Langfuse auth check failed — tracing disabled")
            return False
    except Exception as exc:
        _logger.warning("Langfuse init failed: %s — tracing disabled", exc)
        return False


def flush_langfuse() -> None:
    """Flush pending spans — call on shutdown."""
    if not _initialized:
        return
    try:
        from langfuse import get_client
        get_client().flush()
    except Exception:
        pass
    # Also flush OTEL TracerProvider
    try:
        from opentelemetry import trace as ot
        tp = ot.get_tracer_provider()
        if hasattr(tp, "force_flush"):
            tp.force_flush(timeout_millis=5000)
    except Exception:
        pass
