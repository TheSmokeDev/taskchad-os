"""Discord voice bridge sidecar — main process.

py-cord client + aiohttp control server (127.0.0.1:7861). The main Homie
process (Discord adapter via the orchestration API) tells us which voice
channel to join; we stream mic audio to OpenAI Realtime and play the
responses back, with barge-in. Auth ordering comes from
``runtime.openai_platform_auth`` (configured key -> OPENAI_API_KEY -> Codex
OAuth); instructions come from ``talk_session.build_talk_instructions()``
(the same SOUL.md voice preamble as the /talk browser page).

Control surface (loopback only):
  GET  /status
  POST /join   {"guildId": int, "channelId": int, "textChannelId": int?}
  POST /leave
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

import discord
from aiohttp import web
from dotenv import load_dotenv

SIDECAR_DIR = Path(__file__).resolve().parent
MAIN_SCRIPTS_DIR = SIDECAR_DIR.parent
if str(MAIN_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(MAIN_SCRIPTS_DIR))

import patches  # noqa: E402
from audio import QueueAudioSource, pcm24mono_to_48stereo, pcm48stereo_to_24mono  # noqa: E402
from realtime import RealtimeConfig, RealtimeSession  # noqa: E402
from sinks import RealtimeSink  # noqa: E402
from transcript import TranscriptWriter  # noqa: E402

# Snapshot BEFORE load_dotenv(override=True): the transcript path is a
# lifecycle-owned spawn contract — a stray .env entry must not override it.
_TRANSCRIPT_PATH = os.environ.get("DISCORD_VOICE_TRANSCRIPT_PATH")

load_dotenv(MAIN_SCRIPTS_DIR / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
# Bring-up verbosity: full packet-path detail on the voice machinery and our
# own modules without gateway/http spam. Flip to INFO once stable.
for _name in ("discord_voice", "patches", "sinks", "realtime", "audio"):
    logging.getLogger(_name).setLevel(logging.DEBUG)
for _name in ("discord.voice", "discord.opus"):
    logging.getLogger(_name).setLevel(logging.DEBUG)
_log = logging.getLogger("discord_voice.bridge")

# Injectable pacing seam for the mic pump. The pump paces on a monotonic clock;
# tests drive a virtual clock by patching THESE module names instead of the
# global time.monotonic / asyncio.sleep — patching the globals would also warp
# asyncio's own event-loop timekeeping and the realtime run-poller's deadline
# (realtime.py time.monotonic), which leaked across tests as scheduling flake.
_now = time.monotonic
_sleep = asyncio.sleep

CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 7861
TALK_API_BASE = os.environ.get("DISCORD_VOICE_TALK_API", "http://127.0.0.1:4322")


def _api_headers() -> dict:
    """Loopback JSON headers, plus the bearer when the API runs in token mode."""

    headers = {"Content-Type": "application/json"}
    # The orchestration API enforces bearer equality on every non-exempt route
    # when a token is configured; without this the sidecar 401s in token mode.
    api_token = (os.environ.get("ORCHESTRATION_API_TOKEN") or "").strip()
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
    return headers


def _api_run_reader(run_id: str) -> dict:
    """Read one async run via the main process's /api/talk/runs/<id> route.

    Lets the sidecar speak skill/agent/archon/look results the same way the
    dashboard does, instead of hearing only the started receipt.
    """

    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        f"{TALK_API_BASE}/api/talk/runs/{run_id}",
        headers=_api_headers(),
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"talk run API {exc.code}: {body}") from exc
    return payload if isinstance(payload, dict) else {}


def _api_tool_executor(name: str, arguments: dict) -> str:
    """Execute a talk tool via the main process's /api/talk/tool route.

    The sidecar venv deliberately lacks the integrations/orchestration deps
    (py-cord shares the `discord` namespace with discord.py — the two can
    never cohabit). The main process owns the real tool surface; we relay
    over loopback with stdlib only.
    """

    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        f"{TALK_API_BASE}/api/talk/tool",
        data=json.dumps({"name": name, "arguments": arguments}).encode("utf-8"),
        headers=_api_headers(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"talk tool API {exc.code}: {body}") from exc
    output = payload.get("output")
    if not isinstance(output, str) or not output.strip():
        raise RuntimeError("talk tool API returned no output")
    return output


class VoiceBridge:
    """One Discord voice channel <-> one OpenAI Realtime session."""

    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        self.client = discord.Client(intents=intents)
        self.client.event(self.on_ready)

        self.voice: discord.VoiceClient | None = None
        self.session: RealtimeSession | None = None
        self.playback: QueueAudioSource | None = None
        self.guild_id: int | None = None
        self.channel_id: int | None = None
        self.text_channel_id: int | None = None
        self.auth_source: str | None = None
        self.started_at: float | None = None
        self._mic_task: asyncio.Task | None = None
        self._mic_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._mic_sent = 0
        self._playback_state = None  # ratecv state for the 24k->48k upsampler
        self._ready = asyncio.Event()
        # Vault-debrief transcript: disabled no-op when the lifecycle did
        # not pass a path (spike.py / manual runs keep working unchanged).
        self.transcript = TranscriptWriter(_TRANSCRIPT_PATH)

    # -- discord lifecycle ---------------------------------------------------

    async def on_ready(self) -> None:
        # py-cord's client.event maps the event name from the function's
        # __name__ — this must literally be called on_ready to fire.
        _log.info("logged in as %s", self.client.user)
        self._ready.set()

    async def start(self) -> None:
        asyncio.get_running_loop().create_task(self.client.start(self._token(), reconnect=True))
        await asyncio.wait_for(self._ready.wait(), timeout=30)

    @staticmethod
    def _token() -> str:
        token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("DISCORD_BOT_TOKEN not set in .claude/scripts/.env")
        return token

    # -- control handlers ------------------------------------------------------

    def status(self) -> dict:
        return {
            "ok": True,
            "connected": bool(self.voice and self.voice.is_connected()),
            "dave": bool(self.voice and self.voice.is_dave_connection()),
            "guildId": self.guild_id,
            "channelId": self.channel_id,
            "authSource": self.auth_source,
            "sessionActive": self.session is not None,
            "playing": bool(self.voice and self.voice.is_playing()),
            "playbackReads": self.playback.reads_served if self.playback else None,
            "playbackQueued": len(self.playback._queue) if self.playback else None,
            "rtAppends": self.session.appends_sent if self.session else None,
            "rtEvents": self.session.events_received if self.session else None,
            "micSent": self._mic_sent,
            "daveStats": dict(patches.DAVE_STATS),
            "uptimeS": round(time.time() - self.started_at, 1) if self.started_at else None,
        }

    async def rt_probe(self) -> dict:
        """Force a server response to distinguish dead-session vs VAD wedge."""

        if self.session is None:
            raise RuntimeError("no active realtime session")
        before = self.session.events_received
        await self.session._send(
            {
                "type": "response.create",
                "response": {"instructions": "Say OK in one short word."},
            }
        )
        await asyncio.sleep(5)
        return {
            "ok": True,
            "eventsBefore": before,
            "eventsAfter": self.session.events_received,
            "answered": self.session.events_received > before,
        }

    async def tone(self, seconds: float = 2.0) -> dict:
        """Push a test tone through the exact playback path (diagnostic)."""
        if self.playback is None:
            raise RuntimeError("no active playback — join first")
        import io
        import math
        import struct

        frames = int(48000 * seconds)
        buf = io.BytesIO()
        for i in range(frames):
            sample = int(0.25 * 32767 * math.sin(2 * math.pi * 440 * i / 48000))
            buf.write(struct.pack("<hh", sample, sample))
        self.playback.push(buf.getvalue())
        return {"ok": True, "pushedBytes": len(buf.getvalue()), "playing": self.voice.is_playing() if self.voice else None}

    async def join(self, guild_id: int, channel_id: int, text_channel_id: int | None) -> dict:
        if self.voice is not None and self.voice.is_connected():
            await self.leave()
        guild = self.client.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild else None
        if channel is None or not isinstance(channel, discord.VoiceChannel):
            raise RuntimeError(f"voice channel {channel_id} not found")

        self.voice = await channel.connect()
        for _ in range(30):
            if self.voice.is_connected():
                break
            await asyncio.sleep(0.5)
        self.guild_id, self.channel_id = guild_id, channel_id
        self.text_channel_id = text_channel_id
        _log.info(
            "joined #%s in %s (dave=%s)",
            channel.name, guild.name, self.voice.is_dave_connection(),
        )

        try:
            from runtime import openai_platform_auth
            import talk_session
            import talk_tools

            auth = openai_platform_auth.resolve_openai_platform_auth(
                configured_api_key=talk_session.talk_configured_api_key()
            )
            self.auth_source = auth.source
            self.session = RealtimeSession(
                RealtimeConfig(
                    token=auth.token,
                    instructions=talk_session.build_talk_instructions(),
                    model=talk_session.talk_openai_model(),
                    voice=talk_session.talk_openai_voice(),
                    tools=talk_tools.default_talk_tools(),
                    tool_executor=_api_tool_executor,
                    run_reader=_api_run_reader,
                ),
                on_audio=self._on_assistant_audio,
                on_transcript=self._on_transcript,
                on_barge_in=self._on_barge_in,
            )
            await self.session.connect()
        except Exception:
            # Roll back the voice connection we just made — a failed session
            # setup must not leave the bot parked in the channel.
            try:
                self.voice.stop_listening()
            except Exception:
                pass
            try:
                await self.voice.disconnect(force=True)
            except Exception:
                pass
            self.voice = None
            self.session = None
            self.guild_id = self.channel_id = self.text_channel_id = None
            raise
        self.started_at = time.time()
        # Rotate any leftover file to a unique .pending and start fresh —
        # channel-switch re-joins never touch the lifecycle, so this
        # rotation is what keeps the predecessor session sweepable.
        self.transcript.start(guild_id, channel_id)

        sink = RealtimeSink(self._on_mic_pcm)
        self.voice.start_listening(sink)
        self._mic_task = asyncio.create_task(self._pump_mic())

        self.playback = QueueAudioSource()
        self._playback_state = None
        self.voice.play(self.playback)
        _log.info("realtime session active (auth=%s)", self.auth_source)
        await self._mirror_text(f"Joined **#{channel.name}** — talk to me. (auth: {self.auth_source})")
        return self.status()

    async def leave(self) -> dict:
        await self._mirror_text("Leaving voice. Catch you later.")
        if self._mic_task is not None:
            self._mic_task.cancel()
            try:
                await self._mic_task
            except (asyncio.CancelledError, Exception):
                pass
            self._mic_task = None
        if self.voice is not None:
            try:
                self.voice.stop_listening()
            except Exception:
                pass
            try:
                self.voice.stop()
            except Exception:
                pass
        if self.session is not None:
            await self.session.close()
            self.session = None
        if self.voice is not None:
            try:
                await self.voice.disconnect(force=True)
            except Exception:
                pass
            self.voice = None
        self.playback = None
        self._playback_state = None
        self.started_at = None
        return self.status()

    # -- audio + transcript flow ------------------------------------------------

    def _on_mic_pcm(self, pcm48stereo: bytes, user_id: int | None, ssrc: int | None = None) -> None:
        """Sink callback (router thread) — hand off to the asyncio queue."""

        def _enqueue() -> None:
            try:
                self._mic_queue.put_nowait((pcm48stereo, ssrc))
            except asyncio.QueueFull:
                pass  # backpressure — drop by design, inside the loop

        try:
            self.client.loop.call_soon_threadsafe(_enqueue)
        except Exception as exc:  # noqa: BLE001 — surface the real failure
            _log.warning("mic handoff failed: %s: %s", type(exc).__name__, exc)

    async def _pump_mic(self) -> None:
        from collections import deque

        from audio import rms_dbfs

        _log.info("mic pump started")
        state = None
        speech_tail = 0
        current_ssrc: int | None = None
        # Debug tap: dump the exact pcm24 stream sent to OpenAI for offline
        # analysis (whisper/RMS). Enable with DISCORD_VOICE_DEBUG_PCM=<path>.
        tap_path = os.environ.get("DISCORD_VOICE_DEBUG_PCM", "").strip()
        tap = open(tap_path, "ab") if tap_path else None
        # Noise gate as a delay line, not a dropper. Two rules learned live:
        # never SKIP a chunk (skipping excises 20ms and splices speech into
        # loud unintelligible static — send true zeros instead), and never
        # open the gate on the onset chunk itself (plosives and fricatives
        # sit below the threshold — an 8-chunk lookahead ring flushes as
        # real audio the moment speech arrives, so onsets survive intact).
        # Third rule: the pump must be PACED. Discord trickles DTX
        # keepalives (~12/sec) when nobody talks, but server VAD measures
        # silence in audio time — an unpaced pump makes a 500ms silence gap
        # take seconds of wall time and glues separate sentences into one
        # giant turn. On starvation we synthesize zeros at realtime rate.
        silence_dbfs = float(os.environ.get("DISCORD_VOICE_SILENCE_DBFS", "-35"))
        lookahead = 8  # 160ms delay line; also the pre-roll depth
        hangover_chunks = 30  # ~600ms tail so mid-sentence pauses keep the gate open
        ring: deque[bytes] = deque()
        zeros24 = b"\x00" * 960  # 20ms of 24kHz mono s16
        self._mic_sent = 0
        self._mic_drops = 0  # frames shed to bound latency (observability)
        # Paced jitter buffer (fixes the 20ms mid-word zero-splices, 2026-08-02).
        # The old bare `wait_for(get, timeout=0.02)` RACED Discord's 20ms packet
        # cadence: any packet arriving even 1ms late timed out and injected a
        # zero frame INTO the middle of a word (71 such splices in one live
        # session shredded the operator's speech into empty transcripts). Now a
        # small input buffer (`jbuf`) absorbs arrival jitter — a late packet is
        # covered by a spare buffered frame instead of a zero — while a
        # monotonic clock keeps the emit rate at exactly one 20ms frame per 20ms
        # of wall time. Realtime pacing is preserved (OpenAI's server VAD
        # measures silence in audio time; an unpaced pump glues separate
        # sentences into one turn), so this fixes the splicing WITHOUT
        # regressing the pacing the old loop got right.
        jitter_frames = max(1, int(os.environ.get("DISCORD_VOICE_JITTER_FRAMES", "3")))
        # Latency ceiling: if the pump ever falls behind (a stalled send_audio,
        # a sustained arrival burst), drop the OLDEST buffered frames to stay
        # current rather than let latency grow unbounded — the standard realtime
        # choice (you can't play 2s-stale audio into a live call). Default ~500ms.
        jbuf_max = max(
            jitter_frames,
            int(os.environ.get("DISCORD_VOICE_JITTER_MAX_FRAMES", str(jitter_frames + 25))),
        )
        # Soft high-water (K3 design gate): a one-time arrival burst that pushes
        # the buffer above ~2x priming but below the hard ceiling would FREEZE
        # there — arrival rate == consumption rate, so depth never drains and
        # ~2x priming of latency sticks for the rest of the utterance, a gap the
        # overflow ceiling and the >200ms resync both miss. Depth alone is the
        # signal, so shed back to priming the moment we cross it (no clock event
        # needed). Caps the working set at ~120ms while still absorbing normal
        # jitter, and also bleeds off slow clock-skew drift 60ms-at-a-time
        # instead of a rare 500ms lurch. (Env-tunable; a huge value disables it,
        # which the gate-logic tests use to bulk-preload without shedding.)
        soft_high_water = max(
            jitter_frames,
            int(os.environ.get("DISCORD_VOICE_JITTER_SOFT_FRAMES", str(2 * jitter_frames))),
        )
        jbuf: deque = deque()
        primed = False
        next_frame_at: float | None = None
        resync_pending = False
        try:
            while True:
                # Drain every frame currently available (absorbs arrival bursts).
                while True:
                    try:
                        jbuf.append(self._mic_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                # Discontinuity trim (2026-08-02 review, r1+r2): a hard buffer
                # overflow OR a >200ms clock resync both mean the pump fell
                # behind and is now carrying stale backlog (a stalled send_audio
                # triggers both at once). Shed it down to the priming depth so
                # latency RECOVERS instead of persisting for the rest of the
                # utterance — the resync alone reset the clock but left the
                # backlog, which a 250ms stall measured as ~180ms->~410ms of
                # sticky latency. Reset ONLY the resampler (the drop is an input
                # discontinuity, so filter state is stale). Deliberately do NOT:
                #   - clear the ring — that emptied the delay line and the
                #     len(ring) >= lookahead guard then emitted NOTHING for the
                #     8-frame refill, a ~140ms hole in the paced timeline (r2-A);
                #     keeping it holds <=160ms of recent same-speaker audio and
                #     keeps every slot filled.
                #   - force the gate open — overflow does NOT imply speech (a
                #     stall during below-threshold ambient input would then leak
                #     600ms of noise into the VAD, r2-B). Preserving speech_tail
                #     keeps a mid-speech shed flowing (it was already open) while
                #     an ambient shed stays gated; the retained frames re-open
                #     the gate normally on the next above-threshold syllable.
                if resync_pending or len(jbuf) > soft_high_water or len(jbuf) > jbuf_max:
                    resync_pending = False
                    dropped = 0
                    while len(jbuf) > jitter_frames:
                        jbuf.popleft()
                        dropped += 1
                    if dropped:
                        self._mic_drops += dropped
                        state = None
                        _log.warning(
                            "mic pump: shed %d stale frame(s) to bound latency "
                            "(drops=%d)", dropped, self._mic_drops,
                        )
                # Prime to depth before draining so a single late/lost packet has
                # a spare frame to cover it; an underrun re-primes on next fill.
                if not primed and len(jbuf) >= jitter_frames:
                    primed = True
                if primed and jbuf:
                    pcm, ssrc = jbuf.popleft()
                else:
                    if primed:
                        primed = False  # buffer underran — concede one zero
                    pcm, ssrc = None, current_ssrc

                if self.session is not None:
                    if ssrc != current_ssrc:
                        # Resampler state is per-SSRC — a speaker change or SSRC
                        # re-negotiation must restart it or streams interleave
                        # into garbage (discord.js#11432).
                        state = None
                        ring.clear()
                        current_ssrc = ssrc
                    if pcm is None:
                        pcm24, level = zeros24, -96.0
                    else:
                        pcm24, state = pcm48stereo_to_24mono(pcm, state)
                        level = rms_dbfs(pcm24)
                    if level >= silence_dbfs:
                        speech_tail = hangover_chunks + lookahead
                    elif speech_tail > 0:
                        speech_tail -= 1
                    ring.append(pcm24)
                    if len(ring) >= lookahead:
                        out = ring.popleft() if speech_tail > 0 else zeros24
                        if speech_tail <= 0:
                            ring.popleft()  # discard gated chunk, keep the line moving
                        try:
                            if tap is not None:
                                tap.write(out)
                            await self.session.send_audio(out)
                            self._mic_sent += 1
                            if self._mic_sent % 250 == 1:
                                _log.info(
                                    "mic pump: sent=%d pcm24=%dB rms=%.1f dBFS "
                                    "gate=%s qsize=%d jbuf=%d drops=%d",
                                    self._mic_sent, len(out), level,
                                    "open" if speech_tail > 0 else "shut",
                                    self._mic_queue.qsize(), len(jbuf),
                                    self._mic_drops,
                                )
                        except Exception as exc:  # noqa: BLE001
                            _log.warning("mic pump send failed: %s", exc)
                            return

                # Realtime pace: hold each iteration to a 20ms wall-clock frame.
                now = _now()
                next_frame_at = (now if next_frame_at is None else next_frame_at) + 0.02
                delay = next_frame_at - now
                if delay > 0:
                    await _sleep(delay)
                elif delay < -0.2:
                    # Drifted too far behind (a stall). Resync the clock AND flag
                    # the next iteration to shed the buffered backlog the stall
                    # accumulated — otherwise latency stays doubled all utterance.
                    next_frame_at = now
                    resync_pending = True
        finally:
            if tap is not None:
                tap.close()

    def _on_assistant_audio(self, pcm24: bytes) -> None:
        if self.playback is None:
            return
        # Thread the ratecv state — restarting the upsampler at every chunk
        # boundary clicks in the bot's own voice.
        pcm48, self._playback_state = pcm24mono_to_48stereo(pcm24, self._playback_state)
        self.playback.push(pcm48)

    def _on_barge_in(self) -> None:
        _log.info("barge-in: flushing playback")
        if self.playback is not None:
            self.playback.flush()

    def _on_transcript(self, role: str, text: str, final: bool) -> None:
        _log.info("[%s] %s", role, text)
        if final:
            # Synchronous on the event loop by contract (see transcript.py)
            # — the vault-debrief row and the rotation in join() can never
            # interleave.
            self.transcript.append(role, text)
            asyncio.get_running_loop().create_task(
                self._mirror_text(f"**{'You' if role == 'user' else 'Homie'}:** {text}")
            )

    async def _mirror_text(self, content: str) -> None:
        if not self.text_channel_id:
            return
        channel = self.client.get_channel(self.text_channel_id)
        if channel is not None:
            try:
                await channel.send(content[:1900])
            except Exception as exc:  # noqa: BLE001
                _log.warning("text mirror failed: %s", exc)


# -- control server ------------------------------------------------------------


async def _run() -> None:
    bridge = VoiceBridge()
    await bridge.start()

    async def status(_req: web.Request) -> web.Response:
        return web.json_response(bridge.status())

    async def join(req: web.Request) -> web.Response:
        try:
            body = await req.json()
            result = await bridge.join(
                int(body["guildId"]),
                int(body["channelId"]),
                int(body["textChannelId"]) if body.get("textChannelId") else None,
            )
            return web.json_response(result)
        except Exception as exc:  # noqa: BLE001
            _log.warning("join failed: %s", exc)
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def leave(_req: web.Request) -> web.Response:
        result = await bridge.leave()
        return web.json_response(result)

    async def tone(_req: web.Request) -> web.Response:
        try:
            return web.json_response(await bridge.tone())
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def rt_probe(_req: web.Request) -> web.Response:
        try:
            return web.json_response(await bridge.rt_probe())
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    app = web.Application()
    app.add_routes(
        [
            web.get("/status", status),
            web.post("/join", join),
            web.post("/leave", leave),
            web.post("/tone", tone),
            web.post("/rt-probe", rt_probe),
        ]
    )
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, CONTROL_HOST, CONTROL_PORT).start()
    _log.info("control server on http://%s:%s", CONTROL_HOST, CONTROL_PORT)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass
    await stop.wait()
    _log.info("shutting down")
    await bridge.leave()
    await runner.cleanup()
    await bridge.client.close()


def main() -> None:
    patches.apply_patches()
    # Own our pid on disk so the lifecycle can always find and kill THIS
    # process — a zombie bridge squatting the control port silently hijacks
    # every later join (burned us live on 2026-07-24).
    (SIDECAR_DIR / "bridge.pid").write_text(str(os.getpid()), encoding="utf-8")
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
