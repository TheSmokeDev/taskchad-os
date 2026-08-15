"""One-time persona capability elevation security and lifecycle tests (#262)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
CHAT_DIR = SCRIPTS_DIR.parent / "chat"
for candidate in (SCRIPTS_DIR, CHAT_DIR):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from models import Channel, IncomingMessage, OutgoingMessage, Platform, Thread, User  # noqa: E402
from router import ChatRouter  # noqa: E402

from runtime import persona_elevation, persona_tools, tool_registry  # noqa: E402
from runtime.base import RuntimeResult, RuntimeToolCall  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import config

    saved = dict(tool_registry._REGISTRY)
    tool_registry._REGISTRY.clear()
    persona_elevation.clear_process_grants_for_tests()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data", raising=False)
    monkeypatch.delenv("HOMIE_KILLSWITCH_PERSONA_ELEVATION", raising=False)
    yield
    persona_elevation.clear_process_grants_for_tests()
    tool_registry._REGISTRY.clear()
    tool_registry._REGISTRY.update(saved)


def _toolsets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "runtime.toolsets.TOOLSETS",
        {
            "safe_core": {
                "description": "safe",
                "tools": ["request_tool"],
                "includes": [],
            },
            "extra": {
                "description": "extra",
                "tools": ["extra_read", "dedicated_write"],
                "includes": [],
            },
        },
        raising=False,
    )


def _register(monkeypatch: pytest.MonkeyPatch, calls: list[dict] | None = None) -> list[dict]:
    _toolsets(monkeypatch)
    observed = calls if calls is not None else []
    persona_elevation.register_tools()
    tool_registry.register_tool(
        "extra_read",
        "read one extra thing",
        toolset="extra",
        handler=lambda **kwargs: observed.append(kwargs) or {"ok": True},
        elevatable=True,
    )
    tool_registry.register_tool(
        "dedicated_write",
        "dedicated external mutation",
        toolset="extra",
        handler=lambda **_kwargs: "must not run",
        effect="write",
        dedicated_gate=True,
    )
    return observed


def _context(tmp_path: Path, *, turn_id: str = "turn-1") -> dict:
    return {
        "persona_id": "ai-engineer",
        "platform": "discord",
        "channel_id": "222222222222222222",
        "thread_id": "222222222222222222",
        "guild_id": "guild-1",
        "session_key": "discord:dev:dev",
        "turn_id": turn_id,
        "original_user_id": "operator-1",
        "original_user_name": "Operator",
        "original_user_role": "admin",
        "original_text": "Inspect the build receipt.",
        "has_attachments": False,
        "project_root": str(tmp_path),
    }


def _create_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tool: str = "extra_read",
    arguments: dict | None = None,
    turn_id: str = "turn-1",
):
    _register(monkeypatch)
    payload = persona_tools.build_persona_tool_payload(
        "ai-engineer",
        {"toolsets": ["safe_core"]},
        request_context=_context(tmp_path, turn_id=turn_id),
    )
    assert payload is not None
    definitions, dispatch = payload
    assert {
        row["function"]["name"] for row in definitions
    } == set(persona_tools.PERSONA_CHAT_BASE_TOOLS)
    result = json.loads(
        dispatch(
            "request_tool",
            {
                "tool": tool,
                "reason": "The build receipt is outside my normal scope.",
                "arguments": arguments or {"receipt": "build-7"},
            },
        )
    )
    return result


def test_request_bridge_is_implicit_for_every_named_chat_persona(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register(monkeypatch)
    context = _context(tmp_path)

    no_safe_core = persona_tools.build_persona_tool_payload(
        "crypto",
        {"toolsets": ["extra"]},
        request_context={**context, "persona_id": "crypto"},
    )
    assert no_safe_core is not None
    assert set(persona_tools.PERSONA_CHAT_BASE_TOOLS) <= {
        row["function"]["name"] for row in no_safe_core[0]
    }

    no_declared_scope = persona_tools.build_persona_tool_payload(
        "legacy-persona",
        {},
        request_context={**context, "persona_id": "legacy-persona"},
    )
    assert no_declared_scope is not None
    assert {
        row["function"]["name"] for row in no_declared_scope[0]
    } == set(persona_tools.PERSONA_CHAT_BASE_TOOLS)


def test_request_approval_claim_and_exact_one_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []
    _toolsets(monkeypatch)
    persona_elevation.register_tools()
    tool_registry.register_tool(
        "extra_read",
        "read one extra thing",
        toolset="extra",
        handler=lambda **kwargs: calls.append(kwargs) or "done",
        elevatable=True,
    )
    payload = persona_tools.build_persona_tool_payload(
        "ai-engineer",
        {"toolsets": ["safe_core"]},
        request_context=_context(tmp_path),
    )
    assert payload is not None
    result = json.loads(
        payload[1](
            "request_tool",
            {
                "tool": "extra_read",
                "reason": "Need the exact build receipt.",
                "arguments": {"receipt": "build-7"},
            },
        )
    )
    assert result["status"] == "approval_required"

    decision = persona_elevation.decide_request(
        result["short_code"],
        approve=True,
        operator_id="discord:operator-1",
        platform="discord",
        channel_id="222222222222222222",
    )
    assert decision.outcome == "approved"
    grant, error = persona_elevation.claim_grant(
        result["request_id"],
        persona_id="ai-engineer",
        platform="discord",
        channel_id="222222222222222222",
    )
    assert error == "" and grant is not None

    approved = persona_tools.build_persona_tool_payload(
        "ai-engineer",
        {"toolsets": ["safe_core"]},
        request_context=_context(tmp_path, turn_id="approved-retry"),
        elevation_grant=grant,
    )
    assert approved is not None
    assert {row["function"]["name"] for row in approved[0]} == {
        *persona_tools.PERSONA_CHAT_BASE_TOOLS,
        "extra_read",
    }
    assert "do not match" in approved[1]("extra_read", {"receipt": "other"})
    assert approved[1]("extra_read", {"receipt": "build-7"}) == "done"
    assert "already used" in approved[1]("extra_read", {"receipt": "build-7"})
    assert calls == [{"receipt": "build-7"}]


def test_dedicated_gate_is_never_requestable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _create_request(
        tmp_path,
        monkeypatch,
        tool="dedicated_write",
        arguments={"amount": 1},
    )
    assert result["status"] == "refused"
    assert "cannot use one-time elevation" in result["error"]
    audit = json.loads((tmp_path / "data" / "persona_elevation.jsonl").read_text().strip())
    assert audit["outcome"] == "refused"
    assert audit["persona_id"] == "ai-engineer"
    assert audit["tool_name"] == "dedicated_write"


def test_hidden_argument_tail_cannot_reach_approval_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _create_request(
        tmp_path,
        monkeypatch,
        arguments={"payload": "x" * 901},
    )
    assert result["status"] == "refused"
    assert result["error"] == "arguments exceed the request limit"
    assert persona_elevation.pending_request_for_turn("ai-engineer", "turn-1") is None


def test_request_is_bound_to_origin_and_persona(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _create_request(tmp_path, monkeypatch)
    refused = persona_elevation.decide_request(
        result["request_id"],
        approve=True,
        operator_id="discord:operator-1",
        platform="discord",
        channel_id="different-channel",
    )
    assert refused.outcome == "refused"

    approved = persona_elevation.decide_request(
        result["request_id"],
        approve=True,
        operator_id="discord:operator-1",
        platform="discord",
        channel_id="222222222222222222",
    )
    assert approved.outcome == "approved"
    grant, error = persona_elevation.claim_grant(
        result["request_id"],
        persona_id="founder-operator",
        platform="discord",
        channel_id="222222222222222222",
    )
    assert grant is None and "does not match" in error


def test_denial_is_cas_and_same_turn_cannot_nag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _create_request(tmp_path, monkeypatch)
    denied = persona_elevation.decide_request(
        first["request_id"],
        approve=False,
        operator_id="discord:operator-1",
        platform="discord",
        channel_id="222222222222222222",
    )
    assert denied.outcome == "denied"
    second = persona_elevation.request_tool(
        tool="extra_read",
        reason="ask again",
        arguments={"receipt": "build-8"},
        _persona_id="ai-engineer",
        _dispatch_context=_context(tmp_path),
    )
    repeated = json.loads(second)
    assert repeated["status"] == "denied"
    assert repeated["request_id"] == first["request_id"]


def test_resume_keeps_original_turn_id_and_cannot_chain_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _create_request(tmp_path, monkeypatch)
    assert persona_elevation.decide_request(
        result["request_id"],
        approve=True,
        operator_id="discord:operator-1",
        platform="discord",
        channel_id="222222222222222222",
    ).outcome == "approved"
    grant, error = persona_elevation.claim_grant(
        result["request_id"],
        persona_id="ai-engineer",
        platform="discord",
        channel_id="222222222222222222",
    )
    assert grant is not None and error == ""

    resume = IncomingMessage(
        text="Inspect the build receipt.",
        user=User(Platform.DISCORD, "operator-1", "Operator"),
        channel=Channel(Platform.DISCORD, "222222222222222222"),
        platform=Platform.DISCORD,
        thread=Thread("222222222222222222"),
        platform_message_id=f"elevation-resume:{result['request_id']}",
        raw_event={"elevation_original_turn_id": "turn-1"},
    )
    context = persona_elevation.build_turn_context(
        "ai-engineer",
        resume,
        session_key="discord:dev:dev",
        project_root=tmp_path,
    )
    assert context["turn_id"] == "turn-1"

    tool_registry.register_tool(
        "second_read",
        "another read",
        toolset="extra",
        handler=lambda **_kwargs: "never",
        elevatable=True,
    )
    repeated = json.loads(
        persona_elevation.request_tool(
            tool="second_read",
            reason="try to chain approval",
            arguments={},
            _persona_id="ai-engineer",
            _dispatch_context=context,
        )
    )
    assert repeated["status"] == "consumed"
    assert "already used its one capability request" in repeated["error"]


def test_grant_does_not_survive_process_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _create_request(tmp_path, monkeypatch)
    assert persona_elevation.decide_request(
        result["request_id"],
        approve=True,
        operator_id="discord:operator-1",
        platform="discord",
        channel_id="222222222222222222",
    ).outcome == "approved"
    persona_elevation.clear_process_grants_for_tests()
    grant, error = persona_elevation.claim_grant(
        result["request_id"],
        persona_id="ai-engineer",
        platform="discord",
        channel_id="222222222222222222",
    )
    assert grant is None
    assert "process restarted" in error
    assert persona_elevation.get_request(result["request_id"]).status == "expired"


def test_failed_resume_invalidates_unclaimed_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _create_request(tmp_path, monkeypatch)
    assert persona_elevation.decide_request(
        result["request_id"],
        approve=True,
        operator_id="discord:operator-1",
        platform="discord",
        channel_id="222222222222222222",
    ).outcome == "approved"

    assert persona_elevation.invalidate_grant(
        result["request_id"],
        detail="persona binding changed after approval",
    )
    grant, error = persona_elevation.claim_grant(
        result["request_id"],
        persona_id="ai-engineer",
        platform="discord",
        channel_id="222222222222222222",
    )
    assert grant is None
    assert "unavailable" in error
    request = persona_elevation.get_request(result["request_id"])
    assert request is not None
    assert request.status == "expired"
    assert request.status_detail == "persona binding changed after approval"


def test_expired_request_cannot_be_approved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register(monkeypatch)
    result = json.loads(
        persona_elevation.request_tool(
            tool="extra_read",
            reason="need it",
            arguments={"receipt": "old"},
            _persona_id="ai-engineer",
            _dispatch_context=_context(tmp_path),
            now=100.0,
        )
    )
    decision = persona_elevation.decide_request(
        result["request_id"],
        approve=True,
        operator_id="discord:operator-1",
        platform="discord",
        channel_id="222222222222222222",
        now=1000.0,
    )
    assert decision.outcome == "already_decided"
    assert decision.request.status == "expired"


@pytest.mark.parametrize(
    "command",
    [
        "git push origin main",
        "vercel --prod",
        "Set-Content $env:USERPROFILE/.homie/profiles/x/config.yaml nope",
    ],
)
def test_terminal_cannot_bypass_dedicated_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    _register(monkeypatch)
    tool_registry.register_tool(
        "terminal",
        "shell",
        toolset="extra",
        handler=lambda **_kwargs: "never",
        effect="execute",
        elevatable=True,
    )
    result = json.loads(
        persona_elevation.request_tool(
            tool="terminal",
            reason="run a command",
            arguments={"command": command},
            _persona_id="ai-engineer",
            _dispatch_context=_context(tmp_path, turn_id=command),
        )
    )
    assert result["status"] == "refused"
    assert "dedicated" in result["error"]


def test_elevation_kill_switch_stops_new_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register(monkeypatch)
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_ELEVATION", "disabled")
    result = json.loads(
        persona_elevation.request_tool(
            tool="extra_read",
            reason="need it",
            arguments={},
            _persona_id="ai-engineer",
            _dispatch_context=_context(tmp_path),
        )
    )
    assert result == {
        "status": "refused",
        "error": "capability elevation is disabled by operator",
    }


class _Adapter:
    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []

    async def send(self, message: OutgoingMessage) -> str:
        self.sent.append(message)
        return str(len(self.sent))


def _button_incoming(code: str, *, own: bool = True) -> IncomingMessage:
    return IncomingMessage(
        text=f"__button:capability:approve:{code}",
        user=User(Platform.DISCORD, "operator-1", "Operator"),
        channel=Channel(Platform.DISCORD, "222222222222222222"),
        platform=Platform.DISCORD,
        thread=Thread("222222222222222222"),
        # What the canonical ingress seam (models.resolve_ingress_role,
        # #424/#449) stamps for a configured operator's own authenticated
        # Discord button tap — the fail-closed dataclass default is now
        # "viewer", and _handle_capability_decision trusts this field
        # verbatim (router.py:3098), so the pre-seam implicit "admin" must
        # be stated explicitly here to reproduce production shape.
        user_role="admin",
        raw_event={
            "interaction_type": "button",
            "source_message_is_own": own,
            "guild": "guild-1",
        },
    )


@pytest.mark.asyncio
async def test_discord_button_approves_and_automatically_resumes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _create_request(tmp_path, monkeypatch)
    adapter = _Adapter()
    engine = SimpleNamespace(session_store=object(), project_root=tmp_path)
    router = ChatRouter(engine, SimpleNamespace())
    binding = SimpleNamespace(persona_id="ai-engineer")
    monkeypatch.setattr("router.resolve_discord_channel_binding", lambda _incoming: binding)
    resumed: list[IncomingMessage] = []

    async def fake_run(**kwargs):
        resumed.append(kwargs["incoming"])
        grant, error = persona_elevation.claim_grant(
            kwargs["incoming"].raw_event["elevation_resume_request_id"],
            persona_id="ai-engineer",
            platform="discord",
            channel_id="222222222222222222",
        )
        assert grant is not None and error == ""
        return OutgoingMessage(text="receipt inspected", channel=kwargs["incoming"].channel)

    monkeypatch.setattr("router.run_discord_persona_channel_turn", fake_run)
    await router._handle_capability_decision(
        adapter,
        _button_incoming(result["short_code"]),
        action="approve",
        request_code=result["short_code"],
        authenticated_button=True,
    )
    assert [message.text for message in adapter.sent] == [
        "Approved `extra_read` once for `ai-engineer`. Retrying the original task now.",
        "receipt inspected",
    ]
    assert len(resumed) == 1
    assert resumed[0].text == "Inspect the build receipt."


@pytest.mark.asyncio
async def test_spoofed_button_cannot_decide_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _create_request(tmp_path, monkeypatch)
    adapter = _Adapter()
    router = ChatRouter(SimpleNamespace(), SimpleNamespace())
    await router._handle_capability_decision(
        adapter,
        _button_incoming(result["short_code"], own=False),
        action="approve",
        request_code=result["short_code"],
        authenticated_button=True,
    )
    assert "authenticated buttons" in adapter.sent[0].text
    assert persona_elevation.get_request(result["request_id"]).status == "pending"


@pytest.mark.asyncio
async def test_failed_automatic_retry_expires_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _create_request(tmp_path, monkeypatch)
    adapter = _Adapter()
    router = ChatRouter(SimpleNamespace(session_store=object()), SimpleNamespace())
    monkeypatch.setattr(
        "router.resolve_discord_channel_binding",
        lambda _incoming: SimpleNamespace(persona_id="founder-operator"),
    )

    await router._handle_capability_decision(
        adapter,
        _button_incoming(result["short_code"]),
        action="approve",
        request_code=result["short_code"],
        authenticated_button=True,
    )

    request = persona_elevation.get_request(result["request_id"])
    assert request is not None
    assert request.status == "expired"
    assert "binding changed" in request.status_detail
    grant, error = persona_elevation.claim_grant(
        result["request_id"],
        persona_id="ai-engineer",
        platform="discord",
        channel_id="222222222222222222",
    )
    assert grant is None
    assert "unavailable" in error
    assert "retry failed" in adapter.sent[-1].text


@pytest.mark.asyncio
async def test_request_bound_voice_phrase_approves_telegram_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register(monkeypatch)
    context = _context(tmp_path)
    context.update(
        {
            "platform": "telegram",
            "channel_id": "telegram-operator-1",
            "thread_id": "telegram-operator-1",
        }
    )
    requested = json.loads(
        persona_elevation.request_tool(
            tool="extra_read",
            reason="Need the current receipt.",
            arguments={"receipt": "build-9"},
            _persona_id="ai-engineer",
            _dispatch_context=context,
        )
    )

    class _Engine:
        session_store = object()
        project_root = tmp_path

        def __init__(self) -> None:
            self.resumed: list[IncomingMessage] = []

        async def handle_message(self, message: IncomingMessage):
            self.resumed.append(message)
            grant, error = persona_elevation.claim_grant(
                message.raw_event["elevation_resume_request_id"],
                persona_id="ai-engineer",
                platform="telegram",
                channel_id="telegram-operator-1",
            )
            assert grant is not None and error == ""
            yield OutgoingMessage(text="telegram retry complete", channel=message.channel)

    engine = _Engine()
    router = ChatRouter(engine, SimpleNamespace())
    adapter = _Adapter()
    monkeypatch.setattr("personas.get_active_profile_name", lambda: "ai-engineer")
    incoming = IncomingMessage(
        text=f"approve capability {requested['short_code']}",
        user=User(Platform.TELEGRAM, "operator-1", "Operator"),
        channel=Channel(Platform.TELEGRAM, "telegram-operator-1", is_dm=True),
        platform=Platform.TELEGRAM,
        thread=Thread("telegram-operator-1"),
        # See _button_incoming above — the seam's fail-closed default is
        # "viewer"; this reproduces a configured operator's stamped role.
        user_role="admin",
        voice_origin=True,
    )
    await router._handle_inner(adapter, incoming)
    assert [message.text for message in adapter.sent] == [
        "Approved `extra_read` once for `ai-engineer`. Retrying the original task now.",
        "telegram retry complete",
    ]
    assert len(engine.resumed) == 1


def test_registry_refuses_elevatable_dedicated_gate() -> None:
    with pytest.raises(tool_registry.ToolRegistryError):
        tool_registry.register_tool(
            "impossible",
            "bad policy",
            toolset="extra",
            handler=lambda: None,
            elevatable=True,
            dedicated_gate=True,
        )


@pytest.mark.asyncio
async def test_discord_persona_turn_returns_real_approval_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from discord_persona_runtime import run_discord_persona_channel_turn
    from recall_service import RecallResponse, _FallbackLog
    from session import get_session_store

    _register(monkeypatch)
    profile_root = tmp_path / "profiles" / "ai-engineer"
    for relative in ("memory", "memory/daily", "skills", "data", "state"):
        (profile_root / relative).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "personas.lifecycle.show_profile",
        lambda _persona_id: SimpleNamespace(path=profile_root),
    )
    monkeypatch.setattr(
        "personas.load_persona_config",
        lambda _persona_id: {
            "persona": {"display_name": "AI Engineer", "role": "developer"},
            "toolsets": ["safe_core"],
        },
    )
    monkeypatch.setattr(
        "personas.get_persona_paths",
        lambda _persona_id: {
            "memory": profile_root / "memory",
            "skills": profile_root / "skills",
        },
    )
    monkeypatch.setattr(
        "personas.capabilities.build_capability_scoped_env",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "personas.capabilities.resolve_skill_allowlist",
        lambda _persona_id: [],
    )
    monkeypatch.setattr(
        "runtime.bootstrap.build_session_start_context",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr("cognition.skills.build_skill_index", lambda *_args, **_kwargs: "")

    async def no_recall(**_kwargs):
        return RecallResponse(results=[], formatted_text="", log=_FallbackLog())

    monkeypatch.setattr("recall_service.recall", no_recall)

    async def fake_runtime(request):
        assert request.tool_dispatch is not None
        names = {row["function"]["name"] for row in request.tool_defs}
        assert names == set(persona_tools.PERSONA_CHAT_BASE_TOOLS)
        tool_result = json.loads(
            request.tool_dispatch(
                "request_tool",
                {
                    "tool": "extra_read",
                    "reason": "Need the build receipt.",
                    "arguments": {"receipt": "build-7"},
                },
            )
        )
        assert tool_result["status"] == "approval_required"
        return RuntimeResult(
            text="I need one-time access to inspect that receipt.",
            runtime_lane="generic_runtime",
            provider="openai-codex",
            model="test",
            session_id="session-1",
            tool_call_count=1,
            tool_names_used=["request_tool"],
            tool_calls=[
                RuntimeToolCall(
                    id="call-1",
                    name="request_tool",
                    arguments={"tool": "extra_read"},
                    provider_type="caller_tool",
                    status="completed",
                )
            ],
        )

    monkeypatch.setattr("runtime.lane_router.run_with_runtime_lanes", fake_runtime)
    channel_id = "222222222222222222"
    incoming = IncomingMessage(
        text="Inspect the build receipt.",
        user=User(Platform.DISCORD, "operator-1", "Operator"),
        channel=Channel(Platform.DISCORD, channel_id, name="dev-youtube"),
        platform=Platform.DISCORD,
        thread=Thread(channel_id),
        platform_message_id="discord-message-1",
        raw_event={"guild": "guild-1"},
    )
    binding = SimpleNamespace(
        persona_id="ai-engineer",
        name="dev-youtube",
    )
    outgoing = await run_discord_persona_channel_turn(
        incoming=incoming,
        binding=binding,
        session_store=get_session_store(tmp_path / "sessions.db"),
        project_root=tmp_path,
    )
    assert "Capability request" in outgoing.text
    assert "Exact arguments" in outgoing.text
    assert [component.label for component in outgoing.components] == [
        "Approve once",
        "Deny",
    ]
    pending = persona_elevation.pending_request_for_turn(
        "ai-engineer", "discord-message-1"
    )
    assert pending is not None
    assert pending.tool_name == "extra_read"
