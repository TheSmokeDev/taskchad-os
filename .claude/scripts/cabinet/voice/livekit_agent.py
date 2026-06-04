"""Cabinet handoff hooks for the LiveKit voice transport spike.

LiveKit owns browser media transport in this lane. Cabinet still owns routing,
memory, transcript persistence, and persona behavior. The first testable
boundary is final transcript -> Cabinet text orchestrator.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger("cabinet.voice.livekit_agent")

from security import redact as _redact_mod  # noqa: E402
_redact = _redact_mod.redact


async def handoff_transcript_to_cabinet(
    *,
    meeting_id: int,
    chat_id: str | None,
    transcript: str,
    client_msg_id: str | None = None,
    cabinet_api_module=None,
) -> dict[str, Any]:
    """Post one final LiveKit transcript into Cabinet's normal router path."""

    text = (transcript or "").strip()
    if not text:
        return {"ok": True, "ignored": "empty_transcript"}

    if cabinet_api_module is None:
        from integrations import cabinet_api as cabinet_api_module  # noqa: PLC0415

    message_id = client_msg_id or f"lk_{uuid.uuid4().hex}"
    logger.info(
        "livekit_transcript_handoff meeting=%s chat=%s bytes=%s",
        _redact(str(meeting_id)),
        _redact(str(chat_id or "")),
        _redact(str(len(text.encode("utf-8")))),
    )
    return await cabinet_api_module.send_message(
        meeting_id=meeting_id,
        text=text,
        client_msg_id=message_id,
        chat_id=chat_id or None,
        is_voice=True,
        audience="auto",
        target_agent_id=None,
    )


def register_user_transcript_handoff(session, *, meeting_id: int, chat_id: str | None) -> None:
    """Register a LiveKit Agents final-transcript callback on ``session``.

    This stays import-light so the module is usable without LiveKit installed.
    A real LiveKit ``AgentSession`` emits ``user_input_transcribed`` events
    with ``transcript`` and ``is_final`` attributes.
    """

    @session.on("user_input_transcribed")
    def _on_user_input_transcribed(event) -> None:
        if not getattr(event, "is_final", False):
            return
        transcript = getattr(event, "transcript", "")
        try:
            import asyncio  # noqa: PLC0415

            asyncio.create_task(
                handoff_transcript_to_cabinet(
                    meeting_id=meeting_id,
                    chat_id=chat_id,
                    transcript=transcript,
                )
            )
        except RuntimeError:
            logger.warning("livekit transcript handoff skipped: no running event loop")


__all__ = [
    "handoff_transcript_to_cabinet",
    "register_user_transcript_handoff",
]
