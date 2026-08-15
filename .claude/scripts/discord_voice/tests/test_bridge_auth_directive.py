"""Join-path auth behavior under the Codex billing directive.

`TALK_PREFER_CODEX_OAUTH` tells the voice surfaces to run off the ChatGPT
subscription instead of a metered API key. The bridge's whole job here is to
thread that directive into the resolver and let a refusal fail the join: it
must never fall back to a key on its own, because silently metering the
operator is the surprise the directive exists to prevent.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import bridge
import speaker_auth
import talk_session
from runtime import openai_platform_auth


class _FakeVoice:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.source = None

    def is_connected(self) -> bool:
        return True

    def is_dave_connection(self) -> bool:
        return True

    def is_playing(self) -> bool:
        return True

    def stop_listening(self) -> None:
        self.calls.append("stop_listening")

    def start_listening(self, sink) -> None:
        self.calls.append("start_listening")

    def play(self, source) -> None:
        self.calls.append("play")
        self.source = source

    async def disconnect(self, force: bool = False) -> None:
        self.calls.append("disconnect")


class _FakeChannel:
    name = "voice-lab"

    def __init__(self) -> None:
        self.connected: list[_FakeVoice] = []

    async def connect(self) -> _FakeVoice:
        v = _FakeVoice()
        self.connected.append(v)
        return v


class _FakeSession:
    """RealtimeSession double: records the token it was handed."""

    built: list["_FakeSession"] = []

    def __init__(self, config, **kwargs) -> None:
        self.token = config.token
        self.appends_sent = 0
        self.events_received = 0
        _FakeSession.built.append(self)

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch):
    """A VoiceBridge wired for join() with every network edge faked."""

    _FakeSession.built = []
    channel = _FakeChannel()
    monkeypatch.setattr(bridge.discord, "VoiceChannel", _FakeChannel)
    monkeypatch.setattr(bridge, "RealtimeSession", _FakeSession)
    monkeypatch.setattr(bridge, "RealtimeConfig", lambda **kw: SimpleNamespace(**kw))
    monkeypatch.setattr(bridge, "RealtimeSink", lambda cb: SimpleNamespace(cb=cb))
    monkeypatch.setattr(talk_session, "build_talk_instructions", lambda: "instructions")

    seen: dict = {}

    def resolve(**kwargs):
        seen.update(kwargs)
        if kwargs.get("prefer_codex"):
            return openai_platform_auth.OpenAIPlatformAuth(
                token="codex-token",
                source=openai_platform_auth.SOURCE_CODEX_OAUTH,
                detail="test",
            )
        return openai_platform_auth.OpenAIPlatformAuth(
            token="sk-env", source=openai_platform_auth.SOURCE_ENV, detail="test"
        )

    monkeypatch.setattr(openai_platform_auth, "resolve_openai_platform_auth", resolve)

    vb = bridge.VoiceBridge.__new__(bridge.VoiceBridge)
    vb.voice = None
    vb.session = None
    vb.playback = None
    vb._playback_state = None
    vb._mic_task = None
    vb._mic_queue = asyncio.Queue(maxsize=200)
    vb._mic_sent = 0
    vb._speaker_ledger = speaker_auth.DiscordSpeakerLedger()
    vb._heal_count = 0
    vb._heal_task = None
    vb._rekey_gen = 0
    vb._healed_gen = 0
    vb.guild_id = vb.channel_id = vb.text_channel_id = None
    vb.auth_source = None
    vb.started_at = None
    vb._transport_lock = asyncio.Lock()
    vb.transcript = SimpleNamespace(start=lambda *a: None)
    vb._on_mic_pcm = lambda *a, **k: None
    vb._on_assistant_audio = lambda *a, **k: None
    vb._on_transcript = lambda *a, **k: None
    vb._on_barge_in = lambda *a, **k: None

    async def _pump() -> None:
        return None

    vb._pump_mic = _pump

    async def _mirror(text: str) -> None:
        return None

    vb._mirror_text = _mirror
    guild = SimpleNamespace(
        name="lab", get_channel=lambda cid: channel if cid == 42 else None
    )
    vb.client = SimpleNamespace(get_guild=lambda gid: guild if gid == 1 else None)
    return SimpleNamespace(bridge=vb, channel=channel, seen=seen)


def test_the_bridge_threads_the_directive_and_rides_the_subscription(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TALK_PREFER_CODEX_OAUTH", "1")

    status = asyncio.run(harness.bridge.join(1, 42, None))

    assert harness.seen["prefer_codex"] is True
    assert [s.token for s in _FakeSession.built] == ["codex-token"]
    assert status["authSource"] == openai_platform_auth.SOURCE_CODEX_OAUTH


def test_the_bridge_leaves_the_directive_off_by_default(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TALK_PREFER_CODEX_OAUTH", raising=False)

    status = asyncio.run(harness.bridge.join(1, 42, None))

    assert harness.seen["prefer_codex"] is False
    assert [s.token for s in _FakeSession.built] == ["sk-env"]
    assert status["authSource"] == openai_platform_auth.SOURCE_ENV


def test_an_unusable_subscription_aborts_before_the_bot_ever_joins(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auth is resolved BEFORE the Discord handshake, so the expected refusal
    cannot park the bot in a voice channel — the rollback's disconnect is
    best-effort and would drop the handle even if it failed."""

    def refuse(**kwargs):
        raise openai_platform_auth.OpenAIPlatformAuthError(
            openai_platform_auth.PREFER_CODEX_UNAVAILABLE_MESSAGE
        )

    monkeypatch.setattr(openai_platform_auth, "resolve_openai_platform_auth", refuse)
    monkeypatch.setenv("TALK_PREFER_CODEX_OAUTH", "1")

    with pytest.raises(openai_platform_auth.OpenAIPlatformAuthError) as caught:
        asyncio.run(harness.bridge.join(1, 42, None))

    assert harness.channel.connected == [], "never joined voice at all"
    assert _FakeSession.built == [], "nothing was billed to a key"
    assert harness.bridge.session is None
    assert harness.bridge.voice is None
    assert harness.bridge.channel_id is None, "no half-published identity"
    assert "codex login" in str(caught.value)
    assert "TALK_PREFER_CODEX_OAUTH" in str(caught.value)
