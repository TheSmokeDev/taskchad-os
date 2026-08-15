"""Discord voice API — thin FastAPI router over ``discord_voice_lifecycle``.

Mounted on the orchestration API like the talk router. The chat process
calls these routes over loopback with the orchestration Bearer token; the
sidecar subprocess itself (py-cord + DAVE) is spawned and reaped by
``discord_voice_lifecycle`` — this module only maps HTTP to lifecycle calls.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import discord_voice_lifecycle
from security import kill_switches

logger = logging.getLogger(__name__)

router = APIRouter()


class DiscordVoiceJoinBody(BaseModel):
    """Join target; ``textChannelId`` opts into transcript mirroring.

    ``operatorRole`` is the role the Discord adapter stamped on the operator
    who ran the admin-gated ``/talk join``. It is an AUDIT RECEIPT of who
    opened the session, logged by the sidecar — it is deliberately NOT what
    authorizes tool calls. A voice channel is multi-speaker, so each relayed
    call carries the speaker who actually talked and the main API process
    resolves that id against ``DISCORD_ALLOWED_USERS`` itself.
    """

    guildId: int
    channelId: int
    textChannelId: int | None = None
    operatorRole: str = "viewer"


@router.get("/api/discord/voice/status")
def get_discord_voice_status() -> dict:
    """Report sidecar state: lifecycle state file plus live bridge probe."""

    return {"ok": True, **discord_voice_lifecycle.status()}


@router.post("/api/discord/voice/join")
def join_voice(body: DiscordVoiceJoinBody) -> dict:
    """Ensure the sidecar is running and joined to the given voice channel."""

    try:
        kill_switches.requireEnabled("voice", caller="discord_voice_api.join")
        result = discord_voice_lifecycle.start_session(
            guild_id=body.guildId,
            channel_id=body.channelId,
            text_channel_id=body.textChannelId,
            operator_role=body.operatorRole,
        )
    except kill_switches.KillSwitchDisabled as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "voice features are disabled by operator",
                "switch": exc.switch_name,
            },
        ) from exc
    except discord_voice_lifecycle.DiscordVoiceError as exc:
        raise HTTPException(status_code=503, detail={"error": str(exc)}) from exc

    return {"ok": True, **result}


@router.post("/api/discord/voice/leave")
def leave_voice() -> dict:
    """Leave the voice channel and reap the sidecar subprocess."""

    result = discord_voice_lifecycle.stop_session()
    return {"ok": True, **result}


__all__ = ["router"]
