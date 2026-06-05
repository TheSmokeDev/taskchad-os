from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import adapters.telegram as telegram_adapter
from adapters.telegram import TelegramAdapter, TelegramDeliveryError
from models import Attachment, Channel, MessageComponent, OutgoingMessage, Platform


class FakeTelegramFile:
    def __init__(self, content: bytes = b"") -> None:
        self.content = content
        self.downloaded_to: str | None = None

    async def download_to_drive(self, path: str) -> None:
        self.downloaded_to = path
        Path(path).write_bytes(self.content)


class FakeTelegramBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._next_id = 100
        self.fail_edit = False
        self.fail_markdown_send = False
        self.fail_plain_send = False
        self.files: dict[str, FakeTelegramFile] = {}

    async def get_file(self, file_id: str) -> FakeTelegramFile:
        return self.files[file_id]

    async def edit_message_text(self, **kwargs):
        self.calls.append(("edit_message_text", kwargs))
        if self.fail_edit:
            raise RuntimeError("telegram edit failed")
        return SimpleNamespace(message_id=kwargs["message_id"])

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
        if kwargs.get("parse_mode") == "Markdown" and self.fail_markdown_send:
            raise RuntimeError("markdown parse failed")
        if kwargs.get("parse_mode") is None and self.fail_plain_send:
            raise RuntimeError("plain send failed")
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)


def _adapter_with_fake_bot(bot: FakeTelegramBot) -> TelegramAdapter:
    adapter = TelegramAdapter.__new__(TelegramAdapter)
    adapter._app = SimpleNamespace(bot=bot)
    adapter._queue = telegram_adapter.asyncio.Queue()
    adapter.allowed_user_ids = []
    adapter._sent_messages = {}
    adapter._callback_id_map = {}
    adapter._voice_reply_threads = set()
    adapter._pending_document_groups = {}
    adapter._pending_document_tasks = {}
    adapter._document_group_delay_seconds = 0.01
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


def test_turn_control_buttons_render_in_one_row() -> None:
    bot = FakeTelegramBot()
    adapter = _adapter_with_fake_bot(bot)

    markup = adapter._build_reply_markup(
        [
            MessageComponent("Queue Next", "turn_queue:abc", "secondary"),
            MessageComponent("Steer Current", "turn_steer:abc", "primary"),
        ]
    )

    assert len(markup.inline_keyboard) == 1
    assert [button.text for button in markup.inline_keyboard[0]] == [
        "Queue Next",
        "Steer Current",
    ]


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


@pytest.mark.asyncio
async def test_on_document_downloads_and_queues_attachment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telegram_adapter.tempfile, "gettempdir", lambda: str(tmp_path))
    bot = FakeTelegramBot()
    bot.files["file-123"] = FakeTelegramFile(b"# Game Plan\n\nShip the adapter.")
    adapter = _adapter_with_fake_bot(bot)
    message = SimpleNamespace(
        document=SimpleNamespace(
            file_id="file-123",
            file_unique_id="unique-123",
            file_name="game-plan.md",
            mime_type="text/markdown",
            file_size=29,
        ),
        from_user=SimpleNamespace(id=123456, first_name="Operator"),
        chat_id=123456,
        chat=SimpleNamespace(type="private"),
        reply_to_message=None,
        caption="Please read this",
        message_id=42,
        media_group_id=None,
        to_dict=lambda: {"message_id": 42, "document": {"file_name": "game-plan.md"}},
    )

    await adapter._on_document(SimpleNamespace(message=message), None)

    incoming = adapter._queue.get_nowait()
    assert incoming.text.startswith("[User uploaded a document: game-plan.md]")
    assert "Please read this" in incoming.text
    assert incoming.platform_message_id == "42"
    assert incoming.attachments == [
        Attachment(
            filename="game-plan.md",
            mimetype="text/markdown",
            url=str(
                tmp_path
                / "thehomie_telegram_documents"
                / "unique-123_game-plan.md"
            ),
            size_bytes=29,
        )
    ]
    assert Path(incoming.attachments[0].url or "").read_text() == "# Game Plan\n\nShip the adapter."


@pytest.mark.asyncio
async def test_on_document_media_group_queues_single_combined_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telegram_adapter.tempfile, "gettempdir", lambda: str(tmp_path))
    bot = FakeTelegramBot()
    bot.files["file-a"] = FakeTelegramFile(b"# One")
    bot.files["file-b"] = FakeTelegramFile(b"# Two")
    adapter = _adapter_with_fake_bot(bot)

    def message(file_id: str, unique_id: str, filename: str, message_id: int):
        return SimpleNamespace(
            document=SimpleNamespace(
                file_id=file_id,
                file_unique_id=unique_id,
                file_name=filename,
                mime_type="text/markdown",
                file_size=5,
            ),
            from_user=SimpleNamespace(id=123456, first_name="Operator"),
            chat_id=123456,
            chat=SimpleNamespace(type="private"),
            reply_to_message=None,
            caption="Read these together" if message_id == 42 else "",
            message_id=message_id,
            media_group_id="album-1",
            to_dict=lambda: {
                "message_id": message_id,
                "media_group_id": "album-1",
                "document": {"file_name": filename},
            },
        )

    await adapter._on_document(SimpleNamespace(message=message("file-a", "unique-a", "one.md", 42)), None)
    await adapter._on_document(SimpleNamespace(message=message("file-b", "unique-b", "two.md", 43)), None)
    await telegram_adapter.asyncio.sleep(0.05)

    incoming = adapter._queue.get_nowait()
    assert adapter._queue.empty()
    assert "2 documents in one Telegram attachment group" in incoming.text
    assert "one.md" in incoming.text
    assert "two.md" in incoming.text
    assert "Read these together" in incoming.text
    assert [attachment.filename for attachment in incoming.attachments] == [
        "one.md",
        "two.md",
    ]
    assert incoming.platform_message_id == "42,43"


@pytest.mark.asyncio
async def test_update_falls_back_to_plain_send_and_returns_message_id() -> None:
    bot = FakeTelegramBot()
    bot.fail_edit = True
    bot.fail_markdown_send = True
    adapter = _adapter_with_fake_bot(bot)

    delivered_id = await adapter.update(
        OutgoingMessage(
            text="Final answer with *bad markdown",
            channel=_channel(),
            is_update=True,
            update_message_id="55",
        )
    )

    assert delivered_id == "101"
    assert [name for name, _ in bot.calls] == [
        "edit_message_text",
        "send_message",
        "send_message",
    ]
    assert bot.calls[1][1]["parse_mode"] == "Markdown"
    assert "parse_mode" not in bot.calls[2][1]


@pytest.mark.asyncio
async def test_update_raises_when_markdown_and_plain_delivery_fail() -> None:
    bot = FakeTelegramBot()
    bot.fail_edit = True
    bot.fail_markdown_send = True
    bot.fail_plain_send = True
    adapter = _adapter_with_fake_bot(bot)

    with pytest.raises(TelegramDeliveryError, match="failed to deliver"):
        await adapter.update(
            OutgoingMessage(
                text="Final answer with *bad markdown",
                channel=_channel(),
                is_update=True,
                update_message_id="55",
            )
        )

    assert [name for name, _ in bot.calls] == [
        "edit_message_text",
        "send_message",
        "send_message",
    ]
