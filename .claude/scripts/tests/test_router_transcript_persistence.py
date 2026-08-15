from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from models import Channel, IncomingMessage, Platform, User
from router import ChatRouter
from session import Session, SQLiteSessionStore


class _RecordingAdapter:
    platform = Platform.CLI

    def __init__(self) -> None:
        self.sent = []

    async def send(self, message):
        self.sent.append(message)
        return None

    async def update(self, message):
        self.sent.append(message)


class _RouterOnlyManager:
    command_regex = re.compile(r"^/(\w+)\b(.*)$")

    def __init__(self):
        self.dispatched = []

    def get_router_commands(self):
        return {
            "status",
            "clear",
            "model",
            "provider",
            "teamroom",
            "email",
            "gsc",
            "analytics",
            "design",
            "vault",
        }

    def get_all_command_names(self):
        return [
            "status",
            "clear",
            "model",
            "provider",
            "teamroom",
            "email",
            "gsc",
            "analytics",
            "design",
            "vault",
        ]

    async def dispatch(self, command, adapter, incoming, args, collect_only=False):
        self.dispatched.append((command, args, collect_only))
        if command == "clear":
            return "Session cleared. Next message starts fresh."
        if command == "status":
            return "Session Status"
        if command == "model":
            return "Switched runtime"
        if command == "provider":
            return "Runtime Provider Status"
        if command == "teamroom":
            return "Team Room Workflow"
        if command == "email":
            return f"Email Search: {args}".strip()
        if command == "gsc":
            return f"GSC Stats: {args}".strip()
        if command == "analytics":
            return f"Analytics Stats: {args}".strip()
        if command == "design":
            return f"Design Command: {args}".strip()
        return None

    def detect_intents(self, text):
        return []

    def wants_analysis(self, text):
        return False


class _FakeEngine:
    def __init__(self, store):
        self.session_store = store
        self.messages = []

    async def handle_message(self, message, progress=None):
        self.messages.append(message)
        if False:
            yield None


@pytest.mark.asyncio
async def test_router_command_persists_transcript_turn(tmp_path):
    store = SQLiteSessionStore(tmp_path / "chat.db")
    router = ChatRouter(_FakeEngine(store), _RouterOnlyManager())
    adapter = _RecordingAdapter()

    incoming = IncomingMessage(
        text="/status",
        user=User(Platform.CLI, "cli-user", "User"),
        channel=Channel(Platform.CLI, "cli-test", is_dm=True),
        platform=Platform.CLI,
        timestamp=datetime.now(),
    )

    await router._handle(adapter, incoming)

    session = store.get("cli", "cli-test", "cli-test")
    assert session is not None
    assert session.message_count == 1
    messages = store.list_messages("cli:cli-test:cli-test")
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[0].content == "/status"
    assert messages[1].content == "Session Status"


@pytest.mark.asyncio
async def test_clear_command_does_not_recreate_transcript_rows(tmp_path):
    store = SQLiteSessionStore(tmp_path / "chat.db")
    router = ChatRouter(_FakeEngine(store), _RouterOnlyManager())
    adapter = _RecordingAdapter()

    incoming = IncomingMessage(
        text="/clear",
        user=User(Platform.CLI, "cli-user", "User"),
        channel=Channel(Platform.CLI, "cli-test", is_dm=True),
        platform=Platform.CLI,
        timestamp=datetime.now(),
    )

    await router._handle(adapter, incoming)

    assert store.get("cli", "cli-test", "cli-test") is None
    assert store.list_messages("cli:cli-test:cli-test") == []


@pytest.mark.asyncio
async def test_model_command_persists_current_codex_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_LANE", "generic_runtime")
    monkeypatch.setenv("SECOND_BRAIN_GENERIC_PROVIDER", "openai-codex")
    monkeypatch.setenv("SECOND_BRAIN_CODEX_MODEL", "gpt-5.5")
    store = SQLiteSessionStore(tmp_path / "chat.db")
    router = ChatRouter(_FakeEngine(store), _RouterOnlyManager())
    adapter = _RecordingAdapter()

    incoming = IncomingMessage(
        text="/model codex",
        user=User(Platform.CLI, "cli-user", "User"),
        channel=Channel(Platform.CLI, "cli-test", is_dm=True),
        platform=Platform.CLI,
        timestamp=datetime.now(),
    )

    await router._handle(adapter, incoming)

    session = store.get("cli", "cli-test", "cli-test")
    assert session is not None
    assert session.runtime_lane == "generic_runtime"
    assert session.runtime_provider == "openai-codex"
    assert session.runtime_model == "gpt-5.5"
    assert session.runtime_session_id == ""


@pytest.mark.asyncio
async def test_provider_command_persists_current_claude_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_LANE", "claude_native")
    monkeypatch.delenv("SECOND_BRAIN_GENERIC_PROVIDER", raising=False)
    monkeypatch.setenv("SECOND_BRAIN_CLAUDE_MODEL", "claude-opus-4-6")
    store = SQLiteSessionStore(tmp_path / "chat.db")
    router = ChatRouter(_FakeEngine(store), _RouterOnlyManager())
    adapter = _RecordingAdapter()

    incoming = IncomingMessage(
        text="/provider",
        user=User(Platform.CLI, "cli-user", "User"),
        channel=Channel(Platform.CLI, "cli-test", is_dm=True),
        platform=Platform.CLI,
        timestamp=datetime.now(),
    )

    await router._handle(adapter, incoming)

    session = store.get("cli", "cli-test", "cli-test")
    assert session is not None
    assert session.runtime_lane == "claude_native"
    assert session.runtime_provider == "claude"
    assert session.runtime_model == "claude-opus-4-6"


@pytest.mark.asyncio
async def test_teamroom_runtime_command_persists_requested_runtime_lane(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    monkeypatch.setenv("SECOND_BRAIN_RUNTIME_LANE", "claude_native")
    monkeypatch.setenv("SECOND_BRAIN_GENERIC_PROVIDER", "openai-codex")
    monkeypatch.setenv("SECOND_BRAIN_CODEX_MODEL", "gpt-5.5")
    store = SQLiteSessionStore(tmp_path / "chat.db")
    router = ChatRouter(_FakeEngine(store), _RouterOnlyManager())
    adapter = _RecordingAdapter()

    incoming = IncomingMessage(
        text=(
            "/teamroom --allow-live-agent-run --runtime --lane generic_runtime "
            "How should the team prioritize the next release?"
        ),
        user=User(Platform.CLI, "cli-user", "User"),
        channel=Channel(Platform.CLI, "cli-test", is_dm=True),
        platform=Platform.CLI,
        timestamp=datetime.now(),
    )

    await router._handle(adapter, incoming)

    session = store.get("cli", "cli-test", "cli-test")
    assert session is not None
    assert session.runtime_lane == "generic_runtime"
    assert session.runtime_provider == "auto"
    assert session.runtime_model == ""
    assert session.runtime_session_id == ""


@pytest.mark.asyncio
async def test_pasted_example-project_handoff_does_not_dispatch_embedded_slash_commands(tmp_path):
    store = SQLiteSessionStore(tmp_path / "chat.db")
    engine = _FakeEngine(store)
    manager = _RouterOnlyManager()
    router = ChatRouter(engine, manager)
    adapter = _RecordingAdapter()
    text = """SSH into my server and pull both.

We built a Pay Low mock here:
/opt/YourBusiness-homie/docs/example-project-enterprise-redesign/index.html

Related docs:
docs/example-project-enterprise-redesign/DESIGN.md
docs/research/2026-06-23-example-project-YourProduct-plan-of-attack.md

Generic agency copy used placeholder phone/email/locations and no Pay Low leftovers.
"""

    await router._handle(adapter, _cli_incoming(text))

    assert manager.dispatched == []
    assert engine.messages
    assert engine.messages[0].text == text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "docs/example-project-enterprise-redesign/DESIGN.md",
        "phone/email/locations",
        "https://site.com/email",
        "docs/foo/vault.md",
        "https://site.com/vault",
        "/opt/YourBusiness-homie/docs/example-project-enterprise-redesign/index.html",
        "/design.md",
        "/email/locations",
        "/vault.md",
        "/vault/path",
    ],
)
async def test_command_like_paths_and_urls_fall_through_to_engine(tmp_path, text: str):
    store = SQLiteSessionStore(tmp_path / "chat.db")
    engine = _FakeEngine(store)
    manager = _RouterOnlyManager()
    router = ChatRouter(engine, manager)
    adapter = _RecordingAdapter()

    await router._handle(adapter, _cli_incoming(text))

    assert manager.dispatched == []
    assert engine.messages
    assert engine.messages[0].text == text


@pytest.mark.asyncio
async def test_explicit_multi_command_chain_still_dispatches(tmp_path):
    store = SQLiteSessionStore(tmp_path / "chat.db")
    engine = _FakeEngine(store)
    manager = _RouterOnlyManager()
    router = ChatRouter(engine, manager)
    adapter = _RecordingAdapter()

    await router._handle(adapter, _cli_incoming("/email /gsc /analytics"))

    assert [cmd for cmd, _args, _collect in manager.dispatched] == [
        "email",
        "gsc",
        "analytics",
    ]
    assert all(collect for _cmd, _args, collect in manager.dispatched)
    assert engine.messages == []


@pytest.mark.asyncio
async def test_explicit_design_command_still_dispatches(tmp_path):
    store = SQLiteSessionStore(tmp_path / "chat.db")
    engine = _FakeEngine(store)
    manager = _RouterOnlyManager()
    router = ChatRouter(engine, manager)
    adapter = _RecordingAdapter()

    await router._handle(adapter, _cli_incoming("/design html build a page"))

    assert manager.dispatched == [("design", "html build a page", False)]
    assert engine.messages == []


@pytest.mark.asyncio
async def test_vault_search_calls_recall_with_selected_vault(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import config
    import recall_service
    from recall_service import SearchMode

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(
        config,
        "resolve_vault",
        lambda name: (vault_dir, db_path),
    )
    fake_result = SimpleNamespace(
        path="MEMORY.md",
        start_line=7,
        end_line=9,
        section_title="YourProduct",
        text="YourProduct prospect demo URLs",
    )
    fake_recall = AsyncMock(
        return_value=SimpleNamespace(results=[fake_result], log=SimpleNamespace())
    )
    monkeypatch.setattr(recall_service, "recall", fake_recall)

    store = SQLiteSessionStore(tmp_path / "chat.db")
    router = ChatRouter(_FakeEngine(store), _RouterOnlyManager())
    adapter = _RecordingAdapter()

    await router._handle(adapter, _cli_incoming("/vault search YourProduct --vault thehomie"))

    fake_recall.assert_awaited_once()
    kwargs = fake_recall.await_args.kwargs
    assert kwargs["memory_dir"] == vault_dir
    assert kwargs["search_mode"] is SearchMode.HYBRID
    assert kwargs["caller"] == "vault-command:search"
    assert kwargs["is_slash_command"] is False
    assert "YourProduct prospect demo URLs" in adapter.sent[-1].text


@pytest.mark.asyncio
async def test_vault_db_resolves_selected_vault(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import config

    vault_dir = tmp_path / "coding"
    vault_dir.mkdir()
    db_path = tmp_path / "memory.coding-vault.db"
    db_path.write_text("sqlite-ish", encoding="utf-8")
    monkeypatch.setattr(config, "resolve_vault", lambda name: (vault_dir, db_path))

    store = SQLiteSessionStore(tmp_path / "chat.db")
    router = ChatRouter(_FakeEngine(store), _RouterOnlyManager())
    adapter = _RecordingAdapter()

    await router._handle(adapter, _cli_incoming("/vault db coding-vault"))

    reply = adapter.sent[-1].text
    assert "*Vault DB*" in reply
    assert "coding-vault" in reply
    assert str(vault_dir) in reply
    assert str(db_path) in reply


@pytest.mark.asyncio
async def test_vault_contacts_uses_contact_scoped_recall(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import config
    import recall_service

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    monkeypatch.setattr(config, "resolve_vault", lambda name: (vault_dir, tmp_path / "db.sqlite"))
    fake_recall = AsyncMock(return_value=SimpleNamespace(results=[], log=SimpleNamespace()))
    monkeypatch.setattr(recall_service, "recall", fake_recall)

    store = SQLiteSessionStore(tmp_path / "chat.db")
    router = ChatRouter(_FakeEngine(store), _RouterOnlyManager())
    adapter = _RecordingAdapter()

    await router._handle(adapter, _cli_incoming("/vault contacts Haris"))

    query = fake_recall.await_args.kwargs["query"]
    assert "contacts people clients prospects owners phone email Haris" == query


@pytest.mark.asyncio
async def test_vault_ops_becomes_vault_ops_skill_prompt(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "chat.db")
    engine = _FakeEngine(store)
    router = ChatRouter(engine, _RouterOnlyManager())
    adapter = _RecordingAdapter()

    await router._handle(adapter, _cli_incoming("/vault ops context YourProduct"))

    assert engine.messages
    prompt = engine.messages[0].text
    assert "Use the Skill tool to invoke the 'vault-ops' skill" in prompt
    assert "Command: context" in prompt
    assert "Arguments: YourProduct" in prompt
    assert "Selected vault: thehomie" in prompt


@pytest.mark.asyncio
async def test_router_connects_other_adapters_when_one_connect_hangs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHAT_ADAPTER_CONNECT_TIMEOUT_SECONDS", "0.01")

    class HangingAdapter:
        platform = Platform.TELEGRAM
        disconnected = False

        async def connect(self):
            await asyncio.Event().wait()

        async def disconnect(self):
            self.disconnected = True

        async def listen(self):
            if False:
                yield None

    class ConnectedAdapter:
        platform = Platform.DISCORD

        def __init__(self):
            self.connected = asyncio.Event()

        async def connect(self):
            self.connected.set()

        async def disconnect(self):
            return None

        async def listen(self):
            await asyncio.Event().wait()
            if False:
                yield None

    store = SQLiteSessionStore(tmp_path / "chat.db")
    router = ChatRouter(_FakeEngine(store), _RouterOnlyManager())
    hanging = HangingAdapter()
    connected = ConnectedAdapter()
    router.register(hanging)
    router.register(connected)

    task = asyncio.create_task(router.run())
    await asyncio.wait_for(connected.connected.wait(), timeout=2)
    await asyncio.sleep(1.1)

    assert Platform.TELEGRAM not in router.adapters
    assert Platform.DISCORD in router.adapters
    assert hanging.disconnected is True

    task.cancel()
    await task


# =============================================================================
# Living Mind Act 4 (R1 B4) — router pre-persist marker seam + exactly-once
# brief consumption. Real ConversationEngine + real builder against tmp
# STATE_DIR / vault (config attrs monkeypatched — resolved at call time,
# Rule 1 in action). Zero live reads or writes.
# =============================================================================

_WM_FRESH_TEMPLATE = """---
tags: [system, memory, working]
status: current
date: {date}
summary: "test"
---

# WORKING.md

## Open Threads

## Active Hypotheses

## Unresolved Questions

## Heartbeat Observations

{observations}

## Archived (Cold)
"""


def _act4_env(monkeypatch, tmp_path, *, fresh_vault: bool = True):
    """Point config STATE_DIR / MEMORY_DIR / ledger at tmp paths."""
    import config

    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    observations = (
        f"- [{today}] [calendar] busy day: 5 events" if fresh_vault
        else "- [2026-01-01] [calendar] stale observation"
    )
    (vault / "WORKING.md").write_text(
        _WM_FRESH_TEMPLATE.format(date=today, observations=observations),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    monkeypatch.setattr(config, "MEMORY_DIR", vault)
    monkeypatch.setattr(
        config, "AMENDMENT_LEDGER_FILE", state_dir / "amendment-proposals.jsonl"
    )
    for var in (
        "SESSION_BRIEF_ENABLED",
        "SESSION_BRIEF_AWAY_HOURS",
        "SESSION_BRIEF_MIN_FRESH_ITEMS",
        "SESSION_BRIEF_MAX_PER_SECTION",
        "SESSION_BRIEF_MAX_CHARS",
    ):
        monkeypatch.delenv(var, raising=False)
    return state_dir, vault


def _real_engine(monkeypatch, tmp_path, store, captured):
    import engine as engine_module
    from engine import ConversationEngine

    from runtime.base import RuntimeResult

    project_root = tmp_path / "project"
    (project_root / "TheHomie" / "Memory" / "daily").mkdir(
        parents=True, exist_ok=True
    )
    convo = ConversationEngine(store, project_root)

    async def fake_run(request):
        captured.append(request)
        return RuntimeResult(
            text="ok",
            runtime_lane="generic_runtime",
            provider="openai-codex",
            model="gpt-5.5",
            profile_key="primary-openai-codex",
        )

    monkeypatch.setattr(engine_module, "run_with_runtime_lanes", fake_run)
    return convo


def _seed_old_interactive(store, *, hours_ago: float = 10.0) -> datetime:
    old = datetime.now() - timedelta(hours=hours_ago)
    store.create(
        Session(
            session_id="cli:cli-test:cli-test",
            agent_session_id="",
            platform="cli",
            channel_id="cli-test",
            thread_id="cli-test",
            user_id="1111111111",
            created_at=old,
            updated_at=old,
            message_count=1,
        )
    )
    return old


def _cli_incoming(text: str, *, source: str = "interactive") -> IncomingMessage:
    # Mirror the role the REAL CLI adapter stamps at ingress rather than the
    # dataclass default: that default is deliberately fail-closed (`viewer`) and
    # models an UNSTAMPED surface, while `thehomie chat` is the operator's own
    # shell. Reading the adapter's own constant keeps the two from drifting.
    from adapters.cli_adapter import _CLI_INGRESS_ROLE

    return IncomingMessage(
        text=text,
        user=User(Platform.CLI, "cli-user", "User"),
        channel=Channel(Platform.CLI, "cli-test", is_dm=True),
        platform=Platform.CLI,
        timestamp=datetime.now(),
        user_role=_CLI_INGRESS_ROLE,
        source=source,
    )


@pytest.mark.asyncio
async def test_status_first_marker_survives_then_engine_fires_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    """The B4 flow end-to-end: /status after a seeded gap writes the marker
    pre-bump; the next ENGINE turn fires the brief from the marker boundary
    and DELETES it; the turn after gets no brief (exactly-once)."""
    state_dir, _vault = _act4_env(monkeypatch, tmp_path, fresh_vault=True)
    store = SQLiteSessionStore(tmp_path / "chat.db")
    old = _seed_old_interactive(store, hours_ago=10)
    captured: list = []
    convo = _real_engine(monkeypatch, tmp_path, store, captured)
    router = ChatRouter(convo, _RouterOnlyManager())
    adapter = _RecordingAdapter()

    # 1. /status FIRST — marker written with the PRE-bump boundary, then the
    #    router bump closes the physical gap.
    await router._handle(adapter, _cli_incoming("/status"))
    marker = state_dir / "session-brief-owed.json"
    assert marker.exists(), "router-first turn must write the brief-owed marker"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    boundary = datetime.fromisoformat(payload["last_activity"])
    assert abs(boundary - old) < timedelta(seconds=2)
    bumped = store.get("cli", "cli-test", "cli-test")
    assert bumped.updated_at > old  # the bump happened AFTER the marker write

    # 2. First engine turn: fires from the marker boundary and consumes it.
    outputs = [
        out async for out in convo.handle_message(_cli_incoming("good morning"))
    ]
    assert outputs[-1].text == "ok"
    assert "# Session Opening Brief" in captured[0].prompt
    assert "busy day: 5 events" in captured[0].prompt
    assert not marker.exists(), "completed decision must consume the marker"

    # 3. The turn after gets NO brief (exactly-once).
    outputs = [
        out async for out in convo.handle_message(_cli_incoming("and again"))
    ]
    assert outputs[-1].text == "ok"
    assert "# Session Opening Brief" not in captured[1].prompt


@pytest.mark.asyncio
async def test_silent_engine_decision_also_consumes_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    """A boredom-silent morning consumes the debt too — only-on-fire would
    defer the marker into an off-window afternoon fire."""
    state_dir, _vault = _act4_env(monkeypatch, tmp_path, fresh_vault=False)
    store = SQLiteSessionStore(tmp_path / "chat.db")
    _seed_old_interactive(store, hours_ago=10)
    captured: list = []
    convo = _real_engine(monkeypatch, tmp_path, store, captured)
    router = ChatRouter(convo, _RouterOnlyManager())
    adapter = _RecordingAdapter()

    await router._handle(adapter, _cli_incoming("/status"))
    marker = state_dir / "session-brief-owed.json"
    assert marker.exists()

    outputs = [
        out async for out in convo.handle_message(_cli_incoming("good morning"))
    ]
    assert outputs[-1].text == "ok"
    assert "# Session Opening Brief" not in captured[0].prompt  # stale vault
    assert not marker.exists(), "silent decision must also consume the marker"


@pytest.mark.asyncio
async def test_no_gap_no_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    state_dir, _vault = _act4_env(monkeypatch, tmp_path)
    store = SQLiteSessionStore(tmp_path / "chat.db")
    _seed_old_interactive(store, hours_ago=0.1)  # six minutes — no gap
    captured: list = []
    convo = _real_engine(monkeypatch, tmp_path, store, captured)
    router = ChatRouter(convo, _RouterOnlyManager())
    adapter = _RecordingAdapter()

    await router._handle(adapter, _cli_incoming("/status"))
    assert not (state_dir / "session-brief-owed.json").exists()


@pytest.mark.asyncio
async def test_cron_router_turn_writes_no_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    state_dir, _vault = _act4_env(monkeypatch, tmp_path)
    store = SQLiteSessionStore(tmp_path / "chat.db")
    _seed_old_interactive(store, hours_ago=10)
    captured: list = []
    convo = _real_engine(monkeypatch, tmp_path, store, captured)
    router = ChatRouter(convo, _RouterOnlyManager())
    adapter = _RecordingAdapter()

    await router._handle(adapter, _cli_incoming("/status", source="cron"))
    assert not (state_dir / "session-brief-owed.json").exists()


@pytest.mark.asyncio
async def test_builder_exception_leaves_marker_intact_through_engine_turn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    state_dir, _vault = _act4_env(monkeypatch, tmp_path)
    store = SQLiteSessionStore(tmp_path / "chat.db")
    _seed_old_interactive(store, hours_ago=10)
    captured: list = []
    convo = _real_engine(monkeypatch, tmp_path, store, captured)
    router = ChatRouter(convo, _RouterOnlyManager())
    adapter = _RecordingAdapter()

    await router._handle(adapter, _cli_incoming("/status"))
    marker = state_dir / "session-brief-owed.json"
    assert marker.exists()

    import cognition.proactive_brief as pb

    def _boom(memory_dir, **kwargs):
        raise RuntimeError("builder exploded")

    monkeypatch.setattr(pb, "build_session_opening_brief", _boom)
    outputs = [
        out async for out in convo.handle_message(_cli_incoming("good morning"))
    ]
    assert outputs[-1].text == "ok"  # turn completes bare
    assert "# Session Opening Brief" not in captured[0].prompt
    assert marker.exists(), "exceptions must leave the marker intact for retry"
