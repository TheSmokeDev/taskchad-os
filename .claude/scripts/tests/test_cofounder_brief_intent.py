"""Co-founder autonomy T4 — read-only `/cofounder brief` + prefetch-only intent.

Path map (one test per distinct path, negative path mandatory):

  Renderer (cofounder/brief.py)
  - composite: agenda lines + in-flight counts + last-24h outcomes + echo
  - the 24h window excludes older ledger rows
  - `approved_by=cofounder-autopilot` renders as "self-assigned"
  - the in-flight persona set is derived from `sent` rows ONLY (a refused
    persona is never probed)
  - fail-open: no agenda + no ledger -> text, never a raise
  - fail-open: mailbox construction failure -> brief minus the in-flight block
  - truncation keeps the trailing command echo
  - REAL delegation (real services + real ledger) round-trips into the brief

  Handler (chat/core_handlers.py)
  - `/cofounder brief` routes to the renderer
  - the intent shape (args="" + collect_only=True) returns the BRIEF
  - a bare typed `/cofounder` still returns the usage menu (regression pin)

  Intent wiring (chat/commands.py + chat/router.py)
  - registered with included_in_brief=True and listed prefetch-only
  - the portfolio phrases detect; a broad query includes it
  - natural language rides prefetched_context and reaches the engine
  - NEGATIVE: no natural-language phrase — including one that literally names
    a mutation — can dispatch anything but the read-only brief form
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_SCRIPTS.parent / "chat"))

import commands  # type: ignore[import-not-found]  # noqa: E402
import core_handlers  # type: ignore[import-not-found]  # noqa: E402
import router as router_mod  # type: ignore[import-not-found]  # noqa: E402

import config  # noqa: E402
from cofounder import brief as brief_mod  # noqa: E402
from cofounder import delegate as delegate_mod  # noqa: E402
from cofounder import project_model  # noqa: E402
from extension_manager import ExtensionManager  # noqa: E402
from models import Channel, IncomingMessage, OutgoingMessage, Platform, User  # noqa: E402
from orchestration.convoy_service import ConvoyService  # noqa: E402
from orchestration.db import OrchestrationDB  # noqa: E402
from orchestration.mailbox_service import MailboxService  # noqa: E402
from router import ChatRouter  # noqa: E402

TODAY = "2026-07-05"
NOW_UTC = datetime(2026, 7, 5, 18, 0, tzinfo=UTC)
NOW_LOCAL = datetime(2026, 7, 5, 10, 0)


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in (
        "HOMIE_KILLSWITCH_COFOUNDER_DELEGATION",
        "HOMIE_KILLSWITCH_COFOUNDER",
        "COFOUNDER_DELEGATION_ENABLED",
        "COFOUNDER_PROJECTS_DIR",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def projects_dir(tmp_path, monkeypatch):
    """A tmp projects dir wired through the COFOUNDER_PROJECTS_DIR knob.

    The renderer resolves settings at call time (Rule 1), so the env var is
    enough — no reload.
    """
    path = tmp_path / "cofounder"
    (path / "agendas").mkdir(parents=True)
    monkeypatch.setenv("COFOUNDER_PROJECTS_DIR", str(path))
    return path


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """Route the delegation ledger into tmp (never the real DATA_DIR)."""
    path = tmp_path / "delegation-audit.jsonl"
    monkeypatch.setattr(
        delegate_mod,
        "_resolve_audit_path",
        lambda audit_path=None: Path(audit_path) if audit_path else path,
    )
    return path


@pytest.fixture
def homie_root(tmp_path, monkeypatch):
    root = tmp_path / ".homie"
    monkeypatch.setenv("HOMIE_HOME", str(root))
    return root


@pytest.fixture
def services():
    db = OrchestrationDB(":memory:")
    return ConvoyService(db), MailboxService(db)


class _FakeMailbox:
    def __init__(self, inboxes: dict[str, list]) -> None:
        self.inboxes = inboxes
        self.calls: list[tuple[str, str | None]] = []

    def get_inbox(self, agent_id, msg_type=None, **_kw):
        self.calls.append((agent_id, msg_type))
        return self.inboxes.get(agent_id, [])


def _fake_services(inboxes: dict[str, list]) -> tuple[object, _FakeMailbox]:
    return object(), _FakeMailbox(inboxes)


def _write_agenda(projects_dir: Path, items: list[dict], day: str = TODAY) -> Path:
    path = projects_dir / "agendas" / f"AGENDA-{day}.json"
    path.write_text(
        json.dumps({"date": day, "summary": "s", "items": items}), encoding="utf-8"
    )
    return path


def _item(n=1, persona="sales", repo="YourProduct", task="close the leads", **kw):
    base = {
        "n": n,
        "persona": persona,
        "repo": repo,
        "task": task,
        "why": "w",
        "priority": 1,
        "status": "proposed",
    }
    base.update(kw)
    return base


def _row(outcome="sent", persona="sales", line=1, hours_ago=1, **kw):
    row = {
        "timestamp": (NOW_UTC - timedelta(hours=hours_ago)).isoformat(
            timespec="seconds"
        ),
        "local_date": TODAY,
        "integration": "cofounder",
        "action": "delegate",
        "persona": persona,
        "line": line,
        "outcome": outcome,
        "detail": "",
        "convoy_id": None,
        "message_id": None,
        "approved_by": "operator",
    }
    row.update(kw)
    return row


def _write_ledger(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


def _grant_persona(homie_root: Path, persona_id: str, repos: list[str]) -> None:
    profile_root = homie_root / "profiles" / persona_id
    (profile_root / "state").mkdir(parents=True, exist_ok=True)
    (profile_root / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "persona": {"id": persona_id, "display_name": persona_id.title()},
                "delegation": {"repos": repos},
            }
        ),
        encoding="utf-8",
    )


def _render(**kwargs) -> str:
    kwargs.setdefault("date", TODAY)
    kwargs.setdefault("now", NOW_UTC)
    return brief_mod.render_cofounder_brief(**kwargs)


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def test_brief_composites_agenda_inflight_and_recent_outcomes(projects_dir, ledger):
    _write_agenda(projects_dir, [_item(n=1), _item(n=2, status="delegated")])
    _write_ledger(ledger, [_row(line=1), _row(line=2, outcome="capped", hours_ago=2)])
    _convoy, mailbox = _fake_services({"sales": ["delivery-1"]})

    text = _render(services=(_convoy, mailbox))

    assert "*Co-Founder brief — 2026-07-05*" in text
    assert "1. [P1|draft] sales -> YourProduct: close the leads" in text
    assert "*In flight* — 1 un-acked assignment(s):" in text
    assert "  sales — 1" in text
    assert "*Last 24h* — 1 capped, 1 sent:" in text
    assert "/cofounder run <n>" in text
    # The in-flight read must ask for the typed assignment message only.
    assert mailbox.calls == [("sales", delegate_mod.MSG_TYPE)]


def test_brief_excludes_outcomes_older_than_24h(projects_dir, ledger):
    _write_ledger(
        ledger,
        [_row(line=1, hours_ago=2), _row(line=9, outcome="capped", hours_ago=30)],
    )

    text = _render(services=_fake_services({}))

    assert "*Last 24h* — 1 sent:" in text
    assert "capped" not in text
    assert "line 9" not in text


def test_brief_marks_autopilot_rows_self_assigned(projects_dir, ledger):
    _write_ledger(
        ledger,
        [
            _row(line=1, approved_by="cofounder-autopilot"),
            _row(line=2, persona="marketing", approved_by="operator"),
        ],
    )

    text = _render(services=_fake_services({}))

    assert "line 1 -> sales (self-assigned): sent" in text
    assert "line 2 -> marketing: sent" in text


def test_brief_probes_inflight_only_for_personas_ever_sent(projects_dir, ledger):
    """The persona set is derived from `sent` ledger rows — the exact superset
    of who can hold an assignment. A refused persona is never probed."""
    _write_ledger(
        ledger,
        [_row(persona="sales"), _row(persona="ops", outcome="scope-denied", line=2)],
    )
    _convoy, mailbox = _fake_services({"sales": ["d1"], "ops": ["d2"]})

    text = _render(services=(_convoy, mailbox))

    assert [agent for agent, _msg_type in mailbox.calls] == ["sales"]
    assert "ops" not in text.split("*Last 24h*")[0]


def test_brief_fails_open_without_agenda_or_ledger(projects_dir, ledger):
    text = _render(services=_fake_services({}))

    assert "No agenda for 2026-07-05" in text
    assert "*Last 24h* — no delegation attempts." in text
    assert "Mutations stay typed" in text


def test_brief_survives_mailbox_construction_failure(projects_dir, ledger, monkeypatch):
    _write_ledger(ledger, [_row()])

    def broken():
        raise RuntimeError("orchestration db gone")

    monkeypatch.setattr(delegate_mod, "_build_services", broken)

    text = _render()

    assert "In flight" not in text
    assert "*Last 24h* — 1 sent:" in text
    assert "Mutations stay typed" in text


def test_brief_truncation_preserves_command_echo(projects_dir, ledger):
    _write_agenda(projects_dir, [_item(n=n, task="x" * 60) for n in range(1, 8)])

    text = _render(services=_fake_services({}), max_chars=200)

    assert "[brief truncated]" in text
    assert "Mutations stay typed" in text


def test_brief_reflects_a_real_delegation(
    projects_dir, ledger, homie_root, services
):
    """End-to-end against real services + the real ledger writer: one approved
    line becomes a delegated agenda marker, an in-flight assignment, and a
    `sent` outcome row — all three read back through the brief."""
    _grant_persona(homie_root, "sales", ["YourProduct"])
    _write_agenda(projects_dir, [_item(n=1)])

    result = delegate_mod.run_agenda_line(
        1,
        date=TODAY,
        approved_by="cofounder-autopilot",
        settings=config.get_cofounder_settings(projects_dir=projects_dir),
        services=services,
        now=NOW_LOCAL,
    )
    assert result.outcome == delegate_mod.OUTCOME_SENT

    text = _render(services=services)

    assert "⏳ 1." in text
    assert "*In flight* — 1 un-acked assignment(s):" in text
    assert "  sales — 1" in text
    assert "line 1 -> sales (self-assigned): sent" in text


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def _handle(args: str, *, collect_only: bool = False) -> str:
    incoming = SimpleNamespace(chat_id=42, user_id="operator")
    return await core_handlers.handle_cofounder(
        object(), incoming, args, collect_only=collect_only
    )


@pytest.mark.asyncio
async def test_brief_subcommand_routes_to_renderer(projects_dir, monkeypatch):
    seen: list[str | None] = []

    def fake(*, date=None, **_kw):
        seen.append(date)
        return "BRIEF BODY"

    monkeypatch.setattr(brief_mod, "render_cofounder_brief", fake)

    assert await _handle(f"brief {TODAY}") == "BRIEF BODY"
    assert seen == [TODAY]


@pytest.mark.asyncio
async def test_intent_shape_dispatch_returns_brief_not_usage(projects_dir, monkeypatch):
    """args="" + collect_only=True is the router's intent shape — a data
    request, which must resolve to the brief instead of the usage menu."""
    monkeypatch.setattr(
        brief_mod, "render_cofounder_brief", lambda **_kw: "BRIEF BODY"
    )

    out = await _handle("", collect_only=True)

    assert out == "BRIEF BODY"
    assert "/cofounder approve" not in out


@pytest.mark.asyncio
async def test_bare_slash_command_still_returns_usage(projects_dir):
    out = await _handle("")

    assert "/cofounder brief" in out
    assert "/cofounder approve" in out


# ---------------------------------------------------------------------------
# Intent wiring
# ---------------------------------------------------------------------------


def _cofounder_intents() -> list[tuple[list[str], str, bool]]:
    return [(kws, c, b) for kws, c, b in commands.CORE_INTENTS if c == "cofounder"]


def test_cofounder_intent_registered_and_included_in_brief():
    rows = _cofounder_intents()
    assert len(rows) == 1
    keywords, _command, included = rows[0]
    assert included is True
    for phrase in ("what are we building", "project status", "portfolio"):
        assert phrase in keywords


def test_cofounder_intent_is_prefetch_only():
    assert "cofounder" in router_mod.PREFETCH_ONLY_INTENTS


@pytest.mark.parametrize(
    "text",
    [
        "what are we building?",
        "give me the project status",
        "how's the portfolio",
        "what's the cofounder doing",
    ],
)
def test_portfolio_phrases_detect_the_cofounder_intent(text):
    manager = ExtensionManager()
    manager.register_core_intents(commands.CORE_INTENTS)

    assert "cofounder" in manager.detect_intents(text)


def test_broad_query_includes_cofounder():
    manager = ExtensionManager()
    manager.register_core_intents(commands.CORE_INTENTS)

    assert "cofounder" in manager.detect_intents("how are we looking")


# ---------------------------------------------------------------------------
# Router — prefetch-only path and the negative (no NL mutation) path
# ---------------------------------------------------------------------------


class _RecordingAdapter:
    platform = Platform.CLI

    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []
        self.updates: list[OutgoingMessage] = []

    async def send(self, message: OutgoingMessage) -> str:
        self.sent.append(message)
        return "placeholder-1"

    async def update(self, message: OutgoingMessage) -> str:
        self.updates.append(message)
        return "updated-1"


class _RecordingEngine:
    session_store = None

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.prefetched_contexts: list[str] = []

    async def handle_message(self, incoming: IncomingMessage, progress=None):
        self.messages.append(incoming.text)
        self.prefetched_contexts.append(getattr(incoming, "prefetched_context", ""))
        yield OutgoingMessage(
            text="engine handled",
            channel=incoming.channel,
            thread=incoming.thread,
        )


def _incoming(text: str) -> IncomingMessage:
    return IncomingMessage(
        text=text,
        user=User(Platform.CLI, "cli-user", "Tester"),
        channel=Channel(Platform.CLI, "cli-test", is_dm=True),
        platform=Platform.CLI,
    )


def _build_manager() -> ExtensionManager:
    manager = ExtensionManager()
    manager.register_core_commands(commands.COMMANDS, [], core_handlers.CORE_HANDLERS)
    manager.register_core_intents(commands.CORE_INTENTS)
    return manager


@pytest.mark.asyncio
async def test_natural_language_prefetches_and_lets_the_engine_answer():
    dispatched: list[tuple[str, bool]] = []

    async def fake_cofounder(adapter, incoming, args, *, collect_only=False):
        dispatched.append((args, collect_only))
        return "PORTFOLIO STATE"

    manager = _build_manager()
    manager._commands["cofounder"].handler = fake_cofounder
    engine = _RecordingEngine()
    router = ChatRouter(engine, manager)
    adapter = _RecordingAdapter()

    await router._handle_inner(adapter, _incoming("what are we building?"))

    assert dispatched == [("", True)]
    assert engine.messages == ["what are we building?"]
    assert engine.prefetched_contexts == ["## /cofounder\nPORTFOLIO STATE"]
    # Prefetch-only: the raw data is never posted back as the reply.
    assert all("PORTFOLIO STATE" not in m.text for m in adapter.sent)


@pytest.mark.parametrize(
    "text",
    [
        "what are we building — run agenda line 2",
        "project status, then pause alpha",
        "portfolio: steer alpha to finish the audit",
        "what's the cofounder doing, approve alpha",
    ],
)
@pytest.mark.asyncio
async def test_no_natural_language_phrase_can_reach_a_mutating_subcommand(
    text, projects_dir, ledger, monkeypatch
):
    """NEGATIVE PATH: the intent dispatch is pinned to args="" — even a phrase
    that literally names run/pause/steer/approve reaches only the read-only
    brief. Runs the REAL handler with every mutator wired to a recorder."""
    mutations: list[str] = []
    monkeypatch.setattr(
        delegate_mod,
        "run_agenda_line",
        lambda *a, **k: mutations.append("run_agenda_line"),
    )
    for name in ("update_frontmatter", "append_activity_log", "archive_to_done"):
        monkeypatch.setattr(
            project_model, name, lambda *a, _n=name, **k: mutations.append(_n)
        )

    manager = _build_manager()
    dispatched: list[tuple[str, str, bool]] = []
    original_dispatch = manager.dispatch

    async def spy_dispatch(command, adapter, incoming, args, *, collect_only=False):
        dispatched.append((command, args, collect_only))
        return await original_dispatch(
            command, adapter, incoming, args, collect_only=collect_only
        )

    manager.dispatch = spy_dispatch  # type: ignore[method-assign]
    engine = _RecordingEngine()
    router = ChatRouter(engine, manager)

    await router._handle_inner(_RecordingAdapter(), _incoming(text))

    assert mutations == []
    assert dispatched, "the portfolio intent should still have been detected"
    for command, args, collect_only in dispatched:
        assert (args, collect_only) == ("", True), (
            f"/{command} was dispatched with args={args!r} from natural language"
        )
    # The one reply that did reach the engine is the read-only brief.
    assert engine.prefetched_contexts
    assert "Mutations stay typed" in engine.prefetched_contexts[0]
