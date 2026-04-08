"""Observability helpers for orchestration/team phases.

Langfuse is the primary behavioral trace lane.
Sentry/GlitchTip is the secondary error/crash lane.

These helpers are intentionally no-op safe: observability must never break
runtime, CLI, or API behavior.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from runtime.langfuse_setup import init_langfuse, is_langfuse_enabled

logger = logging.getLogger(__name__)

_SENTRY_INITIALIZED = False
_ROOT = Path(__file__).resolve().parents[3]
_OBS_LOG = _ROOT / ".omx" / "logs" / "team-observability.jsonl"


def init_orchestration_observability() -> None:
    """Best-effort init for Langfuse + Sentry in CLI/API contexts."""
    global _SENTRY_INITIALIZED

    try:
        import config  # noqa: F401  # ensures .env is loaded for direct service usage
    except Exception:
        pass

    try:
        init_langfuse()
    except Exception:
        pass

    if _SENTRY_INITIALIZED:
        return

    dsn = os.getenv("SENTRY_DSN")
    if not dsn:
        return

    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=dsn,
            traces_sample_rate=0.0,
            environment=os.getenv("SENTRY_ENVIRONMENT", "local"),
            release="thehomie-1.0",
        )
        _SENTRY_INITIALIZED = True
    except Exception:
        pass


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _safe_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_value(v) for v in value]
    return str(value)


def _append_observation_log(entry: dict[str, Any]) -> None:
    try:
        _OBS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _OBS_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(_safe_value(entry)) + "\n")
    except Exception:
        # Observability log is auxiliary only.
        pass


def _capture_sentry_exception(
    exc: BaseException,
    *,
    span_name: str,
    metadata: dict[str, Any] | None = None,
) -> str | None:
    if not os.getenv("SENTRY_DSN"):
        return None
    try:
        import sentry_sdk

        md = _safe_value(metadata or {})
        with sentry_sdk.push_scope() as scope:
            scope.set_tag("component", "orchestration")
            scope.set_tag("span_name", span_name)
            for key in ("team_id", "convoy_id", "subtask_id", "agent_id", "msg_type", "requested_backend", "actual_backend"):
                value = md.get(key)
                if value is not None:
                    scope.set_tag(key, str(value))
            scope.set_context("orchestration", md)
            return sentry_sdk.capture_exception(exc)
    except Exception:
        return None


def update_observation(
    *,
    metadata: dict[str, Any] | None = None,
    output: Any | None = None,
    level: str | None = None,
    status_message: str | None = None,
    trace_metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Best-effort update of current Langfuse span/trace; returns IDs if any."""
    ids: dict[str, Any] = {"trace_id": None, "observation_id": None}
    if not is_langfuse_enabled():
        return ids
    try:
        from langfuse import get_client

        lf = get_client()
        if trace_metadata or tags:
            lf.update_current_trace(
                metadata=_safe_value(trace_metadata) if trace_metadata else None,
                tags=tags,
            )
        lf.update_current_span(
            metadata=_safe_value(metadata) if metadata else None,
            output=_safe_value(output) if output is not None else None,
            level=level,
            status_message=status_message,
        )
        ids["trace_id"] = lf.get_current_trace_id()
        ids["observation_id"] = lf.get_current_observation_id()
    except Exception:
        pass
    return ids


@contextmanager
def orchestration_span(
    name: str,
    *,
    input: Any | None = None,
    metadata: dict[str, Any] | None = None,
    trace_metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    expected_exceptions: tuple[type[BaseException], ...] = (),
) -> Iterator[dict[str, Any]]:
    """Context manager for orchestration/team Langfuse spans.

    Returns a mutable state dict with:
    - trace_id
    - observation_id
    - sentry_event_id
    """
    init_orchestration_observability()

    state: dict[str, Any] = {
        "trace_id": None,
        "observation_id": None,
        "sentry_event_id": None,
        "name": name,
    }

    if not is_langfuse_enabled():
        try:
            yield state
            _append_observation_log(
                {"name": name, "status": "ok", **state, "metadata": _safe_value(metadata or {})}
            )
        except Exception as exc:
            if not isinstance(exc, expected_exceptions):
                state["sentry_event_id"] = _capture_sentry_exception(
                    exc, span_name=name, metadata=metadata,
                )
            _append_observation_log(
                {
                    "name": name,
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    **state,
                    "metadata": _safe_value(metadata or {}),
                }
            )
            raise
        return

    try:
        from langfuse import get_client
        lf = get_client()
        span_cm = lf.start_as_current_observation(
            name=name,
            as_type="span",
            input=_safe_value(input) if input is not None else None,
            metadata=_safe_value(metadata) if metadata else None,
        )
    except Exception as init_exc:
        # Langfuse setup itself broke — fall back to no-op but log the degradation.
        _append_observation_log(
            {
                "name": name,
                "status": "degraded",
                "reason": f"langfuse_client_init_failed: {type(init_exc).__name__}",
                "metadata": _safe_value(metadata or {}),
            }
        )
        try:
            yield state
        except Exception as exc:
            if not isinstance(exc, expected_exceptions):
                state["sentry_event_id"] = _capture_sentry_exception(
                    exc, span_name=name, metadata=metadata,
                )
            _append_observation_log(
                {
                    "name": name,
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    **state,
                    "metadata": _safe_value(metadata or {}),
                }
            )
            raise
        return

    with span_cm:
        state.update(
            update_observation(
                trace_metadata=trace_metadata,
                tags=tags or ["orchestration"],
            )
        )
        try:
            yield state
            state.update(update_observation())
            _append_observation_log(
                {"name": name, "status": "ok", **state, "metadata": _safe_value(metadata or {})}
            )
        except Exception as exc:
            if isinstance(exc, expected_exceptions):
                # Expected exception — mark span so operators know Sentry was intentionally suppressed
                state.update(
                    update_observation(
                        metadata={
                            "error_type": type(exc).__name__,
                            "expected": True,
                        },
                        level="WARNING",
                        status_message=f"expected: {exc}",
                    )
                )
            else:
                state["sentry_event_id"] = _capture_sentry_exception(
                    exc, span_name=name, metadata=metadata,
                )
                state.update(
                    update_observation(
                        metadata={
                            "error_type": type(exc).__name__,
                            "sentry_event_id": state["sentry_event_id"],
                        },
                        level="ERROR",
                        status_message=str(exc),
                    )
                )
            _append_observation_log(
                {
                    "name": name,
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    **state,
                    "metadata": _safe_value(metadata or {}),
                }
            )
            raise
