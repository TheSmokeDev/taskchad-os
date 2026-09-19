"""Guided /linkedin Cook Together / Run It for Me workflow tests."""

from __future__ import annotations

from types import SimpleNamespace

import core_handlers
import pytest
from models import Platform
from router import ChatRouter

from social import linkedin_workshop
from social.models import SocialPost, approval_binding_digest


def _incoming(
    text: str = "",
    *,
    button: bool = False,
    channel_id: str = "100",
    user_id: str = "200",
) -> SimpleNamespace:
    channel = SimpleNamespace(platform=Platform.TELEGRAM, platform_id=channel_id)
    return SimpleNamespace(
        text=text,
        channel=channel,
        platform=Platform.TELEGRAM,
        thread=None,
        user=SimpleNamespace(platform_id=user_id),
        raw_event={"interaction_type": "button", "source_message_is_own": True} if button else {},
    )


class FakeAdapter:
    def __init__(self) -> None:
        self.sent: list = []
        self._app = SimpleNamespace(bot=SimpleNamespace(token="fake-review-token"))

    async def send(self, message) -> str:
        self.sent.append(message)
        return str(len(self.sent))

    @property
    def texts(self) -> list[str]:
        return [m.text for m in self.sent]

    def custom_ids(self) -> list[str]:
        return [c.custom_id for c in self.sent[-1].components]


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    from models import MessageComponent, OutgoingMessage

    from social import notify

    adapters = []
    original_init = FakeAdapter.__init__

    def init(self):
        original_init(self)
        adapters.append(self)

    def deliver(post, **kwargs):
        adapter = adapters[-1]
        adapter.sent.append(OutgoingMessage(
            text=post.body, channel=None, components=[
                MessageComponent(label=button["text"], custom_id=button["callback_data"])
                for row in notify._build_reply_markup(post)["inline_keyboard"] for button in row
            ],
        ))
        return True

    monkeypatch.setattr(FakeAdapter, "__init__", init)
    monkeypatch.setattr(notify, "deliver_draft_to_telegram", deliver)
    core_handlers._LINKEDIN_PENDING.clear()
    yield
    core_handlers._LINKEDIN_PENDING.clear()


def _post(post_id: int = 41, *, body: str = "Draft body", media_path: str = ""):
    return SocialPost(id=post_id, channel="linkedin", body=body, media_path=media_path)


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
    assert {cid.split(":")[1] for cid in adapter.custom_ids()} == {
        "approve", "edit", "image", "reject",
    }
    assert all(cid.split(":")[2] == "41" for cid in adapter.custom_ids())


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
    assert any(cid.startswith("social:approve:42:") for cid in adapter.custom_ids())


@pytest.mark.asyncio
async def test_review_reply_revises_copy_in_place(monkeypatch) -> None:
    seen: dict = {}

    def fake_revise(post_id, feedback, *, db_path=None, **binding):
        seen.update(post_id=post_id, feedback=feedback)
        return _post(post_id, body="Revised body")

    monkeypatch.setattr(linkedin_workshop, "revise_linkedin_copy", fake_revise)
    adapter = FakeAdapter()
    incoming = _incoming()
    key = core_handlers._linkedin_channel_key(incoming)
    core_handlers._linkedin_workshop_set(
        key, stage="await_review", post_id=55, mode="cook",
        expected_revision=1, expected_digest=approval_binding_digest(_post(55)),
    )

    assert await core_handlers.try_consume_linkedin_message(
        adapter, _incoming("Make the hook more direct")
    )

    assert seen == {"post_id": 55, "feedback": "Make the hook more direct"}
    assert "Revised body" in adapter.texts[-1]
    assert any(cid.startswith("social:approve:55:") for cid in adapter.custom_ids())


@pytest.mark.asyncio
async def test_image_direction_regenerates_same_draft(monkeypatch) -> None:
    seen: dict = {}

    def fake_image(post_id, direction, *, db_path=None, **binding):
        seen.update(post_id=post_id, direction=direction)
        return _post(post_id, media_path="")

    monkeypatch.setattr(linkedin_workshop, "regenerate_linkedin_image", fake_image)
    adapter = FakeAdapter()
    incoming = _incoming()
    key = core_handlers._linkedin_channel_key(incoming)
    core_handlers._linkedin_workshop_set(key, stage="await_review", post_id=56,
                                       expected_revision=1,
                                       expected_digest=approval_binding_digest(_post(56)))

    assert await core_handlers.try_consume_linkedin_message(
        adapter, _incoming("image: darker editorial control room")
    )

    assert seen == {
        "post_id": 56,
        "direction": "darker editorial control room",
    }
    assert any(cid.startswith("social:approve:56:") for cid in adapter.custom_ids())


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


@pytest.mark.asyncio
@pytest.mark.parametrize("image", [False, True])
async def test_followup_keeps_original_clicked_revision(monkeypatch, image):
    seen = {}

    def revise(post_id, text, **kwargs):
        seen.update(kwargs)
        return _post(post_id)

    monkeypatch.setattr(linkedin_workshop, "revise_linkedin_copy", revise)
    monkeypatch.setattr(linkedin_workshop, "regenerate_linkedin_image", revise)
    adapter = FakeAdapter()
    incoming = _incoming("different composition" if image else "Make it direct")
    core_handlers._linkedin_workshop_set(
        core_handlers._linkedin_channel_key(incoming),
        stage="await_image" if image else "await_revision", post_id=250,
        expected_revision=2, expected_digest="abcdef123456",
    )
    assert await core_handlers.try_consume_linkedin_message(adapter, incoming)
    assert seen == {"expected_revision": 2, "expected_digest": "abcdef123456"}


@pytest.mark.asyncio
async def test_preview_passes_exact_adapter_recipient_and_thread(monkeypatch):
    from social import notify

    seen = {}

    def deliver(post, **kwargs):
        seen.update(kwargs)
        return True

    monkeypatch.setattr(notify, "deliver_draft_to_telegram", deliver)
    incoming = _incoming(channel_id="999")
    incoming.thread = SimpleNamespace(parent_message_id="888")
    await core_handlers._send_linkedin_preview(FakeAdapter(), incoming, _post())
    assert seen == {
        "token": "fake-review-token", "chat_id": "999", "reply_to_message_id": "888",
        "delivery_request_id": None,
    }


@pytest.mark.asyncio
async def test_unbound_legacy_workshop_cannot_revise(monkeypatch):
    adapter = FakeAdapter()
    incoming = _incoming("replace it")
    core_handlers._linkedin_workshop_set(
        core_handlers._linkedin_channel_key(incoming), stage="await_revision", post_id=250,
    )
    assert await core_handlers.try_consume_linkedin_message(adapter, incoming)
    assert "no bound revision" in adapter.texts[-1]
