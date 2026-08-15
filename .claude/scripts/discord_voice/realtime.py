"""OpenAI Realtime WebSocket client for the Discord voice sidecar.

Native-bridge port of OpenClaw's ``OpenAIRealtimeVoiceBridge`` (see
``.tmp/openclaw-pr100671/realtime-voice-provider.ts``): GA session.update
shape, PCM16 24kHz audio both ways, server VAD, barge-in on
``input_audio_buffer.speech_started``. Auth is resolved by the caller via
``runtime.openai_platform_auth`` (API key sources first, Codex OAuth
fallback) — this client only sees the bearer token.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import websockets

from speaker_auth import TRUSTED_CONTINUATION_EVENT_KEY as _CONTINUATION_KEY

_log = logging.getLogger(__name__)

REALTIME_WS_URL = "wss://api.openai.com/v1/realtime?model={model}"
DEFAULT_MODEL = "gpt-realtime-2.1"
INPUT_TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"

OnAudio = Callable[[bytes], None]
OnTranscript = Callable[[str, str, bool], None]  # role, text, final
OnBargeIn = Callable[[], None]
#: (name, arguments, bound_event) -> spoken output. `bound_event` carries the
#: response-correlated speaker binding when a ledger is active, and is the ONLY
#: thing an executor may read to decide who authored the call.
ToolExecutor = Callable[[str, dict, dict], str]
RunReader = Callable[[str], dict]  # run_id -> {"status", "output", "kind"}

# A tool (or a finished run) announcing async work — the same sentinel the
# dashboard Talk page watches for. Keep in lockstep with
# ``talk_runs.started_sentinel`` and Talk.tsx's WORK_STARTED_RE.
WORK_STARTED_RE = re.compile(r"WORK_STARTED #(\d+) kind=(\w+)")
RUN_POLL_INTERVAL_S = 5.0
# Per-kind watch budgets: an Archon build outlives a screen look by hours.
RUN_POLL_CAPS_S: dict[str, float] = {
    "skill": 600.0,
    "agent": 2_700.0,
    "archon": 10_800.0,
    "look": 180.0,
}
DEFAULT_RUN_POLL_CAP_S = 600.0


@dataclass(slots=True)
class RealtimeConfig:
    token: str
    instructions: str
    model: str = DEFAULT_MODEL
    voice: str = "cedar"
    vad_threshold: float = 0.5
    prefix_padding_ms: int = 300
    silence_duration_ms: int = 500
    tools: list[dict] | None = None  # Realtime function-tool schemas
    tool_executor: ToolExecutor | None = None
    run_reader: RunReader | None = None  # polls /api/talk/runs/<id>; None = no polling
    #: Let server VAD auto-create the response. MUST be False when a speaker
    #: ledger is active: the response then has to be minted by US so it can
    #: carry that utterance's opaque binding token in `response.metadata`,
    #: which is the only thing that makes a later function call resolvable to
    #: exactly one speaker.
    automatic_response: bool = True


class RealtimeError(Exception):
    """Realtime session setup or protocol failure."""


def build_session_update(config: RealtimeConfig) -> dict:
    """GA session.update payload (mirrors OpenClaw's buildGaSessionUpdate)."""

    session: dict = {
        "type": "realtime",
        "model": config.model,
        "instructions": config.instructions,
        "output_modalities": ["audio"],
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "noise_reduction": {"type": "near_field"},
                "transcription": {"model": INPUT_TRANSCRIPTION_MODEL},
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": config.vad_threshold,
                    "prefix_padding_ms": config.prefix_padding_ms,
                    "silence_duration_ms": config.silence_duration_ms,
                    "create_response": config.automatic_response,
                    "interrupt_response": True,
                },
            },
            "output": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "voice": config.voice,
            },
        },
    }
    if config.tools:
        session["tools"] = config.tools
        session["tool_choice"] = "auto"
    return {"type": "session.update", "session": session}


class RealtimeSession:
    """One OpenAI Realtime conversation over WebSocket."""

    def __init__(
        self,
        config: RealtimeConfig,
        *,
        on_audio: OnAudio,
        on_transcript: OnTranscript | None = None,
        on_barge_in: OnBargeIn | None = None,
        ledger: Any | None = None,
    ) -> None:
        self.config = config
        self._on_audio = on_audio
        self._on_transcript = on_transcript
        self._on_barge_in = on_barge_in
        # The speaker-authorization ledger (`speaker_auth.DiscordSpeakerLedger`)
        # or None. When present it owns response creation: server VAD does not
        # auto-create, we mint the response at the commit boundary carrying the
        # utterance's binding token, and every tool call resolves through it.
        self._ledger = ledger
        self._ws = None
        self._recv_task: asyncio.Task | None = None
        self._closed = False
        self._response_active = False
        # Response IDs that produced at least one tool call, so teardown knows
        # whether a continuation token is still owed a response.
        self._continued_responses: set[str] = set()
        self.appends_sent = 0
        self.events_received = 0

    async def connect(self) -> None:
        url = REALTIME_WS_URL.format(model=self.config.model)
        self._ws = await websockets.connect(
            url,
            additional_headers={"Authorization": f"Bearer {self.config.token}"},
            max_size=16 * 1024 * 1024,
        )
        event = await self._recv_event()
        if event.get("type") != "session.created":
            raise RealtimeError(f"expected session.created, got {event.get('type')}: {event}")
        await self._send(build_session_update(self.config))
        event = await self._recv_event()
        if event.get("type") != "session.updated":
            raise RealtimeError(f"expected session.updated, got {event.get('type')}: {event}")
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def send_audio(self, pcm24: bytes, speaker: dict | None = None) -> None:
        """Append one PCM16 24kHz chunk to the input buffer.

        `speaker` is recorded against the EXACT bytes being appended, which is
        what lets a VAD interval later resolve to one immutable Discord user.
        `None` means synthesized silence (the pump's paced zeros), not an
        unknown person — the distinction is what keeps a gated pause from
        tainting the utterance around it.
        """

        if self._ledger is not None:
            self._ledger.record_packet(speaker, pcm24)
        await self._send(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm24).decode("ascii"),
            }
        )
        self.appends_sent += 1

    async def close(self) -> None:
        self._closed = True
        if self._recv_task is not None:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    # -- internals -----------------------------------------------------------

    async def _send(self, event: dict) -> None:
        if self._ws is None or self._closed:
            return
        await self._ws.send(json.dumps(event))

    async def _recv_event(self) -> dict:
        if self._ws is None:
            raise RealtimeError("not connected")
        raw = await self._ws.recv()
        return json.loads(raw)

    async def _recv_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    event = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                self._dispatch(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — socket drop ends the session
            if not self._closed:
                _log.warning("realtime recv loop ended: %s", exc)

    def _dispatch(self, event: dict) -> None:
        etype = event.get("type")
        self.events_received += 1
        _log.debug("event %s", etype)
        if etype in (
            "conversation.output_audio.delta",
            "response.audio.delta",
            "response.output_audio.delta",
        ):
            delta = event.get("delta") or event.get("data")
            if delta:
                self._on_audio(base64.b64decode(delta))
                self._response_active = True
        elif etype == "conversation.item.input_audio_transcription.completed":
            transcript = event.get("transcript")
            if not transcript:
                # GA-shape probe: real comprehension but empty transcript —
                # dump the payload once so we can see where the text lives.
                _log.debug("empty input transcript, raw event: %s", event)
                # Surface the miss instead of silent ambiguity (operator ask,
                # 2026-08-03): speech was DETECTED but transcription came
                # back empty — an empty-string final lets the bridge mirror
                # "(heard you, couldn't make out the words)" so the operator
                # can SEE every pickup, intelligible or not.
                if self._on_transcript:
                    self._on_transcript("user", "", True)
            if transcript and self._on_transcript:
                self._on_transcript("user", transcript, True)
        elif etype in (
            "conversation.output_transcript.delta",
            "response.output_text.delta",
            "response.audio_transcript.delta",
            "response.output_audio_transcript.delta",
        ):
            delta = event.get("delta")
            if delta and self._on_transcript:
                self._on_transcript("assistant", delta, False)
        elif etype in (
            "response.output_text.done",
            "response.audio_transcript.done",
            "response.output_audio_transcript.done",
        ):
            transcript = event.get("transcript") or event.get("text")
            if transcript and self._on_transcript:
                self._on_transcript("assistant", transcript, True)
        elif etype == "input_audio_buffer.speech_started":
            if self._ledger is not None:
                self._ledger.note_speech_started(event)
            if self._response_active and self._on_barge_in:
                self._on_barge_in()
        elif etype == "input_audio_buffer.speech_stopped":
            # The VAD interval closes HERE. Attribution is frozen against the
            # PCM that actually fell inside [audio_start_ms, audio_end_ms) —
            # not against whoever happens to be speaking later.
            if self._ledger is not None:
                self._ledger.note_speech_stopped(event)
        elif etype == "input_audio_buffer.committed":
            self._mint_bound_response(event)
        elif etype == "response.function_call_arguments.done":
            self._schedule_tool_call(event)
        elif etype == "response.created":
            if self._ledger is not None:
                self._ledger.note_response_created(event)
            self._response_active = True
        elif etype in ("response.done", "response.cancelled"):
            self._response_active = False
            self._release_response(event)
        elif etype == "error":
            _log.warning("realtime error event: %s", event.get("error"))

    def _mint_bound_response(self, event: dict) -> None:
        """Create this utterance's response ourselves, carrying its token.

        With a ledger active, server VAD does NOT auto-create the response
        (`automatic_response=False`), because a server-created response has no
        metadata we control and therefore nothing a later function call could
        be correlated against. Minting it here is what binds the response ID to
        exactly one resolved speaker.
        """

        if self._ledger is None:
            return
        message = self._ledger.response_for_commit(event)
        if message is None:
            return  # duplicate commit — one utterance gets exactly one response
        asyncio.get_running_loop().create_task(self._send(message))

    def _release_response(self, event: dict) -> None:
        """Drop finished response state, keeping a continued chain's token."""

        if self._ledger is None:
            return
        response = event.get("response")
        response_id = (
            response.get("id") if isinstance(response, dict) else None
        ) or event.get("response_id")
        continued = response_id in self._continued_responses
        self._continued_responses.discard(response_id)
        self._ledger.complete_response(response_id, continued=continued)

    def _schedule_tool_call(self, event: dict) -> None:
        """Run a model function call and feed the output back (fire-and-forget)."""

        executor = self.config.tool_executor
        call_id = event.get("call_id")
        name = event.get("name")
        if executor is None or not call_id or not name:
            return
        try:
            arguments = json.loads(event.get("arguments") or "{}")
            if not isinstance(arguments, dict):
                arguments = {}
        except (TypeError, ValueError):
            arguments = {}
        # Bind BEFORE the call is scheduled — on this loop, in event order, so
        # the binding is the one belonging to the response that emitted the
        # call rather than whatever the session looks like once the task runs.
        bound = event
        if self._ledger is not None:
            bound = self._ledger.bind_tool_event(event)
            response_id = event.get("response_id")
            if response_id:
                self._continued_responses.add(response_id)
        asyncio.get_running_loop().create_task(
            self._run_tool_call(str(call_id), str(name), arguments, bound)
        )

    async def _run_tool_call(
        self, call_id: str, name: str, arguments: dict, bound: dict | None = None
    ) -> None:
        _log.info("tool call: %s(%s)", name, json.dumps(arguments)[:200])
        bound = bound if bound is not None else {}
        try:
            output = await asyncio.to_thread(
                self.config.tool_executor, name, arguments, bound
            )
        except Exception as exc:  # noqa: BLE001 — the model speaks the failure
            output = f"Tool {name} failed: {exc}"
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output or "(no output)",
                },
            }
        )
        # The continuation carries a FRESH token on the same authority chain,
        # so the speaker survives into the response that speaks the result and
        # a second tool call from it is still attributable. An unbound call
        # continues the conversation with no binding at all.
        continuation = bound.get(_CONTINUATION_KEY)
        await self._send(
            continuation
            if isinstance(continuation, dict) and continuation
            else {"type": "response.create"}
        )
        self._watch_for_run(output or "")

    def _watch_for_run(self, text: str) -> None:
        """Start polling when *text* announced async work."""

        if self.config.run_reader is None:
            return
        match = WORK_STARTED_RE.search(text)
        if not match:
            return
        asyncio.get_running_loop().create_task(
            self._poll_run(match.group(1), match.group(2))
        )

    async def _poll_run(self, run_id: str, kind: str) -> None:
        """Watch one async run, then hand the result to the model to speak.

        Mirrors the dashboard poller: the result is injected as a user-role
        note so the model narrates it the moment it lands. A finished run can
        announce a follow-on run (a skill that outgrew its budget hands off to
        a background agent), so results are re-scanned.
        """

        reader = self.config.run_reader
        if reader is None:
            return
        cap = RUN_POLL_CAPS_S.get(kind, DEFAULT_RUN_POLL_CAP_S)
        deadline = time.monotonic() + cap
        while time.monotonic() < deadline:
            await asyncio.sleep(RUN_POLL_INTERVAL_S)
            try:
                run = await asyncio.to_thread(reader, run_id)
            except Exception as exc:  # noqa: BLE001 — transient API failure, keep watching
                _log.debug("run %s poll failed: %s", run_id, exc)
                continue
            status = str((run or {}).get("status") or "running")
            if status == "running":
                continue
            result = str((run or {}).get("output") or "(no output)")
            _log.info("run %s finished (%s)", run_id, status)
            await self._send(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    f"Work run #{run_id} ({run.get('kind') or kind}) finished "
                                    f"with status '{status}'. Result: {result}\n\n"
                                    "Summarize this aloud for owner in one to three spoken "
                                    "sentences."
                                ),
                            }
                        ],
                    },
                }
            )
            await self._send({"type": "response.create"})
            self._watch_for_run(result)
            return
        _log.info("run %s exceeded the %ss watch budget; letting go", run_id, cap)


__all__ = [
    "DEFAULT_MODEL",
    "INPUT_TRANSCRIPTION_MODEL",
    "REALTIME_WS_URL",
    "RealtimeConfig",
    "RealtimeError",
    "RealtimeSession",
    "build_session_update",
]
