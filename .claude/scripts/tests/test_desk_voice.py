"""The homie layer: he decides whether to speak, and whether to interrupt.

Path map:
  is_within_waking_window   wrapping window (the whole reason it exists) |
                            same-day window | zero-width | malformed (fail open)
  desk_voice._parse         valid | post-with-empty-message | ping-without-post |
                            fenced despite instruction | garbage
  notify.post_text          silent by default | pings only the resolved operator ids
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import config  # noqa: E402
from lib import desk_voice  # noqa: E402

_TZ = ZoneInfo("America/Chicago")


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 28, hour, minute, tzinfo=_TZ)


# ---------------------------------------------------------------------------
# The waking window -- 08:00-02:00 CROSSES MIDNIGHT, which is exactly what the
# existing is_within_active_hours() string compare cannot express.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hour,minute,awake",
    [
        (8, 0, True),    # boundary: start is inclusive
        (12, 0, True),   # midday
        (23, 59, True),  # late evening, still before the wrap
        (0, 30, True),   # AFTER midnight and still awake -- the case that
                         # a naive "start <= now <= end" gets wrong
        (1, 59, True),   # last minute
        (2, 0, False),   # boundary: end is exclusive
        (3, 0, False),   # asleep
        (7, 59, False),  # one minute before waking
    ],
)
def test_wrapping_window(monkeypatch, hour, minute, awake):
    monkeypatch.setenv("DESK_WAKING_START", "08:00")
    monkeypatch.setenv("DESK_WAKING_END", "02:00")
    assert config.is_within_waking_window(_at(hour, minute)) is awake


def test_same_day_window_still_works(monkeypatch):
    """A non-wrapping window must keep ordinary single-span behavior."""
    monkeypatch.setenv("DESK_WAKING_START", "09:00")
    monkeypatch.setenv("DESK_WAKING_END", "17:00")
    assert config.is_within_waking_window(_at(12)) is True
    assert config.is_within_waking_window(_at(8)) is False
    assert config.is_within_waking_window(_at(18)) is False


def test_old_helper_provably_cannot_express_the_wrap(monkeypatch):
    """Pins WHY a second helper exists, so nobody 'simplifies' it away.

    The string compare says 00:30 is outside 08:00-02:00. It is not -- that is
    the middle of the operator's evening.
    """
    monkeypatch.setattr(config, "HEARTBEAT_ACTIVE_START", "08:00")
    monkeypatch.setattr(config, "HEARTBEAT_ACTIVE_END", "02:00")
    monkeypatch.setattr(config, "now_local", lambda: _at(0, 30))
    assert config.is_within_active_hours() is False        # the bug
    monkeypatch.setenv("DESK_WAKING_START", "08:00")
    monkeypatch.setenv("DESK_WAKING_END", "02:00")
    assert config.is_within_waking_window(_at(0, 30)) is True  # the fix


@pytest.mark.parametrize(
    "start,end",
    [("nonsense", "02:00"), ("08:00", "99:99"), ("8", "2"), ("08:60", "02:00")],
)
def test_malformed_window_fails_open(monkeypatch, start, end):
    """A typo must never silently mute the desk -- silence looks like health."""
    monkeypatch.setenv("DESK_WAKING_START", start)
    monkeypatch.setenv("DESK_WAKING_END", end)
    assert config.is_within_waking_window(_at(4)) is True


def test_empty_env_means_unset_not_malformed(monkeypatch):
    """Empty string is 'use the default', NOT 'fail open'.

    Distinct from the malformed case above: blanking the var must land on the
    08:00-02:00 default, so 04:00 is correctly asleep. Treating empty as
    malformed would make a cleared env var ping the operator at 4am.
    """
    monkeypatch.setenv("DESK_WAKING_START", "")
    monkeypatch.setenv("DESK_WAKING_END", "")
    assert config.is_within_waking_window(_at(4)) is False
    assert config.is_within_waking_window(_at(12)) is True


def test_zero_width_window_is_always_on(monkeypatch):
    monkeypatch.setenv("DESK_WAKING_START", "08:00")
    monkeypatch.setenv("DESK_WAKING_END", "08:00")
    assert config.is_within_waking_window(_at(3)) is True


# ---------------------------------------------------------------------------
# Parsing his decision
# ---------------------------------------------------------------------------


def test_parse_valid_decision():
    got = desk_voice._parse('{"post": true, "ping": true, "message": "yo bro"}')
    assert (got.post, got.ping, got.text) == (True, True, "yo bro")


def test_post_with_empty_message_becomes_silence():
    """Trusting the flag alone would publish an empty message."""
    got = desk_voice._parse('{"post": true, "ping": true, "message": "   "}')
    assert got.post is False
    assert got.ping is False


def test_ping_cannot_ride_without_a_post():
    """An @ with no message is a pure interruption."""
    got = desk_voice._parse('{"post": false, "ping": true, "message": "hey"}')
    assert got.ping is False


def test_parse_salvages_code_fences():
    got = desk_voice._parse('```json\n{"post": true, "ping": false, "message": "hi"}\n```')
    assert got is not None and got.text == "hi"


@pytest.mark.parametrize("raw", ["", "not json at all", "{{{", "[]"])
def test_parse_garbage_returns_none(raw):
    """None => the caller posts cards unwrapped, exactly as it shipped."""
    assert desk_voice._parse(raw) is None


def test_voice_kill_switch(monkeypatch):
    import security.kill_switches as ks

    monkeypatch.setattr(ks, "is_disabled", lambda name: name == desk_voice.KILL_SWITCH_NAME)
    assert desk_voice.voice_enabled() is False


# ---------------------------------------------------------------------------
# The ping itself
# ---------------------------------------------------------------------------


class _Target:
    guild_id = "1"
    channel_id = "2"
    allowed_user_ids = ("4242",)
    bot_token = "t"


def _capture_post(monkeypatch):
    from discord_alpha import notify

    sent = {}

    def _fake_request(target, *, path, payload, method):
        sent["payload"] = payload
        return {"id": "9", "application_id": None}

    monkeypatch.setattr(notify, "_request", _fake_request)
    return notify, sent


def test_post_text_is_silent_by_default(monkeypatch):
    notify, sent = _capture_post(monkeypatch)
    notify.post_text(_Target(), "morning")
    assert sent["payload"]["allowed_mentions"]["users"] == []
    assert "<@" not in sent["payload"]["content"]


def test_post_text_pings_only_the_resolved_operator(monkeypatch):
    notify, sent = _capture_post(monkeypatch)
    notify.post_text(_Target(), "this is live", ping_operator=True)
    payload = sent["payload"]
    assert payload["content"].startswith("<@4242>")
    assert payload["allowed_mentions"]["users"] == ["4242"]
    # @everyone / role pings can never fire from scraped third-party text.
    assert payload["allowed_mentions"]["parse"] == []
    assert payload["allowed_mentions"]["roles"] == []


def test_ping_requested_but_no_operator_id_stays_silent(monkeypatch):
    notify, sent = _capture_post(monkeypatch)

    class _NoUsers(_Target):
        allowed_user_ids = ()

    notify.post_text(_NoUsers(), "hi", ping_operator=True)
    assert sent["payload"]["allowed_mentions"]["users"] == []
    assert "<@" not in sent["payload"]["content"]
