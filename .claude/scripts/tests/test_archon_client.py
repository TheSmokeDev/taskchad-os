"""Tests for `.claude/scripts/integrations/archon_client.py` (epic #252, ticket #253).

Every URL, body key and status mapping asserted here was read off the Archon
server source (v0.6.0) — the file:line citations live in the client's
docstrings. These tests are the contract lock: if Archon's wire shape moves,
one of them fails instead of a voice-dispatched run silently going nowhere.

Pattern: ``httpx.MockTransport`` injected through the ``client=`` kwarg every
helper accepts. No new dependency — httpx is already a project dep, and this
mirrors ``tests/test_cabinet_http_client.py``.

Two live-optional suites are skipped by default:

* ``ARCHON_LIVE_TESTS=1`` — read-only round trip against a running Archon.
* ``ARCHON_LIVE_DISPATCH=1`` + ``ARCHON_LIVE_CODEBASE_ID=<id>`` — the
  ``spike-echo`` dispatch smoke. Costs ~53s and a worktree, so it is a
  deliberate opt-in on top of the read-only flag.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest

from integrations import archon_client

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client(handler: Any) -> httpx.AsyncClient:
    """AsyncClient backed by MockTransport running ``handler(request)``."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _capturing(
    captured: list[httpx.Request],
    payload: dict[str, Any] | None = None,
    status: int = 200,
):
    """Handler that records every request and answers with a fixed JSON body."""

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status, json=payload if payload is not None else {})

    return handler


def _exploding(exc: Exception):
    """Handler that raises a transport exception instead of answering."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


RUN_WIRE: dict[str, Any] = {
    "id": "23c6c29ad89b24d6e662af355bbd4158",
    "workflow_name": "spike-echo",
    "conversation_id": "worker-db-id",
    "parent_conversation_id": "parent-db-id",
    "parent_run_id": None,
    "codebase_id": "058ef39d",
    "status": "running",
    "user_message": "/workflow run spike-echo hello",
    "metadata": {"approval": {"nodeId": "gate", "message": "ok?"}},
    "started_at": "2026-07-27T17:59:31.000Z",
    "completed_at": None,
    "last_activity_at": "2026-07-27T18:00:24.000Z",
    "working_path": "/tmp/worktrees/archon/thread-03fb5edf",
    "user_id": None,
}

EVENT_WIRE: dict[str, Any] = {
    "id": "evt-1",
    "workflow_run_id": "23c6c29ad89b24d6e662af355bbd4158",
    "event_type": "tool_called",
    "step_index": 1,
    "step_name": "echo-node",
    "data": {"tool_name": "Bash", "tool_input": {"command": "echo hi"}},
    "created_at": "2026-07-27T18:00:02.000Z",
}


# ===========================================================================
# Rule 1 — config resolved at CALL time, never bound in a default arg
# ===========================================================================


def test_base_url_defaults_to_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARCHON_API_BASE_URL", raising=False)
    assert archon_client._base_url() == "http://127.0.0.1:3090"


def test_base_url_reresolves_between_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Rule 1 proof: a mid-process env change MUST take effect.

    A `def f(base=os.getenv(...))` default would cache the first value in
    ``f.__defaults__`` and this assertion would fail on the second call.
    """
    monkeypatch.setenv("ARCHON_API_BASE_URL", "http://first:1111")
    assert archon_client._base_url() == "http://first:1111"
    monkeypatch.setenv("ARCHON_API_BASE_URL", "http://second:2222")
    assert archon_client._base_url() == "http://second:2222"


def test_base_url_strips_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCHON_API_BASE_URL", "http://box:3090///")
    assert archon_client._base_url() == "http://box:3090"


def test_base_url_rejects_non_http_value_with_receipt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ARCHON_API_BASE_URL", "127.0.0.1:3090")
    assert archon_client._base_url() == archon_client.DEFAULT_BASE_URL
    assert "ARCHON_API_BASE_URL" in capsys.readouterr().err


def test_timeout_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARCHON_API_TIMEOUT_S", raising=False)
    assert archon_client._timeout_s() == archon_client.DEFAULT_TIMEOUT_S
    monkeypatch.setenv("ARCHON_API_TIMEOUT_S", "2.5")
    assert archon_client._timeout_s() == 2.5


@pytest.mark.parametrize("bad", ["abc", "0", "-3"])
def test_timeout_rejects_bad_values_with_receipt(
    bad: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ARCHON_API_TIMEOUT_S", bad)
    assert archon_client._timeout_s() == archon_client.DEFAULT_TIMEOUT_S
    assert "ARCHON_API_TIMEOUT_S" in capsys.readouterr().err


def test_receipt_survives_non_ascii_on_a_cp1252_console(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A receipt must never become the failure it was reporting.

    Env values and localized Windows OSError strings can be non-ASCII; a
    cp1252 console raises UnicodeEncodeError on the first such byte.
    """
    monkeypatch.setenv("ARCHON_API_BASE_URL", "ftp://café-box:3090")
    assert archon_client._base_url() == archon_client.DEFAULT_BASE_URL
    err = capsys.readouterr().err
    assert err.isascii()
    assert "ARCHON_API_BASE_URL" in err


def test_receipt_swallows_a_dead_stderr() -> None:
    """A broken stderr must not propagate out of a diagnostic line."""

    class _DeadStream:
        def write(self, _: str) -> int:
            raise OSError("stream closed")

        def flush(self) -> None:
            raise OSError("stream closed")

    original = archon_client.sys.stderr
    archon_client.sys.stderr = _DeadStream()  # type: ignore[assignment]
    try:
        archon_client._receipt("anything")  # must not raise
    finally:
        archon_client.sys.stderr = original


def test_no_module_level_cached_client() -> None:
    """Rule 2 — no shared httpx client hiding at module scope."""
    for name, value in vars(archon_client).items():
        assert not isinstance(
            value, (httpx.AsyncClient, httpx.Client)
        ), f"module-level cached client: {name}"


# ===========================================================================
# Contract validation — ValueError BEFORE any network work
# ===========================================================================


@pytest.mark.asyncio
async def test_blank_codebase_id_raises_before_dispatch() -> None:
    captured: list[httpx.Request] = []
    async with _client(_capturing(captured)) as c:
        with pytest.raises(ValueError, match="codebase_id"):
            await archon_client.create_conversation_and_dispatch(
                "   ", "do the thing", client=c
            )
    assert captured == [], "validation must run before the request"


@pytest.mark.asyncio
async def test_blank_message_raises_before_dispatch() -> None:
    captured: list[httpx.Request] = []
    async with _client(_capturing(captured)) as c:
        with pytest.raises(ValueError, match="message"):
            await archon_client.create_conversation_and_dispatch(
                "cb-1", "   \n ", client=c
            )
    assert captured == []


@pytest.mark.asyncio
async def test_send_message_rejects_path_traversal_id() -> None:
    """Mirrors Archon's own `/^[\\w-]+$/` guard (api.ts:2496)."""
    captured: list[httpx.Request] = []
    async with _client(_capturing(captured)) as c:
        with pytest.raises(ValueError, match="conversation_id"):
            await archon_client.send_message("../../etc/passwd", "hi", client=c)
    assert captured == []


@pytest.mark.asyncio
async def test_get_run_rejects_malformed_run_id() -> None:
    captured: list[httpx.Request] = []
    async with _client(_capturing(captured)) as c:
        with pytest.raises(ValueError, match="run_id"):
            await archon_client.get_run("abc/../../secrets", client=c)
    assert captured == []


@pytest.mark.parametrize("bad", ["", "spike echo", "-leading", "../evil", "a\nb"])
def test_build_workflow_message_rejects_bad_names(bad: str) -> None:
    with pytest.raises(ValueError, match="workflow"):
        archon_client.build_workflow_message(bad, "some text")


def test_build_workflow_message_rejects_blank_text() -> None:
    """A blank brief is exactly the vague-voice-turn failure F2 warns about."""
    with pytest.raises(ValueError, match="message"):
        archon_client.build_workflow_message("spike-echo", "   ")


@pytest.mark.asyncio
async def test_steer_rejects_unknown_action() -> None:
    captured: list[httpx.Request] = []
    async with _client(_capturing(captured)) as c:
        with pytest.raises(ValueError, match="action"):
            await archon_client.steer("run1", "delete", client=c)
    assert captured == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["resume", "abandon", "cancel"])
async def test_steer_refuses_note_on_bodyless_action(action: str) -> None:
    """Dropping an operator's stated reason silently is worse than refusing."""
    captured: list[httpx.Request] = []
    async with _client(_capturing(captured)) as c:
        with pytest.raises(ValueError, match="no note"):
            await archon_client.steer("run1", action, note="because", client=c)
    assert captured == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_limit", [0, -1, "10"])
async def test_list_runs_rejects_bad_limit(bad_limit: Any) -> None:
    captured: list[httpx.Request] = []
    async with _client(_capturing(captured)) as c:
        with pytest.raises(ValueError, match="limit"):
            await archon_client.list_runs(bad_limit, client=c)
    assert captured == []


@pytest.mark.asyncio
async def test_list_runs_rejects_unknown_status() -> None:
    """Archon silently DROPS an unrecognised status (api.ts:3493) and returns
    everything — which reads like "the filter matched nothing to exclude"."""
    captured: list[httpx.Request] = []
    async with _client(_capturing(captured)) as c:
        with pytest.raises(ValueError, match="status"):
            await archon_client.list_runs(10, status="in_progress", client=c)
    assert captured == []


# ===========================================================================
# Dispatch — exact URL + body
# ===========================================================================


@pytest.mark.asyncio
async def test_create_conversation_and_dispatch_wire_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARCHON_API_BASE_URL", "http://127.0.0.1:3090")
    captured: list[httpx.Request] = []
    payload = {
        "conversationId": "web-1785174868068-x5630h",
        "id": "conv-db-42",
        "dispatched": True,
        "accepted": True,
        "status": "started",
    }
    async with _client(_capturing(captured, payload)) as c:
        result = await archon_client.create_conversation_and_dispatch(
            "058ef39d", "hello archon", client=c
        )

    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST"
    assert str(request.url) == "http://127.0.0.1:3090/api/conversations"
    import json

    assert json.loads(request.content) == {
        "codebaseId": "058ef39d",
        "message": "hello archon",
    }
    assert isinstance(result, archon_client.ArchonDispatch)
    assert result.conversation_id == "web-1785174868068-x5630h"
    assert result.conversation_db_id == "conv-db-42"
    assert result.dispatched is True
    assert result.accepted is True
    assert result.status == "started"


def test_build_workflow_message_is_byte_identical_to_archon() -> None:
    """`api.ts:3094` builds exactly: `/workflow run ${name} ${message}`."""
    assert (
        archon_client.build_workflow_message("spike-echo", "echo hi")
        == "/workflow run spike-echo echo hi"
    )


@pytest.mark.asyncio
async def test_dispatch_workflow_sends_the_orchestrator_string() -> None:
    captured: list[httpx.Request] = []
    payload = {"conversationId": "web-1", "id": "db-1", "dispatched": True}
    async with _client(_capturing(captured, payload)) as c:
        await archon_client.dispatch_workflow(
            "058ef39d", "spike-echo", "ship the thing", client=c
        )
    import json

    body = json.loads(captured[0].content)
    assert body["message"] == "/workflow run spike-echo ship the thing"
    assert body["codebaseId"] == "058ef39d"


@pytest.mark.asyncio
async def test_send_message_wire_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCHON_API_BASE_URL", "http://127.0.0.1:3090")
    captured: list[httpx.Request] = []
    async with _client(_capturing(captured, {"accepted": True, "status": "started"})) as c:
        body = await archon_client.send_message(
            "web-1785174868068-x5630h", "looks good, ship it", client=c
        )
    request = captured[0]
    assert request.method == "POST"
    assert str(request.url) == (
        "http://127.0.0.1:3090/api/conversations/web-1785174868068-x5630h/message"
    )
    import json

    assert json.loads(request.content) == {"message": "looks good, ship it"}
    assert body == {"accepted": True, "status": "started"}


@pytest.mark.asyncio
async def test_dispatch_missing_ids_is_a_protocol_error() -> None:
    """A 2xx body without conversationId means client/server version skew."""
    async with _client(_capturing([], {"dispatched": True})) as c:
        with pytest.raises(archon_client.ArchonProtocolError):
            await archon_client.create_conversation_and_dispatch(
                "cb", "msg", client=c
            )


# ===========================================================================
# Read — get_run / list_runs
# ===========================================================================


@pytest.mark.asyncio
async def test_get_run_parses_run_and_events(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCHON_API_BASE_URL", "http://127.0.0.1:3090")
    captured: list[httpx.Request] = []
    detail = {
        "run": {
            **RUN_WIRE,
            "worker_platform_id": "web-worker-1",
            "parent_platform_id": "web-parent-1",
            "conversation_platform_id": None,
        },
        "events": [EVENT_WIRE],
    }
    async with _client(_capturing(captured, detail)) as c:
        result = await archon_client.get_run(RUN_WIRE["id"], client=c)

    assert captured[0].method == "GET"
    assert str(captured[0].url) == (
        f"http://127.0.0.1:3090/api/workflows/runs/{RUN_WIRE['id']}"
    )
    assert result.run.id == RUN_WIRE["id"]
    assert result.run.workflow_name == "spike-echo"
    assert result.run.status == "running"
    assert result.run.parent_conversation_id == "parent-db-id"
    assert result.run.worker_platform_id == "web-worker-1"
    assert result.run.working_path == "/tmp/worktrees/archon/thread-03fb5edf"
    assert result.run.metadata["approval"]["nodeId"] == "gate"
    assert len(result.events) == 1
    assert result.events[0].event_type == "tool_called"
    assert result.events[0].step_name == "echo-node"
    assert result.events[0].data["tool_name"] == "Bash"


@pytest.mark.asyncio
async def test_get_run_maps_404_to_not_found() -> None:
    async with _client(_capturing([], {"error": "Workflow run not found"}, 404)) as c:
        with pytest.raises(archon_client.ArchonNotFoundError) as excinfo:
            await archon_client.get_run("deadbeef", client=c)
    assert excinfo.value.friendly_message


@pytest.mark.asyncio
async def test_get_run_missing_run_key_is_protocol_error() -> None:
    async with _client(_capturing([], {"events": []})) as c:
        with pytest.raises(archon_client.ArchonProtocolError):
            await archon_client.get_run("deadbeef", client=c)


@pytest.mark.asyncio
async def test_list_runs_sends_params_and_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARCHON_API_BASE_URL", "http://127.0.0.1:3090")
    captured: list[httpx.Request] = []
    async with _client(_capturing(captured, {"runs": [RUN_WIRE]})) as c:
        runs = await archon_client.list_runs(
            5, status="paused", codebase_id="058ef39d", client=c
        )
    request = captured[0]
    assert request.method == "GET"
    assert request.url.path == "/api/workflows/runs"
    assert dict(request.url.params) == {
        "limit": "5",
        "status": "paused",
        "codebaseId": "058ef39d",
    }
    assert len(runs) == 1
    assert runs[0].id == RUN_WIRE["id"]


@pytest.mark.asyncio
async def test_list_runs_omits_absent_filters() -> None:
    captured: list[httpx.Request] = []
    async with _client(_capturing(captured, {"runs": []})) as c:
        assert await archon_client.list_runs(client=c) == []
    assert dict(captured[0].url.params) == {"limit": "50"}


@pytest.mark.asyncio
async def test_list_runs_missing_runs_key_is_protocol_error() -> None:
    async with _client(_capturing([], {"items": []})) as c:
        with pytest.raises(archon_client.ArchonProtocolError):
            await archon_client.list_runs(client=c)


@pytest.mark.asyncio
async def test_list_runs_skips_non_dict_entries() -> None:
    async with _client(_capturing([], {"runs": [RUN_WIRE, "junk", None]})) as c:
        runs = await archon_client.list_runs(client=c)
    assert len(runs) == 1


# ===========================================================================
# Steer
# ===========================================================================


@pytest.mark.asyncio
async def test_steer_approve_sends_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCHON_API_BASE_URL", "http://127.0.0.1:3090")
    captured: list[httpx.Request] = []
    async with _client(
        _capturing(captured, {"success": True, "message": "Approved"})
    ) as c:
        result = await archon_client.steer(
            "run-abc", "approve", note="looks good", client=c
        )
    request = captured[0]
    assert request.method == "POST"
    assert str(request.url) == (
        "http://127.0.0.1:3090/api/workflows/runs/run-abc/approve"
    )
    import json

    assert json.loads(request.content) == {"comment": "looks good"}
    assert result.action == "approve"
    assert result.run_id == "run-abc"
    assert result.success is True
    assert result.message == "Approved"


@pytest.mark.asyncio
async def test_steer_reject_uses_reason_key_not_comment() -> None:
    """approve→`comment`, reject→`reason` (workflow.schemas.ts:155-162)."""
    captured: list[httpx.Request] = []
    async with _client(_capturing(captured, {"success": True, "message": "ok"})) as c:
        await archon_client.steer(
            "run-abc", "reject", note="wrong branch", client=c
        )
    import json

    assert json.loads(captured[0].content) == {"reason": "wrong branch"}


@pytest.mark.asyncio
async def test_steer_approve_without_note_sends_empty_object() -> None:
    captured: list[httpx.Request] = []
    async with _client(_capturing(captured, {"success": True, "message": "ok"})) as c:
        await archon_client.steer("run-abc", "approve", client=c)
    assert captured[0].content == b"{}"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["resume", "abandon", "cancel"])
async def test_steer_bodyless_actions_send_no_body(action: str) -> None:
    captured: list[httpx.Request] = []
    async with _client(_capturing(captured, {"success": True, "message": "ok"})) as c:
        await archon_client.steer("run-abc", action, client=c)
    assert captured[0].url.path == f"/api/workflows/runs/run-abc/{action}"
    assert captured[0].content == b""


@pytest.mark.asyncio
async def test_steer_maps_400_to_bad_request() -> None:
    """Archon 400s an approve against a run that is not paused."""
    async with _client(_capturing([], {"error": "Run is not paused"}, 400)) as c:
        with pytest.raises(archon_client.ArchonBadRequestError):
            await archon_client.steer("run-abc", "approve", client=c)


def test_steer_actions_cover_the_shipped_endpoints() -> None:
    assert archon_client.STEER_ACTIONS == {
        "approve",
        "reject",
        "resume",
        "abandon",
        "cancel",
    }


# ===========================================================================
# Status + transport error mapping
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, archon_client.ArchonBadRequestError),
        (401, archon_client.ArchonAuthError),
        (403, archon_client.ArchonAuthError),
        (404, archon_client.ArchonNotFoundError),
        (422, archon_client.ArchonBadRequestError),
        (500, archon_client.ArchonServerError),
        (502, archon_client.ArchonServerError),
    ],
)
async def test_status_mapping(status: int, expected: type[Exception]) -> None:
    async with _client(_capturing([], {"error": "x"}, status)) as c:
        with pytest.raises(expected) as excinfo:
            await archon_client.list_runs(client=c)
    assert isinstance(excinfo.value, archon_client.ArchonAPIError)
    assert excinfo.value.friendly_message


@pytest.mark.asyncio
async def test_connect_error_maps_to_unreachable() -> None:
    async with _client(_exploding(httpx.ConnectError("refused"))) as c:
        with pytest.raises(archon_client.ArchonUnreachableError) as excinfo:
            await archon_client.list_runs(client=c)
    assert "not reachable" in excinfo.value.friendly_message


@pytest.mark.asyncio
async def test_read_timeout_maps_to_timeout_not_unreachable() -> None:
    """A wedged Archon and a missing Archon are different operator problems."""
    async with _client(_exploding(httpx.ReadTimeout("slow"))) as c:
        with pytest.raises(archon_client.ArchonTimeoutError):
            await archon_client.list_runs(client=c)


@pytest.mark.asyncio
async def test_other_transport_errors_map_to_base_error() -> None:
    async with _client(_exploding(httpx.TooManyRedirects("loop"))) as c:
        with pytest.raises(archon_client.ArchonAPIError):
            await archon_client.list_runs(client=c)


@pytest.mark.asyncio
async def test_empty_body_on_200_does_not_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    async with _client(handler) as c:
        body = await archon_client.send_message("web-1", "hi", client=c)
    assert body == {}


# ===========================================================================
# Record tolerance — Archon is separately versioned
# ===========================================================================


def test_run_from_wire_tolerates_unknown_fields() -> None:
    run = archon_client.ArchonRun.from_wire(
        {**RUN_WIRE, "some_future_column": 42}
    )
    assert run.id == RUN_WIRE["id"]
    assert run.raw["some_future_column"] == 42


def test_run_from_wire_tolerates_missing_optionals() -> None:
    run = archon_client.ArchonRun.from_wire({"id": "r1"})
    assert run.status == ""
    assert run.metadata == {}
    assert run.conversation_id is None


def test_run_from_wire_requires_id() -> None:
    with pytest.raises(archon_client.ArchonProtocolError):
        archon_client.ArchonRun.from_wire({"workflow_name": "x"})


def test_run_status_helpers_match_archon_definitions() -> None:
    """Ported verbatim from workflow-run.ts:22-32."""
    assert archon_client.TERMINAL_RUN_STATUSES == {
        "completed",
        "failed",
        "cancelled",
    }
    assert archon_client.RESUMABLE_RUN_STATUSES == {"failed", "paused"}
    assert archon_client.ArchonRun.from_wire(
        {"id": "r", "status": "completed"}
    ).is_terminal
    assert not archon_client.ArchonRun.from_wire(
        {"id": "r", "status": "running"}
    ).is_terminal
    assert archon_client.ArchonRun.from_wire({"id": "r", "status": "paused"}).is_paused


def test_event_from_wire_normalises_non_dict_data() -> None:
    """`data` is LLM-adjacent; a non-object must not become an attribute error."""
    event = archon_client.ArchonEvent.from_wire(
        {**EVENT_WIRE, "data": "not-an-object"}
    )
    assert event.data == {}
    assert event.raw["data"] == "not-an-object"


def test_event_from_wire_tolerates_null_step_index() -> None:
    event = archon_client.ArchonEvent.from_wire({**EVENT_WIRE, "step_index": None})
    assert event.step_index is None


# ===========================================================================
# F7 — loopback posture (Rule 2: physical socket state, not a config claim)
# ===========================================================================


def test_posture_reports_exposed_when_lan_address_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        archon_client, "_local_ipv4_addresses", lambda: ("192.168.1.50",)
    )
    monkeypatch.setattr(
        archon_client, "_tcp_connect", lambda addr, port, timeout: "accepted"
    )
    posture = archon_client.check_loopback_posture(3090)
    assert posture.checked is True
    assert posture.pinned is False
    assert posture.reachable_addresses == ("192.168.1.50",)
    assert "EXPOSED" in posture.summary
    assert "192.168.1.50" in posture.summary


def test_posture_reports_pinned_when_lan_address_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        archon_client, "_local_ipv4_addresses", lambda: ("192.168.1.50",)
    )
    monkeypatch.setattr(
        archon_client, "_tcp_connect", lambda addr, port, timeout: "refused"
    )
    posture = archon_client.check_loopback_posture(3090)
    assert posture.pinned is True
    assert posture.checked is True
    assert posture.reachable_addresses == ()
    assert "PINNED" in posture.summary


def test_posture_untestable_on_timeout_fails_closed_not_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex round-2 MAJOR: a timeout is not proof of refusal.

    `_tcp_connect` previously collapsed every non-accept outcome (including a
    bare `socket.timeout`, which is an `OSError` but NOT `ConnectionRefusedError`)
    into a boolean `False`, so `check_loopback_posture` read it the same as an
    explicit refusal and certified `pinned=True` for an address that was never
    actually proven closed. A firewall silently dropping the SYN (or any other
    transient network failure) must read as untestable, not pinned.
    """
    monkeypatch.setattr(
        archon_client, "_local_ipv4_addresses", lambda: ("192.168.1.50",)
    )
    monkeypatch.setattr(
        archon_client, "_tcp_connect", lambda addr, port, timeout: "inconclusive"
    )
    posture = archon_client.check_loopback_posture(3090)
    assert posture.checked is False
    assert posture.pinned is False
    assert posture.reachable_addresses == ()
    assert "inconclusive" in posture.summary
    assert archon_client._main(["archon_client", "posture"]) == 1


def test_posture_reports_partial_exposure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A multi-homed box can be pinned on one NIC and open on another."""
    monkeypatch.setattr(
        archon_client,
        "_local_ipv4_addresses",
        lambda: ("192.168.1.50", "10.8.0.2"),
    )
    monkeypatch.setattr(
        archon_client,
        "_tcp_connect",
        lambda addr, port, timeout: "accepted" if addr == "10.8.0.2" else "refused",
    )
    posture = archon_client.check_loopback_posture(3090)
    assert posture.pinned is False
    assert posture.reachable_addresses == ("10.8.0.2",)


def test_posture_marks_unchecked_with_no_lan_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(archon_client, "_local_ipv4_addresses", lambda: ())
    posture = archon_client.check_loopback_posture(3090)
    assert posture.checked is False
    assert "could not be tested" in posture.summary


def test_posture_unchecked_fails_closed_not_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An untestable posture must never assert `pinned=True`.

    Codex round-1 MAJOR: `checked=False` (no LAN address to probe — including
    a swallowed OSError from `_local_ipv4_addresses`) previously still set
    `pinned=True`, so `_main` exited 0 and falsely certified a pin nobody
    verified. Fail closed instead: unchecked must read as NOT pinned.
    """
    monkeypatch.setattr(archon_client, "_local_ipv4_addresses", lambda: ())
    posture = archon_client.check_loopback_posture(3090)
    assert posture.checked is False
    assert posture.pinned is False


def test_posture_refuses_to_certify_a_remote_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kimi round-1 MAJOR: the port comes from the base URL but the addresses
    come from THIS machine. Pointed at a remote Archon, the probe would emit
    its strongest pass output about a host it never probed — refuse instead.
    """
    monkeypatch.setattr(
        archon_client, "_base_url", lambda: "http://192.168.1.50:3090"
    )
    monkeypatch.setattr(
        archon_client, "_local_ipv4_addresses", lambda: ("10.0.0.5",)
    )
    posture = archon_client.check_loopback_posture()
    assert posture.checked is False
    assert posture.pinned is False
    assert posture.remote_host == "192.168.1.50"
    assert "192.168.1.50" in posture.summary
    assert "ON that host" in posture.summary


def test_posture_explicit_port_still_probes_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit `port` argument asks about THIS machine's exposure — the
    remote-URL refusal applies only to the default resolve-from-config path
    (every existing caller of `check_loopback_posture(<port>)` keeps its
    local-probe semantics)."""
    monkeypatch.setattr(
        archon_client, "_base_url", lambda: "http://192.168.1.50:3090"
    )
    monkeypatch.setattr(
        archon_client, "_local_ipv4_addresses", lambda: ("10.0.0.5",)
    )
    monkeypatch.setattr(archon_client, "_tcp_connect", lambda a, p, t: "refused")
    posture = archon_client.check_loopback_posture(3090)
    assert posture.checked is True
    assert posture.pinned is True


def test_posture_cli_exit_code_nonzero_when_unchecked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ops CLI must not exit 0 for an untestable posture (fail closed)."""
    monkeypatch.setattr(archon_client, "_local_ipv4_addresses", lambda: ())
    assert archon_client._main(["archon_client", "posture"]) == 1


def test_posture_port_resolves_from_base_url_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 1 — the probed port follows the env, it is not frozen at import."""
    seen: list[int] = []
    monkeypatch.setattr(
        archon_client, "_local_ipv4_addresses", lambda: ("192.168.1.50",)
    )
    monkeypatch.setattr(
        archon_client,
        "_tcp_connect",
        lambda addr, port, timeout: seen.append(port) or "refused",
    )
    monkeypatch.setenv("ARCHON_API_BASE_URL", "http://127.0.0.1:4444")
    archon_client.check_loopback_posture()
    monkeypatch.setenv("ARCHON_API_BASE_URL", "http://127.0.0.1:5555")
    archon_client.check_loopback_posture()
    assert seen == [4444, 5555]


def test_local_ipv4_addresses_excludes_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_getaddrinfo(host: str, port: Any, family: Any):
        return [
            (family, 1, 6, "", ("127.0.0.1", 0)),
            (family, 1, 6, "", ("192.168.1.50", 0)),
            (family, 1, 6, "", ("192.168.1.50", 0)),
        ]

    monkeypatch.setattr(archon_client.socket, "getaddrinfo", fake_getaddrinfo)
    assert archon_client._local_ipv4_addresses() == ("192.168.1.50",)


def test_local_ipv4_addresses_survives_lookup_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*args: Any, **kwargs: Any):
        raise OSError("no dns")

    monkeypatch.setattr(archon_client.socket, "getaddrinfo", boom)
    assert archon_client._local_ipv4_addresses() == ()
    assert "address lookup failed" in capsys.readouterr().err


def test_posture_cli_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        archon_client, "_local_ipv4_addresses", lambda: ("192.168.1.50",)
    )
    monkeypatch.setattr(
        archon_client, "_tcp_connect", lambda addr, port, timeout: "accepted"
    )
    assert archon_client._main(["archon_client", "posture"]) == 1
    monkeypatch.setattr(
        archon_client, "_tcp_connect", lambda addr, port, timeout: "refused"
    )
    assert archon_client._main(["archon_client", "posture"]) == 0
    assert archon_client._main(["archon_client", "nonsense"]) == 2


# ===========================================================================
# Public export — the client is framework code, so it ships
#
# `docs/` is default-deny in the sanitizer; a new manual page that nobody adds
# to INCLUDE_FILES is silently dropped from the public export and nobody
# notices until someone goes looking for it. These two cover the INCLUDE_FILES
# edit this ticket made. (`scripts/sanitize_test.py` owns the repo-wide sweep.)
# ===========================================================================

_MANUAL_PAGE_REL = "docs/manual/features/archon-execution-client.md"


def _load_sanitize() -> Any:
    import sys as _sys
    from pathlib import Path as _Path

    scripts_dir = _Path(archon_client.__file__).resolve().parents[3] / "scripts"
    if str(scripts_dir) not in _sys.path:
        _sys.path.insert(0, str(scripts_dir))
    import sanitize  # noqa: PLC0415  (import after sys.path manipulation)

    return sanitize


def test_manual_page_is_allowlisted_for_public_export() -> None:
    sanitize = _load_sanitize()
    assert _MANUAL_PAGE_REL in sanitize.INCLUDE_FILES
    assert sanitize.is_denied(_MANUAL_PAGE_REL) is False


def test_manual_page_is_born_clean() -> None:
    """The page must survive the scrubber byte-for-byte.

    ``scrub_content`` IS the born-clean oracle: it applies the sanitizer's real
    REPLACEMENTS table (operator name, home paths, private repo/vault names), so
    a byte-identical round trip proves the page carries none of them. Asserted
    this way rather than against a copied term list on purpose — a copied list
    both drifts from the canonical table and, since this file scans itself,
    would fail on its own literals.
    """
    from pathlib import Path as _Path

    sanitize = _load_sanitize()
    repo_root = _Path(archon_client.__file__).resolve().parents[3]
    raw = (repo_root / _MANUAL_PAGE_REL).read_text(encoding="utf-8")
    assert sanitize.scrub_content(raw, _MANUAL_PAGE_REL) == raw


def test_client_and_tests_are_born_clean() -> None:
    """Same bar for the client and this test file — both export publicly."""
    from pathlib import Path as _Path

    sanitize = _load_sanitize()
    for path in (_Path(archon_client.__file__), _Path(__file__)):
        raw = path.read_text(encoding="utf-8")
        assert sanitize.scrub_content(raw, path.name) == raw, (
            f"{path.name} is not born clean — the scrubber rewrote it"
        )


# ===========================================================================
# Live-optional — opt-in, skipped by default
# ===========================================================================

_LIVE = os.getenv("ARCHON_LIVE_TESTS") == "1"
_LIVE_DISPATCH = os.getenv("ARCHON_LIVE_DISPATCH") == "1"
_LIVE_CODEBASE = os.getenv("ARCHON_LIVE_CODEBASE_ID", "")


@pytest.mark.asyncio
@pytest.mark.skipif(not _LIVE, reason="set ARCHON_LIVE_TESTS=1 with Archon running")
async def test_live_read_contract() -> None:
    """Read-only round trip against a live Archon: list_runs → get_run.

    Proves the wire shape this module encodes still matches the running build.
    Mutates nothing and costs no worktree.
    """
    runs = await archon_client.list_runs(5)
    assert isinstance(runs, list)
    if not runs:
        pytest.skip("live Archon has no runs to read")
    detail = await archon_client.get_run(runs[0].id)
    assert detail.run.id == runs[0].id
    assert all(e.workflow_run_id == runs[0].id for e in detail.events)


@pytest.mark.skipif(not _LIVE, reason="set ARCHON_LIVE_TESTS=1 with Archon running")
def test_live_loopback_posture_is_pinned() -> None:
    """F7 acceptance: a LAN-address connect to Archon must be REFUSED.

    Skip is legitimate ONLY when the host has no non-loopback address to
    probe. Inconclusive probes (`checked=False` with `lan_addresses`
    present — blackholed SYN, firewall drop) are NOT proof and must FAIL:
    the old `not posture.checked` skip silently passed exactly that state
    (Codex round-3 major). On hosts where vSwitch interfaces blackhole
    instead of refusing, this failing is correct fail-closed behavior —
    the physical bind receipt (netstat) is the acceptance evidence there.
    """
    posture = archon_client.check_loopback_posture()
    if not posture.lan_addresses:
        pytest.skip("no non-loopback address on this host")
    assert posture.checked, posture.summary
    assert posture.pinned, posture.summary


@pytest.mark.asyncio
@pytest.mark.skipif(
    not (_LIVE and _LIVE_DISPATCH and _LIVE_CODEBASE),
    reason=(
        "set ARCHON_LIVE_TESTS=1 ARCHON_LIVE_DISPATCH=1 "
        "ARCHON_LIVE_CODEBASE_ID=<id> — costs ~53s and one worktree"
    ),
)
async def test_live_spike_echo_dispatch() -> None:
    """Dispatch `.archon/workflows/spike-echo.yaml` — the standing smoke.

    Zero repo mutation by design (one bash node + one forced tool call).
    Asserts only that the dispatch was ACCEPTED; the run's own completion is
    Archon's business and takes ~53s.
    """
    result = await archon_client.dispatch_workflow(
        _LIVE_CODEBASE, "spike-echo", "smoke test from the Homie archon client"
    )
    assert result.dispatched is True
    assert result.accepted is True
    assert result.conversation_id.startswith("web-")
