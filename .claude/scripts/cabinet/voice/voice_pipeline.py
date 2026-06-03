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

import asyncio
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
        OutputAudioRawFrame,
        StartFrame,
        TTSAudioRawFrame,
        TextFrame,
        TranscriptionFrame,
        TTSUpdateSettingsFrame,
        UserStoppedSpeakingFrame,
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

    class OutputAudioRawFrame(AudioRawFrame):  # type: ignore[no-redef,misc]
        pass

    class TTSAudioRawFrame(OutputAudioRawFrame):  # type: ignore[no-redef,misc]
        pass

    class TTSUpdateSettingsFrame(Frame):  # type: ignore[no-redef,misc]
        def __init__(self, settings: dict | None = None) -> None:
            self.settings = settings or {}

    class UserStoppedSpeakingFrame(Frame):  # type: ignore[no-redef,misc]
        def __init__(self, emulated: bool = False) -> None:
            self.emulated = emulated

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
    signal, enough trailing silent audio, or the wall-clock idle timer fires.
    owner's voice cabinet is single-speaker so this naive flush model is fine.

    Sample rate: PCM16 mono at 16 kHz (matches the Pipecat browser client
    bundle + warroom/server.py:131-149).
    """

    DEFAULT_SAMPLE_RATE = 16000
    DEFAULT_CHANNELS = 1
    # Bytes per second at 16kHz PCM16 mono.
    _BYTES_PER_SAMPLE = 2
    _DEFAULT_MIN_UTTERANCE_SECS = 0.35
    _DEFAULT_MAX_UTTERANCE_SECS = 8.0
    _DEFAULT_MIN_RMS = 120
    _DEFAULT_IDLE_FLUSH_SECS = 0.9
    _DEFAULT_SILENCE_RMS = 180

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._buffer: bytearray = bytearray()
        self._sample_rate: int = self.DEFAULT_SAMPLE_RATE
        self._audio_frame_count: int = 0
        self._silent_bytes: int = 0
        self._has_speech: bool = False
        self._speech_generation: int = 0
        self._idle_flush_task: asyncio.Task[None] | None = None
        self._idle_flush_generation: int | None = None
        self._flush_lock = asyncio.Lock()

    async def process_frame(self, frame, direction) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, UserStoppedSpeakingFrame):
            if direction == FrameDirection.DOWNSTREAM:
                await self._flush_to_transcript(self._sample_rate, trigger="vad_stop")
                return
            await self.push_frame(frame, direction)
            return

        # Pass-through everything except inbound audio.
        if not isinstance(frame, AudioRawFrame):
            await self.push_frame(frame, direction)
            return
        # Skip TTS-generated audio coming downstream.
        if direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return

        self._sample_rate = frame.sample_rate or self.DEFAULT_SAMPLE_RATE
        audio = frame.audio or b""
        if len(audio) % self._BYTES_PER_SAMPLE != 0:
            logger.warning(
                "stt_ignored reason=odd_pcm_bytes bytes=%s sample_rate=%s",
                _redact(str(len(audio))),
                _redact(str(self._sample_rate)),
            )
            return

        frame_rms = self._pcm16_rms(audio)
        self._buffer.extend(audio)
        self._audio_frame_count += 1
        if self._audio_frame_count == 1 or self._audio_frame_count % 50 == 0:
            logger.info(
                "stt_audio_frame bytes=%s sample_rate=%s buffer_bytes=%s rms=%s",
                _redact(str(len(audio))),
                _redact(str(self._sample_rate)),
                _redact(str(len(self._buffer))),
                _redact(str(frame_rms)),
            )

        if frame_rms <= self._silence_rms():
            self._silent_bytes += len(audio)
        else:
            self._silent_bytes = 0
            self._has_speech = True
            self._speech_generation += 1

        if self._has_speech and len(self._buffer) >= self._min_transcribe_bytes(self._sample_rate):
            self._arm_idle_flush_timer(self._sample_rate, self._speech_generation)

        if (
            self._has_speech
            and len(self._buffer) >= self._min_transcribe_bytes(self._sample_rate)
            and self._silent_bytes >= self._idle_flush_bytes(self._sample_rate)
        ):
            await self._flush_to_transcript(self._sample_rate, trigger="idle_silence")
            return

        # Safety-net flush only for genuinely long continuous speech. The
        # previous 1s byte-count flush raced VAD, chopped turns, and sent
        # silence/noise into Whisper where it hallucinated. The preferred
        # boundary is VAD stop, with idle-silence detection covering browser
        # WebSocket sessions where VAD stop frames do not arrive reliably.
        if len(self._buffer) >= self._max_flush_bytes(self._sample_rate):
            await self._flush_to_transcript(self._sample_rate, trigger="max_buffer")

    async def _flush_to_transcript(self, sample_rate: int, *, trigger: str) -> None:
        async with self._flush_lock:
            if not self._buffer:
                logger.info("stt_flush trigger=%s ignored=empty_buffer", _redact(trigger))
                self._reset_utterance_state()
                return
            audio_bytes = bytes(self._buffer)
            self._buffer.clear()
            self._reset_utterance_state()
            duration_ms = int((len(audio_bytes) / (sample_rate * self._BYTES_PER_SAMPLE)) * 1000)
            rms = self._pcm16_rms(audio_bytes)
            logger.info(
                "stt_flush trigger=%s bytes=%s duration_ms=%s rms=%s",
                _redact(trigger),
                _redact(str(len(audio_bytes))),
                _redact(str(duration_ms)),
                _redact(str(rms)),
            )

            if len(audio_bytes) < self._min_transcribe_bytes(sample_rate):
                logger.info(
                    "stt_ignored reason=too_short bytes=%s min_bytes=%s",
                    _redact(str(len(audio_bytes))),
                    _redact(str(self._min_transcribe_bytes(sample_rate))),
                )
                return
            if rms < self._min_rms():
                logger.info(
                    "stt_ignored reason=low_rms rms=%s min_rms=%s",
                    _redact(str(rms)),
                    _redact(str(self._min_rms())),
                )
                return

            # Wrap raw PCM as a WAV file for voice.transcribe_audio_file.
            try:
                wav_path = await self._buffer_to_temp_wav(audio_bytes, sample_rate)
            except Exception as exc:  # noqa: BLE001
                logger.warning("HomieSTT WAV wrap failed: %s", _redact(str(exc)))
                return

            try:
                import voice  # noqa: PLC0415 — late-bind so import failures don't kill pipeline.
                text = await self._transcribe_wav(voice, wav_path)
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

    def _arm_idle_flush_timer(self, sample_rate: int, speech_generation: int) -> None:
        current = self._idle_flush_task
        if current is not None and not current.done():
            if self._idle_flush_generation == speech_generation:
                return
            current.cancel()
        task = asyncio.create_task(self._idle_flush_after(sample_rate, speech_generation))
        self._idle_flush_task = task
        self._idle_flush_generation = speech_generation

    async def _idle_flush_after(self, sample_rate: int, speech_generation: int) -> None:
        try:
            await asyncio.sleep(self._idle_flush_secs())
            if speech_generation != self._speech_generation:
                return
            if not self._has_speech or len(self._buffer) < self._min_transcribe_bytes(sample_rate):
                return
            await self._flush_to_transcript(sample_rate, trigger="idle_timer")
        except asyncio.CancelledError:
            return

    def _cancel_idle_flush_timer(self) -> None:
        task = self._idle_flush_task
        if task is None:
            return
        self._idle_flush_task = None
        self._idle_flush_generation = None
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if task is not current and not task.done():
            task.cancel()

    async def _transcribe_wav(self, voice_module, wav_path: str) -> str:
        """Transcribe with the cabinet's free/local preference first.

        The shared Phase 4 cascade still exists for generic voice features, but
        cabinet voice should not burn time against exhausted cloud STT keys.
        ``CABINET_VOICE_STT_PROVIDER=cascade`` opts back into the full cascade.
        """
        provider = (os.environ.get("CABINET_VOICE_STT_PROVIDER") or "faster_whisper").strip().lower()
        if provider in {"faster_whisper", "faster-whisper", "local", "free"}:
            installed = getattr(voice_module, "_faster_whisper_installed", lambda: False)
            provider_cls = getattr(voice_module, "_FasterWhisperProvider", None)
            if provider_cls is not None and installed():
                model_size = os.environ.get("FASTER_WHISPER_MODEL", "base")
                text = await provider_cls(model_size=model_size).transcribe(wav_path)
                logger.info(
                    "stt_provider provider=faster_whisper model=%s source=cabinet_local",
                    _redact(model_size),
                )
                filter_fn = getattr(voice_module, "_filter_whisper_hallucination", lambda value: value)
                return filter_fn(text)
            logger.warning("cabinet local faster-whisper unavailable; falling back to voice cascade")
        elif provider not in {"cascade", "auto", ""}:
            logger.warning("unknown CABINET_VOICE_STT_PROVIDER=%s; falling back to voice cascade", _redact(provider))
        return await voice_module.transcribe_audio_file(wav_path)

    @classmethod
    def _min_transcribe_bytes(cls, sample_rate: int) -> int:
        seconds = cls._env_float("CABINET_STT_MIN_UTTERANCE_SECS", cls._DEFAULT_MIN_UTTERANCE_SECS)
        return int(max(0.0, seconds) * sample_rate * cls._BYTES_PER_SAMPLE)

    @classmethod
    def _max_flush_bytes(cls, sample_rate: int) -> int:
        seconds = cls._env_float("CABINET_STT_MAX_UTTERANCE_SECS", cls._DEFAULT_MAX_UTTERANCE_SECS)
        return int(max(1.0, seconds) * sample_rate * cls._BYTES_PER_SAMPLE)

    @classmethod
    def _idle_flush_bytes(cls, sample_rate: int) -> int:
        return int(cls._idle_flush_secs() * sample_rate * cls._BYTES_PER_SAMPLE)

    @classmethod
    def _idle_flush_secs(cls) -> float:
        seconds = cls._env_float("CABINET_STT_IDLE_FLUSH_SECS", cls._DEFAULT_IDLE_FLUSH_SECS)
        return max(0.1, seconds)

    @classmethod
    def _min_rms(cls) -> int:
        return int(cls._env_float("CABINET_STT_MIN_RMS", float(cls._DEFAULT_MIN_RMS)))

    @classmethod
    def _silence_rms(cls) -> int:
        return int(cls._env_float("CABINET_STT_SILENCE_RMS", float(cls._DEFAULT_SILENCE_RMS)))

    def _reset_utterance_state(self) -> None:
        self._cancel_idle_flush_timer()
        self._silent_bytes = 0
        self._has_speech = False
        self._speech_generation += 1

    @staticmethod
    def _env_float(name: str, default: float) -> float:
        raw = os.environ.get(name, "")
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    @staticmethod
    def _pcm16_rms(pcm_bytes: bytes) -> int:
        sample_count = len(pcm_bytes) // 2
        if sample_count <= 0:
            return 0
        view = memoryview(pcm_bytes[: sample_count * 2]).cast("h")
        total = 0
        for sample in view:
            total += int(sample) * int(sample)
        return int((total / sample_count) ** 0.5)

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
        self._current_tts_settings: tuple[str, str] | None = None

    async def process_frame(self, frame, direction) -> None:
        await super().process_frame(frame, direction)

        # Voice-switch update from the bridge.
        if isinstance(frame, TTSUpdateSettingsFrame):
            settings = getattr(frame, "settings", None) or {}
            voice = settings.get("voice") if isinstance(settings, dict) else None
            provider = settings.get("provider") if isinstance(settings, dict) else None
            provider_ok = isinstance(provider, str) and bool(provider)
            voice_ok = isinstance(voice, str) and bool(voice)
            logger.info(
                "tts_settings provider=%s voice=%s",
                _redact(str(provider)),
                _redact(str(voice)),
            )
            if provider_ok != voice_ok:
                logger.warning(
                    "tts_settings rejected provider=%s voice=%s",
                    _redact(str(provider)),
                    _redact(str(voice)),
                )
                return
            if not provider_ok or not voice_ok:
                logger.warning("tts_settings rejected provider=None voice=None")
                return
            next_settings = (provider, voice)
            # Voice-switch guard — only update when the atomic provider/voice
            # tuple actually changed.
            if next_settings != self._current_tts_settings:
                self._current_tts_settings = next_settings
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
        if self._current_tts_settings:
            provider, voice = self._current_tts_settings
            voice_overrides = {provider: voice}
            logger.info(
                "tts_settings provider=%s voice=%s",
                _redact(provider),
                _redact(voice),
            )
        logger.info("tts_synthesize voice_overrides=%s", bool(voice_overrides))

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

        # Codex review finding #2 — decode compressed TTS output to PCM16.
        # Edge returns MP3 (FF F3 frame sync), Kokoro/OpenAI return Opus,
        # ElevenLabs returns MP3. Pushing those compressed bytes wrapped in
        # ``OutputAudioRawFrame(sample_rate=24000)`` makes Pipecat's output
        # transport and the JS client decode them as RAW PCM and play garbage
        # noise — owner's "trash" audio experience in meeting #6 was almost
        # certainly this, even after the cascade fix routed TTS to Edge.
        # ``voice.transcode_to_pcm16`` uses ffmpeg with auto-detected input
        # format and emits raw 16-bit LE PCM at the target sample rate.
        try:
            pcm_bytes = await voice_module.transcode_to_pcm16(
                audio_bytes,
                sample_rate=self.DEFAULT_SAMPLE_RATE,
                channels=1,
            )
        except Exception as exc:  # noqa: BLE001 — fail-quiet if ffmpeg unavailable.
            logger.warning(
                "HomieTTS PCM transcode failed (audio will be garbled): %s",
                _redact(str(exc)),
            )
            # Don't push compressed bytes downstream — they'd play as noise.
            return

        if not pcm_bytes:
            return

        # Pipecat TTS services emit ``TTSAudioRawFrame`` (a subclass of
        # ``OutputAudioRawFrame``). Use that canonical frame type so the output
        # transport and RTVI observers treat this as TTS audio, not a bare raw
        # audio mixin.
        await self.push_frame(
            TTSAudioRawFrame(
                audio=pcm_bytes,
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
    router_proc = AgentRouter(agent_names=broadcast_order)
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
