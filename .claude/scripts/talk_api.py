"""Talk mode API — thin FastAPI router over ``talk_session``.

Mounted on the orchestration API like the dashboard/pairing routers.
The browser Talk page calls these routes through the Hono proxy; auth is
the orchestration Bearer-token middleware. Only ephemeral client secrets
cross the wire — resolved OpenAI credentials never appear in responses.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import talk_flush
import talk_runs
import talk_session
import talk_tools
from security import kill_switches

logger = logging.getLogger(__name__)

router = APIRouter()


class TalkSessionBody(BaseModel):
    """Optional per-session overrides; blank values fall back to env defaults."""

    voice: str | None = None
    model: str | None = None


class TalkSpeakerBinding(BaseModel):
    """The sidecar's verdict on WHICH utterance produced this call.

    `trusted` is the whole contract: the sidecar resolved the speaker from the
    audio interval server VAD selected, and correlated it to this call through
    the Realtime response ID. `reason` explains a refusal (ambiguous speakers,
    unresolved attribution, a recycled response id) so the denial can be honest
    instead of a bare permission error. `token` is the binding's opaque id,
    carried for audit correlation only — it grants nothing.
    """

    token: str | None = None
    trusted: bool = False
    reason: str = ""


class TalkToolBody(BaseModel):
    """One Realtime function call, plus WHO it came from.

    The Discord voice sidecar is a separate process, so its speaker identity
    only exists here if it rides the wire. It sends the raw `speakerId` rather
    than a role: this process owns the allowlist and does the resolving, so a
    sidecar can name a speaker but can never assert an authority.

    `speakerBinding` is the other half of that: naming a speaker is not enough,
    the sidecar must also be able to prove the name belongs to the utterance
    the model answered. A body with no binding fails CLOSED on the voice
    transport — the sidecar always sends one, so its absence means an unknown
    client rather than an older trusted one.

    Defaults are the browser transport with no speaker, which resolves to the
    browser session's own role — so the browser client, which has no utterances
    to attribute, behaves exactly as before.
    """

    name: str
    arguments: dict = {}
    transport: str = "browser"
    speakerId: str | None = None
    speakerBinding: TalkSpeakerBinding | None = None


class TalkFlushItem(BaseModel):
    """One finalized transcript row from the browser Talk page."""

    role: str = ""
    text: str = ""


class TalkFlushBody(BaseModel):
    """Session-end debrief payload; everything optional so teardown never 422s."""

    sessionId: str = ""
    startedAt: str | None = None
    transcript: list[TalkFlushItem] = []


@router.get("/api/talk/status")
def get_talk_status() -> dict:
    """Report Talk readiness: auth source, model, voice, kill-switch state."""

    return {"ok": True, **talk_session.talk_status()}


@router.post("/api/talk/session")
def create_session(body: TalkSessionBody | None = None) -> dict:
    """Mint an ephemeral OpenAI Realtime client secret for the browser."""

    try:
        descriptor = talk_session.create_talk_session(
            voice=(body.voice if body else None),
            model=(body.model if body else None),
        )
    except kill_switches.KillSwitchDisabled as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "voice features are disabled by operator",
                "switch": exc.switch_name,
            },
        ) from exc
    except talk_session.TalkAuthError as exc:
        raise HTTPException(status_code=503, detail={"error": str(exc)}) from exc
    except talk_session.TalkUpstreamError as exc:
        logger.warning("talk session mint upstream failure: %s", exc)
        raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc
    except talk_session.TalkSessionError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    # Minting the session IS this process's authorization event. Every
    # /api/talk/* route is classified `admin` in orchestration/route_policy.py,
    # so reaching here means an admin-authorized caller opened the session, and
    # the BROWSER tool surface it just unlocked runs with that authority. Scoped
    # to that transport by name: a Discord-originated call resolves its own
    # speaker and never reads this.
    talk_tools.set_browser_session_role("admin")

    return {"ok": True, **descriptor.to_wire()}


@router.post("/api/talk/tool")
def execute_tool(body: TalkToolBody) -> dict:
    """Execute one minted-session function call via the Python tool surface.

    Execution failures return 200 with the error text so the model can say
    what broke; unknown tool names are a client bug and return 400.
    """

    try:
        kill_switches.requireEnabled("voice", caller="talk_api.tool")
    except kill_switches.KillSwitchDisabled as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "voice features are disabled by operator",
                "switch": exc.switch_name,
            },
        ) from exc
    try:
        output = talk_tools.execute_talk_tool(
            body.name,
            body.arguments,
            transport=body.transport,
            speaker_id=body.speakerId,
            binding=body.speakerBinding,
        )
    except kill_switches.KillSwitchDisabled as exc:
        # A PER-TOOL switch (e.g. archon_dispatch) fires inside the handler,
        # after the voice-switch guard above has already passed. Without this
        # arm it escaped as an unhandled 500 and took the Realtime tool
        # channel down with it (codex R4 major). Same 503 + switch shape the
        # voice guard uses, so the browser can say which switch is off.
        raise HTTPException(
            status_code=503,
            detail={
                "error": f"{body.name} is switched off by the operator",
                "switch": exc.switch_name,
            },
        ) from exc
    except talk_tools.TalkToolError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
    return {"ok": True, "output": output[:8000]}


@router.post("/api/talk/flush")
def flush_session(body: TalkFlushBody) -> dict:
    """Session-end vault debrief: transcript → detached memory_flush spawn.

    Fired by the Talk page on stop/close. Always 200 with a receipt —
    trivial sessions are skipped server-side, and a flush failure on
    teardown must never surface as a page error. 503 only for the voice
    kill switch, matching the tool route.
    """

    try:
        kill_switches.requireEnabled("voice", caller="talk_api.flush")
    except kill_switches.KillSwitchDisabled as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "voice features are disabled by operator",
                "switch": exc.switch_name,
            },
        ) from exc
    receipt = talk_flush.start_session_flush(
        [{"role": item.role, "text": item.text} for item in body.transcript],
        session_id=body.sessionId,
        started_at=body.startedAt,
    )
    return {"ok": True, **receipt}


@router.get("/api/talk/runs")
def list_runs(limit: int = 10, history: bool = False) -> dict:
    """Recent async runs (skill, agent, archon, look).

    Default shape is unchanged (the Talk page's poller). The dashboard Runs
    panel passes ``?limit=50&history=true`` to merge the persisted JSONL
    tail — runs from dead API processes appear with ``fromHistory: true``
    and orphaned ``running`` rows are reported as ``lost``.
    """

    return {"ok": True, "runs": talk_runs.list_runs(limit, include_history=history)}


@router.get("/api/talk/runs/{run_id}")
def get_run(run_id: int) -> dict:
    """Poll one async run; the Talk page injects the result when it lands."""

    run = talk_runs.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail={"error": f"unknown run #{run_id}"})
    return {"ok": True, "runId": run_id, **run}


@router.get("/api/talk/skill-runs/{run_id}")
def get_skill_run(run_id: int) -> dict:
    """Back-compat alias for the original skill-run poll route."""

    run = talk_tools.get_skill_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail={"error": f"unknown skill run #{run_id}"})
    return {"ok": True, "runId": run_id, **run}


__all__ = ["router"]
