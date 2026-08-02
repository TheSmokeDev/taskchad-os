"""Spike: prove py-cord + DAVE voice on owner's Discord guild.

Modes:
  --list                Print guilds, voice channels, and who is inside.
  --channel ID [--seconds N]
                        Join the voice channel, assert the DAVE handshake,
                        play a 2s test tone (send path), then listen for N
                        seconds counting decoded PCM packets per user
                        (receive path), and print a verdict report.

Run from the sidecar venv:
  cd .claude/scripts/discord_voice
  uv run python spike.py --list
  uv run python spike.py --channel 1234567890 --seconds 20
"""

from __future__ import annotations

import argparse
import asyncio
import io
import math
import os
import struct
import sys
from pathlib import Path

import discord
from discord.sinks.core import Sink
from dotenv import load_dotenv

MAIN_ENV = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(MAIN_ENV, override=True)

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
SAMPLE_RATE = 48_000


class CountingSink(Sink):
    """Per-packet receive probe: counts decoded PCM chunks per user.

    py-cord 2.8's receive machinery expects a composite-sink surface that
    the shipped Sink base never got (half-landed rewrite): walk_children,
    root, and __sink_listeners__. We provide the minimal leaf-sink surface;
    packets reach write() via PacketRouter regardless.
    """

    __sink_listeners__: list = []

    def __init__(self) -> None:
        super().__init__()
        self.root = self
        self.packets = 0
        self.bytes = 0
        self.users: set[int] = set()
        self.first_len: int | None = None

    def is_opus(self) -> bool:
        """PacketDecoder contract: False = decode opus to PCM before write()."""

        return False

    def walk_children(self, with_self: bool = False):
        return [self] if with_self else []

    def write(self, data: bytes, user: int) -> None:
        self.packets += 1
        self.bytes += len(data)
        self.users.add(user)
        if self.first_len is None:
            self.first_len = len(data)
        if self.packets % 250 == 1:
            print(f"  [recv] packets={self.packets} bytes={self.bytes} users={len(self.users)}", flush=True)


def _tone_pcm(seconds: float = 2.0, freq: float = 440.0) -> bytes:
    """16-bit 48kHz stereo sine tone for discord.PCMAudio."""

    frames = int(SAMPLE_RATE * seconds)
    buf = io.BytesIO()
    for i in range(frames):
        sample = int(0.25 * 32767 * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
        buf.write(struct.pack("<hh", sample, sample))
    return buf.getvalue()


async def _wait_for_cache(client: discord.Client) -> None:
    """GUILD_CREATE events stream in after on_ready; wait for the cache."""

    for _ in range(50):
        if client.guilds:
            return
        await asyncio.sleep(0.2)


async def _list_guilds(client: discord.Client) -> None:
    for guild in client.guilds:
        print(f"guild: {guild.name} (id={guild.id})", flush=True)
        for channel in guild.voice_channels:
            members = ", ".join(m.display_name for m in channel.members) or "empty"
            print(f"  #{channel.name} (id={channel.id}) — {members}", flush=True)


async def _run_spike(client: discord.Client, channel_id: int, seconds: int) -> None:
    channel = client.get_channel(channel_id)
    if channel is None:
        for guild in client.guilds:
            channel = guild.get_channel(channel_id)
            if channel is not None:
                break
    if channel is None or not isinstance(channel, discord.VoiceChannel):
        print(f"ERROR: voice channel {channel_id} not found. Run --list first.")
        return

    print(f"joining #{channel.name} in {channel.guild.name} ...", flush=True)
    voice = await channel.connect()
    try:
        # Non-blocking settle: py-cord's wait_until_connected() is a sync
        # threading wait that would freeze this loop mid-DAVE-handshake.
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
        print("tone playback finished.", flush=True)

        sink = CountingSink()
        voice.start_listening(sink)
        print(f"listening for {seconds}s — TALK NOW ...", flush=True)
        await asyncio.sleep(seconds)
        try:
            voice.stop_listening()
        except Exception as exc:  # py-cord shutdown race — verdict still prints
            print(f"(stop_listening race ignored: {exc})", flush=True)

        print("=== verdict ===")
        print(f"dave: {voice.is_dave_connection()}")
        print(f"packets: {sink.packets}")
        print(f"bytes: {sink.bytes}")
        print(f"users heard: {len(sink.users)}")
        print(f"first chunk len: {sink.first_len} (expect 3840 = 20ms 48k stereo s16)")
        ok = sink.packets > 0
        print(f"RECEIVE {'WORKS' if ok else 'FAILED — zero packets decoded'}")
    finally:
        await voice.disconnect(force=False)
        print("disconnected.", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--channel", type=int, default=0)
    parser.add_argument("--seconds", type=int, default=20)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.debug:
        import logging

        logging.basicConfig(
            level=logging.DEBUG,
            format="%(name)s %(levelname)s %(message)s",
            stream=sys.stdout,
        )
        for noisy in ("discord.gateway", "discord.http", "discord.client"):
            logging.getLogger(noisy).setLevel(logging.INFO)

    if not TOKEN:
        print(f"ERROR: DISCORD_BOT_TOKEN not set (looked in {MAIN_ENV})")
        sys.exit(1)

    intents = discord.Intents.none()
    intents.guilds = True
    intents.voice_states = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        print(f"logged in as {client.user}", flush=True)
        exit_code = 0
        try:
            await _wait_for_cache(client)
            if args.list or not args.channel:
                await _list_guilds(client)
            else:
                await _run_spike(client, args.channel, args.seconds)
        except Exception as exc:  # noqa: BLE001 — spike reports and exits
            print(f"SPIKE ERROR: {type(exc).__name__}: {exc}", flush=True)
            exit_code = 1
        finally:
            # Hard-exit immediately (voice.disconnect() already ran in
            # _run_spike's own finally). py-cord dispatches on_ready as a
            # scheduled task, so a graceful client.close() here races the
            # gateway loop into a re-identify + get_gateway crash.
            os._exit(exit_code)

    # client.run() owns the loop — voice connection state binds to it
    # correctly (asyncio.run(client.start()) split voice futures onto a
    # different loop). os._exit in on_ready's finally pre-empts the
    # reconnect/re-identify crash on shutdown.
    client.run(TOKEN, reconnect=False)


if __name__ == "__main__":
    main()
