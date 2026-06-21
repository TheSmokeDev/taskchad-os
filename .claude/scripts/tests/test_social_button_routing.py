"""Tests for social-draft button routing in the chat router.

Exercises ChatRouter._handle_social_button in isolation (bound to a light
shim) so we don't stand up a full engine/manager. Covers the Step-2 auth
guard (only genuine button taps), the approve->dispatch sequencing gated on
real DB status, reject, malformed input, and unknown actions.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from router import ChatRouter


class _RecordingAdapter:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, message) -> None:
        self.sent.append(message.text)


def _shim(approved: bool):
    """A minimal object carrying the real router methods under test."""
    obj = SimpleNamespace()
    obj._handle_social_button = ChatRouter._handle_social_button.__get__(obj)
    obj._social_edit_reply = ChatRouter._social_edit_reply.__get__(obj)
    obj._social_post_is_approved = lambda pid: approved
    return obj


def _incoming(*, button: bool):
    raw_event = {"interaction_type": "button"} if button else {}
    return SimpleNamespace(raw_event=raw_event, channel=None, thread=None)


def test_social_button_is_immediate_so_active_turn_does_not_block_tap():
    incoming = SimpleNamespace(text="__button:social:approve:5")

    assert ChatRouter._is_immediate_button(incoming) is True


@pytest.fixture()
def fake_core_handlers(monkeypatch):
    calls: list[str] = []

    async def fake_handle_social(adapter, incoming, args, *, collect_only=False):
        calls.append(args)
        if args.startswith("approve"):
            return "Post #5 approved (linkedin). Dispatch: /social post 5"
        if args.startswith("post"):
            return "Post #5 dispatched successfully. URL: http://x"
        if args.startswith("reject"):
            return "Post #5 rejected."
        return "?"

    mod = types.ModuleType("core_handlers")
    mod.handle_social = fake_handle_social
    monkeypatch.setitem(sys.modules, "core_handlers", mod)
    return calls


@pytest.mark.asyncio
async def test_non_button_interaction_is_refused(fake_core_handlers):
    """Step-2 HIGH fix: a synthesized __button:social:* from a non-button
    ingress must NOT trigger a write."""
    adapter = _RecordingAdapter()
    obj = _shim(approved=True)
    await obj._handle_social_button(adapter, _incoming(button=False), "social:approve:5")
    assert fake_core_handlers == []  # handle_social never called
    assert "only run from the draft buttons" in adapter.sent[0]


@pytest.mark.asyncio
async def test_approve_runs_approve_then_post_when_approved(fake_core_handlers):
    adapter = _RecordingAdapter()
    obj = _shim(approved=True)
    await obj._handle_social_button(adapter, _incoming(button=True), "social:approve:5")
    assert fake_core_handlers == ["approve 5", "post 5"]
    assert "dispatched successfully" in adapter.sent[-1]


@pytest.mark.asyncio
async def test_approve_does_not_post_when_not_approved(fake_core_handlers):
    """If the post never reached 'approved' state, dispatch must NOT run."""
    adapter = _RecordingAdapter()
    obj = _shim(approved=False)
    await obj._handle_social_button(adapter, _incoming(button=True), "social:approve:5")
    assert fake_core_handlers == ["approve 5"]  # no "post 5"
    assert "approved" in adapter.sent[-1].lower()


@pytest.mark.asyncio
async def test_reject_routes_to_reject(fake_core_handlers):
    adapter = _RecordingAdapter()
    obj = _shim(approved=False)
    await obj._handle_social_button(adapter, _incoming(button=True), "social:reject:5")
    assert fake_core_handlers == ["reject 5"]
    assert "rejected" in adapter.sent[-1].lower()


@pytest.mark.asyncio
async def test_malformed_custom_id_is_rejected(fake_core_handlers):
    adapter = _RecordingAdapter()
    obj = _shim(approved=True)
    await obj._handle_social_button(adapter, _incoming(button=True), "social:approve:notanid")
    assert fake_core_handlers == []
    assert "malformed" in adapter.sent[0].lower()


@pytest.mark.asyncio
async def test_unknown_action_is_reported(fake_core_handlers):
    adapter = _RecordingAdapter()
    obj = _shim(approved=True)
    await obj._handle_social_button(adapter, _incoming(button=True), "social:frobnicate:5")
    assert fake_core_handlers == []
    assert "unknown social action" in adapter.sent[-1].lower()


def test_social_post_is_approved_reads_real_db(monkeypatch, tmp_path):
    """The REAL status gate (not the stub): draft=False, approved=True,
    missing=False (fail-closed). This is what authorizes a live post."""
    import config
    from social.service import SocialPostService

    db = tmp_path / "orch.db"
    monkeypatch.setattr(config, "ORCHESTRATION_DB_PATH", db)
    svc = SocialPostService(db_path=db)
    pid = svc.create_draft(channel="linkedin", title="t", body="b")

    obj = SimpleNamespace()
    helper = ChatRouter._social_post_is_approved.__get__(obj)

    assert helper(pid) is False  # draft is not approved
    svc.approve_post(pid)
    assert helper(pid) is True  # now approved
    assert helper(999999) is False  # missing -> fail closed
