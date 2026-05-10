"""PRD-8 Phase 6 / WS1 — voice_router.AgentRouter port tests.

Covers contract criteria:
  * agent_router_routing_precedence_chain_verbatim
  * interim_transcription_frame_dropped
  * broadcast_triggers_set_verbatim
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from cabinet.voice import voice_router  # noqa: E402


# ─── Module-level invariants ─────────────────────────────────────────────


def test_broadcast_triggers_verbatim():
    """voice_router.BROADCAST_TRIGGERS == verbatim port from warroom/router.py:54-57."""
    expected = {
        "everyone",
        "all",
        "team",
        "standup",
        "status update",
        "status report",
    }
    assert voice_router.BROADCAST_TRIGGERS == expected


def test_default_agent_names_verbatim():
    """_DEFAULT_AGENT_NAMES matches warroom/router.py:46 verbatim."""
    expected = frozenset({"main", "research", "comms", "content", "ops"})
    assert voice_router._DEFAULT_AGENT_NAMES == expected


def test_pin_path_renamed_per_translation_boundary():
    """PIN_PATH renamed from /tmp/warroom-pin.json -> /tmp/cabinet-voice-pin.json."""
    pin_path_str = str(voice_router.PIN_PATH)
    assert "cabinet-voice-pin.json" in pin_path_str
    assert "warroom-pin.json" not in pin_path_str


def test_roster_path_renamed_per_translation_boundary():
    """ROSTER_PATH renamed from /tmp/warroom-agents.json -> /tmp/cabinet-roster.json."""
    roster_path_str = str(voice_router.ROSTER_PATH)
    assert "cabinet-roster.json" in roster_path_str
    assert "warroom-agents.json" not in roster_path_str


# ─── _build_agent_pattern — verbatim shape ─────────────────────────────────


def test_build_agent_pattern_longest_first_match():
    """Longest names sort first so "researcher" doesn't match "research" prefix."""
    names = {"r", "research", "comms"}
    pattern = voice_router._build_agent_pattern(names)
    # Longest match wins.
    m = pattern.match("research, summarize this")
    assert m is not None
    assert m.group(1).lower() == "research"


def test_build_agent_pattern_greeting_prefixes():
    """_GREETING_PREFIXES matches optional hey/yo/ok/okay/alright."""
    names = {"main", "research"}
    pattern = voice_router._build_agent_pattern(names)
    for prefix in ("hey", "yo", "ok", "okay", "alright", ""):
        msg = f"{prefix} research, what's new"
        m = pattern.match(msg)
        assert m is not None, f"Failed to match prefix={prefix!r}"
        assert m.group(1).lower() == "research"


# ─── AgentRouter routing precedence + interim drop ─────────────────────────


class _StubFrameProcessor:
    """Minimal stub recording every push_frame call."""

    def __init__(self) -> None:
        self.pushed: list[tuple[object, object]] = []

    async def push_frame(self, frame, direction=None) -> None:
        self.pushed.append((frame, direction))


@pytest.mark.asyncio
async def test_interim_dropped(monkeypatch):
    """InterimTranscriptionFrame must NOT be pushed downstream."""
    router = voice_router.AgentRouter()

    # Patch the parent push_frame to record calls.
    pushed: list = []

    async def fake_push(frame, direction=None):
        pushed.append((frame, direction))

    monkeypatch.setattr(router, "push_frame", fake_push)
    # The class-level FrameProcessor.process_frame is a no-op stub when
    # pipecat is not installed; for tests we exercise the router's own logic.

    # Build an interim frame stub.
    interim = voice_router.InterimTranscriptionFrame()
    await router.process_frame(interim, voice_router.FrameDirection.DOWNSTREAM)

    # Interim must be silently dropped — NO push_frame call.
    assert pushed == []


@pytest.mark.asyncio
async def test_broadcast_trigger_emits_route_frame(monkeypatch):
    """A broadcast trigger word produces AgentRouteFrame(agent_id='all', mode='broadcast')."""
    router = voice_router.AgentRouter()
    pushed: list = []

    async def fake_push(frame, direction=None):
        pushed.append((frame, direction))

    monkeypatch.setattr(router, "push_frame", fake_push)

    # Build a final TranscriptionFrame with broadcast text.
    frame = voice_router.TranscriptionFrame()
    frame.text = "everyone, status update please"
    await router.process_frame(frame, voice_router.FrameDirection.DOWNSTREAM)

    # Should have pushed exactly one AgentRouteFrame with agent_id='all'.
    assert len(pushed) == 1
    pushed_frame, _ = pushed[0]
    assert isinstance(pushed_frame, voice_router.AgentRouteFrame)
    assert pushed_frame.agent_id == "all"
    assert pushed_frame.mode == "broadcast"


@pytest.mark.asyncio
async def test_name_prefix_emits_single_route_frame(monkeypatch):
    """A name-prefixed utterance routes to the named agent in single mode."""
    router = voice_router.AgentRouter()
    pushed: list = []

    async def fake_push(frame, direction=None):
        pushed.append((frame, direction))

    monkeypatch.setattr(router, "push_frame", fake_push)

    frame = voice_router.TranscriptionFrame()
    frame.text = "research, summarize the latest threat report"
    await router.process_frame(frame, voice_router.FrameDirection.DOWNSTREAM)

    assert len(pushed) == 1
    f, _ = pushed[0]
    assert isinstance(f, voice_router.AgentRouteFrame)
    assert f.agent_id == "research"
    assert f.mode == "single"
    assert "summarize" in f.message.lower()


@pytest.mark.asyncio
async def test_default_main_emits_single_route_frame(monkeypatch):
    """When no broadcast/prefix/pin matches, route to main."""
    router = voice_router.AgentRouter()
    pushed: list = []

    async def fake_push(frame, direction=None):
        pushed.append((frame, direction))

    monkeypatch.setattr(router, "push_frame", fake_push)
    # Stub _get_pinned_agent to return None (no pin file).
    monkeypatch.setattr(router, "_get_pinned_agent", lambda: None)

    frame = voice_router.TranscriptionFrame()
    frame.text = "what's the weather"
    await router.process_frame(frame, voice_router.FrameDirection.DOWNSTREAM)

    assert len(pushed) == 1
    f, _ = pushed[0]
    assert f.agent_id == "main"
    assert f.mode == "single"


@pytest.mark.asyncio
async def test_pinned_routes_to_pinned_when_no_other_match(monkeypatch):
    """Pinned agent overrides default fallback (but not broadcast/prefix)."""
    router = voice_router.AgentRouter()
    pushed: list = []

    async def fake_push(frame, direction=None):
        pushed.append((frame, direction))

    monkeypatch.setattr(router, "push_frame", fake_push)
    monkeypatch.setattr(router, "_get_pinned_agent", lambda: "comms")

    frame = voice_router.TranscriptionFrame()
    frame.text = "what's the weather"
    await router.process_frame(frame, voice_router.FrameDirection.DOWNSTREAM)

    assert len(pushed) == 1
    f, _ = pushed[0]
    assert f.agent_id == "comms"
    assert f.mode == "single"


@pytest.mark.asyncio
async def test_broadcast_beats_pinned(monkeypatch):
    """Broadcast trigger wins even when an agent is pinned."""
    router = voice_router.AgentRouter()
    pushed: list = []

    async def fake_push(frame, direction=None):
        pushed.append((frame, direction))

    monkeypatch.setattr(router, "push_frame", fake_push)
    monkeypatch.setattr(router, "_get_pinned_agent", lambda: "comms")

    frame = voice_router.TranscriptionFrame()
    frame.text = "team, what's our standup"
    await router.process_frame(frame, voice_router.FrameDirection.DOWNSTREAM)

    f, _ = pushed[0]
    assert f.agent_id == "all"
    assert f.mode == "broadcast"


@pytest.mark.asyncio
async def test_name_prefix_beats_pinned(monkeypatch):
    """Explicit name prefix wins even when another agent is pinned."""
    router = voice_router.AgentRouter()
    pushed: list = []

    async def fake_push(frame, direction=None):
        pushed.append((frame, direction))

    monkeypatch.setattr(router, "push_frame", fake_push)
    monkeypatch.setattr(router, "_get_pinned_agent", lambda: "comms")

    frame = voice_router.TranscriptionFrame()
    frame.text = "hey research, look at this"
    await router.process_frame(frame, voice_router.FrameDirection.DOWNSTREAM)

    f, _ = pushed[0]
    assert f.agent_id == "research"


# ─── Roster mtime cache + JSON parse defenses ─────────────────────────────


def test_refresh_skips_when_file_missing(tmp_path, monkeypatch):
    """_refresh_agent_names_from_roster handles missing roster gracefully."""
    fake_roster = tmp_path / "missing.json"
    monkeypatch.setattr(voice_router, "ROSTER_PATH", fake_roster)
    # Snapshot AGENT_NAMES; refresh shouldn't mutate it when the file is missing.
    snapshot = set(voice_router.AGENT_NAMES)
    voice_router._refresh_agent_names_from_roster()
    assert voice_router.AGENT_NAMES == snapshot


def test_refresh_handles_malformed_json(tmp_path, monkeypatch):
    """Malformed JSON falls back to cached AGENT_NAMES (not crash)."""
    bad = tmp_path / "bad-roster.json"
    bad.write_text("{not valid json")
    monkeypatch.setattr(voice_router, "ROSTER_PATH", bad)
    monkeypatch.setattr(voice_router, "_roster_mtime", 0.0)  # force re-read
    snapshot = set(voice_router.AGENT_NAMES)
    voice_router._refresh_agent_names_from_roster()
    # Cached names preserved.
    assert voice_router.AGENT_NAMES == snapshot
