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
    lift_cap: bool = True,
) -> list[bytes]:
    monkeypatch.delenv("DISCORD_VOICE_DEBUG_PCM", raising=False)
    monkeypatch.setenv("DISCORD_VOICE_SILENCE_DBFS", "-35")
    # Most tests here validate the delay-line noise gate, not the jitter
    # buffer's latency ceiling. They bulk-preload the whole clip at once
    # (unrealistic — real audio arrives one 20ms frame at a time), which would
    # trip the drop-oldest overflow cap and discard the onset. Lift the cap so
    # every fed frame reaches the gate. The overflow test passes lift_cap=False
    # to exercise the cap deliberately (it sets its own tight cap).
    if lift_cap:
        monkeypatch.setenv("DISCORD_VOICE_JITTER_MAX_FRAMES", "100000")
        monkeypatch.setenv("DISCORD_VOICE_JITTER_SOFT_FRAMES", "100000")

    # Drive the pump on a VIRTUAL clock (via the bridge._now/_sleep seam), not
    # real time. The old real-timed harness paced each frame with a real
    # asyncio.sleep(0.02) — ~1s of wall time per test plus timer churn — which
    # under CPU load pushed the realtime run-poller past its real-time deadline
    # between polls, flaking an unrelated test. Virtual time is instant and
    # deterministic, and the gate logic is time-INDEPENDENT so the emitted
    # sequence is identical.
    clock = _Clock()
    real_sleep = asyncio.sleep
    monkeypatch.setattr(bridge, "_now", clock.monotonic)
    monkeypatch.setattr(bridge, "_sleep", _make_fake_sleep(clock, real_sleep))

    async def run() -> list[bytes]:
        vb = bridge.VoiceBridge.__new__(bridge.VoiceBridge)
        vb._mic_queue = asyncio.Queue(maxsize=200)
        vb._mic_sent = 0
        vb._mic_drops = 0
        sent: list[bytes] = []

        async def send_audio(chunk: bytes, speaker: dict | None = None) -> None:
            sent.append(chunk)

        vb.session = SimpleNamespace(send_audio=send_audio)
        task = asyncio.create_task(vb._pump_mic())
        await real_sleep(0)
        for item in chunks:
            # The real queue carries (pcm, ssrc, speaker_id) — these gate tests
            # are about audio timing, so the speaker is None. Appended at the
            # driver boundary so every case below keeps its 2-tuple data.
            vb._mic_queue.put_nowait((item[0], item[1], None))
        # The paced jitter buffer drains the whole queue into its input buffer
        # on the first iteration, so "queue empty" no longer means "done
        # emitting" — the pump paces each buffered frame out one per virtual
        # 20ms. Wait for EMISSION (every input frame plus a margin for the
        # delay-line flush + gate close plus idle_ms of trailing paced zeros),
        # not for the queue to drain. The pump advances the clock via its
        # pacing _sleep; the driver yields with the real sleep only.
        target = len(chunks) + 8 + max(0, idle_ms // 20)
        for _ in range(200_000):
            if vb._mic_sent >= target or (clock.t - 1000.0) >= 120.0:
                break
            await real_sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return sent

    return asyncio.run(run())


# ---------------------------------------------------------------------------
# Virtual-clock harness (2026-08-02 review, finding 4): the pump PACES on
# time.monotonic, so cadence, jitter absorption, underrun, and stall recovery
# can only be asserted deterministically by driving a controlled clock. Every
# pacing sleep advances the virtual clock by its delay; a scripted queue
# releases frames at scheduled virtual arrival times; send_audio records the
# virtual instant of each emit. No wall-clock timing, no flakiness.
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self, t0: float = 1000.0) -> None:
        self.t = t0

    def monotonic(self) -> float:
        return self.t


def _make_fake_sleep(clock: _Clock, real_sleep):
    async def fake_sleep(delay: float = 0.0, result=None):
        clock.t += max(0.0, float(delay))
        await real_sleep(0)  # yield so the pump and driver interleave
        return result

    return fake_sleep


class _ScriptedQueue:
    """Releases each frame once the virtual clock reaches its arrival time."""

    def __init__(self, clock: _Clock, schedule: list[tuple[float, tuple[bytes, int]]]) -> None:
        self._clock = clock
        self._items = sorted(schedule, key=lambda item: item[0])
        self._i = 0

    def get_nowait(self):
        if self._i < len(self._items) and self._items[self._i][0] <= self._clock.t - 1000.0:
            item = self._items[self._i][1]
            self._i += 1
            # Same (pcm, ssrc, speaker_id) contract as the real _mic_queue.
            return (item[0], item[1], None)
        raise asyncio.QueueEmpty

    def qsize(self) -> int:
        return len(self._items) - self._i


def _drive_virtual(
    schedule: list[tuple[float, tuple[bytes, int]]],
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_until: float,
    jitter_frames: int = 3,
    send_stall: tuple[int, float] | None = None,
):
    monkeypatch.delenv("DISCORD_VOICE_DEBUG_PCM", raising=False)
    monkeypatch.setenv("DISCORD_VOICE_SILENCE_DBFS", "-35")
    monkeypatch.setenv("DISCORD_VOICE_JITTER_FRAMES", str(jitter_frames))
    monkeypatch.setenv("DISCORD_VOICE_JITTER_MAX_FRAMES", str(jitter_frames + 25))

    clock = _Clock()
    real_sleep = asyncio.sleep
    # Patch the pump's INJECTABLE seam (bridge._now / bridge._sleep), NOT the
    # global time.monotonic / asyncio.sleep — patching the globals warps
    # asyncio's own loop timekeeping and leaks scheduling flake into unrelated
    # tests (the realtime run-poller's time.monotonic deadline).
    monkeypatch.setattr(bridge, "_now", clock.monotonic)
    monkeypatch.setattr(bridge, "_sleep", _make_fake_sleep(clock, real_sleep))

    async def run():
        vb = bridge.VoiceBridge.__new__(bridge.VoiceBridge)
        vb._mic_queue = _ScriptedQueue(clock, schedule)
        vb._mic_sent = 0
        vb._mic_drops = 0
        sends: list[tuple[float, bytes]] = []

        async def send_audio(chunk: bytes, speaker: dict | None = None) -> None:
            sends.append((clock.t - 1000.0, chunk))
            if send_stall is not None and len(sends) == send_stall[0]:
                clock.t += send_stall[1]  # a blocking send: wall time jumps

        vb.session = SimpleNamespace(send_audio=send_audio)
        task = asyncio.create_task(vb._pump_mic())
        # Only the pump's pacing sleep advances the clock; the driver yields
        # with the ORIGINAL sleep so it never moves virtual time itself.
        for _ in range(200_000):
            if clock.t - 1000.0 >= run_until:
                break
            await real_sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return sends, vb

    return asyncio.run(run())


def test_pacing_holds_realtime_rate_no_busy_spin(monkeypatch: pytest.MonkeyPatch) -> None:
    # A prime burst then steady 20ms arrivals. The pump must emit one frame per
    # 20ms of virtual time — bracketed on BOTH sides so a busy-spin (thousands
    # of frames) and a starved pump (near zero) each fail. Codex's original
    # concern: the old "at least five zeros" assertion let a busy-spin pass.
    schedule: list[tuple[float, tuple[bytes, int]]] = [(0.0, (LOUD48, 7))] * 12
    t = 0.02
    for _ in range(80):
        schedule.append((t, (LOUD48, 7)))
        t += 0.02
    sends, _ = _drive_virtual(schedule, monkeypatch, run_until=1.0)

    times = [ts for ts, _ in sends]
    assert len(times) >= 2
    intervals = [b - a for a, b in zip(times, times[1:])]
    assert all(abs(iv - 0.02) < 1e-6 for iv in intervals), intervals
    # ~1s / 20ms minus the 8-frame ring-fill ramp -> low-to-mid 40s.
    assert 35 <= len(times) <= 52, len(times)


def test_jittered_arrival_never_splices(monkeypatch: pytest.MonkeyPatch) -> None:
    # The actual bug, now deterministic: packets arriving late (25ms) then early
    # (15ms), averaging 20ms. The old bare-timeout pump zero-spliced on every
    # late frame; the primed jitter buffer must absorb the lateness so the
    # emitted speech region has NO interior zero splice.
    schedule: list[tuple[float, tuple[bytes, int]]] = [(0.0, (LOUD48, 7))] * 8
    jit = [0.025, 0.015, 0.024, 0.016, 0.026, 0.014]
    t = 0.02
    for k in range(60):
        schedule.append((t, (LOUD48, 7)))
        t += jit[k % len(jit)]
    sends, _ = _drive_virtual(schedule, monkeypatch, run_until=1.2)

    seq = [c for _, c in sends]
    reals = [i for i, c in enumerate(seq) if c == LOUD24]
    assert reals, "no audio emitted"
    interior = seq[min(reals): max(reals) + 1]
    assert ZEROS24 not in interior, "jitter caused a mid-word zero splice"


def test_underrun_emits_zeros_then_reprimes(monkeypatch: pytest.MonkeyPatch) -> None:
    # A burst of speech, a 360ms starvation gap (no arrivals), then more speech.
    # The pump must emit zeros through the gap (underrun handled cleanly, not
    # stale/garbage) and resume real audio after (re-prime).
    schedule: list[tuple[float, tuple[bytes, int]]] = [(0.0, (LOUD48, 7))] * 12
    t = 0.6  # 360ms of silence between the two speech bursts
    for _ in range(12):
        schedule.append((t, (LOUD48, 7)))
        t += 0.02
    sends, _ = _drive_virtual(schedule, monkeypatch, run_until=1.2)

    seq = [c for _, c in sends]
    assert LOUD24 in seq
    assert ZEROS24 in seq
    first_loud = min(i for i, c in enumerate(seq) if c == LOUD24)
    last_loud = max(i for i, c in enumerate(seq) if c == LOUD24)
    assert any(seq[i] == ZEROS24 for i in range(first_loud, last_loud)), "gap not zeroed"


def test_send_stall_sheds_backlog_and_keeps_running(monkeypatch: pytest.MonkeyPatch) -> None:
    # Steady speech; on the 15th send, send_audio blocks 250ms (clock jump).
    # The queue fills during the stall; the pump must SHED the accumulated
    # backlog (drops>0) so latency recovers instead of staying doubled for the
    # rest of the utterance, keep emitting afterward, and — r2 finding A — add
    # NO extra output hole during recovery: the ONLY large send interval is the
    # injected 250ms stall itself. (The r1 fix cleared the ring on a shed, whose
    # 8-frame lookahead refill added a further ~140ms empty gap; keeping the
    # ring removes it.)
    schedule: list[tuple[float, tuple[bytes, int]]] = [(0.0, (LOUD48, 7))] * 8
    t = 0.02
    for _ in range(90):
        schedule.append((t, (LOUD48, 7)))
        t += 0.02
    sends, vb = _drive_virtual(
        schedule, monkeypatch, run_until=1.8, send_stall=(15, 0.25)
    )

    assert vb._mic_drops > 0, "stall backlog was never shed"
    assert len(sends) > 15, "pump stopped emitting after the stall"
    times = [ts for ts, _ in sends]
    intervals = [b - a for a, b in zip(times, times[1:])]
    big = [iv for iv in intervals if iv > 0.10]
    assert len(big) == 1, f"expected only the injected stall, got {big}"
    assert big[0] <= 0.30, f"recovery added an extra hole beyond the 250ms stall: {big}"
    assert LOUD24 in [c for _, c in sends], "mid-speech continuation did not flow"


def test_soft_high_water_sheds_frozen_backlog_without_a_stall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # K3 design gate: a one-time arrival burst (WiFi jitter / producer
    # catch-up) that lands the buffer between the priming depth and the hard
    # ceiling would otherwise FREEZE there — arrival rate == consumption rate,
    # so the depth never drains and ~2x priming of latency sticks for the rest
    # of the utterance. With NO clock stall (no resync) and NO hard overflow
    # (10 < jbuf_max 28), the soft high-water (2 x jitter_frames = 6) must still
    # shed to priming on depth alone, then keep the speech flowing.
    schedule: list[tuple[float, tuple[bytes, int]]] = [(0.0, (LOUD48, 7))] * 10
    t = 0.02
    for _ in range(40):
        schedule.append((t, (LOUD48, 7)))
        t += 0.02
    sends, vb = _drive_virtual(schedule, monkeypatch, run_until=1.0, jitter_frames=3)

    assert vb._mic_drops > 0, "soft high-water never shed the frozen backlog"
    assert LOUD24 in [c for _, c in sends], "speech stopped flowing after the shed"


def test_send_stall_during_silence_stays_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    # r2 finding B: a stall while continuous BELOW-threshold ambient audio
    # streams. The shed must NOT force the gate open — that would blast ~600ms
    # of ambient noise into OpenAI's server VAD and glue/spuriously start turns.
    # Preserving speech_tail (closed here) keeps the ambient gated: only zeros
    # come out after recovery.
    schedule: list[tuple[float, tuple[bytes, int]]] = [(0.0, (LOW48, 7))] * 8
    t = 0.02
    for _ in range(90):
        schedule.append((t, (LOW48, 7)))
        t += 0.02
    sends, vb = _drive_virtual(
        schedule, monkeypatch, run_until=1.8, send_stall=(15, 0.25)
    )

    assert vb._mic_drops > 0, "stall backlog was never shed"
    assert {c for _, c in sends} == {ZEROS24}, "ambient leaked through the gate after a shed"


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


def test_overflow_drops_oldest_to_bound_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    # The jitter buffer's HARD latency ceiling: if the pump ever falls far
    # behind, it must drop the OLDEST buffered frames to stay current rather
    # than let latency grow unbounded. Here the bulk preload (3 LOUD onset, then
    # 40 LOW) far exceeds a tight 8-frame hard cap, so the drain-all on the
    # first iteration overflows and discards the oldest frames — the LOUD onset
    # — keeping only the newest. The gate therefore never sees a LOUD frame: no
    # LOUD24 is ever emitted. The soft high-water is disabled here so the HARD
    # ceiling is what fires (the soft mark has its own test above).
    monkeypatch.setenv("DISCORD_VOICE_JITTER_FRAMES", "2")
    monkeypatch.setenv("DISCORD_VOICE_JITTER_MAX_FRAMES", "8")
    monkeypatch.setenv("DISCORD_VOICE_JITTER_SOFT_FRAMES", "100000")
    sent = _drive([(LOUD48, 7)] * 3 + [(LOW48, 7)] * 40, monkeypatch, lift_cap=False)

    assert LOUD24 not in sent  # oldest (the onset) was dropped to bound latency
    assert set(sent) <= {ZEROS24, LOW24}
