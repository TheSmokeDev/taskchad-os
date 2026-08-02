"""PCM helper tests — resample sizes, queue-source frames, levels."""

from __future__ import annotations

import audio

FRAME = b"\x01\x02" * 1920  # 3840 bytes = 20ms of 48kHz stereo s16


def test_pcm48_to_24_size() -> None:
    out, _state = audio.pcm48stereo_to_24mono(FRAME)
    assert len(out) == 960  # 20ms of 24kHz mono s16


def test_pcm24_to_48_size() -> None:
    # audioop.ratecv primes on the first (state=None) call — one stereo frame
    # short — then produces exactly one Discord frame per 20ms input chunk.
    chunk = b"\x03\x04" * 480
    out, state = audio.pcm24mono_to_48stereo(chunk)
    assert len(out) == 3836
    out, state = audio.pcm24mono_to_48stereo(chunk, state)
    assert len(out) == 3840


def test_resample_roundtrip_sizes() -> None:
    down, down_state = audio.pcm48stereo_to_24mono(FRAME)
    up, _up_state = audio.pcm24mono_to_48stereo(down)
    assert len(down) == 960
    assert len(up) >= len(FRAME) - 4  # ratecv priming drops at most one frame


def test_resample_state_chaining_keeps_sizes() -> None:
    state = None
    total = 0
    for _ in range(3):
        out, state = audio.pcm48stereo_to_24mono(FRAME, state)
        total += len(out)
    assert total == 3 * 960


def test_queue_source_empty_read_is_silence_frame() -> None:
    src = audio.QueueAudioSource()
    frame = src.read()
    assert len(frame) == audio.DISCORD_FRAME_BYTES == 3840
    assert frame == b"\0" * audio.DISCORD_FRAME_BYTES
    assert src.is_opus() is False


def test_queue_source_splits_large_push_into_frames() -> None:
    src = audio.QueueAudioSource()
    src.push(b"\x01" * 3840 * 2)
    assert src.read() == b"\x01" * 3840
    assert src.read() == b"\x01" * 3840
    assert src.read() == b"\0" * 3840


def test_queue_source_pads_partial_frame() -> None:
    src = audio.QueueAudioSource()
    src.push(b"\x02" * 1000)
    frame = src.read()
    assert len(frame) == 3840
    assert frame[:1000] == b"\x02" * 1000
    assert frame[1000:] == b"\0" * 2840


def test_queue_source_carries_remainder_across_reads() -> None:
    src = audio.QueueAudioSource()
    src.push(b"\x04" * 5000)
    assert src.read() == b"\x04" * 3840
    frame = src.read()
    assert frame[:1160] == b"\x04" * 1160
    assert frame[1160:] == b"\0" * (3840 - 1160)


def test_queue_source_flush_drops_queued_audio() -> None:
    src = audio.QueueAudioSource()
    src.push(b"\x03" * 5000)
    src.flush()
    assert src.read() == b"\0" * 3840


def test_rms_dbfs_silence_floor() -> None:
    assert audio.rms_dbfs(b"\0" * 960) == -96.0


def test_rms_dbfs_full_scale_near_zero() -> None:
    loud = b"\xff\x7f" * 480  # max positive s16 samples
    assert audio.rms_dbfs(loud) > -1.0


def test_rms_dbfs_quieter_signal_reads_lower() -> None:
    loud = audio.rms_dbfs(b"\xff\x7f" * 480)
    quiet = audio.rms_dbfs(b"\x00\x10" * 480)  # amplitude 4096
    assert quiet < loud
