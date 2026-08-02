"""Realtime receive sink for the Discord voice sidecar.

py-cord 2.8's receive machinery expects a composite-sink API surface that
the shipped ``Sink`` base never got (half-landed rewrite — see
``patches.py``). ``RealtimeSink`` is a leaf sink carrying that surface and
forwarding decoded 48kHz stereo PCM into a caller-provided callback as it
arrives (~20ms chunks), instead of buffering to files.
"""

from __future__ import annotations

from collections.abc import Callable

from discord.sinks.core import Sink


class RealtimeSink(Sink):
    """Streams decoded PCM to ``on_pcm(pcm48k_stereo, user_id | None, ssrc)``."""

    __sink_listeners__: list = []

    def __init__(self, on_pcm: Callable[[bytes, int | None, int | None], None]) -> None:
        super().__init__()
        self.root = self
        self._on_pcm = on_pcm
        self.packets = 0
        self.bytes = 0

    # -- py-cord 2.8 receive-machinery surface -----------------------------

    def is_opus(self) -> bool:
        """Decoder contract: False = decode opus to PCM before write()."""

        return False

    def walk_children(self, with_self: bool = False) -> list:
        return [self] if with_self else []

    # -- packet flow --------------------------------------------------------

    def write(self, data, user) -> None:
        pcm = getattr(data, "pcm", None)
        if not pcm:
            return
        user_id = getattr(user, "id", None)
        if user_id is None and isinstance(user, int):
            user_id = user
        ssrc = getattr(getattr(data, "packet", None), "ssrc", None)
        self.packets += 1
        self.bytes += len(pcm)
        self._on_pcm(pcm, user_id, ssrc)


__all__ = ["RealtimeSink"]
