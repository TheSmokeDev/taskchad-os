"""PCM audio helpers for the Discord voice sidecar.

Discord voice is 16-bit 48kHz stereo PCM; OpenAI Realtime is 16-bit 24kHz
mono PCM. All conversion lives here, stateful where continuity matters.
"""

from __future__ import annotations

import audioop
import collections
import threading

import discord

DISCORD_FRAME_BYTES = 3840  # 20ms of 48kHz stereo s16 — one VoiceClient frame


def pcm48stereo_to_24mono(data: bytes, state: list | None = None) -> tuple[bytes, object]:
    """48kHz stereo s16 -> 24kHz mono s16. Pass the returned state back in."""

    mono = audioop.tomono(data, 2, 0.5, 0.5)
    out, new_state = audioop.ratecv(mono, 2, 1, 48000, 24000, state)
    return out, new_state


def pcm24mono_to_48stereo(data: bytes, state: list | None = None) -> tuple[bytes, object]:
    """24kHz mono s16 -> 48kHz stereo s16. Pass the returned state back in."""

    up, new_state = audioop.ratecv(data, 2, 1, 24000, 48000, state)
    return audioop.tostereo(up, 2, 1.0, 1.0), new_state


def rms_dbfs(pcm: bytes) -> float:
    """dBFS level of an s16 PCM chunk; -inf-ish floor at -96 for silence."""

    import math

    rms = audioop.rms(pcm, 2)
    if rms <= 0:
        return -96.0
    return round(20 * math.log10(rms / 32768.0), 1)


class QueueAudioSource(discord.AudioSource):
    """Continuous playback source fed by a thread-safe PCM queue.

    ``read()`` always returns exactly one 20ms Discord frame — silence when
    the queue is empty — so playback spans the whole voice session instead
    of ending at the first gap. ``flush()`` is the barge-in brake.
    """

    def __init__(self) -> None:
        self._queue: collections.deque[bytes] = collections.deque()
        self._carry = bytearray()
        self._lock = threading.Lock()
        self.reads_served = 0

    def push(self, pcm48stereo: bytes) -> None:
        with self._lock:
            self._queue.append(pcm48stereo)

    def flush(self) -> None:
        with self._lock:
            self._queue.clear()
            self._carry.clear()

    def drain_pending(self) -> bytes:
        """Atomically remove and return all queued + partial PCM.

        Used by the DAVE re-key heal to transplant pending assistant audio
        into a FRESH source before the old transport stops — py-cord's
        AudioPlayer calls ``cleanup()`` (== ``flush()``) when ``stop()`` ends
        its thread, so reusing the same source across a transport flap would
        silently discard everything still queued.
        """
        with self._lock:
            pending = bytes(self._carry) + b"".join(self._queue)
            self._queue.clear()
            self._carry.clear()
            return pending

    def read(self) -> bytes:
        with self._lock:
            while len(self._carry) < DISCORD_FRAME_BYTES and self._queue:
                self._carry += self._queue.popleft()
            if len(self._carry) >= DISCORD_FRAME_BYTES:
                frame = bytes(self._carry[:DISCORD_FRAME_BYTES])
                del self._carry[:DISCORD_FRAME_BYTES]
                self.reads_served += 1
                return frame
            if self._carry:
                frame = bytes(self._carry).ljust(DISCORD_FRAME_BYTES, b"\0")
                self._carry.clear()
                self.reads_served += 1
                return frame
        self.reads_served += 1
        return b"\0" * DISCORD_FRAME_BYTES

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        self.flush()


__all__ = [
    "DISCORD_FRAME_BYTES",
    "QueueAudioSource",
    "pcm24mono_to_48stereo",
    "pcm48stereo_to_24mono",
    "rms_dbfs",
]
