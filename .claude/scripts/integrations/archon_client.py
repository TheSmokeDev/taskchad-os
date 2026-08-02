"""Archon HTTP client — the ONLY module that talks to Archon's REST API.

Epic: "Archon as the Execution Spine" (#252), ticket #253.
Architecture: ``PRDs/active/PRD-archon-execution-spine.architecture.md``.

The Homie is a **first-class Archon client**: it deploys work through Archon's
orchestrator, reads run state back, and steers paused runs through Archon's own
gate endpoints. Archon owns execution, isolation, and telemetry. This module
owns the transport, and nothing else in the framework opens an HTTP connection
to Archon.

Design inheritance (do not re-decide these here)
------------------------------------------------

**F1 — client, not adapter.** Archon has no adapter registry; adapters are
hand-instantiated inside ``packages/server/src/index.ts``, so "be an adapter"
would mean a TypeScript class compiled into Archon's server. We are a client.

**F3 — never a raw run POST.** There IS no raw run path. Archon's
``POST /api/workflows/{name}/run`` merely builds the string
``/workflow run <name> <message>`` (``packages/server/src/routes/api.ts:3094``)
and hands it to ``handleMessage()`` — the same funnel Telegram, Slack, Discord,
webhooks and the CLI all use. Dispatching through the conversation-message form
gets the orchestrator's full pre-flight (requirement gates before spend,
conversation→codebase binding, isolation resolution with stale-env recovery and
merged-worktree cleanup, and resume-before-fresh with a compare-and-swap). A
naive client that skipped it would lose all of that, so :func:`dispatch_workflow`
builds the byte-identical string for you — never hand-roll it at a call site.

**F6 — this module is NOT the live telemetry path.** Archon's
``/api/stream/__dashboard__`` SSE is single-slot (``registerStream`` closes any
existing stream), so the Homie and an open Archon Console evict each other. Live
event tailing is a read-only cursor-tail of ``remote_agent_workflow_events``
owned by a separate ticket. :func:`get_run` here is the ON-DEMAND detail fetch —
run + full event log in one call — not a poll loop.

**F7 — no auth locally, and that is a finding.** Archon's ``resolveAuthContext``
falls through to ``undefined``; the server binds ``0.0.0.0`` by default
(``packages/server/src/index.ts:868`` — ``process.env.HOST || '0.0.0.0'``), so
anything on the LAN can list runs, read prompt text, and approve or cancel work.
:func:`check_loopback_posture` is the physical-state probe for that; see
``docs/manual/features/archon-execution-client.md`` for the pin runbook.

**Not in this module** (deliberately, they belong to sibling tickets): the
dispatch capability gate + audit row, prompt synthesis (F2), the work-item ↔
conversation correlation ledger, and the event tail.

Anti-pattern compliance
-----------------------

* **Rule 1** — no config bound in a default argument. ``ARCHON_API_BASE_URL``
  and ``ARCHON_API_TIMEOUT_S`` are resolved inside :func:`_base_url` /
  :func:`_timeout_s` at CALL time, so a test or a live retune takes effect.
* **Rule 2** — no module-level cached client, and :func:`check_loopback_posture`
  reads PHYSICAL socket state (does a LAN address actually accept a connection)
  rather than trusting an env var or a config claim about the bind.
* **Rule 3** — internal seams (``_local_ipv4_addresses``, ``_tcp_connect``) are
  called through module attributes, so monkeypatching the module propagates.

Error contract
--------------

Every helper raises a typed :class:`ArchonAPIError` subclass carrying
``friendly_message`` for direct operator display. This module is a transport,
so failures are REPORTED, not swallowed — a caller on a fail-open path is
responsible for its own try/except (that is the caller's contract, not ours).
Contract violations (bad action, empty text, malformed id) raise ``ValueError``
BEFORE any network work is attempted.

Event loop
----------

The HTTP surface is async (``httpx.AsyncClient``) and safe on the bot loop
subject to its timeout. :func:`check_loopback_posture` does BLOCKING socket
work — call it via ``asyncio.to_thread`` from async code, and remember that
to_thread ARGUMENTS evaluate on the loop, so resolve the port inside the thread
if it comes from config.
"""

from __future__ import annotations

import os
import re
import socket
import sys
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

# ---------------------------------------------------------------------------
# Constants — Archon wire contract, read off the Archon server source (v0.6.0).
# Every file:line citation below refers to Archon's own repository.
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = "http://127.0.0.1:3090"
DEFAULT_TIMEOUT_S = 15.0
DEFAULT_ARCHON_PORT = 3090

#: Steering actions Archon exposes as ``POST /api/workflows/runs/{id}/{action}``
#: (``api.ts:738-827``). Mirrors ``manage-run-tool.ts``'s discriminator for the
#: subset that maps to an HTTP endpoint.
STEER_ACTIONS = frozenset({"approve", "reject", "resume", "abandon", "cancel"})

#: Steering actions that accept an operator note, and the body key each one
#: expects (``approveWorkflowRunBodySchema`` / ``rejectWorkflowRunBodySchema``,
#: ``workflow.schemas.ts:155-162``). Actions absent from this map take no body.
_STEER_NOTE_KEYS = {"approve": "comment", "reject": "reason"}

#: Verbatim port of ``TERMINAL_WORKFLOW_STATUSES`` / ``RESUMABLE_WORKFLOW_STATUSES``
#: from ``packages/workflows/src/schemas/workflow-run.ts:22-32``. Exported so
#: consumers decide run lifecycle from Archon's own definition instead of
#: inventing a parallel one.
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})
RESUMABLE_RUN_STATUSES = frozenset({"failed", "paused"})

#: Archon validates web conversation ids with ``/^[\w-]+$/`` before building an
#: upload directory from them (``api.ts:2496``). Mirrored exactly so a traversal
#: attempt fails here with a ValueError instead of a confusing remote 400.
_CONVERSATION_ID_RE = re.compile(r"^[\w-]+$")

#: Archon run ids are hex strings; keep the accepted set tight because the value
#: is interpolated into a URL path.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

#: Workflow names resolve to a yaml filename under ``.archon/workflows/``.
_WORKFLOW_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------


def _receipt(message: str) -> None:
    """Print an operator receipt to stderr, defensively.

    Receipt text can carry an env value or a localized OS error string, and a
    Windows console handed a cp1252 codec raises UnicodeEncodeError on the
    first non-ASCII byte — turning a diagnostic line into the exception that
    takes down the caller. Encode to ASCII with escapes, and swallow a failed
    write rather than let a log line become the failure.
    """
    try:
        print(message.encode("ascii", "backslashreplace").decode("ascii"),
              file=sys.stderr)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Config resolvers — Rule 1: resolved at CALL time, never bound as a default
# ---------------------------------------------------------------------------


def _base_url() -> str:
    """Return the Archon API base URL with trailing slashes stripped.

    Env: ``ARCHON_API_BASE_URL``, default ``http://127.0.0.1:3090``.

    A value that is not an http(s) URL is rejected in favour of the default —
    building a request against it would fail deep inside httpx with a message
    that names neither the env var nor the bad value. The fallback prints a
    receipt (house rule: every swallow leaves one).
    """
    raw = os.getenv("ARCHON_API_BASE_URL", "").strip() or DEFAULT_BASE_URL
    if not raw.startswith(("http://", "https://")):
        _receipt(
            f"[archon_client] ignoring ARCHON_API_BASE_URL={raw!r} "
            f"(not an http(s) URL); using {DEFAULT_BASE_URL}"
        )
        return DEFAULT_BASE_URL
    return raw.rstrip("/")


def _timeout_s() -> float:
    """Return the per-request timeout in seconds.

    Env: ``ARCHON_API_TIMEOUT_S``, default 15.0. Dispatch is safe under a short
    timeout because Archon's conversation lock stores the handler promise and
    returns immediately (``conversation-lock.ts:82-104``) — the HTTP response
    does NOT wait for the workflow to finish.

    A non-numeric or non-positive value falls back to the default with a
    receipt rather than raising, so a fat-fingered ``.env`` cannot take the bot
    down on an unrelated code path.
    """
    raw = os.getenv("ARCHON_API_TIMEOUT_S", "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        _receipt(
            f"[archon_client] ignoring ARCHON_API_TIMEOUT_S={raw!r} "
            f"(not a number); using {DEFAULT_TIMEOUT_S}"
        )
        return DEFAULT_TIMEOUT_S
    if value <= 0:
        _receipt(
            f"[archon_client] ignoring ARCHON_API_TIMEOUT_S={raw!r} "
            f"(must be > 0); using {DEFAULT_TIMEOUT_S}"
        )
        return DEFAULT_TIMEOUT_S
    return value


def _archon_port() -> int:
    """Return the TCP port of the configured Archon base URL.

    Used by :func:`check_loopback_posture` so the exposure probe targets the
    port we actually dial, not a hardcoded guess. Falls back to
    :data:`DEFAULT_ARCHON_PORT` when the URL carries no explicit port.
    """
    try:
        parsed = httpx.URL(_base_url())
    except Exception:  # pragma: no cover — _base_url already guards the scheme
        return DEFAULT_ARCHON_PORT
    return parsed.port or (443 if parsed.scheme == "https" else DEFAULT_ARCHON_PORT)


def _archon_host() -> str:
    """Hostname of the configured Archon base URL ('' when unparseable)."""
    try:
        return httpx.URL(_base_url()).host
    except Exception:  # pragma: no cover — _base_url already guards the scheme
        return ""


def _is_local_host(host: str) -> bool:
    """True only for hosts this machine's own interface probes can speak for."""
    return host == "localhost" or host == "::1" or host.startswith("127.")


# ---------------------------------------------------------------------------
# Friendly error hierarchy
# ---------------------------------------------------------------------------


class ArchonAPIError(Exception):
    """Base class for every archon_client failure.

    Carries ``friendly_message`` so voice/chat handlers can speak it verbatim
    instead of leaking a stack trace.
    """

    friendly_message: str = "Archon API error."


class ArchonUnreachableError(ArchonAPIError):
    """``httpx.ConnectError`` — the Archon server is not answering."""

    friendly_message = (
        "Archon is not reachable on its API port. Check the PM2 app is online "
        "(`pm2 list`) and that ARCHON_API_BASE_URL points at it."
    )


class ArchonTimeoutError(ArchonAPIError):
    """``httpx.TimeoutException`` — Archon accepted the socket but did not answer.

    Dispatch returns immediately by design, so a timeout here means Archon is
    wedged (or the timeout knob is set absurdly low), not that the work is slow.
    """

    friendly_message = (
        "Archon accepted the connection but did not answer in time. It may be "
        "wedged — check the server logs before retrying."
    )


class ArchonAuthError(ArchonAPIError):
    """HTTP 401/403.

    Archon ships with web auth OFF on this box (F7), so this only fires once
    Better Auth or a proxy-header trust is turned on and the Homie has no
    matching identity.
    """

    friendly_message = (
        "Archon refused the request as unauthenticated. Web auth is now on and "
        "the Homie has no identity configured for it."
    )


class ArchonBadRequestError(ArchonAPIError):
    """HTTP 400 — Archon rejected the payload shape.

    Most commonly an unknown ``codebaseId`` (``api.ts:2359-2363``) or an empty
    message body.
    """

    friendly_message = "Archon rejected the request as malformed."


class ArchonNotFoundError(ArchonAPIError):
    """HTTP 404 — no run or conversation with that id."""

    friendly_message = "Archon has no record with that id."


class ArchonServerError(ArchonAPIError):
    """HTTP 5xx — Archon errored internally."""

    friendly_message = "Archon hit an internal error. Check its server log."


class ArchonProtocolError(ArchonAPIError):
    """A 2xx response whose body is not the shape Archon documents.

    Distinct from :class:`ArchonBadRequestError` (which is Archon rejecting US) — this
    is Archon answering in a shape this client cannot read, i.e. a version skew
    between the Homie and the Archon build. Raising rather than coercing keeps
    a silent-wrong-answer out of the steering path.
    """

    friendly_message = (
        "Archon answered in an unexpected shape — the Homie's client and the "
        "Archon build may have drifted apart."
    )


# ---------------------------------------------------------------------------
# Typed wire records
#
# Every ``from_wire`` is TOLERANT of unknown/absent optional keys and keeps the
# untouched payload in ``raw``: Archon is a separately-versioned system, and a
# new field it adds must never crash the Homie. Fields the client's own
# behaviour depends on (ids) are REQUIRED — a missing one is a protocol error,
# not something to paper over with a default.
# ---------------------------------------------------------------------------


def _require_str(payload: dict[str, Any], key: str, what: str) -> str:
    """Return ``payload[key]`` as a non-empty string or raise ArchonProtocolError."""
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ArchonProtocolError(f"{what}: missing or non-string {key!r}")
    return value


def _opt_str(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True)
class ArchonDispatch:
    """Result of an atomic create-conversation-and-dispatch.

    Wire shape (``api.ts:2415-2420`` merged with ``dispatchToOrchestrator``'s
    ``{accepted, status}`` return at ``api.ts:2114``)::

        {"conversationId": str, "id": str, "dispatched": true,
         "accepted": true, "status": "started"|"queued-conversation"|"queued-capacity"}

    Attributes:
        conversation_id: the PLATFORM conversation id (``web-<ts>-<rand>``).
            This is what every other ``/api/conversations/{id}/…`` route takes.
        conversation_db_id: the DATABASE row id. This is the value that appears
            as ``parent_conversation_id`` on the resulting workflow run — see
            :func:`list_runs` for why the distinction matters.
        dispatched: False when the conversation was created without a message.
        accepted: Archon accepted the message for processing.
        status: the conversation-lock outcome — ``started`` means it is running
            now; ``queued-conversation`` / ``queued-capacity`` mean it is behind
            other work. NOT a workflow status.
    """

    conversation_id: str
    conversation_db_id: str
    dispatched: bool
    accepted: bool
    status: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> ArchonDispatch:
        return cls(
            conversation_id=_require_str(payload, "conversationId", "dispatch"),
            conversation_db_id=_require_str(payload, "id", "dispatch"),
            dispatched=bool(payload.get("dispatched", False)),
            accepted=bool(payload.get("accepted", False)),
            status=str(payload.get("status", "")),
            raw=payload,
        )


@dataclass(frozen=True)
class ArchonRun:
    """A workflow run row (``workflow-run.ts:110-132`` + detail-only extras).

    ``status`` is one of pending/running/completed/failed/cancelled/paused —
    compare against :data:`TERMINAL_RUN_STATUSES` / :data:`RESUMABLE_RUN_STATUSES`
    rather than hardcoding the strings.
    """

    id: str
    workflow_name: str
    status: str
    conversation_id: str | None
    parent_conversation_id: str | None
    parent_run_id: str | None
    codebase_id: str | None
    user_message: str
    working_path: str | None
    started_at: str | None
    completed_at: str | None
    last_activity_at: str | None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Present only on the run-detail fetch (``api.ts:3568-3574``).
    worker_platform_id: str | None = None
    parent_platform_id: str | None = None
    conversation_platform_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> ArchonRun:
        if not isinstance(payload, dict):
            raise ArchonProtocolError("run: expected an object")
        metadata = payload.get("metadata")
        return cls(
            id=_require_str(payload, "id", "run"),
            workflow_name=str(payload.get("workflow_name", "")),
            status=str(payload.get("status", "")),
            conversation_id=_opt_str(payload, "conversation_id"),
            parent_conversation_id=_opt_str(payload, "parent_conversation_id"),
            parent_run_id=_opt_str(payload, "parent_run_id"),
            codebase_id=_opt_str(payload, "codebase_id"),
            user_message=str(payload.get("user_message", "")),
            working_path=_opt_str(payload, "working_path"),
            started_at=_opt_str(payload, "started_at"),
            completed_at=_opt_str(payload, "completed_at"),
            last_activity_at=_opt_str(payload, "last_activity_at"),
            metadata=metadata if isinstance(metadata, dict) else {},
            worker_platform_id=_opt_str(payload, "worker_platform_id"),
            parent_platform_id=_opt_str(payload, "parent_platform_id"),
            conversation_platform_id=_opt_str(payload, "conversation_platform_id"),
            raw=payload,
        )

    @property
    def is_terminal(self) -> bool:
        """True when Archon considers this run finished and un-transitionable."""
        return self.status in TERMINAL_RUN_STATUSES

    @property
    def is_paused(self) -> bool:
        """True when the run sits at an approval / interactive-loop gate."""
        return self.status == "paused"


@dataclass(frozen=True)
class ArchonEvent:
    """A workflow event row (``packages/core/src/schemas/workflow-event.ts``).

    ``data`` is a free-form JSON blob whose contents vary by ``event_type`` —
    ``tool_called`` / ``tool_completed`` carry ``tool_name``, ``tool_input`` and
    ``duration_ms``. Treat every value in it as untrusted: it originates in an
    LLM's tool call.
    """

    id: str
    workflow_run_id: str
    event_type: str
    step_index: int | None
    step_name: str | None
    created_at: str | None
    data: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> ArchonEvent:
        if not isinstance(payload, dict):
            raise ArchonProtocolError("event: expected an object")
        data = payload.get("data")
        step_index = payload.get("step_index")
        return cls(
            id=str(payload.get("id", "")),
            workflow_run_id=str(payload.get("workflow_run_id", "")),
            event_type=str(payload.get("event_type", "")),
            step_index=step_index if isinstance(step_index, int) else None,
            step_name=_opt_str(payload, "step_name"),
            created_at=_opt_str(payload, "created_at"),
            data=data if isinstance(data, dict) else {},
            raw=payload,
        )


@dataclass(frozen=True)
class ArchonRunDetail:
    """``GET /api/workflows/runs/{runId}`` — the run plus its FULL event log.

    One call, no pagination: Archon returns every event for the run. That is
    fine as an on-demand narration fetch and wrong as a poll loop — see the
    module docstring's F6 note.
    """

    run: ArchonRun
    events: tuple[ArchonEvent, ...]

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> ArchonRunDetail:
        run_payload = payload.get("run")
        if not isinstance(run_payload, dict):
            raise ArchonProtocolError("run detail: missing 'run' object")
        events_payload = payload.get("events")
        events = events_payload if isinstance(events_payload, list) else []
        return cls(
            run=ArchonRun.from_wire(run_payload),
            events=tuple(
                ArchonEvent.from_wire(e) for e in events if isinstance(e, dict)
            ),
        )


@dataclass(frozen=True)
class ArchonSteerResult:
    """``{success, message}`` from any steering endpoint (``workflow.schemas.ts:145-152``)."""

    action: str
    run_id: str
    success: bool
    message: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


# ---------------------------------------------------------------------------
# Input validation — contract errors raised BEFORE any network work
# ---------------------------------------------------------------------------


def _validate_conversation_id(conversation_id: str) -> str:
    if not isinstance(conversation_id, str) or not conversation_id:
        raise ValueError("conversation_id must be a non-empty string")
    if not _CONVERSATION_ID_RE.match(conversation_id):
        # Archon rejects these itself (api.ts:2496) to block path traversal into
        # its upload directory; failing here names the real problem.
        raise ValueError(
            f"conversation_id {conversation_id!r} must match [A-Za-z0-9_-]+"
        )
    return conversation_id


def _validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id must be a non-empty string")
    if not _RUN_ID_RE.match(run_id):
        raise ValueError(f"run_id {run_id!r} must match [A-Za-z0-9_-]+")
    return run_id


def _validate_message(message: str) -> str:
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message must be a non-blank string")
    return message


def _validate_codebase_id(codebase_id: str) -> str:
    if not isinstance(codebase_id, str) or not codebase_id.strip():
        # A conversation with no codebase gets no codebase binding, so the
        # orchestrator's isolation pre-flight has nothing to resolve against and
        # the work lands in the wrong tree. Refuse rather than dispatch blind.
        raise ValueError("codebase_id must be a non-blank string")
    return codebase_id.strip()


# ---------------------------------------------------------------------------
# Internal HTTP plumbing — Rule 2: no module-level cached client
# ---------------------------------------------------------------------------


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    """Return the JSON object body, or ``{}`` for an empty/non-JSON 2xx body."""
    try:
        data = response.json()
    except Exception:
        return {}
    if isinstance(data, dict):
        return data
    return {"_data": data}


def _check_status(response: httpx.Response) -> dict[str, Any]:
    """Map an HTTP status onto the friendly error hierarchy, else return the body."""
    code = response.status_code
    if code in (401, 403):
        raise ArchonAuthError()
    if code == 404:
        raise ArchonNotFoundError()
    if code == 400:
        raise ArchonBadRequestError()
    if code >= 500:
        raise ArchonServerError()
    if code >= 400:
        # Unmapped 4xx (405, 409, 422 …) — from the caller's seat these are all
        # "Archon did not accept this request as shaped".
        raise ArchonBadRequestError()
    return _safe_json(response)


async def _request(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    client: httpx.AsyncClient | None,
) -> dict[str, Any]:
    """Issue one request and map transport/status failures to typed errors.

    Rule 2: when ``client`` is None a fresh ``AsyncClient`` is created with an
    explicit timeout and closed on exit. A caller-supplied client owns its own
    lifecycle AND its own timeout (that is how tests inject ``MockTransport``).
    """
    url = f"{_base_url()}{path}"
    try:
        if client is not None:
            response = await client.request(
                method, url, json=json_body, params=params
            )
        else:
            async with httpx.AsyncClient(timeout=_timeout_s()) as owned:
                response = await owned.request(
                    method, url, json=json_body, params=params
                )
    except httpx.ConnectError as exc:
        raise ArchonUnreachableError() from exc
    except httpx.TimeoutException as exc:
        raise ArchonTimeoutError() from exc
    except httpx.HTTPError as exc:
        raise ArchonAPIError() from exc
    return _check_status(response)


# ---------------------------------------------------------------------------
# Public surface — dispatch
# ---------------------------------------------------------------------------


async def create_conversation_and_dispatch(
    codebase_id: str,
    message: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> ArchonDispatch:
    """``POST /api/conversations`` with ``{codebaseId, message}`` — create + dispatch.

    The atomic form: Archon creates the conversation, persists the message,
    sets a placeholder title, and hands the message to ``handleMessage()`` in
    one round trip (``api.ts:2353-2428``). Doing it atomically avoids the ghost
    "Untitled conversation" a create-then-send pair leaves behind.

    Spike A (2026-07-27, run ``23c6c29ad89b24d6e662af355bbd4158``) proved this
    path gets the orchestrator's full pre-flight — isolation worktree, correct
    codebase binding, normal node events — despite NULL web attribution.

    Returns immediately: the conversation lock stores the handler promise and
    returns a status without awaiting the run.

    Args:
        codebase_id: a registered Archon codebase id. Required — see
            :func:`_validate_codebase_id` for why a blank one is refused.
        message: what to send. For a workflow, prefer :func:`dispatch_workflow`
            so the ``/workflow run …`` string is built exactly once, here.

    Raises:
        ValueError: blank ``codebase_id`` or blank ``message``.
        ArchonBadRequestError: HTTP 400 — usually an unknown ``codebaseId``.
        ArchonUnreachableError / ArchonTimeoutError / ArchonServerError: transport.
        ArchonProtocolError: 2xx body without ``conversationId`` / ``id``.
    """
    codebase_id = _validate_codebase_id(codebase_id)
    message = _validate_message(message)
    body = await _request(
        "POST",
        "/api/conversations",
        json_body={"codebaseId": codebase_id, "message": message},
        client=client,
    )
    return ArchonDispatch.from_wire(body)


def build_workflow_message(workflow: str, text: str) -> str:
    """Return the exact orchestrator string Archon's own run endpoint builds.

    ``api.ts:3094``::

        const fullMessage = `/workflow run ${workflowName} ${message}`;

    Exposed as a function (and reused by :func:`dispatch_workflow`) so the
    format lives in exactly one place. A caller that hand-assembles this string
    can silently drift from Archon's and lose the workflow dispatch entirely —
    the orchestrator would treat it as ordinary chat.

    Raises:
        ValueError: malformed workflow name, or blank ``text``. A blank brief is
            refused because the orchestrator uses
            ``synthesizedPrompt ?? originalMessage`` — an empty original is how
            a vague voice turn ("yeah, do that") reaches the worker with no task
            in it at all.
    """
    if not isinstance(workflow, str) or not _WORKFLOW_NAME_RE.match(workflow):
        raise ValueError(
            f"workflow {workflow!r} must match [A-Za-z0-9][A-Za-z0-9._-]*"
        )
    text = _validate_message(text)
    return f"/workflow run {workflow} {text}"


async def dispatch_workflow(
    codebase_id: str,
    workflow: str,
    text: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> ArchonDispatch:
    """Dispatch a named workflow through the orchestrator (F3-safe path).

    Thin composition of :func:`build_workflow_message` and
    :func:`create_conversation_and_dispatch`. This is the entry point callers
    should reach for; there is deliberately no raw run-POST helper in this
    module because there is no raw run path in Archon.

    ``text`` must already be a complete, self-contained brief — the caller's
    obligation (F2), enforced upstream by the dispatch tool, not here. All this
    function guarantees is that a BLANK brief never leaves the box.
    """
    message = build_workflow_message(workflow, text)
    return await create_conversation_and_dispatch(
        codebase_id, message, client=client
    )


async def send_message(
    conversation_id: str,
    text: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """``POST /api/conversations/{id}/message`` — steering / NL-approval transport.

    This is the natural-language steering primitive (F4): when a run is paused,
    ANY non-slash message on its conversation becomes the approval
    (``orchestrator-agent.ts:901-1020``), so "looks good, ship it" resumes a
    paused DAG without touching the approve endpoint.

    ``conversation_id`` is the PLATFORM id (``web-…``), not the database row id.

    Returns the raw ``{accepted, status}`` dispatch body — the status is the
    conversation-lock outcome, not a workflow status.
    """
    conversation_id = _validate_conversation_id(conversation_id)
    text = _validate_message(text)
    return await _request(
        "POST",
        f"/api/conversations/{conversation_id}/message",
        json_body={"message": text},
        client=client,
    )


# ---------------------------------------------------------------------------
# Public surface — read
# ---------------------------------------------------------------------------


async def get_run(
    run_id: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> ArchonRunDetail:
    """``GET /api/workflows/runs/{runId}`` — run + full event log in ONE call.

    The on-demand detail fetch behind ``check_work``-style narration. Not a
    poll loop: it returns every event for the run with no pagination, and the
    live tail is a separate read-only DB cursor (F6).

    Raises:
        ArchonNotFoundError: HTTP 404 — no run with that id.
    """
    run_id = _validate_run_id(run_id)
    body = await _request(
        "GET", f"/api/workflows/runs/{run_id}", client=client
    )
    return ArchonRunDetail.from_wire(body)


async def list_runs(
    limit: int = 50,
    *,
    conversation_id: str | None = None,
    status: str | None = None,
    codebase_id: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[ArchonRun]:
    """``GET /api/workflows/runs`` — recent runs, newest first.

    Args:
        limit: server-clamped to 1..200 (``api.ts:3499``); 50 is Archon's own
            default.
        conversation_id: **the run's own DATABASE conversation id**, not a
            platform id and not necessarily yours. Load-bearing gotcha: for a
            web-dispatched run Archon spawns a WORKER conversation and sets
            ``run.conversation_id`` to that worker, putting the dispatching
            conversation in ``run.parent_conversation_id``
            (``api.ts:3546-3559``). The filter is a plain
            ``conversation_id = $1`` (``db/workflows.ts:1228-1231``), so
            filtering by the id :class:`ArchonDispatch` handed you matches
            NOTHING. To find the run you dispatched, list unfiltered and match
            ``parent_conversation_id == dispatch.conversation_db_id``.
        status: one of pending/running/completed/failed/cancelled/paused. An
            unrecognised value is silently dropped by Archon (``api.ts:3493``),
            so it is validated here instead — a typo would otherwise return
            every run and read as "no filtering happened, must be fine".
        codebase_id: filters via the conversation's codebase.

    Raises:
        ValueError: non-positive ``limit`` or an unknown ``status``.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit must be a positive int")
    params: dict[str, Any] = {"limit": limit}
    if status is not None:
        known = TERMINAL_RUN_STATUSES | RESUMABLE_RUN_STATUSES | {"pending", "running"}
        if status not in known:
            raise ValueError(
                f"status {status!r} is not an Archon run status ({sorted(known)})"
            )
        params["status"] = status
    if conversation_id is not None:
        params["conversationId"] = conversation_id
    if codebase_id is not None:
        params["codebaseId"] = codebase_id
    body = await _request(
        "GET", "/api/workflows/runs", params=params, client=client
    )
    runs = body.get("runs")
    if not isinstance(runs, list):
        raise ArchonProtocolError("run list: missing 'runs' array")
    return [ArchonRun.from_wire(r) for r in runs if isinstance(r, dict)]


# ---------------------------------------------------------------------------
# Public surface — steer
# ---------------------------------------------------------------------------


async def steer(
    run_id: str,
    action: str,
    *,
    note: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> ArchonSteerResult:
    """``POST /api/workflows/runs/{runId}/{action}`` — the steering gate.

    Mirrors ``manage-run-tool.ts``'s action discriminator for the five actions
    that map to an HTTP endpoint. The two-step confirm-preview that tool wraps
    destructive actions in is a CALLER concern (it is a conversation contract,
    not a transport one) — this function performs the action it is given.

    Args:
        run_id: the Archon run id.
        action: one of :data:`STEER_ACTIONS`.
        note: the operator's comment (``approve``) or reason (``reject``).
            ``resume`` / ``abandon`` / ``cancel`` take no body, so passing a
            note with one of those raises rather than silently discarding the
            operator's stated reason.

    Raises:
        ValueError: unknown action, malformed run id, or a note on an action
            that has nowhere to put it.
        ArchonNotFoundError: HTTP 404 — no run with that id.
        ArchonBadRequestError: HTTP 400 — e.g. approving a run that is not paused.
    """
    run_id = _validate_run_id(run_id)
    if not isinstance(action, str) or action not in STEER_ACTIONS:
        raise ValueError(
            f"action {action!r} must be one of {sorted(STEER_ACTIONS)}"
        )
    note_key = _STEER_NOTE_KEYS.get(action)
    if note is not None and note_key is None:
        raise ValueError(
            f"action {action!r} takes no note; Archon's {action} endpoint has "
            "no body field to carry one"
        )
    json_body: dict[str, Any] | None = None
    if note_key is not None:
        # Archon's approve/reject routes declare a JSON body; send the object
        # even when empty so the route's validator has something to parse.
        json_body = {} if note is None else {note_key: note}
    body = await _request(
        "POST",
        f"/api/workflows/runs/{run_id}/{action}",
        json_body=json_body,
        client=client,
    )
    return ArchonSteerResult(
        action=action,
        run_id=run_id,
        success=bool(body.get("success", False)),
        message=str(body.get("message", "")),
        raw=body,
    )


# ---------------------------------------------------------------------------
# Ops posture — F7 loopback pinning
#
# Rule 2: this reads PHYSICAL state. It does not ask what HOST is set to, and it
# does not read a config file — it tries to open a TCP connection to this box's
# own LAN address and reports what actually happened. A pin that "is configured"
# but did not take effect (server not restarted, env precedence lost to a shell
# export) fails this check, which is the entire point.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoopbackPosture:
    """Physical exposure of the Archon API port on this host's LAN addresses.

    Attributes:
        port: the port probed (from ``ARCHON_API_BASE_URL``).
        lan_addresses: non-loopback IPv4 addresses this host answers on.
        reachable_addresses: the subset that ACCEPTED a TCP connection — i.e.
            the addresses from which a LAN peer could drive Archon.
        pinned: True only when at least one LAN address was actually probed
            and none accepted a connection. False both when a LAN address
            accepted a connection AND when ``checked`` is False — an
            untestable posture fails closed rather than certifying a pin
            nobody verified.
        checked: False when the host has no non-loopback address to probe (an
            offline box, or a lookup failure), OR when a probed address never
            returned an explicit refusal (a timeout, a dropped connection,
            any other non-``ConnectionRefusedError`` ``OSError``) — in either
            case ``pinned`` proves nothing and is always False.
    """

    port: int
    lan_addresses: tuple[str, ...]
    reachable_addresses: tuple[str, ...]
    pinned: bool
    checked: bool
    #: Set when ARCHON_API_BASE_URL targets a host this machine's interface
    #: probes cannot speak for (Kimi round-1 major): the probe enumerates
    #: LOCAL addresses, so a remote target must refuse certification rather
    #: than emit the strongest pass output about a host it never probed.
    remote_host: str | None = None

    @property
    def summary(self) -> str:
        """One operator-readable line."""
        if not self.checked:
            if self.remote_host:
                return (
                    f"Archon :{self.port} — ARCHON_API_BASE_URL targets "
                    f"{self.remote_host}, not this machine; loopback posture "
                    "must be verified ON that host, not from here."
                )
            if not self.lan_addresses:
                return (
                    f"Archon :{self.port} — no non-loopback address on this "
                    "host; LAN exposure could not be tested."
                )
            return (
                f"Archon :{self.port} — connection attempt to "
                f"{', '.join(self.lan_addresses)} was inconclusive (timeout "
                "or another non-refusal error, not an explicit refusal); "
                "LAN exposure could not be verified."
            )
        if self.pinned:
            return (
                f"Archon :{self.port} is PINNED to loopback — refused on "
                f"{', '.join(self.lan_addresses)}."
            )
        return (
            f"Archon :{self.port} is EXPOSED on "
            f"{', '.join(self.reachable_addresses)} with no auth. Anything on "
            "the LAN can read prompts and approve or cancel work."
        )


def _local_ipv4_addresses() -> tuple[str, ...]:
    """Return this host's non-loopback IPv4 addresses.

    Module-level so tests can substitute it (Rule 3 shape — callers reach it
    through the module, so a monkeypatch propagates).
    """
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError as exc:
        _receipt(f"[archon_client] address lookup failed: {exc}")
        return ()
    seen: list[str] = []
    for info in infos:
        # sockaddr is (host, port) for AF_INET but the stdlib types it as a
        # union covering AF_INET6's 4-tuple, so narrow before use rather than
        # trusting the family filter.
        address = info[4][0]
        if not isinstance(address, str):
            continue
        if address.startswith("127.") or address in seen:
            continue
        seen.append(address)
    return tuple(seen)


#: Outcome of a single :func:`_tcp_connect` probe. Only ``"refused"`` is
#: proof nothing is listening — a timeout or any other non-refusal
#: ``OSError`` is ambiguous (a host firewall silently dropping the SYN, a
#: transient network blip) and must never be treated as equivalent proof, or
#: an untestable address gets falsely certified `pinned=True` (Codex round-2
#: MAJOR: the previous bool-returning version collapsed every non-accept
#: outcome, including a bare socket timeout, into "refused").
_ProbeOutcome = Literal["accepted", "refused", "inconclusive"]


def _tcp_connect(address: str, port: int, timeout: float) -> _ProbeOutcome:
    """Classify a TCP connect attempt to ``address:port``.

    Module-level for the same substitution reason as
    :func:`_local_ipv4_addresses`. BLOCKING — see the module docstring.
    """
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return "accepted"
    except ConnectionRefusedError:
        return "refused"
    except OSError:
        return "inconclusive"


def check_loopback_posture(
    port: int | None = None,
    *,
    timeout: float = 1.0,
) -> LoopbackPosture:
    """Probe whether Archon's API port is reachable from this host's LAN address.

    Rule 1: ``port=None`` resolves from ``ARCHON_API_BASE_URL`` at call time.

    BLOCKING socket work — from async code call it as
    ``await asyncio.to_thread(archon_client.check_loopback_posture)``. Do not
    pass a config-derived port positionally into ``to_thread``: to_thread's
    ARGUMENTS are evaluated on the event loop, so resolve it inside instead
    (which the ``None`` default already does for you).
    """
    resolved_port = _archon_port() if port is None else port
    target_host = _archon_host()
    if port is None and not _is_local_host(target_host):
        # Kimi round-1 MAJOR: port comes from the base URL but addresses come
        # from THIS machine — inconsistent scope. When the configured Archon
        # is remote, probing local interfaces proves nothing about it; the
        # module invariant (an untestable posture never certifies a pin)
        # gains this dimension rather than an exception to it.
        return LoopbackPosture(
            port=resolved_port,
            lan_addresses=(),
            reachable_addresses=(),
            pinned=False,
            checked=False,
            remote_host=target_host or "<unparseable base URL>",
        )
    addresses = _local_ipv4_addresses()
    if not addresses:
        # Fail closed: an untestable posture must never assert `pinned=True`.
        # `addresses` is also empty when `_local_ipv4_addresses` swallowed an
        # OSError from a transient lookup failure, not only on a genuinely
        # offline box — vacuously "no LAN address accepted a connection" is
        # not evidence of a pin. `checked=False` already tells callers this
        # proves nothing; `pinned=False` keeps the CLI exit code (and any
        # other caller gating on `.pinned` alone) from falsely certifying
        # security when address enumeration fails.
        return LoopbackPosture(
            port=resolved_port,
            lan_addresses=(),
            reachable_addresses=(),
            pinned=False,
            checked=False,
        )
    outcomes = {a: _tcp_connect(a, resolved_port, timeout) for a in addresses}
    reachable = tuple(a for a in addresses if outcomes[a] == "accepted")
    inconclusive = tuple(a for a in addresses if outcomes[a] == "inconclusive")
    if reachable:
        # At least one LAN address accepted a connection — definitively exposed.
        return LoopbackPosture(
            port=resolved_port,
            lan_addresses=addresses,
            reachable_addresses=reachable,
            pinned=False,
            checked=True,
        )
    if inconclusive:
        # Fail closed: a timeout or other non-refusal error is not proof of a
        # pin. Certifying it as one is exactly the bug Codex round-2 caught —
        # an untestable address (blackholed, firewall-dropped SYN) reported
        # `pinned=True` with zero actual evidence. Same "untestable never
        # certifies a pin" rule as the no-address branch above.
        return LoopbackPosture(
            port=resolved_port,
            lan_addresses=addresses,
            reachable_addresses=(),
            pinned=False,
            checked=False,
        )
    # Every probed address came back with an explicit refusal — that is the
    # only outcome that actually proves nothing is listening.
    return LoopbackPosture(
        port=resolved_port,
        lan_addresses=addresses,
        reachable_addresses=(),
        pinned=True,
        checked=True,
    )


def _main(argv: list[str]) -> int:
    """``uv run python -m integrations.archon_client posture`` — the ops check.

    Exit code 0 = pinned AND verified. Any nonzero exit (1) means the pin is
    NOT proven: either a LAN address answered (exposed), this host had no
    non-loopback address to probe (untestable), or a probed address returned
    something other than an explicit refusal (also untestable) — fail closed
    rather than certify a pin nobody checked. Read the printed summary line
    to tell "exposed" from "untestable"; an automated caller can safely treat
    any nonzero exit as "not verified safe" without parsing it.
    """
    command = argv[1] if len(argv) > 1 else "posture"
    if command != "posture":
        print(f"usage: {argv[0]} posture", file=sys.stderr)
        return 2
    posture = check_loopback_posture()
    print(posture.summary)
    return 0 if posture.pinned else 1


if __name__ == "__main__":  # pragma: no cover — CLI entry point
    raise SystemExit(_main(sys.argv))


__all__ = [
    # Config / constants
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT_S",
    "STEER_ACTIONS",
    "TERMINAL_RUN_STATUSES",
    "RESUMABLE_RUN_STATUSES",
    # Records
    "ArchonDispatch",
    "ArchonRun",
    "ArchonEvent",
    "ArchonRunDetail",
    "ArchonSteerResult",
    "LoopbackPosture",
    # Dispatch
    "build_workflow_message",
    "create_conversation_and_dispatch",
    "dispatch_workflow",
    "send_message",
    # Read
    "get_run",
    "list_runs",
    # Steer
    "steer",
    # Ops
    "check_loopback_posture",
    # Errors
    "ArchonAPIError",
    "ArchonUnreachableError",
    "ArchonTimeoutError",
    "ArchonAuthError",
    "ArchonBadRequestError",
    "ArchonNotFoundError",
    "ArchonServerError",
    "ArchonProtocolError",
]
