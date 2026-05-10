"""Voice cascade: STT + TTS providers with cascade fallback.

PORTED VERBATIM FROM ClaudeClaw `src/voice.ts:1-503` per PRD-8 §0a port-first
authoring rule (owner lock 2026-05-08). Hermes extras providers (faster-whisper,
KittenTTS, Mistral Voxtral, Gemini TTS) layered on top of the upstream cascade.

Cascade orders match upstream:
  STT: Groq Whisper -> faster-whisper local -> whisper-cpp local -> Mistral -> OpenAI
  TTS: ElevenLabs -> Gradium -> Mistral -> Gemini -> OpenAI -> Kokoro -> KittenTTS -> Edge -> macOS-say

Anti-patterns:
  Rule 1: All functions use None sentinels — no tunable config in default args.
  Rule 2: Provider classes stateless dataclasses; only `_ffmpeg_available` cached
          at module scope (one-time subprocess probe, derived state).
  Rule 3: Optional dep imports lazy inside method bodies. httpx is HARD dep.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import logging
import os
import platform
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Final, Protocol

logger = logging.getLogger(__name__)

# PRD-8 Phase 7b WS1 (codex post-build F1) — log-message redaction. Wrap dynamic
# args (provider exception strings, paths, URLs) at every cabinet/voice/dashboard
# log call site so secrets embedded in those values get scrubbed before logs land.
# Module-attribute import (Rule 3); redact() is unconditional (NOT kill-switch
# gated — see security/redact.py docstring).
from security import redact as _redact_mod  # noqa: E402
_redact = _redact_mod.redact

# Module-state cache — Rule 2 exception (one-time subprocess probe, derived state)
_ffmpeg_available: bool | None = None

# Per-provider character limits — VERBATIM PORT from Hermes tools/tts_tool.py:132-142.
# Do NOT alter values — this is a "port verbatim" claim. Homie-specific extensions
# (gradium / kokoro / macos_say) are layered AFTER the Hermes resolver, not inside
# this dict. (R1 B2: prior version invented values & extra entries.)
PROVIDER_MAX_TEXT_LENGTH: Final[dict[str, int]] = {
    "edge": 5000,         # edge-tts practical sync limit
    "openai": 4096,       # https://platform.openai.com/docs/guides/text-to-speech
    "xai": 15000,         # https://docs.x.ai/developers/model-capabilities/audio/text-to-speech
    "minimax": 10000,     # https://platform.minimax.io/docs/api-reference/speech-t2a-http (sync)
    "mistral": 4000,      # conservative; no published per-request cap
    "gemini": 5000,       # Gemini TTS caps at ~8k input tokens / ~655s audio
    "elevenlabs": 10000,  # fallback when model-aware lookup can't resolve (multilingual_v2)
    "neutts": 2000,       # local model, quality falls off on long text
    "kittentts": 2000,    # local 25MB model
}

# ElevenLabs caps vary by model_id — VERBATIM PORT from tts_tool.py:145-154.
ELEVENLABS_MODEL_MAX_TEXT_LENGTH: Final[dict[str, int]] = {
    "eleven_v3": 5000,
    "eleven_ttv_v3": 5000,
    "eleven_multilingual_v2": 10000,
    "eleven_multilingual_v1": 10000,
    "eleven_english_sts_v2": 10000,
    "eleven_english_sts_v1": 10000,
    "eleven_flash_v2": 30000,
    "eleven_flash_v2_5": 40000,
}

# Final fallback when provider isn't recognised at all — VERBATIM PORT from tts_tool.py:157.
FALLBACK_MAX_TEXT_LENGTH: Final[int] = 4000

# Homie-specific extension dict — applied AFTER the Hermes resolver returns the
# default for an unknown provider. NOT part of the verbatim port; explicitly
# labelled as a Homie augmentation.
_HOMIE_PROVIDER_CHAR_LIMITS_EXTENSION: Final[dict[str, int]] = {
    "gradium": 4000,
    "kokoro": 5000,
    "macos_say": 100000,  # /usr/bin/say takes long text
}

# ElevenLabs default model id (matches Hermes default for parity).
_DEFAULT_ELEVENLABS_MODEL_ID: Final[str] = "eleven_multilingual_v2"


def _resolve_max_text_length(
    provider: str | None,
    tts_config: dict | None = None,
) -> int:
    """VERBATIM port of Hermes tts_tool.py:163-197 _resolve_max_text_length.

    Resolution order (matches Hermes byte-for-byte — no Homie branch inside):
      1. tts.<provider>.max_text_length user override (config dict)
      2. ElevenLabs model-aware table (keyed on configured model_id)
      3. PROVIDER_MAX_TEXT_LENGTH default
      4. FALLBACK_MAX_TEXT_LENGTH (4000)

    Non-positive or non-int overrides fall through to the default so a broken
    config can't accidentally disable truncation entirely.

    The Homie-extension dict (gradium / kokoro / macos_say) is layered by the
    separate `resolve_max_text_length()` wrapper below — NOT by this verbatim
    Hermes resolver (R3 NB2 fix). Cascade hot path MUST call the public wrapper.
    """
    if not provider:
        return FALLBACK_MAX_TEXT_LENGTH
    key = provider.lower().strip()
    cfg = tts_config or {}
    prov_cfg = cfg.get(key) if isinstance(cfg.get(key), dict) else {}

    override = prov_cfg.get("max_text_length") if prov_cfg else None
    if isinstance(override, bool):
        # bool is an int subclass; treat explicit booleans as "not set"
        override = None
    if isinstance(override, int) and override > 0:
        return override

    if key == "elevenlabs":
        model_id = (prov_cfg or {}).get("model_id") or _DEFAULT_ELEVENLABS_MODEL_ID
        mapped = ELEVENLABS_MODEL_MAX_TEXT_LENGTH.get(str(model_id).strip())
        if mapped:
            return mapped

    return PROVIDER_MAX_TEXT_LENGTH.get(key, FALLBACK_MAX_TEXT_LENGTH)


def resolve_max_text_length(
    provider: str | None,
    tts_config: dict | None = None,
) -> int:
    """Homie wrapper — calls the verbatim Hermes resolver, then layers the
    Homie-extension dict for providers Hermes doesn't know about (gradium,
    kokoro, macos_say). Cascade callers MUST go through this wrapper, not
    `_resolve_max_text_length()` directly, so the extension applies. (R3 NB2
    fix — keeps the verbatim port truly verbatim.)
    """
    hermes_value = _resolve_max_text_length(provider, tts_config)
    # Hermes returns FALLBACK_MAX_TEXT_LENGTH when it doesn't recognize the
    # provider — only THEN do we look at the Homie extension dict, so a known
    # Hermes provider (e.g. elevenlabs) NEVER gets re-mapped by this layer.
    if hermes_value != FALLBACK_MAX_TEXT_LENGTH:
        return hermes_value
    if provider:
        key = provider.lower().strip()
        if key in _HOMIE_PROVIDER_CHAR_LIMITS_EXTENSION:
            return _HOMIE_PROVIDER_CHAR_LIMITS_EXTENSION[key]
    return hermes_value  # FALLBACK_MAX_TEXT_LENGTH


# ─── Provider Protocols ────────────────────────────────────────────────────

class SpeechToTextProvider(Protocol):
    """Interface for speech-to-text providers."""

    async def transcribe(self, audio_bytes: bytes) -> str:
        """Return the transcript for the provided audio bytes."""


class TextToSpeechProvider(Protocol):
    """Interface for text-to-speech providers."""

    async def synthesize(self, text: str) -> bytes:
        """Return encoded speech audio bytes for the provided text."""


# ─── Helpers (port voice.ts:16-26) ─────────────────────────────────────────

async def _has_ffmpeg() -> bool:
    """Cached check that ffmpeg is on PATH. Port voice.ts:16-26.

    Rule 2 exception — derived state from a one-time subprocess probe.
    """
    global _ffmpeg_available
    if _ffmpeg_available is not None:
        return _ffmpeg_available
    _ffmpeg_available = shutil.which("ffmpeg") is not None
    return _ffmpeg_available


def _faster_whisper_installed() -> bool:
    """Lazy capability check — does NOT import the package."""
    try:
        return importlib.util.find_spec("faster_whisper") is not None
    except (ValueError, ModuleNotFoundError):
        return False


def _kittentts_installed() -> bool:
    """Lazy capability check — does NOT import the package."""
    try:
        return importlib.util.find_spec("kittentts") is not None
    except (ValueError, ModuleNotFoundError):
        return False


def _edge_tts_installed() -> bool:
    """Lazy capability check — does NOT import the package."""
    try:
        return importlib.util.find_spec("edge_tts") is not None
    except (ValueError, ModuleNotFoundError):
        return False


def _wrap_pcm_as_wav(
    pcm_bytes: bytes,
    sample_rate: int = 24000,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    """Wrap raw signed-little-endian PCM with a standard WAV RIFF header.

    Port from Hermes tts_tool.py:601-632 — Gemini TTS returns
    audio/L16;codec=pcm;rate=24000 with no container.
    """
    byte_rate = sample_rate * channels * sample_width
    block_align = channels * sample_width
    data_size = len(pcm_bytes)
    fmt_chunk = struct.pack(
        "<4sIHHIIHH",
        b"fmt ",
        16,             # fmt chunk size (PCM)
        1,              # audio format (PCM)
        channels,
        sample_rate,
        byte_rate,
        block_align,
        sample_width * 8,
    )
    data_chunk_header = struct.pack("<4sI", b"data", data_size)
    riff_size = 4 + len(fmt_chunk) + len(data_chunk_header) + data_size
    riff_header = struct.pack("<4sI4s", b"RIFF", riff_size, b"WAVE")
    return riff_header + fmt_chunk + data_chunk_header + pcm_bytes


async def _ffmpeg_pcm_wav_to_opus(wav_bytes: bytes) -> bytes:
    """Transcode WAV (or PCM-wrapped WAV) to OGG Opus via ffmpeg subprocess.

    Used by Gemini TTS provider (PCM 24kHz output) and KittenTTS WAV output to
    produce Opus suitable for Telegram voice bubbles.

    Raises RuntimeError if ffmpeg unavailable — cascade falls through.
    """
    if not await _has_ffmpeg():
        raise RuntimeError("ffmpeg not installed — required for opus transcode")

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-f", "wav",
        "-i", "pipe:0",
        "-c:a", "libopus",
        "-b:a", "48k",
        "-f", "ogg",
        "-y",
        "pipe:1",
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(input=wav_bytes)
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"ffmpeg transcode failed (rc={proc.returncode}): {err}")
    return stdout


def _audio_bytes_to_temp_path(audio_bytes: bytes, suffix: str = ".ogg") -> str:
    """Write audio_bytes to a NamedTemporaryFile and return its path.

    Used by providers that take a file path (faster-whisper, whisper-cpp,
    Mistral SDK) when the caller passed bytes via the legacy 3-arg
    transcribe() back-compat surface.
    """
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="homie_voice_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(audio_bytes)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


# ─── STT Providers (port voice.ts:113-276 + Hermes extras) ─────────────────


@dataclass(slots=True)
class _GroqWhisperProvider:
    """Port voice.ts:157-223 transcribeAudioGroq() — Groq Whisper API.

    multipart/form-data POST to api.groq.com/openai/v1/audio/transcriptions
    with model='whisper-large-v3' and response_format='json'.
    """
    api_key: str
    model: str = "whisper-large-v3"

    async def transcribe(self, file_path: str) -> str:
        import httpx  # hard dep, NOT optional (R1 NM1)

        async with httpx.AsyncClient(timeout=60.0) as client:
            with open(file_path, "rb") as f:
                files = {
                    "file": (os.path.basename(file_path), f, "audio/ogg"),
                }
                data = {
                    "model": self.model,
                    "response_format": "json",
                }
                resp = await client.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    files=files,
                    data=data,
                )
                resp.raise_for_status()
                return resp.json().get("text", "")


@dataclass(slots=True)
class _FasterWhisperProvider:
    """NEW Hermes extras — faster-whisper local STT.

    Port from Hermes tools/transcription_tools.py:349-429. Auto-downloads
    base model (~150MB) on first call. True offline STT. Lazy import.

    Note: NO module-state model cache (Rule 2 — recreate per call). Slower
    cold-start in exchange for clean derived-state semantics.
    """
    model_size: str = "base"

    async def transcribe(self, file_path: str) -> str:
        from faster_whisper import WhisperModel  # lazy (Rule 3)

        # faster-whisper is sync — wrap in thread executor.
        # NOTE: faster-whisper library API is `model.transcribe(file_path, **kwargs)`
        # — do NOT confuse with our cascade `transcribe_audio_file()` entrypoint.
        # See Hermes transcription_tools.py:401 + :421 for the verbatim shape.
        # R3 NB1 fix: this is the EXTERNAL library API, NOT the cascade entrypoint.
        def _sync_transcribe() -> str:
            model = WhisperModel(self.model_size, device="cpu", compute_type="int8")
            segments, _info = model.transcribe(file_path)
            return " ".join(s.text.strip() for s in segments).strip()

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _sync_transcribe)


@dataclass(slots=True)
class _MistralVoxtralSttProvider:
    """NEW Hermes extras — multilingual STT via Mistral Voxtral.

    21 languages. Port from Hermes transcription_tools.py:637-668.
    """
    api_key: str
    model: str = "voxtral-mini-latest"

    async def transcribe(self, file_path: str) -> str:
        from mistralai.client import Mistral  # lazy (Rule 3)

        def _sync_transcribe() -> str:
            with Mistral(api_key=self.api_key) as client:
                with open(file_path, "rb") as audio_file:
                    result = client.audio.transcriptions.complete(
                        model=self.model,
                        file={"content": audio_file, "file_name": os.path.basename(file_path)},
                    )
                # Mistral SDK returns an object with `.text` attribute
                text = getattr(result, "text", None)
                if text is None and hasattr(result, "transcript"):
                    text = result.transcript
                return text or ""

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _sync_transcribe)


@dataclass(slots=True)
class _WhisperCppProvider:
    """Port voice.ts:231-254 transcribeAudioLocal() — whisper-cpp subprocess.

    ffmpeg transcode to 16kHz mono WAV first, then `whisper-cpp -m <model>
    -f <wav> --output-json --no-timestamps -l auto`.
    """
    binary_path: str
    model_path: str

    async def transcribe(self, file_path: str) -> str:
        if not await _has_ffmpeg():
            raise RuntimeError("ffmpeg not installed — required for whisper-cpp transcode")

        # Build temp WAV path next to the source file
        src_path = Path(file_path)
        wav_path = str(src_path.with_suffix(".wav"))

        # Step 1: transcode to 16kHz mono WAV
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-i", file_path,
            "-ar", "16000",
            "-ac", "1",
            "-y",
            wav_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"ffmpeg WAV transcode failed: {err}")

        try:
            # Step 2: whisper-cpp invocation
            proc = await asyncio.create_subprocess_exec(
                self.binary_path,
                "-m", self.model_path,
                "-f", wav_path,
                "--output-json",
                "--no-timestamps",
                "-l", "auto",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace")[:500]
                raise RuntimeError(f"whisper-cpp failed: {err}")
            try:
                result = json.loads(stdout.decode("utf-8", errors="replace"))
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"whisper-cpp output not valid JSON: {exc}") from exc

            segments = result.get("transcription") or []
            text = " ".join(s.get("text", "") for s in segments).strip()
            return text
        finally:
            try:
                os.unlink(wav_path)
            except OSError:
                pass


@dataclass(slots=True)
class OpenAIWhisperProvider:
    """EXISTING — preserved for back-compat. OpenAI Whisper STT provider.

    Accepts either bytes (legacy 3-arg path) or a file path string.
    """
    api_key: str
    model: str = "whisper-1"

    async def transcribe(self, audio_or_path: bytes | str) -> str:
        from openai import AsyncOpenAI  # lazy (Rule 3)

        client = AsyncOpenAI(api_key=self.api_key)
        if isinstance(audio_or_path, (bytes, bytearray)):
            buf = BytesIO(audio_or_path)
            buf.name = "audio.ogg"
            resp = await client.audio.transcriptions.create(model=self.model, file=buf)
            return resp.text
        # File path — read into memory
        with open(audio_or_path, "rb") as f:
            buf = BytesIO(f.read())
            buf.name = os.path.basename(audio_or_path)
            resp = await client.audio.transcriptions.create(model=self.model, file=buf)
            return resp.text


# ─── STT Cascade (port voice.ts:262-276) ──────────────────────────────────


# STT cascade order — module-level for AST-style introspection in tests.
# Order matches voice.ts:262-276 with Hermes extras inserted at priority slots.
_STT_CASCADE_ORDER: Final[tuple[str, ...]] = (
    "groq",            # voice.ts cloud primary
    "faster_whisper",  # Hermes extras — local offline
    "whisper_cpp",     # voice.ts local fallback
    "mistral",         # Hermes extras — multilingual
    "openai",          # back-compat
)


async def transcribe_audio_file(file_path: str | Path) -> str:
    """STT cascade: Groq → faster-whisper local → whisper-cpp local → Mistral → OpenAI.

    NEW canonical cascade entrypoint per R1 B6 (avoids signature collision with
    legacy `transcribe(audio_bytes, api_key, model)` at voice.py:104-107 which
    is preserved verbatim for back-compat).

    Port voice.ts:262-276 verbatim. Provider order:
      1. Groq Whisper (cloud, fastest) if GROQ_API_KEY set
      2. faster-whisper local (Hermes extras) — runs offline
      3. whisper-cpp local (voice.ts:231-254) if WHISPER_MODEL_PATH set
      4. Mistral Voxtral (Hermes extras) — multilingual fallback
      5. OpenAI Whisper (back-compat) if OPENAI_API_KEY set

    On exception per provider, log warn and try next. If all fail, raise.

    PRD-8 Phase 7b WS2.1 — operator kill-switch ("voice"). Module-attribute
    lookup so monkeypatch propagates (Rule 3). Catches BEFORE any provider
    cascade attempt; refusal counter increments + audit_log row written.
    Adapters catch ``KillSwitchDisabled`` and emit friendly degraded reply.
    """
    # Phase 7b kill-switch — late-bind module import (Rule 3).
    from security import kill_switches
    kill_switches.requireEnabled("voice", caller="voice_cascade_transcribe")

    path_str = str(file_path)
    last_err: Exception | None = None

    # 1. Groq Whisper (cloud, primary)
    groq_key = os.environ.get("GROQ_API_KEY")
    if groq_key:
        try:
            return await _GroqWhisperProvider(api_key=groq_key).transcribe(path_str)
        except Exception as e:
            logger.warning("Groq Whisper failed, trying faster-whisper local: %s", _redact(str(e)))
            last_err = e

    # 2. faster-whisper local (Hermes extras)
    if _faster_whisper_installed():
        try:
            model_size = os.environ.get("FASTER_WHISPER_MODEL", "base")
            return await _FasterWhisperProvider(model_size=model_size).transcribe(path_str)
        except ImportError as e:
            logger.warning("faster-whisper import failed, trying next: %s", _redact(str(e)))
            last_err = e
        except Exception as e:
            logger.warning("faster-whisper local failed, trying next: %s", _redact(str(e)))
            last_err = e

    # 3. whisper-cpp local (voice.ts:231-254)
    whisper_model = os.environ.get("WHISPER_MODEL_PATH")
    if whisper_model:
        whisper_bin = os.environ.get("WHISPER_CPP_PATH", "whisper-cpp")
        try:
            return await _WhisperCppProvider(
                binary_path=whisper_bin,
                model_path=whisper_model,
            ).transcribe(path_str)
        except Exception as e:
            logger.warning("whisper-cpp local failed, trying Mistral: %s", _redact(str(e)))
            last_err = e

    # 4. Mistral Voxtral (Hermes extras)
    mistral_key = os.environ.get("MISTRAL_API_KEY")
    if mistral_key:
        try:
            return await _MistralVoxtralSttProvider(api_key=mistral_key).transcribe(path_str)
        except ImportError as e:
            logger.warning("mistralai SDK unavailable, trying OpenAI Whisper: %s", _redact(str(e)))
            last_err = e
        except Exception as e:
            logger.warning("Mistral Voxtral STT failed, trying OpenAI Whisper: %s", _redact(str(e)))
            last_err = e

    # 5. OpenAI Whisper (back-compat)
    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key:
        try:
            return await OpenAIWhisperProvider(api_key=openai_key).transcribe(path_str)
        except Exception as e:
            logger.warning("OpenAI Whisper failed: %s", _redact(str(e)))
            last_err = e

    raise RuntimeError(
        f"All STT providers failed (or no provider configured). Last error: {last_err}"
    )


# Legacy back-compat — DO NOT REMOVE. Kept verbatim from voice.py:104-107.
async def transcribe(audio_bytes: bytes, api_key: str, model: str = "whisper-1") -> str:
    """LEGACY 3-arg STT helper — single-shot OpenAI Whisper transcribe(bytes, key, model).

    Adapters that haven't migrated to the cascade still call this. R1 B6 fix:
    legacy 3-arg signature stays; new cascade is `transcribe_audio_file()`.

    PRD-8 Phase 7b WS2.1 R4 (codex R3 NM4 — CORRECTNESS FIX): operator
    kill-switch ("voice"). Module-attribute lookup so monkeypatch propagates
    (Rule 3). Closes the Telegram fallback bypass — adapters/telegram.py:587
    rerouted through this entrypoint will refuse with KillSwitchDisabled when
    HOMIE_KILLSWITCH_VOICE=disabled. Refusal counter increments + audit_log
    row written; 6 adapters catch and emit friendly degraded reply.
    """
    # Phase 7b kill-switch — late-bind module import (Rule 3).
    from security import kill_switches
    kill_switches.requireEnabled("voice", caller="voice_legacy_transcribe")

    return await OpenAIWhisperProvider(api_key=api_key, model=model).transcribe(audio_bytes)


# ─── TTS Providers (port voice.ts:278-435 + Hermes extras) ────────────────


@dataclass(slots=True)
class _ElevenLabsProvider:
    """Port voice.ts:283-313 synthesizeSpeechElevenLabs.

    eleven_turbo_v2_5, stability=0.5, similarity_boost=0.75. POST to
    api.elevenlabs.io/v1/text-to-speech/{voice_id} with xi-api-key header.
    """
    api_key: str
    voice_id: str
    model_id: str = "eleven_turbo_v2_5"
    stability: float = 0.5
    similarity_boost: float = 0.75

    async def synthesize(self, text: str) -> bytes:
        import httpx  # hard dep

        payload = {
            "text": text,
            "model_id": self.model_id,
            "voice_settings": {
                "stability": self.stability,
                "similarity_boost": self.similarity_boost,
            },
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}",
                headers={
                    "xi-api-key": self.api_key,
                    "Content-Type": "application/json",
                    "Accept": "audio/mpeg",
                },
                json=payload,
            )
            resp.raise_for_status()
            return resp.content


@dataclass(slots=True)
class _GradiumProvider:
    """Port voice.ts:321-348 synthesizeSpeechGradium.

    output_format='opus', only_audio=true, endpoint
    eu.api.gradium.ai/api/post/speech/tts, header x-api-key.
    """
    api_key: str
    voice_id: str

    async def synthesize(self, text: str) -> bytes:
        import httpx  # hard dep

        payload = {
            "text": text,
            "voice_id": self.voice_id,
            "output_format": "opus",
            "only_audio": True,
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://eu.api.gradium.ai/api/post/speech/tts",
                headers={
                    "x-api-key": self.api_key,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            return resp.content


@dataclass(slots=True)
class _MistralVoxtralTtsProvider:
    """NEW Hermes extras — voxtral-mini-tts-2603, 21 languages, native Opus.

    Port from Hermes tts_tool.py:552-595.
    """
    api_key: str
    model: str = "voxtral-mini-tts-2603"
    voice_id: str = "axios"

    async def synthesize(self, text: str) -> bytes:
        from mistralai.client import Mistral  # lazy (Rule 3)

        def _sync_synth() -> bytes:
            with Mistral(api_key=self.api_key) as client:
                response = client.audio.speech.complete(
                    model=self.model,
                    input=text,
                    voice_id=self.voice_id,
                    response_format="opus",
                )
                # Mistral returns base64-encoded audio in `audio_data`
                return base64.b64decode(response.audio_data)

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _sync_synth)


@dataclass(slots=True)
class _GeminiTtsProvider:
    """NEW Hermes extras — gemini-2.5-flash-preview-tts. 30 voices.

    PCM 24kHz output → ffmpeg opus transcode required.

    R1 NM1: REST shape via httpx (NOT google.generativeai SDK). Hermes uses
    requests directly per tts_tool.py:635-688; we use httpx directly. Prefers
    GEMINI_API_KEY then GOOGLE_API_KEY (matches tts_tool.py:654).
    """
    api_key: str
    voice: str = "Charon"
    model: str = "gemini-2.5-flash-preview-tts"
    base_url: str = "https://generativelanguage.googleapis.com/v1beta"

    async def synthesize(self, text: str) -> bytes:
        import httpx  # hard dep, NOT optional (R1 NM1)

        payload: dict[str, Any] = {
            "contents": [{"parts": [{"text": text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {"voiceName": self.voice},
                    },
                },
            },
        }
        endpoint = f"{self.base_url.rstrip('/')}/models/{self.model}:generateContent"
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                endpoint,
                params={"key": self.api_key},
                headers={"Content-Type": "application/json"},
                json=payload,
            )
            if resp.status_code != 200:
                try:
                    err = resp.json().get("error", {})
                    detail = err.get("message") or resp.text[:300]
                except Exception:
                    detail = resp.text[:300]
                raise RuntimeError(
                    f"Gemini TTS API error (HTTP {resp.status_code}): {detail}"
                )

            data = resp.json()
            try:
                parts = data["candidates"][0]["content"]["parts"]
            except (KeyError, IndexError, TypeError) as e:
                raise RuntimeError(f"Gemini TTS response was malformed: {e}") from e
            audio_part = next(
                (p for p in parts if "inlineData" in p or "inline_data" in p),
                None,
            )
            if audio_part is None:
                raise RuntimeError("Gemini TTS response contained no audio data")
            inline = audio_part.get("inlineData") or audio_part.get("inline_data") or {}
            audio_b64 = inline.get("data", "")
            if not audio_b64:
                raise RuntimeError("Gemini TTS returned empty audio data")

            pcm_bytes = base64.b64decode(audio_b64)
            wav_bytes = _wrap_pcm_as_wav(pcm_bytes)
            # Transcode to opus for Telegram parity. Falls through cascade if
            # ffmpeg is missing.
            return await _ffmpeg_pcm_wav_to_opus(wav_bytes)


@dataclass(slots=True)
class _KokoroProvider:
    """Port voice.ts:358-396 synthesizeSpeechKokoro — local OpenAI-compat TTS.

    KOKORO_URL default http://localhost:8880, /v1/audio/speech,
    response_format='opus'.
    """
    base_url: str = "http://localhost:8880"
    voice: str = "af_heart"
    model: str = "kokoro"

    async def synthesize(self, text: str) -> bytes:
        import httpx  # hard dep

        payload = {
            "model": self.model,
            "input": text,
            "voice": self.voice,
            "response_format": "opus",
        }
        url = f"{self.base_url.rstrip('/')}/v1/audio/speech"
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                url,
                headers={"Content-Type": "application/json"},
                json=payload,
            )
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"Kokoro HTTP {resp.status_code}: {resp.text[:300]}"
                )
            return resp.content


@dataclass(slots=True)
class _KittenTtsProvider:
    """NEW Hermes extras — pip install kittentts; 25MB ONNX; cross-platform.

    Default voice 'Jasper' matches Hermes default. Generates WAV at 24kHz,
    transcodes to Opus via ffmpeg.
    """
    voice: str = "Jasper"
    model: str = "KittenML/kitten-tts-nano-0.2"

    async def synthesize(self, text: str) -> bytes:
        from kittentts import KittenTTS  # lazy (Rule 3)
        import soundfile as sf  # lazy

        def _sync_synth() -> bytes:
            mdl = KittenTTS(self.model)
            audio = mdl.generate(text, voice=self.voice)
            buf = BytesIO()
            sf.write(buf, audio, 24000, format="WAV")
            return buf.getvalue()

        loop = asyncio.get_running_loop()
        wav_bytes = await loop.run_in_executor(None, _sync_synth)
        return await _ffmpeg_pcm_wav_to_opus(wav_bytes)


@dataclass(slots=True)
class _MacOsSayProvider:
    """Port voice.ts:405-435 synthesizeSpeechLocal — macOS-only fallback.

    /usr/bin/say -v <voice> -o <aiff> then ffmpeg transcode to opus.
    """
    voice: str = "Thomas"

    async def synthesize(self, text: str) -> bytes:
        if platform.system() != "Darwin":
            raise RuntimeError("Local TTS only available on macOS")
        if not await _has_ffmpeg():
            raise RuntimeError("ffmpeg not installed — required for local TTS")

        # Use temp files — say cannot stream
        aiff_fd, aiff_path = tempfile.mkstemp(suffix=".aiff", prefix="homie_tts_")
        ogg_fd, ogg_path = tempfile.mkstemp(suffix=".ogg", prefix="homie_tts_")
        # Close immediately — subprocess overwrites
        os.close(aiff_fd)
        os.close(ogg_fd)

        try:
            # Step 1: say -> aiff
            proc = await asyncio.create_subprocess_exec(
                "/usr/bin/say",
                "-v", self.voice,
                "-o", aiff_path,
                text,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(
                    f"/usr/bin/say failed: {stderr.decode('utf-8', errors='replace')[:300]}"
                )

            # Step 2: ffmpeg aiff -> opus ogg
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-i", aiff_path,
                "-c:a", "libopus",
                "-b:a", "48k",
                "-y",
                ogg_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(
                    f"ffmpeg transcode failed: {stderr.decode('utf-8', errors='replace')[:300]}"
                )

            with open(ogg_path, "rb") as f:
                return f.read()
        finally:
            for p in (aiff_path, ogg_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass


@dataclass(slots=True)
class EdgeTtsProvider:
    """EXISTING — preserved for back-compat AND now in cascade."""

    voice: str = "en-US-GuyNeural"

    async def synthesize(self, text: str) -> bytes:
        import edge_tts  # lazy (Rule 3)

        communicate = edge_tts.Communicate(text, self.voice)
        buf = BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        return buf.getvalue()


@dataclass(slots=True)
class OpenAITtsProvider:
    """EXISTING — preserved for back-compat AND now in cascade."""

    api_key: str
    voice: str = "alloy"

    async def synthesize(self, text: str) -> bytes:
        from openai import AsyncOpenAI  # lazy (Rule 3)

        client = AsyncOpenAI(api_key=self.api_key)
        resp = await client.audio.speech.create(
            model="tts-1",
            voice=self.voice,
            input=text,
            response_format="opus",
        )
        return resp.content


# ─── TTS Cascade (port voice.ts:443-479) ──────────────────────────────────


# TTS cascade order — module-level for AST-style introspection in tests.
# Order matches voice.ts:443-479 with Hermes extras inserted at priority slots.
_TTS_CASCADE_ORDER: Final[tuple[str, ...]] = (
    "elevenlabs",   # voice.ts primary
    "gradium",      # voice.ts secondary
    "mistral",      # Hermes extras
    "gemini",       # Hermes extras
    "openai",       # already-existing module — added to cascade
    "kokoro",       # voice.ts local
    "kittentts",    # Hermes extras local
    "edge",         # already-existing module — added to cascade
    "macos_say",    # voice.ts mac-only fallback
)


async def _try_provider(
    provider_name: str,
    provider: TextToSpeechProvider,
    text: str,
    tts_config: dict | None = None,
) -> bytes:
    """Truncate text per provider char limit, then call provider.synthesize.

    R4 NB1: hot path uses the public `resolve_max_text_length()` wrapper, NOT
    the private `_resolve_max_text_length()`. The wrapper layers the Homie
    extension dict (gradium/kokoro/macos_say) AFTER Hermes returns FALLBACK,
    so Homie-only providers get their intended caps; Hermes-known providers
    (elevenlabs=10000, etc.) pass through unchanged.
    """
    cap = resolve_max_text_length(provider_name, tts_config)
    text_to_send = text[:cap] if len(text) > cap else text
    return await provider.synthesize(text_to_send)


async def synthesize(
    text: str,
    tts_config: dict | None = None,
    voice_overrides: dict[str, str] | None = None,
) -> bytes:
    """TTS cascade: ElevenLabs → Gradium → Mistral → Gemini → OpenAI → Kokoro → KittenTTS → Edge → macOS-say.

    Port voice.ts:443-479. Per-provider char-limit truncation BEFORE the call
    (Hermes tts_tool.py:132-197). On exception, log warn + try next provider.

    R4 NB1: Hot path MUST use the public wrapper resolve_max_text_length(),
    NOT the private _resolve_max_text_length(). The wrapper layers the Homie
    extension dict (gradium/kokoro/macos_say) AFTER Hermes returns FALLBACK,
    so Homie-only providers get their intended caps. Hermes-known providers
    (elevenlabs=10000, etc.) pass through unchanged.

    PRD-8 Phase 7b WS2.1 R4 (codex R3 NM4 — CORRECTNESS FIX): operator
    kill-switch ("voice"). Module-attribute lookup so monkeypatch propagates
    (Rule 3). Catches BEFORE any provider cascade attempt; refusal counter
    increments + audit_log row written. Adapters catch ``KillSwitchDisabled``
    and emit friendly degraded reply.

    PRD-8 Phase 6 / WS0 (port-first cabinet voice — voice_overrides backport):

    ``voice_overrides`` is an OPTIONAL ``dict[str, str]`` keyed by provider name
    (``elevenlabs``/``gradium``/``mistral``/``gemini``/``openai``/``kokoro``/
    ``kittentts``/``edge``/``macos_say``). When supplied, the matching cascade
    branch reads its voice id/name from the override before falling back to the
    env var default. Forward-additive: ``voice_overrides=None`` (default) and
    ``voice_overrides={}`` BOTH preserve existing Phase 4 behavior verbatim —
    every provider continues to read voice from the same env vars it always
    has. Phase 6's HomieTTS computes ``{voice_provider: voice_id}`` per-persona
    from ``<profile>/config.yaml.cabinet.voice_id`` + ``cabinet.voice_provider``.

    Rule 1 compliance: ``voice_overrides=None`` is the sentinel; resolved at
    call time inside the body. No def-time bind to module/config constants.
    """
    # Phase 7b kill-switch — late-bind module import (Rule 3).
    from security import kill_switches
    kill_switches.requireEnabled("voice", caller="voice_cascade_synthesize")

    # Rule 1: resolve sentinel inside body. Empty dict is the no-override path.
    if voice_overrides is None:
        voice_overrides = {}

    last_err: Exception | None = None

    # 1. ElevenLabs (voice.ts primary)
    el_key = os.environ.get("ELEVENLABS_API_KEY")
    el_voice = voice_overrides.get("elevenlabs") or os.environ.get("ELEVENLABS_VOICE_ID")
    if el_key and el_voice:
        try:
            return await _try_provider(
                "elevenlabs",
                _ElevenLabsProvider(api_key=el_key, voice_id=el_voice),
                text,
                tts_config,
            )
        except Exception as e:
            logger.warning("ElevenLabs TTS failed, trying Gradium: %s", _redact(str(e)))
            last_err = e

    # 2. Gradium (voice.ts secondary)
    gr_key = os.environ.get("GRADIUM_API_KEY")
    gr_voice = voice_overrides.get("gradium") or os.environ.get("GRADIUM_VOICE_ID")
    if gr_key and gr_voice:
        try:
            return await _try_provider(
                "gradium",
                _GradiumProvider(api_key=gr_key, voice_id=gr_voice),
                text,
                tts_config,
            )
        except Exception as e:
            logger.warning("Gradium TTS failed, trying Mistral: %s", _redact(str(e)))
            last_err = e

    # 3. Mistral Voxtral (Hermes extras) — Mistral does not currently expose a
    # per-call voice override on the public API; voice_overrides["mistral"] is
    # accepted for forward compatibility but does not affect provider config.
    mistral_key = os.environ.get("MISTRAL_API_KEY")
    if mistral_key:
        try:
            return await _try_provider(
                "mistral",
                _MistralVoxtralTtsProvider(api_key=mistral_key),
                text,
                tts_config,
            )
        except ImportError as e:
            logger.warning("mistralai SDK unavailable, trying Gemini: %s", _redact(str(e)))
            last_err = e
        except Exception as e:
            logger.warning("Mistral Voxtral TTS failed, trying Gemini: %s", _redact(str(e)))
            last_err = e

    # 4. Gemini TTS (Hermes extras) — prefer GEMINI_API_KEY then GOOGLE_API_KEY
    gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if gemini_key:
        try:
            gemini_voice = voice_overrides.get("gemini") or os.environ.get("GEMINI_TTS_VOICE", "Charon")
            return await _try_provider(
                "gemini",
                _GeminiTtsProvider(api_key=gemini_key, voice=gemini_voice),
                text,
                tts_config,
            )
        except Exception as e:
            logger.warning("Gemini TTS failed, trying OpenAI: %s", _redact(str(e)))
            last_err = e

    # 5. OpenAI TTS (existing module — added to cascade)
    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key:
        try:
            openai_voice = voice_overrides.get("openai") or os.environ.get("OPENAI_TTS_VOICE", "alloy")
            return await _try_provider(
                "openai",
                OpenAITtsProvider(api_key=openai_key, voice=openai_voice),
                text,
                tts_config,
            )
        except Exception as e:
            logger.warning("OpenAI TTS failed, trying Kokoro: %s", _redact(str(e)))
            last_err = e

    # 6. Kokoro local OpenAI-compat (voice.ts)
    kokoro_url = os.environ.get("KOKORO_URL")
    if kokoro_url:
        try:
            kokoro_voice = voice_overrides.get("kokoro") or os.environ.get("KOKORO_VOICE", "af_heart")
            return await _try_provider(
                "kokoro",
                _KokoroProvider(
                    base_url=kokoro_url,
                    voice=kokoro_voice,
                    model=os.environ.get("KOKORO_MODEL", "kokoro"),
                ),
                text,
                tts_config,
            )
        except Exception as e:
            logger.warning("Kokoro TTS failed, trying KittenTTS: %s", _redact(str(e)))
            last_err = e

    # 7. KittenTTS (Hermes extras — local cross-platform)
    if _kittentts_installed():
        try:
            kitten_voice = voice_overrides.get("kittentts") or os.environ.get("KITTENTTS_VOICE", "Jasper")
            return await _try_provider(
                "kittentts",
                _KittenTtsProvider(voice=kitten_voice),
                text,
                tts_config,
            )
        except ImportError as e:
            logger.warning("kittentts unavailable, trying Edge: %s", _redact(str(e)))
            last_err = e
        except Exception as e:
            logger.warning("KittenTTS failed, trying Edge: %s", _redact(str(e)))
            last_err = e

    # 8. Edge TTS (existing module — added to cascade)
    if _edge_tts_installed():
        try:
            edge_voice = voice_overrides.get("edge") or os.environ.get("EDGE_TTS_VOICE", "en-US-GuyNeural")
            return await _try_provider(
                "edge",
                EdgeTtsProvider(voice=edge_voice),
                text,
                tts_config,
            )
        except ImportError as e:
            logger.warning("edge-tts unavailable, trying macOS-say: %s", _redact(str(e)))
            last_err = e
        except Exception as e:
            logger.warning("Edge TTS failed, trying macOS-say: %s", _redact(str(e)))
            last_err = e

    # 9. macOS-say (voice.ts mac-only fallback)
    if platform.system() == "Darwin":
        try:
            macos_voice = voice_overrides.get("macos_say") or os.environ.get("TTS_VOICE", "Thomas")
            return await _try_provider(
                "macos_say",
                _MacOsSayProvider(voice=macos_voice),
                text,
                tts_config,
            )
        except Exception as e:
            logger.warning("macOS-say failed: %s", _redact(str(e)))
            last_err = e

    raise RuntimeError(
        f"All TTS providers failed (or no provider configured). Last error: {last_err}"
    )


# ─── Capability Snapshot (port voice.ts:487-503) ──────────────────────────


def voice_capabilities() -> dict[str, bool]:
    """Port voice.ts:487-503 voiceCapabilities() with Homie extension layer.

    Returns dict {'stt': bool, 'tts': bool}.

    STT true if any of: GROQ_API_KEY, WHISPER_MODEL_PATH, OPENAI_API_KEY,
    MISTRAL_API_KEY set, OR faster_whisper installed.

    TTS true if any of: ElevenLabs (BOTH key+voice_id), Gradium (BOTH),
    MISTRAL_API_KEY, GEMINI_API_KEY, GOOGLE_API_KEY, OPENAI_API_KEY,
    KOKORO_URL set, OR kittentts/edge_tts installed, OR Darwin platform.
    """
    return {
        "stt": (
            bool(os.environ.get("GROQ_API_KEY"))
            or bool(os.environ.get("WHISPER_MODEL_PATH"))
            or bool(os.environ.get("OPENAI_API_KEY"))
            or bool(os.environ.get("MISTRAL_API_KEY"))
            or _faster_whisper_installed()
        ),
        "tts": (
            (bool(os.environ.get("ELEVENLABS_API_KEY")) and bool(os.environ.get("ELEVENLABS_VOICE_ID")))
            or (bool(os.environ.get("GRADIUM_API_KEY")) and bool(os.environ.get("GRADIUM_VOICE_ID")))
            or bool(os.environ.get("MISTRAL_API_KEY"))
            or bool(os.environ.get("GEMINI_API_KEY"))
            or bool(os.environ.get("GOOGLE_API_KEY"))
            or bool(os.environ.get("OPENAI_API_KEY"))
            or bool(os.environ.get("KOKORO_URL"))
            or _kittentts_installed()
            or _edge_tts_installed()
            or platform.system() == "Darwin"
        ),
    }


# ─── Existing back-compat wrappers (preserved verbatim from voice.py:78-119) ─


@dataclass(slots=True)
class VoiceProviderSet:
    """Resolved voice providers used by the Telegram adapter."""

    stt: SpeechToTextProvider | None
    tts: TextToSpeechProvider


def build_voice_provider_set(
    *,
    openai_api_key: str = "",
    stt_model: str = "whisper-1",
    tts_engine: str = "edge",
    tts_voice_edge: str = "en-US-GuyNeural",
    tts_voice_openai: str = "alloy",
) -> VoiceProviderSet:
    """Resolve independent STT and TTS providers from config (legacy helper)."""

    stt = OpenAIWhisperProvider(openai_api_key, stt_model) if openai_api_key else None
    if tts_engine == "openai" and openai_api_key:
        tts: TextToSpeechProvider = OpenAITtsProvider(openai_api_key, tts_voice_openai)
    else:
        tts = EdgeTtsProvider(tts_voice_edge)
    return VoiceProviderSet(stt=stt, tts=tts)


async def synthesize_edge(text: str, voice: str = "en-US-GuyNeural") -> bytes:
    """Backward-compatible helper for direct Edge TTS calls."""
    return await EdgeTtsProvider(voice=voice).synthesize(text)


async def synthesize_openai(text: str, api_key: str, voice: str = "alloy") -> bytes:
    """Backward-compatible helper for direct OpenAI TTS calls."""
    return await OpenAITtsProvider(api_key=api_key, voice=voice).synthesize(text)


__all__ = [
    # Constants
    "PROVIDER_MAX_TEXT_LENGTH",
    "ELEVENLABS_MODEL_MAX_TEXT_LENGTH",
    "FALLBACK_MAX_TEXT_LENGTH",
    # Resolvers
    "resolve_max_text_length",
    # Cascade entrypoints (NEW canonical)
    "transcribe_audio_file",
    "synthesize",
    "voice_capabilities",
    # Legacy back-compat
    "transcribe",
    "synthesize_edge",
    "synthesize_openai",
    "build_voice_provider_set",
    "VoiceProviderSet",
    # Provider classes (existing)
    "OpenAIWhisperProvider",
    "EdgeTtsProvider",
    "OpenAITtsProvider",
    "SpeechToTextProvider",
    "TextToSpeechProvider",
]
