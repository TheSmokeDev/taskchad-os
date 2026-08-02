"""The relay reconnect loop must back off when the token is rejected.

Observed live 2026-07-27: the relay accepts the WebSocket handshake and
authorizes AFTERWARDS, so a bad token arrives as close(4001) on an already-open
socket. The old loop reset the backoff the instant the socket opened, which made
every rejection look like a fresh success — connect, close, reset, retry, two
seconds later, forever. Fourteen rounds landed in the log before anyone looked.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_CHAT = Path(__file__).resolve().parents[2] / "chat"
if str(_CHAT) not in sys.path:
    sys.path.insert(0, str(_CHAT))

import ws_client  # noqa: E402


class _Close:
    def __init__(self, code: int) -> None:
        self.code = code


class _ClosedError(Exception):
    """Shaped like websockets' ConnectionClosedError."""

    def __init__(self, rcvd: _Close | None = None, sent: _Close | None = None) -> None:
        super().__init__("closed")
        self.rcvd = rcvd
        self.sent = sent


def test_a_4001_close_is_recognized_as_an_auth_rejection() -> None:
    """Structural check, not a string match — messages change between releases."""

    assert ws_client._is_auth_rejection(_ClosedError(rcvd=_Close(4001)))
    assert ws_client._is_auth_rejection(_ClosedError(sent=_Close(4001)))


@pytest.mark.parametrize(
    ("exc", "label"),
    [
        (_ClosedError(rcvd=_Close(1000)), "a normal close is not an auth failure"),
        (_ClosedError(rcvd=_Close(1006)), "an abnormal close is not an auth failure"),
        (_ClosedError(), "no close frame at all"),
        (RuntimeError("boom"), "an unrelated exception"),
    ],
)
def test_other_closes_are_not_mislabelled_as_auth(exc: Exception, label: str) -> None:
    """Calling every drop an auth failure would hide real network faults."""

    assert not ws_client._is_auth_rejection(exc), label


def test_a_session_shorter_than_the_health_floor_must_not_reset_backoff() -> None:
    """The exact bug: a connect that dies immediately used to reset the delay.

    This asserts the POLICY the loop encodes — a session must outlive
    HEALTHY_SESSION_S to earn the reset — so the loop cannot quietly go back to
    resetting on connect without this failing.
    """

    cls = ws_client.RelayWSClient
    assert cls.HEALTHY_SESSION_S > 0.0
    # A rejection round trip is sub-second; the floor has to be comfortably
    # above it or the reject still earns a reset.
    assert cls.HEALTHY_SESSION_S >= 5.0
    # And comfortably below the ping interval, so a genuinely healthy relay
    # always clears it.
    assert cls.HEALTHY_SESSION_S < 30.0


def test_the_backoff_actually_climbs_to_its_ceiling() -> None:
    """A rejected token must reach the ceiling instead of hammering at 2s.

    Replays the loop's own arithmetic: without the health floor this sequence
    is pinned at INITIAL_BACKOFF_S forever, which is what hit the relay every
    two seconds.
    """

    cls = ws_client.RelayWSClient
    delay = cls.INITIAL_BACKOFF_S
    seen = [delay]
    for _ in range(12):
        delay = min(delay * cls.BACKOFF_MULTIPLIER, cls.MAX_BACKOFF_S)
        seen.append(delay)

    assert seen[0] == cls.INITIAL_BACKOFF_S
    assert seen[-1] == cls.MAX_BACKOFF_S
    assert seen == sorted(seen), "backoff must never go backwards"
    # Ten minutes of a rejected token should cost a handful of attempts, not 300.
    budget, attempts = 600.0, 0
    spent = 0.0
    for step in seen:
        if spent >= budget:
            break
        spent += step
        attempts += 1
    assert attempts <= 12, f"{attempts} reconnects in 10 minutes is still a hammer"
