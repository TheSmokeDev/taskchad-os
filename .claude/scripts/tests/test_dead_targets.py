"""Tests for the dead-target registry and its cabinet-relay wiring.

One test per distinct code path (Testing Principle):

  * ``DeadTargetRegistry``: mark/clear idempotency + returns, is_dead,
    empty-chat-id guards, persistence round-trip, corrupt/non-dict/malformed
    JSON tolerance, unwritable-path degrade-to-memory, is_dead_error_kind.
  * ``classify_send_error``: telegram Forbidden / BadRequest(chat-not-found),
    discord Forbidden / NotFound, slack channel_not_found / is_archived (both
    dict and SlackResponse.data shapes), string fallback, transient → None.
  * ``_slack_error_code``: dict resp / ``.data`` obj / ``.get`` callable / None.
  * cabinet relay wiring: is_dead origin skipped; success → clear; permanent
    error → mark_dead; transient error → NOT recorded; fail-open (bool return
    unchanged) when the registry is None or raises.

Async paths are driven with ``asyncio.run`` (no pytest-asyncio dependency).
The chat/ and scripts/ dirs are on sys.path via ``tests/conftest.py``.
"""

from __future__ import annotations

import asyncio
import json

import cabinet_relay
from models import Channel, Platform

from orchestration.dead_targets import (
    DeadTargetRegistry,
    _normalize,
    _slack_error_code,
    classify_send_error,
)

# ---------------------------------------------------------------------------
# DeadTargetRegistry — persistence + public API
# ---------------------------------------------------------------------------


def test_normalize_key() -> None:
    assert _normalize("Telegram", " 555 ") == "telegram:555"


def test_mark_dead_idempotent(tmp_path) -> None:
    reg = DeadTargetRegistry(path=tmp_path / "d.json")
    assert reg.mark_dead("telegram", "5") is True   # newly added
    assert reg.mark_dead("telegram", "5") is False  # already present
    assert reg.is_dead("telegram", "5") is True


def test_clear_returns_and_self_heals(tmp_path) -> None:
    reg = DeadTargetRegistry(path=tmp_path / "d.json")
    assert reg.clear("telegram", "5") is False  # nothing to clear
    reg.mark_dead("telegram", "5")
    assert reg.clear("telegram", "5") is True   # removed
    assert reg.is_dead("telegram", "5") is False


def test_empty_chat_id_guards(tmp_path) -> None:
    reg = DeadTargetRegistry(path=tmp_path / "d.json")
    assert reg.is_dead("telegram", None) is False
    assert reg.is_dead("telegram", "") is False
    assert reg.mark_dead("telegram", None) is False
    assert reg.clear("telegram", "") is False


def test_persistence_round_trip(tmp_path) -> None:
    p = tmp_path / "d.json"
    r1 = DeadTargetRegistry(path=p)
    r1.mark_dead("telegram", "123", reason="the group chat was deleted")
    assert p.exists()
    # A fresh registry over the same file sees the persisted flag.
    r2 = DeadTargetRegistry(path=p)
    assert r2.is_dead("telegram", "123")
    # A clear persists too.
    assert r1.clear("telegram", "123") is True
    r3 = DeadTargetRegistry(path=p)
    assert not r3.is_dead("telegram", "123")


def test_corrupt_file_starts_empty(tmp_path) -> None:
    p = tmp_path / "d.json"
    p.write_text("{not valid json")
    reg = DeadTargetRegistry(path=p)  # must not raise
    assert reg.all_dead() == {}


def test_non_dict_json_ignored(tmp_path) -> None:
    p = tmp_path / "d.json"
    p.write_text("[1, 2, 3]")
    reg = DeadTargetRegistry(path=p)
    assert reg.all_dead() == {}


def test_malformed_entries_filtered(tmp_path) -> None:
    p = tmp_path / "d.json"
    p.write_text(json.dumps({"telegram:1": {"reason": "x"}, "telegram:2": "notadict"}))
    reg = DeadTargetRegistry(path=p)
    assert "telegram:1" in reg.all_dead()
    assert "telegram:2" not in reg.all_dead()


def test_unwritable_path_degrades_to_memory(tmp_path) -> None:
    # Parent is a FILE, so mkdir(parents=True) / write raises OSError inside
    # _flush_locked — which must swallow it and keep the in-memory state.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    reg = DeadTargetRegistry(path=blocker / "sub" / "dead.json")  # construction ok
    assert reg.mark_dead("telegram", "999") is True   # no raise
    assert reg.is_dead("telegram", "999") is True      # in-memory retained
    assert not (blocker / "sub").exists()              # nothing persisted


def test_is_dead_error_kind() -> None:
    assert DeadTargetRegistry.is_dead_error_kind("forbidden") is True
    assert DeadTargetRegistry.is_dead_error_kind("not_found") is True
    assert DeadTargetRegistry.is_dead_error_kind(None) is False
    assert DeadTargetRegistry.is_dead_error_kind("") is False
    assert DeadTargetRegistry.is_dead_error_kind("rate_limited") is False


# ---------------------------------------------------------------------------
# classify_send_error — provider branches
# ---------------------------------------------------------------------------


def test_classify_telegram_forbidden() -> None:
    from telegram.error import Forbidden

    assert classify_send_error(Forbidden("the group chat was deleted")) == "forbidden"


def test_classify_telegram_badrequest_chat_not_found() -> None:
    from telegram.error import BadRequest

    assert classify_send_error(BadRequest("Chat not found")) == "not_found"


def test_classify_telegram_badrequest_other_is_none() -> None:
    from telegram.error import BadRequest

    # A BadRequest that is not a chat-not-found is transient/other → None.
    assert classify_send_error(BadRequest("Message is too long")) is None


def test_classify_discord_forbidden() -> None:
    from discord.errors import Forbidden

    class _Resp:
        status = 403
        reason = "Forbidden"

    assert classify_send_error(Forbidden(_Resp(), "Missing Permissions")) == "forbidden"


def test_classify_discord_not_found() -> None:
    from discord.errors import NotFound

    class _Resp:
        status = 404
        reason = "Not Found"

    assert classify_send_error(NotFound(_Resp(), "Unknown Channel")) == "not_found"


def test_classify_slack_channel_not_found_dict_response() -> None:
    from slack_sdk.errors import SlackApiError

    exc = SlackApiError("channel_not_found", {"ok": False, "error": "channel_not_found"})
    assert classify_send_error(exc) == "not_found"


def test_classify_slack_is_archived_data_response() -> None:
    from slack_sdk.errors import SlackApiError

    class _Resp:
        data = {"ok": False, "error": "is_archived"}

    exc = SlackApiError("is_archived", _Resp())
    assert classify_send_error(exc) == "forbidden"


def test_classify_string_fallback_forbidden() -> None:
    # A wrapped/generic error not recognized as a provider type falls back to
    # string matching.
    blocked = RuntimeError("Forbidden: bot was blocked by the user")
    assert classify_send_error(blocked) == "forbidden"
    assert classify_send_error(RuntimeError("user is deactivated")) == "forbidden"
    assert classify_send_error(RuntimeError("bot was kicked from the group chat")) == "forbidden"


def test_classify_string_fallback_chat_level_not_found() -> None:
    # WHOLE-CHAT not-found phrases dead-mark the origin.
    assert classify_send_error(RuntimeError("Bad Request: chat not found")) == "not_found"
    assert classify_send_error(RuntimeError("channel not found")) == "not_found"
    assert classify_send_error(RuntimeError("Unknown Channel")) == "not_found"


def test_classify_string_fallback_subresource_not_found_is_none() -> None:
    # F1: a thread/topic/message/sub-resource not-found must NOT dead-mark the
    # parent chat — the registry records whole-chat deaths only. These all
    # classify None so a reachable origin is never permanently suppressed.
    for text in (
        "message not found",
        "thread not found",
        "topic_deleted",
        "message_id_invalid",
        "the resource was not found",
    ):
        assert classify_send_error(RuntimeError(text)) is None, text


def test_classify_transient_is_none() -> None:
    assert classify_send_error(RuntimeError("temporary network blip")) is None
    assert classify_send_error(TimeoutError("timed out")) is None


# ---------------------------------------------------------------------------
# _slack_error_code — shape normalization
# ---------------------------------------------------------------------------


def test_slack_error_code_shapes() -> None:
    class _Exc:
        def __init__(self, response) -> None:
            self.response = response

    # No response → "".
    class _NoResp:
        response = None

    assert _slack_error_code(_NoResp()) == ""
    # dict response.
    assert _slack_error_code(_Exc({"error": "channel_not_found"})) == "channel_not_found"

    # SlackResponse-like: .data holds the parsed body.
    class _WithData:
        data = {"error": "is_archived"}

    assert _slack_error_code(_Exc(_WithData())) == "is_archived"

    # Only a .get callable (no .data).
    class _WithGet:
        data = None

        def get(self, key, default=""):
            return "not_in_channel" if key == "error" else default

    assert _slack_error_code(_Exc(_WithGet())) == "not_in_channel"


# ---------------------------------------------------------------------------
# cabinet relay wiring
# ---------------------------------------------------------------------------


class _FakeAdapter:
    def __init__(self, fail_exc: BaseException | None = None) -> None:
        self.sent: list = []
        self.fail_exc = fail_exc

    async def send(self, message):
        if self.fail_exc is not None:
            raise self.fail_exc
        self.sent.append(message)
        return "mid-1"


def _origin(platform=Platform.DISCORD, pid: str = "chan-dt") -> Channel:
    return Channel(platform=platform, platform_id=pid)


def _evt(seq: int, inner: dict) -> dict:
    return {"seq": seq, "event": inner}


def _agent_done(agent_id: str, text: str) -> dict:
    return {"type": "agent_done", "agentId": agent_id, "text": text, "incomplete": False}


def test_relay_skips_dead_origin(monkeypatch, tmp_path) -> None:
    """A proven-dead origin short-circuits BEFORE the stream is subscribed."""
    from integrations import cabinet_api

    reg = DeadTargetRegistry(path=tmp_path / "d.json")
    reg.mark_dead("discord", "chan-dt")
    monkeypatch.setattr(cabinet_relay, "_get_dead_registry", lambda: reg)

    stream_calls = {"n": 0}

    async def _gen(meeting_id, *a, **k):
        stream_calls["n"] += 1
        yield _evt(1, _agent_done("sales", "hi"))

    monkeypatch.setattr(cabinet_api, "stream_meeting", _gen)
    cabinet_relay._active_relays.discard(40)
    adapter = _FakeAdapter()

    asyncio.run(cabinet_relay._relay_meeting(40, adapter, _origin(), 0))

    assert stream_calls["n"] == 0     # never subscribed
    assert adapter.sent == []
    assert 40 not in cabinet_relay._active_relays  # dedup cleared for a future retry


def test_relay_live_origin_not_skipped(monkeypatch, tmp_path) -> None:
    """A non-dead origin relays normally (registry present, empty)."""
    from integrations import cabinet_api

    reg = DeadTargetRegistry(path=tmp_path / "d.json")
    monkeypatch.setattr(cabinet_relay, "_get_dead_registry", lambda: reg)

    async def _gen(meeting_id, *a, **k):
        yield _evt(1, _agent_done("sales", "ship it"))

    monkeypatch.setattr(cabinet_api, "stream_meeting", _gen)
    adapter = _FakeAdapter()

    asyncio.run(cabinet_relay._relay_meeting(41, adapter, _origin(), 0))

    assert [m.text for m in adapter.sent] == ["**Sales:** ship it"]


def test_safe_send_clears_on_success(monkeypatch, tmp_path) -> None:
    reg = DeadTargetRegistry(path=tmp_path / "d.json")
    reg.mark_dead("discord", "chan-dt")
    assert reg.is_dead("discord", "chan-dt")
    monkeypatch.setattr(cabinet_relay, "_get_dead_registry", lambda: reg)

    ok = asyncio.run(cabinet_relay._safe_send(_FakeAdapter(), _origin(), "hi"))

    assert ok is True
    assert not reg.is_dead("discord", "chan-dt")  # self-healed


def test_safe_send_marks_dead_on_permanent_error(monkeypatch, tmp_path) -> None:
    from telegram.error import Forbidden

    reg = DeadTargetRegistry(path=tmp_path / "d.json")
    monkeypatch.setattr(cabinet_relay, "_get_dead_registry", lambda: reg)
    adapter = _FakeAdapter(fail_exc=Forbidden("bot was blocked by the user"))
    origin = _origin(platform=Platform.TELEGRAM, pid="555")

    ok = asyncio.run(cabinet_relay._safe_send(adapter, origin, "hi"))

    assert ok is False               # bool contract preserved on failure
    assert reg.is_dead("telegram", "555")


def test_safe_send_transient_error_not_recorded(monkeypatch, tmp_path) -> None:
    reg = DeadTargetRegistry(path=tmp_path / "d.json")
    monkeypatch.setattr(cabinet_relay, "_get_dead_registry", lambda: reg)
    adapter = _FakeAdapter(fail_exc=RuntimeError("temporary network blip"))
    origin = _origin(platform=Platform.TELEGRAM, pid="777")

    ok = asyncio.run(cabinet_relay._safe_send(adapter, origin, "hi"))

    assert ok is False
    assert not reg.is_dead("telegram", "777")
    assert reg.all_dead() == {}


def test_safe_send_registry_none_is_transparent(monkeypatch) -> None:
    monkeypatch.setattr(cabinet_relay, "_get_dead_registry", lambda: None)
    adapter = _FakeAdapter()

    ok = asyncio.run(cabinet_relay._safe_send(adapter, _origin(), "hi"))

    assert ok is True
    assert len(adapter.sent) == 1  # message went through, unaffected


def test_safe_send_fail_open_when_registry_raises(monkeypatch) -> None:
    """A registry that raises on clear/mark must not change the bool result."""

    class _BoomReg:
        def clear(self, *a, **k):
            raise RuntimeError("registry boom")

        def mark_dead(self, *a, **k):
            raise RuntimeError("registry boom")

        def is_dead(self, *a, **k):
            raise RuntimeError("registry boom")

    monkeypatch.setattr(cabinet_relay, "_get_dead_registry", lambda: _BoomReg())

    # Success path: clear raises but send still reports True.
    ok = asyncio.run(cabinet_relay._safe_send(_FakeAdapter(), _origin(), "hi"))
    assert ok is True

    # Failure path: mark_dead raises but send still reports False.
    adapter = _FakeAdapter(fail_exc=RuntimeError("Forbidden"))
    ok = asyncio.run(cabinet_relay._safe_send(adapter, _origin(), "hi"))
    assert ok is False


def test_relay_skip_fail_open_when_is_dead_raises(monkeypatch) -> None:
    """A broken is_dead check must not block a relay (fail-open pre-check)."""
    from integrations import cabinet_api

    class _BoomReg:
        def is_dead(self, *a, **k):
            raise RuntimeError("is_dead boom")

        def clear(self, *a, **k):
            return False

    monkeypatch.setattr(cabinet_relay, "_get_dead_registry", lambda: _BoomReg())

    async def _gen(meeting_id, *a, **k):
        yield _evt(1, _agent_done("sales", "still relayed"))

    monkeypatch.setattr(cabinet_api, "stream_meeting", _gen)
    adapter = _FakeAdapter()

    asyncio.run(cabinet_relay._relay_meeting(42, adapter, _origin(), 0))

    assert [m.text for m in adapter.sent] == ["**Sales:** still relayed"]


# ---------------------------------------------------------------------------
# Iteration-2 F1 — real adapter failure semantics
# ---------------------------------------------------------------------------


class _SwallowAdapter:
    """Mirrors slack.py: catches its own send exception, prints, returns a falsy
    message id — a delivery failure that does NOT raise."""

    def __init__(self, result=None) -> None:
        self.attempts = 0
        self.result = result

    async def send(self, message):
        self.attempts += 1
        return self.result  # falsy (None) => swallowed failure


def test_safe_send_falsy_result_no_false_clear(monkeypatch, tmp_path) -> None:
    """A falsy send result for non-empty text is a FAILURE — a dead flag must
    NOT be falsely cleared (slack.py swallow-and-return-None path)."""
    reg = DeadTargetRegistry(path=tmp_path / "d.json")
    reg.mark_dead("slack", "C123")
    monkeypatch.setattr(cabinet_relay, "_get_dead_registry", lambda: reg)
    origin = _origin(platform=Platform.SLACK, pid="C123")

    ok = asyncio.run(cabinet_relay._safe_send(_SwallowAdapter(), origin, "hi"))

    assert ok is False                    # falsy result for non-empty text = failure
    assert reg.is_dead("slack", "C123")   # NOT falsely cleared


def test_safe_send_falsy_result_not_marked_dead(monkeypatch, tmp_path) -> None:
    """A swallowed failure (no exception) is transient — not mark_dead'd (there
    is nothing to classify); the invariant is only 'no false success/clear'."""
    reg = DeadTargetRegistry(path=tmp_path / "d.json")
    monkeypatch.setattr(cabinet_relay, "_get_dead_registry", lambda: reg)
    origin = _origin(platform=Platform.SLACK, pid="C999")

    ok = asyncio.run(cabinet_relay._safe_send(_SwallowAdapter(), origin, "hi"))

    assert ok is False
    assert reg.all_dead() == {}


def test_safe_send_truthy_result_still_clears(monkeypatch, tmp_path) -> None:
    """A truthy message id is a real success → clear proceeds (bool contract)."""
    reg = DeadTargetRegistry(path=tmp_path / "d.json")
    reg.mark_dead("slack", "C7")
    monkeypatch.setattr(cabinet_relay, "_get_dead_registry", lambda: reg)
    origin = _origin(platform=Platform.SLACK, pid="C7")

    ok = asyncio.run(cabinet_relay._safe_send(_SwallowAdapter(result="ts-1"), origin, "hi"))

    assert ok is True
    assert not reg.is_dead("slack", "C7")  # real success self-heals


def test_classify_wrapped_provider_via_cause_chain() -> None:
    """A provider error wrapped by an adapter delivery error (raise ... from)
    still classifies through the __cause__ chain (bug b)."""
    from adapters.telegram import TelegramDeliveryError
    from telegram.error import Forbidden

    cause = Forbidden("bot was blocked by the user")
    try:
        raise TelegramDeliveryError("Telegram failed to deliver 1 text chunk(s)") from cause
    except TelegramDeliveryError as wrapper:
        assert classify_send_error(wrapper) == "forbidden"


def test_classify_wrapped_provider_via_context_chain() -> None:
    """Implicit __context__ chaining (raise inside except, no `from`) also
    classifies."""
    from adapters.telegram import TelegramDeliveryError
    from telegram.error import Forbidden

    try:
        try:
            raise Forbidden("bot was blocked by the user")
        except Forbidden:
            raise TelegramDeliveryError("Telegram failed to deliver 1 text chunk(s)")
    except TelegramDeliveryError as wrapper:
        assert wrapper.__cause__ is None
        assert wrapper.__context__ is not None
        assert classify_send_error(wrapper) == "forbidden"


def test_classify_unchained_wrapper_is_none() -> None:
    """A bare delivery error with no provider cause is transient → None (the
    original detail was lost, so we must NOT dead-mark on the generic text)."""
    from adapters.telegram import TelegramDeliveryError

    err = TelegramDeliveryError("Telegram failed to deliver 1 text chunk(s)")
    assert err.__cause__ is None
    assert err.__context__ is None
    assert classify_send_error(err) is None


def test_classify_cause_chain_cycle_safe() -> None:
    """A cyclic cause chain must not hang and still classifies from the chain."""
    a = RuntimeError("wrapper with no signal")
    b = RuntimeError("bot was blocked")
    a.__cause__ = b
    b.__cause__ = a  # cycle

    assert classify_send_error(a) == "forbidden"


def test_safe_send_marks_dead_on_wrapped_telegram_error(monkeypatch, tmp_path) -> None:
    """End-to-end (bug b): the relay's _safe_send catches an adapter
    TelegramDeliveryError chained from Forbidden and records the target dead."""
    from adapters.telegram import TelegramDeliveryError
    from telegram.error import Forbidden

    wrapper = TelegramDeliveryError("Telegram failed to deliver 1 text chunk(s)")
    wrapper.__cause__ = Forbidden("bot was blocked by the user")  # mirror raise-from

    reg = DeadTargetRegistry(path=tmp_path / "d.json")
    monkeypatch.setattr(cabinet_relay, "_get_dead_registry", lambda: reg)
    adapter = _FakeAdapter(fail_exc=wrapper)
    origin = _origin(platform=Platform.TELEGRAM, pid="12345")

    ok = asyncio.run(cabinet_relay._safe_send(adapter, origin, "hi"))

    assert ok is False
    assert reg.is_dead("telegram", "12345")
