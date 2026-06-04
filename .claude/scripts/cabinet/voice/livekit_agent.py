"""Cabinet LiveKit voice transport runner and transcript handoff.

LiveKit owns browser media transport in this lane. Cabinet still owns routing,
memory, transcript persistence, and persona behavior. The first testable
boundary is final transcript -> Cabinet text orchestrator.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("cabinet.voice.livekit_agent")

from cabinet.voice import livekit_session  # noqa: E402
from security import redact as _redact_mod  # noqa: E402

_redact = _redact_mod.redact

DEFAULT_STT_MODEL = "deepgram/nova-3"
DEFAULT_STT_LANGUAGE = "multi"
DEFAULT_TURN_DETECTION = "stt"
AGENT_INSTRUCTIONS = (
    "You are a transcript-only Cabinet voice transport adapter. "
    "Do not answer the user directly. Forward final user transcripts to Cabinet."
)


@dataclass(frozen=True)
class LiveKitAgentConfig:
    """Configuration for one Cabinet LiveKit transcript receiver."""

    meeting_id: int
    chat_id: str | None
    room_name: str
    server_url: str
    agent_name: str
    stt_model: str
    stt_language: str
    turn_detection: str


def _env_text(name: str, default: str) -> str:
    return (os.environ.get(name) or default).strip() or default


def build_agent_config(*, meeting_id: int, chat_id: str | None = None) -> LiveKitAgentConfig:
    """Build the local LiveKit agent config for a Cabinet meeting."""

    return LiveKitAgentConfig(
        meeting_id=meeting_id,
        chat_id=chat_id or None,
        room_name=livekit_session.build_room_name(meeting_id),
        server_url=livekit_session.livekit_server_url(),
        agent_name=livekit_session.livekit_agent_name(),
        stt_model=_env_text("CABINET_LIVEKIT_STT_MODEL", DEFAULT_STT_MODEL),
        stt_language=_env_text("CABINET_LIVEKIT_STT_LANGUAGE", DEFAULT_STT_LANGUAGE),
        turn_detection=_env_text("CABINET_LIVEKIT_TURN_DETECTION", DEFAULT_TURN_DETECTION),
    )


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


def _log_handoff_task_result(task: asyncio.Task) -> None:
    try:
        task.result()
    except Exception:
        logger.exception("livekit transcript handoff failed")


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
            task = asyncio.create_task(
                handoff_transcript_to_cabinet(
                    meeting_id=meeting_id,
                    chat_id=chat_id,
                    transcript=transcript,
                )
            )
            task.add_done_callback(_log_handoff_task_result)
        except RuntimeError:
            logger.warning("livekit transcript handoff skipped: no running event loop")


def _load_livekit_agent_deps():
    try:
        from livekit import agents  # noqa: PLC0415
        from livekit.agents import (  # noqa: PLC0415
            Agent,
            AgentServer,
            AgentSession,
            inference,
            room_io,
        )
    except ImportError as exc:  # pragma: no cover - optional dependency guard.
        raise livekit_session.LiveKitDependencyMissing(
            "Install the optional livekit extra to run the Cabinet LiveKit agent"
        ) from exc
    return agents, Agent, AgentServer, AgentSession, inference, room_io


def create_agent_server(
    config: LiveKitAgentConfig,
    *,
    server_factory: Callable[..., Any] | None = None,
    session_factory: Callable[..., Any] | None = None,
    stt_factory: Callable[..., Any] | None = None,
    agent_factory: Callable[..., Any] | None = None,
    room_options_factory: Callable[..., Any] | None = None,
    register_handoff: Callable[..., None] = register_user_transcript_handoff,
):
    """Create a LiveKit ``AgentServer`` that forwards final transcripts to Cabinet.

    Factory hooks keep unit tests import-light and avoid requiring a real
    LiveKit server for callback wiring coverage.
    """

    api_key, api_secret = livekit_session.livekit_api_credentials()
    if not all(
        [server_factory, session_factory, stt_factory, agent_factory, room_options_factory]
    ):
        _, Agent, AgentServer, AgentSession, inference, room_io = _load_livekit_agent_deps()
        server_factory = server_factory or AgentServer
        session_factory = session_factory or AgentSession
        stt_factory = stt_factory or inference.STT
        agent_factory = agent_factory or Agent
        room_options_factory = room_options_factory or room_io.RoomOptions

    server = server_factory(
        ws_url=config.server_url,
        api_key=api_key,
        api_secret=api_secret,
    )

    @server.rtc_session(agent_name=config.agent_name)
    async def _cabinet_livekit_session(ctx) -> None:
        stt = stt_factory(
            model=config.stt_model,
            language=config.stt_language,
            api_key=api_key,
            api_secret=api_secret,
        )
        session = session_factory(
            stt=stt,
            turn_handling={"turn_detection": config.turn_detection},
        )
        register_handoff(session, meeting_id=config.meeting_id, chat_id=config.chat_id)
        agent = agent_factory(instructions=AGENT_INSTRUCTIONS)
        room_options = room_options_factory(
            audio_input=True,
            audio_output=False,
            text_output=False,
        )
        logger.info(
            "livekit_agent_start meeting=%s room=%s url=%s model=%s language=%s",
            _redact(str(config.meeting_id)),
            _redact(config.room_name),
            _redact(config.server_url),
            _redact(config.stt_model),
            _redact(config.stt_language),
        )
        await session.start(
            room=ctx.room,
            agent=agent,
            room_options=room_options,
        )

    return server


def run_agent_app(
    config: LiveKitAgentConfig,
    *,
    livekit_cli_args: Sequence[str] | None = None,
    create_server_fn: Callable[[LiveKitAgentConfig], Any] = create_agent_server,
    cli_runner: Callable[[Any], Any] | None = None,
) -> None:
    """Run the LiveKit Agents CLI for a configured Cabinet meeting."""

    server = create_server_fn(config)
    if cli_runner is None:
        agents, *_ = _load_livekit_agent_deps()
        cli_runner = agents.cli.run_app

    cli_args = list(livekit_cli_args or ["connect", "--room", config.room_name])
    old_argv = sys.argv[:]
    try:
        sys.argv = [old_argv[0], *cli_args]
        cli_runner(server)
    finally:
        sys.argv = old_argv


def _parse_args(argv: Sequence[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        description="Run the Cabinet LiveKit transcript agent for one meeting."
    )
    parser.add_argument("--meeting-id", type=int, required=True)
    parser.add_argument("--chat-id", default=None)
    return parser.parse_known_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args, livekit_cli_args = _parse_args(sys.argv[1:] if argv is None else argv)
    config = build_agent_config(meeting_id=args.meeting_id, chat_id=args.chat_id)
    run_agent_app(config, livekit_cli_args=livekit_cli_args)


__all__ = [
    "AGENT_INSTRUCTIONS",
    "DEFAULT_STT_LANGUAGE",
    "DEFAULT_STT_MODEL",
    "DEFAULT_TURN_DETECTION",
    "LiveKitAgentConfig",
    "build_agent_config",
    "create_agent_server",
    "handoff_transcript_to_cabinet",
    "main",
    "register_user_transcript_handoff",
    "run_agent_app",
]


if __name__ == "__main__":
    main()
