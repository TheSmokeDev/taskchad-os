"""The speaker id must survive the audio path — it is authorization data.

A Discord voice channel is a room. `/talk join` is admin-gated, so the operator
who opened the session is an admin, but anyone else in the channel can talk to
the bot too. The sink knows who is speaking (`sinks.py` reads the real Discord
user off every packet); the bridge used to drop that id when queuing audio, so
the tool surface could only see the OPENER and every speaker inherited their
admin.

R7 BLOCKER — carrying the id was necessary but not sufficient. The bridge read
`_current_speaker` at the moment the function call was RELAYED, which is a
TOCTOU race: the model answers utterance U, but by the time it emits the call
another member's packet has already overwritten that value, and the call goes
out under THEIR id. A stranger could author a command and have an operator's
stray packet sign it. The fix (ported from hermes-talk's `talk_operator_auth`)
binds attribution to the VAD interval and correlates it by response id, so a
call resolves through `speaker_auth` and never through a live read.

These tests cover the SIDECAR half against the real bridge and the real
`RealtimeSession` dispatch. The bridge is a SEPARATE PROCESS from the one that
authorizes, so nothing it knows is in the authorizer's memory — the request is
the only thing that crosses. The allowlist half is proven in
`.claude/scripts/tests/test_ingress_role_seam.py`, and the two processes are
exercised together in
`.claude/scripts/tests/test_talk_two_process_authorization.py`.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import bridge
import realtime
import speaker_auth

OPERATOR = 555
STRANGER = 999


def _bridge_with_queue() -> bridge.VoiceBridge:
    """A VoiceBridge with only the mic plumbing — no discord.Client, no network."""
    inst = bridge.VoiceBridge.__new__(bridge.VoiceBridge)
    inst._mic_queue = asyncio.Queue(maxsize=200)
    inst._current_speaker = None
    inst._speaker_ledger = speaker_auth.DiscordSpeakerLedger()
    inst.client = SimpleNamespace(loop=SimpleNamespace(call_soon_threadsafe=lambda fn: fn()))
    return inst


class _CollectingWS:
    """Captures everything the session sends; never yields inbound events."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def last(self, etype: str) -> dict | None:
        for message in reversed(self.sent):
            if message.get("type") == etype:
                return message
        return None


def _session(inst: bridge.VoiceBridge, ws: _CollectingWS) -> realtime.RealtimeSession:
    """The REAL session object, wired to the REAL bridge executor and ledger."""
    session = realtime.RealtimeSession(
        realtime.RealtimeConfig(
            token="tok",
            instructions="inst",
            tool_executor=inst._bound_tool_executor,
            automatic_response=False,
        ),
        on_audio=lambda _pcm: None,
        ledger=inst._speaker_ledger,
    )
    session._ws = ws
    return session


def _pcm(ms: int) -> bytes:
    return bytes(ms * 24 * 2)


class _Clock:
    """Virtual pacing clock — same seam `test_bridge_gate.py` drives the pump on."""

    def __init__(self, t0: float = 1000.0) -> None:
        self.t = t0

    def monotonic(self) -> float:
        return self.t


def _make_fake_sleep(clock: _Clock, real_sleep):
    async def fake_sleep(delay: float = 0.0, result=None):
        clock.t += max(0.0, float(delay))
        await real_sleep(0)
        return result

    return fake_sleep


async def _speak(session, ws, user_id, *, item, start_ms, end_ms, response_id):
    """One complete utterance: audio, VAD boundary, commit, response bound."""
    await session.send_audio(_pcm(end_ms - start_ms), {"user_id": user_id})
    session._dispatch(
        {
            "type": "input_audio_buffer.speech_started",
            "item_id": item,
            "audio_start_ms": start_ms,
        }
    )
    session._dispatch(
        {
            "type": "input_audio_buffer.speech_stopped",
            "item_id": item,
            "audio_end_ms": end_ms,
        }
    )
    session._dispatch({"type": "input_audio_buffer.committed", "item_id": item})
    await asyncio.sleep(0)  # let the minted response.create flush to the ws
    create = ws.last("response.create")
    assert create is not None, "the commit must mint a response we control"
    session._dispatch(
        {
            "type": "response.created",
            "response": {"id": response_id, "metadata": create["response"]["metadata"]},
        }
    )


async def _call(session, *, response_id, call_id="c1", name="homie_command"):
    session._dispatch(
        {
            "type": "response.function_call_arguments.done",
            "response_id": response_id,
            "call_id": call_id,
            "name": name,
            "arguments": json.dumps({"command": "diagnostics", "args": ""}),
        }
    )
    for _ in range(6):  # drain the tool task + its to_thread hop
        await asyncio.sleep(0.01)


# -- R5/R6: the id reaches the wire at all -------------------------------------


def test_the_sink_callback_carries_the_speaker_into_the_queue() -> None:
    """The hop that used to discard it: `_on_mic_pcm` -> `_mic_queue`."""
    inst = _bridge_with_queue()

    inst._on_mic_pcm(b"\x00" * 8, 555, ssrc=42)

    pcm, ssrc, speaker = inst._mic_queue.get_nowait()
    assert pcm == b"\x00" * 8
    assert ssrc == 42
    assert speaker == 555


def test_two_speakers_stay_distinguishable_through_the_queue() -> None:
    """Interleaved speakers keep their own ids — the queue is per-packet, so a
    second speaker's audio can never arrive labelled as the first."""
    inst = _bridge_with_queue()

    inst._on_mic_pcm(b"\x01" * 8, 555, ssrc=1)
    inst._on_mic_pcm(b"\x02" * 8, 999, ssrc=2)

    assert [entry[2] for entry in (inst._mic_queue.get_nowait(), inst._mic_queue.get_nowait())] == [
        555,
        999,
    ]


def test_publish_speaker_records_bridge_state_not_a_shared_global() -> None:
    """R6: the speaker is BRIDGE state.

    It used to be written into this process's `talk_tools` globals, which the
    authorizing process never sees — the sidecar said viewer while the main
    process said admin. Keeping it on the instance makes the boundary obvious:
    the only way it reaches the authorizer is on the wire.
    """
    inst = _bridge_with_queue()

    inst._publish_speaker(999)

    assert inst._current_speaker == 999


# -- R7: attribution is bound to the utterance, not to the clock ---------------


def test_a_late_operator_packet_cannot_sign_a_strangers_command() -> None:
    """THE R7 BLOCKER, reproduced end to end and inverted.

    The stranger authors the utterance. Before the model's function call comes
    back, the operator's packet lands and overwrites `_current_speaker` — the
    exact interleaving the old relay read. The call must still resolve to its
    AUTHOR (the stranger, whom the main process then refuses), never to the
    operator whose audio merely arrived in between.
    """
    sent: list[dict] = []
    inst = _bridge_with_queue()
    ws = _CollectingWS()

    async def drive() -> None:
        session = _session(inst, ws)
        await _speak(session, ws, STRANGER, item="i1", start_ms=0, end_ms=20, response_id="resp_1")

        # The race: the operator speaks while the model is still composing.
        await session.send_audio(_pcm(20), {"user_id": OPERATOR})
        inst._publish_speaker(OPERATOR)

        await _call(session, response_id="resp_1")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            bridge,
            "_api_tool_executor",
            lambda name, arguments, speaker_id=None, binding=None: sent.append(
                {"speaker_id": speaker_id, "binding": binding}
            )
            or "ok",
        )
        asyncio.run(drive())

    assert len(sent) == 1
    assert sent[0]["speaker_id"] == STRANGER, (
        "the call was relayed under the operator's id — the TOCTOU race is back"
    )
    assert sent[0]["binding"].trusted is True


def test_two_speakers_inside_one_turn_relay_no_speaker_at_all() -> None:
    """When the utterance itself is ambiguous nobody gets to own it.

    A tie is refused rather than guessed: the wire carries no id, the main
    process resolves `viewer`, and the operator simply re-asks in a clean turn.
    """
    sent: list[dict] = []
    inst = _bridge_with_queue()
    ws = _CollectingWS()

    async def drive() -> None:
        session = _session(inst, ws)
        await session.send_audio(_pcm(20), {"user_id": STRANGER})
        await session.send_audio(_pcm(20), {"user_id": OPERATOR})
        session._dispatch(
            {"type": "input_audio_buffer.speech_started", "item_id": "i1", "audio_start_ms": 0}
        )
        session._dispatch(
            {"type": "input_audio_buffer.speech_stopped", "item_id": "i1", "audio_end_ms": 40}
        )
        session._dispatch({"type": "input_audio_buffer.committed", "item_id": "i1"})
        await asyncio.sleep(0)
        create = ws.last("response.create")
        session._dispatch(
            {
                "type": "response.created",
                "response": {"id": "resp_1", "metadata": create["response"]["metadata"]},
            }
        )
        await _call(session, response_id="resp_1")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            bridge,
            "_api_tool_executor",
            lambda name, arguments, speaker_id=None, binding=None: sent.append(
                {"speaker_id": speaker_id, "binding": binding}
            )
            or "ok",
        )
        asyncio.run(drive())

    assert sent[0]["speaker_id"] is None
    assert sent[0]["binding"].trusted is False
    assert sent[0]["binding"].reason == "ambiguous speakers"


def test_the_operators_own_clean_turn_still_resolves_to_them() -> None:
    """Fail-closed must not mean fail-always: the ordinary case still works."""
    sent: list[dict] = []
    inst = _bridge_with_queue()
    ws = _CollectingWS()

    async def drive() -> None:
        session = _session(inst, ws)
        await _speak(session, ws, OPERATOR, item="i1", start_ms=0, end_ms=20, response_id="resp_1")
        await _call(session, response_id="resp_1")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            bridge,
            "_api_tool_executor",
            lambda name, arguments, speaker_id=None, binding=None: sent.append(
                {"speaker_id": speaker_id, "binding": binding}
            )
            or "ok",
        )
        asyncio.run(drive())

    assert sent[0]["speaker_id"] == OPERATOR
    assert sent[0]["binding"].trusted is True


def test_a_call_on_an_unminted_response_relays_no_speaker() -> None:
    """A response we never minted carries no token, so it borrows nobody —
    including whoever happens to be mid-sentence at the time."""
    sent: list[dict] = []
    inst = _bridge_with_queue()
    ws = _CollectingWS()

    async def drive() -> None:
        session = _session(inst, ws)
        await _speak(session, ws, OPERATOR, item="i1", start_ms=0, end_ms=20, response_id="resp_1")
        session._dispatch({"type": "response.created", "response": {"id": "resp_ghost"}})
        await _call(session, response_id="resp_ghost")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            bridge,
            "_api_tool_executor",
            lambda name, arguments, speaker_id=None, binding=None: sent.append(
                {"speaker_id": speaker_id, "binding": binding}
            )
            or "ok",
        )
        asyncio.run(drive())

    assert sent[0]["speaker_id"] is None
    assert sent[0]["binding"].trusted is False


def test_the_tool_continuation_carries_the_binding_forward() -> None:
    """The response that speaks the tool result stays attributable, so a second
    call in the same breath does not silently lose the speaker."""
    inst = _bridge_with_queue()
    ws = _CollectingWS()

    async def drive() -> None:
        session = _session(inst, ws)
        await _speak(session, ws, OPERATOR, item="i1", start_ms=0, end_ms=20, response_id="resp_1")
        await _call(session, response_id="resp_1")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            bridge,
            "_api_tool_executor",
            lambda name, arguments, speaker_id=None, binding=None: "ok",
        )
        asyncio.run(drive())

    continuation = ws.last("response.create")
    assert continuation["response"]["metadata"][speaker_auth.BINDING_METADATA_KEY]


def test_the_pump_labels_gated_frames_as_silence_not_as_the_speaker() -> None:
    """The pump's own attribution contract.

    Gated frames are synthesized zeros — silence, not a person. If they were
    labelled with the current speaker, a pause would extend that speaker's
    claim over audio they did not produce; if they were labelled unresolved,
    every pause would taint the turn around it.
    """
    speakers: list[object] = []
    clock = _Clock()
    real_sleep = asyncio.sleep

    async def run() -> None:
        vb = _bridge_with_queue()
        vb._mic_sent = 0
        vb._mic_drops = 0

        async def send_audio(chunk: bytes, speaker: dict | None = None) -> None:
            speakers.append(speaker)

        vb.session = SimpleNamespace(send_audio=send_audio)
        task = asyncio.create_task(vb._pump_mic())
        await real_sleep(0)
        loud = b"\x40\x40" * 480
        quiet = bytes(1920)
        for _ in range(20):  # speech
            vb._mic_queue.put_nowait((loud, 1, OPERATOR))
        for _ in range(60):  # long enough to outlast the hangover
            vb._mic_queue.put_nowait((quiet, 1, OPERATOR))
        for _ in range(200_000):
            if vb._mic_sent >= 70 or (clock.t - 1000.0) >= 120.0:
                break
            await real_sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DISCORD_VOICE_JITTER_MAX_FRAMES", "100000")
        mp.setenv("DISCORD_VOICE_JITTER_SOFT_FRAMES", "100000")
        mp.setattr(bridge, "_now", clock.monotonic)
        mp.setattr(bridge, "_sleep", _make_fake_sleep(clock, real_sleep))
        asyncio.run(run())

    assert {"user_id": OPERATOR} in speakers, "open-gate frames must name the speaker"
    assert None in speakers, "gated frames must be recorded as silence"
    assert all(s is None or s == {"user_id": OPERATOR} for s in speakers)
