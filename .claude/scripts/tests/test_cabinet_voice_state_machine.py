"""Cabinet voice state-machine regression tests."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from cabinet.voice.voice_pipeline import (
    AudioRawFrame,
    FrameDirection,
    HomieSTT,
    HomieTTS,
    TranscriptionFrame,
    TTSUpdateSettingsFrame,
    UserStoppedSpeakingFrame,
)


@pytest.mark.asyncio
async def test_homie_tts_rejects_voice_only_settings_frame() -> None:
    tts = HomieTTS()
    await tts.process_frame(
        TTSUpdateSettingsFrame(settings={"provider": "edge", "voice": "voice_A"}),
        FrameDirection.DOWNSTREAM,
    )

    await tts.process_frame(
        TTSUpdateSettingsFrame(settings={"voice": "voice_B"}),
        FrameDirection.DOWNSTREAM,
    )

    assert tts._current_tts_settings == ("edge", "voice_A")


@pytest.mark.asyncio
async def test_homie_tts_rejects_provider_only_settings_frame() -> None:
    tts = HomieTTS()
    await tts.process_frame(
        TTSUpdateSettingsFrame(settings={"provider": "edge", "voice": "voice_A"}),
        FrameDirection.DOWNSTREAM,
    )

    await tts.process_frame(
        TTSUpdateSettingsFrame(settings={"provider": "elevenlabs"}),
        FrameDirection.DOWNSTREAM,
    )

    assert tts._current_tts_settings == ("edge", "voice_A")


@pytest.mark.asyncio
async def test_homie_tts_accepts_atomic_provider_voice_update() -> None:
    tts = HomieTTS()

    await tts.process_frame(
        TTSUpdateSettingsFrame(settings={"provider": "edge", "voice": "voice_A"}),
        FrameDirection.DOWNSTREAM,
    )
    await tts.process_frame(
        TTSUpdateSettingsFrame(settings={"provider": "elevenlabs", "voice": "voice_B"}),
        FrameDirection.DOWNSTREAM,
    )

    assert tts._current_tts_settings == ("elevenlabs", "voice_B")


async def _capture_stt_flush(monkeypatch: pytest.MonkeyPatch, text: str):
    seen_paths: list[str] = []

    async def fake_transcribe_audio_file(path: str) -> str:
        seen_paths.append(path)
        return text

    monkeypatch.setitem(
        sys.modules,
        "voice",
        SimpleNamespace(transcribe_audio_file=fake_transcribe_audio_file),
    )

    stt = HomieSTT()
    pushed: list[object] = []

    async def fake_push(frame, direction=None):
        pushed.append(frame)

    stt.push_frame = fake_push
    return stt, pushed, seen_paths


@pytest.mark.asyncio
async def test_homie_stt_flushes_on_user_stopped_speaking_frame(monkeypatch) -> None:
    stt, pushed, seen_paths = await _capture_stt_flush(monkeypatch, "hello owner")

    await stt.process_frame(
        AudioRawFrame(audio=b"\x00\x10" * 6000, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    assert pushed == []

    await stt.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    transcripts = [frame for frame in pushed if isinstance(frame, TranscriptionFrame)]
    assert [frame.text for frame in transcripts] == ["hello owner"]
    assert len(seen_paths) == 1


@pytest.mark.asyncio
async def test_homie_stt_flushes_on_byte_count_safety_net(monkeypatch) -> None:
    monkeypatch.setenv("CABINET_STT_MAX_UTTERANCE_SECS", "1")
    stt, pushed, seen_paths = await _capture_stt_flush(monkeypatch, "long utterance")

    await stt.process_frame(
        AudioRawFrame(audio=b"\x00\x10" * 17000, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )

    transcripts = [frame for frame in pushed if isinstance(frame, TranscriptionFrame)]
    assert [frame.text for frame in transcripts] == ["long utterance"]
    assert len(seen_paths) == 1


@pytest.mark.asyncio
async def test_homie_stt_rejects_odd_length_pcm_frames(monkeypatch, caplog) -> None:
    stt, pushed, seen_paths = await _capture_stt_flush(monkeypatch, "should not transcribe")
    caplog.set_level("WARNING", logger="cabinet.voice.pipeline")

    await stt.process_frame(
        AudioRawFrame(audio=b"\x00\x10\x7f", sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await stt.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    assert stt._buffer == bytearray()
    assert pushed == []
    assert seen_paths == []
    assert "stt_ignored reason=odd_pcm_bytes bytes=3 sample_rate=16000" in caplog.text
