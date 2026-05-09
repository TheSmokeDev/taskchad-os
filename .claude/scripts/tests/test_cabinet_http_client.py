"""Test PRD-8 Phase 5b / WS3.1 — cabinet_api.py HTTP client wrappers.

Asserts the helpers in `.claude/scripts/integrations/cabinet_api.py` POST/GET
to the EXACT URLs Phase 5a ships (verified `dashboard_api.py:2022-2540`),
map errors to the friendly hierarchy, and respect Rule 1 (None sentinels)
+ Rule 2 (no module-level cached client).

Pattern: `httpx.MockTransport` injected via the `client=` kwarg every helper
accepts. No `pytest-httpx`/`respx` dependency required — `httpx` is already
a project dep.

NOTE: name is `test_cabinet_http_client.py` (NOT `test_cabinet_api.py`).
Phase 5a already owns `tests/test_cabinet_api.py` for REST endpoint tests.
"""

from __future__ import annotations

import ast
import re
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from integrations import cabinet_api


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


CABINET_API_PATH = Path(cabinet_api.__file__)


def _build_client(handler: Any) -> httpx.AsyncClient:
    """Build an AsyncClient backed by MockTransport with the given handler.

    The handler signature is `(request: httpx.Request) -> httpx.Response`.
    """
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _capture_handler(captured: list[httpx.Request], response: httpx.Response):
    """Build a handler that captures requests AND returns a fixed response."""

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return response

    return handler


# ---------------------------------------------------------------------------
# F1 — module exists + httpx async client pattern
# ---------------------------------------------------------------------------


def test_module_exists() -> None:
    """The module is importable from `integrations`."""
    assert CABINET_API_PATH.exists()
    assert hasattr(cabinet_api, "create_meeting")
    assert hasattr(cabinet_api, "list_meetings")
    assert hasattr(cabinet_api, "get_transcripts")
    assert hasattr(cabinet_api, "send_message")
    assert hasattr(cabinet_api, "end_meeting")


def test_httpx_client_pattern() -> None:
    """All helpers accept `client: httpx.AsyncClient | None = None` keyword."""
    import inspect

    for name in ("create_meeting", "list_meetings", "get_transcripts", "send_message", "end_meeting"):
        helper = getattr(cabinet_api, name)
        sig = inspect.signature(helper)
        assert "client" in sig.parameters, f"{name} missing `client` param"
        assert sig.parameters["client"].default is None, f"{name} client default not None"


# ---------------------------------------------------------------------------
# F2 — endpoint URLs match Phase 5a exactly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_urls_create_meeting() -> None:
    captured: list[httpx.Request] = []
    response = httpx.Response(
        200,
        json={"ok": True, "meetingId": 1, "autoEnded": []},
    )
    async with _build_client(_capture_handler(captured, response)) as client:
        await cabinet_api.create_meeting(chat_id="telegram:42", client=client)
    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/api/cabinet/new"


@pytest.mark.asyncio
async def test_endpoint_urls_list_meetings() -> None:
    captured: list[httpx.Request] = []
    response = httpx.Response(200, json={"ok": True, "meetings": []})
    async with _build_client(_capture_handler(captured, response)) as client:
        await cabinet_api.list_meetings(limit=20, chat_id="telegram:42", client=client)
    assert len(captured) == 1
    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/api/cabinet/list"


@pytest.mark.asyncio
async def test_endpoint_urls_send_message() -> None:
    captured: list[httpx.Request] = []
    response = httpx.Response(200, json={"ok": True, "queued": True})
    async with _build_client(_capture_handler(captured, response)) as client:
        await cabinet_api.send_message(1, "hi", chat_id="telegram:42", client=client)
    assert captured[0].method == "POST"
    assert captured[0].url.path == "/api/cabinet/send"


@pytest.mark.asyncio
async def test_endpoint_urls_get_transcripts() -> None:
    captured: list[httpx.Request] = []
    response = httpx.Response(
        200,
        json={"ok": True, "transcript": [], "pinnedAgent": None, "latestSeq": 0, "agents": []},
    )
    async with _build_client(_capture_handler(captured, response)) as client:
        await cabinet_api.get_transcripts(1, limit=200, chat_id="telegram:42", client=client)
    assert captured[0].method == "GET"
    assert captured[0].url.path == "/api/cabinet/transcripts"


@pytest.mark.asyncio
async def test_endpoint_urls_end_meeting() -> None:
    captured: list[httpx.Request] = []
    response = httpx.Response(200, json={"ok": True, "meetingId": 1})
    async with _build_client(_capture_handler(captured, response)) as client:
        await cabinet_api.end_meeting(1, chat_id="telegram:42", client=client)
    assert captured[0].method == "POST"
    assert captured[0].url.path == "/api/cabinet/end"


# ---------------------------------------------------------------------------
# F3 — body shapes (R1 B1+B3 regression: chatId only, no mode/personas)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_body_shapes_create_meeting_no_mode_or_personas() -> None:
    """Outbound body has ONLY `chatId` — NO mode/personas/pinned (R1 B2)."""
    import json as _json

    captured: list[httpx.Request] = []
    response = httpx.Response(200, json={"ok": True, "meetingId": 1, "autoEnded": []})
    async with _build_client(_capture_handler(captured, response)) as client:
        await cabinet_api.create_meeting(chat_id="telegram:42", client=client)
    body = _json.loads(captured[0].content.decode())
    assert body == {"chatId": "telegram:42"}, f"unexpected body: {body}"
    # Defensive: no leakage of forbidden keys
    for forbidden in ("mode", "personas", "pinned", "pinnedAgent", "roster"):
        assert forbidden not in body


@pytest.mark.asyncio
async def test_body_shapes_create_meeting_no_chat_id() -> None:
    """When chat_id is None, body is empty {}."""
    import json as _json

    captured: list[httpx.Request] = []
    response = httpx.Response(200, json={"ok": True, "meetingId": 1, "autoEnded": []})
    async with _build_client(_capture_handler(captured, response)) as client:
        await cabinet_api.create_meeting(client=client)
    body = _json.loads(captured[0].content.decode())
    assert body == {}


@pytest.mark.asyncio
async def test_body_shapes_send_message_includes_required_fields() -> None:
    """`/send` body must carry meetingId, text, clientMsgId at minimum."""
    import json as _json

    captured: list[httpx.Request] = []
    response = httpx.Response(200, json={"ok": True, "queued": True})
    async with _build_client(_capture_handler(captured, response)) as client:
        await cabinet_api.send_message(7, "hello", chat_id="telegram:42", client=client)
    body = _json.loads(captured[0].content.decode())
    assert body["meetingId"] == 7
    assert body["text"] == "hello"
    assert body["chatId"] == "telegram:42"
    # clientMsgId is required wire-shape — auto-generated when caller passes None
    assert "clientMsgId" in body
    assert isinstance(body["clientMsgId"], str)
    assert len(body["clientMsgId"]) == 32  # uuid.uuid4().hex


@pytest.mark.asyncio
async def test_body_shapes_end_meeting() -> None:
    import json as _json

    captured: list[httpx.Request] = []
    response = httpx.Response(200, json={"ok": True, "meetingId": 9})
    async with _build_client(_capture_handler(captured, response)) as client:
        await cabinet_api.end_meeting(9, chat_id="telegram:42", client=client)
    body = _json.loads(captured[0].content.decode())
    assert body == {"meetingId": 9, "chatId": "telegram:42"}


# ---------------------------------------------------------------------------
# F4 — query strings for GET endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_meetings_query_string() -> None:
    captured: list[httpx.Request] = []
    response = httpx.Response(200, json={"ok": True, "meetings": []})
    async with _build_client(_capture_handler(captured, response)) as client:
        await cabinet_api.list_meetings(limit=15, chat_id="telegram:42", client=client)
    qp = dict(captured[0].url.params)
    assert qp.get("limit") == "15"
    assert qp.get("chatId") == "telegram:42"


@pytest.mark.asyncio
async def test_get_transcripts_query_string() -> None:
    captured: list[httpx.Request] = []
    response = httpx.Response(
        200,
        json={"ok": True, "transcript": [], "pinnedAgent": None, "latestSeq": 0, "agents": []},
    )
    async with _build_client(_capture_handler(captured, response)) as client:
        await cabinet_api.get_transcripts(
            5, limit=200, chat_id="telegram:42",
            before_ts=1234567890, before_id=99,
            client=client,
        )
    qp = dict(captured[0].url.params)
    assert qp.get("meetingId") == "5"
    assert qp.get("limit") == "200"
    assert qp.get("chatId") == "telegram:42"
    assert qp.get("beforeTs") == "1234567890"
    assert qp.get("beforeId") == "99"


# ---------------------------------------------------------------------------
# F5 — response mapping (camelCase → CabinetMeetingRef + raw dict)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_meeting_maps_response_to_dataclass() -> None:
    response = httpx.Response(
        200,
        json={"ok": True, "meetingId": 42, "autoEnded": [11, 12]},
    )
    async with _build_client(_capture_handler([], response)) as client:
        ref = await cabinet_api.create_meeting(chat_id="telegram:42", client=client)
    assert isinstance(ref, cabinet_api.CabinetMeetingRef)
    assert ref.id == 42
    assert ref.chat_id == "telegram:42"
    assert ref.auto_ended_ids == [11, 12]


@pytest.mark.asyncio
async def test_end_meeting_already_ended_camel_case() -> None:
    """Already-ended response uses camelCase `alreadyEnded` (NOT snake_case)."""
    response = httpx.Response(
        200,
        json={"ok": True, "meetingId": 9, "alreadyEnded": True},
    )
    async with _build_client(_capture_handler([], response)) as client:
        result = await cabinet_api.end_meeting(9, chat_id="t:1", client=client)
    assert result.get("alreadyEnded") is True


# ---------------------------------------------------------------------------
# F6 — clientMsgId auto-generation (R1 M3) and idempotent override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_auto_generates_client_msg_id() -> None:
    """When client_msg_id is None, helper generates uuid.uuid4().hex (32 chars)."""
    import json as _json

    captured: list[httpx.Request] = []
    response = httpx.Response(200, json={"ok": True, "queued": True})
    async with _build_client(_capture_handler(captured, response)) as client:
        await cabinet_api.send_message(1, "msg", chat_id="t:1", client=client)
    body = _json.loads(captured[0].content.decode())
    cm = body["clientMsgId"]
    assert isinstance(cm, str)
    # uuid.uuid4().hex is 32 lowercase hex chars
    assert re.fullmatch(r"[0-9a-f]{32}", cm), f"unexpected clientMsgId shape: {cm}"


@pytest.mark.asyncio
async def test_send_message_uses_provided_client_msg_id() -> None:
    """Caller-provided clientMsgId is passed through (idempotency control)."""
    import json as _json

    captured: list[httpx.Request] = []
    response = httpx.Response(200, json={"ok": True, "queued": True})
    custom = "tg:42:msg:777"
    async with _build_client(_capture_handler(captured, response)) as client:
        await cabinet_api.send_message(
            1, "msg", client_msg_id=custom, chat_id="t:1", client=client,
        )
    body = _json.loads(captured[0].content.decode())
    assert body["clientMsgId"] == custom


# ---------------------------------------------------------------------------
# F7 — friendly error mapping (R1 B6 + R3-MN1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connection_refused_friendly_error() -> None:
    """httpx.ConnectError → CabinetAPIUnreachable with friendly_message."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    async with _build_client(handler) as client:
        with pytest.raises(cabinet_api.CabinetAPIUnreachable) as ei:
            await cabinet_api.create_meeting(chat_id="t:1", client=client)
        assert "not running" in ei.value.friendly_message.lower()


@pytest.mark.asyncio
async def test_auth_failure_friendly_error() -> None:
    """HTTP 401 → CabinetAuthFailure."""
    response = httpx.Response(401, json={"error": "unauthorized"})
    async with _build_client(_capture_handler([], response)) as client:
        with pytest.raises(cabinet_api.CabinetAuthFailure) as ei:
            await cabinet_api.create_meeting(chat_id="t:1", client=client)
        assert "ORCHESTRATION_API_TOKEN" in ei.value.friendly_message


@pytest.mark.asyncio
async def test_killswitch_503_friendly_error() -> None:
    """HTTP 503 on synchronous endpoint → CabinetKillSwitchDisabled."""
    response = httpx.Response(503, json={"error": "kill_switch_disabled"})
    async with _build_client(_capture_handler([], response)) as client:
        with pytest.raises(cabinet_api.CabinetKillSwitchDisabled) as ei:
            await cabinet_api.create_meeting(chat_id="t:1", client=client)
        assert "disabled" in ei.value.friendly_message.lower()


@pytest.mark.asyncio
async def test_404_meeting_not_found() -> None:
    response = httpx.Response(404, json={"error": "meeting_not_found"})
    async with _build_client(_capture_handler([], response)) as client:
        with pytest.raises(cabinet_api.CabinetMeetingNotFound):
            await cabinet_api.end_meeting(999, chat_id="t:1", client=client)


@pytest.mark.asyncio
async def test_410_meeting_ended() -> None:
    response = httpx.Response(410, json={"error": "meeting_ended"})
    async with _build_client(_capture_handler([], response)) as client:
        with pytest.raises(cabinet_api.CabinetMeetingEnded):
            await cabinet_api.send_message(1, "x", chat_id="t:1", client=client)


@pytest.mark.asyncio
async def test_400_bad_request_invalid_client_msg_id() -> None:
    response = httpx.Response(400, json={"error": "invalid clientMsgId"})
    async with _build_client(_capture_handler([], response)) as client:
        with pytest.raises(cabinet_api.CabinetBadRequest):
            await cabinet_api.send_message(1, "x", chat_id="t:1", client=client)


@pytest.mark.asyncio
async def test_chat_mismatch_403_raises_cabinet_chat_scope_mismatch() -> None:
    """R3-MN1: Phase 5a returns 403 chat_mismatch from dashboard_api.py:2360
    et al. Helper must map to dedicated CabinetChatScopeMismatch BEFORE the
    catch-all >= 400 branch (which would bucket as CabinetBadRequest).
    Friendly message must steer the operator toward `/cabinet list`.
    """
    response = httpx.Response(403, json={"detail": "chat_mismatch"})
    async with _build_client(_capture_handler([], response)) as client:
        with pytest.raises(cabinet_api.CabinetChatScopeMismatch) as ei:
            await cabinet_api.send_message(1, "hi", chat_id="t:wrong", client=client)
        msg = ei.value.friendly_message.lower()
        assert "different chat" in msg
        assert "/cabinet list" in msg


@pytest.mark.asyncio
async def test_send_message_returns_queued_response() -> None:
    """R1 B6 / R2 NB1+NM4: /send is fire-and-forget. 200 {ok, queued} →
    helper returns dict; does NOT raise CabinetKillSwitchDisabled."""
    response = httpx.Response(200, json={"ok": True, "queued": True})
    async with _build_client(_capture_handler([], response)) as client:
        result = await cabinet_api.send_message(1, "hi", chat_id="t:1", client=client)
    assert result == {"ok": True, "queued": True}


# ---------------------------------------------------------------------------
# F8 — bearer-token semantics (loopback default + server-set mode)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bearer_token_set_when_env_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ORCHESTRATION_API_TOKEN env is set, Authorization: Bearer header is sent."""
    monkeypatch.setenv("ORCHESTRATION_API_TOKEN", "test-token-abc")

    captured: list[httpx.Request] = []
    response = httpx.Response(200, json={"ok": True, "meetingId": 1, "autoEnded": []})
    async with _build_client(_capture_handler(captured, response)) as client:
        await cabinet_api.create_meeting(chat_id="t:1", client=client)
    auth = captured[0].headers.get("Authorization")
    assert auth == "Bearer test-token-abc"


@pytest.mark.asyncio
async def test_no_auth_header_when_token_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2 NM4: when env is unset, NO Authorization header (loopback default)."""
    monkeypatch.delenv("ORCHESTRATION_API_TOKEN", raising=False)

    captured: list[httpx.Request] = []
    response = httpx.Response(200, json={"ok": True, "meetingId": 1, "autoEnded": []})
    async with _build_client(_capture_handler(captured, response)) as client:
        await cabinet_api.create_meeting(chat_id="t:1", client=client)
    # The header dict for httpx.Request stores headers case-insensitively
    assert "authorization" not in (h.lower() for h in captured[0].headers.keys())


# ---------------------------------------------------------------------------
# F9 — base URL fallback (default + env override)
# ---------------------------------------------------------------------------


def test_default_base_url_constant() -> None:
    """Default base URL is http://127.0.0.1:4322 (loopback default)."""
    assert cabinet_api.DEFAULT_BASE_URL == "http://127.0.0.1:4322"


def test_base_url_helper_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ORCHESTRATION_API_BASE_URL", raising=False)
    assert cabinet_api._base_url() == "http://127.0.0.1:4322"


def test_base_url_helper_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHESTRATION_API_BASE_URL", "http://10.0.0.5:9999")
    assert cabinet_api._base_url() == "http://10.0.0.5:9999"


# ---------------------------------------------------------------------------
# F10 — Rule 1 + Rule 2 AST scans
# ---------------------------------------------------------------------------


def _parse_module() -> ast.Module:
    return ast.parse(CABINET_API_PATH.read_text(encoding="utf-8"))


def test_none_sentinel_resolves_in_body() -> None:
    """Rule 1: every public helper takes `client: AsyncClient | None = None`.

    Default value MUST be a literal None (resolved at call time inside body),
    NOT a module-level constant or expression that evaluates at def time.
    """
    module = _parse_module()
    public_helpers = {"create_meeting", "list_meetings", "get_transcripts", "send_message", "end_meeting"}
    seen: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.AsyncFunctionDef) and node.name in public_helpers:
            seen.add(node.name)
            args = node.args
            client_default = None
            # `client` is keyword-only after the `*` separator in our helpers.
            for kwarg, default in zip(args.kwonlyargs, args.kw_defaults, strict=False):
                if kwarg.arg == "client":
                    client_default = default
                    break
            assert client_default is not None or _is_none_constant(client_default), (
                f"{node.name}: `client` keyword-only default missing or non-None"
            )
            # The default must be the literal `None` AST node (Constant with value None).
            assert _is_none_constant(client_default), (
                f"{node.name}: `client=` default is not literal None: {ast.dump(client_default)}"
            )
    missing = public_helpers - seen
    assert not missing, f"helpers not found: {missing}"


def _is_none_constant(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def test_no_module_level_client() -> None:
    """Rule 2: no module-level cached httpx.AsyncClient or env cache.

    Walk top-level assignments — none may be an `httpx.AsyncClient(...)`
    construction, nor an `os.getenv(...)` direct assignment (which would
    cache env at import time). _safe_helper functions and constants
    (DEFAULT_*, exception class definitions) are allowed.
    """
    module = _parse_module()
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        # The right-hand side cannot be a Call to httpx.AsyncClient
        rhs = node.value
        if isinstance(rhs, ast.Call):
            func = rhs.func
            # Block `httpx.AsyncClient(...)` and `AsyncClient(...)` at module scope
            if isinstance(func, ast.Attribute) and func.attr == "AsyncClient":
                pytest.fail(
                    f"Rule 2 violation: module-level httpx.AsyncClient assignment at line {node.lineno}"
                )
            if isinstance(func, ast.Name) and func.id == "AsyncClient":
                pytest.fail(
                    f"Rule 2 violation: module-level AsyncClient() at line {node.lineno}"
                )
            # Block `os.getenv(...)` at module scope (env cache antipattern)
            if isinstance(func, ast.Attribute) and func.attr == "getenv":
                if isinstance(func.value, ast.Name) and func.value.id == "os":
                    pytest.fail(
                        f"Rule 2 violation: module-level os.getenv at line {node.lineno}"
                    )


def test_no_module_level_env_cache_constants() -> None:
    """Defensive: top-level constants are uppercase + literal scalar, not env reads."""
    module = _parse_module()
    for node in module.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if isinstance(tgt, ast.Name) and tgt.id.isupper():
                # Allowed: literal Constant (DEFAULT_BASE_URL, DEFAULT_TIMEOUT_S)
                if isinstance(node.value, ast.Constant):
                    continue
                if isinstance(node.value, ast.List | ast.Tuple):
                    continue
                pytest.fail(
                    f"Suspicious module-level uppercase assignment at line {node.lineno}"
                    f": {ast.dump(node.value)}"
                )


# ---------------------------------------------------------------------------
# F11 — _safe_json (R2 NM1) maps empty/non-JSON 2xx → {}
# ---------------------------------------------------------------------------


def test_safe_json_handles_empty_response() -> None:
    """200 with empty body → _safe_json returns {} not crash."""
    r = httpx.Response(200, content=b"")
    assert cabinet_api._safe_json(r) == {}


def test_safe_json_handles_non_json_body() -> None:
    r = httpx.Response(200, content=b"not json at all <html>")
    assert cabinet_api._safe_json(r) == {}


# ---------------------------------------------------------------------------
# F12 — error precedence (403 BEFORE generic 4xx — R3-MN1 regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_403_does_not_fall_through_to_bad_request() -> None:
    """R3-MN1 regression: 403 must surface as CabinetChatScopeMismatch,
    NOT CabinetBadRequest from the catch-all >= 400 branch."""
    response = httpx.Response(403, json={"detail": "chat_mismatch"})
    async with _build_client(_capture_handler([], response)) as client:
        with pytest.raises(cabinet_api.CabinetChatScopeMismatch):
            await cabinet_api.end_meeting(7, chat_id="t:wrong", client=client)


# ---------------------------------------------------------------------------
# F13 — friendly error class hierarchy
# ---------------------------------------------------------------------------


def test_friendly_error_classes_exist_with_messages() -> None:
    """All 8 declared classes exist with non-empty friendly_message."""
    classes = [
        cabinet_api.CabinetAPIError,
        cabinet_api.CabinetAPIUnreachable,
        cabinet_api.CabinetAuthFailure,
        cabinet_api.CabinetKillSwitchDisabled,
        cabinet_api.CabinetMeetingNotFound,
        cabinet_api.CabinetMeetingEnded,
        cabinet_api.CabinetBadRequest,
        cabinet_api.CabinetChatScopeMismatch,
    ]
    for cls in classes:
        assert isinstance(cls.friendly_message, str)
        assert len(cls.friendly_message) > 0


def test_subclasses_inherit_from_base() -> None:
    for cls in (
        cabinet_api.CabinetAPIUnreachable,
        cabinet_api.CabinetAuthFailure,
        cabinet_api.CabinetKillSwitchDisabled,
        cabinet_api.CabinetMeetingNotFound,
        cabinet_api.CabinetMeetingEnded,
        cabinet_api.CabinetBadRequest,
        cabinet_api.CabinetChatScopeMismatch,
    ):
        assert issubclass(cls, cabinet_api.CabinetAPIError)


# ---------------------------------------------------------------------------
# F14 — uuid auto-gen monkeypatch (deterministic test for R1 M3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_auto_generated_id_uses_uuid_uuid4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the auto-gen path goes through `uuid.uuid4().hex` so consumers
    relying on the 32-char hex shape don't break under future refactor."""
    import json as _json

    sentinel = uuid.UUID("12345678123456781234567812345678")
    monkeypatch.setattr(cabinet_api.uuid, "uuid4", lambda: sentinel)

    captured: list[httpx.Request] = []
    response = httpx.Response(200, json={"ok": True, "queued": True})
    async with _build_client(_capture_handler(captured, response)) as client:
        await cabinet_api.send_message(1, "x", chat_id="t:1", client=client)
    body = _json.loads(captured[0].content.decode())
    assert body["clientMsgId"] == sentinel.hex
