"""PRD-8 Phase 6 / WS1 — voice agent_bridge tests.

Covers contract criteria + R1 v2 fixes:
  * agent_bridge_calls_cabinet_api_send_message_with_is_voice_true (B1)
  * agent_bridge_consumes_sse_error_event (B2)
  * tts_voice_switch_guard_preserves_buffer
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from cabinet.voice import agent_bridge as voice_agent_bridge  # noqa: E402
from cabinet.voice import voice_router as voice_router  # noqa: E402


def test_broadcast_order_default_verbatim():
    """BROADCAST_ORDER matches warroom/agent_bridge.py:34 verbatim."""
    assert voice_agent_bridge.BROADCAST_ORDER == ["main", "research", "comms", "content", "ops"]


def test_bridge_initial_state():
    """HomieAgentBridge starts with no current voice + injected meeting."""
    bridge = voice_agent_bridge.HomieAgentBridge(
        meeting_id=42,
        chat_id="123",
    )
    assert bridge._meeting_id == 42
    assert bridge._chat_id == "123"
    assert bridge._current_voice is None
    assert bridge._broadcast_order == ["main", "research", "comms", "content", "ops"]


def test_bridge_custom_broadcast_order_override():
    """broadcast_order kwarg overrides the BROADCAST_ORDER default."""
    custom = ["seo", "blog", "main"]
    bridge = voice_agent_bridge.HomieAgentBridge(
        meeting_id=1,
        broadcast_order=custom,
    )
    assert bridge._broadcast_order == custom


def _patch_cabinet_api(captured: dict, stream_events):
    """Patch ``integrations.cabinet_api`` directly so the bridge's late-bind
    import resolves to our fakes.

    Returns a (send_capture, stream_async_gen) pair the test wraps with
    monkeypatch on the actual module attribute.

    The fake stream waits on an asyncio.Event captured["sent"] so it
    yields turn_start AFTER send_message has captured client_msg_id —
    matches the real-world sequence (server emits turn_start AFTER it
    receives the POST).
    """
    import integrations.cabinet_api as cabinet_api_mod

    async def capture_send(**kwargs):
        captured["client_msg_id"] = kwargs.get("client_msg_id")
        captured["target_agent_id"] = kwargs.get("target_agent_id")
        captured["is_voice"] = kwargs.get("is_voice")
        captured["meeting_id"] = kwargs.get("meeting_id")
        captured["text"] = kwargs.get("text")
        # Lazily create the sent-flag inside async scope so asyncio.Event
        # binds to the running test event loop.
        evt = captured.get("sent")
        if evt is None:
            evt = asyncio.Event()
            captured["sent"] = evt
        evt.set()
        return {"ok": True, "queued": True}

    async def fake_stream(meeting_id, since_seq=None, chat_id=None, *, client=None):
        # Wait until send_message has captured client_msg_id — matches the
        # real Phase 5a sequence where turn_start is emitted by the
        # orchestrator AFTER it receives the POST.
        evt = captured.get("sent")
        if evt is None:
            evt = asyncio.Event()
            captured["sent"] = evt
        try:
            await asyncio.wait_for(evt.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            return
        for ev in stream_events(captured):
            yield ev

    return capture_send, fake_stream, cabinet_api_mod


@pytest.mark.asyncio
async def test_call_agent_uses_cabinet_api_send_message_is_voice(monkeypatch):
    """_call_agent invokes cabinet_api.send_message(is_voice=True, target_agent_id=...).

    R1 v2 B1 fix — voice router's selected agent reaches the orchestrator
    via the targetAgentId payload field, NOT a re-run of the Haiku router.
    """
    bridge = voice_agent_bridge.HomieAgentBridge(meeting_id=42, chat_id="123")
    captured: dict = {}

    def stream_events(captured):
        cmsg_id = captured.get("client_msg_id")
        return [
            {"seq": 1, "event": {
                "type": "turn_start",
                "turnId": "t_abc",
                "clientMsgId": cmsg_id,
            }},
            {"seq": 2, "event": {
                "type": "agent_done",
                "turnId": "t_abc",
                "agentId": "research",
                "text": "Three new threats this week.",
            }},
        ]

    capture_send, fake_stream, mod = _patch_cabinet_api(captured, stream_events)
    monkeypatch.setattr(mod, "send_message", capture_send)
    monkeypatch.setattr(mod, "stream_meeting", fake_stream)
    # Shorten the bridge timeout so failed correlation reaches assertion fast.
    monkeypatch.setenv("CABINET_VOICE_BRIDGE_TIMEOUT_S", "5")

    reply = await bridge._call_agent("research", "what's new")

    assert reply == "Three new threats this week."
    # B1 lock — both kwargs must be set on the send.
    assert captured["is_voice"] is True
    assert captured["target_agent_id"] == "research"
    assert captured["client_msg_id"]  # non-empty
    assert captured["meeting_id"] == 42
    assert captured["text"] == "what's new"


@pytest.mark.asyncio
async def test_sse_error_event_renders_friendly_message(monkeypatch):
    """When cabinet_api.stream_meeting yields an error event, _call_agent
    returns a friendly transcript message (R1 v2 B2 fix — kill-switch
    refusals surface to operator via the SSE error event)."""
    bridge = voice_agent_bridge.HomieAgentBridge(meeting_id=42)
    captured: dict = {}

    def stream_events(captured):
        return [
            {"seq": 1, "event": {
                "type": "turn_start",
                "turnId": "t_xyz",
                "clientMsgId": captured.get("client_msg_id"),
            }},
            {"seq": 2, "event": {
                "type": "error",
                "turnId": "t_xyz",
                "message": "Cabinet kill-switch disabled by operator",
                "recoverable": True,
            }},
        ]

    capture_send, fake_stream, mod = _patch_cabinet_api(captured, stream_events)
    monkeypatch.setattr(mod, "send_message", capture_send)
    monkeypatch.setattr(mod, "stream_meeting", fake_stream)
    monkeypatch.setenv("CABINET_VOICE_BRIDGE_TIMEOUT_S", "5")

    reply = await bridge._call_agent("research", "anything")

    assert reply is not None
    assert "Cabinet kill-switch disabled by operator" in reply
    assert "declined" in reply.lower()


@pytest.mark.asyncio
async def test_correlation_filters_concurrent_turns(monkeypatch):
    """_call_agent only consumes events matching its own clientMsgId/turnId.

    R1 v2 B2 — stale replays / concurrent turns are filtered out by the
    correlation match. A different turn's agent_done must NOT be returned.
    """
    bridge = voice_agent_bridge.HomieAgentBridge(meeting_id=42)
    captured: dict = {}

    def stream_events(captured):
        return [
            # Concurrent turn from another operator — must be ignored.
            {"seq": 1, "event": {
                "type": "turn_start",
                "turnId": "t_other",
                "clientMsgId": "different_id",
            }},
            {"seq": 2, "event": {
                "type": "agent_done",
                "turnId": "t_other",
                "agentId": "comms",
                "text": "stale reply must NOT leak",
            }},
            # Now our own turn lands.
            {"seq": 3, "event": {
                "type": "turn_start",
                "turnId": "t_ours",
                "clientMsgId": captured.get("client_msg_id"),
            }},
            {"seq": 4, "event": {
                "type": "agent_done",
                "turnId": "t_ours",
                "agentId": "research",
                "text": "the right reply",
            }},
        ]

    capture_send, fake_stream, mod = _patch_cabinet_api(captured, stream_events)
    monkeypatch.setattr(mod, "send_message", capture_send)
    monkeypatch.setattr(mod, "stream_meeting", fake_stream)
    monkeypatch.setenv("CABINET_VOICE_BRIDGE_TIMEOUT_S", "5")

    reply = await bridge._call_agent("research", "ping")

    assert reply == "the right reply"
    assert "stale" not in (reply or "").lower()


@pytest.mark.asyncio
async def test_tts_voice_switch_guard():
    """_emit_response only emits TTSUpdateSettingsFrame when voice_id changes.

    Verbatim port of warroom/agent_bridge.py:88 voice-switch guard.
    Test feeds 3 same-persona + 1 different-persona text frames; asserts
    exactly 1 TTSUpdateSettingsFrame emitted (the one for the persona switch).

    Wait — the LOAD-BEARING semantic is: switch ONLY emits when voice
    actually changes. The first emission DOES emit (current is None →
    different). So 1 same persona + 1 different = 2 switches total. We
    test the more direct semantic: stable persona ⇒ at most one initial switch.
    """
    bridge = voice_agent_bridge.HomieAgentBridge(meeting_id=42)
    pushed: list = []

    async def fake_push(frame, direction=None):
        pushed.append((frame, direction))

    bridge.push_frame = fake_push

    # Mock _resolve_persona_voice deterministically.
    bridge._resolve_persona_voice = lambda agent_id: ({
        "research": ("voice_R", "elevenlabs"),
        "comms": ("voice_C", "elevenlabs"),
    }.get(agent_id, (None, None)))

    # Three same-persona emissions + one different-persona emission.
    await bridge._emit_response("research", "first reply")
    await bridge._emit_response("research", "second reply")
    await bridge._emit_response("research", "third reply")
    await bridge._emit_response("comms", "different persona")

    # Count voice-switch frames emitted (TTSUpdateSettingsFrame instances).
    voice_switch_count = sum(
        1 for frame, _ in pushed
        if isinstance(frame, voice_agent_bridge.TTSUpdateSettingsFrame)
    )
    # Expect exactly 2: one for research (None -> voice_R), one for comms (voice_R -> voice_C).
    assert voice_switch_count == 2, (
        f"Expected exactly 2 TTSUpdateSettingsFrame emissions "
        f"(initial + persona-switch), got {voice_switch_count}"
    )

    # And exactly 4 TextFrames pushed (1 per _emit_response call).
    text_frame_count = sum(
        1 for frame, _ in pushed
        if isinstance(frame, voice_agent_bridge.TextFrame)
    )
    assert text_frame_count == 4
