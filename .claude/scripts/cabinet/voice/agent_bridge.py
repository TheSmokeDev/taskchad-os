"""HomieAgentBridge — Pipecat FrameProcessor that bridges voice routing to Phase 5a.

STRUCTURE VERBATIM port of ClaudeClaw ``warroom/agent_bridge.py:37-94``. The
``_call_agent`` body is REPLACED to invoke Phase 5a's
:func:`integrations.cabinet_api.send_message` (with ``is_voice=True`` and
``target_agent_id=<routed agent>``) and consume
:func:`integrations.cabinet_api.stream_meeting` for the response.

The Node CLI hop (``warroom/agent_bridge.py:96-181`` — subprocess
``node dist/agent-voice-bridge.js``) collapses to a single in-process HTTP
call. This is the same 5b-A pattern Phase 5b uses: the cabinet REST API
process is the orchestrator, voice subprocess is a thin presenter.

R1 v2 B1 + B2 fixes are baked into this implementation:

* **B1 (target agent reaches orchestrator):** voice ``AgentRouter`` selects
  the persona id; this bridge passes it as ``target_agent_id`` on the HTTP
  POST so Phase 5a's orchestrator pins the turn instead of re-routing via
  Haiku. Broadcast mode loops the snapshot of cabinet personas (matches
  upstream ``BROADCAST_ORDER`` pattern).
* **B2 (SSE correlation):** every outgoing turn generates a deterministic
  ``client_msg_id`` BEFORE the send, then the bridge subscribes to
  :func:`stream_meeting` and waits for the matching ``turn_start.clientMsgId``
  + ``agent_done.turnId`` before emitting TTS. Stale replays / concurrent
  turns are filtered out by the correlation match.

R1 v2 B6 fix (avatar route): this bridge does NOT serve avatars — that's
:mod:`dashboard_api`'s job. We route through ``/api/cabinet/voice/avatars/{id}.png``
which is explicitly mounted as a Homie deviation (see ``dashboard_api`` route
docstring + Translation Boundary Audit row).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Optional

# Pipecat is an optional Phase 6 install — wrap so AST scans + tests run
# without the heavy dep.
try:  # pragma: no cover — exercised by integration only.
    from pipecat.frames.frames import TextFrame, TTSUpdateSettingsFrame
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
    _PIPECAT_AVAILABLE = True
except ImportError:  # pragma: no cover — pipecat optional dep.
    _PIPECAT_AVAILABLE = False

    class FrameProcessor:  # type: ignore[no-redef]
        async def process_frame(self, frame, direction) -> None:  # noqa: D401
            ...

        async def push_frame(self, frame, direction=None) -> None:
            ...

    class FrameDirection:  # type: ignore[no-redef]
        DOWNSTREAM = "DOWNSTREAM"
        UPSTREAM = "UPSTREAM"

    class TextFrame:  # type: ignore[no-redef]
        def __init__(self, text: str = "") -> None:
            self.text = text

    class TTSUpdateSettingsFrame:  # type: ignore[no-redef]
        def __init__(self, settings: dict | None = None) -> None:
            self.settings = settings or {}


from .voice_router import AGENT_NAMES, AgentRouteFrame  # noqa: E402

logger = logging.getLogger("cabinet.voice.agent_bridge")

# PRD-8 Phase 7b — log-message redaction (Rule 3 module-attribute lookup).
from security import redact as _redact_mod  # noqa: E402
_redact = _redact_mod.redact


# How long to wait for the orchestrator response on a single voice turn
# before we emit a friendly fallback. Matches upstream BRIDGE_TIMEOUT
# (warroom/agent_bridge.py:28). Configurable via env for slow-network ops.
import os  # noqa: E402

_BRIDGE_TIMEOUT_DEFAULT_S = 60.0


def _bridge_timeout_seconds() -> float:
    """Resolve the per-turn timeout at call time (Rule 1 — no def-time bind)."""
    raw = os.environ.get("CABINET_VOICE_BRIDGE_TIMEOUT_S", "")
    if not raw:
        return _BRIDGE_TIMEOUT_DEFAULT_S
    try:
        return float(raw)
    except ValueError:
        return _BRIDGE_TIMEOUT_DEFAULT_S


# ── BROADCAST_ORDER — port of warroom/agent_bridge.py:34 ──────────────────


# Default broadcast order matches upstream verbatim. Per-meeting snapshot
# of the active roster overrides this when the meeting was created via
# Phase 6's broadcast_order JSON column on cabinet_meetings (see
# dashboard_db.py:_apply_phase_6_columns).
BROADCAST_ORDER: list[str] = ["main", "research", "comms", "content", "ops"]


# ── HomieAgentBridge — port of warroom/agent_bridge.py:37-94 ──────────────


class HomieAgentBridge(FrameProcessor):  # type: ignore[misc]
    """Receives :class:`AgentRouteFrame`s and emits voice-switched
    :class:`TextFrame`s for downstream TTS.

    STRUCTURE VERBATIM port of ``ClaudeAgentBridge``
    (``warroom/agent_bridge.py:37-94``); ``_call_agent`` body replaced to
    use Phase 5a's HTTP cabinet API.

    Construction args:
        meeting_id: Phase 5a cabinet meeting id (HTTP turns post here).
        chat_id: Telegram chat id for chat-scope binding (None == any).
        broadcast_order: per-meeting roster snapshot (overrides
            :data:`BROADCAST_ORDER` default).
        on_server_message: optional callback invoked with the RTVI
            ``server-message`` envelope (so the HTML page can render
            ``agent_selected`` / ``hand_down`` / ``agent_error`` events).
            When None, server messages are dropped.
    """

    def __init__(
        self,
        meeting_id: int,
        chat_id: str | None = None,
        broadcast_order: list[str] | None = None,
        on_server_message=None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._meeting_id = meeting_id
        self._chat_id = chat_id
        # Rule 1: broadcast_order=None sentinel — resolve at construction.
        self._broadcast_order: list[str] = list(broadcast_order or BROADCAST_ORDER)
        self._on_server_message = on_server_message
        # Voice-switch guard — emit TTSUpdateSettingsFrame ONLY when voice
        # actually changes (verbatim from warroom/agent_bridge.py:88).
        self._current_voice: Optional[str] = None
        # Per-persona voice config cache. Resolved per turn from
        # <profile>/config.yaml.cabinet.voice_id + voice_provider.
        self._voice_config_cache: dict[str, tuple[str | None, str | None]] = {}

    # ── Frame entry point — verbatim from warroom/agent_bridge.py:45-57 ──

    async def process_frame(self, frame, direction) -> None:
        # CRITICAL: Must call super first so the parent registers StartFrame.
        await super().process_frame(frame, direction)

        # Only handle AgentRouteFrames going downstream.
        if not isinstance(frame, AgentRouteFrame):
            await self.push_frame(frame, direction)
            return

        if frame.mode == "broadcast":
            await self._handle_broadcast(frame.message)
        else:
            await self._handle_single(frame.agent_id, frame.message)

    # ── Single-target turn — verbatim shape from warroom/agent_bridge.py:59-66 ──

    async def _handle_single(self, agent_id: str, message: str) -> None:
        """Route a message to one agent and emit its response."""
        # Match upstream's "if not in roster, fall to default" guard.
        if agent_id not in AGENT_NAMES:
            agent_id = "main"

        response = await self._call_agent(agent_id, message)
        if response:
            await self._emit_response(agent_id, response)

    # ── Broadcast — verbatim shape from warroom/agent_bridge.py:68-75 ────

    async def _handle_broadcast(self, message: str) -> None:
        """Send the message to each agent in order and emit all responses."""
        for agent_id in self._broadcast_order:
            response = await self._call_agent(agent_id, message)
            if response:
                tagged = f"{agent_id.capitalize()} here. {response}"
                await self._emit_response(agent_id, tagged)

    # ── Voice-switched TTS emit — verbatim from warroom/agent_bridge.py:77-94 ──

    async def _emit_response(self, agent_id: str, text: str) -> None:
        """Switch TTS voice to the agent's voice, then emit the text.

        Verbatim port — the ``if voice_id != self._current_voice`` guard is
        load-bearing for the voice-switch buffer behavior. ``TTSUpdateSettingsFrame``
        carries the per-persona voice override; the downstream HomieTTS reads
        it via the Pipecat frame protocol.
        """
        voice_id, voice_provider = self._resolve_persona_voice(agent_id)

        # Only send a voice-switch frame if we have a voice AND it actually changed.
        # This is the load-bearing guard preserved verbatim from upstream.
        if voice_id and voice_id != self._current_voice:
            settings: dict[str, Any] = {"voice": voice_id}
            if voice_provider:
                settings["provider"] = voice_provider
            await self.push_frame(TTSUpdateSettingsFrame(settings=settings))
            self._current_voice = voice_id

        await self.push_frame(TextFrame(text=text))

    def _resolve_persona_voice(self, agent_id: str) -> tuple[str | None, str | None]:
        """Resolve (voice_id, voice_provider) for ``agent_id`` from config.yaml.

        Caches per-process; persona config is stable across a meeting. Falls
        through to (None, None) when the persona has no cabinet voice config —
        downstream HomieTTS keeps its default voice.

        Q4 wire-translation: ``agent_id`` may be the wire ``"main"``. We
        resolve internally to ``"default"`` via :func:`personas.get_persona`'s
        same translation logic.
        """
        cached = self._voice_config_cache.get(agent_id)
        if cached is not None:
            return cached
        from .personas import resolve_internal_persona_id  # noqa: PLC0415
        internal_id = resolve_internal_persona_id(agent_id)
        try:
            import personas  # noqa: PLC0415
            cfg = personas.load_persona_config(internal_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "voice config load failed for %s: %s",
                _redact(internal_id),
                _redact(str(exc)),
            )
            self._voice_config_cache[agent_id] = (None, None)
            return None, None
        cabinet = cfg.get("cabinet") if isinstance(cfg, dict) else None
        if not isinstance(cabinet, dict):
            self._voice_config_cache[agent_id] = (None, None)
            return None, None
        voice_id = cabinet.get("voice_id") if isinstance(cabinet.get("voice_id"), str) else None
        voice_provider = (
            cabinet.get("voice_provider")
            if isinstance(cabinet.get("voice_provider"), str)
            else None
        )
        result = (voice_id, voice_provider)
        self._voice_config_cache[agent_id] = result
        return result

    # ── _call_agent — REPLACED body (R1 v2 B1 + B2 correlation) ──────────

    async def _call_agent(self, agent_id: str, message: str) -> Optional[str]:
        """Invoke Phase 5a's orchestrator over HTTP and consume the SSE
        stream until the matching response arrives.

        Replaces ``warroom/agent_bridge.py:96-181`` Node CLI shell-out with
        an in-process HTTP call (5b-A pattern). Correlation contract per
        R1 v2 B2:

          1. Generate a deterministic ``client_msg_id`` BEFORE the send so
             the bridge can match the SSE ``turn_start.clientMsgId`` event.
          2. Subscribe to :func:`stream_meeting` BEFORE the send so the
             ``turn_start`` event isn't missed (race-safe).
          3. POST ``/api/cabinet/send`` with ``is_voice=True``,
             ``target_agent_id=agent_id``, and the deterministic id.
          4. Filter SSE events to the matching ``turnId`` (correlation).
          5. Wait for ``agent_done`` / ``error`` / ``turn_complete`` /
             timeout. Render kill-switch refusal events as friendly text.

        Args:
            agent_id: routed persona id (wire-side; ``"main"`` resolves to
                ``"default"`` server-side).
            message: user transcript text.

        Returns:
            Agent text reply, or a friendly fallback string on error /
            timeout / kill-switch refusal.
        """
        # Late-bind cabinet_api so import order stays loose for tests.
        from integrations import cabinet_api  # noqa: PLC0415

        client_msg_id = uuid.uuid4().hex
        timeout_s = _bridge_timeout_seconds()

        # PRD-8 Phase 6 v2 fix-pass 2026-05-10 (M2) — wrap meeting_id via
        # _redact to honor the Rule 3 "every dynamic arg via _redact"
        # contract uniformly across cabinet/voice/.
        logger.info(
            "voice turn meeting=%s agent=%s msg_preview=%r",
            _redact(str(self._meeting_id)),
            _redact(agent_id),
            _redact(message[:80]),
        )

        # Subscribe to the meeting's SSE stream BEFORE send (race-safe).
        # We use a small per-call task so we can cancel cleanly on timeout.
        target_turn_id: Optional[str] = None
        agent_text_reply: Optional[str] = None
        terminal_event = asyncio.Event()

        async def _consumer() -> None:
            nonlocal target_turn_id, agent_text_reply
            try:
                async for envelope in cabinet_api.stream_meeting(
                    self._meeting_id,
                    chat_id=self._chat_id,
                ):
                    if not isinstance(envelope, dict):
                        continue
                    event = envelope.get("event") if isinstance(envelope.get("event"), dict) else envelope
                    if not isinstance(event, dict):
                        continue
                    etype = event.get("type")

                    # Forward server-message-style events (agent_selected,
                    # hand_down, agent_error) to the HTML page if a callback
                    # was registered.
                    if self._on_server_message is not None and etype in {
                        "agent_selected",
                        "hand_down",
                        "agent_error",
                    }:
                        try:
                            await self._maybe_await(
                                self._on_server_message({"type": etype, "data": event})
                            )
                        except Exception:  # noqa: BLE001
                            pass

                    # Correlation: lock on the matching turn_start.
                    if etype == "turn_start":
                        if event.get("clientMsgId") == client_msg_id:
                            target_turn_id = event.get("turnId")
                        continue

                    # Without a target turn yet, skip events from concurrent turns.
                    if target_turn_id is None:
                        continue
                    if event.get("turnId") and event.get("turnId") != target_turn_id:
                        continue

                    if etype == "agent_done":
                        # First persona reply for our turn — capture and stop.
                        text = event.get("text")
                        if isinstance(text, str) and text.strip():
                            agent_text_reply = text.strip()
                            terminal_event.set()
                            return
                    elif etype == "error":
                        # Render kill-switch refusal / orchestrator errors as
                        # friendly text (R1 v2 B2 — voice consumer surfaces
                        # SSE error events to operator).
                        msg = event.get("message")
                        agent_text_reply = (
                            f"The cabinet declined this turn: "
                            f"{(msg or 'unknown error').strip()}"
                        )
                        terminal_event.set()
                        return
                    elif etype == "turn_complete":
                        # Reached turn_complete without an agent_done — let the
                        # caller fall through with whatever (possibly None) reply
                        # we captured. Voice cabinet shouldn't typically hit this.
                        terminal_event.set()
                        return
            except cabinet_api.CabinetAPIError as exc:
                # Friendly_message present; surface to operator transcript.
                agent_text_reply = exc.friendly_message
                terminal_event.set()
            except Exception as exc:  # noqa: BLE001 — defensive top-level
                logger.warning("voice SSE consumer crashed: %s", _redact(str(exc)))
                terminal_event.set()

        consumer_task = asyncio.create_task(_consumer())

        # Brief grace before posting so the SSE subscription is established.
        # The dashboard cabinet_stream emits a meeting_state snapshot
        # immediately on subscribe; the consumer's first iteration drains
        # that within microseconds. A 0.05s sleep is empirically safe.
        await asyncio.sleep(0.05)

        try:
            await cabinet_api.send_message(
                meeting_id=self._meeting_id,
                text=message,
                client_msg_id=client_msg_id,
                chat_id=self._chat_id,
                is_voice=True,
                target_agent_id=agent_id,
            )
        except cabinet_api.CabinetAPIError as exc:
            # Synchronous post failed (auth / connect / 503 on the rare
            # synchronous endpoints). Cancel the consumer; surface friendly.
            consumer_task.cancel()
            return exc.friendly_message
        except Exception as exc:  # noqa: BLE001
            consumer_task.cancel()
            logger.warning("voice send_message crashed: %s", _redact(str(exc)))
            return f"The {agent_id} agent ran into an issue. Try again in a moment."

        # Wait up to bridge timeout for the matching agent_done / error.
        try:
            await asyncio.wait_for(terminal_event.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            consumer_task.cancel()
            try:
                await consumer_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            # PRD-8 Phase 6 v2 fix-pass 2026-05-10 (M2) — uniform _redact
            # wrapping for meeting_id + timeout_s.
            logger.warning(
                "voice turn meeting=%s agent=%s timed out after %ss",
                _redact(str(self._meeting_id)),
                _redact(agent_id),
                _redact(f"{timeout_s:.0f}"),
            )
            return f"The {agent_id} agent took too long to respond."

        # Clean shutdown of the consumer.
        consumer_task.cancel()
        try:
            await consumer_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

        return agent_text_reply

    @staticmethod
    async def _maybe_await(result) -> None:
        """Allow ``on_server_message`` to be sync or async."""
        if asyncio.iscoroutine(result):
            await result


__all__ = [
    "BROADCAST_ORDER",
    "HomieAgentBridge",
]
