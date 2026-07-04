"""Test Phase 2 / WS-chat — scheduled_api.py cross-process create client.

Asserts ``integrations/scheduled_api.py`` POSTs to ``/api/scheduled``, maps
status codes to the friendly error hierarchy (400 guard-refusal carries the
server's verbatim detail; 422 invalid cron; 401 auth), and respects Rule 1
(base_url/token read from env at CALL time) + Rule 2 (no module-level cached
client).

Pattern: ``httpx.MockTransport`` injected via the ``client=`` kwarg — no
``pytest-httpx``/``respx`` dependency (``httpx`` is already a project dep).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from integrations import scheduled_api

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_client(handler: Any) -> httpx.AsyncClient:
    """Build an AsyncClient backed by MockTransport with the given handler."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _fixed(status_code: int, json_body: Any) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=json_body)

    return handler


_SPEC = {
    "persona_id": "default",
    "prompt": "Send the morning brief.",
    "schedule": "0 8 * * *",
    "next_run": None,
}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_returns_row_on_200() -> None:
    row = {"id": 7, "persona_id": "default", "prompt": "x", "schedule": "0 8 * * *"}
    async with _build_client(_fixed(200, row)) as client:
        result = await scheduled_api.create_scheduled_task(_SPEC, client=client)
    assert result == row


@pytest.mark.asyncio
async def test_posts_to_api_scheduled_with_spec_body() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"id": 1})

    async with _build_client(handler) as client:
        await scheduled_api.create_scheduled_task(_SPEC, client=client)

    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/api/scheduled"
    import json as _json

    assert _json.loads(req.content.decode()) == _SPEC


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_400_maps_to_refused_with_verbatim_detail() -> None:
    detail = (
        "Blocked: job contains a bot lifecycle command (launch/kill/restart "
        "of run_chat.sh / chat/main.py / thehomie)."
    )
    async with _build_client(_fixed(400, {"detail": detail})) as client:
        with pytest.raises(scheduled_api.ScheduledCreateRefused) as excinfo:
            await scheduled_api.create_scheduled_task(_SPEC, client=client)
    assert excinfo.value.friendly_message == detail


@pytest.mark.asyncio
async def test_422_maps_to_invalid() -> None:
    async with _build_client(_fixed(422, {"detail": "invalid cron: 'every 30m'"})) as client:
        with pytest.raises(scheduled_api.ScheduledCreateInvalid) as excinfo:
            await scheduled_api.create_scheduled_task(_SPEC, client=client)
    # friendly_message carries the server detail when present.
    assert "invalid cron" in excinfo.value.friendly_message


@pytest.mark.asyncio
async def test_401_maps_to_auth_failure() -> None:
    async with _build_client(_fixed(401, {"detail": "unauthorized"})) as client:
        with pytest.raises(scheduled_api.ScheduledAuthFailure):
            await scheduled_api.create_scheduled_task(_SPEC, client=client)


@pytest.mark.asyncio
async def test_connect_error_maps_to_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with _build_client(handler) as client:
        with pytest.raises(scheduled_api.ScheduledAPIUnreachable):
            await scheduled_api.create_scheduled_task(_SPEC, client=client)


@pytest.mark.asyncio
async def test_refused_and_invalid_are_scheduled_api_errors() -> None:
    """The chat handler catches the base class — subclasses must inherit it."""
    assert issubclass(scheduled_api.ScheduledCreateRefused, scheduled_api.ScheduledAPIError)
    assert issubclass(scheduled_api.ScheduledCreateInvalid, scheduled_api.ScheduledAPIError)
    assert issubclass(scheduled_api.ScheduledAuthFailure, scheduled_api.ScheduledAPIError)
    assert issubclass(scheduled_api.ScheduledAPIUnreachable, scheduled_api.ScheduledAPIError)


# ---------------------------------------------------------------------------
# Rule 1 — env read at call time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_base_url_and_token_read_from_env_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ORCHESTRATION_API_BASE_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("ORCHESTRATION_API_TOKEN", "sekret")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"id": 1})

    async with _build_client(handler) as client:
        await scheduled_api.create_scheduled_task(_SPEC, client=client)

    req = captured[0]
    assert str(req.url).startswith("http://127.0.0.1:9999/api/scheduled")
    assert req.headers.get("Authorization") == "Bearer sekret"


@pytest.mark.asyncio
async def test_no_auth_header_in_loopback_no_token_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ORCHESTRATION_API_TOKEN", raising=False)
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"id": 1})

    async with _build_client(handler) as client:
        await scheduled_api.create_scheduled_task(_SPEC, client=client)

    assert "Authorization" not in captured[0].headers


@pytest.mark.asyncio
async def test_trailing_slash_base_url_does_not_double_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A configured base URL with a trailing slash must not produce
    # `//api/scheduled` on the wire.
    monkeypatch.setenv("ORCHESTRATION_API_BASE_URL", "http://127.0.0.1:4322/")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"id": 1})

    async with _build_client(handler) as client:
        await scheduled_api.create_scheduled_task(_SPEC, client=client)

    assert str(captured[0].url) == "http://127.0.0.1:4322/api/scheduled"
    assert "//api/scheduled" not in str(captured[0].url)


def test_no_module_level_client_cached() -> None:
    """Rule 2 — the module must not hold a cached client attribute."""
    for attr in vars(scheduled_api).values():
        assert not isinstance(attr, httpx.AsyncClient)
