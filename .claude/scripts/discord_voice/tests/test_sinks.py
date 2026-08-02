"""RealtimeSink tests — py-cord 2.8 router surface + write() forwarding."""

from __future__ import annotations

from types import SimpleNamespace

from sinks import RealtimeSink


def _sink() -> tuple[RealtimeSink, list[tuple[bytes, int | None, int | None]]]:
    received: list[tuple[bytes, int | None, int | None]] = []
    sink = RealtimeSink(lambda pcm, user_id, ssrc: received.append((pcm, user_id, ssrc)))
    return sink, received


def test_sink_surface_matches_router_contract() -> None:
    sink, _received = _sink()

    assert sink.is_opus() is False
    assert sink.walk_children() == []
    assert sink.walk_children(with_self=True) == [sink]
    assert sink.root is sink
    assert sink.__sink_listeners__ == []


def test_write_forwards_pcm_and_user_object() -> None:
    sink, received = _sink()

    data = SimpleNamespace(pcm=b"abc", packet=SimpleNamespace(ssrc=7))
    sink.write(data, SimpleNamespace(id=42))

    assert received == [(b"abc", 42, 7)]
    assert sink.packets == 1
    assert sink.bytes == 3


def test_write_accepts_int_user() -> None:
    sink, received = _sink()

    sink.write(SimpleNamespace(pcm=b"xy", packet=SimpleNamespace(ssrc=3)), 7)

    assert received == [(b"xy", 7, 3)]


def test_write_unknown_user_and_ssrc_forward_none() -> None:
    sink, received = _sink()

    sink.write(SimpleNamespace(pcm=b"z"), None)

    assert received == [(b"z", None, None)]


def test_write_skips_missing_or_empty_pcm() -> None:
    sink, received = _sink()

    sink.write(SimpleNamespace(pcm=b""), SimpleNamespace(id=1))
    sink.write(SimpleNamespace(), SimpleNamespace(id=1))

    assert received == []
    assert sink.packets == 0
    assert sink.bytes == 0
