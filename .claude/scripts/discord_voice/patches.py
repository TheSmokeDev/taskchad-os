"""py-cord 2.8.0 receive-path fixes (upstream: Pycord-Development/pycord#3139).

py-cord 2.8's voice receive rewrite shipped half-landed. Three localized
bugs break realtime receive under DAVE; this module replaces exactly those
spots at sidecar boot (no site-packages edits, no fork):

1. ``PacketRouter.get_decoder`` now returns our ``RealtimeDecoder`` — a
   lean arrival-order decoder for realtime use. The stock ``PacketDecoder``
   opus-decodes the still-DAVE-encrypted frame and then calls
   ``dave.decrypt()`` on the resulting PCM, and only when
   ``can_passthrough(user_id)`` is set (passthrough means "unencrypted
   allowed", so decrypt is skipped exactly when DAVE enforcement requires
   it). Protocol order is: dave.decrypt the encrypted frame FIRST, then
   opus-decode. (py-cord's reader already runs DAVE in the right order and
   substitutes OPUS_SILENCE on failure — our decoder must NOT decrypt again;
   double-decrypt turns speech into noise.)
2. Our decoder resolves users via ``VoiceClient._ssrc_to_id`` (the map
   py-cord actually maintains) instead of relying on the stock path.
3. The sink API surface the 2.8 router expects
   (``__sink_listeners__`` / ``walk_children`` / ``root`` / ``is_opus``)
   lives in ``sinks.RealtimeSink`` — see that module.
4. ``PacketDecryptor.decrypt_rtp`` gains the RFC 3550 s5.1 padding strip.
   py-cord parses ``packet.padding`` (rtp.py:100) but never consumes it, so
   padded packets carry N trailing garbage bytes into ``dave.decrypt()``,
   which throws and gets substituted with OPUS_SILENCE — exactly the
   "channel appears deaf, turns come back empty" profile (same bug class as
   discord.js c486fb8 / hermes-agent#11272). Every stage is counted in
   ``DAVE_STATS`` so the bridge /status shows the failure rate live.
5. Opus silence frames (``f8fffe``) bypass DAVE entirely. Discord sends them
   UNENCRYPTED even inside a DAVE call — libdave preserves them verbatim —
   so ``dave.decrypt()`` rejects every one with
   ``UnencryptedWhenPassthroughDisabled``. py-cord then substitutes the same
   OPUS_SILENCE the frame already was, which is harmless in isolation but
   erases the distinction between "speaker paused" and "we lost a packet".
   Those want opposite concealment: a pause is true zeros, a loss is opus
   PLC (``decode(None)``). ``pop_data`` now picks by ``packet.realtime_lost``.

Delete this file when an upstream release lands the official fix.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any

import davey
from discord.opus import Decoder
from discord.voice import VoiceData
from discord.voice.packets.core import OPUS_SILENCE

_log = logging.getLogger(__name__)

SILENCE_PCM_20MS = b"\x00" * 3840  # 20ms of 48kHz stereo s16
RTP_OPUS_PAYLOAD_TYPE = 0x78  # discord.js util/constants.ts

# Live receive-path counters, surfaced via the bridge /status endpoint.
DAVE_STATS: dict[str, int] = {
    "transport": 0,    # transport-layer decryptions performed
    "silence": 0,      # unencrypted Opus silence frames passed through
    "padded": 0,       # packets with the RTP padding bit set
    "pad_all": 0,      # pad count >= payload len: entirely padding, no audio
    "pad_bad": 0,      # padding byte 0 (RFC-invalid; left for DAVE to decide)
    "non_opus": 0,     # RTP payload type != 0x78 (observe-only for now)
    "dave_ok": 0,      # DAVE decryptions that succeeded
    "dave_fail": 0,    # DAVE decryptions that threw -> concealed
    "dave_unencrypted": 0,  # subset of dave_fail: frame carried no DAVE framing
    "concealed": 0,    # 20ms slots filled by opus PLC
}


class RealtimeDecoder:
    """Arrival-order RTP -> PCM decoder honoring the DAVE protocol order.

    Interface matches what ``PacketRouter`` uses: ``push_packet``,
    ``pop_data``, ``set_user_id``, ``reset``, ``destroy``.
    """

    def __init__(self, router: Any, ssrc: int) -> None:
        self.router = router
        self.ssrc = ssrc
        self._decoder: Decoder | None = Decoder()
        self._queue: deque = deque()
        self._user_id: int | None = None

    @property
    def sink(self):
        return self.router.sink

    def set_user_id(self, user_id: int) -> None:
        self._user_id = user_id

    def push_packet(self, packet) -> None:
        self._queue.append(packet)
        self.router.waiter.register(self)

    def pop_data(self, *, timeout: float = 0) -> VoiceData | None:
        if not self._queue:
            self.router.waiter.unregister(self)
            return None
        packet = self._queue.popleft()
        if not self._queue:
            self.router.waiter.unregister(self)

        frame = packet.decrypted_data
        if not frame or len(frame) < 10:
            # Two causes land here and they want OPPOSITE concealment:
            #   * a genuine Opus silence frame (f8fffe) is Discord's
            #     end-of-talk-spurt marker. It MEANS silence, and the voice
            #     docs warn that treating it as audio causes "unintended Opus
            #     interpolation" with the next transmission -> true zeros.
            #   * a rejected/lost packet is a GAP in otherwise continuous
            #     speech. Opus PLC reconstructs it and keeps decoder state
            #     coherent; a hard zero splice does neither.
            # Never DROP the slot either way: a drop excises 20ms and
            # butt-splices the remainder at non-zero crossings, which is
            # precisely "healthy RMS, unintelligible speech" on the model side.
            pcm = self._conceal() if getattr(packet, "realtime_lost", False) else SILENCE_PCM_20MS
        else:
            pcm = self._decode(frame) or self._conceal()
        member = self._resolve_member()
        return VoiceData(packet, member, pcm=pcm)

    def _conceal(self) -> bytes:
        """Opus packet-loss concealment for one missing 20ms frame."""

        if self._decoder is None:
            return SILENCE_PCM_20MS
        try:
            pcm = self._decoder.decode(None, fec=False)
        except Exception as exc:  # noqa: BLE001 — PLC is best-effort
            _log.debug("PLC failed for ssrc %s: %s", self.ssrc, exc)
            return SILENCE_PCM_20MS
        if not pcm:
            return SILENCE_PCM_20MS
        DAVE_STATS["concealed"] += 1
        return pcm

    def reset(self) -> None:
        self._queue.clear()
        self._decoder = Decoder()
        self.router.waiter.unregister(self)

    def destroy(self) -> None:
        self._queue.clear()
        self._decoder = None
        self.router.waiter.unregister(self)

    def _resolve_member(self):
        vc = self.sink.client
        if vc is None:
            return None
        user_id = self._user_id or vc._ssrc_to_id.get(self.ssrc)
        if user_id is None:
            return None
        return vc.guild.get_member(user_id) or vc.client.get_user(user_id)

    def _decode(self, frame: bytes) -> bytes:
        """Opus-decode one frame.

        DAVE decrypt is owned by py-cord's reader (reader.decrypt_rtp runs
        it BEFORE us, in the correct order, and substitutes OPUS_SILENCE on
        failures). Decrypting again here would double-decrypt into garbage —
        that was the noise the model heard.
        """

        assert self._decoder is not None
        try:
            pcm = self._decoder.decode(frame, fec=False)
            _log.debug("opus ok ssrc=%s pcm_len=%d", self.ssrc, len(pcm))
            return pcm
        except Exception as exc:  # noqa: BLE001 — corrupt opus frame
            _log.debug("opus decode failed for ssrc %s: %s", self.ssrc, exc)
            return b""


def _patched_get_decoder(self, ssrc: int) -> RealtimeDecoder:
    with self._lock:
        decoder = self.decoders.get(ssrc)
        if decoder is None:
            decoder = self.decoders[ssrc] = RealtimeDecoder(self, ssrc)
        return decoder


def _patched_decrypt_rtp(self, packet):
    """``PacketDecryptor.decrypt_rtp`` + RFC 3550 s5.1 padding strip.

    Mirrors py-cord 2.8's reader.py implementation exactly (transport
    decrypt -> DAVE decrypt -> OPUS_SILENCE on failure), with two changes:
    padding bytes are stripped between transport and DAVE, and every stage
    is counted in ``DAVE_STATS``.
    """

    state = self.client._connection
    dave = state.dave_session

    raw_payload = self._decryptor_rtp(packet)
    DAVE_STATS["transport"] += 1
    packet.realtime_lost = False

    if getattr(packet, "payload", RTP_OPUS_PAYLOAD_TYPE) != RTP_OPUS_PAYLOAD_TYPE:
        # discord.js#11449 drops these outright. Count first, enforce only if
        # a live run proves they occur — a wrong guard here goes deaf.
        DAVE_STATS["non_opus"] += 1

    # Discord sends Opus silence frames (f8fffe) UNENCRYPTED even inside a
    # DAVE call: libdave preserves them verbatim and skips decryption, and
    # the voice docs describe them as the end-of-transmission marker. Handing
    # one to dave.decrypt() raises UnencryptedWhenPassthroughDisabled, and the
    # reader then substitutes the very OPUS_SILENCE the frame already was — a
    # no-op that costs a traceback per frame AND makes a genuine crypto
    # failure indistinguishable from ordinary silence downstream.
    if raw_payload[:3] == OPUS_SILENCE:
        DAVE_STATS["silence"] += 1
        packet.decrypted_data = OPUS_SILENCE
        return packet.decrypted_data

    if getattr(packet, "padding", False) and raw_payload:
        DAVE_STATS["padded"] += 1
        pad = raw_payload[-1]
        if 0 < pad < len(raw_payload):
            raw_payload = raw_payload[:-pad]
        elif pad == len(raw_payload):
            # pad == len means the frame is EXACTLY padding — every byte is
            # padding, there is no audio to extract. Discord's connection/probe
            # burst sends these UNENCRYPTED as 255 bytes of 0xFF (pad byte
            # 0xFF == len 255). Handing one to dave.decrypt() only raises
            # UnencryptedWhenPassthroughDisabled and spams a traceback per
            # frame (52 in one live session, 2026-08-02, which the old
            # "should be unreachable" guard below could not explain), so
            # short-circuit to genuine silence exactly like the f8fffe frame
            # above — realtime_lost stays False: this IS silence, not a loss.
            DAVE_STATS["pad_all"] += 1
            packet.decrypted_data = OPUS_SILENCE
            return packet.decrypted_data
        elif pad > len(raw_payload):
            # pad > len is an over-count — it violates RFC 3550 and is NOT
            # genuine end-of-transmission silence. Do not hand it to DAVE (it
            # would raise) and do not mislabel it as silence: mark the packet
            # lost so the decoder conceals it (PLC), the same shape as the
            # dave_fail path below, instead of hard-zeroing a real audio slot.
            DAVE_STATS["pad_bad"] += 1
            packet.realtime_lost = True
            packet.decrypted_data = OPUS_SILENCE
            return packet.decrypted_data
        else:
            # pad == 0 violates RFC 3550 (the count includes itself) but strips
            # NOTHING — the full payload may be intact audio carrying a spurious
            # padding bit. Leave it as-is and let DAVE try; dave_fail records a
            # genuine crypto failure. (Not short-circuited: unlike pad > len,
            # this can still be real audio.)
            DAVE_STATS["pad_bad"] += 1

    if dave is not None and dave.ready:
        uid = state.ssrc_user_map.get(packet.ssrc)
        if uid:
            try:
                decrypted_audio = dave.decrypt(
                    uid, davey.MediaType.audio, raw_payload
                )

                if packet.extended:
                    offset = packet.update_extended_header(decrypted_audio)
                    packet.decrypted_data = decrypted_audio[offset:]
                else:
                    packet.decrypted_data = decrypted_audio
                DAVE_STATS["dave_ok"] += 1
            except Exception as exc:
                DAVE_STATS["dave_fail"] += 1
                if "UnencryptedWhenPassthroughDisabled" in str(exc):
                    # The 2026-08-02 live run identified these as all-padding
                    # probe frames (pad byte 0xFF == len 255), now
                    # short-circuited by the pad_all guard above. If this
                    # STILL fires, it is a genuinely different unencrypted
                    # shape — log it periodically so the next run can name it.
                    DAVE_STATS["dave_unencrypted"] += 1
                    if DAVE_STATS["dave_unencrypted"] % 25 == 1:
                        _log.warning(
                            "DAVE rejected an unencrypted frame: len=%d head=%s "
                            "tail=%s padding=%s payload_type=%s",
                            len(raw_payload),
                            raw_payload[:4].hex(),
                            raw_payload[-4:].hex(),
                            getattr(packet, "padding", None),
                            getattr(packet, "payload", None),
                        )
                packet.realtime_lost = True
                _log.debug(
                    "Ignoring exception while decoding DAVE packet", exc_info=exc
                )
                packet.decrypted_data = OPUS_SILENCE

    return packet.decrypted_data


def apply_patches() -> None:
    """Install the receive-path fixes. Idempotent."""

    from discord.voice.receive.reader import PacketDecryptor
    from discord.voice.receive.router import PacketRouter

    PacketRouter.get_decoder = _patched_get_decoder
    PacketDecryptor.decrypt_rtp = _patched_decrypt_rtp
    _log.info("py-cord receive patches applied (RealtimeDecoder + RTP padding strip)")


__all__ = ["DAVE_STATS", "RealtimeDecoder", "SILENCE_PCM_20MS", "apply_patches"]
