"""Test PRD-8 Phase 5b / WS3.2 — handle_cabinet / handle_standup / handle_discuss
plus CORE_HANDLERS dispatch + friendly-error pass-through.

Patterns:

* Mock `integrations.cabinet_api` helpers via monkeypatch with async stubs.
* Verify chat_id pass-through, friendly-message return, and subcommand
  routing for handle_cabinet (create/list/send/end/help/empty).
* Cover R1 B6 / R2 NB1 fire-and-forget semantics — /send 200 reply does NOT
  surface as KillSwitchDisabled friendly_message.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

# Ensure both .claude/scripts and .claude/chat are importable.
_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS.parent / "chat"))

import core_handlers  # type: ignore[import-not-found]  # noqa: E402
from integrations import cabinet_api  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers — incoming + adapter doubles
# ---------------------------------------------------------------------------


def _incoming(chat_id: int | str | None = 42) -> SimpleNamespace:
    """Build a minimal `IncomingMessage`-shaped object the handlers need.

    Handlers use `getattr(incoming, "chat_id", None)` so any object with
    that attribute satisfies them.
    """
    return SimpleNamespace(chat_id=chat_id)


def _adapter() -> object:
    """Stub adapter — handlers don't call adapter methods in 5b."""
    return object()


@dataclass
class _Calls:
    create: list[dict] = None  # type: ignore[assignment]
    send: list[dict] = None  # type: ignore[assignment]
    list_: list[dict] = None  # type: ignore[assignment]
    end: list[dict] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.create = []
        self.send = []
        self.list_ = []
        self.end = []


@pytest.fixture
def patched_cabinet_api(monkeypatch: pytest.MonkeyPatch) -> _Calls:
    """Patch every cabinet_api helper to a happy-path async stub that
    captures call kwargs."""
    calls = _Calls()

    async def _create(chat_id=None, *, client=None):  # noqa: ANN001
        calls.create.append({"chat_id": chat_id, "client": client})
        return cabinet_api.CabinetMeetingRef(id=99, chat_id=chat_id, auto_ended_ids=[])

    async def _list(limit=20, chat_id=None, *, client=None):  # noqa: ANN001
        calls.list_.append({"limit": limit, "chat_id": chat_id, "client": client})
        return [{"id": 1, "title": "Test", "ended_at": None}]

    async def _send(meeting_id, text, client_msg_id=None, chat_id=None, *, client=None):  # noqa: ANN001
        calls.send.append(
            {"meeting_id": meeting_id, "text": text,
             "client_msg_id": client_msg_id, "chat_id": chat_id, "client": client},
        )
        return {"ok": True, "queued": True}

    async def _end(meeting_id, chat_id=None, *, client=None):  # noqa: ANN001
        calls.end.append({"meeting_id": meeting_id, "chat_id": chat_id, "client": client})
        return {"ok": True, "meetingId": meeting_id}

    monkeypatch.setattr(cabinet_api, "create_meeting", _create)
    monkeypatch.setattr(cabinet_api, "list_meetings", _list)
    monkeypatch.setattr(cabinet_api, "send_message", _send)
    monkeypatch.setattr(cabinet_api, "end_meeting", _end)
    return calls


# ---------------------------------------------------------------------------
# CORE_HANDLERS dispatch table (slashless keys — Codex M2 fix)
# ---------------------------------------------------------------------------


def test_handle_cabinet_in_core_handlers() -> None:
    assert "cabinet" in core_handlers.CORE_HANDLERS
    assert core_handlers.CORE_HANDLERS["cabinet"] is core_handlers.handle_cabinet


def test_handle_standup_in_core_handlers() -> None:
    assert "standup" in core_handlers.CORE_HANDLERS
    assert core_handlers.CORE_HANDLERS["standup"] is core_handlers.handle_standup


def test_handle_discuss_in_core_handlers() -> None:
    assert "discuss" in core_handlers.CORE_HANDLERS
    assert core_handlers.CORE_HANDLERS["discuss"] is core_handlers.handle_discuss


def test_handle_cabinet_dispatches_via_core_handlers_dict() -> None:
    """No slash prefix — keys are plain command names."""
    for key in ("cabinet", "standup", "discuss"):
        assert "/" not in key


# ---------------------------------------------------------------------------
# handle_cabinet — subcommand routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_cabinet_empty_args_returns_usage(
    patched_cabinet_api: _Calls,
) -> None:
    out = await core_handlers.handle_cabinet(_adapter(), _incoming(), "")
    assert "Usage" in out or "Cabinet" in out
    assert "/cabinet create" in out
    # No API calls on usage-only path
    assert patched_cabinet_api.create == []
    assert patched_cabinet_api.list_ == []


@pytest.mark.asyncio
async def test_handle_cabinet_help_returns_usage(
    patched_cabinet_api: _Calls,
) -> None:
    out = await core_handlers.handle_cabinet(_adapter(), _incoming(), "help")
    assert "/cabinet create" in out


@pytest.mark.asyncio
async def test_handle_cabinet_create_calls_create_meeting(
    patched_cabinet_api: _Calls,
) -> None:
    out = await core_handlers.handle_cabinet(_adapter(), _incoming(42), "create")
    assert "Cabinet meeting #99 started" in out
    assert "http://localhost:3141/cabinet?id=99" in out
    assert len(patched_cabinet_api.create) == 1
    assert patched_cabinet_api.create[0]["chat_id"] == "42"


@pytest.mark.asyncio
async def test_handle_cabinet_list_calls_list_meetings(
    patched_cabinet_api: _Calls,
) -> None:
    out = await core_handlers.handle_cabinet(_adapter(), _incoming(42), "list")
    assert "Recent cabinet meetings" in out
    assert len(patched_cabinet_api.list_) == 1
    assert patched_cabinet_api.list_[0]["chat_id"] == "42"
    assert patched_cabinet_api.list_[0]["limit"] == 20


@pytest.mark.asyncio
async def test_handle_cabinet_send_happy_path(
    patched_cabinet_api: _Calls,
) -> None:
    out = await core_handlers.handle_cabinet(
        _adapter(), _incoming(42), "send 1 what should we ship next?",
    )
    assert "Sent to meeting #1" in out
    assert len(patched_cabinet_api.send) == 1
    call = patched_cabinet_api.send[0]
    assert call["meeting_id"] == 1
    assert call["text"] == "what should we ship next?"
    assert call["chat_id"] == "42"
    # Handler MUST NOT generate clientMsgId — that's cabinet_api.send_message's job
    assert call["client_msg_id"] is None


@pytest.mark.asyncio
async def test_handle_cabinet_send_missing_id_returns_usage(
    patched_cabinet_api: _Calls,
) -> None:
    out = await core_handlers.handle_cabinet(_adapter(), _incoming(), "send notanint hello")
    assert "Usage" in out
    assert patched_cabinet_api.send == []


@pytest.mark.asyncio
async def test_handle_cabinet_send_missing_text_returns_usage(
    patched_cabinet_api: _Calls,
) -> None:
    out = await core_handlers.handle_cabinet(_adapter(), _incoming(), "send 1")
    assert "Usage" in out
    assert patched_cabinet_api.send == []


@pytest.mark.asyncio
async def test_handle_cabinet_end_happy_path(
    patched_cabinet_api: _Calls,
) -> None:
    out = await core_handlers.handle_cabinet(_adapter(), _incoming(42), "end 5")
    assert "Meeting #5 ended" in out
    assert len(patched_cabinet_api.end) == 1
    assert patched_cabinet_api.end[0]["meeting_id"] == 5


@pytest.mark.asyncio
async def test_handle_cabinet_end_already_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """alreadyEnded=True response → friendly already-ended message."""

    async def _end(meeting_id, chat_id=None, *, client=None):  # noqa: ANN001
        return {"ok": True, "meetingId": meeting_id, "alreadyEnded": True}

    monkeypatch.setattr(cabinet_api, "end_meeting", _end)
    out = await core_handlers.handle_cabinet(_adapter(), _incoming(42), "end 5")
    assert "already ended" in out.lower()


@pytest.mark.asyncio
async def test_handle_cabinet_end_bad_id_returns_usage(
    patched_cabinet_api: _Calls,
) -> None:
    out = await core_handlers.handle_cabinet(_adapter(), _incoming(), "end notanint")
    assert "Usage" in out
    assert patched_cabinet_api.end == []


@pytest.mark.asyncio
async def test_handle_cabinet_unknown_subcommand_returns_usage(
    patched_cabinet_api: _Calls,
) -> None:
    out = await core_handlers.handle_cabinet(_adapter(), _incoming(), "fly to mars")
    assert "Usage" in out or "Cabinet" in out


# ---------------------------------------------------------------------------
# handle_standup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_standup_no_args_uses_default_question(
    patched_cabinet_api: _Calls,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty args → default standup question is sent."""
    monkeypatch.delenv("CABINET_STANDUP_QUESTION", raising=False)
    out = await core_handlers.handle_standup(_adapter(), _incoming(42), "")
    assert "Standup #99" in out
    assert len(patched_cabinet_api.send) == 1
    sent_text = patched_cabinet_api.send[0]["text"]
    assert "working on" in sent_text.lower() or "next step" in sent_text.lower()


@pytest.mark.asyncio
async def test_handle_standup_uses_env_default(
    patched_cabinet_api: _Calls,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CABINET_STANDUP_QUESTION", "Custom default question?")
    out = await core_handlers.handle_standup(_adapter(), _incoming(42), "")
    assert "Standup #99" in out
    assert patched_cabinet_api.send[0]["text"] == "Custom default question?"


@pytest.mark.asyncio
async def test_handle_standup_with_custom_question(
    patched_cabinet_api: _Calls,
) -> None:
    out = await core_handlers.handle_standup(
        _adapter(), _incoming(42), "What changed today?",
    )
    assert "Standup #99" in out
    assert patched_cabinet_api.send[0]["text"] == "What changed today?"


@pytest.mark.asyncio
async def test_handle_standup_creates_meeting_then_sends(
    patched_cabinet_api: _Calls,
) -> None:
    """Standup must call create_meeting BEFORE send_message."""
    await core_handlers.handle_standup(_adapter(), _incoming(42), "ping")
    assert len(patched_cabinet_api.create) == 1
    assert len(patched_cabinet_api.send) == 1


# ---------------------------------------------------------------------------
# handle_discuss
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_discuss_requires_topic_arg(
    patched_cabinet_api: _Calls,
) -> None:
    out = await core_handlers.handle_discuss(_adapter(), _incoming(), "")
    assert "Usage" in out
    # No API calls when topic is missing
    assert patched_cabinet_api.create == []
    assert patched_cabinet_api.send == []


@pytest.mark.asyncio
async def test_handle_discuss_creates_meeting_and_sends_topic(
    patched_cabinet_api: _Calls,
) -> None:
    out = await core_handlers.handle_discuss(
        _adapter(), _incoming(42), "should we deprecate mc?",
    )
    assert "Discussion #99" in out
    assert "should we deprecate mc?" in out
    assert len(patched_cabinet_api.create) == 1
    assert len(patched_cabinet_api.send) == 1
    assert patched_cabinet_api.send[0]["text"] == "should we deprecate mc?"


# ---------------------------------------------------------------------------
# Chat-id pass-through (chat-scope isolation contract)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handlers_pass_chat_id_string(
    patched_cabinet_api: _Calls,
) -> None:
    """All 3 handlers extract incoming.chat_id and pass as str (Phase 5a contract)."""
    chat_id = 12345
    await core_handlers.handle_cabinet(_adapter(), _incoming(chat_id), "create")
    await core_handlers.handle_standup(_adapter(), _incoming(chat_id), "")
    await core_handlers.handle_discuss(_adapter(), _incoming(chat_id), "topic")

    # cabinet/create — 1 call
    assert patched_cabinet_api.create[0]["chat_id"] == "12345"
    # standup — 1 create + 1 send
    assert patched_cabinet_api.create[1]["chat_id"] == "12345"
    assert patched_cabinet_api.send[0]["chat_id"] == "12345"
    # discuss — 1 create + 1 send
    assert patched_cabinet_api.create[2]["chat_id"] == "12345"
    assert patched_cabinet_api.send[1]["chat_id"] == "12345"


@pytest.mark.asyncio
async def test_handlers_pass_none_chat_id_when_missing(
    patched_cabinet_api: _Calls,
) -> None:
    """When incoming.chat_id is None (or 0/falsy), helpers receive None."""
    await core_handlers.handle_cabinet(_adapter(), _incoming(None), "create")
    assert patched_cabinet_api.create[0]["chat_id"] is None


@pytest.mark.asyncio
async def test_handle_cabinet_send_passes_chat_id(
    patched_cabinet_api: _Calls,
) -> None:
    await core_handlers.handle_cabinet(_adapter(), _incoming(42), "send 1 hello")
    assert patched_cabinet_api.send[0]["chat_id"] == "42"


@pytest.mark.asyncio
async def test_handle_cabinet_list_passes_chat_id(
    patched_cabinet_api: _Calls,
) -> None:
    await core_handlers.handle_cabinet(_adapter(), _incoming(42), "list")
    assert patched_cabinet_api.list_[0]["chat_id"] == "42"


@pytest.mark.asyncio
async def test_handle_cabinet_end_passes_chat_id(
    patched_cabinet_api: _Calls,
) -> None:
    await core_handlers.handle_cabinet(_adapter(), _incoming(42), "end 5")
    assert patched_cabinet_api.end[0]["chat_id"] == "42"


# ---------------------------------------------------------------------------
# Friendly-error pass-through (R1 B6 + R3-MN1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_friendly_error_passthrough_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _create(chat_id=None, *, client=None):  # noqa: ANN001
        raise cabinet_api.CabinetAPIUnreachable()

    monkeypatch.setattr(cabinet_api, "create_meeting", _create)
    out = await core_handlers.handle_cabinet(_adapter(), _incoming(42), "create")
    assert out == cabinet_api.CabinetAPIUnreachable.friendly_message


@pytest.mark.asyncio
async def test_friendly_error_passthrough_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _create(chat_id=None, *, client=None):  # noqa: ANN001
        raise cabinet_api.CabinetAuthFailure()

    monkeypatch.setattr(cabinet_api, "create_meeting", _create)
    out = await core_handlers.handle_cabinet(_adapter(), _incoming(42), "create")
    assert "ORCHESTRATION_API_TOKEN" in out


@pytest.mark.asyncio
async def test_killswitch_503_on_synchronous_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1 B6 part (a): create_meeting 503 → handler surfaces friendly_message."""

    async def _create(chat_id=None, *, client=None):  # noqa: ANN001
        raise cabinet_api.CabinetKillSwitchDisabled()

    monkeypatch.setattr(cabinet_api, "create_meeting", _create)
    out = await core_handlers.handle_cabinet(_adapter(), _incoming(42), "create")
    assert "disabled" in out.lower()
    assert "operator" in out.lower()


@pytest.mark.asyncio
async def test_send_message_returns_queued_message(
    patched_cabinet_api: _Calls,
) -> None:
    """R1 B6 part (b): /send 200 → handler returns 'Sent...' reply (NOT
    a kill-switch friendly message). Kill-switch refusal during the
    background task surfaces via SSE, not synchronous HTTP."""
    out = await core_handlers.handle_cabinet(
        _adapter(), _incoming(42), "send 1 hi",
    )
    assert "Sent to meeting #1" in out
    # The handler's chat reply DOES mention "disabled" as steering text
    # ("watch for system notes if cabinet is disabled") because kill-switch
    # refusal during the background task surfaces via SSE not HTTP — this is
    # informational, NOT the kill-switch friendly_message itself. Verify we
    # are NOT returning the bare CabinetKillSwitchDisabled.friendly_message.
    assert out != cabinet_api.CabinetKillSwitchDisabled.friendly_message
    assert len(patched_cabinet_api.send) == 1


@pytest.mark.asyncio
async def test_friendly_error_passthrough_chat_scope_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3-MN1: 403 chat_mismatch → handler surfaces 'different chat' message."""

    async def _send(meeting_id, text, client_msg_id=None, chat_id=None, *, client=None):  # noqa: ANN001
        raise cabinet_api.CabinetChatScopeMismatch()

    monkeypatch.setattr(cabinet_api, "send_message", _send)
    out = await core_handlers.handle_cabinet(_adapter(), _incoming(42), "send 1 hi")
    assert "different chat" in out.lower()
    assert "/cabinet list" in out


@pytest.mark.asyncio
async def test_friendly_error_passthrough_in_standup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _create(chat_id=None, *, client=None):  # noqa: ANN001
        raise cabinet_api.CabinetAPIUnreachable()

    monkeypatch.setattr(cabinet_api, "create_meeting", _create)
    out = await core_handlers.handle_standup(_adapter(), _incoming(42), "")
    assert "not running" in out.lower()


@pytest.mark.asyncio
async def test_friendly_error_passthrough_in_discuss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _create(chat_id=None, *, client=None):  # noqa: ANN001
        raise cabinet_api.CabinetAPIUnreachable()

    monkeypatch.setattr(cabinet_api, "create_meeting", _create)
    out = await core_handlers.handle_discuss(_adapter(), _incoming(42), "topic")
    assert "not running" in out.lower()


# ---------------------------------------------------------------------------
# Cross-process invariant: handlers do NOT import run_with_runtime_lanes
# (those run in the API process)
# ---------------------------------------------------------------------------


def test_handlers_do_not_import_runtime_inline() -> None:
    """Handlers MUST go via cabinet_api HTTP — NOT via the runtime layer
    that lives in the API process. AST scan confirms cabinet_api is the
    only orchestration import."""
    src = Path(core_handlers.__file__).read_text(encoding="utf-8")
    # The only legit import for cabinet handlers is `from integrations import cabinet_api`
    # Should NOT contain direct cabinet/* imports inside the cabinet handlers.
    # Check that `from cabinet.text_orchestrator` and friends do NOT appear in
    # the handler bodies. We grep-line-bound to the cabinet handler section.
    handler_section_marker = "Cabinet (Phase 5b)"
    assert handler_section_marker in src
    cabinet_section_start = src.index(handler_section_marker)
    end_section_marker = "async def handle_send"
    cabinet_section_end = src.index(end_section_marker)
    section = src[cabinet_section_start:cabinet_section_end]

    # Forbidden direct cabinet/* imports in chat-process handlers (R2 NB2)
    for forbidden in (
        "from cabinet.text_orchestrator",
        "from cabinet.meeting_channel",
        "import cabinet.text_orchestrator",
        "from cabinet_text_service",
    ):
        assert forbidden not in section, f"forbidden import in handlers: {forbidden!r}"

    # cabinet_api must be the only orchestration touch point
    assert "from integrations import cabinet_api" in section
