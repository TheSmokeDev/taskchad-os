"""One slow Discord response must not cost the process its slash commands.

Observed live 2026-07-27: `Discord slash command sync timed out after 20s`,
once, at adapter connect — and that was it. The sync runs a single time per
process, so `/shots` simply never appeared and nothing said so again. Discord
rate-limits command syncs hard, which makes the slow response the EXPECTED
case, not the exotic one.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_CHAT = Path(__file__).resolve().parents[2] / "chat"
if str(_CHAT) not in sys.path:
    sys.path.insert(0, str(_CHAT))

from adapters import discord as discord_adapter  # noqa: E402

_sync = discord_adapter.DiscordAdapter._sync_native_slash_commands


class _Fake:
    """Only the two attributes the retry wrapper touches."""

    _sync_native_slash_commands = _sync

    def __init__(self, succeed_on: int | None) -> None:
        self._slash_commands_synced = False
        self.calls = 0
        self._succeed_on = succeed_on

    async def _sync_native_slash_commands_once(self, discord, *, attempt=1):
        self.calls += 1
        if self._succeed_on is not None and self.calls >= self._succeed_on:
            self._slash_commands_synced = True


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []

    async def fake(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake)
    return slept


def test_a_transient_timeout_is_retried_until_it_lands(no_sleep) -> None:
    fake = _Fake(succeed_on=3)

    asyncio.run(fake._sync_native_slash_commands(None))

    assert fake._slash_commands_synced, "a recoverable sync must recover"
    assert fake.calls == 3
    # Backoff climbs, and nothing sleeps after the attempt that succeeded.
    assert no_sleep == [5.0, 15.0]


def test_a_permanent_failure_stops_instead_of_looping_forever(no_sleep) -> None:
    fake = _Fake(succeed_on=None)

    asyncio.run(fake._sync_native_slash_commands(None))

    assert not fake._slash_commands_synced
    assert fake.calls == 4, "bounded attempts, not an infinite retry"
    assert no_sleep == [5.0, 15.0, 45.0]


def test_an_already_synced_process_does_no_work(no_sleep) -> None:
    """The guard is what keeps this one-shot per process."""

    fake = _Fake(succeed_on=1)
    fake._slash_commands_synced = True

    asyncio.run(fake._sync_native_slash_commands(None))

    assert fake.calls == 0
    assert no_sleep == []


def test_shots_is_in_the_menu_discord_actually_registers() -> None:
    """The command this whole path exists to deliver.

    `/shots` was in neither TELEGRAM_NATIVE_COMMANDS nor NATIVE_MENU_EXCLUDED,
    so it dispatched when typed in full and never autocompleted anywhere.
    """

    names = [name for name, _desc in discord_adapter.get_discord_native_command_menu()]

    assert "shots" in names
    assert len(names) == len(set(names)), "a duplicate would fail the Discord sync"
