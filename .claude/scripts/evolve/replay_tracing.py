"""Phase 2.4 — Langfuse replay-tagged span helpers.

When a replay opts in via `disable_tracing=False`, we route its spans under a
dedicated Langfuse namespace (`user_id="evolve-replay"`,
`session_id="evolve:<experiment_id>"`) tagged with the experiment id and a
deterministic fingerprint of the override set. Production cost reports stay
clean; experimental runs are filterable in one click; the 2.3.1 decision
artifact picks up a clickable trace URL via `langfuse_trace_url()`.

Late-binds Langfuse via `from runtime import langfuse_setup` then
`langfuse_setup.is_langfuse_enabled()` — module-attribute lookup so
`isolate_langfuse()` monkey-patches in tests propagate through correctly.
Every call into the SDK is wrapped in try/except — observability must never
break the replay.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from contextlib import contextmanager
from typing import Any, Iterator


_REPLAY_USER_ID = "evolve-replay"
_REPLAY_SESSION_PREFIX = "evolve:"
_REPLAY_TAGS: tuple[str, ...] = ("evolve-replay",)
_FINGERPRINT_LEN = 16


def override_fingerprint(overrides: dict[str, Any] | None) -> str:
    """Stable 16-char SHA-1 fingerprint of an override dict.

    Canonicalizes via `json.dumps(sort_keys=True, default=str)`, hashes, and
    returns the first 16 hex chars (~64 bits of collision resistance — enough
    to eyeball "two replays used the same overrides" in a CLI table).

    Assumes overrides are JSON-primitive values (float/int/str/bool). Non-
    primitive types (Path, datetime, etc.) get stringified via `default=str`,
    which can produce platform-dependent hashes (e.g. `WindowsPath` vs
    `PosixPath`). Evolve overrides are config primitives in practice, so
    this is a documented assumption rather than a runtime check.
    """
    canon = json.dumps(overrides or {}, sort_keys=True, default=str)
    return hashlib.sha1(canon.encode("utf-8")).hexdigest()[:_FINGERPRINT_LEN]


def build_experiment_tag(
    experiment_id: str,
    overrides: dict[str, Any] | None,
    baseline_experiment_id: str | None = None,
) -> dict[str, Any]:
    """Build the PRD-shape tag dict carried as Langfuse trace metadata.

    Returns a dict with keys: ``experiment_id``, ``replay`` (always True),
    ``override_fingerprint`` (16-char sha1), ``baseline_experiment_id``
    (None when running an absolute baseline rather than a candidate).
    """
    return {
        "experiment_id": experiment_id,
        "replay": True,
        "override_fingerprint": override_fingerprint(overrides),
        "baseline_experiment_id": baseline_experiment_id,
    }


def _base_url() -> str | None:
    """Return ``LANGFUSE_BASE_URL`` with no trailing slash, or None when absent."""
    raw = os.getenv("LANGFUSE_BASE_URL", "").strip()
    return raw.rstrip("/") if raw else None


def _is_enabled() -> bool:
    """Module-attribute lookup of ``is_langfuse_enabled``.

    Goes through ``runtime.langfuse_setup`` rather than importing the function
    directly, so ``isolate_langfuse()``'s monkey-patch at the module attribute
    propagates through to URL helpers and the root-span context. Falls back
    to False on any import / attribute error so callers never crash from a
    missing dependency.
    """
    try:
        from runtime import langfuse_setup
        return bool(langfuse_setup.is_langfuse_enabled())
    except Exception:
        return False


def langfuse_trace_url(experiment_id: str) -> str | None:
    """Filter URL pointing at the session for this experiment.

    Returns ``<LANGFUSE_BASE_URL>/sessions?sessionId=evolve:<id>`` when both
    Langfuse is enabled and ``LANGFUSE_BASE_URL`` is set; otherwise None.
    The filter URL is canonical — there is no ``LANGFUSE_PROJECT_ID`` env
    var in this codebase to construct a deeper link. If/when project-aware
    URLs become available, this is the function to update.
    """
    if not _is_enabled():
        return None
    base = _base_url()
    if not base:
        return None
    return f"{base}/sessions?sessionId={_REPLAY_SESSION_PREFIX}{experiment_id}"


def langfuse_session_url(experiment_id: str) -> str | None:
    """Same filter URL as ``langfuse_trace_url`` today; separated for future
    deep-linking when project-ID-aware URLs become available."""
    return langfuse_trace_url(experiment_id)


@contextmanager
def replay_root_span(
    experiment_id: str,
    experiment_tag: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    """Wrap a replay block with ``propagate_attributes`` + a root span.

    All children (the existing ``@observe``-decorated ``recall``,
    ``classify_tier``, ``run_recall_pipeline``) auto-nest under ``replay_run``
    via OTEL context propagation — no instrumentation changes needed inside
    cognition/.

    Yields the experiment tag dict so callers can record the same metadata
    on their report. Fails open silently when Langfuse is disabled or the
    SDK is missing — the replay still runs, just without span emission.

    Exception propagation: an exception raised inside the ``with`` block
    flows through to the wrapped span's ``__exit__`` so Langfuse marks the
    span as failed (mirrors ``engine.py:407-417``).
    """
    if not _is_enabled():
        yield experiment_tag
        return

    try:
        from langfuse import get_client, propagate_attributes
    except Exception:
        yield experiment_tag
        return

    session_id = f"{_REPLAY_SESSION_PREFIX}{experiment_id}"

    _prop_ctx = None
    _root_ctx = None
    _exc_info: tuple[Any, Any, Any] = (None, None, None)

    try:
        _prop_ctx = propagate_attributes(
            session_id=session_id,
            user_id=_REPLAY_USER_ID,
            tags=list(_REPLAY_TAGS),
            metadata=dict(experiment_tag),
        )
        _prop_ctx.__enter__()
    except Exception:
        _prop_ctx = None

    try:
        client = get_client()
        _root_ctx = client.start_as_current_observation(
            as_type="span",
            name="replay_run",
            input={"experiment_id": experiment_id},
            metadata=dict(experiment_tag),
        )
        _root_ctx.__enter__()
    except Exception:
        _root_ctx = None

    try:
        yield experiment_tag
    except BaseException:
        _exc_info = sys.exc_info()
        raise
    finally:
        if _root_ctx is not None:
            try:
                _root_ctx.__exit__(*_exc_info)
            except Exception:
                pass
        if _prop_ctx is not None:
            try:
                _prop_ctx.__exit__(*_exc_info)
            except Exception:
                pass


__all__ = [
    "build_experiment_tag",
    "langfuse_session_url",
    "langfuse_trace_url",
    "override_fingerprint",
    "replay_root_span",
]
