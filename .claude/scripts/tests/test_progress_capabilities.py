"""Focused contract tests for framework-native progress capabilities."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.base import ProgressCapabilities, resolve_progress_capabilities
from adapters.cli_adapter import CLIAdapter
from adapters.discord import DiscordAdapter
from adapters.slack import SlackAdapter
from adapters.telegram import TelegramAdapter
from adapters.web import WebAdapter
from adapters.webhook import WebhookAdapter
from adapters.whatsapp import WhatsAppAdapter
from models import Channel, OutgoingMessage, Platform, Thread


ALL_DISABLED = ProgressCapabilities()


def _without_constructor(adapter_type: type) -> object:
    """Build an adapter without connecting to a platform or loading secrets."""
    return adapter_type.__new__(adapter_type)


def test_progress_capabilities_are_frozen_and_default_disabled() -> None:
    capabilities = ProgressCapabilities()

    assert capabilities == ProgressCapabilities(
        enabled=False,
        typing=False,
        editable=False,
        recover_failed_status=False,
    )
    with pytest.raises(FrozenInstanceError):
        capabilities.enabled = True  # type: ignore[misc]


@pytest.mark.parametrize(
    ("adapter_type", "expected"),
    [
        pytest.param(
            DiscordAdapter,
            ProgressCapabilities(True, True, True, True),
            id="discord",
        ),
        pytest.param(
            TelegramAdapter,
            ProgressCapabilities(True, True, True, True),
            id="telegram",
        ),
        pytest.param(
            SlackAdapter,
            ProgressCapabilities(True, False, True, True),
            id="slack",
        ),
        pytest.param(
            WebAdapter,
            ProgressCapabilities(True, False, True, True),
            id="mission-control-relay",
        ),
        pytest.param(CLIAdapter, ALL_DISABLED, id="cli"),
        pytest.param(WhatsAppAdapter, ALL_DISABLED, id="whatsapp"),
        pytest.param(WebhookAdapter, ALL_DISABLED, id="webhook"),
    ],
)
def test_framework_adapter_progress_matrix(
    adapter_type: type,
    expected: ProgressCapabilities,
) -> None:
    adapter = _without_constructor(adapter_type)

    assert resolve_progress_capabilities(adapter) == expected


def test_unknown_adapter_defaults_to_disabled() -> None:
    assert resolve_progress_capabilities(object()) == ALL_DISABLED


@pytest.mark.parametrize(
    "adapter",
    [
        pytest.param(SimpleNamespace(progress_capabilities=None), id="none"),
        pytest.param(
            SimpleNamespace(
                progress_capabilities={
                    "enabled": True,
                    "typing": True,
                    "editable": True,
                    "recover_failed_status": True,
                }
            ),
            id="mapping-is-not-contract",
        ),
        pytest.param(
            SimpleNamespace(progress_capabilities=True),
            id="boolean-is-not-contract",
        ),
    ],
)
def test_resolver_rejects_non_contract_values(adapter: object) -> None:
    assert resolve_progress_capabilities(adapter) == ALL_DISABLED


def test_resolver_fails_quiet_when_capability_property_raises() -> None:
    class BrokenAdapter:
        @property
        def progress_capabilities(self) -> ProgressCapabilities:
            raise RuntimeError("adapter capability lookup failed")

    assert resolve_progress_capabilities(BrokenAdapter()) == ALL_DISABLED


@pytest.mark.asyncio
async def test_slack_failed_edit_returns_no_delivery_receipt() -> None:
    adapter = _without_constructor(SlackAdapter)
    adapter.app = SimpleNamespace(
        client=SimpleNamespace(
            chat_update=AsyncMock(side_effect=RuntimeError("Slack edit failed"))
        )
    )

    result = await adapter.update(
        OutgoingMessage(
            text="Working",
            channel=Channel(Platform.SLACK, "channel-1"),
            is_update=True,
            update_message_id="message-1",
        )
    )

    assert result is None


@pytest.mark.asyncio
async def test_slack_long_update_defers_before_any_edit() -> None:
    adapter = _without_constructor(SlackAdapter)
    chat_update = AsyncMock()
    adapter.app = SimpleNamespace(client=SimpleNamespace(chat_update=chat_update))

    result = await adapter.update(
        OutgoingMessage(
            text="x" * 5000,
            channel=Channel(Platform.SLACK, "channel-1"),
            is_update=True,
            update_message_id="message-1",
        )
    )

    assert result is None
    chat_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_slack_marker_update_has_no_media_or_text_side_effect() -> None:
    adapter = _without_constructor(SlackAdapter)
    chat_update = AsyncMock()
    files_upload = AsyncMock()
    adapter.app = SimpleNamespace(
        client=SimpleNamespace(
            chat_update=chat_update,
            files_upload_v2=files_upload,
        )
    )

    result = await adapter.update(
        OutgoingMessage(
            text="Here it is [SEND_FILE:report.pdf]",
            channel=Channel(Platform.SLACK, "channel-1"),
            is_update=True,
            update_message_id="message-1",
        )
    )

    assert result is None
    chat_update.assert_not_awaited()
    files_upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_slack_partial_fresh_send_raises_instead_of_claiming_success() -> None:
    adapter = _without_constructor(SlackAdapter)
    chat_post = AsyncMock(
        side_effect=[{"ts": "message-1"}, RuntimeError("second chunk failed")]
    )
    adapter.app = SimpleNamespace(client=SimpleNamespace(chat_postMessage=chat_post))

    with pytest.raises(RuntimeError, match="failed to deliver 1 message chunk"):
        await adapter.send(
            OutgoingMessage(
                text="x" * 5000,
                channel=Channel(Platform.SLACK, "channel-1"),
            )
        )

    assert chat_post.await_count == 2


@pytest.mark.asyncio
async def test_web_marker_update_has_no_binary_or_text_side_effect() -> None:
    adapter = _without_constructor(WebAdapter)
    send_response = AsyncMock()
    adapter.ws_client = SimpleNamespace(send_response=send_response)

    result = await adapter.update(
        OutgoingMessage(
            text="Here it is [SEND_FILE:report.pdf]",
            channel=Channel(Platform.WEB, "dashboard"),
            thread=Thread(
                thread_id="conversation-1",
                parent_message_id="request-1",
            ),
            is_update=True,
            update_message_id="request-1",
        )
    )

    assert result is None
    send_response.assert_not_awaited()
