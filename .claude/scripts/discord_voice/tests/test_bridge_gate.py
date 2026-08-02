"""Delay-line noise-gate tests for the mic pump (bridge._pump_mic).

The pump never drops a timeline slot: gated chunks go out as true zeros,
speech onsets flush an 8-chunk pre-roll ring, and a 30-chunk hangover keeps
the gate open across mid-sentence pauses. VoiceBridge is constructed via
``__new__`` so no discord.Client or network is needed — the pump only
touches ``_mic_queue``, ``session`` and ``_mic_sent``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import bridge

SILENCE48 = b"\x00" * 3840   # 20ms of 48kHz stereo digital silence
LOW48 = b"\x01\x01" * 1920   # -42 dBFS — below the gate, nonzero on the wire
LOUD48 = b"\x00\x40" * 1920  # -6 dBFS — speech level

ZEROS24 = b"\x00" * 960
LOW24 = b"\x01\x01" * 480
LOUD24 = b"\x00\x40" * 480


def _drive(
    chunks: list[tuple[bytes, int]],
    monkeypatch: pytest.MonkeyPatch,
    *,
    idle_ms: int = 60,
) -> list[bytes]:
    monkeypatch.delenv("DISCORD_VOICE_DEBUG_PCM", raising=False)
    monkeypatch.setenv("DISCORD_VOICE_SILENCE_DBFS", "-35")

    async def run() -> list[bytes]:
        vb = bridge.VoiceBridge.__new__(bridge.VoiceBridge)
        vb._mic_queue = asyncio.Queue(maxsize=200)
        vb._mic_sent = 0
        sent: list[bytes] = []

        async def send_audio(chunk: bytes) -> None:
            sent.append(chunk)

        vb.session = SimpleNamespace(send_audio=send_audio)
        task = asyncio.create_task(vb._pump_mic())
        await asyncio.sleep(0)
        for item in chunks:
            vb._mic_queue.put_nowait(item)
        for _ in range(300):
            if vb._mic_queue.empty():
                break
            await asyncio.sleep(0.01)
        # The pump is PACED: it keeps emitting zeros at realtime rate after
        # the queue drains. Give it a beat, then cancel — callers assert on
        # send prefixes, trailing paced zeros are expected.
        await asyncio.sleep(idle_ms / 1000)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return sent

    return asyncio.run(run())


def test_silence_sends_zeros_and_never_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _drive([(SILENCE48, 7)] * 10, monkeypatch)

    # 10 chunks in, 7 held by the delay line -> 3 sends before pacing, all
    # true zeros; pacing only ever adds more zeros.
    assert sent[:3] == [ZEROS24] * 3
    assert set(sent) == {ZEROS24}


def test_onset_flushes_preroll_as_real_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _drive([(LOW48, 7)] * 8 + [(LOUD48, 7)] * 5, monkeypatch)

    # 13 in - 7 in the line = 6 sends. The first is the pre-speech zeros
    # slot; the next five are pre-roll chunks that arrived BELOW threshold
    # but exit as real audio the moment speech opens the gate.
    assert len(sent) >= 6
    assert sent[:6] == [ZEROS24] + [LOW24] * 5


def test_hangover_keeps_gate_open_then_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _drive([(LOUD48, 7)] * 3 + [(LOW48, 7)] * 40, monkeypatch)

    # 43 in - 7 in the line = 36 sends: 3 speech + 30 hangover as real
    # audio, then the gate shuts and everything after is zeros.
    assert len(sent) >= 36
    assert sent[:3] == [LOUD24] * 3
    assert sent[3:33] == [LOW24] * 30
    assert sent[33:] == [ZEROS24] * (len(sent) - 33)


def test_ssrc_change_resets_delay_line(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [(LOUD48, 7)] * 10 + [(LOUD48, 8)] * 10
    sent = _drive(chunks, monkeypatch)

    # 3 sends per speaker; the SSRC change cleared the line (7 chunks
    # discarded) instead of interleaving two streams through one resampler.
    assert len(sent) >= 6
    assert sent[:6] == [LOUD24] * 6


def test_starved_queue_paces_zeros_at_realtime_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    # Nothing fed for 400ms: a purely event-driven pump sends nothing, which
    # is exactly what glued owner's sentences together live (server VAD
    # measures silence in audio time). The paced pump must emit zeros.
    sent = _drive([], monkeypatch, idle_ms=400)

    assert len(sent) >= 5
    assert set(sent) == {ZEROS24}
