"""Tests for the cabinet → chat relay (``.claude/chat/cabinet_relay.py``).

One test per distinct code path (Testing Principle):

  * ``_relay_meeting``: agent_done → name-prefixed send; skip incomplete/empty;
    origin routing; fail-open on send error; meeting_ended stop; max_turns cap;
    dedup-guard cleared on exit.
  * ``ensure_relay``: dedup (one stream for two calls); disabled returns False;
    missing channel returns False; no running loop returns False.
  * handler wiring: ``handle_standup`` calls ``ensure_relay`` and returns the
    "answer right here" reply when the relay is active.

Async paths are driven with ``asyncio.run`` (no pytest-asyncio dependency).
"""
from __future__ import annotations

import asyncio

import pytest

import cabinet_relay
from models import Channel, Platform


@pytest.fixture(autouse=True)
def _isolate_dead_registry(tmp_path, monkeypatch):
    """Keep the relay's dead-target registry off the real ``config.STATE_DIR``.

    The dead-target wiring lazily builds a module-singleton registry the first
    time a relay send runs. Pin it to a per-test tmp file (and reset the cached
    singleton) so these existing relay tests neither read nor write the real
    state dir, and so a mark in one test can't leak into another.
    """
    from orchestration.dead_targets import DeadTargetRegistry

    monkeypatch.setattr(cabinet_relay, "_dead_registry", None, raising=False)
    reg = DeadTargetRegistry(path=tmp_path / "dead_targets.json")
    monkeypatch.setattr(cabinet_relay, "_get_dead_registry", lambda: reg)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeAdapter:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list = []
        self.attempts = 0
        self.fail = fail

    async def send(self, message):  # mirrors PlatformAdapter.send
        self.attempts += 1
        if self.fail:
            raise RuntimeError("send boom")
        self.sent.append(message)
        return "mid-1"


class _FakeIncoming:
    def __init__(self, channel) -> None:
        self.channel = channel


def _origin(platform=Platform.DISCORD, pid: str = "chan-1") -> Channel:
    return Channel(platform=platform, platform_id=pid)


def _evt(seq: int, inner: dict) -> dict:
    return {"seq": seq, "event": inner}


def _agent_done(agent_id: str, text: str, incomplete: bool = False) -> dict:
    return {
        "type": "agent_done",
        "agentId": agent_id,
        "text": text,
        "incomplete": incomplete,
    }


def _fake_stream(events):
    """Return an async-generator function that yields ``events`` then stops."""

    async def _gen(meeting_id, *args, **kwargs):
        for e in events:
            yield e

    return _gen


# ---------------------------------------------------------------------------
# _relay_meeting
# ---------------------------------------------------------------------------


def test_relay_posts_agent_done_turns(monkeypatch):
    from integrations import cabinet_api

    events = [
        _evt(0, {"type": "meeting_state", "agents": []}),
        _evt(1, _agent_done("sales", "Ship it today.")),
        _evt(2, {"type": "agent_typing", "agentId": "marketing"}),
        _evt(3, _agent_done("marketing", "Promote the launch.")),
        _evt(4, {"type": "meeting_ended"}),
        _evt(5, _agent_done("finance", "should not appear")),
    ]
    monkeypatch.setattr(cabinet_api, "stream_meeting", _fake_stream(events))
    adapter = _FakeAdapter()
    origin = _origin()

    asyncio.run(cabinet_relay._relay_meeting(11, adapter, origin, 0))

    assert [m.text for m in adapter.sent] == [
        "**Sales:** Ship it today.",
        "**Marketing:** Promote the launch.",
    ]
    assert all(m.channel is origin for m in adapter.sent)


def test_relay_skips_incomplete_and_empty(monkeypatch):
    from integrations import cabinet_api

    events = [
        _evt(1, _agent_done("sales", "", incomplete=True)),
        _evt(2, _agent_done("ops", "   ")),  # whitespace-only → skipped
        _evt(3, _agent_done("seo_content", "Real answer.")),
    ]
    monkeypatch.setattr(cabinet_api, "stream_meeting", _fake_stream(events))
    adapter = _FakeAdapter()

    asyncio.run(cabinet_relay._relay_meeting(12, adapter, _origin(), 0))

    assert [m.text for m in adapter.sent] == ["**Seo Content:** Real answer."]


def test_relay_origin_routing_telegram(monkeypatch):
    from integrations import cabinet_api

    monkeypatch.setattr(
        cabinet_api, "stream_meeting",
        _fake_stream([_evt(1, _agent_done("sales", "hi"))]),
    )
    adapter = _FakeAdapter()
    origin = _origin(platform=Platform.TELEGRAM, pid="555")

    asyncio.run(cabinet_relay._relay_meeting(13, adapter, origin, 0))

    assert len(adapter.sent) == 1
    assert adapter.sent[0].channel.platform is Platform.TELEGRAM
    assert adapter.sent[0].channel.platform_id == "555"


def test_relay_fail_open_on_send_error(monkeypatch):
    from integrations import cabinet_api

    monkeypatch.setattr(
        cabinet_api, "stream_meeting",
        _fake_stream([_evt(1, _agent_done("a", "x")), _evt(2, _agent_done("b", "y"))]),
    )
    adapter = _FakeAdapter(fail=True)

    # Must NOT raise even though send() raises on every turn.
    asyncio.run(cabinet_relay._relay_meeting(14, adapter, _origin(), 0))

    assert adapter.sent == []  # nothing captured, but the loop survived


def test_relay_meeting_ended_stops(monkeypatch):
    from integrations import cabinet_api

    events = [
        _evt(1, _agent_done("sales", "first")),
        _evt(2, {"type": "meeting_ended"}),
        _evt(3, _agent_done("ops", "after-end")),
    ]
    monkeypatch.setattr(cabinet_api, "stream_meeting", _fake_stream(events))
    adapter = _FakeAdapter()

    asyncio.run(cabinet_relay._relay_meeting(15, adapter, _origin(), 0))

    assert [m.text for m in adapter.sent] == ["**Sales:** first"]


def test_relay_max_turns_cap(monkeypatch):
    from integrations import cabinet_api

    events = [_evt(i, _agent_done(f"p{i}", f"t{i}")) for i in range(1, 6)]
    monkeypatch.setattr(cabinet_api, "stream_meeting", _fake_stream(events))
    adapter = _FakeAdapter()

    asyncio.run(cabinet_relay._relay_meeting(16, adapter, _origin(), 2))

    assert len(adapter.sent) == 2


def test_relay_clears_dedup_on_exit(monkeypatch):
    from integrations import cabinet_api

    cabinet_relay._active_relays.add(17)
    monkeypatch.setattr(cabinet_api, "stream_meeting", _fake_stream([]))

    asyncio.run(cabinet_relay._relay_meeting(17, _FakeAdapter(), _origin(), 0))

    assert 17 not in cabinet_relay._active_relays


def test_relay_breaks_on_consecutive_send_failures(monkeypatch):
    """A persistently dead channel stops after 3 consecutive send failures
    instead of dropping the whole roster into a silent void."""
    from integrations import cabinet_api

    events = [_evt(i, _agent_done(f"p{i}", f"t{i}")) for i in range(1, 6)]
    monkeypatch.setattr(cabinet_api, "stream_meeting", _fake_stream(events))
    adapter = _FakeAdapter(fail=True)

    asyncio.run(cabinet_relay._relay_meeting(18, adapter, _origin(), 0))

    assert adapter.attempts == 3  # broke after 3 consecutive failures, not all 5


def test_relay_posts_notice_on_stream_error(monkeypatch):
    """Stream dies before ANY turn → the operator gets a one-time in-chat
    notice with the dashboard fallback (review finding 1)."""
    from integrations import cabinet_api

    async def _raising(meeting_id, *a, **k):
        raise RuntimeError("api down")
        yield  # async-generator marker

    monkeypatch.setattr(cabinet_api, "stream_meeting", _raising)
    cabinet_relay._meeting_high_seq.pop(28, None)
    adapter = _FakeAdapter()

    asyncio.run(cabinet_relay._relay_meeting(28, adapter, _origin(), 0))

    assert len(adapter.sent) == 1
    assert "Couldn't relay" in adapter.sent[0].text
    assert "cabinet?id=28" in adapter.sent[0].text
    assert 28 not in cabinet_relay._active_relays


def test_relay_no_notice_after_partial(monkeypatch):
    """Stream dies AFTER some turns were relayed → no scary notice (they
    already got real answers); the relayed turns stand."""
    from integrations import cabinet_api

    async def _gen(meeting_id, *a, **k):
        yield _evt(1, _agent_done("sales", "hi"))
        raise RuntimeError("mid-stream boom")

    monkeypatch.setattr(cabinet_api, "stream_meeting", _gen)
    cabinet_relay._meeting_high_seq.pop(29, None)
    adapter = _FakeAdapter()

    asyncio.run(cabinet_relay._relay_meeting(29, adapter, _origin(), 0))

    assert [m.text for m in adapter.sent] == ["**Sales:** hi"]  # no notice appended


def test_relay_resume_no_duplicate(monkeypatch):
    """Re-subscribe after a mid-meeting stream death resumes AFTER the last
    delivered seq — already-posted turns are NOT replayed (HIGH-severity fix)."""
    from integrations import cabinet_api

    captured: list = []

    def _resumable():
        async def _gen(meeting_id, since_seq=None, *a, **k):
            captured.append(since_seq)
            if since_seq in (None, 0):
                yield _evt(0, {"type": "meeting_state"})
                yield _evt(1, _agent_done("sales", "A"))
                yield _evt(2, _agent_done("marketing", "B"))
                raise RuntimeError("mid-stream death")
            yield _evt(3, _agent_done("finance", "C"))
            yield _evt(4, {"type": "meeting_ended"})

        return _gen

    monkeypatch.setattr(cabinet_api, "stream_meeting", _resumable())
    cabinet_relay._meeting_high_seq.pop(30, None)
    adapter = _FakeAdapter()
    origin = _origin()

    # First subscribe — delivers 2 turns then dies; high-water = 2.
    asyncio.run(cabinet_relay._relay_meeting(30, adapter, origin, 0))
    assert [m.text for m in adapter.sent] == ["**Sales:** A", "**Marketing:** B"]
    assert cabinet_relay._meeting_high_seq.get(30) == 2

    # Re-subscribe — must resume at since_seq=2, NOT replay sales/marketing.
    asyncio.run(cabinet_relay._relay_meeting(30, adapter, origin, 0))
    assert captured == [None, 2]
    assert [m.text for m in adapter.sent] == [
        "**Sales:** A", "**Marketing:** B", "**Finance:** C",
    ]
    assert 30 not in cabinet_relay._meeting_high_seq  # popped on meeting_ended


# ---------------------------------------------------------------------------
# ensure_relay
# ---------------------------------------------------------------------------


def test_ensure_relay_dedups(monkeypatch):
    from integrations import cabinet_api

    calls = {"n": 0}

    async def _gen(meeting_id, *a, **k):
        calls["n"] += 1
        return
        yield  # noqa — makes this an async generator function

    monkeypatch.setattr(cabinet_api, "stream_meeting", _gen)
    monkeypatch.delenv("CABINET_CHAT_RELAY_ENABLED", raising=False)
    cabinet_relay._active_relays.clear()
    adapter = _FakeAdapter()
    incoming = _FakeIncoming(_origin())

    async def _run():
        r1 = cabinet_relay.ensure_relay(21, adapter, incoming)
        r2 = cabinet_relay.ensure_relay(21, adapter, incoming)
        for _ in range(5):  # let the single spawned task run to completion
            await asyncio.sleep(0)
        return r1, r2

    r1, r2 = asyncio.run(_run())
    assert r1 is True and r2 is True
    assert calls["n"] == 1  # second ensure_relay deduped → only one stream


def test_ensure_relay_disabled(monkeypatch):
    from integrations import cabinet_api

    calls = {"n": 0}

    async def _gen(meeting_id, *a, **k):
        calls["n"] += 1
        return
        yield

    monkeypatch.setattr(cabinet_api, "stream_meeting", _gen)
    monkeypatch.setenv("CABINET_CHAT_RELAY_ENABLED", "false")
    cabinet_relay._active_relays.clear()

    async def _run():
        result = cabinet_relay.ensure_relay(22, _FakeAdapter(), _FakeIncoming(_origin()))
        for _ in range(3):
            await asyncio.sleep(0)
        return result

    result = asyncio.run(_run())
    assert result is False
    assert calls["n"] == 0
    assert 22 not in cabinet_relay._active_relays


def test_ensure_relay_no_channel(monkeypatch):
    monkeypatch.setenv("CABINET_CHAT_RELAY_ENABLED", "true")
    cabinet_relay._active_relays.clear()

    async def _run():
        return cabinet_relay.ensure_relay(23, _FakeAdapter(), _FakeIncoming(None))

    assert asyncio.run(_run()) is False
    assert 23 not in cabinet_relay._active_relays


def test_ensure_relay_no_running_loop(monkeypatch):
    monkeypatch.setenv("CABINET_CHAT_RELAY_ENABLED", "true")
    cabinet_relay._active_relays.clear()

    # Called with NO running loop → RuntimeError swallowed → False.
    assert cabinet_relay.ensure_relay(24, _FakeAdapter(), _FakeIncoming(_origin())) is False
    assert 24 not in cabinet_relay._active_relays


# ---------------------------------------------------------------------------
# handler wiring
# ---------------------------------------------------------------------------


def test_handle_standup_wires_relay(monkeypatch):
    import core_handlers
    from integrations import cabinet_api

    class _Ref:
        id = 99

    async def _create(chat_id=None, *, client=None):
        return _Ref()

    async def _send(meeting_id, text, *a, **k):
        return {"ok": True, "queued": True}

    monkeypatch.setattr(cabinet_api, "create_meeting", _create)
    monkeypatch.setattr(cabinet_api, "send_message", _send)

    seen: dict = {}

    def _ensure(meeting_id, adapter, incoming):
        seen["meeting_id"] = meeting_id
        return True

    monkeypatch.setattr(cabinet_relay, "ensure_relay", _ensure)
    monkeypatch.delenv("HOMIE_KILLSWITCH_CABINET", raising=False)

    result = asyncio.run(
        core_handlers.handle_standup(_FakeAdapter(), _FakeIncoming(_origin()), "what's up?")
    )

    assert seen["meeting_id"] == 99
    assert "answer right here" in result
