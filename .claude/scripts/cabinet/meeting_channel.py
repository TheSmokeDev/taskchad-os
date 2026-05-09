"""Per-meeting event channel for cabinet text mode.

PORTED VERBATIM FROM ClaudeClaw `src/warroom-text-events.ts:1-215` per PRD-8 §0a.

Same-process invariant: producer (text_orchestrator.handle_text_turn) AND
subscriber (/api/cabinet/stream?meetingId= SSE handler in dashboard_api.py)
BOTH live in the orchestration API process. Module-local _CHANNELS dict
bridges them. Phase 5b's chat process accesses via HTTP only — never imports
this module.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Final

# WarRoomTextEvent → CabinetEvent (Q-naming lock; shape verbatim).
# 20 variants matching warroom-text-events.ts:20-45 exactly:
#   meeting_state, turn_start, status_update, router_decision, agent_selected,
#   agent_typing, agent_chunk, agent_done, intervention_skipped, tool_call,
#   tool_result, turn_complete, turn_aborted, system_note, divider,
#   meeting_state_update, meeting_ended, replay_gap, error, ping
CABINET_EVENT_TYPES: Final[frozenset[str]] = frozenset({
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
})


# Type alias — discriminated event dict. Keys camelCase to mirror the wire
# contract (turnId/clientMsgId/agentId etc.) so the Preact UI's render
# switch consumes the same shape ClaudeClaw uses.
CabinetEvent = dict[str, Any]


@dataclass
class ChannelEntry:
    """Port warroom-text-events.ts:47-51 ChannelEntry verbatim."""
    seq: int
    ts: int  # ms epoch
    event: CabinetEvent


class MeetingChannel:
    """Port warroom-text-events.ts:53-154 MeetingChannel verbatim.

    `emit(event)` returns the assigned seq, or -1 when the event's turnId
    has been marked finalized. `markTurnFinalized` caps to last 32 entries
    via FIFO. `since(seq)` returns entries strictly newer than `seq`.
    """

    FINALIZED_CAP: Final[int] = 32
    DEFAULT_MAX_BUFFER: Final[int] = 500

    # Default mirrors DEFAULT_MAX_BUFFER (literal inlined per Rule 1).
    def __init__(self, max_buffer: int = 500):
        self._seq = 0
        self._buffer: deque[ChannelEntry] = deque(maxlen=max_buffer)
        self.max_buffer = max_buffer
        self.last_activity_at = int(time.time() * 1000)
        self._finalized_turns: set[str] = set()
        self._finalized_turn_order: list[str] = []
        self._subscribers: list[asyncio.Queue[ChannelEntry]] = []

    def emit(self, event: CabinetEvent) -> int:
        """Port warroom-text-events.ts:78-94. Returns -1 on finalized-turn drop."""
        turn_id = event.get("turnId")
        if turn_id and turn_id in self._finalized_turns:
            return -1
        self._seq += 1
        entry = ChannelEntry(seq=self._seq, ts=int(time.time() * 1000), event=event)
        self._buffer.append(entry)
        self.last_activity_at = entry.ts
        for q in self._subscribers:
            try:
                q.put_nowait(entry)
            except asyncio.QueueFull:
                # Slow subscriber — drop the event for them; the live chunk
                # is best-effort and the durable transcript table is the
                # source of truth for replay.
                pass
        return self._seq

    def mark_turn_finalized(self, turn_id: str) -> None:
        """Port warroom-text-events.ts:99-107 — bounded FIFO cap 32."""
        if turn_id in self._finalized_turns:
            return
        self._finalized_turns.add(turn_id)
        self._finalized_turn_order.append(turn_id)
        while len(self._finalized_turn_order) > self.FINALIZED_CAP:
            evict = self._finalized_turn_order.pop(0)
            self._finalized_turns.discard(evict)

    def is_turn_finalized(self, turn_id: str) -> bool:
        """Port warroom-text-events.ts:112-114."""
        return turn_id in self._finalized_turns

    def since(self, since_seq: int) -> list[ChannelEntry]:
        """Port warroom-text-events.ts:117-123."""
        return [e for e in self._buffer if e.seq > since_seq]

    def oldest_seq(self) -> int:
        """Port warroom-text-events.ts:126-128."""
        return self._buffer[0].seq if self._buffer else 0

    def latest_seq(self) -> int:
        """Port warroom-text-events.ts:131-133."""
        return self._seq

    def subscribe(self, queue_size: int = 100) -> tuple[asyncio.Queue[ChannelEntry], Any]:
        """Port warroom-text-events.ts:136-140. Returns (queue, unsubscribe_fn).

        The Python port substitutes asyncio.Queue for EventEmitter. The
        unsubscribe callable removes the queue from the subscriber list
        and is idempotent.
        """
        self.last_activity_at = int(time.time() * 1000)
        q: asyncio.Queue[ChannelEntry] = asyncio.Queue(maxsize=queue_size)
        self._subscribers.append(q)

        def unsub() -> None:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

        return q, unsub

    def listener_count(self) -> int:
        """Port warroom-text-events.ts:143-145."""
        return len(self._subscribers)

    def close(self) -> None:
        """Port warroom-text-events.ts:148-153."""
        self._subscribers.clear()
        self._buffer.clear()
        self._finalized_turns.clear()
        self._finalized_turn_order.clear()


# ── Module-local registry — Rule 2 EXCEPTION ──────────────────────────────
# Same-process invariant gate: producer + subscriber must share THIS dict.
# This is a runtime registry of live in-memory objects, NOT a cache of
# resolved config state — Rule 2 forbids the latter, not the former.
_CHANNELS: dict[int, MeetingChannel] = {}


def get_channel(meeting_id: int) -> MeetingChannel:
    """Port warroom-text-events.ts:158-165 — lazy-create."""
    ch = _CHANNELS.get(meeting_id)
    if ch is None:
        ch = MeetingChannel()
        _CHANNELS[meeting_id] = ch
    return ch


def close_channel(meeting_id: int) -> None:
    """Port warroom-text-events.ts:167-173."""
    ch = _CHANNELS.pop(meeting_id, None)
    if ch is not None:
        ch.close()


def _reset_channels() -> None:
    """Port warroom-text-events.ts:176-179. @internal for tests."""
    for ch in list(_CHANNELS.values()):
        ch.close()
    _CHANNELS.clear()


# ── Idle eviction sweeper ────────────────────────────────────────────────
# Port warroom-text-events.ts:181-215 — 1h TTL on listener-less channels.
IDLE_TTL_S: Final[int] = 60 * 60
_sweep_task: asyncio.Task | None = None


def _sweep_idle_channels(now_ms: int | None = None) -> None:
    """Port warroom-text-events.ts:191-198. Synchronous helper for tests."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    evict_ids: list[int] = []
    for mid, ch in list(_CHANNELS.items()):
        if ch.listener_count() > 0:
            continue
        if (now_ms - ch.last_activity_at) < IDLE_TTL_S * 1000:
            continue
        evict_ids.append(mid)
    for mid in evict_ids:
        close_channel(mid)


async def _sweep_loop(interval_s: int) -> None:
    while True:
        await asyncio.sleep(interval_s)
        _sweep_idle_channels()


def start_channel_sweeper(interval_s: int = 10 * 60) -> None:
    """Port warroom-text-events.ts:200-206. Idempotent."""
    global _sweep_task
    if _sweep_task is not None and not _sweep_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — caller will start sweeper later.
        return
    _sweep_task = loop.create_task(_sweep_loop(interval_s))


def stop_channel_sweeper() -> None:
    """Port warroom-text-events.ts:208-210."""
    global _sweep_task
    if _sweep_task is not None:
        _sweep_task.cancel()
        _sweep_task = None


__all__ = [
    "CABINET_EVENT_TYPES",
    "CabinetEvent",
    "ChannelEntry",
    "IDLE_TTL_S",
    "MeetingChannel",
    "close_channel",
    "get_channel",
    "start_channel_sweeper",
    "stop_channel_sweeper",
]
