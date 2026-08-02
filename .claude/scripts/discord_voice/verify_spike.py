"""Live DAVE-receive gate for the Discord voice sidecar.

Joins a voice channel with the receive patches applied, plays a test tone,
listens for N seconds with a RealtimeSink, then dumps the captured PCM to
a WAV and prints a verdict: packet counts, chunk sizes, and dBFS levels —
real speech shows sustained energy around -35..-15 dBFS; decode failure
shows silence (< -70 dBFS) or zero packets.

Run from the sidecar venv:
  cd .claude/scripts/discord_voice
  uv run python verify_spike.py --channel 1426978011877736522 --seconds 30
"""

from __future__ import annotations

import argparse
import asyncio
import io
import math
import os
import struct
import sys
import time
import wave
from pathlib import Path

import discord
from dotenv import load_dotenv

import patches
from audio import rms_dbfs
from sinks import RealtimeSink

MAIN_ENV = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(MAIN_ENV, override=True)

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
SAMPLE_RATE = 48_000
OUT_DIR = Path(__file__).resolve().parents[2] / ".tmp"


def _tone_pcm(seconds: float = 2.0, freq: float = 440.0) -> bytes:
    frames = int(SAMPLE_RATE * seconds)
    buf = io.BytesIO()
    for i in range(frames):
        sample = int(0.25 * 32767 * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
        buf.write(struct.pack("<hh", sample, sample))
    return buf.getvalue()


async def _run(client: discord.Client, channel_id: int, seconds: int) -> bool:
    channel = client.get_channel(channel_id)
    if channel is None:
        for guild in client.guilds:
            channel = guild.get_channel(channel_id)
            if channel is not None:
                break
    if channel is None or not isinstance(channel, discord.VoiceChannel):
        print(f"ERROR: voice channel {channel_id} not found", flush=True)
        return False

    print(f"joining #{channel.name} in {channel.guild.name} ...", flush=True)
    voice = await channel.connect()
    captured = bytearray()
    users: set[int] = set()

    def on_pcm(pcm: bytes, user_id: int | None) -> None:
        captured.extend(pcm)
        if user_id is not None:
            users.add(user_id)
        if sink.packets % 250 == 1:
            print(f"  [recv] packets={sink.packets} bytes={sink.bytes} users={len(users)}", flush=True)

    sink = RealtimeSink(on_pcm)
    try:
        for _ in range(30):
            if voice.is_connected():
                break
            await asyncio.sleep(0.5)
        print(
            f"connected={voice.is_connected()} is_dave_connection={voice.is_dave_connection()}",
            flush=True,
        )

        print("playing 2s test tone (send path)...", flush=True)
        voice.play(discord.PCMAudio(io.BytesIO(_tone_pcm())))
        while voice.is_playing():
            await asyncio.sleep(0.1)

        voice.start_listening(sink)
        print(f"listening for {seconds}s — TALK NOW ...", flush=True)
        await asyncio.sleep(seconds)
        try:
            voice.stop_listening()
        except Exception as exc:  # py-cord shutdown race — verdict still prints
            print(f"(stop_listening race ignored: {exc})", flush=True)
    finally:
        await voice.disconnect(force=False)

    OUT_DIR.mkdir(exist_ok=True)
    wav_path = OUT_DIR / f"discord-voice-verify-{int(time.time())}.wav"
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(bytes(captured))

    pcm = bytes(captured)
    mid = len(pcm) // 2
    print("=== verdict ===", flush=True)
    print(f"dave: {voice.is_dave_connection()}", flush=True)
    print(f"packets: {sink.packets}", flush=True)
    print(f"bytes: {sink.bytes}", flush=True)
    print(f"users heard: {len(users)}", flush=True)
    print(f"first-half dBFS: {rms_dbfs(pcm[:mid])}  second-half dBFS: {rms_dbfs(pcm[mid:])}", flush=True)
    print(f"wav: {wav_path}", flush=True)
    ok = sink.packets > 0 and rms_dbfs(pcm) > -70.0
    print(f"RECEIVE {'WORKS — DAVE receive patched' if ok else 'FAILED — silence or zero packets'}", flush=True)
    return ok


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", type=int, required=True)
    parser.add_argument("--seconds", type=int, default=30)
    args = parser.parse_args()

    if not TOKEN:
        print(f"ERROR: DISCORD_BOT_TOKEN not set (looked in {MAIN_ENV})")
        sys.exit(1)

    patches.apply_patches()

    intents = discord.Intents.none()
    intents.guilds = True
    intents.voice_states = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        print(f"logged in as {client.user}", flush=True)
        code = 0
        try:
            for _ in range(50):
                if client.guilds:
                    break
                await asyncio.sleep(0.2)
            ok = await _run(client, args.channel, args.seconds)
            code = 0 if ok else 1
        except Exception as exc:  # noqa: BLE001
            print(f"VERIFY ERROR: {type(exc).__name__}: {exc}", flush=True)
            code = 1
        finally:
            os._exit(code)

    client.run(TOKEN, reconnect=False)


if __name__ == "__main__":
    main()
