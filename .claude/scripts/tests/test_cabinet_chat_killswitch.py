"""PRD-8 Phase 7b commit-2 (WS3) — cabinet chat-process kill-switch contract tests.

Asserts the operator kill-switch ("cabinet") gates ALL THREE chat-process
cabinet handlers BEFORE any HTTP call to localhost:4322:

  1. ``core_handlers.handle_cabinet`` — `/cabinet [create | list | send | end]`
  2. ``core_handlers.handle_standup`` — `/standup [optional seed question]`
  3. ``core_handlers.handle_discuss`` — `/discuss <topic>`

Phase 5a's API-process orchestrator (`dashboard_api.py:2336`) ALREADY gates
the cabinet kill-switch at the server side. This commit-2 adds the chat-process
side gate so refusals from `/cabinet send` initiated in Telegram count toward
the same `kill_switches.counters.cabinet` snapshot. Symmetric refusal counting:
disabled-state refusals from BOTH chat surface AND API surface increment the
same counter.

All three chat handlers use the Rule-3 module-attribute import inside the body:

    from security import kill_switches
    kill_switches.requireEnabled("cabinet", caller="handle_<name>")

so monkeypatch propagates correctly during tests.

UX choice: ``/cabinet help`` returns usage text WITHOUT triggering the kill
switch (help check runs before the try/requireEnabled block). Operators can
always discover usage even when cabinet is operator-disabled. The test
``test_handle_cabinet_help_works_when_disabled`` locks this behavior.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Ensure chat/ on path so ``import core_handlers`` works.
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "chat"))

import core_handlers  # noqa: E402

from security import kill_switches  # noqa: E402


@pytest.fixture(autouse=True)
def reset_counters():
    """Each test starts with empty refusal counters and audit-write failures."""
    kill_switches._REFUSAL_COUNTERS.clear()
    kill_switches._AUDIT_WRITE_FAILURES.clear()
    yield
    kill_switches._REFUSAL_COUNTERS.clear()
    kill_switches._AUDIT_WRITE_FAILURES.clear()


def _make_incoming(chat_id: str = "12345"):
    """Build a minimal incoming-message stub with a chat_id attribute."""
    incoming = MagicMock()
    incoming.chat_id = chat_id
    return incoming


# ──────────────────────────────────────────────────────────────────────
# Chokepoint 1: handle_cabinet
# ──────────────────────────────────────────────────────────────────────


def test_handle_cabinet_create_killswitch_disabled(monkeypatch):
    """`/cabinet create` returns friendly reply (NOT stack trace) when disabled.

    Verifies: refusal counter increments, NO HTTP call to cabinet_api was
    made, friendly_message is returned to chat.
    """
    monkeypatch.setenv("HOMIE_KILLSWITCH_CABINET", "disabled")

    # Mock cabinet_api so any accidental HTTP call would fail loudly.
    create_mock = AsyncMock(side_effect=AssertionError("cabinet_api.create_meeting MUST NOT be called when killswitch disabled"))
    list_mock = AsyncMock(side_effect=AssertionError("cabinet_api.list_meetings MUST NOT be called when killswitch disabled"))
    send_mock = AsyncMock(side_effect=AssertionError("cabinet_api.send_message MUST NOT be called when killswitch disabled"))
    end_mock = AsyncMock(side_effect=AssertionError("cabinet_api.end_meeting MUST NOT be called when killswitch disabled"))

    import integrations.cabinet_api as cabinet_api_mod
    monkeypatch.setattr(cabinet_api_mod, "create_meeting", create_mock)
    monkeypatch.setattr(cabinet_api_mod, "list_meetings", list_mock)
    monkeypatch.setattr(cabinet_api_mod, "send_message", send_mock)
    monkeypatch.setattr(cabinet_api_mod, "end_meeting", end_mock)

    result = asyncio.run(core_handlers.handle_cabinet(MagicMock(), _make_incoming(), "create"))

    assert "Cabinet is disabled by operator" in result, (
        f"Expected friendly kill-switch message, got: {result!r}"
    )
    assert kill_switches.get_refusal_counters().get("cabinet", 0) >= 1
    create_mock.assert_not_called()
    list_mock.assert_not_called()
    send_mock.assert_not_called()
    end_mock.assert_not_called()


def test_handle_cabinet_list_killswitch_disabled(monkeypatch):
    """`/cabinet list` is also gated."""
    monkeypatch.setenv("HOMIE_KILLSWITCH_CABINET", "disabled")
    list_mock = AsyncMock(side_effect=AssertionError("MUST NOT be called"))
    import integrations.cabinet_api as cabinet_api_mod
    monkeypatch.setattr(cabinet_api_mod, "list_meetings", list_mock)

    result = asyncio.run(core_handlers.handle_cabinet(MagicMock(), _make_incoming(), "list"))
    assert "Cabinet is disabled by operator" in result
    list_mock.assert_not_called()


def test_handle_cabinet_send_killswitch_disabled(monkeypatch):
    """`/cabinet send <id> <text>` is gated even though /api/cabinet/send is fire-and-forget.

    Phase 5a's send is fire-and-forget (returns 200 queued); the kill-switch
    refusal MUST happen at the chat-process boundary BEFORE the request is
    queued, so disabled-state never reaches the API.
    """
    monkeypatch.setenv("HOMIE_KILLSWITCH_CABINET", "disabled")
    send_mock = AsyncMock(side_effect=AssertionError("MUST NOT be called"))
    import integrations.cabinet_api as cabinet_api_mod
    monkeypatch.setattr(cabinet_api_mod, "send_message", send_mock)

    result = asyncio.run(core_handlers.handle_cabinet(MagicMock(), _make_incoming(), "send 42 hello"))
    assert "Cabinet is disabled by operator" in result
    send_mock.assert_not_called()


def test_handle_cabinet_help_works_when_disabled(monkeypatch):
    """`/cabinet help` returns usage text even when killswitch disabled.

    UX choice: help is always accessible because the help check runs BEFORE
    the kill-switch try block. Operator can always learn how to use the
    surface; only operations are gated.
    """
    monkeypatch.setenv("HOMIE_KILLSWITCH_CABINET", "disabled")

    result = asyncio.run(core_handlers.handle_cabinet(MagicMock(), _make_incoming(), "help"))
    assert "Cabinet is disabled by operator" not in result, (
        "/cabinet help should NOT trigger killswitch — it returns usage text only"
    )
    # Usage text mentions /cabinet subcommands
    assert "create" in result.lower() or "send" in result.lower()
    # Counter should NOT have incremented (help didn't reach the gate)
    assert kill_switches.get_refusal_counters().get("cabinet", 0) == 0


# ──────────────────────────────────────────────────────────────────────
# Chokepoint 2: handle_standup
# ──────────────────────────────────────────────────────────────────────


def test_handle_standup_killswitch_disabled(monkeypatch):
    """`/standup` is gated; cabinet_api.create_meeting + send_message NOT called."""
    monkeypatch.setenv("HOMIE_KILLSWITCH_CABINET", "disabled")
    create_mock = AsyncMock(side_effect=AssertionError("MUST NOT be called"))
    send_mock = AsyncMock(side_effect=AssertionError("MUST NOT be called"))
    import integrations.cabinet_api as cabinet_api_mod
    monkeypatch.setattr(cabinet_api_mod, "create_meeting", create_mock)
    monkeypatch.setattr(cabinet_api_mod, "send_message", send_mock)

    result = asyncio.run(core_handlers.handle_standup(MagicMock(), _make_incoming(), ""))
    assert "Cabinet is disabled by operator" in result
    create_mock.assert_not_called()
    send_mock.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# Chokepoint 3: handle_discuss
# ──────────────────────────────────────────────────────────────────────


def test_handle_discuss_killswitch_disabled(monkeypatch):
    """`/discuss <topic>` is gated."""
    monkeypatch.setenv("HOMIE_KILLSWITCH_CABINET", "disabled")
    create_mock = AsyncMock(side_effect=AssertionError("MUST NOT be called"))
    send_mock = AsyncMock(side_effect=AssertionError("MUST NOT be called"))
    import integrations.cabinet_api as cabinet_api_mod
    monkeypatch.setattr(cabinet_api_mod, "create_meeting", create_mock)
    monkeypatch.setattr(cabinet_api_mod, "send_message", send_mock)

    result = asyncio.run(core_handlers.handle_discuss(MagicMock(), _make_incoming(), "should we deprecate mc?"))
    assert "Cabinet is disabled by operator" in result
    create_mock.assert_not_called()
    send_mock.assert_not_called()


def test_handle_discuss_empty_args_returns_usage_without_triggering_killswitch(monkeypatch):
    """`/discuss` (no topic) returns usage text WITHOUT incrementing the counter.

    The empty-args check runs BEFORE the try/requireEnabled block, so an
    operator who fat-fingers `/discuss` doesn't get refused — they get a
    usage hint.
    """
    monkeypatch.setenv("HOMIE_KILLSWITCH_CABINET", "disabled")

    result = asyncio.run(core_handlers.handle_discuss(MagicMock(), _make_incoming(), ""))
    assert "Cabinet is disabled by operator" not in result
    assert "Usage" in result
    assert kill_switches.get_refusal_counters().get("cabinet", 0) == 0


# ──────────────────────────────────────────────────────────────────────
# Counter accumulation across all 3 chokepoints
# ──────────────────────────────────────────────────────────────────────


def test_counter_accumulates_across_all_three_handlers(monkeypatch):
    """3 refusals (1 per handler) yield counter == 3 — symmetric counting works."""
    monkeypatch.setenv("HOMIE_KILLSWITCH_CABINET", "disabled")

    import integrations.cabinet_api as cabinet_api_mod
    monkeypatch.setattr(cabinet_api_mod, "create_meeting", AsyncMock(side_effect=AssertionError))
    monkeypatch.setattr(cabinet_api_mod, "send_message", AsyncMock(side_effect=AssertionError))

    asyncio.run(core_handlers.handle_cabinet(MagicMock(), _make_incoming(), "create"))
    asyncio.run(core_handlers.handle_standup(MagicMock(), _make_incoming(), "what's up"))
    asyncio.run(core_handlers.handle_discuss(MagicMock(), _make_incoming(), "topic"))

    counters = kill_switches.get_refusal_counters()
    assert counters.get("cabinet", 0) == 3, (
        f"3 chokepoint refusals should accumulate to counter=3, got {counters!r}"
    )


# ──────────────────────────────────────────────────────────────────────
# Caller telemetry — refusal audit logs identify the chat-process source
# ──────────────────────────────────────────────────────────────────────


def test_caller_arg_distinguishes_chat_handlers(monkeypatch):
    """Each handler passes a distinct caller= so audit logs can attribute the source.

    Phase 5a's API-process gate uses caller="cabinet_send_route" (or similar).
    Phase 7b WS3 chat-process gates use caller="handle_cabinet" / "handle_standup"
    / "handle_discuss". Together they let the audit log distinguish chat-side
    vs API-side refusal sources for the same `cabinet` switch.
    """
    monkeypatch.setenv("HOMIE_KILLSWITCH_CABINET", "disabled")

    captured: list[str] = []

    real_require = kill_switches.requireEnabled

    def spy_require(switch_name: str, *, caller: str = ""):
        captured.append(caller)
        return real_require(switch_name, caller=caller)

    monkeypatch.setattr(kill_switches, "requireEnabled", spy_require)

    import integrations.cabinet_api as cabinet_api_mod
    monkeypatch.setattr(cabinet_api_mod, "create_meeting", AsyncMock(side_effect=AssertionError))
    monkeypatch.setattr(cabinet_api_mod, "send_message", AsyncMock(side_effect=AssertionError))

    asyncio.run(core_handlers.handle_cabinet(MagicMock(), _make_incoming(), "create"))
    asyncio.run(core_handlers.handle_standup(MagicMock(), _make_incoming(), "x"))
    asyncio.run(core_handlers.handle_discuss(MagicMock(), _make_incoming(), "topic"))

    assert captured == ["handle_cabinet", "handle_standup", "handle_discuss"], (
        f"Each handler must pass its own caller= identifier; got {captured!r}"
    )


# ──────────────────────────────────────────────────────────────────────
# Module-attribute import contract (Rule 3) — source-text assertion
# ──────────────────────────────────────────────────────────────────────


def test_chat_handlers_use_module_attribute_lookup_for_kill_switches():
    """Source-text contract: cabinet handlers import `from security import kill_switches`.

    Rule 3 says monkeypatch must propagate. `from security.kill_switches import
    requireEnabled` would defeat that. AST-style source assertion catches
    accidental refactor.
    """
    src = (SCRIPTS_DIR.parent / "chat" / "core_handlers.py").read_text(encoding="utf-8")

    # Each of the 3 handlers should have `from security import kill_switches`
    # somewhere inside its body. We check at least 3 occurrences in the file.
    forbidden = "from security.kill_switches import"
    assert forbidden not in src, (
        f"Forbidden direct-symbol import found in core_handlers.py: {forbidden!r}. "
        f"Use `from security import kill_switches; kill_switches.requireEnabled(...)` instead."
    )

    correct = "from security import kill_switches"
    assert src.count(correct) >= 3, (
        f"Expected >=3 module-attribute imports in core_handlers.py "
        f"(one per cabinet handler), got {src.count(correct)}."
    )


# ──────────────────────────────────────────────────────────────────────
# Killswitch-NOT-disabled smoke (handler proceeds normally)
# ──────────────────────────────────────────────────────────────────────


def test_handle_cabinet_proceeds_when_killswitch_not_disabled(monkeypatch):
    """When env unset, `/cabinet create` reaches cabinet_api.create_meeting.

    Locks the inverse: the gate doesn't fire false-positive when the operator
    hasn't disabled cabinet.
    """
    monkeypatch.delenv("HOMIE_KILLSWITCH_CABINET", raising=False)

    import integrations.cabinet_api as cabinet_api_mod
    fake_ref = MagicMock()
    fake_ref.id = 99
    create_mock = AsyncMock(return_value=fake_ref)
    monkeypatch.setattr(cabinet_api_mod, "create_meeting", create_mock)

    result = asyncio.run(core_handlers.handle_cabinet(MagicMock(), _make_incoming(), "create"))
    assert "Cabinet is disabled by operator" not in result
    assert "#99" in result
    create_mock.assert_called_once()
    assert kill_switches.get_refusal_counters().get("cabinet", 0) == 0
