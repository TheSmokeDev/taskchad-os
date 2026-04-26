from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.telegram import TelegramAdapter
from models import Attachment, Channel, OutgoingMessage, Platform


class FakeTelegramBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._next_id = 100

    async def send_photo(self, **kwargs):
        self.calls.append(("send_photo", kwargs))
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)

    async def send_document(self, **kwargs):
        self.calls.append(("send_document", kwargs))
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)

    async def send_message(self, **kwargs):
        self.calls.append(("send_message", kwargs))
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)


def _adapter_with_fake_bot(bot: FakeTelegramBot) -> TelegramAdapter:
    adapter = TelegramAdapter.__new__(TelegramAdapter)
    adapter._app = SimpleNamespace(bot=bot)
    adapter._sent_messages = {}
    adapter._callback_id_map = {}
    adapter._voice_reply_threads = set()
    return adapter


def _channel() -> Channel:
    return Channel(platform=Platform.TELEGRAM, platform_id="123", is_dm=True)


def test_extract_media_directive_removes_path_from_text() -> None:
    text, media = TelegramAdapter._extract_media_directives(
        "Here it is\nMEDIA:C:\\tmp\\portrait.png\nDone"
    )

    assert text == "Here it is\nDone"
    assert len(media) == 1
    assert media[0].source == "C:\\tmp\\portrait.png"


@pytest.mark.asyncio
async def test_send_media_directive_uploads_photo_without_echoing_path(tmp_path: Path) -> None:
    image_path = tmp_path / "portrait.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    bot = FakeTelegramBot()
    adapter = _adapter_with_fake_bot(bot)

    first_id = await adapter.send(
        OutgoingMessage(
            text=f"Native image output\nMEDIA:{image_path}",
            channel=_channel(),
        )
    )

    assert first_id == "101"
    assert [name for name, _ in bot.calls] == ["send_photo"]
    call = bot.calls[0][1]
    assert call["caption"] == "Native image output"
    assert Path(call["photo"].name).name == image_path.name


@pytest.mark.asyncio
async def test_send_attachment_uses_document_for_non_image(tmp_path: Path) -> None:
    doc_path = tmp_path / "report.pdf"
    doc_path.write_bytes(b"%PDF-1.7")
    bot = FakeTelegramBot()
    adapter = _adapter_with_fake_bot(bot)

    await adapter.send(
        OutgoingMessage(
            text="Report attached",
            channel=_channel(),
            attachments=[
                Attachment(
                    filename="report.pdf",
                    mimetype="application/pdf",
                    url=str(doc_path),
                )
            ],
        )
    )

    assert [name for name, _ in bot.calls] == ["send_document"]
    assert bot.calls[0][1]["caption"] == "Report attached"
