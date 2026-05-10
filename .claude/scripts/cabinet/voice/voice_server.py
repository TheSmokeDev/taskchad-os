"""Cabinet voice WebSocket server — entry point + transport factory.

Port of ClaudeClaw ``warroom/server.py`` legacy mode (drops live mode entirely
since Gemini Live is single-vendor).

VERBATIM ports:

* :func:`make_transport` — ``warroom/server.py:131-149`` shape: input 16kHz,
  output 24kHz, vad_analyzer=None, ProtobufFrameSerializer.
  **Phase 7a deviation:** host defaults to ``127.0.0.1`` (R1 v2 B4 fix).
  Operator opts into LAN exposure via ``CABINET_VOICE_BIND=0.0.0.0``.
* :func:`print_ready` — ``warroom/server.py:152-159`` JSON handshake
  emitted on stdout for caller/operator parsing.
* :func:`run_voice_server` — ``warroom/server.py:768-779`` legacy mode shape
  (event handlers ``on_client_disconnected`` / ``on_client_connected`` from
  ``server.py:768-774`` — R1 v2 B4 fix; the prior PRP draft cited 694-712
  which is the LIVE mode handlers).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

# Ensure scripts/ is on sys.path so `voice`, `personas`, `integrations`,
# `security`, `cabinet` are importable when this module runs as a script
# (R1 v2 M2 fix — voice subprocess sys.path bootstrap).
_SCRIPTS_DIR = Path(__file__).resolve().parents[2]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))

# PRP-7a R2 NM1 (Tier A entry point) — apply persona override so the voice
# subprocess inherits the active profile's HOMIE_HOME / .env / paths. Same
# shim call other entry points (run_api.py, chat/main.py) make.
from personas import apply_persona_override  # noqa: E402

apply_persona_override()

# Pipecat optional dep — wrap so AST scans + tests still load.
try:  # pragma: no cover — exercised by integration only.
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.serializers.protobuf import ProtobufFrameSerializer
    from pipecat.transports.network.websocket_server import (
        WebsocketServerParams,
        WebsocketServerTransport,
    )
    _PIPECAT_AVAILABLE = True
except ImportError:  # pragma: no cover — pipecat optional dep.
    _PIPECAT_AVAILABLE = False

    class WebsocketServerTransport:  # type: ignore[no-redef]
        def __init__(self, host: str = "127.0.0.1", port: int = 7860, params=None) -> None:
            self.host = host
            self.port = port
            self.params = params

        def event_handler(self, name):
            def _decorator(fn):
                return fn

            return _decorator

        def input(self):
            return None

        def output(self):
            return None

    class WebsocketServerParams:  # type: ignore[no-redef]
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    class ProtobufFrameSerializer:  # type: ignore[no-redef]
        pass

    class PipelineRunner:  # type: ignore[no-redef]
        def __init__(self, handle_sigterm: bool = True) -> None:
            self.handle_sigterm = handle_sigterm

        async def run(self, task) -> None:  # noqa: D401
            ...


from . import config as voice_config  # noqa: E402

logger = logging.getLogger("cabinet.voice.server")

from security import redact as _redact_mod  # noqa: E402
_redact = _redact_mod.redact


def make_transport(
    port: int,
    host: str | None = None,
    audio_in_sr: int = 16000,
    audio_out_sr: int = 24000,
):
    """Build the WebsocketServerTransport.

    VERBATIM port of warroom/server.py:131-149 shape (params + serializer
    layout + audio rates). **Phase 7a deviation:** host defaults to
    ``127.0.0.1`` (operator opts into LAN via env), R1 v2 B4 fix.

    Args:
        port: WebSocket listen port.
        host: bind interface (defaults to :func:`config.voice_bind`,
            which itself defaults to ``127.0.0.1``).
        audio_in_sr: mic sample rate (default 16000 — matches Pipecat
            client bundle's microphone capture).
        audio_out_sr: TTS sample rate (default 24000 — matches Phase 4 TTS
            output and upstream ``server.py:131-149``).

    Rule 1: ``host=None`` is the sentinel — resolve at call time.
    """
    bind_host = host if host is not None else voice_config.voice_bind()
    return WebsocketServerTransport(
        host=bind_host,
        port=port,
        params=WebsocketServerParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=audio_in_sr,
            audio_out_sample_rate=audio_out_sr,
            vad_analyzer=None,
            serializer=ProtobufFrameSerializer(),
        ),
    )


def print_ready(port: int, mode: str = "legacy") -> None:
    """VERBATIM port of warroom/server.py:152-159.

    Emits JSON handshake on stdout so the caller (orchestrator / operator
    CLI) can parse the ready signal.
    """
    connection_info: dict[str, Any] = {
        "ws_url": f"ws://localhost:{port}",
        "status": "ready",
        "transport": "websocket",
        "mode": mode,
    }


def _load_broadcast_order_from_db(meeting_id: int) -> list[str] | None:
    """Read ``broadcast_order`` from the cabinet_meetings row for
    ``meeting_id``. Returns parsed list of agent ids on success.

    Returns ``None`` (caller falls back to hardcoded BROADCAST_ORDER)
    when:
    * the column is NULL (pre-Phase-6 row)
    * JSON parsing fails
    * the DB read raises (DB locked, schema drift, etc.)

    Defense-in-depth: every failure mode is caught and downgraded so a
    voice meeting NEVER fails to start due to a roster snapshot read.

    Added by PRD-8 Phase 6 v2 fix-pass 2026-05-10 (M3 fix).
    """
    try:
        from dashboard_db import get_connection  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "voice broadcast_order: dashboard_db import failed (%s); using hardcoded default",
            _redact(str(exc)),
        )
        return None
    try:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT broadcast_order FROM cabinet_meetings WHERE id = ?",
                (meeting_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "voice broadcast_order: DB read failed for meeting=%s (%s); using hardcoded default",
            _redact(str(meeting_id)),
            _redact(str(exc)),
        )
        return None
    if not row:
        return None
    raw = row[0] if not isinstance(row, dict) else row.get("broadcast_order")
    if raw is None or not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, list):
        return None
    return [str(x) for x in parsed if x]
    print(json.dumps(connection_info), flush=True)


# ── Legacy mode entry point — port of warroom/server.py:768-779 ────────


async def run_voice_server(
    meeting_id: int,
    chat_id: str | None = None,
    broadcast_order: list[str] | None = None,
    on_server_message=None,
    port: int | None = None,
    host: str | None = None,
) -> None:
    """Run the cabinet voice server bound to a single Phase 5a meeting.

    Port of ``warroom/server.py:run_legacy_mode`` (lines 726-780). Drops
    the live-mode branch entirely (single-vendor Gemini Live).

    Pipeline shape from :func:`voice_pipeline.build_voice_pipeline` matches
    upstream verbatim: ``transport.input → STT → router → bridge → TTS →
    transport.output``.

    Event handlers (R1 v2 B4 fix — port from ``server.py:768-774``,
    NOT 694-712 which is live-mode context-reset code).

    Rule 1: ``port=None`` / ``host=None`` sentinels resolved at call time.
    """
    if not _PIPECAT_AVAILABLE:
        raise RuntimeError(
            "pipecat-ai is not installed; install with "
            "`uv add pipecat-ai[websocket,silero]==0.0.108`."
        )

    from .voice_pipeline import build_voice_pipeline  # noqa: PLC0415

    resolved_port = port if port is not None else voice_config.voice_port()
    transport = make_transport(resolved_port, host=host)

    # PRD-8 Phase 6 v2 fix-pass 2026-05-10 (M3 fix) — when caller didn't
    # pass an explicit broadcast_order, hydrate it from the cabinet_meetings
    # row written at create time (dashboard_api.cabinet_new). This is the
    # snapshot path: voice subprocess uses the order from the live persona
    # registry at meeting create, not whatever the registry looks like at
    # voice-process spawn time. Fall through to the hardcoded default if
    # the column is NULL (pre-migration row) or DB read fails.
    if broadcast_order is None:
        broadcast_order = _load_broadcast_order_from_db(meeting_id)

    pipeline, task = build_voice_pipeline(
        transport,
        meeting_id=meeting_id,
        chat_id=chat_id,
        broadcast_order=broadcast_order,
        on_server_message=on_server_message,
    )

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):  # noqa: ARG001 — Pipecat signature
        logger.info("Voice client disconnected; keeping pipeline alive for next meeting")

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):  # noqa: ARG001 — Pipecat signature
        logger.info("Voice client connected (legacy mode)")

    print_ready(resolved_port, "legacy")
    runner = PipelineRunner(handle_sigterm=True)
    # PRD-8 Phase 6 v2 fix-pass 2026-05-10 (M2) — wrap host + port via _redact
    # to satisfy the Rule 3 "every dynamic arg via _redact" contract. Even
    # though host/port are low-sensitivity, the contract is uniform.
    logger.info(
        "Cabinet voice LEGACY mode on ws://%s:%s (meeting=%s)",
        _redact(str(host or voice_config.voice_bind())),
        _redact(str(resolved_port)),
        _redact(str(meeting_id)),
    )
    try:
        await runner.run(task)
    finally:
        logger.info("Cabinet voice session ended (meeting=%s)", _redact(str(meeting_id)))


def main() -> None:
    """CLI entry point — minimal arg parser + asyncio runner.

    Usage::

        python -m cabinet.voice.voice_server --meeting-id <N> [--chat-id <X>]

    Environment:

        CABINET_VOICE_PORT: WebSocket port (default 7860).
        CABINET_VOICE_BIND: bind host (default 127.0.0.1; set 0.0.0.0 for LAN).
    """
    import argparse  # noqa: PLC0415
    parser = argparse.ArgumentParser(prog="cabinet-voice")
    parser.add_argument("--meeting-id", type=int, required=True)
    parser.add_argument("--chat-id", type=str, default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--host", type=str, default=None)
    args = parser.parse_args()

    try:
        asyncio.run(
            run_voice_server(
                meeting_id=args.meeting_id,
                chat_id=args.chat_id,
                port=args.port,
                host=args.host,
            )
        )
    except KeyboardInterrupt:
        logger.info("Cabinet voice shut down by user.")
    except Exception as exc:  # noqa: BLE001
        logger.error("Cabinet voice crashed: %s", _redact(str(exc)), exc_info=True)
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "main",
    "make_transport",
    "print_ready",
    "run_voice_server",
]
