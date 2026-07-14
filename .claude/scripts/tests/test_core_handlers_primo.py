"""Guided /primo copy plus image workshop tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import core_handlers
from router import ChatRouter
from social import primo_workshop


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
    core_handlers._PRIMO_PENDING.clear()
    core_handlers._LINKEDIN_PENDING.clear()
    yield
    core_handlers._PRIMO_PENDING.clear()
    core_handlers._LINKEDIN_PENDING.clear()


def _post(post_id: int = 71, *, body: str = "Primo body", media_path: str = ""):
    return SimpleNamespace(id=post_id, body=body, media_path=media_path)


def test_primo_is_router_handler_and_buttons_are_immediate() -> None:
    assert core_handlers.CORE_HANDLERS["primo"] is core_handlers.handle_primo
    incoming = SimpleNamespace(text="__button:primo_flow:mode:cook")
    assert ChatRouter._is_immediate_button(incoming) is True


@pytest.mark.asyncio
async def test_bare_primo_offers_two_modes_and_clears_linkedin() -> None:
    adapter = FakeAdapter()
    incoming = _incoming()
    key = core_handlers._linkedin_channel_key(incoming)
    core_handlers._linkedin_workshop_set(key, stage="await_review", post_id=9)

    result = await core_handlers.handle_primo(adapter, incoming, "")

    assert result is None
    assert "Cook Together" in adapter.texts[-1]
    assert adapter.custom_ids()[:2] == [
        "primo_flow:mode:cook",
        "primo_flow:mode:run",
    ]
    assert key not in core_handlers._LINKEDIN_PENDING
    assert core_handlers._PRIMO_PENDING[key]["stage"] == "await_mode"


@pytest.mark.asyncio
async def test_cook_topic_then_image_choice_generates_photo_preview(
    monkeypatch, tmp_path: Path
) -> None:
    image = tmp_path / "primo.png"
    image.write_bytes(b"png")
    seen: dict = {}

    def fake_create(*, topic, mode, media_mode, db_path=None):
        seen.update(topic=topic, mode=mode, media_mode=media_mode)
        return _post(media_path=str(image))

    monkeypatch.setattr(primo_workshop, "create_primo_draft", fake_create)
    adapter = FakeAdapter()
    button = _incoming(button=True)

    await core_handlers.handle_primo(adapter, button, "")
    await core_handlers.handle_primo_button(adapter, button, "primo_flow:mode:cook")
    assert await core_handlers.try_consume_primo_message(
        adapter, _incoming("What agent wallets can prove")
    )
    assert "primo_flow:media:image" in adapter.custom_ids()
    await core_handlers.handle_primo_button(adapter, button, "primo_flow:media:image")

    assert seen == {
        "topic": "What agent wallets can prove",
        "mode": "cook",
        "media_mode": "image",
    }
    assert "social:approve:71" in adapter.custom_ids()
    assert "primo_flow:remove:71" in adapter.custom_ids()
    assert adapter.sent[-1].attachments


@pytest.mark.asyncio
async def test_run_mode_waits_for_media_then_text_only_generates(monkeypatch) -> None:
    seen: dict = {}

    def fake_create(*, topic, mode, media_mode, db_path=None):
        seen.update(topic=topic, mode=mode, media_mode=media_mode)
        return _post(72)

    monkeypatch.setattr(primo_workshop, "create_primo_draft", fake_create)
    adapter = FakeAdapter()
    button = _incoming(button=True)

    await core_handlers.handle_primo(adapter, button, "")
    await core_handlers.handle_primo_button(adapter, button, "primo_flow:mode:run")
    assert seen == {}
    assert "primo_flow:media:none" in adapter.custom_ids()
    await core_handlers.handle_primo_button(adapter, button, "primo_flow:media:none")

    assert seen == {"topic": None, "mode": "run", "media_mode": "none"}
    assert "social:approve:72" in adapter.custom_ids()
    assert "primo_flow:remove:72" not in adapter.custom_ids()


@pytest.mark.asyncio
async def test_auto_choice_is_forwarded(monkeypatch) -> None:
    seen: dict = {}

    def fake_create(*, topic, mode, media_mode, db_path=None):
        seen.update(media_mode=media_mode)
        return _post(73)

    monkeypatch.setattr(primo_workshop, "create_primo_draft", fake_create)
    adapter = FakeAdapter()
    button = _incoming(button=True)
    await core_handlers.handle_primo(adapter, button, "run")
    await core_handlers.handle_primo_button(adapter, button, "primo_flow:media:auto")

    assert seen["media_mode"] == "auto"
    assert "social:approve:73" in adapter.custom_ids()


@pytest.mark.asyncio
async def test_explicit_image_failure_has_no_approval_button(monkeypatch) -> None:
    def fake_create(*, topic, mode, media_mode, db_path=None):
        raise primo_workshop.PrimoImageRequiredError(74)

    monkeypatch.setattr(primo_workshop, "create_primo_draft", fake_create)
    adapter = FakeAdapter()
    button = _incoming(button=True)
    await core_handlers.handle_primo(adapter, button, "run")
    await core_handlers.handle_primo_button(adapter, button, "primo_flow:media:image")

    assert "social:approve:74" not in adapter.custom_ids()
    assert "primo_flow:retry:74" in adapter.custom_ids()
    assert "primo_flow:textonly:74" in adapter.custom_ids()


@pytest.mark.asyncio
async def test_remove_image_sends_fresh_text_only_preview(monkeypatch) -> None:
    monkeypatch.setattr(
        primo_workshop,
        "remove_primo_image",
        lambda post_id, **_: _post(post_id, media_path=""),
    )
    adapter = FakeAdapter()
    await core_handlers.handle_primo_button(
        adapter,
        _incoming(button=True),
        "primo_flow:remove:75",
    )

    assert "social:approve:75" in adapter.custom_ids()
    assert "primo_flow:remove:75" not in adapter.custom_ids()


@pytest.mark.asyncio
async def test_synthetic_primo_button_is_refused(monkeypatch) -> None:
    called = False

    def fake_create(*, topic, mode, media_mode, db_path=None):
        nonlocal called
        called = True
        return _post()

    monkeypatch.setattr(primo_workshop, "create_primo_draft", fake_create)
    adapter = FakeAdapter()
    await core_handlers.handle_primo_button(
        adapter,
        _incoming(button=False),
        "primo_flow:media:image",
    )

    assert called is False
    assert "only run from the displayed buttons" in adapter.texts[-1]


@pytest.mark.asyncio
async def test_stale_media_button_cannot_generate(monkeypatch) -> None:
    called = False

    def fake_create(*, topic, mode, media_mode, db_path=None):
        nonlocal called
        called = True
        return _post()

    monkeypatch.setattr(primo_workshop, "create_primo_draft", fake_create)
    adapter = FakeAdapter()
    await core_handlers.handle_primo_button(
        adapter,
        _incoming(button=True),
        "primo_flow:media:image",
    )

    assert called is False
    assert "expired" in adapter.texts[-1]
