"""Guided /linkedin Cook Together / Run It for Me workflow tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import core_handlers
from router import ChatRouter
from social import linkedin_workshop


def _incoming(
    text: str = "",
    *,
    button: bool = False,
    channel_id: str = "100",
    user_id: str = "200",
) -> SimpleNamespace:
    channel = SimpleNamespace(platform="telegram", platform_id=channel_id)
    return SimpleNamespace(
        text=text,
        channel=channel,
        platform="telegram",
        thread=None,
        user=SimpleNamespace(platform_id=user_id),
        raw_event={"interaction_type": "button"} if button else {},
    )


class FakeAdapter:
    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, message) -> str:
        self.sent.append(message)
        return str(len(self.sent))

    @property
    def texts(self) -> list[str]:
        return [m.text for m in self.sent]

    def custom_ids(self) -> list[str]:
        return [c.custom_id for c in self.sent[-1].components]


@pytest.fixture(autouse=True)
def _clean_state():
    core_handlers._LINKEDIN_PENDING.clear()
    yield
    core_handlers._LINKEDIN_PENDING.clear()


def _post(post_id: int = 41, *, body: str = "Draft body", media_path: str = ""):
    return SimpleNamespace(id=post_id, body=body, media_path=media_path)


def test_linkedin_is_router_handler() -> None:
    assert core_handlers.CORE_HANDLERS["linkedin"] is core_handlers.handle_linkedin


def test_linkedin_flow_button_is_immediate() -> None:
    incoming = SimpleNamespace(text="__button:linkedin_flow:mode:cook")
    assert ChatRouter._is_immediate_button(incoming) is True


@pytest.mark.asyncio
async def test_bare_linkedin_offers_two_modes() -> None:
    adapter = FakeAdapter()
    incoming = _incoming()

    result = await core_handlers.handle_linkedin(adapter, incoming, "")

    assert result is None
    assert "Cook Together" in adapter.texts[-1]
    assert adapter.custom_ids()[:2] == [
        "linkedin_flow:mode:cook",
        "linkedin_flow:mode:run",
    ]
    key = core_handlers._linkedin_channel_key(incoming)
    assert core_handlers._LINKEDIN_PENDING[key]["stage"] == "await_mode"


@pytest.mark.asyncio
async def test_cook_button_then_topic_generates_approval_preview(monkeypatch) -> None:
    seen: dict = {}

    def fake_create(*, topic, mode, db_path=None):
        seen.update(topic=topic, mode=mode, db_path=db_path)
        return _post()

    monkeypatch.setattr(linkedin_workshop, "create_linkedin_draft", fake_create)
    adapter = FakeAdapter()
    incoming = _incoming(button=True)

    await core_handlers.handle_linkedin(adapter, incoming, "")
    await core_handlers.handle_linkedin_button(
        adapter, incoming, "linkedin_flow:mode:cook"
    )
    typed = _incoming("What I learned repairing a real browser workflow")
    assert await core_handlers.try_consume_linkedin_message(adapter, typed) is True

    assert seen["mode"] == "cook"
    assert "repairing a real browser workflow" in seen["topic"]
    assert "social:approve:41" in adapter.custom_ids()
    assert "linkedin_flow:revise:41" in adapter.custom_ids()
    assert "linkedin_flow:image:41" in adapter.custom_ids()
    assert "social:reject:41" in adapter.custom_ids()


@pytest.mark.asyncio
async def test_run_button_generates_without_topic(monkeypatch) -> None:
    seen: dict = {}

    def fake_create(*, topic, mode, db_path=None):
        seen.update(topic=topic, mode=mode)
        return _post(42)

    monkeypatch.setattr(linkedin_workshop, "create_linkedin_draft", fake_create)
    adapter = FakeAdapter()
    incoming = _incoming(button=True)

    await core_handlers.handle_linkedin(adapter, incoming, "")
    await core_handlers.handle_linkedin_button(
        adapter, incoming, "linkedin_flow:mode:run"
    )

    assert seen == {"topic": None, "mode": "run"}
    assert "social:approve:42" in adapter.custom_ids()


@pytest.mark.asyncio
async def test_review_reply_revises_copy_in_place(monkeypatch) -> None:
    seen: dict = {}

    def fake_revise(post_id, feedback, *, db_path=None):
        seen.update(post_id=post_id, feedback=feedback)
        return _post(post_id, body="Revised body")

    monkeypatch.setattr(linkedin_workshop, "revise_linkedin_copy", fake_revise)
    adapter = FakeAdapter()
    incoming = _incoming()
    key = core_handlers._linkedin_channel_key(incoming)
    core_handlers._linkedin_workshop_set(
        key, stage="await_review", post_id=55, mode="cook"
    )

    assert await core_handlers.try_consume_linkedin_message(
        adapter, _incoming("Make the hook more direct")
    )

    assert seen == {"post_id": 55, "feedback": "Make the hook more direct"}
    assert "Revised body" in adapter.texts[-1]
    assert "social:approve:55" in adapter.custom_ids()


@pytest.mark.asyncio
async def test_image_direction_regenerates_same_draft(monkeypatch) -> None:
    seen: dict = {}

    def fake_image(post_id, direction, *, db_path=None):
        seen.update(post_id=post_id, direction=direction)
        return _post(post_id, media_path="")

    monkeypatch.setattr(linkedin_workshop, "regenerate_linkedin_image", fake_image)
    adapter = FakeAdapter()
    incoming = _incoming()
    key = core_handlers._linkedin_channel_key(incoming)
    core_handlers._linkedin_workshop_set(key, stage="await_review", post_id=56)

    assert await core_handlers.try_consume_linkedin_message(
        adapter, _incoming("image: darker editorial control room")
    )

    assert seen == {
        "post_id": 56,
        "direction": "darker editorial control room",
    }
    assert "social:approve:56" in adapter.custom_ids()


@pytest.mark.asyncio
async def test_synthetic_workshop_button_is_refused(monkeypatch) -> None:
    called = False

    def fake_create(*, topic, mode, db_path=None):
        nonlocal called
        called = True
        return _post()

    monkeypatch.setattr(linkedin_workshop, "create_linkedin_draft", fake_create)
    adapter = FakeAdapter()

    await core_handlers.handle_linkedin_button(
        adapter,
        _incoming(button=False),
        "linkedin_flow:mode:run",
    )

    assert called is False
    assert "only run from the displayed buttons" in adapter.texts[-1]


@pytest.mark.asyncio
async def test_commands_and_unmatched_mode_text_fall_through() -> None:
    adapter = FakeAdapter()
    incoming = _incoming()
    await core_handlers.handle_linkedin(adapter, incoming, "")

    assert not await core_handlers.try_consume_linkedin_message(
        adapter, _incoming("/status")
    )
    assert not await core_handlers.try_consume_linkedin_message(
        adapter, _incoming("unrelated conversation")
    )
