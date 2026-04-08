"""Tests for adapters.whatsapp — webhook parsing, verification, normalization."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add chat dir to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "chat"))

from adapters.whatsapp import WhatsAppAdapter
from models import Platform


def _make_adapter(
    allowed_numbers: list[str] | None = None,
) -> WhatsAppAdapter:
    """Create a WhatsAppAdapter for testing."""
    return WhatsAppAdapter(
        access_token="fake-token",
        phone_number_id="123456789",
        verify_token="my-verify-token",
        webhook_port=8443,
        allowed_numbers=allowed_numbers,
    )


def _make_webhook_payload(
    phone: str = "15551234567",
    text: str = "Hello bot",
    msg_type: str = "text",
    msg_id: str = "wamid.xxx",
    contact_name: str = "Test User",
) -> dict:
    """Create a sample WhatsApp webhook payload."""
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": phone,
                                    "type": msg_type,
                                    "text": {"body": text},
                                    "id": msg_id,
                                }
                            ],
                            "contacts": [
                                {
                                    "wa_id": phone,
                                    "profile": {"name": contact_name},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


# ── Platform property ──────────────────────────────────────


def test_whatsapp_platform():
    adapter = _make_adapter()
    assert adapter.platform == Platform.WHATSAPP


# ── Webhook verification ──────────────────────────────────


@pytest.mark.asyncio
async def test_webhook_verify_success():
    adapter = _make_adapter()
    request = MagicMock()
    request.query = {
        "hub.mode": "subscribe",
        "hub.verify_token": "my-verify-token",
        "hub.challenge": "challenge123",
    }
    response = await adapter._handle_verify(request)
    assert response.text == "challenge123"


@pytest.mark.asyncio
async def test_webhook_verify_wrong_token():
    adapter = _make_adapter()
    request = MagicMock()
    request.query = {
        "hub.mode": "subscribe",
        "hub.verify_token": "wrong-token",
        "hub.challenge": "challenge123",
    }
    response = await adapter._handle_verify(request)
    assert response.status == 403


@pytest.mark.asyncio
async def test_webhook_verify_wrong_mode():
    adapter = _make_adapter()
    request = MagicMock()
    request.query = {
        "hub.mode": "unsubscribe",
        "hub.verify_token": "my-verify-token",
        "hub.challenge": "challenge123",
    }
    response = await adapter._handle_verify(request)
    assert response.status == 403


# ── Webhook message parsing ──────────────────────────────


@pytest.mark.asyncio
async def test_webhook_parse_text_message():
    adapter = _make_adapter()
    payload = _make_webhook_payload(
        phone="15551234567", text="Hello bot", contact_name="Test User"
    )
    request = MagicMock()
    request.json = AsyncMock(return_value=payload)

    await adapter._handle_webhook(request)

    assert not adapter._queue.empty()
    msg = adapter._queue.get_nowait()
    assert msg.text == "Hello bot"
    assert msg.user.platform_id == "15551234567"
    assert msg.user.display_name == "Test User"
    assert msg.platform == Platform.WHATSAPP
    assert msg.channel.is_dm is True
    assert msg.thread.thread_id == "15551234567"
    assert msg.platform_message_id == "wamid.xxx"


@pytest.mark.asyncio
async def test_webhook_skip_non_text():
    adapter = _make_adapter()
    payload = _make_webhook_payload(msg_type="image")
    request = MagicMock()
    request.json = AsyncMock(return_value=payload)

    await adapter._handle_webhook(request)
    assert adapter._queue.empty()


@pytest.mark.asyncio
async def test_webhook_multiple_messages():
    adapter = _make_adapter()
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "15551111111",
                                    "type": "text",
                                    "text": {"body": "First"},
                                    "id": "msg1",
                                },
                                {
                                    "from": "15552222222",
                                    "type": "text",
                                    "text": {"body": "Second"},
                                    "id": "msg2",
                                },
                            ],
                            "contacts": [],
                        }
                    }
                ]
            }
        ]
    }
    request = MagicMock()
    request.json = AsyncMock(return_value=payload)

    await adapter._handle_webhook(request)
    assert adapter._queue.qsize() == 2


@pytest.mark.asyncio
async def test_webhook_missing_contacts():
    adapter = _make_adapter()
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "15559999999",
                                    "type": "text",
                                    "text": {"body": "No contact"},
                                    "id": "msg1",
                                }
                            ],
                            "contacts": [],
                        }
                    }
                ]
            }
        ]
    }
    request = MagicMock()
    request.json = AsyncMock(return_value=payload)

    await adapter._handle_webhook(request)
    msg = adapter._queue.get_nowait()
    # Should use phone number as display name when no contacts
    assert msg.user.display_name == "15559999999"


# ── Allowed numbers filter ────────────────────────────────


@pytest.mark.asyncio
async def test_allowed_numbers_passes():
    adapter = _make_adapter(allowed_numbers=["15551234567"])
    payload = _make_webhook_payload(phone="15551234567")
    request = MagicMock()
    request.json = AsyncMock(return_value=payload)

    await adapter._handle_webhook(request)
    assert not adapter._queue.empty()


@pytest.mark.asyncio
async def test_allowed_numbers_blocks():
    adapter = _make_adapter(allowed_numbers=["15551234567"])
    payload = _make_webhook_payload(phone="15559999999")
    request = MagicMock()
    request.json = AsyncMock(return_value=payload)

    await adapter._handle_webhook(request)
    assert adapter._queue.empty()


@pytest.mark.asyncio
async def test_allowed_numbers_empty_allows_all():
    adapter = _make_adapter(allowed_numbers=[])
    payload = _make_webhook_payload(phone="15559999999")
    request = MagicMock()
    request.json = AsyncMock(return_value=payload)

    await adapter._handle_webhook(request)
    assert not adapter._queue.empty()
