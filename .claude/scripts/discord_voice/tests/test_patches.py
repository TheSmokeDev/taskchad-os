"""RealtimeDecoder + router/decryptor patch tests.

A stub ``davey`` module is injected into ``sys.modules`` BEFORE importing
patches (the sidecar venv ships the real one, but DaveSession must be a
recording stub). The stub proxies the real module's attributes — py-cord's
own import chain reads ``davey.DAVE_PROTOCOL_VERSION`` — and only swaps
``DaveSession`` / ``MediaType``. The opus ``Decoder`` is likewise
monkeypatched with a recording fake.

DAVE decrypt is owned by py-cord's reader (it runs BEFORE the decoder, in
the correct protocol order, substituting OPUS_SILENCE on failure). These
tests assert the decoder honors that boundary (no double-decrypt), keeps
the timeline true (silence frames out, never dropped slots), and that the
RTP padding strip feeds DAVE a clean payload.
"""

from __future__ import annotations

import sys
import threading
import types
from types import SimpleNamespace

import pytest

import davey as _real_davey


class _DaveSession:
    """Recording DAVE session; ``fail=True`` simulates an undecryptable frame."""

    def __init__(self, calls: list, *, fail: bool = False) -> None:
        self._calls = calls
        self._fail = fail

    def decrypt(self, user_id: int, media_type, frame: bytes) -> bytes:
        self._calls.append(("dave", user_id, frame))
        if self._fail:
            raise RuntimeError("undecryptable frame")
        return b"dec:" + frame


# Stub davey before patches imports it — real attributes proxied (py-cord's
# import machinery needs them), DaveSession/MediaType stubbed.
_davey_stub = types.ModuleType("davey")
_davey_stub.__dict__.update(vars(_real_davey))
_davey_stub.DaveSession = _DaveSession
_davey_stub.MediaType = SimpleNamespace(audio=_real_davey.MediaType.audio)
sys.modules["davey"] = _davey_stub
sys.modules.pop("patches", None)  # make the import below see our stub

import patches  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Waiter:
    def __init__(self) -> None:
        self.registered: list = []
        self.unregistered: list = []

    def register(self, decoder) -> None:
        self.registered.append(decoder)

    def unregister(self, decoder) -> None:
        self.unregistered.append(decoder)


def _voice_client(
    *,
    ssrc_map: dict[int, int],
    member,
    dave: _DaveSession | None,
    fallback_user=None,
) -> SimpleNamespace:
    """Fake the py-cord VoiceClient attributes the decoder touches."""
    return SimpleNamespace(
        _ssrc_to_id=ssrc_map,
        guild=SimpleNamespace(get_member=lambda uid: member if member and uid == member.id else None),
        client=SimpleNamespace(get_user=lambda uid: fallback_user),
        _connection=SimpleNamespace(dave_session=dave),
    )


def _router(vc) -> SimpleNamespace:
    return SimpleNamespace(sink=SimpleNamespace(client=vc), waiter=_Waiter())


@pytest.fixture
def decoder_calls(monkeypatch: pytest.MonkeyPatch) -> list:
    """Swap patches.Decoder for a recording fake; return the call log."""

    calls: list = []

    class FakeDecoder:
        def __init__(self) -> None:
            calls.append(("opus_init",))

        def decode(self, frame: bytes, fec: bool = False) -> bytes:
            calls.append(("opus", frame))
            return b"pcm:" + frame

    monkeypatch.setattr(patches, "Decoder", FakeDecoder)
    return calls


# ---------------------------------------------------------------------------
# RealtimeDecoder packet flow
# ---------------------------------------------------------------------------


def test_normal_frame_decodes_and_resolves_member(decoder_calls: list) -> None:
    member = SimpleNamespace(id=42, name="owner")
    dave = _DaveSession(decoder_calls)
    vc = _voice_client(ssrc_map={7: 42}, member=member, dave=dave)
    router = _router(vc)
    dec = patches.RealtimeDecoder(router, ssrc=7)

    frame = b"enc-frame-at-least-10-bytes"
    dec.push_packet(SimpleNamespace(decrypted_data=frame))
    data = dec.pop_data()

    assert data is not None
    assert data.pcm == b"pcm:" + frame
    assert data.source is member  # resolved via _ssrc_to_id -> guild member
    # DAVE is the reader's job — the decoder must never decrypt (double
    # decrypt turns speech into noise); opus decode is the only call.
    kinds = [c[0] for c in decoder_calls]
    assert kinds == ["opus_init", "opus"]
    assert router.waiter.registered == [dec]
    assert router.waiter.unregistered == [dec]


def test_tiny_dtx_frame_emits_silence_not_drop(decoder_calls: list) -> None:
    """3-byte DTX/OPUS_SILENCE frames must keep their 20ms timeline slot."""

    vc = _voice_client(ssrc_map={}, member=None, dave=None)
    dec = patches.RealtimeDecoder(_router(vc), ssrc=7)

    dec.push_packet(SimpleNamespace(decrypted_data=b"\xf8\xff\xfe"))
    data = dec.pop_data()

    assert data is not None
    assert data.pcm == patches.SILENCE_PCM_20MS
    assert "opus" not in [c[0] for c in decoder_calls]


def test_missing_decrypted_data_emits_silence(decoder_calls: list) -> None:
    vc = _voice_client(ssrc_map={}, member=None, dave=None)
    dec = patches.RealtimeDecoder(_router(vc), ssrc=7)

    dec.push_packet(SimpleNamespace(decrypted_data=b""))
    data = dec.pop_data()

    assert data is not None
    assert data.pcm == patches.SILENCE_PCM_20MS
    assert [c[0] for c in decoder_calls] == ["opus_init"]


def test_opus_decode_failure_emits_silence(decoder_calls: list, monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingDecoder:
        def decode(self, frame: bytes, fec: bool = False) -> bytes:
            raise RuntimeError("corrupt opus frame")

    monkeypatch.setattr(patches, "Decoder", FailingDecoder)
    vc = _voice_client(ssrc_map={}, member=None, dave=None)
    dec = patches.RealtimeDecoder(_router(vc), ssrc=7)

    dec.push_packet(SimpleNamespace(decrypted_data=b"corrupt-frame-10plus"))
    data = dec.pop_data()

    assert data is not None
    assert data.pcm == patches.SILENCE_PCM_20MS


def test_set_user_id_overrides_ssrc_map(decoder_calls: list) -> None:
    fallback = SimpleNamespace(id=99, name="fallback-user")
    vc = _voice_client(
        ssrc_map={7: 42}, member=None, dave=None, fallback_user=fallback
    )
    dec = patches.RealtimeDecoder(_router(vc), ssrc=7)
    dec.set_user_id(99)

    dec.push_packet(SimpleNamespace(decrypted_data=b"enc-x-10plus-bytes"))
    data = dec.pop_data()

    assert data is not None
    assert data.source is fallback


def test_pop_data_empty_queue_returns_none(decoder_calls: list) -> None:
    vc = _voice_client(ssrc_map={}, member=None, dave=None)
    dec = patches.RealtimeDecoder(_router(vc), ssrc=7)

    assert dec.pop_data() is None


# ---------------------------------------------------------------------------
# RTP padding strip (PacketDecryptor.decrypt_rtp patch)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_dave_stats():
    saved = dict(patches.DAVE_STATS)
    for key in patches.DAVE_STATS:
        patches.DAVE_STATS[key] = 0
    yield
    patches.DAVE_STATS.clear()
    patches.DAVE_STATS.update(saved)


class _ReadyDave:
    """Recording DAVE session with .ready, shaped like davey.DaveSession."""

    def __init__(self, *, fail: bool = False) -> None:
        self.ready = True
        self.received: list[bytes] = []
        self._fail = fail

    def decrypt(self, user_id: int, media_type, frame: bytes) -> bytes:
        self.received.append(frame)
        if self._fail:
            raise RuntimeError("undecryptable frame")
        return b"plain:" + frame


def _decryptor_self(payload: bytes, dave, ssrc_map: dict[int, int] | None = None) -> SimpleNamespace:
    """Fake the PacketDecryptor attributes _patched_decrypt_rtp touches."""

    return SimpleNamespace(
        _decryptor_rtp=lambda packet: payload,
        client=SimpleNamespace(
            _connection=SimpleNamespace(
                dave_session=dave,
                ssrc_user_map={7: 42} if ssrc_map is None else ssrc_map,
            )
        ),
    )


def _packet(*, padding: bool = False, extended: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        padding=padding, extended=extended, ssrc=7, decrypted_data=None
    )


def test_padding_stripped_before_dave() -> None:
    dave = _ReadyDave()
    payload = b"opus-payload" + b"\x00\x00\x03"  # RFC 3550: last byte = count
    packet = _packet(padding=True)

    result = patches._patched_decrypt_rtp(_decryptor_self(payload, dave), packet)

    assert dave.received == [b"opus-payload"]
    assert result == b"plain:opus-payload"
    assert patches.DAVE_STATS["padded"] == 1
    assert patches.DAVE_STATS["pad_bad"] == 0
    assert patches.DAVE_STATS["dave_ok"] == 1


def test_unpadded_packet_flows_through() -> None:
    dave = _ReadyDave()
    packet = _packet(padding=False)

    patches._patched_decrypt_rtp(_decryptor_self(b"opus-payload", dave), packet)

    assert dave.received == [b"opus-payload"]
    assert patches.DAVE_STATS["padded"] == 0
    assert patches.DAVE_STATS["dave_ok"] == 1


def test_bad_padding_left_for_dave_to_reject() -> None:
    dave = _ReadyDave(fail=True)
    payload = b"opus-payload\x00"  # pad count 0 — RFC violation
    packet = _packet(padding=True)

    result = patches._patched_decrypt_rtp(_decryptor_self(payload, dave), packet)

    assert dave.received == [payload]  # unstripped — DAVE rejects it
    assert result == patches.OPUS_SILENCE
    assert patches.DAVE_STATS["pad_bad"] == 1
    assert patches.DAVE_STATS["dave_fail"] == 1


def test_all_padding_frame_short_circuits_to_silence() -> None:
    """A frame that is ENTIRELY padding (pad byte >= payload len) carries no
    audio — Discord's all-0xFF connection/probe burst (pad byte 0xFF == len
    255). It must return OPUS_SILENCE WITHOUT calling dave.decrypt, which
    would otherwise raise UnencryptedWhenPassthroughDisabled and spam a
    traceback per frame (52 in one live session, 2026-08-02)."""
    dave = _ReadyDave(fail=True)  # raises if ever called
    payload = b"\xff" * 255  # last byte 0xFF == 255 == len -> all padding
    packet = _packet(padding=True)

    result = patches._patched_decrypt_rtp(_decryptor_self(payload, dave), packet)

    assert result == patches.OPUS_SILENCE
    assert dave.received == []  # dave.decrypt was NEVER reached
    assert patches.DAVE_STATS["pad_all"] == 1
    assert patches.DAVE_STATS["dave_fail"] == 0  # no doomed decrypt attempt
    assert patches.DAVE_STATS["dave_unencrypted"] == 0  # no spurious warning
    assert packet.realtime_lost is False  # this IS silence, not a loss


def test_over_length_padding_is_concealed_as_lost_not_silence() -> None:
    """pad > len is an over-count — it violates RFC 3550 and is NOT genuine
    end-of-transmission silence (unlike pad == len). It must be marked lost so
    the decoder conceals it (PLC), not mislabelled as silence and not handed to
    DAVE (which would raise). Finding 3 from the 2026-08-02 review: the old
    `pad >= len` branch swept this into the all-padding silence path."""
    dave = _ReadyDave(fail=True)  # raises if ever called
    payload = b"opus-payload-bytes" + bytes([200])  # last byte 200 > len (19)
    packet = _packet(padding=True)

    result = patches._patched_decrypt_rtp(_decryptor_self(payload, dave), packet)

    assert result == patches.OPUS_SILENCE
    assert dave.received == []  # malformed — never handed to DAVE
    assert patches.DAVE_STATS["pad_bad"] == 1
    assert patches.DAVE_STATS["pad_all"] == 0  # NOT counted as genuine silence
    assert patches.DAVE_STATS["dave_fail"] == 0
    assert packet.realtime_lost is True  # marked lost -> decoder conceals (PLC)


def test_dave_failure_substitutes_opus_silence() -> None:
    dave = _ReadyDave(fail=True)
    packet = _packet()

    result = patches._patched_decrypt_rtp(_decryptor_self(b"frame", dave), packet)

    assert result == patches.OPUS_SILENCE
    assert patches.DAVE_STATS["dave_fail"] == 1
    assert patches.DAVE_STATS["dave_ok"] == 0


def test_dave_not_ready_returns_none() -> None:
    dave = SimpleNamespace(ready=False)
    packet = _packet()

    result = patches._patched_decrypt_rtp(_decryptor_self(b"frame", dave), packet)

    assert result is None
    assert patches.DAVE_STATS["dave_ok"] == 0
    assert patches.DAVE_STATS["dave_fail"] == 0


# ---------------------------------------------------------------------------
# Router patch application
# ---------------------------------------------------------------------------


def test_apply_patches_swaps_router_and_decryptor() -> None:
    from discord.voice.receive.reader import PacketDecryptor
    from discord.voice.receive.router import PacketRouter

    original_decoder = PacketRouter.get_decoder
    original_decrypt = PacketDecryptor.decrypt_rtp
    try:
        patches.apply_patches()
        assert PacketRouter.get_decoder is patches._patched_get_decoder
        assert PacketDecryptor.decrypt_rtp is patches._patched_decrypt_rtp
    finally:
        PacketRouter.get_decoder = original_decoder
        PacketDecryptor.decrypt_rtp = original_decrypt


def test_patched_get_decoder_caches_per_ssrc(decoder_calls: list) -> None:
    vc = _voice_client(ssrc_map={}, member=None, dave=None)
    router = SimpleNamespace(
        sink=SimpleNamespace(client=vc),
        waiter=_Waiter(),
        decoders={},
        _lock=threading.Lock(),
    )

    d1 = patches._patched_get_decoder(router, 5)
    d2 = patches._patched_get_decoder(router, 5)
    d3 = patches._patched_get_decoder(router, 6)

    assert isinstance(d1, patches.RealtimeDecoder)
    assert d1 is d2
    assert d3 is not d1
    assert d3.ssrc == 6


# ---------------------------------------------------------------------------
# Opus silence frames bypass DAVE (the UnencryptedWhenPassthroughDisabled bug)
# ---------------------------------------------------------------------------


def test_opus_silence_frame_never_reaches_dave() -> None:
    """f8fffe arrives UNENCRYPTED inside a DAVE call; libdave skips it.

    Handing one to dave.decrypt() raises UnencryptedWhenPassthroughDisabled
    and the reader substitutes the very OPUS_SILENCE the frame already was.
    """

    dave = _ReadyDave(fail=True)
    packet = _packet()

    result = patches._patched_decrypt_rtp(
        _decryptor_self(patches.OPUS_SILENCE, dave), packet
    )

    assert dave.received == []  # never handed to DAVE
    assert result == patches.OPUS_SILENCE
    assert patches.DAVE_STATS["silence"] == 1
    assert patches.DAVE_STATS["dave_fail"] == 0
    assert packet.realtime_lost is False


def test_silence_frame_does_not_trip_the_padding_counter() -> None:
    """The 3-byte silence payload's last byte (0xfe) is not a pad count.

    This is the pad_bad == dave_fail == 53 correlation observed live: one
    population of packets incrementing both counters.
    """

    dave = _ReadyDave(fail=True)
    packet = _packet(padding=True)

    patches._patched_decrypt_rtp(_decryptor_self(patches.OPUS_SILENCE, dave), packet)

    assert patches.DAVE_STATS["pad_bad"] == 0
    assert patches.DAVE_STATS["padded"] == 0
    assert patches.DAVE_STATS["silence"] == 1


def test_real_padded_payload_still_stripped() -> None:
    """Regression guard: the silence short-circuit must not shadow padding."""

    dave = _ReadyDave()
    packet = _packet(padding=True)

    patches._patched_decrypt_rtp(
        _decryptor_self(b"opus-payload\x00\x00\x03", dave), packet
    )

    assert dave.received == [b"opus-payload"]
    assert patches.DAVE_STATS["padded"] == 1
    assert patches.DAVE_STATS["silence"] == 0


def test_unencrypted_rejection_is_classified_separately() -> None:
    """If the short-circuit ever misses, the failure must be identifiable."""

    class _Unencrypted(_ReadyDave):
        def decrypt(self, user_id, media_type, frame):
            raise ValueError(
                "Failed to decrypt: DecryptionFailed(UnencryptedWhenPassthroughDisabled)"
            )

    packet = _packet()
    patches._patched_decrypt_rtp(_decryptor_self(b"frame-data", _Unencrypted()), packet)

    assert patches.DAVE_STATS["dave_unencrypted"] == 1
    assert patches.DAVE_STATS["dave_fail"] == 1
    assert packet.realtime_lost is True


def test_ordinary_crypto_failure_is_not_counted_as_unencrypted() -> None:
    packet = _packet()
    patches._patched_decrypt_rtp(_decryptor_self(b"frame", _ReadyDave(fail=True)), packet)

    assert patches.DAVE_STATS["dave_fail"] == 1
    assert patches.DAVE_STATS["dave_unencrypted"] == 0


def test_non_opus_payload_type_counted_but_not_dropped() -> None:
    """discord.js#11449 drops these. We count first — a wrong guard goes deaf."""

    dave = _ReadyDave()
    packet = _packet()
    packet.payload = 0x6F  # not RTP_OPUS_PAYLOAD_TYPE

    result = patches._patched_decrypt_rtp(_decryptor_self(b"opus-payload", dave), packet)

    assert patches.DAVE_STATS["non_opus"] == 1
    assert result == b"plain:opus-payload"  # still processed


# ---------------------------------------------------------------------------
# Concealment: a pause and a loss are not the same 20ms
# ---------------------------------------------------------------------------


def test_lost_packet_uses_plc_not_zeros(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rejected packet is a GAP — opus PLC reconstructs it and keeps
    decoder state coherent. Hard zeros do neither."""

    plc_calls: list = []

    class PlcDecoder:
        def decode(self, frame, fec: bool = False) -> bytes:
            plc_calls.append(frame)
            assert frame is None, "PLC must pass None, not a frame"
            return b"\x01\x02" * 1920

    monkeypatch.setattr(patches, "Decoder", PlcDecoder)
    vc = _voice_client(ssrc_map={}, member=None, dave=None)
    dec = patches.RealtimeDecoder(_router(vc), ssrc=7)

    dec.push_packet(
        SimpleNamespace(decrypted_data=patches.OPUS_SILENCE, realtime_lost=True)
    )
    data = dec.pop_data()

    assert data is not None
    assert plc_calls == [None]
    assert data.pcm != patches.SILENCE_PCM_20MS
    assert patches.DAVE_STATS["concealed"] == 1


def test_genuine_silence_frame_uses_zeros_not_plc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Discord's voice docs: silence frames exist to STOP opus interpolation.

    Concealing one would smear the previous talk spurt across the pause.
    """

    plc_calls: list = []

    class PlcDecoder:
        def decode(self, frame, fec: bool = False) -> bytes:
            plc_calls.append(frame)
            return b"\x7f\x7f" * 1920

    monkeypatch.setattr(patches, "Decoder", PlcDecoder)
    vc = _voice_client(ssrc_map={}, member=None, dave=None)
    dec = patches.RealtimeDecoder(_router(vc), ssrc=7)

    dec.push_packet(
        SimpleNamespace(decrypted_data=patches.OPUS_SILENCE, realtime_lost=False)
    )
    data = dec.pop_data()

    assert data is not None
    assert plc_calls == []
    assert data.pcm == patches.SILENCE_PCM_20MS
    assert patches.DAVE_STATS["concealed"] == 0


def test_plc_failure_falls_back_to_zeros(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenDecoder:
        def decode(self, frame, fec: bool = False) -> bytes:
            raise RuntimeError("opus state gone")

    monkeypatch.setattr(patches, "Decoder", BrokenDecoder)
    vc = _voice_client(ssrc_map={}, member=None, dave=None)
    dec = patches.RealtimeDecoder(_router(vc), ssrc=7)

    dec.push_packet(SimpleNamespace(decrypted_data=b"", realtime_lost=True))
    data = dec.pop_data()

    assert data is not None
    assert data.pcm == patches.SILENCE_PCM_20MS
