"""Test PRD-8 Phase 5a / WS1.2 — MeetingChannel port verbatim.

Asserts:
  - emit() returns -1 on finalized-turn drop (load-bearing safety net).
  - Ring buffer max 500 with deque maxlen.
  - markTurnFinalized FIFO cap 32.
  - since(seq) returns entries strictly newer.
  - 20 CABINET_EVENT_TYPES variants set-equality (M3).
  - Idle eviction (1h TTL) for listener-less channels.
  - Module-local _CHANNELS registry — same-process invariant gate.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from cabinet.meeting_channel import (
    CABINET_EVENT_TYPES,
    IDLE_TTL_S,
    MeetingChannel,
    _reset_channels,
    _sweep_idle_channels,
    close_channel,
    get_channel,
)


@pytest.fixture(autouse=True)
def _reset_state():
    _reset_channels()
    yield
    _reset_channels()


def test_event_taxonomy_20_variants_set_equality() -> None:
    """M3 — 20 CabinetEvent variants verbatim from warroom-text-events.ts:20-45."""
    expected = {
        "meeting_state",
        "turn_start",
        "status_update",
        "router_decision",
        "agent_selected",
        "agent_typing",
        "agent_chunk",
        "agent_done",
        "intervention_skipped",
        "tool_call",
        "tool_result",
        "turn_complete",
        "turn_aborted",
        "system_note",
        "divider",
        "meeting_state_update",
        "meeting_ended",
        "replay_gap",
        "error",
        "ping",
    }
    assert CABINET_EVENT_TYPES == expected
    assert len(CABINET_EVENT_TYPES) == 20


def test_emit_returns_seq_for_normal_event() -> None:
    ch = MeetingChannel()
    seq = ch.emit({"type": "system_note", "text": "hi", "tone": "info", "dismissable": True})
    assert seq == 1
    seq2 = ch.emit({"type": "ping"})
    assert seq2 == 2


def test_emit_returns_minus_one_for_finalized_turn() -> None:
    """Load-bearing safety net — late chunks for finalized turns get dropped."""
    ch = MeetingChannel()
    ch.mark_turn_finalized("t_abc")
    dropped = ch.emit({"type": "agent_chunk", "turnId": "t_abc", "agentId": "default", "role": "primary", "delta": "x"})
    assert dropped == -1
    assert ch.is_turn_finalized("t_abc")


def test_emit_without_turnid_passes_finalized_check() -> None:
    """Events without `turnId` (system_note global, divider) ignore the finalized set."""
    ch = MeetingChannel()
    ch.mark_turn_finalized("t_old")
    seq = ch.emit({"type": "ping"})
    assert seq == 1


def test_finalized_turns_fifo_cap_32() -> None:
    ch = MeetingChannel()
    for i in range(40):
        ch.mark_turn_finalized(f"t_{i}")
    # Oldest 8 should have been evicted; latest 32 remain.
    assert not ch.is_turn_finalized("t_0")
    assert not ch.is_turn_finalized("t_7")
    assert ch.is_turn_finalized("t_8")
    assert ch.is_turn_finalized("t_39")


def test_ring_buffer_max_500() -> None:
    ch = MeetingChannel(max_buffer=10)
    for i in range(20):
        ch.emit({"type": "ping", "i": i})
    # Ring buffer holds most recent 10 only.
    assert ch.oldest_seq() == 11
    assert ch.latest_seq() == 20
    entries = ch.since(0)
    assert len(entries) == 10


def test_since_strictly_newer() -> None:
    ch = MeetingChannel()
    for _ in range(5):
        ch.emit({"type": "ping"})
    assert [e.seq for e in ch.since(0)] == [1, 2, 3, 4, 5]
    assert [e.seq for e in ch.since(2)] == [3, 4, 5]
    assert ch.since(5) == []


def test_subscribe_receives_emitted_events() -> None:
    async def _run() -> None:
        ch = MeetingChannel()
        q, unsub = ch.subscribe()
        try:
            ch.emit({"type": "system_note", "text": "x", "tone": "info", "dismissable": False})
            entry = await asyncio.wait_for(q.get(), timeout=1.0)
            assert entry.event["text"] == "x"
            assert entry.seq == 1
        finally:
            unsub()
    asyncio.run(_run())


def test_subscribe_unsub_idempotent() -> None:
    async def _run() -> None:
        ch = MeetingChannel()
        _, unsub = ch.subscribe()
        unsub()
        unsub()  # second call should not raise
    asyncio.run(_run())


def test_close_clears_state() -> None:
    ch = MeetingChannel()
    ch.emit({"type": "ping"})
    ch.mark_turn_finalized("t_x")
    ch.close()
    assert ch.listener_count() == 0
    assert ch.latest_seq() == 1  # seq counter retained per upstream contract
    assert ch.oldest_seq() == 0  # buffer cleared
    assert not ch.is_turn_finalized("t_x")


def test_get_channel_lazy_creates_and_close_drops() -> None:
    ch1 = get_channel(42)
    ch2 = get_channel(42)
    assert ch1 is ch2
    close_channel(42)
    ch3 = get_channel(42)
    assert ch3 is not ch1


def test_idle_sweeper_evicts_listenerless_after_ttl() -> None:
    ch = get_channel(1)
    # Force last_activity_at into the past beyond TTL.
    ch.last_activity_at = int(time.time() * 1000) - (IDLE_TTL_S * 1000) - 1
    _sweep_idle_channels()
    # Channel evicted; lazy-create returns a NEW one.
    ch2 = get_channel(1)
    assert ch2 is not ch


def test_idle_sweeper_keeps_active_listener_channels() -> None:
    async def _run() -> None:
        ch = get_channel(2)
        ch.last_activity_at = int(time.time() * 1000) - (IDLE_TTL_S * 1000) - 1
        _q, _unsub = ch.subscribe()
        try:
            _sweep_idle_channels()
            # Active subscriber → not evicted; same instance.
            ch_again = get_channel(2)
            assert ch_again is ch
        finally:
            _unsub()
    asyncio.run(_run())


def test_meeting_state_event_carries_camelcase_fields() -> None:
    """Wire shape contract — camelCase preserved (turnId, agentId, etc.)."""
    ch = MeetingChannel()
    ch.emit({
        "type": "meeting_state",
        "meetingId": "abc",
        "pinnedAgent": "default",
        "agents": [{"id": "default", "name": "Main", "description": "host"}],
        "isFresh": True,
    })
    entries = ch.since(0)
    assert "pinnedAgent" in entries[0].event
    assert "isFresh" in entries[0].event
