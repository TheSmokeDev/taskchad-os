"""Gated Archon dispatch — the gate, the F2 brief contract, and correlation.

Nothing here reaches Archon. The HTTP client is substituted at
``talk_archon.archon_client`` (module-attribute lookup, Rule 3), the ledger is
a real temp SQLite file, and the audit trail is a real temp JSONL — assertions
read actual rows and actual lines, never a return value standing in for one.

The operator's live ``~/.archon/archon.db`` is never opened: every codebase
lookup here points at a temp DB built with Archon's own column names.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))

import talk_archon
from integrations import archon_client, capabilities


# ─── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "archon_dispatch.jsonl"


def _audit_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def archon_db(tmp_path: Path) -> Path:
    """A temp ledger with Archon's real codebase + run column names."""

    path = tmp_path / "archon.db"
    connection = sqlite3.connect(path)
    with connection:
        connection.execute(
            "CREATE TABLE remote_agent_codebases "
            "(id TEXT PRIMARY KEY, name TEXT, default_cwd TEXT, kind TEXT)"
        )
        connection.execute(
            "CREATE TABLE remote_agent_workflow_runs "
            "(id TEXT PRIMARY KEY, workflow_name TEXT, status TEXT, working_path TEXT, "
            " started_at TEXT, completed_at TEXT, parent_conversation_id TEXT, "
            " conversation_id TEXT)"
        )
    connection.close()
    return path


GOOD_BRIEF = (
    "Build the YourProduct employee page at /employee with the three-tier pricing "
    "table and a Stripe checkout link; done when it renders on production."
)


class _FakeDispatch:
    """Stand-in for ArchonDispatch with the two ids the correlation key needs."""

    def __init__(self, conversation_id: str, conversation_db_id: str, status: str = "started"):
        self.conversation_id = conversation_id
        self.conversation_db_id = conversation_db_id
        self.dispatched = True
        self.accepted = True
        self.status = status


@pytest.fixture
def spy_client(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Record every dispatch_workflow call; return the recorded list."""

    calls: list[dict] = []

    async def fake_dispatch(codebase_id, workflow, text, *, client=None):
        calls.append({"codebase_id": codebase_id, "workflow": workflow, "text": text})
        return _FakeDispatch("web-1785-abc", "conv-db-1")

    monkeypatch.setattr(archon_client, "dispatch_workflow", fake_dispatch)
    return calls


@pytest.fixture(autouse=True)
def _bound_codebase(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Default binding so gate tests exercise the gate, not path resolution.

    Both audit sinks and the codebase registry are redirected to tmp_path, so
    the operator's live ``~/.archon/archon.db``, ``archon_dispatch.jsonl`` and
    ``dashboard.db`` are never touched.
    """

    monkeypatch.setenv("ARCHON_CODEBASE_ID", "cb-test")
    # BOTH audit sinks are redirected, not just the JSONL: a test that omits
    # `audit_path` would otherwise append a fake refusal to the operational
    # archon_dispatch.jsonl, and the kill-switch layer writes a row into the
    # real dashboard.db underneath (codex R2 major). Operational append-only
    # stores must never carry rows a test invented.
    import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    # DASHBOARD_DB_PATH is bound SEPARATELY at import (config.py:280) — moving
    # DATA_DIR alone leaves the kill-switch layer writing refusal rows into the
    # operational dashboard.db (codex R3 major; second binding missed in R2).
    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", tmp_path / "dashboard.db")
    # The registry the codebase override validates against: seeded so the
    # bound id is REGISTERED (resolution now refuses an unverifiable target),
    # while still never opening the operator's live ~/.archon/archon.db.
    registry = tmp_path / "gate-archon.db"
    connection = sqlite3.connect(registry)
    with connection:
        connection.execute(
            "CREATE TABLE remote_agent_codebases "
            "(id TEXT PRIMARY KEY, name TEXT, default_cwd TEXT, kind TEXT)"
        )
        connection.execute(
            "INSERT INTO remote_agent_codebases (id, name, default_cwd, kind) "
            "VALUES ('cb-test', 'owner/repo', ?, 'repo')",
            # The row must physically point AT this repo: the override
            # disambiguates between registered rows, it cannot redirect.
            (str(Path(talk_archon.__file__).resolve().parents[2]),),
        )
    connection.close()
    monkeypatch.setenv("TALK_ARCHON_DB", str(registry))


# ─── F2: the brief must stand on its own ─────────────────────────────────


@pytest.mark.parametrize(
    "brief",
    [
        "yeah do that",
        "yes, go ahead",
        "do what we discussed",
        "run that thing",
        "same as before",
        "the usual please",
        "go ahead with that",
        "like I said",
        "",
        "   ",
    ],
)
def test_referential_briefs_are_refused(brief: str) -> None:
    reason = talk_archon.brief_refusal_reason(brief)

    assert reason is not None
    assert "never sees this conversation" in reason


@pytest.mark.parametrize(
    "brief",
    [
        GOOD_BRIEF,
        "Audit every YourBusiness fleet brand site for missing canonical tags and "
        "write the findings into the vault.",
        "Migrate the lead attribution pipeline off Supabase onto Postgres, "
        "keeping the existing webhook contract intact.",
    ],
)
def test_self_contained_briefs_pass(brief: str) -> None:
    assert talk_archon.brief_refusal_reason(brief) is None


def test_a_long_brief_of_pure_pointers_still_fails() -> None:
    """Length alone is not self-containment — the content floor is the point."""

    brief = "yeah ok so go ahead and do that thing we discussed, " * 4

    assert talk_archon.brief_refusal_reason(brief) is not None


def test_brief_floors_resolve_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule 1: the floors are read on every call, not bound at import."""

    terse = "Ship the pricing fix"
    assert talk_archon.brief_refusal_reason(terse) is not None

    monkeypatch.setenv("TALK_ARCHON_MIN_BRIEF_CHARS", "5")
    monkeypatch.setenv("TALK_ARCHON_MIN_BRIEF_WORDS", "2")

    assert talk_archon.brief_refusal_reason(terse) is None


def test_garbage_floors_fall_back_to_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fat-fingered .env must not silently disable the F2 gate."""

    monkeypatch.setenv("TALK_ARCHON_MIN_BRIEF_CHARS", "lots")
    monkeypatch.setenv("TALK_ARCHON_MIN_BRIEF_WORDS", "-3")

    assert talk_archon.brief_floors() == (40, 6)
    assert talk_archon.brief_refusal_reason("yeah do that") is not None


def test_zero_floors_are_the_gate_switched_off_and_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero is not a smaller floor (codex R1: (0, 0) let 'yeah do that' through)."""

    monkeypatch.setenv("TALK_ARCHON_MIN_BRIEF_CHARS", "0")
    monkeypatch.setenv("TALK_ARCHON_MIN_BRIEF_WORDS", "0")

    assert talk_archon.brief_floors() == (40, 6)
    assert talk_archon.brief_refusal_reason("yeah do that") is not None


def test_a_vague_brief_never_reaches_the_client(
    spy_client: list[dict], audit_path: Path
) -> None:
    """The F2 lock: 'yeah do that' is refused before any dispatch call."""

    with pytest.raises(talk_archon.ArchonDispatchRefusedError):
        talk_archon.require_dispatch_allowed(
            "archon-clutch", "yeah do that", audit_path=audit_path
        )

    assert spy_client == []
    outcomes = [row["outcome"] for row in _audit_rows(audit_path)]
    assert outcomes == ["refused_vague_brief"]
    assert _audit_rows(audit_path)[0]["brief_preview"] == "yeah do that"


# ─── the gate ────────────────────────────────────────────────────────────


def test_granted_dispatch_audits_and_carries_the_resolution(audit_path: Path) -> None:
    grant = talk_archon.require_dispatch_allowed(
        "archon-clutch", GOOD_BRIEF, audit_path=audit_path
    )

    assert grant.workflow == "archon-clutch"
    assert grant.codebase_id == "cb-test"
    assert grant.brief == GOOD_BRIEF
    rows = _audit_rows(audit_path)
    assert [r["outcome"] for r in rows] == ["granted"]
    granted = rows[0]
    assert granted["codebase_id"] == "cb-test"
    assert granted["brief_preview"].startswith("Build the YourProduct employee page")
    assert granted["integration"] == "archon" and granted["action"] == "dispatch"


def test_kill_switch_refuses_and_audits(
    monkeypatch: pytest.MonkeyPatch, spy_client: list[dict], audit_path: Path
) -> None:
    monkeypatch.setenv("HOMIE_KILLSWITCH_ARCHON_DISPATCH", "disabled")

    from security import kill_switches

    # House contract (codex R3 major): the kill-switch refusal PROPAGATES with
    # its switch name intact so callers can map it structurally — it is not
    # flattened into an ordinary dispatch refusal.
    with pytest.raises(kill_switches.KillSwitchDisabled) as excinfo:
        talk_archon.require_dispatch_allowed(
            "archon-clutch", GOOD_BRIEF, audit_path=audit_path
        )

    assert excinfo.value.switch_name == talk_archon.KILL_SWITCH
    assert spy_client == []
    assert [r["outcome"] for r in _audit_rows(audit_path)] == ["refused_killswitch"]


def test_kill_switch_refusal_is_counted() -> None:
    """House rule: a refusal raises AND increments the operator-visible counter."""

    from security import kill_switches

    before = kill_switches.get_health_snapshot()["counters"].get(talk_archon.KILL_SWITCH, 0)
    import os

    os.environ["HOMIE_KILLSWITCH_ARCHON_DISPATCH"] = "disabled"
    try:
        with pytest.raises(kill_switches.KillSwitchDisabled):
            talk_archon.require_dispatch_allowed("archon-clutch", GOOD_BRIEF)
    finally:
        os.environ.pop("HOMIE_KILLSWITCH_ARCHON_DISPATCH", None)

    after = kill_switches.get_health_snapshot()["counters"].get(talk_archon.KILL_SWITCH, 0)
    assert after == before + 1


def test_the_declared_action_matches_what_this_lane_can_actually_prove() -> None:
    """The declaration is HONEST, asserted against the REAL policy registry.

    Earlier revisions claimed `operator_confirmed` and hid the `model`
    exposure. The Talk lane cannot prove a spoken yes (`talk_api.py` receives
    only a model-authored tool name and argument dict), so claiming one was
    an assertion the code could not back. Under the operator's blast-radius
    rule a dispatch is the free tier — bounded to a worktree and tokens — so
    it is declared model-initiable, and the gate for real spend lives on the
    workflow's own APPROVE SPEND pause nodes.
    """

    declared = capabilities.get_integration_action("archon", "dispatch")

    assert declared is not None
    assert declared.is_mutating
    assert "model" in declared.exposures
    assert capabilities.is_integration_action_allowed(
        "archon", "dispatch", surface="model"
    )
    # The kill switch remains the operator's off-lever for the whole surface.
    assert talk_archon.KILL_SWITCH == "archon_dispatch"


def test_policy_denial_refuses_and_audits(
    monkeypatch: pytest.MonkeyPatch, spy_client: list[dict], audit_path: Path
) -> None:
    def deny(*args, **kwargs):
        raise capabilities.IntegrationPolicyError("archon.dispatch is disabled by policy")

    monkeypatch.setattr(talk_archon.capabilities, "require_integration_action", deny)

    with pytest.raises(talk_archon.ArchonDispatchRefusedError) as excinfo:
        talk_archon.require_dispatch_allowed(
            "archon-clutch", GOOD_BRIEF, audit_path=audit_path
        )

    assert "disabled by policy" in str(excinfo.value)
    assert spy_client == []
    assert [r["outcome"] for r in _audit_rows(audit_path)] == ["denied"]


def test_unresolvable_codebase_refuses_and_audits(
    monkeypatch: pytest.MonkeyPatch, archon_db: Path, tmp_path: Path, audit_path: Path
) -> None:
    monkeypatch.delenv("ARCHON_CODEBASE_ID", raising=False)
    unknown = tmp_path / "some-repo"
    unknown.mkdir()

    with pytest.raises(talk_archon.ArchonDispatchRefusedError) as excinfo:
        talk_archon.require_dispatch_allowed(
            "archon-clutch",
            GOOD_BRIEF,
            repo_root=unknown,
            db_path=archon_db,
            audit_path=audit_path,
        )

    assert "ARCHON_CODEBASE_ID" in str(excinfo.value)
    assert [r["outcome"] for r in _audit_rows(audit_path)] == ["refused_unresolved_codebase"]


def test_a_blank_workflow_is_a_caller_bug_not_a_policy_event(audit_path: Path) -> None:
    """Contract ValueError is raised BEFORE the gate, so it writes no audit row."""

    with pytest.raises(ValueError):
        talk_archon.require_dispatch_allowed("  ", GOOD_BRIEF, audit_path=audit_path)

    assert _audit_rows(audit_path) == []


def test_an_unwritable_audit_refuses_the_grant(
    tmp_path: Path, spy_client: list[dict]
) -> None:
    """Codex R2 major: a granted dispatch must not outrun its own record.

    With no confirmation gate on this path, the append-only trail IS the
    accountability — so a grant whose row cannot be persisted is refused.
    """

    unwritable = tmp_path / "a-file" / "nested.jsonl"
    unwritable.parent.write_text("not a directory", encoding="utf-8")

    with pytest.raises(talk_archon.ArchonDispatchRefusedError) as excinfo:
        talk_archon.require_dispatch_allowed(
            "archon-clutch", GOOD_BRIEF, audit_path=unwritable
        )

    assert "could not write the dispatch audit record" in str(excinfo.value)
    assert spy_client == []


def test_a_refusal_still_survives_an_unwritable_audit(tmp_path: Path) -> None:
    """The strictness is asymmetric: a lost refusal row costs nothing."""

    unwritable = tmp_path / "b-file" / "nested.jsonl"
    unwritable.parent.write_text("not a directory", encoding="utf-8")

    with pytest.raises(talk_archon.ArchonDispatchRefusedError) as excinfo:
        talk_archon.require_dispatch_allowed(
            "archon-clutch", "yeah do that", audit_path=unwritable
        )

    # Refused for the vague brief, not for the audit sink.
    assert "never sees this conversation" in str(excinfo.value)


# ─── codebase binding (Rule 2: physical state) ───────────────────────────


def test_env_override_wins_when_it_is_registered_for_this_repo(
    monkeypatch: pytest.MonkeyPatch, archon_db: Path, tmp_path: Path
) -> None:
    """The override DISAMBIGUATES between rows pointing at this repo."""

    repo = tmp_path / "thehomie"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv("ARCHON_CODEBASE_ID", "explicit-id")
    connection = sqlite3.connect(archon_db)
    with connection:
        connection.execute(
            "INSERT INTO remote_agent_codebases (id, name, default_cwd, kind) "
            "VALUES ('explicit-id', 'owner/repo', ?, 'repo')",
            (str(repo),),
        )
        connection.execute(
            "INSERT INTO remote_agent_codebases (id, name, default_cwd, kind) "
            "VALUES ('other-id', 'owner/repo-alt', ?, 'repo')",
            (str(repo),),
        )
    connection.close()

    assert talk_archon.resolve_codebase_id(repo, db_path=archon_db) == "explicit-id"


def test_an_override_registered_for_another_repo_is_refused(
    monkeypatch: pytest.MonkeyPatch, archon_db: Path, tmp_path: Path
) -> None:
    """Codex R3 blocker: existence in the registry is not authority.

    A stale or copied binding whose row points somewhere else would make
    Archon clone and edit a DIFFERENT codebase.
    """

    repo = tmp_path / "thehomie"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv("ARCHON_CODEBASE_ID", "wrong-repo-id")
    connection = sqlite3.connect(archon_db)
    with connection:
        connection.execute(
            "INSERT INTO remote_agent_codebases (id, name, default_cwd, kind) "
            "VALUES ('wrong-repo-id', 'owner/other', 'C:/definitely/another/repo', 'repo')"
        )
    connection.close()

    with pytest.raises(talk_archon.ArchonDispatchRefusedError) as excinfo:
        talk_archon.resolve_codebase_id(repo, db_path=archon_db)

    assert "different codebase" in str(excinfo.value)


def test_a_stale_env_override_is_refused_against_a_readable_registry(
    monkeypatch: pytest.MonkeyPatch, archon_db: Path
) -> None:
    """Rule 2 (codex R1): the env id is a claim; the registry is the state."""

    monkeypatch.setenv("ARCHON_CODEBASE_ID", "stale-id")
    connection = sqlite3.connect(archon_db)
    with connection:
        connection.execute(
            "INSERT INTO remote_agent_codebases (id, name, default_cwd, kind) "
            "VALUES ('real-id', 'owner/repo', 'C:/elsewhere', 'repo')"
        )
    connection.close()

    with pytest.raises(talk_archon.ArchonDispatchRefusedError) as excinfo:
        talk_archon.resolve_codebase_id(Path("/nowhere"), db_path=archon_db)

    assert "not registered in Archon" in str(excinfo.value)
    assert "owner/repo" in str(excinfo.value)


def test_an_unreadable_registry_refuses_rather_than_certifying(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fail CLOSED (codex R2 major): an unverifiable target is not dispatched.

    Dispatch spends a clone plus a worktree, and an unverified id can land
    that work in the wrong repository. Archon's registry is local and the
    server is up whenever a dispatch is legitimate, so an unreadable registry
    is itself a reason to stop.
    """

    monkeypatch.setenv("ARCHON_CODEBASE_ID", "explicit-id")

    with pytest.raises(talk_archon.ArchonDispatchRefusedError) as excinfo:
        talk_archon.resolve_codebase_id(
            Path("/nowhere"), db_path=tmp_path / "missing.db"
        )

    assert "cannot be verified" in str(excinfo.value)


# ─── correlation key ─────────────────────────────────────────────────────


def test_correlation_ref_round_trips() -> None:
    ref = talk_archon.build_correlation_ref(7, "conv-db-1", "web-1785-abc")

    assert ref == "talk:7:archon:conv-db-1:conv:web-1785-abc"
    assert talk_archon.parse_correlation_ref(ref) == {
        "run_id": 7,
        "conversation_db_id": "conv-db-1",
        "conversation_id": "web-1785-abc",
    }


def test_correlation_ref_keeps_the_legacy_prefix() -> None:
    """Existing consumers match on 'talk:' — the Archon ids are APPENDED."""

    ref = talk_archon.build_correlation_ref(7, "conv-db-1", "web-1785-abc")

    assert ref.startswith("talk:7")


def test_legacy_refs_still_parse() -> None:
    """Rows written before this ticket are real and must not read as corrupt."""

    assert talk_archon.parse_correlation_ref("talk:12") == {
        "run_id": 12,
        "conversation_db_id": None,
        "conversation_id": None,
    }


@pytest.mark.parametrize("ref", ["", None, "paperclip:99", "talk:notanint", "talk"])
def test_foreign_refs_parse_to_none(ref) -> None:
    assert talk_archon.parse_correlation_ref(ref) is None


@pytest.mark.parametrize(
    ("run_id", "db_id", "platform_id"),
    [
        (0, "a", "b"),
        (-1, "a", "b"),
        (True, "a", "b"),
        (1, "has:colon", "b"),
        (1, "a", "has:colon"),
        (1, "", "b"),
        (1, "a", None),
    ],
)
def test_build_correlation_ref_rejects_ambiguous_input(run_id, db_id, platform_id) -> None:
    """A ':' inside an id would make the ref unparseable — refuse, don't encode."""

    with pytest.raises(ValueError):
        talk_archon.build_correlation_ref(run_id, db_id, platform_id)


# ─── dispatch_now ────────────────────────────────────────────────────────


def _grant() -> talk_archon.DispatchGrant:
    return talk_archon.DispatchGrant(
        workflow="archon-clutch",
        brief=GOOD_BRIEF,
        codebase_id="cb-test",
        caller="test",
    )


def test_dispatch_goes_through_the_client_and_audits(
    spy_client: list[dict], audit_path: Path
) -> None:
    dispatch = talk_archon.dispatch_now(_grant(), audit_path=audit_path, run_id=3)

    assert dispatch.conversation_db_id == "conv-db-1"
    assert spy_client == [
        {"codebase_id": "cb-test", "workflow": "archon-clutch", "text": GOOD_BRIEF}
    ]
    row = _audit_rows(audit_path)[0]
    assert row["outcome"] == "dispatched"
    assert row["conversation_id"] == "web-1785-abc"
    assert row["run_id"] == 3


def test_dispatch_refuses_to_run_on_an_event_loop(spy_client: list[dict]) -> None:
    """The absolute event-loop rule, enforced structurally rather than by comment."""

    async def on_the_loop():
        return talk_archon.dispatch_now(_grant())

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(on_the_loop())

    assert "must never run on an event loop" in str(excinfo.value)
    assert spy_client == []


def test_a_transport_failure_becomes_a_speakable_error(
    monkeypatch: pytest.MonkeyPatch, audit_path: Path
) -> None:
    async def unreachable(*args, **kwargs):
        raise archon_client.ArchonUnreachableError()

    monkeypatch.setattr(archon_client, "dispatch_workflow", unreachable)

    with pytest.raises(talk_archon.ArchonDispatchError) as excinfo:
        talk_archon.dispatch_now(_grant(), audit_path=audit_path)

    assert "not reachable" in str(excinfo.value)
    assert [r["outcome"] for r in _audit_rows(audit_path)] == ["failed"]


# ─── run lookup ──────────────────────────────────────────────────────────


def test_run_lookup_joins_on_the_parent_conversation(archon_db: Path) -> None:
    """The load-bearing join: a web dispatch puts OUR id in parent_conversation_id."""

    connection = sqlite3.connect(archon_db)
    with connection:
        connection.execute(
            "INSERT INTO remote_agent_workflow_runs "
            "(id, workflow_name, status, parent_conversation_id, conversation_id, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("run-abc", "archon-clutch", "running", "conv-db-1", "worker-conv-9", "2026-07-27"),
        )
    connection.close()

    assert talk_archon.run_id_for_conversation("conv-db-1", db_path=archon_db) == "run-abc"
    # Matching the worker conversation instead would find nothing — that miss
    # reads exactly like "not started yet", which is why the join is explicit.
    assert talk_archon.run_id_for_conversation("worker-conv-9", db_path=archon_db) is None


def test_run_lookup_is_none_before_the_run_registers(archon_db: Path) -> None:
    assert talk_archon.run_id_for_conversation("conv-db-1", db_path=archon_db) is None


def test_run_lookup_survives_a_missing_ledger(tmp_path: Path) -> None:
    assert talk_archon.run_id_for_conversation("x", db_path=tmp_path / "absent.db") is None


def test_default_workflow_resolves_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    assert talk_archon.default_workflow() == "archon-ralph-dag"

    monkeypatch.setenv("TALK_ARCHON_DEFAULT_WORKFLOW", "archon-clutch")

    assert talk_archon.default_workflow() == "archon-clutch"


# ─── codex R4 regressions ────────────────────────────────────────────────


def test_a_blank_default_cwd_row_is_refused(
    monkeypatch: pytest.MonkeyPatch, archon_db: Path, tmp_path: Path
) -> None:
    """os.path.abspath("") is the PROCESS CWD, so a blank row certified itself.

    Whenever the process happened to be running in the repo root, an override
    naming a row with no registered path passed the "points at this repo"
    check — restoring the wrong-repository dispatch class (codex R4 blocker).
    """

    repo = tmp_path / "thehomie"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv("ARCHON_CODEBASE_ID", "blank-row")
    monkeypatch.chdir(repo)
    connection = sqlite3.connect(archon_db)
    with connection:
        connection.execute(
            "INSERT INTO remote_agent_codebases (id, name, default_cwd, kind) "
            "VALUES ('blank-row', 'owner/blank', '', 'repo')"
        )
    connection.close()

    with pytest.raises(talk_archon.ArchonDispatchRefusedError) as excinfo:
        talk_archon.resolve_codebase_id(repo, db_path=archon_db)

    assert "different codebase" in str(excinfo.value)


@pytest.mark.parametrize(
    "brief",
    [
        # Long enough to clear both floors, still pure pointer (codex R4 major).
        "Proceed with the plan above exactly as agreed, ensuring all "
        "requirements are met and the implementation is complete.",
        "Go ahead with that and make sure everything we discussed is handled "
        "properly across the whole system before you finish the work.",
    ],
)
def test_a_padded_referential_brief_is_refused(brief: str) -> None:
    """Padding must not carry a pointer past the floors.

    The floors were a LENGTH test; a referential phrase was stripped before
    counting, so surrounding filler cleared the bar and the brief reached the
    worker verbatim. A pointer is now refused at any length.
    """

    reason = talk_archon.brief_refusal_reason(brief)

    assert reason is not None
    assert "points back" in reason
