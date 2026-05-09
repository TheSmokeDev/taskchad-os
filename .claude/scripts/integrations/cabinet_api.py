"""Cabinet HTTP client — Phase 5b chat-process wrappers for the Phase 5a
`/api/cabinet/*` REST surface (running in the orchestration API process at
``localhost:4322``).

Mirrors the shape of ``finance_api.py`` (env-loaded auth, dataclass returns,
friendly-error patterns) but uses ``httpx.AsyncClient`` (NOT sync) because
the chat handlers are async.

Cross-process invariant: the Telegram bot lives in ``.claude/chat/main.py``
(its own Python process); the cabinet orchestrator + ``/api/cabinet/*`` REST
endpoints live in ``.claude/scripts/orchestration/run_api.py`` (a separate
Python process). Module-local channel registries cannot bridge the two.
The chat process MUST go via HTTP — that is what this module provides.

Authoritative REST surface — verified at
``.claude/scripts/dashboard_api.py:2022-2540`` (R1 revision 2026-05-09):

    POST /api/cabinet/new          body={chatId?: str}
    GET  /api/cabinet/list         ?limit=N&chatId=X
    GET  /api/cabinet/transcripts  ?meetingId=N&...
    POST /api/cabinet/send         body={meetingId, text, clientMsgId, chatId?}
    POST /api/cabinet/end          body={meetingId, chatId?}

Anti-pattern compliance:

* Rule 1: every public helper takes ``client: httpx.AsyncClient | None = None``
  resolved at call time. NO default-bound config values.
* Rule 2: NO module-level cached httpx client. Helpers create a fresh client
  if the caller passes ``None`` and close it on exit, OR use the
  caller-provided client (lifecycle owned by caller).
* Rule 3: cabinet_api.py does NOT touch ``security.kill_switches`` or
  Langfuse — those are orchestrator-side concerns. If a future patch adds
  module-attribute lookup, it must use ``from security import kill_switches``
  + ``kill_switches.is_disabled(...)`` (NOT direct symbol imports).
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:4322"
DEFAULT_TIMEOUT_S = 10.0


# ---------------------------------------------------------------------------
# Env helpers — Rule 1: resolve at CALL time, not import time
# ---------------------------------------------------------------------------


def _base_url() -> str:
    """Return the orchestration API base URL.

    Defaults to ``http://127.0.0.1:4322`` (matches
    ``.claude/scripts/orchestration/api.py:252-279`` host/port defaults).
    Env: ``ORCHESTRATION_API_BASE_URL``.
    """
    return os.getenv("ORCHESTRATION_API_BASE_URL", DEFAULT_BASE_URL)


def _bearer_token() -> str:
    """Return the orchestration API bearer token (or empty string).

    Empty string == loopback no-token mode (server allows requests without
    Authorization header). The server only enforces the bearer middleware
    when its own ``ORCHESTRATION_API_TOKEN`` is set
    (see ``orchestration/api.py:252-278``).
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


class CabinetAPIError(Exception):
    """Base class for all cabinet_api errors.

    Carries ``friendly_message`` so chat handlers can return it directly to
    the operator instead of leaking a stack trace.
    """

    friendly_message: str = "Cabinet API error."


class CabinetAPIUnreachable(CabinetAPIError):
    """``httpx.ConnectError`` — orchestration API not running."""

    friendly_message = (
        "Cabinet API is not running. Start it with "
        "`cd .claude/scripts && uv run python -m orchestration.run_api`."
    )


class CabinetAuthFailure(CabinetAPIError):
    """HTTP 401 — bearer token missing or wrong on client side.

    Only fires when the SERVER has ``ORCHESTRATION_API_TOKEN`` set AND the
    client either omits the Authorization header or sends a wrong token.
    Loopback no-token mode (server unset, client unset) does NOT raise.
    """

    friendly_message = (
        "Cabinet auth failed — check ORCHESTRATION_API_TOKEN in .env."
    )


class CabinetKillSwitchDisabled(CabinetAPIError):
    """HTTP 503 — ``kill_switches.requireEnabled('cabinet')`` refused on the
    orchestrator side (synchronous endpoints only).

    NOTE: ``/api/cabinet/send`` is fire-and-forget and returns 200 ``{ok,
    queued}`` regardless of orchestrator state. Kill-switch refusal during
    background turn execution surfaces as a channel ``{type: "error"}`` SSE
    event — NOT a synchronous 503. So this exception can only fire on
    ``create_meeting`` / ``end_meeting`` (and the helpers that use them).
    """

    friendly_message = (
        "Cabinet is disabled by the operator. Reach out for an override."
    )


class CabinetMeetingNotFound(CabinetAPIError):
    """HTTP 404 ``meeting_not_found``."""

    friendly_message = "Meeting not found."


class CabinetMeetingEnded(CabinetAPIError):
    """HTTP 410 ``meeting_ended``."""

    friendly_message = "Meeting has already ended."


class CabinetBadRequest(CabinetAPIError):
    """HTTP 400 — covers ``invalid clientMsgId``, ``empty text``, etc."""

    friendly_message = "Cabinet rejected the request as malformed."


class CabinetChatScopeMismatch(CabinetAPIError):
    """HTTP 403 ``chat_mismatch`` — meeting belongs to a different chat scope.

    Phase 5a's ``_cabinet_chat_match_or_403`` (``dashboard_api.py:1986-1997``,
    raised at ``:2360``, ``:2389``, ``:2415``, ``:2443``, ``:2470``, ``:2512``)
    rejects requests where the request's ``chatId`` mismatches the meeting's
    stored ``chat_id``. The friendly chat reply surfaces this so operators
    don't think the bot is broken.
    """

    friendly_message = (
        "That meeting belongs to a different chat. "
        "Use /cabinet list to see meetings in this chat."
    )


# ---------------------------------------------------------------------------
# Dataclass return type
# ---------------------------------------------------------------------------


@dataclass
class CabinetMeetingRef:
    """Minimal reference returned by :func:`create_meeting`.

    Field shape matches the ``cabinet_new`` JSON response
    (``dashboard_api.py:2052-2120``):

        {"ok": True, "meetingId": int, "autoEnded": list[int]}

    Plus ``chat_id`` echoed back from the caller (so subsequent calls can
    pass through chat-scope without re-deriving from ``incoming``).
    """

    id: int
    chat_id: str | None
    auto_ended_ids: list[int]


# ---------------------------------------------------------------------------
# Internal HTTP helpers — Rule 2: NO module-level cached client
# ---------------------------------------------------------------------------


def _safe_json(r: httpx.Response) -> dict[str, Any]:
    """Return ``r.json()`` or ``{}`` on empty/non-JSON 2xx body.

    R2 NM1: avoid crashing chat handlers on a 200 response with empty body
    (e.g. ``Content-Length: 0`` from a future endpoint variation).
    """
    try:
        data = r.json()
    except (ValueError, Exception):
        return {}
    if isinstance(data, dict):
        return data
    # Some endpoints (e.g. /list) return a dict, some helpers expect dict.
    # Non-dict 2xx bodies are wrapped so callers that index ["meetings"]
    # etc. fail with KeyError, not TypeError.
    return {"_data": data}


def _check_status(r: httpx.Response) -> dict[str, Any]:
    """Map HTTP status to friendly cabinet errors.

    Order matters — the more-specific 4xx codes are checked BEFORE the
    catch-all ``>= 400`` so a 403 surfaces as ``CabinetChatScopeMismatch``,
    not the generic ``CabinetBadRequest`` (R3-MN1 fix).
    """
    if r.status_code == 401:
        raise CabinetAuthFailure()
    if r.status_code == 403:
        # R3-MN1: Phase 5a returns 403 chat_mismatch from
        # dashboard_api.py:2360, :2389, :2415, :2443, :2470, :2512 when
        # the meeting's chat_id doesn't match the request's chat_id.
        # Map BEFORE the generic >= 400 branch.
        raise CabinetChatScopeMismatch()
    if r.status_code == 404:
        raise CabinetMeetingNotFound()
    if r.status_code == 410:
        raise CabinetMeetingEnded()
    if r.status_code == 503:
        raise CabinetKillSwitchDisabled()
    if r.status_code == 400:
        raise CabinetBadRequest()
    if r.status_code >= 500:
        # Other 5xx — surface as generic CabinetAPIError. Friendly message
        # comes from the base class default.
        raise CabinetAPIError()
    if r.status_code >= 400:
        # Catch-all for unmapped 4xx (e.g. 405, 409, 422). Treat as bad
        # request shape from the chat-side perspective.
        raise CabinetBadRequest()
    return _safe_json(r)


async def _post(
    path: str,
    payload: dict[str, Any],
    *,
    client: httpx.AsyncClient | None,
) -> dict[str, Any]:
    """POST helper with friendly-error wrapping.

    Rule 2: when ``client`` is None, create a fresh ``AsyncClient`` and
    close it on exit. When ``client`` is caller-provided, lifecycle is the
    caller's responsibility (e.g. tests inject ``MockTransport``-backed
    clients).
    """
    url = f"{_base_url()}{path}"
    headers = _auth_headers()
    try:
        if client is not None:
            r = await client.post(url, json=payload, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as c:
                r = await c.post(url, json=payload, headers=headers)
    except httpx.ConnectError as e:
        raise CabinetAPIUnreachable() from e
    except httpx.HTTPError as e:
        raise CabinetAPIError() from e
    return _check_status(r)


async def _get(
    path: str,
    params: dict[str, Any],
    *,
    client: httpx.AsyncClient | None,
) -> dict[str, Any]:
    """GET helper with friendly-error wrapping. Mirror of :func:`_post`."""
    url = f"{_base_url()}{path}"
    headers = _auth_headers()
    try:
        if client is not None:
            r = await client.get(url, params=params, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as c:
                r = await c.get(url, params=params, headers=headers)
    except httpx.ConnectError as e:
        raise CabinetAPIUnreachable() from e
    except httpx.HTTPError as e:
        raise CabinetAPIError() from e
    return _check_status(r)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


async def create_meeting(
    chat_id: str | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> CabinetMeetingRef:
    """POST ``/api/cabinet/new`` — create a new cabinet meeting.

    Body shape (verified ``dashboard_api.py:2052-2120``)::

        {"chatId": chat_id?}

    Mode is hardcoded ``"text"`` by Phase 5a; roster auto-snapshots the
    currently-active personas via ``_roster_from_personas()``
    (``cabinet/text_orchestrator.py:81-130``). Operators manage active
    personas via ``/persona`` BEFORE running ``/cabinet``. There is NO
    persona-selection arg at this layer (R1 B2 fix).

    Returns a :class:`CabinetMeetingRef` with the new ``meetingId``, the
    echoed ``chat_id``, and any prior open meetings auto-ended by Phase
    5a's "one open meeting per chat" rule.
    """
    payload: dict[str, Any] = {}
    if chat_id is not None:
        payload["chatId"] = chat_id
    body = await _post("/api/cabinet/new", payload, client=client)
    return CabinetMeetingRef(
        id=int(body["meetingId"]),
        chat_id=chat_id,
        auto_ended_ids=list(body.get("autoEnded", [])),
    )


async def list_meetings(
    limit: int = 20,
    chat_id: str | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    """GET ``/api/cabinet/list`` — list recent cabinet meetings.

    Returns the list at ``body["meetings"]`` (Phase 5a's response shape at
    ``dashboard_api.py:2022-2050``). Each meeting dict has ``id``,
    ``started_at``, ``ended_at``, ``mode``, ``title``, ``chat_id`` etc.
    """
    params: dict[str, Any] = {"limit": limit}
    if chat_id is not None:
        params["chatId"] = chat_id
    body = await _get("/api/cabinet/list", params, client=client)
    return list(body.get("meetings", []))


async def get_transcripts(
    meeting_id: int,
    limit: int = 200,
    chat_id: str | None = None,
    before_ts: int | None = None,
    before_id: int | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """GET ``/api/cabinet/transcripts`` — paginated transcript fetch.

    Response shape (verified ``dashboard_api.py:2153-2210``)::

        {"ok": True, "transcript": [...], "pinnedAgent": str|None,
         "latestSeq": int, "agents": [...]}

    Note the response key is ``transcript`` (singular), NOT ``transcripts``.
    """
    params: dict[str, Any] = {"meetingId": meeting_id, "limit": limit}
    if chat_id is not None:
        params["chatId"] = chat_id
    if before_ts is not None:
        params["beforeTs"] = before_ts
    if before_id is not None:
        params["beforeId"] = before_id
    return await _get("/api/cabinet/transcripts", params, client=client)


async def send_message(
    meeting_id: int,
    text: str,
    client_msg_id: str | None = None,
    chat_id: str | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """POST ``/api/cabinet/send`` — add an operator turn to a meeting.

    Body shape (verified ``dashboard_api.py:2336-2380``)::

        {"meetingId": int, "text": str, "clientMsgId": str, "chatId": str?}

    ``clientMsgId`` is REQUIRED on the wire — Phase 5a's ``cabinet_send``
    raises 400 ``invalid clientMsgId`` if missing or empty. R1 M3 fix:
    when the caller passes ``None``, this helper auto-generates via
    ``uuid.uuid4().hex``. To preserve idempotency the caller may pass an
    explicit value (e.g. derived from a Telegram message id).

    This endpoint is FIRE-AND-FORGET: returns 200 ``{ok: True, queued:
    True}`` regardless of orchestrator state. Kill-switch refusal during
    background execution surfaces as a channel ``{type: "error"}`` SSE
    event — NOT a synchronous 503. Therefore this helper can NEVER raise
    :class:`CabinetKillSwitchDisabled` (verified
    ``dashboard_api.py:2362-XXXX`` + ``test_cabinet_api.py:300-315``).
    """
    if client_msg_id is None:
        client_msg_id = uuid.uuid4().hex
    payload: dict[str, Any] = {
        "meetingId": meeting_id,
        "text": text,
        "clientMsgId": client_msg_id,
    }
    if chat_id is not None:
        payload["chatId"] = chat_id
    return await _post("/api/cabinet/send", payload, client=client)


async def end_meeting(
    meeting_id: int,
    chat_id: str | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """POST ``/api/cabinet/end`` — close a cabinet meeting.

    Body shape (verified ``dashboard_api.py:2504-2540``)::

        {"meetingId": int, "chatId": str?}

    Response includes ``alreadyEnded`` (camelCase) on the no-op path. This
    is a synchronous endpoint, so it CAN raise
    :class:`CabinetKillSwitchDisabled` if the operator has the cabinet
    kill switch flipped.
    """
    payload: dict[str, Any] = {"meetingId": meeting_id}
    if chat_id is not None:
        payload["chatId"] = chat_id
    return await _post("/api/cabinet/end", payload, client=client)


__all__ = [
    "CabinetMeetingRef",
    "create_meeting",
    "list_meetings",
    "get_transcripts",
    "send_message",
    "end_meeting",
    # Friendly error hierarchy (handlers catch these and return
    # ``e.friendly_message`` to chat).
    "CabinetAPIError",
    "CabinetAPIUnreachable",
    "CabinetAuthFailure",
    "CabinetKillSwitchDisabled",
    "CabinetMeetingNotFound",
    "CabinetMeetingEnded",
    "CabinetBadRequest",
    "CabinetChatScopeMismatch",
]
