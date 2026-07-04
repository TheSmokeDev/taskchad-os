"""Scheduled-task HTTP client — cross-process create wrapper for the
``/api/scheduled`` REST surface (served in the orchestration API process at
``localhost:4322``, same base as ``/api/cabinet/*``).

Mirrors the shape of ``cabinet_api.py`` (env-loaded auth, friendly-error
classes carrying ``friendly_message``) but scoped to the single POST the
suggestions-accept boundary needs.

Cross-process invariant: the chat process (``.claude/chat/main.py``) and the
API process serving ``/api/scheduled`` are SEPARATE Python processes. The
suggestions-accept path MUST NOT import a local ``create_scheduled`` — it POSTs
here so the Phase-1 bot-lifecycle guard (``_scan_scheduled_prompt`` inside
``create_scheduled``) runs SERVER-SIDE. That is how "accept flows THROUGH the
guard" holds.

Anti-pattern compliance:

* Rule 1: ``_base_url()`` / ``_bearer_token()`` read env at CALL time. No
  default-bound config values.
* Rule 2: NO module-level cached httpx client. The helper creates a fresh
  client when the caller passes ``None`` and closes it on exit, OR uses the
  caller-provided client (lifecycle owned by the caller — tests inject a
  ``MockTransport``-backed client).
"""

from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:4322"
DEFAULT_TIMEOUT_S = 10.0


# ---------------------------------------------------------------------------
# Env helpers — Rule 1: resolve at CALL time, not import time
# ---------------------------------------------------------------------------


def _base_url() -> str:
    """Return the orchestration API base URL (``ORCHESTRATION_API_BASE_URL``).

    Trailing slashes are stripped so a configured ``…:4322/`` can't produce a
    double-slash ``//api/scheduled`` path.
    """
    return os.getenv("ORCHESTRATION_API_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _bearer_token() -> str:
    """Return the orchestration API bearer token (or empty string).

    Empty string == loopback no-token mode (server allows requests without an
    Authorization header). The server only enforces the bearer middleware when
    its own ``ORCHESTRATION_API_TOKEN`` is set.
    """
    return os.getenv("ORCHESTRATION_API_TOKEN", "")


def _auth_headers() -> dict[str, str]:
    """Build the Authorization header dict.

    Returns ``{}`` when no token is set on the client side — matches
    loopback-default semantics. When a token is set, returns
    ``{"Authorization": "Bearer <token>"}``.
    """
    token = _bearer_token()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Friendly error classes
# ---------------------------------------------------------------------------


class ScheduledAPIError(Exception):
    """Base class for all scheduled_api errors.

    Carries ``friendly_message`` so chat handlers can return it directly to the
    operator instead of leaking a stack trace.
    """

    friendly_message: str = "Scheduled API error."


class ScheduledAPIUnreachable(ScheduledAPIError):  # noqa: N818 — contract-frozen friendly-error name (mirrors cabinet_api's N818 ignore)
    """``httpx.ConnectError`` — orchestration API not running."""

    friendly_message = (
        "Scheduled API is not running. Start it with "
        "`cd .claude/scripts && uv run python -m orchestration.run_api`."
    )


class ScheduledAuthFailure(ScheduledAPIError):  # noqa: N818 — contract-frozen friendly-error name
    """HTTP 401 — bearer token missing or wrong on the client side.

    Only fires when the SERVER has ``ORCHESTRATION_API_TOKEN`` set AND the
    client either omits the Authorization header or sends a wrong token.
    Loopback no-token mode (server unset, client unset) does NOT raise.
    """

    friendly_message = (
        "Scheduled auth failed — check ORCHESTRATION_API_TOKEN in .env."
    )


class ScheduledCreateRefused(ScheduledAPIError):  # noqa: N818 — contract-frozen friendly-error name
    """HTTP 400 — the server-side bot-lifecycle guard refused the prompt.

    ``create_scheduled`` translates a ``BotLifecycleBlocked`` into
    ``HTTPException(400, detail=<message>)``; that detail is carried verbatim
    into ``friendly_message`` so the operator sees exactly why the create was
    refused (never a 500 / stack trace).
    """

    friendly_message = "Scheduled create refused."

    def __init__(self, detail: str | None = None) -> None:
        message = detail or self.friendly_message
        super().__init__(message)
        # Instance override so the handler returns the server's verbatim reason.
        self.friendly_message = message


class ScheduledCreateInvalid(ScheduledAPIError):  # noqa: N818 — contract-frozen friendly-error name
    """HTTP 422 — the schedule failed the server's 5-field cron validation."""

    friendly_message = (
        "Invalid schedule — the cron expression was rejected. "
        "Use a 5-field cron like `0 8 * * *`."
    )

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or self.friendly_message)
        if detail:
            self.friendly_message = detail


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _error_detail(r: httpx.Response) -> str | None:
    """Best-effort extraction of a FastAPI ``{"detail": ...}`` error body."""
    try:
        data = r.json()
    except (ValueError, Exception):
        return None
    if isinstance(data, dict):
        detail = data.get("detail")
        if isinstance(detail, str) and detail:
            return detail
    return None


def _safe_json(r: httpx.Response) -> dict[str, Any]:
    """Return ``r.json()`` as a dict, or ``{}`` on empty/non-JSON 2xx body."""
    try:
        data = r.json()
    except (ValueError, Exception):
        return {}
    if isinstance(data, dict):
        return data
    return {"_data": data}


def _check_status(r: httpx.Response) -> dict[str, Any]:
    """Map HTTP status to friendly scheduled errors.

    Order matters — the specific 4xx codes are checked BEFORE the catch-all
    ``>= 400`` so a 400 surfaces as ``ScheduledCreateRefused`` (guard) and a
    422 as ``ScheduledCreateInvalid`` (bad cron), not a generic error.
    """
    if r.status_code == 401:
        raise ScheduledAuthFailure()
    if r.status_code == 400:
        raise ScheduledCreateRefused(_error_detail(r))
    if r.status_code == 422:
        raise ScheduledCreateInvalid(_error_detail(r))
    if r.status_code >= 400:
        # Unmapped 4xx/5xx — surface as generic ScheduledAPIError.
        raise ScheduledAPIError()
    return _safe_json(r)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


async def create_scheduled_task(
    job_spec: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """POST ``/api/scheduled`` — create a scheduled task through the guard.

    ``job_spec`` is the ``CreateScheduledBody`` shape:
    ``{"persona_id", "prompt", "schedule", "next_run"}``. The server runs
    ``_validate_cron`` + ``_scan_scheduled_prompt`` (Phase-1 guard) before the
    INSERT, so a bot-lifecycle prompt is refused server-side (HTTP 400 →
    :class:`ScheduledCreateRefused`) and a bad cron is rejected (HTTP 422 →
    :class:`ScheduledCreateInvalid`).

    Rule 2: when ``client`` is None a fresh ``AsyncClient`` is created and
    closed on exit; when the caller injects a client, the caller owns its
    lifecycle (e.g. a ``MockTransport``-backed test client).

    Returns the created ``scheduled_tasks`` row dict on success.
    """
    url = f"{_base_url()}/api/scheduled"
    headers = _auth_headers()
    try:
        if client is not None:
            r = await client.post(url, json=job_spec, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as c:
                r = await c.post(url, json=job_spec, headers=headers)
    except httpx.ConnectError as e:
        raise ScheduledAPIUnreachable() from e
    except httpx.HTTPError as e:
        raise ScheduledAPIError() from e
    return _check_status(r)


__all__ = [
    "create_scheduled_task",
    "ScheduledAPIError",
    "ScheduledAPIUnreachable",
    "ScheduledAuthFailure",
    "ScheduledCreateRefused",
    "ScheduledCreateInvalid",
]
