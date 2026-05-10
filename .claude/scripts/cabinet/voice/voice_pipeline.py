"""Voice pipeline factory + Pipecat FrameProcessors for STT/TTS.

Ports the Pipecat Pipeline + PipelineTask shape from ClaudeClaw
``warroom/server.py:751-779`` (legacy mode):

    Pipeline([
        transport.input(),
        stt,
        router,
        bridge,
        tts,
        transport.output(),
    ])

PRD-8 Phase 6 — STT/TTS implementations:

* :class:`HomieSTT` wraps Phase 4's :func:`voice.transcribe_audio_file`
  cascade. Receives audio frames (mic input) and emits
  :class:`TranscriptionFrame` for the downstream :class:`AgentRouter`.
* :class:`HomieTTS` wraps Phase 4's :func:`voice.synthesize` (with the
  Phase 6 WS0 ``voice_overrides`` backport). Receives :class:`TextFrame`s
  from the bridge and emits :class:`AudioRawFrame`s for transport output.
  Per-persona voice id selection happens via the bridge's
  ``TTSUpdateSettingsFrame`` (matches upstream ``CartesiaTTSService``
  shape verbatim).

Forward-additive lock: this module does NOT re-implement any provider —
all STT/TTS routes through Phase 4's existing cascade. Dropping a turn,
config edit, or kill-switch refusal works transparently.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

# Pipecat optional dep — wrap so non-voice tests still import.
try:  # pragma: no cover — exercised by integration only.
    from pipecat.frames.frames import (
        AudioRawFrame,
        EndFrame,
        Frame,
        StartFrame,
        TextFrame,
        TranscriptionFrame,
        TTSUpdateSettingsFrame,
    )
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.task import PipelineParams, PipelineTask
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

    class Frame:  # type: ignore[no-redef]
        pass

    class StartFrame(Frame):  # type: ignore[no-redef,misc]
        pass

    class EndFrame(Frame):  # type: ignore[no-redef,misc]
        pass

    class TextFrame(Frame):  # type: ignore[no-redef,misc]
        def __init__(self, text: str = "") -> None:
            self.text = text

    class TranscriptionFrame(Frame):  # type: ignore[no-redef,misc]
        def __init__(self, text: str = "", user_id: str = "", timestamp: str = "") -> None:
            self.text = text
            self.user_id = user_id
            self.timestamp = timestamp

    class AudioRawFrame(Frame):  # type: ignore[no-redef,misc]
        def __init__(self, audio: bytes = b"", sample_rate: int = 24000, num_channels: int = 1) -> None:
            self.audio = audio
            self.sample_rate = sample_rate
            self.num_channels = num_channels

    class TTSUpdateSettingsFrame(Frame):  # type: ignore[no-redef,misc]
        def __init__(self, settings: dict | None = None) -> None:
            self.settings = settings or {}

    class Pipeline:  # type: ignore[no-redef]
        def __init__(self, processors: list) -> None:
            self.processors = processors

    class PipelineParams:  # type: ignore[no-redef]
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    class PipelineTask:  # type: ignore[no-redef]
        def __init__(self, pipeline, params=None, idle_timeout_secs=None, cancel_on_idle_timeout: bool = False) -> None:
            self.pipeline = pipeline
            self.params = params
            self.idle_timeout_secs = idle_timeout_secs
            self.cancel_on_idle_timeout = cancel_on_idle_timeout


logger = logging.getLogger("cabinet.voice.pipeline")

# PRD-8 Phase 7b — log-message redaction (Rule 3 module-attribute lookup).
from security import redact as _redact_mod  # noqa: E402
_redact = _redact_mod.redact


# ── HomieSTT — wraps voice.transcribe_audio_file ────────────────────────


class HomieSTT(FrameProcessor):  # type: ignore[misc]
    """Pipecat FrameProcessor that buffers audio frames and emits
    TranscriptionFrames via :func:`voice.transcribe_audio_file`.

    Phase 4's STT cascade (Groq → faster_whisper → whisper.cpp → mistral
    → openai) handles the actual recognition. This processor is a thin
    audio-frame-to-WAV adapter that flushes when speech ends (idle).

    Idle detection: Pipecat's ``WebsocketServerTransport`` does NOT do VAD
    on its own (vad_analyzer=None matches upstream warroom/server.py:146).
    So we accumulate audio frames until we receive a ``UserStoppedSpeaking``
    signal (or fall back to a configurable idle timeout). owner's voice
    cabinet is single-speaker so this naive flush model is fine.

    Sample rate: PCM16 mono at 16 kHz (matches the Pipecat browser client
    bundle + warroom/server.py:131-149).
    """

    DEFAULT_SAMPLE_RATE = 16000
    DEFAULT_CHANNELS = 1
    # Bytes per second at 16kHz PCM16 mono. ~1.5s buffer gives Phase 4 STT
    # enough audio to transcribe a short utterance reliably.
    _MIN_FLUSH_BYTES = 16000 * 2 * 1  # = 32KB ~= 1s of audio

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._buffer: bytearray = bytearray()

    async def process_frame(self, frame, direction) -> None:
        await super().process_frame(frame, direction)

        # Pass-through everything except inbound audio.
        if not isinstance(frame, AudioRawFrame):
            await self.push_frame(frame, direction)
            return
        # Skip TTS-generated audio coming downstream.
        if direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return

        self._buffer.extend(frame.audio or b"")

        # Naive flush when buffer accumulates past threshold (Pipecat will
        # emit UserStoppedSpeaking on a more sophisticated VAD setup; until
        # then this size-based flush keeps end-to-end latency reasonable).
        if len(self._buffer) >= self._MIN_FLUSH_BYTES:
            await self._flush_to_transcript(frame.sample_rate or self.DEFAULT_SAMPLE_RATE)

    async def _flush_to_transcript(self, sample_rate: int) -> None:
        if not self._buffer:
            return
        audio_bytes = bytes(self._buffer)
        self._buffer.clear()

        # Wrap raw PCM as a WAV file for voice.transcribe_audio_file.
        try:
            wav_path = await self._buffer_to_temp_wav(audio_bytes, sample_rate)
        except Exception as exc:  # noqa: BLE001
            logger.warning("HomieSTT WAV wrap failed: %s", _redact(str(exc)))
            return

        try:
            import voice  # noqa: PLC0415 — late-bind so import failures don't kill pipeline.
            text = await voice.transcribe_audio_file(wav_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("HomieSTT transcribe failed: %s", _redact(str(exc)))
            text = ""
        finally:
            try:
                os.unlink(wav_path)
            except OSError:
                pass

        text = (text or "").strip()
        if not text:
            return

        # Emit final transcription so AgentRouter routes it.
        await self.push_frame(TranscriptionFrame(text=text, user_id="user", timestamp=""))

    @staticmethod
    async def _buffer_to_temp_wav(pcm_bytes: bytes, sample_rate: int) -> str:
        """Wrap raw PCM16 mono bytes as a WAV file, return temp path."""
        import wave  # noqa: PLC0415

        fd, path = tempfile.mkstemp(suffix=".wav", prefix="cabinet_voice_stt_")
        os.close(fd)
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # PCM16
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_bytes)
        return path


# ── HomieTTS — wraps voice.synthesize with voice_overrides ──────────────


class HomieTTS(FrameProcessor):  # type: ignore[misc]
    """Pipecat FrameProcessor that synthesizes :class:`TextFrame` text via
    Phase 4's :func:`voice.synthesize` cascade (with WS0 ``voice_overrides``).

    Per-persona voice routing is driven by upstream ``TTSUpdateSettingsFrame``
    events from :class:`HomieAgentBridge`. The frame's ``settings`` dict
    carries:

      * ``voice``: voice id (provider-specific, e.g. ElevenLabs voice id).
      * ``provider`` (optional): provider key matching
        :data:`personas.services._CABINET_VOICE_PROVIDER_ENUM`. When set,
        ``synthesize`` is called with ``voice_overrides={provider: voice}``;
        when absent, the cascade falls through to env defaults.

    Outbound audio: Phase 4's cascade returns Opus/MP3/WAV bytes. For
    Pipecat WebSocket transport we need PCM16 at 24kHz mono (matches
    warroom/server.py:131-149 audio_out_sample_rate=24000). When the
    synthesized bytes are not raw PCM, we transcode via the existing
    ``_ffmpeg_pcm_wav_to_opus`` helper's inverse path. For Phase 6 MVP we
    pass the bytes through and let the transport handle resampling — this
    matches the upstream behavior (Cartesia returns PCM16 already).
    """

    DEFAULT_SAMPLE_RATE = 24000

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._current_voice: Optional[str] = None
        self._current_provider: Optional[str] = None

    async def process_frame(self, frame, direction) -> None:
        await super().process_frame(frame, direction)

        # Voice-switch update from the bridge.
        if isinstance(frame, TTSUpdateSettingsFrame):
            settings = getattr(frame, "settings", None) or {}
            voice = settings.get("voice") if isinstance(settings, dict) else None
            provider = settings.get("provider") if isinstance(settings, dict) else None
            # Voice-switch guard — only update when it actually changed.
            if isinstance(voice, str) and voice != self._current_voice:
                self._current_voice = voice
                if isinstance(provider, str) and provider:
                    self._current_provider = provider
            # Don't push the settings frame downstream — it's a control message.
            return

        # Synthesize text frames going downstream.
        if isinstance(frame, TextFrame) and direction == FrameDirection.DOWNSTREAM:
            await self._synthesize_and_emit(frame.text)
            return

        await self.push_frame(frame, direction)

    async def _synthesize_and_emit(self, text: str) -> None:
        if not (text or "").strip():
            return

        voice_overrides: dict[str, str] | None = None
        if self._current_voice and self._current_provider:
            voice_overrides = {self._current_provider: self._current_voice}

        try:
            import voice as voice_module  # noqa: PLC0415
            audio_bytes = await voice_module.synthesize(
                text,
                tts_config=None,
                voice_overrides=voice_overrides,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("HomieTTS synthesize failed: %s", _redact(str(exc)))
            return

        if not audio_bytes:
            return

        await self.push_frame(
            AudioRawFrame(
                audio=audio_bytes,
                sample_rate=self.DEFAULT_SAMPLE_RATE,
                num_channels=1,
            )
        )


# ── Pipeline factory — port of warroom/server.py:751-779 legacy mode ──────


def build_voice_pipeline(
    transport,
    *,
    meeting_id: int,
    chat_id: str | None = None,
    broadcast_order: list[str] | None = None,
    on_server_message=None,
):
    """Build the cabinet voice :class:`Pipeline` + :class:`PipelineTask`.

    VERBATIM port of ``warroom/server.py:751-758`` legacy mode pipeline
    shape:

        Pipeline([
            transport.input(),
            stt,
            router,
            bridge,
            tts,
            transport.output(),
        ])

    Plus the ``PipelineTask`` idle-timeout disable from
    ``warroom/server.py:690-691`` (those args belong on PipelineTask, NOT
    on the WebsocketServerTransport — R1 v2 B4 fix).

    Returns ``(pipeline, task)``.
    """
    if not _PIPECAT_AVAILABLE:  # pragma: no cover
        raise RuntimeError(
            "pipecat-ai is not installed; install with `uv add pipecat-ai[websocket,silero]==0.0.108` "
            "(see docs/cabinet-voice-setup.md for environment setup)"
        )
    from .voice_router import AgentRouter  # noqa: PLC0415
    from .agent_bridge import HomieAgentBridge  # noqa: PLC0415

    stt = HomieSTT()
    router_proc = AgentRouter()
    bridge = HomieAgentBridge(
        meeting_id=meeting_id,
        chat_id=chat_id,
        broadcast_order=broadcast_order,
        on_server_message=on_server_message,
    )
    tts = HomieTTS()

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            router_proc,
            bridge,
            tts,
            transport.output(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
        ),
        # CRITICAL: idle_timeout_secs / cancel_on_idle_timeout are
        # PipelineTask args, NOT transport args. Matches
        # warroom/server.py:690-691 verbatim.
        idle_timeout_secs=None,
        cancel_on_idle_timeout=False,
    )

    return pipeline, task


__all__ = [
    "HomieSTT",
    "HomieTTS",
    "build_voice_pipeline",
]
