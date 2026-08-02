"""Realtime session tests — session.update shape + fake-WS connect/dispatch.

No pytest-asyncio in the sidecar venv: async flows run via ``asyncio.run``
inside sync tests. ``websockets.connect`` is monkeypatched with a queue-fed
fake so the whole handshake + event dispatch runs in-process.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

import realtime


# ---------------------------------------------------------------------------
# build_session_update
# ---------------------------------------------------------------------------


def test_build_session_update_shape() -> None:
    cfg = realtime.RealtimeConfig(
        token="t", instructions="be helpful", model="m-1", voice="cedar"
    )

    payload = realtime.build_session_update(cfg)

    assert payload["type"] == "session.update"
    session = payload["session"]
    assert session["type"] == "realtime"
    assert session["model"] == "m-1"
    assert session["instructions"] == "be helpful"
    assert session["output_modalities"] == ["audio"]
    audio_in = session["audio"]["input"]
    assert audio_in["format"] == {"type": "audio/pcm", "rate": 24000}
    assert audio_in["transcription"] == {"model": realtime.INPUT_TRANSCRIPTION_MODEL}
    turn = audio_in["turn_detection"]
    assert turn["type"] == "server_vad"
    assert turn["create_response"] is True
    assert turn["interrupt_response"] is True
    assert turn["threshold"] == 0.5
    assert turn["prefix_padding_ms"] == 300
    assert turn["silence_duration_ms"] == 500
    audio_out = session["audio"]["output"]
    assert audio_out["format"] == {"type": "audio/pcm", "rate": 24000}
    assert audio_out["voice"] == "cedar"


# ---------------------------------------------------------------------------
# Fake WebSocket
# ---------------------------------------------------------------------------


class _FakeWS:
    """Queue-fed websocket double: sent frames captured, incoming scripted."""

    def __init__(self, scripted: list[str]) -> None:
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue = asyncio.Queue()
        for item in scripted:
            self.incoming.put_nowait(item)
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def recv(self) -> str:
        item = await self.incoming.get()
        if item is None:
            raise RuntimeError("websocket closed")
        return item

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        while True:
            item = await self.incoming.get()
            if item is None:
                return
            yield item

    async def close(self) -> None:
        self.closed = True
        self.incoming.put_nowait(None)


def _patch_connect(monkeypatch: pytest.MonkeyPatch, ws: _FakeWS) -> dict:
    captured: dict = {}

    async def fake_connect(url, additional_headers=None, max_size=None):
        captured.update(url=url, headers=additional_headers, max_size=max_size)
        return ws

    monkeypatch.setattr(realtime.websockets, "connect", fake_connect)
    return captured


# ---------------------------------------------------------------------------
# RealtimeSession
# ---------------------------------------------------------------------------


def test_connect_handshake_and_event_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _FakeWS([
        json.dumps({"type": "session.created"}),
        json.dumps({"type": "session.updated"}),
    ])
    captured = _patch_connect(monkeypatch, ws)
    audio_chunks: list[bytes] = []
    transcripts: list[tuple[str, str, bool]] = []
    barges: list[bool] = []

    async def main() -> None:
        session = realtime.RealtimeSession(
            realtime.RealtimeConfig(
                token="tok", instructions="inst", model="gpt-realtime-2.1", voice="cedar"
            ),
            on_audio=audio_chunks.append,
            on_transcript=lambda role, text, final: transcripts.append((role, text, final)),
            on_barge_in=lambda: barges.append(True),
        )
        await session.connect()

        # Handshake: correct URL + auth header, session.update sent first.
        assert captured["url"] == realtime.REALTIME_WS_URL.format(model="gpt-realtime-2.1")
        assert captured["headers"]["Authorization"] == "Bearer tok"
        update = ws.sent[0]
        assert update["type"] == "session.update"
        assert update["session"]["model"] == "gpt-realtime-2.1"
        assert update["session"]["audio"]["output"]["voice"] == "cedar"

        # Audio append base64 roundtrip.
        pcm = b"\x00\x01" * 480
        await session.send_audio(pcm)
        append = ws.sent[1]
        assert append["type"] == "input_audio_buffer.append"
        assert base64.b64decode(append["audio"]) == pcm

        # Dispatch: audio delta -> on_audio bytes.
        ws.incoming.put_nowait(json.dumps({
            "type": "response.audio.delta",
            "delta": base64.b64encode(b"out-pcm").decode("ascii"),
        }))
        # Active response + speech_started -> barge-in.
        ws.incoming.put_nowait(json.dumps({"type": "response.created"}))
        ws.incoming.put_nowait(json.dumps({"type": "input_audio_buffer.speech_started"}))
        # Transcript events for both roles.
        ws.incoming.put_nowait(json.dumps({
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "hey homie",
        }))
        ws.incoming.put_nowait(json.dumps({
            "type": "response.audio_transcript.done",
            "transcript": "hello owner",
        }))
        # Response over -> speech_started does NOT barge in again.
        ws.incoming.put_nowait(json.dumps({"type": "response.done"}))
        ws.incoming.put_nowait(json.dumps({"type": "input_audio_buffer.speech_started"}))

        for _ in range(100):
            if len(audio_chunks) == 1 and len(transcripts) == 2 and len(barges) == 1:
                break
            await asyncio.sleep(0.01)

        await session.close()

    asyncio.run(main())

    assert audio_chunks == [b"out-pcm"]
    assert transcripts == [("user", "hey homie", True), ("assistant", "hello owner", True)]
    assert barges == [True]
    assert ws.closed is True


def test_speech_started_without_active_response_does_not_barge_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _FakeWS([
        json.dumps({"type": "session.created"}),
        json.dumps({"type": "session.updated"}),
    ])
    _patch_connect(monkeypatch, ws)
    barges: list[bool] = []

    async def main() -> None:
        session = realtime.RealtimeSession(
            realtime.RealtimeConfig(token="t", instructions="i"),
            on_audio=lambda _b: None,
            on_barge_in=lambda: barges.append(True),
        )
        await session.connect()
        ws.incoming.put_nowait(json.dumps({"type": "input_audio_buffer.speech_started"}))
        await asyncio.sleep(0.1)
        await session.close()

    asyncio.run(main())

    assert barges == []


def test_connect_rejects_bad_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _FakeWS([json.dumps({"type": "error", "error": {"message": "nope"}})])
    _patch_connect(monkeypatch, ws)

    async def main() -> None:
        session = realtime.RealtimeSession(
            realtime.RealtimeConfig(token="t", instructions="i"),
            on_audio=lambda _b: None,
        )
        with pytest.raises(realtime.RealtimeError, match="expected session.created"):
            await session.connect()

    asyncio.run(main())


def test_session_update_includes_tools_when_configured() -> None:
    config = realtime.RealtimeConfig(
        token="tok",
        instructions="inst",
        tools=[{"type": "function", "name": "memory_search", "parameters": {}}],
        tool_executor=lambda name, args: "ok",
    )

    update = realtime.build_session_update(config)

    assert update["session"]["tool_choice"] == "auto"
    assert update["session"]["tools"][0]["name"] == "memory_search"


def test_session_update_omits_tools_by_default() -> None:
    update = realtime.build_session_update(
        realtime.RealtimeConfig(token="tok", instructions="inst")
    )

    assert "tools" not in update["session"]
    assert "tool_choice" not in update["session"]


def test_function_call_executes_and_feeds_output(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _FakeWS([
        json.dumps({"type": "session.created"}),
        json.dumps({"type": "session.updated"}),
    ])
    _patch_connect(monkeypatch, ws)
    executed: list[tuple[str, dict]] = []

    def executor(name: str, arguments: dict) -> str:
        executed.append((name, arguments))
        return "Top 1 memory note: lane-first is the contract"

    async def main() -> None:
        session = realtime.RealtimeSession(
            realtime.RealtimeConfig(
                token="tok",
                instructions="inst",
                tools=[{"type": "function", "name": "memory_search"}],
                tool_executor=executor,
            ),
            on_audio=lambda chunk: None,
        )
        await session.connect()
        ws.incoming.put_nowait(json.dumps({
            "type": "response.function_call_arguments.done",
            "call_id": "call-1",
            "name": "memory_search",
            "arguments": '{"query": "lane-first"}',
        }))

        for _ in range(200):
            kinds = [e.get("type") for e in ws.sent]
            if "conversation.item.create" in kinds and "response.create" in kinds:
                break
            await asyncio.sleep(0.01)
        await session.close()

    asyncio.run(main())

    assert executed == [("memory_search", {"query": "lane-first"})]
    item = next(e for e in ws.sent if e.get("type") == "conversation.item.create")
    assert item["item"]["type"] == "function_call_output"
    assert item["item"]["call_id"] == "call-1"
    assert "lane-first is the contract" in item["item"]["output"]
    assert any(e.get("type") == "response.create" for e in ws.sent)


def test_function_call_failure_is_spoken_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _FakeWS([
        json.dumps({"type": "session.created"}),
        json.dumps({"type": "session.updated"}),
    ])
    _patch_connect(monkeypatch, ws)

    def executor(name: str, arguments: dict) -> str:
        raise RuntimeError("vault exploded")

    async def main() -> None:
        session = realtime.RealtimeSession(
            realtime.RealtimeConfig(
                token="tok",
                instructions="inst",
                tools=[{"type": "function", "name": "memory_search"}],
                tool_executor=executor,
            ),
            on_audio=lambda chunk: None,
        )
        await session.connect()
        ws.incoming.put_nowait(json.dumps({
            "type": "response.function_call_arguments.done",
            "call_id": "call-9",
            "name": "memory_search",
            "arguments": "not-json",
        }))

        for _ in range(200):
            if any(e.get("type") == "response.create" for e in ws.sent):
                break
            await asyncio.sleep(0.01)
        await session.close()

    asyncio.run(main())

    item = next(e for e in ws.sent if e.get("type") == "conversation.item.create")
    assert "vault exploded" in item["item"]["output"]


# ---------------------------------------------------------------------------
# async run polling (the sidecar half of the WORK_STARTED contract)
# ---------------------------------------------------------------------------


class _Recorder:
    """A session whose sends are captured instead of hitting a socket."""

    def __init__(self, config: realtime.RealtimeConfig) -> None:
        self.session = realtime.RealtimeSession(config, on_audio=lambda _b: None)
        self.sent: list[dict] = []

        async def _send(payload: dict) -> None:
            self.sent.append(payload)

        self.session._send = _send  # type: ignore[method-assign]


def test_sentinel_regex_matches_the_python_registry_format() -> None:
    match = realtime.WORK_STARTED_RE.search(
        "WORK_STARTED #12 kind=archon (archon-clutch) It's running now."
    )

    assert match is not None
    assert match.group(1) == "12"
    assert match.group(2) == "archon"


def test_run_poll_caps_cover_every_kind() -> None:
    # An Archon build must outlive a screen look, or results stop being spoken.
    assert realtime.RUN_POLL_CAPS_S["archon"] > realtime.RUN_POLL_CAPS_S["agent"]
    assert realtime.RUN_POLL_CAPS_S["agent"] > realtime.RUN_POLL_CAPS_S["skill"]
    assert realtime.RUN_POLL_CAPS_S["look"] < realtime.RUN_POLL_CAPS_S["skill"]


def test_finished_run_is_injected_for_speech(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(realtime, "RUN_POLL_INTERVAL_S", 0.0)
    reads: list[str] = []

    def reader(run_id: str) -> dict:
        reads.append(run_id)
        if len(reads) < 2:
            return {"status": "running", "output": ""}
        return {"status": "done", "output": "Vault digest: 3 notes.", "kind": "skill"}

    rec = _Recorder(
        realtime.RealtimeConfig(token="t", instructions="i", run_reader=reader)
    )

    async def drive() -> None:
        rec.session._watch_for_run("WORK_STARTED #7 kind=skill (vault-ops)")
        for _ in range(50):
            await asyncio.sleep(0)
            if rec.sent:
                break

    asyncio.run(drive())

    assert reads == ["7", "7"]  # kept polling while it ran
    item = rec.sent[0]["item"]
    assert item["role"] == "user"
    assert "Vault digest: 3 notes." in item["content"][0]["text"]
    assert "status 'done'" in item["content"][0]["text"]
    assert rec.sent[1] == {"type": "response.create"}


def test_no_polling_without_a_run_reader() -> None:
    rec = _Recorder(realtime.RealtimeConfig(token="t", instructions="i"))

    async def drive() -> None:
        rec.session._watch_for_run("WORK_STARTED #7 kind=skill (vault-ops)")
        await asyncio.sleep(0)

    asyncio.run(drive())

    assert rec.sent == []


def test_output_without_a_sentinel_starts_no_poll() -> None:
    called: list[str] = []
    rec = _Recorder(
        realtime.RealtimeConfig(
            token="t", instructions="i", run_reader=lambda rid: called.append(rid) or {}
        )
    )

    async def drive() -> None:
        rec.session._watch_for_run("Nothing on the calendar today.")
        await asyncio.sleep(0)

    asyncio.run(drive())

    assert called == []
    assert rec.sent == []


def test_transient_reader_failures_keep_watching(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(realtime, "RUN_POLL_INTERVAL_S", 0.0)
    attempts: list[int] = []

    def flaky(run_id: str) -> dict:
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("talk run API 503")
        return {"status": "failed", "output": "the lane died", "kind": "agent"}

    rec = _Recorder(
        realtime.RealtimeConfig(token="t", instructions="i", run_reader=flaky)
    )

    async def drive() -> None:
        rec.session._watch_for_run("WORK_STARTED #3 kind=agent (audit)")
        for _ in range(50):
            await asyncio.sleep(0)
            if rec.sent:
                break

    asyncio.run(drive())

    assert len(attempts) == 3  # two failures did not abort the watch
    assert "the lane died" in rec.sent[0]["item"]["content"][0]["text"]


def test_a_chained_run_keeps_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    """A skill that outgrows its budget hands off to an agent — follow it."""

    monkeypatch.setattr(realtime, "RUN_POLL_INTERVAL_S", 0.0)
    seen: list[str] = []

    def reader(run_id: str) -> dict:
        seen.append(run_id)
        if run_id == "7":
            return {
                "status": "failed",
                "kind": "skill",
                "output": "moved to a background agent. WORK_STARTED #8 kind=agent (skill continued)",
            }
        return {"status": "done", "output": "finished the audit", "kind": "agent"}

    rec = _Recorder(
        realtime.RealtimeConfig(token="t", instructions="i", run_reader=reader)
    )

    async def drive() -> None:
        rec.session._watch_for_run("WORK_STARTED #7 kind=skill (vault-ops)")
        for _ in range(80):
            await asyncio.sleep(0)
            if len(rec.sent) >= 4:
                break

    asyncio.run(drive())

    assert seen == ["7", "8"]
    assert "finished the audit" in rec.sent[2]["item"]["content"][0]["text"]


def test_watch_budget_expiry_stops_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(realtime, "RUN_POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(realtime, "RUN_POLL_CAPS_S", {"look": -1.0})

    rec = _Recorder(
        realtime.RealtimeConfig(
            token="t", instructions="i", run_reader=lambda rid: {"status": "running"}
        )
    )

    async def drive() -> None:
        rec.session._watch_for_run("WORK_STARTED #9 kind=look (screen)")
        for _ in range(10):
            await asyncio.sleep(0)

    asyncio.run(drive())

    assert rec.sent == []  # budget already spent — nothing injected
