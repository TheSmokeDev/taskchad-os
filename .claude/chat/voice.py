"""Voice provider interfaces and helpers for chat adapters."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Protocol


class SpeechToTextProvider(Protocol):
    """Interface for speech-to-text providers."""

    async def transcribe(self, audio_bytes: bytes) -> str:
        """Return the transcript for the provided audio bytes."""


class TextToSpeechProvider(Protocol):
    """Interface for text-to-speech providers."""

    async def synthesize(self, text: str) -> bytes:
        """Return encoded speech audio bytes for the provided text."""


@dataclass(slots=True)
class OpenAIWhisperProvider:
    """OpenAI Whisper-based STT provider."""

    api_key: str
    model: str = "whisper-1"

    async def transcribe(self, audio_bytes: bytes) -> str:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=self.api_key)
        buf = BytesIO(audio_bytes)
        buf.name = "audio.ogg"  # Whisper accepts OGG natively.
        resp = await client.audio.transcriptions.create(model=self.model, file=buf)
        return resp.text


@dataclass(slots=True)
class EdgeTtsProvider:
    """Edge TTS provider with no API key requirement."""

    voice: str = "en-US-GuyNeural"

    async def synthesize(self, text: str) -> bytes:
        import edge_tts

        communicate = edge_tts.Communicate(text, self.voice)
        buf = BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        return buf.getvalue()


@dataclass(slots=True)
class OpenAITtsProvider:
    """OpenAI TTS provider."""

    api_key: str
    voice: str = "alloy"

    async def synthesize(self, text: str) -> bytes:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=self.api_key)
        resp = await client.audio.speech.create(
            model="tts-1",
            voice=self.voice,
            input=text,
            response_format="opus",
        )
        return resp.content


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
    """Resolve independent STT and TTS providers from config."""

    stt = OpenAIWhisperProvider(openai_api_key, stt_model) if openai_api_key else None
    if tts_engine == "openai" and openai_api_key:
        tts: TextToSpeechProvider = OpenAITtsProvider(openai_api_key, tts_voice_openai)
    else:
        tts = EdgeTtsProvider(tts_voice_edge)
    return VoiceProviderSet(stt=stt, tts=tts)


async def transcribe(audio_bytes: bytes, api_key: str, model: str = "whisper-1") -> str:
    """Backward-compatible helper for direct STT calls."""

    return await OpenAIWhisperProvider(api_key=api_key, model=model).transcribe(audio_bytes)


async def synthesize_edge(text: str, voice: str = "en-US-GuyNeural") -> bytes:
    """Backward-compatible helper for direct Edge TTS calls."""

    return await EdgeTtsProvider(voice=voice).synthesize(text)


async def synthesize_openai(text: str, api_key: str, voice: str = "alloy") -> bytes:
    """Backward-compatible helper for direct OpenAI TTS calls."""

    return await OpenAITtsProvider(api_key=api_key, voice=voice).synthesize(text)
