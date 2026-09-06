from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest
from models import Platform
from router import ChatRouter

PACKAGE = "insights_20260903_abcdef123456"
DIGEST = "123456abcdef"


class _Adapter:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, message) -> None:
        self.sent.append(message)


def _incoming(
    button: bool,
    *,
    own: bool = True,
    platform=Platform.TELEGRAM,
):
    return SimpleNamespace(
        raw_event=(
            {"interaction_type": "button", "source_message_is_own": own}
            if button
            else {}
        ),
        channel=None,
        thread=None,
        platform=platform,
    )


def _shim():
    obj = SimpleNamespace()
    obj._handle_insights_button = ChatRouter._handle_insights_button.__get__(obj)
    return obj


def test_insights_button_is_immediate():
    incoming = SimpleNamespace(
        text=f"__button:insights:preview:{PACKAGE}:1:{DIGEST}"
    )
    assert ChatRouter._is_immediate_button(incoming) is True


@pytest.mark.asyncio
async def test_synthesized_insights_button_is_refused(monkeypatch):
    called = False
    module = types.ModuleType("social.tenant_insights")

    def approve(*args, **kwargs):
        nonlocal called
        called = True

    module.approve_for_preview = approve
    module.deliver_insights_preview = lambda *a, **k: True
    module.load_package = lambda *a, **k: ({}, None)
    module.publish_approved_preview = approve
    monkeypatch.setitem(sys.modules, "social.tenant_insights", module)

    adapter = _Adapter()
    await _shim()._handle_insights_button(
        adapter,
        _incoming(False),
        f"insights:preview:{PACKAGE}:1:{DIGEST}",
    )
    assert called is False
    assert "only run from Telegram buttons" in adapter.sent[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "incoming",
    [
        _incoming(True, own=False),
        _incoming(True, platform=Platform.DISCORD),
    ],
)
async def test_insights_button_requires_owned_telegram_provenance(
    monkeypatch, incoming
):
    module = types.ModuleType("social.tenant_insights")
    called = False

    def approve(*args, **kwargs):
        nonlocal called
        called = True

    module.approve_for_preview = approve
    module.deliver_insights_preview = lambda *a, **k: True
    module.load_package = lambda *a, **k: ({}, None)
    module.publish_approved_preview = approve
    monkeypatch.setitem(sys.modules, "social.tenant_insights", module)
    adapter = _Adapter()
    await _shim()._handle_insights_button(
        adapter,
        incoming,
        f"insights:preview:{PACKAGE}:1:{DIGEST}",
    )
    assert called is False
    assert "only run from Telegram buttons" in adapter.sent[0].text


@pytest.mark.asyncio
async def test_preview_button_calls_only_preview_and_delivers_proof(monkeypatch):
    calls = []
    module = types.ModuleType("social.tenant_insights")

    def approve(package_id, **kwargs):
        calls.append(("preview", package_id, kwargs))
        return SimpleNamespace(status="awaiting_publish_approval", reasons=())

    def deliver(package_id):
        calls.append(("deliver", package_id))
        return True

    module.approve_for_preview = approve
    module.deliver_insights_preview = deliver
    module.load_package = lambda *a, **k: ({}, None)
    module.publish_approved_preview = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("publish must not run on approval one")
    )
    monkeypatch.setitem(sys.modules, "social.tenant_insights", module)

    adapter = _Adapter()
    await _shim()._handle_insights_button(
        adapter,
        _incoming(True),
        f"insights:preview:{PACKAGE}:1:{DIGEST}",
    )
    assert calls[0] == (
        "preview",
        PACKAGE,
        {"revision": 1, "digest": DIGEST},
    )
    assert calls[1] == ("deliver", PACKAGE)
    assert "exact publish approval" in adapter.sent[-1].text


@pytest.mark.asyncio
async def test_publish_button_calls_exact_publish_path(monkeypatch):
    calls = []
    module = types.ModuleType("social.tenant_insights")
    module.approve_for_preview = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("preview must not rerun")
    )
    module.deliver_insights_preview = lambda *a, **k: True

    def publish(package_id, **kwargs):
        calls.append((package_id, kwargs))
        return SimpleNamespace(status="published", reasons=())

    module.publish_approved_preview = publish
    module.load_package = lambda package_id: (
        {
            "publish_receipt": {
                "liveUrl": "https://YourProduct.com/blog/proof",
                "commit": "a" * 40,
            }
        },
        None,
    )
    monkeypatch.setitem(sys.modules, "social.tenant_insights", module)

    adapter = _Adapter()
    await _shim()._handle_insights_button(
        adapter,
        _incoming(True),
        f"insights:publish:{PACKAGE}:1:{DIGEST}",
    )
    assert calls == [(PACKAGE, {"revision": 1, "digest": DIGEST})]
    assert "https://YourProduct.com/blog/proof" in adapter.sent[-1].text
