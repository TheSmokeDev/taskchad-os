"""DAVE re-key self-heal tests (bridge.on_voice_state_update -> _heal_worker).

Root cause being defended: any membership change in the bot's voice channel
re-keys the E2EE group (new MLS epoch) and py-cord's receive path does not
survive the transition — the bot goes silently deaf (live session 2026-08-03
07:53: user joined 46s after the bot, epoch 1 fired, zero RTP decrypted ever
after). The heal reconnects the Discord voice transport so the group re-forms
with everyone present, keeping the OpenAI session and mic pump untouched.

r1 review contract locked here: cooldowns SLEEP (never drop a re-key), the
worker coalesces bursts, transport mutations serialize on _transport_lock,
pending playback is TRANSPLANTED into a fresh source (py-cord's AudioPlayer
calls source.cleanup() == flush when stop() ends), and other BOTS' membership
changes also trigger healing (only this client's own events are suppressed).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import bridge
from audio import QueueAudioSource


# ---------------------------------------------------------------------------
# membership_change_kind — the pure re-key trigger classifier
# ---------------------------------------------------------------------------


def test_user_joining_our_channel_is_a_join() -> None:
    assert bridge.VoiceBridge.membership_change_kind(False, None, 42, 42) == "join"


def test_user_leaving_our_channel_is_a_leave() -> None:
    assert bridge.VoiceBridge.membership_change_kind(False, 42, None, 42) == "leave"


def test_other_bot_joining_also_rekeys() -> None:
    # r1 finding 4: EVERY membership change re-keys the MLS group — another
    # voice bot entering must trigger healing too. Only OUR OWN client is
    # suppressed (member_is_self), not bots in general.
    assert bridge.VoiceBridge.membership_change_kind(False, None, 42, 42) == "join"


def test_our_own_move_is_self_move() -> None:
    assert bridge.VoiceBridge.membership_change_kind(True, 42, 99, 42) == "self_move"


def test_our_own_disconnect_is_self_gone() -> None:
    assert bridge.VoiceBridge.membership_change_kind(True, 42, None, 42) == "self_gone"


def test_mute_deafen_toggle_is_ignored() -> None:
    # Same channel before/after — a state flip, not a membership change.
    assert bridge.VoiceBridge.membership_change_kind(False, 42, 42, 42) is None
    assert bridge.VoiceBridge.membership_change_kind(True, 42, 42, 42) is None


def test_other_channels_are_ignored() -> None:
    assert bridge.VoiceBridge.membership_change_kind(False, 7, 9, 42) is None


def test_move_from_ours_to_other_channel_is_a_leave() -> None:
    assert bridge.VoiceBridge.membership_change_kind(False, 42, 9, 42) == "leave"


def test_not_connected_is_ignored_for_others() -> None:
    assert bridge.VoiceBridge.membership_change_kind(False, None, 42, None) is None


# ---------------------------------------------------------------------------
# Fakes — py-cord-faithful where it matters
# ---------------------------------------------------------------------------


class _FakeVoice:
    """Voice client double honoring py-cord's stop()->source.cleanup() contract."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.source = None

    def is_connected(self) -> bool:
        return True

    def is_dave_connection(self) -> bool:
        return True

    def stop_listening(self) -> None:
        self.calls.append("stop_listening")

    def stop(self) -> None:
        self.calls.append("stop")
        if self.source is not None:
            self.source.cleanup()  # py-cord AudioPlayer does this on stop

    async def disconnect(self, force: bool = False) -> None:
        self.calls.append("disconnect")

    def start_listening(self, sink) -> None:
        self.calls.append("start_listening")

    def play(self, source) -> None:
        self.calls.append("play")
        self.source = source


class _FakeChannel:
    def __init__(self, fail_connects: int = 0) -> None:
        self.connected: list[_FakeVoice] = []
        self._fail = fail_connects

    async def connect(self) -> _FakeVoice:
        if self._fail > 0:
            self._fail -= 1
            raise RuntimeError("connect refused")
        v = _FakeVoice()
        self.connected.append(v)
        return v


def _bridge_with(channel: _FakeChannel) -> bridge.VoiceBridge:
    vb = bridge.VoiceBridge.__new__(bridge.VoiceBridge)
    vb.voice = _FakeVoice()
    vb.guild_id = 1
    vb.channel_id = 42
    vb.playback = QueueAudioSource()
    vb.voice.source = vb.playback
    vb._on_mic_pcm = lambda *a, **k: None
    vb._transport_lock = asyncio.Lock()
    vb._heal_task = None
    vb._heal_count = 0
    vb._heal_failures = 0
    vb._last_heal_at = 0.0
    vb._rekey_gen = 0
    vb._healed_gen = 0
    vb._heal_streak = 0
    vb._churn_advised = False
    vb._fail_advised = False
    vb.mirrored: list[str] = []

    async def _mirror(text: str) -> None:
        vb.mirrored.append(text)

    vb._mirror_text = _mirror
    guild = SimpleNamespace(get_channel=lambda cid: channel if cid == 42 else None)
    vb.client = SimpleNamespace(get_guild=lambda gid: guild if gid == 1 else None)
    return vb


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def now(self) -> float:
        return self.t


def _patch_time(monkeypatch, clock: _Clock) -> None:
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float = 0.0, result=None):
        clock.t += max(0.0, float(delay))
        await real_sleep(0)
        return result

    monkeypatch.setattr(bridge, "_now", clock.now)
    monkeypatch.setattr(bridge, "_sleep", fake_sleep)


# ---------------------------------------------------------------------------
# _heal_worker — coalescing reconnect
# ---------------------------------------------------------------------------


def test_worker_heals_and_syncs_generation(monkeypatch) -> None:
    channel = _FakeChannel()
    clock = _Clock()

    async def run() -> bridge.VoiceBridge:
        vb = _bridge_with(channel)
        _patch_time(monkeypatch, clock)
        vb._rekey_gen = 1
        await vb._heal_worker()
        return vb

    vb = asyncio.run(run())

    assert vb._heal_count == 1
    assert vb._healed_gen == vb._rekey_gen == 1
    new_voice = channel.connected[0]
    assert "start_listening" in new_voice.calls
    assert "play" in new_voice.calls
    assert vb.voice is new_voice


def test_pending_playback_transplanted_not_flushed(monkeypatch) -> None:
    # r1 finding 3: py-cord's stop() calls source.cleanup() (== flush). The
    # heal must move pending PCM into a FRESH source BEFORE stopping the old
    # transport, or the current assistant reply is silently cut off.
    channel = _FakeChannel()
    clock = _Clock()
    pending = b"\x01\x02" * 4800

    async def run() -> bridge.VoiceBridge:
        vb = _bridge_with(channel)
        _patch_time(monkeypatch, clock)
        vb.playback.push(pending)
        old_playback = vb.playback
        vb._rekey_gen = 1
        await vb._heal_worker()
        assert vb.playback is not old_playback  # fresh source, not reused
        return vb

    vb = asyncio.run(run())

    assert vb.playback.drain_pending() == pending  # survived the flap
    assert channel.connected[0].source is vb.playback


def test_burst_of_rekeys_coalesces_into_one_reconnect(monkeypatch) -> None:
    channel = _FakeChannel()
    clock = _Clock()

    async def run() -> bridge.VoiceBridge:
        vb = _bridge_with(channel)

        real_sleep = asyncio.sleep
        bumps = iter([3, 5])  # more members arrive during each debounce

        async def fake_sleep(delay: float = 0.0, result=None):
            clock.t += max(0.0, float(delay))
            nxt = next(bumps, None)
            if nxt is not None:
                vb._rekey_gen = nxt
            await real_sleep(0)
            return result

        monkeypatch.setattr(bridge, "_now", clock.now)
        monkeypatch.setattr(bridge, "_sleep", fake_sleep)
        vb._rekey_gen = 1
        await vb._heal_worker()
        return vb

    vb = asyncio.run(run())

    assert vb._healed_gen == 5  # every event covered...
    assert vb._heal_count == 1  # ...by ONE reconnect


def test_cooldown_sleeps_never_drops(monkeypatch) -> None:
    # r1 finding 1: a re-key arriving inside the 10s cooldown must STILL be
    # healed (after the wait) — dropping it leaves the bot permanently deaf.
    channel = _FakeChannel()
    clock = _Clock()

    async def run() -> bridge.VoiceBridge:
        vb = _bridge_with(channel)
        _patch_time(monkeypatch, clock)
        vb._last_heal_at = clock.t - 2  # a heal ran 2s ago
        vb._rekey_gen = 1
        await vb._heal_worker()
        return vb

    vb = asyncio.run(run())

    assert vb._heal_count == 1  # healed anyway — after sleeping out the cooldown
    assert vb._healed_gen == 1


def test_join_covered_generation_makes_worker_noop(monkeypatch) -> None:
    # r1 finding 2: a fresh join() syncs the generations; a stale worker pass
    # must then do NOTHING instead of flapping the brand-new transport.
    channel = _FakeChannel()
    clock = _Clock()

    async def run() -> bridge.VoiceBridge:
        vb = _bridge_with(channel)
        _patch_time(monkeypatch, clock)
        vb._rekey_gen = vb._healed_gen = 4  # join() already covered everything
        await vb._heal_worker()
        return vb

    vb = asyncio.run(run())

    assert channel.connected == []
    assert vb._heal_count == 0


def test_churn_backoff_escalates_cooldown(monkeypatch) -> None:
    # K3 C1: a churning channel must converge on ~one reconnect per minute,
    # not flap every ~11.5s forever. With a streak of 6 the cooldown is 60s:
    # a heal 30s after the last one must SLEEP the remaining ~30s first.
    channel = _FakeChannel()
    clock = _Clock()

    async def run() -> tuple[bridge.VoiceBridge, float]:
        vb = _bridge_with(channel)
        _patch_time(monkeypatch, clock)
        vb._heal_streak = 6
        vb._last_heal_at = clock.t - 30  # last heal 30s ago
        start = clock.t
        vb._rekey_gen = 1
        await vb._heal_worker()
        return vb, start

    vb, start = asyncio.run(run())

    assert vb._heal_count == 1  # still healed — slept, never dropped
    assert clock.t - start >= 29  # ...but only after the escalated cooldown


def test_churn_advisory_fires_once_per_episode(monkeypatch) -> None:
    channel = _FakeChannel()
    clock = _Clock()

    async def run() -> bridge.VoiceBridge:
        vb = _bridge_with(channel)
        _patch_time(monkeypatch, clock)
        vb._heal_streak = 5
        vb._last_heal_at = clock.t - 5  # rapid cadence — streak will hit 6
        vb._rekey_gen = 1
        await vb._heal_worker()  # heal #1: streak -> 6, advisory fires
        vb._rekey_gen = 2
        await vb._heal_worker()  # heal #2: streak -> 7, advisory latched
        return vb

    vb = asyncio.run(run())

    assert vb._heal_count == 2
    assert vb._heal_streak == 7
    churn_msgs = [m for m in vb.mirrored if "churning" in m]
    assert len(churn_msgs) == 1  # once per episode, not per heal


def test_fail_advisory_latched_across_workers(monkeypatch) -> None:
    # K3 C4: a dead voice backend + busy channel = unlimited events, each
    # spawning a worker that fails 3x. The advisory must fire ONCE per
    # degraded episode, not once per worker.
    channel = _FakeChannel(fail_connects=99)
    clock = _Clock()

    async def run() -> bridge.VoiceBridge:
        vb = _bridge_with(channel)
        _patch_time(monkeypatch, clock)
        vb._rekey_gen = 1
        await vb._heal_worker()  # 3 failures -> advisory + latch
        vb._rekey_gen = 2
        await vb._heal_worker()  # more failures -> latched, silent
        return vb

    vb = asyncio.run(run())

    failing_msgs = [m for m in vb.mirrored if "keeps failing" in m]
    assert len(failing_msgs) == 1


def test_three_failures_stop_worker_and_mirror_advice(monkeypatch) -> None:
    channel = _FakeChannel(fail_connects=99)
    clock = _Clock()

    async def run() -> bridge.VoiceBridge:
        vb = _bridge_with(channel)
        _patch_time(monkeypatch, clock)
        vb._rekey_gen = 1
        await vb._heal_worker()
        return vb

    vb = asyncio.run(run())

    assert vb._heal_failures == 3
    assert vb._heal_count == 0
    assert any("re-sync keeps failing" in m for m in vb.mirrored)


# ---------------------------------------------------------------------------
# on_voice_state_update — kick teardown, move-following, echo suppression (r2)
# ---------------------------------------------------------------------------

_SELF_ID = 999


class _FakeSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _event_bridge(channel: _FakeChannel) -> bridge.VoiceBridge:
    vb = _bridge_with(channel)
    vb.client.user = SimpleNamespace(id=_SELF_ID)
    vb.session = _FakeSession()
    vb._mic_task = None
    vb.text_channel_id = None
    vb._playback_state = None
    vb.started_at = 1.0
    return vb


def _fire(vb, member_id: int, before_ch: int | None, after_ch: int | None):
    member = SimpleNamespace(id=member_id)
    before = SimpleNamespace(
        channel=SimpleNamespace(id=before_ch) if before_ch else None
    )
    after = SimpleNamespace(channel=SimpleNamespace(id=after_ch) if after_ch else None)
    return vb.on_voice_state_update(member, before, after)


def test_kick_tears_down_and_never_auto_rejoins(monkeypatch) -> None:
    # r2 finding 1: an external kick must fully tear down (session closed,
    # channel identity cleared), and a LATER membership event in the old
    # channel must not schedule a heal that rejoins a deliberately-removed bot.
    channel = _FakeChannel()
    clock = _Clock()

    async def run() -> bridge.VoiceBridge:
        vb = _event_bridge(channel)
        _patch_time(monkeypatch, clock)
        session = vb.session
        await _fire(vb, _SELF_ID, 42, None)  # kicked, no operation in flight
        assert session.closed
        assert vb.voice is None and vb.channel_id is None and vb.guild_id is None
        assert any("disconnected from the voice channel" in m for m in vb.mirrored)
        gen_before = vb._rekey_gen
        await _fire(vb, 5, None, 42)  # someone joins the OLD channel later
        assert vb._rekey_gen == gen_before  # no re-key counted
        assert vb._heal_task is None  # no worker spawned
        return vb

    vb = asyncio.run(run())
    assert channel.connected == []  # never reconnected


def test_own_disconnect_echo_during_operation_is_suppressed() -> None:
    # heal/join hold the lock while flapping the transport — their own
    # disconnect echo must not tear down the session mid-operation.
    channel = _FakeChannel()

    async def run() -> bridge.VoiceBridge:
        vb = _event_bridge(channel)
        async with vb._transport_lock:
            await _fire(vb, _SELF_ID, 42, None)
        return vb

    vb = asyncio.run(run())
    assert vb.session.closed is False  # untouched
    assert vb.channel_id == 42


def test_admin_move_mid_heal_is_followed_not_suppressed() -> None:
    # r2 finding 2: only the EXPECTED transition (back into our own channel)
    # is suppressed under the lock. A move to a DIFFERENT channel is a real
    # admin drag — follow it and re-key even mid-operation.
    channel = _FakeChannel()

    async def run() -> bridge.VoiceBridge:
        vb = _event_bridge(channel)
        async with vb._transport_lock:
            await _fire(vb, _SELF_ID, 42, 77)
        if vb._heal_task is not None:
            vb._heal_task.cancel()
        return vb

    vb = asyncio.run(run())
    assert vb.channel_id == 77  # followed the admin move
    assert vb._rekey_gen == 1  # and counted the re-key


def test_own_reconnect_echo_same_channel_is_suppressed() -> None:
    channel = _FakeChannel()

    async def run() -> bridge.VoiceBridge:
        vb = _event_bridge(channel)
        async with vb._transport_lock:
            await _fire(vb, _SELF_ID, None, 42)  # our own rejoin echo
        return vb

    vb = asyncio.run(run())
    assert vb._rekey_gen == 0  # not a re-key
    assert vb.channel_id == 42


def test_rekey_during_join_handshake_is_counted(monkeypatch) -> None:
    # r2 finding 3: join() publishes channel_id BEFORE awaiting connect(), so
    # a member joining during the handshake (voice still None) must bump the
    # generation — the trailing worker heals it right after join completes.
    channel = _FakeChannel()

    async def run() -> bridge.VoiceBridge:
        vb = _event_bridge(channel)
        vb.voice = None  # handshake in flight: identity published, no client yet
        await _fire(vb, 5, None, 42)
        if vb._heal_task is not None:
            vb._heal_task.cancel()
        return vb

    vb = asyncio.run(run())
    assert vb._rekey_gen == 1  # counted, not dropped


def test_reconnect_adopts_physical_channel_after_mid_heal_move(monkeypatch) -> None:
    # Rule 2: if the transport lands in a different channel than the cached
    # target (admin moved us while connect() was in flight), adopt where we
    # physically are instead of declaring success with stale routing.
    class _MovedVoice(_FakeVoice):
        channel = SimpleNamespace(id=77)

    class _MovedChannel(_FakeChannel):
        async def connect(self):
            v = _MovedVoice()
            self.connected.append(v)
            return v

    channel = _MovedChannel()
    clock = _Clock()

    async def run() -> bridge.VoiceBridge:
        vb = _bridge_with(channel)
        _patch_time(monkeypatch, clock)
        vb._rekey_gen = 1
        await vb._heal_worker()
        return vb

    vb = asyncio.run(run())
    assert vb._heal_count == 1
    assert vb.channel_id == 77  # physical truth adopted


def test_teardown_mid_worker_exits_cleanly(monkeypatch) -> None:
    channel = _FakeChannel()
    clock = _Clock()

    async def run() -> bridge.VoiceBridge:
        vb = _bridge_with(channel)
        _patch_time(monkeypatch, clock)
        vb.voice = None  # leave() tore down before the worker's pass
        vb._rekey_gen = 1
        await vb._heal_worker()
        return vb

    vb = asyncio.run(run())

    assert channel.connected == []
    assert vb._heal_count == 0
