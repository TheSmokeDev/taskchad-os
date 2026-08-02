"""Archon steering surface — manage_run, NL approval, check_work narration (#259).

Path map — one non-vacuous test per distinct code path:

  gate read       uncapped blob beats the 800-char display cap · db missing ·
                  malformed blob · no approval event · newest-wins
  phrase          selects a framework constant · case-insensitive · REFUSES
                  arbitrary text (injection) · end-to-end from the ledger ·
                  unreadable ledger degrades to ""
  steer_now       phrase attached at act time · no-phrase gate stays bare ·
                  operator note + phrase both survive · note on a note-less
                  action is kept but not sent · two audit rows on success ·
                  two on transport failure · two on an Archon refusal ·
                  unwritable audit REFUSES · kill switch propagates · unknown
                  action / bad run id raise before any gate work · event-loop
                  guard
  say_now         posts through send_message · unaccepted is NOT delivered ·
                  blank text / bad conversation id raise · kill switch
  resolve         blank→single paused · blank→ambiguous · blank→nothing ·
                  receipt number · receipt with no Archon id yet · unknown
                  receipt · raw id · garbage
  narration       node + tool calls · paused says so · unknown run · degrades
                  when the node read breaks
  manage_run      destructive previews without confirm (nothing sent) · acts
                  with confirm · say needs words · say with no conversation ·
                  list puts paused first · unknown action · help
  check_work      archon detail narrates · no-arg surfaces paused runs

The ledger is a real fixture SQLite file built from the live archon.db DDL, so
every read here exercises the actual SQL. Only the network boundary
(``archon_client.steer`` / ``send_message``) is replaced — never the seam under
test.
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
import talk_runs
import talk_tools
from integrations import archon_approvals, archon_client, archon_events
from security import kill_switches

# ─────────────────────────────────────────────────────────────────────────────
# Fixture ledger — verbatim DDL from the live archon.db
# ─────────────────────────────────────────────────────────────────────────────
_EVENTS_DDL = """
CREATE TABLE remote_agent_workflow_events (
    id TEXT PRIMARY KEY,
    workflow_run_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    step_index INTEGER,
    step_name TEXT,
    data TEXT DEFAULT '{}',
    created_at TEXT
)
"""

_RUNS_DDL = """
CREATE TABLE remote_agent_workflow_runs (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    codebase_id TEXT,
    workflow_name TEXT NOT NULL,
    user_message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    current_step_index INTEGER,
    metadata TEXT DEFAULT '{}',
    parent_conversation_id TEXT,
    started_at TEXT,
    completed_at TEXT,
    last_activity_at TEXT,
    working_path TEXT
)
"""

RUN = "23c6c29ad89b24d6e662af355bbd4158"
OTHER_RUN = "9f1e2d3c4b5a69788796a5b4c3d2e1f0"


def _add_run(
    path: Path,
    run_id: str = RUN,
    *,
    workflow_name: str = "archon-clutch",
    status: str = "running",
    started_at: str = "2026-07-28 09:00:00",
    working_path: str | None = "C:/wt/clutch",
    conversation_id: str = "conv-db-1",
) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "INSERT INTO remote_agent_workflow_runs "
            "(id, conversation_id, workflow_name, user_message, status, "
            "started_at, last_activity_at, working_path) "
            "VALUES (?, ?, ?, 'do the thing', ?, ?, ?, ?)",
            (run_id, conversation_id, workflow_name, status, started_at, started_at, working_path),
        )
        connection.commit()
    finally:
        connection.close()


def _add_event(
    path: Path,
    event_id: str,
    *,
    run_id: str = RUN,
    event_type: str = "node_started",
    step_name: str | None = "implement",
    data: str = "{}",
    created_at: str = "2026-07-28 09:01:00",
) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "INSERT INTO remote_agent_workflow_events "
            "(id, workflow_run_id, event_type, step_index, step_name, data, created_at) "
            "VALUES (?, ?, ?, NULL, ?, ?, ?)",
            (event_id, run_id, event_type, step_name, data, created_at),
        )
        connection.commit()
    finally:
        connection.close()


def _gate_message(phrase: str = "APPROVE DEPLOY", pad: int = 1200) -> str:
    """A realistic gate message: config dump first, the phrase LAST.

    The live ledger holds approval messages of 2,009 / 2,156 / 28,605
    characters with the phrase at the end — which is exactly why the capped
    display reader cannot be used to find it.
    """
    return ("resolved run config: " + ("x" * pad)) + f"\n\nReply with {phrase} to continue."


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fixture archon.db bound to the ONE knob the Talk slice reads."""
    import config

    path = tmp_path / "archon.db"
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(_EVENTS_DDL)
        connection.execute(_RUNS_DDL)
        connection.commit()
    finally:
        connection.close()
    monkeypatch.setenv("TALK_ARCHON_DB", str(path))
    monkeypatch.setenv("ARCHON_EVENTS_DB", str(path))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", tmp_path / "dashboard.db")
    monkeypatch.delenv("HOMIE_KILLSWITCH_ARCHON_STEER", raising=False)
    talk_runs.reset_for_tests()
    yield path
    talk_runs.reset_for_tests()


@pytest.fixture
def steer_spy(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Replace ONLY the network boundary; every gate and read stays real."""
    calls: list[dict] = []

    async def fake_steer(run_id, action, *, note=None, client=None):
        calls.append({"run_id": run_id, "action": action, "note": note})
        return archon_client.ArchonSteerResult(
            action=action, run_id=run_id, success=True, message=""
        )

    monkeypatch.setattr(archon_client, "steer", fake_steer)
    return calls


@pytest.fixture
def say_spy(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    async def fake_send(conversation_id, text, *, client=None):
        calls.append({"conversation_id": conversation_id, "text": text})
        return {"accepted": True, "status": "started"}

    monkeypatch.setattr(archon_client, "send_message", fake_send)
    return calls


def _audit_rows(tmp_path: Path) -> list[dict]:
    path = tmp_path / "archon_steer.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ─── the uncapped gate read ──────────────────────────────────────────────


def test_gate_read_beats_the_display_cap_that_hid_the_phrase(ledger: Path) -> None:
    """The #268 receipt: the phrase lives past the display reader's 800 chars."""
    message = _gate_message("APPROVE DEPLOY", pad=1200)
    _add_run(ledger, status="paused")
    _add_event(
        ledger,
        "e1",
        event_type="approval_requested",
        step_name="deploy-gate",
        data=json.dumps({"message": message}),
    )

    raw, status = archon_events.read_gate_data_raw(RUN, db_path=ledger)
    assert status == archon_events.STATUS_OK
    assert raw["message"] == message
    assert "APPROVE DEPLOY" in raw["message"]

    # The display reader — correct for the wire, fatal for the control plane.
    events, _ = archon_events.read_recent_events(run_id=RUN, db_path=ledger)
    displayed = events[-1]["data"]["message"]
    assert "APPROVE DEPLOY" not in displayed
    assert displayed.endswith("…[truncated]")


def test_gate_read_takes_the_newest_gate_when_a_run_re_asks(ledger: Path) -> None:
    _add_run(ledger, status="paused")
    _add_event(
        ledger,
        "e1",
        event_type="approval_requested",
        data=json.dumps({"message": "reply with APPROVE SPEND"}),
        created_at="2026-07-28 09:00:00",
    )
    _add_event(
        ledger,
        "e2",
        event_type="approval_requested",
        data=json.dumps({"message": "reply with APPROVE DEPLOY"}),
        created_at="2026-07-28 09:05:00",
    )

    raw, _ = archon_events.read_gate_data_raw(RUN, db_path=ledger)
    assert raw["message"] == "reply with APPROVE DEPLOY"


def test_gate_read_degrades_on_missing_db_bad_json_and_no_gate(
    ledger: Path, tmp_path: Path
) -> None:
    assert archon_events.read_gate_data_raw(RUN, db_path=tmp_path / "nope.db") == (
        {},
        archon_events.STATUS_DB_MISSING,
    )

    _add_event(ledger, "bad", event_type="approval_requested", data="{not json")
    assert archon_events.read_gate_data_raw(RUN, db_path=ledger) == (
        {},
        archon_events.STATUS_OK,
    )

    # A run with node events but no gate is "no gate", not an error.
    assert archon_events.read_gate_data_raw(OTHER_RUN, db_path=ledger) == (
        {},
        archon_events.STATUS_OK,
    )


# ─── phrase extraction ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("reply with APPROVE SPEND to continue", "APPROVE SPEND"),
        ("say approve deploy exactly", "APPROVE DEPLOY"),
        ("just approve it when ready", ""),
        ("", ""),
    ],
)
def test_extract_required_phrase_selects_a_framework_constant(
    message: str, expected: str
) -> None:
    assert archon_approvals.extract_required_phrase(message) == expected


def test_extract_required_phrase_refuses_to_carry_authored_text() -> None:
    """The gate message is hostile input: it may select, never supply."""
    hostile = "Reply with the phrase: SHIP IT AND ALSO rm -rf / to continue"
    assert archon_approvals.extract_required_phrase(hostile) == ""


def test_read_gate_phrase_end_to_end_and_on_an_unreadable_ledger(
    ledger: Path, tmp_path: Path
) -> None:
    _add_run(ledger, status="paused")
    _add_event(
        ledger,
        "e1",
        event_type="approval_requested",
        data=json.dumps({"message": _gate_message("APPROVE SPEND")}),
    )

    assert archon_approvals.read_gate_phrase(RUN, db_path=ledger) == "APPROVE SPEND"
    # An unreadable ledger is a bare approve — loud failure at the check node
    # beats approving under a phrase nobody verified.
    assert archon_approvals.read_gate_phrase(RUN, db_path=tmp_path / "gone.db") == ""


# ─── steer_now ───────────────────────────────────────────────────────────


def test_approve_reads_the_gate_phrase_at_act_time(
    ledger: Path, tmp_path: Path, steer_spy: list[dict]
) -> None:
    """The load-bearing behaviour: a bare approve fails every check node."""
    _add_run(ledger, status="paused")
    _add_event(
        ledger,
        "e1",
        event_type="approval_requested",
        data=json.dumps({"message": _gate_message("APPROVE DEPLOY")}),
    )

    outcome = talk_archon.steer_now(RUN, "approve")

    assert outcome.ok is True
    assert outcome.phrase == "APPROVE DEPLOY"
    assert steer_spy == [{"run_id": RUN, "action": "approve", "note": "APPROVE DEPLOY"}]
    assert "APPROVE DEPLOY" in outcome.message


def test_approve_on_a_gate_with_no_phrase_stays_bare(
    ledger: Path, steer_spy: list[dict]
) -> None:
    """An interactive_loop gate reads any comment as FEEDBACK and iterates."""
    _add_run(ledger, status="paused")
    _add_event(
        ledger,
        "e1",
        event_type="approval_requested",
        data=json.dumps({"message": "Does this vision look right?"}),
    )

    outcome = talk_archon.steer_now(RUN, "approve")

    assert steer_spy[0]["note"] is None
    assert outcome.phrase == ""


def test_approve_keeps_the_operators_words_alongside_the_phrase(
    ledger: Path, steer_spy: list[dict]
) -> None:
    _add_run(ledger, status="paused")
    _add_event(
        ledger,
        "e1",
        event_type="approval_requested",
        data=json.dumps({"message": _gate_message("APPROVE SPEND")}),
    )

    talk_archon.steer_now(RUN, "approve", note="yeah, the budget is fine")

    sent = steer_spy[0]["note"]
    assert "yeah, the budget is fine" in sent
    assert "APPROVE SPEND" in sent


def test_a_note_on_a_note_less_action_is_logged_not_dropped(
    ledger: Path, tmp_path: Path, steer_spy: list[dict]
) -> None:
    """Archon's cancel endpoint has no body field — say so, don't swallow it."""
    _add_run(ledger)

    outcome = talk_archon.steer_now(RUN, "cancel", note="wrong repo")

    assert steer_spy[0]["note"] is None
    assert "no field for a reason" in outcome.message
    assert any(row["note_preview"] == "wrong repo" for row in _audit_rows(tmp_path))


def test_every_path_writes_exactly_attempt_then_result(
    ledger: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Success is never encoded as the ABSENCE of a second row."""
    _add_run(ledger)

    async def ok(run_id, action, *, note=None, client=None):
        return archon_client.ArchonSteerResult(
            action=action, run_id=run_id, success=True, message=""
        )

    monkeypatch.setattr(archon_client, "steer", ok)
    talk_archon.steer_now(RUN, "resume")
    assert [r["outcome"] for r in _audit_rows(tmp_path)] == ["resume_attempted", "resume"]

    async def refused(run_id, action, *, note=None, client=None):
        return archon_client.ArchonSteerResult(
            action=action, run_id=run_id, success=False, message="run is not paused"
        )

    monkeypatch.setattr(archon_client, "steer", refused)
    outcome = talk_archon.steer_now(RUN, "approve")
    assert outcome.ok is False
    assert "not paused" in outcome.message
    assert [r["outcome"] for r in _audit_rows(tmp_path)][-2:] == [
        "approve_attempted",
        "rejected_by_archon",
    ]

    async def boom(run_id, action, *, note=None, client=None):
        raise archon_client.ArchonUnreachableError()

    monkeypatch.setattr(archon_client, "steer", boom)
    outcome = talk_archon.steer_now(RUN, "cancel")
    assert outcome.ok is False
    assert [r["outcome"] for r in _audit_rows(tmp_path)][-2:] == [
        "cancel_attempted",
        "failed",
    ]


def test_an_unwritable_audit_refuses_the_steer(
    ledger: Path, monkeypatch: pytest.MonkeyPatch, steer_spy: list[dict]
) -> None:
    """No record, no mutation — an unrecorded cancel cannot be reconstructed."""
    _add_run(ledger)

    def unwritable(**_fields):
        raise OSError("disk full")

    monkeypatch.setattr(talk_archon, "append_steer_audit_record", unwritable)

    outcome = talk_archon.steer_now(RUN, "cancel")

    assert outcome.ok is False
    assert "audit record" in outcome.message
    assert steer_spy == []


def test_kill_switch_propagates_with_its_shape_intact(
    ledger: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steer_spy: list[dict]
) -> None:
    _add_run(ledger)
    monkeypatch.setenv("HOMIE_KILLSWITCH_ARCHON_STEER", "disabled")

    with pytest.raises(kill_switches.KillSwitchDisabled) as excinfo:
        talk_archon.steer_now(RUN, "cancel")

    assert excinfo.value.switch_name == "archon_steer"
    assert steer_spy == []
    assert _audit_rows(tmp_path)[0]["outcome"] == "refused_killswitch"


def test_contract_violations_raise_before_any_gate_work(
    ledger: Path, tmp_path: Path, steer_spy: list[dict]
) -> None:
    with pytest.raises(ValueError):
        talk_archon.steer_now(RUN, "detonate")
    with pytest.raises(ValueError):
        talk_archon.steer_now("../../etc/passwd", "cancel")

    assert steer_spy == []
    assert _audit_rows(tmp_path) == []


def test_steering_refuses_to_run_on_an_event_loop(ledger: Path) -> None:
    """The 2026-07-13 wedge class, made structural."""
    _add_run(ledger)

    async def on_loop() -> None:
        with pytest.raises(RuntimeError, match="never run on an event loop"):
            talk_archon.steer_now(RUN, "cancel")
        with pytest.raises(RuntimeError, match="never run on an event loop"):
            talk_archon.say_now("web-1785-abc", "hi")

    asyncio.run(on_loop())


# ─── say_now (the NL approval path) ──────────────────────────────────────


def test_say_posts_the_operators_words_and_names_the_sharp_edge(
    ledger: Path, tmp_path: Path, say_spy: list[dict]
) -> None:
    outcome = talk_archon.say_now("web-1785-abc", "looks good, ship it")

    assert outcome.ok is True
    assert say_spy == [
        {"conversation_id": "web-1785-abc", "text": "looks good, ship it"}
    ]
    # A conversation reply cannot reject — "no" would ALSO approve.
    assert "never refuse" in outcome.message
    assert [r["outcome"] for r in _audit_rows(tmp_path)] == ["say_attempted", "say"]


def test_an_unaccepted_message_is_not_reported_as_delivered(
    ledger: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def not_accepted(conversation_id, text, *, client=None):
        return {"accepted": False, "status": "queued-capacity"}

    monkeypatch.setattr(archon_client, "send_message", not_accepted)

    outcome = talk_archon.say_now("web-1785-abc", "also update the readme")

    assert outcome.ok is False
    assert "did not hear it" in outcome.message
    assert _audit_rows(tmp_path)[-1]["outcome"] == "refused_by_archon"


def test_say_contract_violations_and_kill_switch(
    ledger: Path, monkeypatch: pytest.MonkeyPatch, say_spy: list[dict]
) -> None:
    with pytest.raises(ValueError):
        talk_archon.say_now("web-1785-abc", "   ")
    with pytest.raises(ValueError):
        talk_archon.say_now("web/../evil", "hello")

    monkeypatch.setenv("HOMIE_KILLSWITCH_ARCHON_STEER", "disabled")
    with pytest.raises(kill_switches.KillSwitchDisabled):
        talk_archon.say_now("web-1785-abc", "hello")

    assert say_spy == []


# ─── resolving what the operator said into a run id ──────────────────────


def test_blank_reference_picks_the_single_paused_run(ledger: Path) -> None:
    _add_run(ledger, status="paused")
    _add_run(ledger, OTHER_RUN, status="completed")

    assert talk_tools.resolve_archon_run("") == (RUN, "")
    assert talk_tools.resolve_archon_run(None) == (RUN, "")


def test_blank_reference_asks_when_two_runs_are_paused(ledger: Path) -> None:
    _add_run(ledger, status="paused", workflow_name="archon-clutch")
    _add_run(ledger, OTHER_RUN, status="paused", workflow_name="video-production")

    run_id, problem = talk_tools.resolve_archon_run("")

    assert run_id == ""
    assert "2 runs paused" in problem
    assert "archon-clutch" in problem and "video-production" in problem


def test_blank_reference_falls_back_to_the_only_active_run(ledger: Path) -> None:
    """Nothing paused, one thing going — that is unambiguous, so use it."""
    _add_run(ledger, status="running")

    assert talk_tools.resolve_archon_run("") == (RUN, "")


def test_blank_reference_with_nothing_going(ledger: Path) -> None:
    _add_run(ledger, status="completed")

    run_id, problem = talk_tools.resolve_archon_run("")

    assert run_id == ""
    assert "Nothing is running or paused" in problem


def test_a_receipt_number_resolves_through_the_session_registry(ledger: Path) -> None:
    run_id = talk_runs.start_run("archon", "archon-clutch", lambda _r: "done")
    talk_runs.annotate_run(run_id, archon_run_id=RUN)

    assert talk_tools.resolve_archon_run(str(run_id)) == (RUN, "")
    assert talk_tools.resolve_archon_run(f"#{run_id}") == (RUN, "")


def test_a_receipt_not_yet_matched_to_archon_says_so(ledger: Path) -> None:
    run_id = talk_runs.start_run("archon", "archon-clutch", lambda _r: "done")

    resolved, problem = talk_tools.resolve_archon_run(str(run_id))

    assert resolved == ""
    assert "not been matched to an Archon run" in problem


def test_unknown_receipt_and_garbage_reference(ledger: Path) -> None:
    assert "don't have a run #4242" in talk_tools.resolve_archon_run("4242")[1]
    assert "is not an Archon run id" in talk_tools.resolve_archon_run("the big one")[1]
    # A real Archon id passes through untouched.
    assert talk_tools.resolve_archon_run(RUN) == (RUN, "")


# ─── narration ───────────────────────────────────────────────────────────


def test_narration_names_the_node_and_the_recent_tool_calls(ledger: Path) -> None:
    _add_run(ledger, status="running")
    _add_event(ledger, "n1", event_type="node_started", step_name="implement")
    _add_event(
        ledger,
        "t1",
        event_type="tool_called",
        step_name="implement",
        data=json.dumps({"tool_name": "Bash", "tool_input": {"command": "pytest"}}),
        created_at="2026-07-28 09:02:00",
    )
    _add_event(
        ledger,
        "t2",
        event_type="tool_called",
        step_name="implement",
        data=json.dumps({"tool_name": "Edit"}),
        created_at="2026-07-28 09:03:00",
    )

    spoken = talk_tools.narrate_archon_run(RUN)

    assert "archon-clutch" in spoken and "running" in spoken
    assert "node 'implement'" in spoken
    assert "Bash in implement" in spoken and "Edit in implement" in spoken
    assert "C:/wt/clutch" in spoken


def test_narration_says_a_paused_run_is_waiting(ledger: Path) -> None:
    _add_run(ledger, status="paused")

    assert "PAUSED waiting on you" in talk_tools.narrate_archon_run(RUN)


def test_narration_on_an_unknown_run(ledger: Path) -> None:
    assert "no run" in talk_tools.narrate_archon_run(OTHER_RUN)


def test_narration_survives_a_broken_node_read(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A status question must never come back as an exception."""
    _add_run(ledger, status="running")

    def boom(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(archon_events, "read_current_node", boom)
    monkeypatch.setattr(archon_events, "read_recent_events", boom)

    spoken = talk_tools.narrate_archon_run(RUN)

    assert "running" in spoken
    assert "node" not in spoken


# ─── manage_run ──────────────────────────────────────────────────────────


def _manage(**arguments) -> str:
    return talk_tools.execute_talk_tool("manage_run", arguments)


def test_destructive_actions_preview_before_they_act(
    ledger: Path, steer_spy: list[dict]
) -> None:
    _add_run(ledger, status="running")
    _add_event(ledger, "n1", event_type="node_started", step_name="render")

    preview = _manage(action="cancel", run_id=RUN)

    assert "would stop it where it stands" in preview
    assert "confirm true" in preview
    assert "node 'render'" in preview  # a real preview, not a template
    assert steer_spy == []


@pytest.mark.parametrize("action", ["reject", "cancel", "abandon"])
def test_confirmed_destructive_actions_fire(
    ledger: Path, steer_spy: list[dict], action: str
) -> None:
    _add_run(ledger, status="paused")

    _manage(action=action, run_id=RUN, confirm=True)

    assert steer_spy[0]["run_id"] == RUN
    assert steer_spy[0]["action"] == action


def test_approve_and_resume_need_no_confirm(ledger: Path, steer_spy: list[dict]) -> None:
    """Announce-then-act guards destruction, not the answer to a gate."""
    _add_run(ledger, status="paused")

    _manage(action="approve", run_id=RUN)
    _manage(action="resume", run_id=RUN)

    assert [call["action"] for call in steer_spy] == ["approve", "resume"]


def test_say_needs_words_and_a_conversation_to_send_them_to(
    ledger: Path, say_spy: list[dict]
) -> None:
    _add_run(ledger, status="paused")

    assert "needs the words" in _manage(action="say", run_id=RUN)
    assert "can't find the conversation" in _manage(
        action="say", run_id=RUN, note="also fix the readme"
    )
    assert say_spy == []


def test_say_routes_through_the_conversation_recorded_at_dispatch(
    ledger: Path, say_spy: list[dict]
) -> None:
    _add_run(ledger, status="paused")
    run_id = talk_runs.start_run("archon", "archon-clutch", lambda _r: "done")
    talk_runs.annotate_run(
        run_id, archon_run_id=RUN, archon_conversation_id="web-1785-abc"
    )

    _manage(action="say", run_id=str(run_id), note="looks good, ship it")

    assert say_spy == [
        {"conversation_id": "web-1785-abc", "text": "looks good, ship it"}
    ]


def test_list_puts_paused_runs_first(ledger: Path) -> None:
    _add_run(ledger, status="running", workflow_name="archon-ralph-dag")
    _add_run(ledger, OTHER_RUN, status="paused", workflow_name="image-node-factory")

    output = _manage(action="list")

    assert output.index("Paused, waiting on you") < output.index("Running:")
    assert "image-node-factory" in output and "archon-ralph-dag" in output


def test_list_with_nothing_going(ledger: Path) -> None:
    assert "No Archon runs or background agents are going or paused" in _manage(
        action="list"
    )


def test_unknown_action_and_help(ledger: Path) -> None:
    assert "isn't a manage_run action" in _manage(action="detonate")
    assert "preview first and need a confirm" in _manage(action="help")


def test_manage_run_is_advertised_to_the_voice_session() -> None:
    tools = {tool["name"]: tool for tool in talk_tools.default_talk_tools()}

    assert set(tools["manage_run"]["parameters"]["properties"]["action"]["enum"]) == {
        "help", "list", "get", "say", "approve", "reject", "resume", "cancel", "abandon",
    }


# ─── check_work narration ────────────────────────────────────────────────


def test_check_work_detail_narrates_the_node_and_tools(ledger: Path) -> None:
    _add_run(ledger, status="running")
    _add_event(ledger, "n1", event_type="node_started", step_name="implement")
    _add_event(
        ledger,
        "t1",
        event_type="tool_called",
        step_name="implement",
        data=json.dumps({"tool_name": "Bash"}),
        created_at="2026-07-28 09:02:00",
    )
    run_id = talk_runs.start_run("archon", "archon-clutch", lambda _r: "watching")
    talk_runs.annotate_run(run_id, archon_run_id=RUN)

    output = talk_tools.execute_talk_tool("check_work", {"run_id": run_id})

    assert f"Run #{run_id}" in output
    assert "node 'implement'" in output
    assert "Bash" in output


def test_check_work_surfaces_what_is_waiting_on_the_operator(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_run(ledger, status="paused", workflow_name="image-node-factory")
    monkeypatch.setattr(talk_tools, "_voice_subtask_lines", lambda limit=5: [])

    output = talk_tools.execute_talk_tool("check_work", {})

    assert "Paused, waiting on you" in output
    assert "image-node-factory" in output


# ─────────────────────────────────────────────────────────────────────────────
# Round-2 gate findings — the voice-native approval and the default question
# ─────────────────────────────────────────────────────────────────────────────
def test_say_attaches_the_gates_phrase_so_the_check_node_passes(
    ledger: Path, say_spy: list[dict]
) -> None:
    """BLOCKER — "looks good, ship it" resumed the DAG and then failed it.

    say_now forwarded the operator's words verbatim, so Archon accepted the
    message and resumed, and the deterministic <gate>-check node immediately
    failed the run for want of APPROVE SPEND. The Homie reported the approval
    landed while the workflow rejected it. The phrase is padded past the
    800-char display cap on purpose — the read has to be the uncapped one.
    """
    _add_run(ledger, status="paused")
    _add_event(
        ledger,
        "e1",
        event_type="approval_requested",
        data=json.dumps({"message": _gate_message("APPROVE SPEND")}),
    )

    talk_archon.say_now("web-1785-abc", "looks good, ship it", run_id=RUN)

    sent = say_spy[0]["text"]
    assert "looks good, ship it" in sent, "the operator's own words must survive"
    assert "APPROVE SPEND" in sent, "the gate greps for the constant, not the sentence"


def test_say_leaves_a_gate_that_asks_for_no_phrase_alone(
    ledger: Path, say_spy: list[dict]
) -> None:
    """An interactive_loop gate reads ANY non-empty comment as feedback and runs
    another iteration. Appending a phrase nobody asked for would silently change
    what approving means, so a gate with no phrase check gets the words only."""
    _add_run(ledger, status="paused")
    _add_event(
        ledger,
        "e1",
        event_type="approval_requested",
        data=json.dumps({"message": "does this look right to you?"}),
    )

    talk_archon.say_now("web-1785-abc", "yeah that's right", run_id=RUN)

    assert say_spy[0]["text"] == "yeah that's right"


def test_say_does_not_double_the_phrase_the_operator_already_said(
    ledger: Path, say_spy: list[dict]
) -> None:
    _add_run(ledger, status="paused")
    _add_event(
        ledger,
        "e1",
        event_type="approval_requested",
        data=json.dumps({"message": _gate_message("APPROVE DEPLOY")}),
    )

    talk_archon.say_now("web-1785-abc", "APPROVE DEPLOY", run_id=RUN)

    assert say_spy[0]["text"].upper().count("APPROVE DEPLOY") == 1


def test_manage_run_say_threads_the_run_id_through_to_the_phrase(
    ledger: Path, say_spy: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seam, not just the primitive: say_now can only attach the phrase if
    the CALLER hands it the run id, and the caller had it all along. Without
    this the tool path stays broken while the unit test passes."""
    _add_run(ledger, status="paused")
    _add_event(
        ledger,
        "e1",
        event_type="approval_requested",
        data=json.dumps({"message": _gate_message("APPROVE SPEND")}),
    )
    monkeypatch.setattr(talk_tools, "_conversation_for_run", lambda _r: "web-1785-abc")

    talk_tools.execute_talk_tool(
        "manage_run", {"action": "say", "run_id": RUN, "note": "looks good, ship it"}
    )

    sent = say_spy[0]["text"]
    assert "looks good, ship it" in sent
    assert "APPROVE SPEND" in sent


def test_the_bare_question_narrates_the_live_run(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAJOR — check_work declares run_id optional, so "how's it going?" arrives
    as check_work {}. That path listed workflow names and statuses and never
    called the narrator, so the PRD's first what-done-looks-like bullet went
    unmet on the most likely request. The node and the recent tool calls are in
    the ledger the whole time."""
    _add_run(ledger, status="running", workflow_name="epic-piv-ticket")
    _add_event(ledger, "n1", event_type="node_started", step_name="implement")
    _add_event(
        ledger,
        "t1",
        event_type="tool_called",
        data=json.dumps({"tool_name": "Bash"}),
    )
    monkeypatch.setattr(talk_tools, "_voice_subtask_lines", lambda limit=5: [])

    output = talk_tools.execute_talk_tool("check_work", {})

    assert "implement" in output, "the bare question must say WHERE the run is"
    assert "Bash" in output, "and WHAT it is doing"


def _set_codebase(path: Path, run_id: str, codebase_id: str) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "UPDATE remote_agent_workflow_runs SET codebase_id = ? WHERE id = ?",
            (codebase_id, run_id),
        )
        connection.commit()
    finally:
        connection.close()


@pytest.mark.parametrize("confirm", ["false", "no", 0, "", [], "true", 1, "yes"])
def test_only_a_literal_true_confirm_destroys(
    ledger: Path, steer_spy: list[dict], confirm
) -> None:
    """MAJOR — the arguments dict is untyped from the model/client and the gate
    was `bool(...)`, so confirm="false" CANCELLED a run: bool("false") is True.
    A probe returned FIRED with mutation_called=true. Truthy strings must land
    on the preview, and so must truthy non-booleans — the only value that
    destroys is `True`."""
    _add_run(ledger, status="running")

    output = talk_tools.execute_talk_tool(
        "manage_run", {"action": "cancel", "run_id": RUN, "confirm": confirm}
    )

    assert steer_spy == [], f"confirm={confirm!r} must not reach the mutation"
    assert "cancel" in output.lower()


def test_a_literal_true_confirm_still_fires(
    ledger: Path, steer_spy: list[dict]
) -> None:
    """The guard must not cost the operator the verb itself."""
    _add_run(ledger, status="running")

    talk_tools.execute_talk_tool(
        "manage_run", {"action": "cancel", "run_id": RUN, "confirm": True}
    )

    assert [c["action"] for c in steer_spy] == ["cancel"]


def test_say_refuses_a_running_run_instead_of_claiming_it_landed(
    ledger: Path, say_spy: list[dict]
) -> None:
    """MAJOR — Archon routes plain text to the WORKFLOW only when the run is
    paused (orchestrator-agent.ts:1162-1176). On a running run the message is
    filed as parent-conversation chatter, the worker never sees it, and Archon
    still answers 200 — so "sent it to the run" was true of the HTTP call and
    false of the world. A run with no pause point is cancellable, not
    redirectable, and it has to say so."""
    _add_run(ledger, status="running")

    output = talk_tools.execute_talk_tool(
        "manage_run",
        {"action": "say", "run_id": RUN, "note": "switch to Postgres"},
    )

    assert say_spy == [], "nothing should reach Archon for a running run"
    assert "not paused" in output.lower()
    assert "cancel" in output.lower(), "it must name what it CAN do instead"


def test_say_still_reaches_a_paused_run(
    ledger: Path, say_spy: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_run(ledger, status="paused")
    monkeypatch.setattr(talk_tools, "_conversation_for_run", lambda _r: "web-1785-abc")

    talk_tools.execute_talk_tool(
        "manage_run", {"action": "say", "run_id": RUN, "note": "looks good"}
    )

    assert len(say_spy) == 1


def test_a_run_from_another_codebase_is_not_steerable(
    ledger: Path, steer_spy: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAJOR — run targeting queried on status alone, so a syntactically valid
    id from ANY of this box's ten registered codebases reached steer_now. The
    ledger row decides ownership, not the shape of the string."""
    _add_run(ledger, status="paused")
    _set_codebase(ledger, RUN, "cb-somebody-else")
    monkeypatch.setattr(talk_tools, "_steerable_codebase_id", lambda: "cb-ours")

    output = talk_tools.execute_talk_tool(
        "manage_run", {"action": "approve", "run_id": RUN}
    )

    assert steer_spy == [], "another project's run must never be mutated"
    assert "different archon project" in output.lower()


def test_our_own_codebase_run_still_steers(
    ledger: Path, steer_spy: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_run(ledger, status="paused")
    _set_codebase(ledger, RUN, "cb-ours")
    monkeypatch.setattr(talk_tools, "_steerable_codebase_id", lambda: "cb-ours")

    talk_tools.execute_talk_tool("manage_run", {"action": "approve", "run_id": RUN})

    assert [c["action"] for c in steer_spy] == ["approve"]


def test_blank_resolution_ignores_another_projects_paused_run(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The concrete failure: ONE paused run, not ours, and blank resolution's
    single-candidate branch hands it straight to a mutation."""
    _add_run(ledger, status="paused")
    _set_codebase(ledger, RUN, "cb-somebody-else")
    monkeypatch.setattr(talk_tools, "_steerable_codebase_id", lambda: "cb-ours")

    run_id, problem = talk_tools.resolve_archon_run("")

    assert run_id == ""
    assert problem, "it must refuse, not pick a stranger's run"


def test_say_refuses_when_the_ledger_cannot_confirm_the_pause(
    ledger: Path, say_spy: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kimi design gate, MINOR — UNKNOWN is not PAUSED.

    _archon_run_row returns None on a locked/missing ledger by design, and the
    first cut of the honesty guard read `if row and status != "paused"`, so the
    unreadable case fell THROUGH to the send. Archon answers accepted=true for
    any valid conversation message, so the Homie would announce that the words
    were an approval while the worker never saw them — a control the operator
    believes fired and did not, which is the costly failure for this deployment.
    """
    _add_run(ledger, status="paused")
    monkeypatch.setattr(talk_tools, "_conversation_for_run", lambda _r: "web-1785-abc")
    monkeypatch.setattr(talk_tools, "_archon_run_row", lambda _db, _r: None)

    output = talk_tools.execute_talk_tool(
        "manage_run", {"action": "say", "run_id": RUN, "note": "looks good, ship it"}
    )

    assert say_spy == [], "an unconfirmable pause must not send"
    assert "can't read" in output.lower() or "cannot read" in output.lower()
    assert "approval" in output.lower(), "it must name what it refused to claim"


# ─────────────────────────────────────────────────────────────────────────────
# Background agents — the delegate_task steer/cancel branch
#
# Path map (one non-vacuous test per distinct path):
#   say → queue receipt · steer delivered as a --resume follow-up · rotated
#   session id chains (no fallback) · stderr miss marker → context
#   re-assembly · empty turn-1 sid → re-assembly · steer on terminal refused ·
#   cancel preview (no confirm) kills nothing · non-True confirms never kill ·
#   confirmed cancel finishes FIRST then kills the annotated pid · cancel on a
#   terminal run performs zero kills · follow-up cap exhaustion marks the
#   pending steer undelivered · budget floor skips the spawn · blank reference
#   names live agents · get names pending steers · skill/look receipts get the
#   kind-aware refusal. Only the turn boundary (_run_agent_turn) and the kill
#   (_kill_pid_tree) are faked — never the seam under test.
# ─────────────────────────────────────────────────────────────────────────────

import threading
import time as _time


@pytest.fixture
def agent_env(monkeypatch: pytest.MonkeyPatch):
    """Registry-only agent runs: ledger stubbed out, archon db untouched."""

    talk_runs.reset_for_tests()
    monkeypatch.setattr(talk_tools, "_create_voice_convoy", lambda title, task: (1, 1))
    monkeypatch.setattr(talk_tools, "_try_convoy_service", lambda: None)
    yield
    talk_runs.reset_for_tests()


def _agent_envelope(response: str, session_id: str = "cli-s1", stderr: str = "") -> dict:
    return {
        "success": True,
        "response": response,
        "session_id": session_id,
        "error": "unknown engine error",
        "stderr": stderr,
    }


def _wait_agent_terminal(run_id: int, timeout: float = 5.0) -> dict:
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        run = talk_runs.get_run(run_id)
        if run and run["status"] in talk_runs.TERMINAL_STATUSES:
            return run
        _time.sleep(0.02)
    raise AssertionError(f"agent run {run_id} never reached a terminal status")


def test_steer_lands_as_resumed_follow_up(agent_env, monkeypatch) -> None:
    calls: list[dict] = []
    first_turn_gate = threading.Event()

    def fake_turn(prompt, lane, timeout_s, run_id, resume_sid=""):
        calls.append({"prompt": prompt, "resume_sid": resume_sid})
        if len(calls) == 1:
            assert first_turn_gate.wait(timeout=5)
            return _agent_envelope("first reply", session_id="cli-s1")
        return _agent_envelope("steered reply", session_id="cli-s2")

    monkeypatch.setattr(talk_tools, "_run_agent_turn", fake_turn)
    run_id, _ = talk_tools.start_agent_run("audit the site", "audit", "codex")

    receipt = _manage(action="say", run_id=str(run_id), note="focus on pricing")
    assert "next turn boundary" in receipt
    first_turn_gate.set()

    run = _wait_agent_terminal(run_id)
    assert run["status"] == "done"
    assert run["output"] == "steered reply"
    assert calls[1]["resume_sid"] == "cli-s1"
    assert calls[1]["prompt"] == "focus on pricing"


def test_rotated_session_id_chains_without_fallback(agent_env, monkeypatch) -> None:
    """The runtime id legitimately rotates on resume — chain it, never fall
    back to re-assembly on a mere id change."""

    calls: list[dict] = []
    gate = threading.Event()

    def fake_turn(prompt, lane, timeout_s, run_id, resume_sid=""):
        calls.append({"prompt": prompt, "resume_sid": resume_sid})
        if len(calls) == 1:
            assert gate.wait(timeout=5)
            return _agent_envelope("first", session_id="cli-a")
        if len(calls) == 2:
            # Queue the NEXT steer during the follow-up so the third turn
            # exists — it must resume the ROTATED id.
            talk_runs.queue_steer(run_id, "second steer")
            return _agent_envelope("second", session_id="cli-ROTATED")
        return _agent_envelope("third", session_id="cli-b")

    monkeypatch.setattr(talk_tools, "_run_agent_turn", fake_turn)
    run_id, _ = talk_tools.start_agent_run("task", "t", "codex")
    _manage(action="say", run_id=str(run_id), note="first steer")
    gate.set()

    run = _wait_agent_terminal(run_id)
    assert run["status"] == "done"
    assert calls[1]["resume_sid"] == "cli-a"
    assert calls[2]["resume_sid"] == "cli-ROTATED"
    assert "Original task" not in calls[2]["prompt"]


def test_resume_miss_marker_falls_back_to_reassembly(agent_env, monkeypatch) -> None:
    calls: list[dict] = []
    gate = threading.Event()
    miss_line = "Warning: session 'cli-gone' not found, starting new session"

    def fake_turn(prompt, lane, timeout_s, run_id, resume_sid=""):
        calls.append({"prompt": prompt, "resume_sid": resume_sid})
        if len(calls) == 1:
            assert gate.wait(timeout=5)
            return _agent_envelope("first reply", session_id="cli-gone")
        if len(calls) == 2:
            return _agent_envelope(
                "context-less garbage", session_id="cli-fresh", stderr=miss_line
            )
        return _agent_envelope("recovered reply", session_id="cli-r")

    monkeypatch.setattr(talk_tools, "_run_agent_turn", fake_turn)
    run_id, _ = talk_tools.start_agent_run("build the report", "report", "codex")
    _manage(action="say", run_id=str(run_id), note="use Q3 numbers")
    gate.set()

    run = _wait_agent_terminal(run_id)
    assert run["status"] == "done"
    assert run["output"] == "recovered reply"
    assert calls[2]["resume_sid"] == ""
    assert "Original task" in calls[2]["prompt"]
    assert "use Q3 numbers" in calls[2]["prompt"]


def test_empty_turn_one_sid_goes_straight_to_reassembly(agent_env, monkeypatch) -> None:
    calls: list[dict] = []
    gate = threading.Event()

    def fake_turn(prompt, lane, timeout_s, run_id, resume_sid=""):
        calls.append({"prompt": prompt, "resume_sid": resume_sid})
        if len(calls) == 1:
            assert gate.wait(timeout=5)
            return _agent_envelope("first reply", session_id="")
        return _agent_envelope("steered", session_id="cli-x")

    monkeypatch.setattr(talk_tools, "_run_agent_turn", fake_turn)
    run_id, _ = talk_tools.start_agent_run("task", "t", "codex")
    _manage(action="say", run_id=str(run_id), note="go deeper")
    gate.set()

    run = _wait_agent_terminal(run_id)
    assert run["status"] == "done"
    assert calls[1]["resume_sid"] == ""
    assert "Original task" in calls[1]["prompt"]


def test_steer_on_terminal_run_is_refused(agent_env, monkeypatch) -> None:
    monkeypatch.setattr(
        talk_tools,
        "_run_agent_turn",
        lambda prompt, lane, timeout_s, run_id, resume_sid="": _agent_envelope("done fast"),
    )
    run_id, _ = talk_tools.start_agent_run("task", "t", "codex")
    _wait_agent_terminal(run_id)

    out = _manage(action="say", run_id=str(run_id), note="too late")
    assert "already finished" in out


def test_cancel_preview_without_confirm_kills_nothing(agent_env, monkeypatch) -> None:
    kills: list[int] = []
    monkeypatch.setattr(talk_tools, "_kill_pid_tree", kills.append)
    gate = threading.Event()

    def fake_turn(prompt, lane, timeout_s, run_id, resume_sid=""):
        talk_runs.annotate_run(run_id, pid=4242)
        assert gate.wait(timeout=5)
        talk_runs.annotate_run(run_id, pid=None)
        return _agent_envelope("finished after preview")

    monkeypatch.setattr(talk_tools, "_run_agent_turn", fake_turn)
    run_id, _ = talk_tools.start_agent_run("long task", "long", "codex")

    preview = _manage(action="cancel", run_id=str(run_id))
    assert "confirm true" in preview
    assert "long" in preview
    assert kills == []
    assert talk_runs.get_run(run_id)["status"] == "running"

    gate.set()
    assert _wait_agent_terminal(run_id)["status"] == "done"


@pytest.mark.parametrize("confirm", ["false", "no", 0, "", "[]", 1, "yes"])
def test_agent_cancel_non_true_confirms_never_kill(
    agent_env, monkeypatch, confirm
) -> None:
    kills: list[int] = []
    monkeypatch.setattr(talk_tools, "_kill_pid_tree", kills.append)
    gate = threading.Event()

    def fake_turn(prompt, lane, timeout_s, run_id, resume_sid=""):
        assert gate.wait(timeout=5)
        return _agent_envelope("ok")

    monkeypatch.setattr(talk_tools, "_run_agent_turn", fake_turn)
    run_id, _ = talk_tools.start_agent_run("task", "t", "codex")

    out = _manage(action="cancel", run_id=str(run_id), confirm=confirm)
    assert "confirm true" in out
    assert kills == []

    gate.set()
    _wait_agent_terminal(run_id)


def test_confirmed_cancel_finishes_first_and_kills_the_pid(
    agent_env, monkeypatch
) -> None:
    kills: list[int] = []
    monkeypatch.setattr(talk_tools, "_kill_pid_tree", kills.append)
    release = threading.Event()

    def fake_turn(prompt, lane, timeout_s, run_id, resume_sid=""):
        talk_runs.annotate_run(run_id, pid=4242)
        assert release.wait(timeout=5)
        talk_runs.annotate_run(run_id, pid=None)
        return _agent_envelope("reply nobody hears")

    monkeypatch.setattr(talk_tools, "_run_agent_turn", fake_turn)
    run_id, _ = talk_tools.start_agent_run("doomed", "doomed", "codex")

    deadline = _time.time() + 3
    while _time.time() < deadline:
        run = talk_runs.get_run(run_id)
        if run and run["meta"].get("pid"):
            break
        _time.sleep(0.02)

    out = _manage(action="cancel", run_id=str(run_id), confirm=True)
    assert "Cancelled" in out
    assert kills == [4242]
    run = talk_runs.get_run(run_id)
    assert run["status"] == "failed"
    assert "cancelled by operator" in run["output"]

    release.set()
    _time.sleep(0.2)
    assert talk_runs.get_run(run_id)["output"].startswith("cancelled by operator")


def test_cancel_on_terminal_run_performs_zero_kills(agent_env, monkeypatch) -> None:
    kills: list[int] = []
    monkeypatch.setattr(talk_tools, "_kill_pid_tree", kills.append)
    monkeypatch.setattr(
        talk_tools,
        "_run_agent_turn",
        lambda prompt, lane, timeout_s, run_id, resume_sid="": _agent_envelope("done"),
    )
    run_id, _ = talk_tools.start_agent_run("task", "t", "codex")
    _wait_agent_terminal(run_id)

    out = _manage(action="cancel", run_id=str(run_id), confirm=True)
    assert "already finished" in out
    assert kills == []
    assert talk_runs.get_run(run_id)["output"] == "done"


def test_follow_up_cap_marks_late_steer_undelivered(agent_env, monkeypatch) -> None:
    calls: list[str] = []
    gate = threading.Event()

    def fake_turn(prompt, lane, timeout_s, run_id, resume_sid=""):
        calls.append(prompt)
        if len(calls) == 1:
            assert gate.wait(timeout=5)
        # Every boundary finds a fresh steer — the cap must end it.
        talk_runs.queue_steer(run_id, f"steer {len(calls)}")
        return _agent_envelope(f"reply {len(calls)}", session_id="cli-s")

    monkeypatch.setattr(talk_tools, "_run_agent_turn", fake_turn)
    run_id, _ = talk_tools.start_agent_run("task", "t", "codex")
    gate.set()

    run = _wait_agent_terminal(run_id)
    assert run["status"] == "done"
    assert len(calls) == 1 + talk_tools._MAX_STEER_TURNS
    assert "arrived too late" in run["output"]
    assert run["meta"]["undelivered_steers"]


def test_budget_floor_skips_the_spawn(agent_env, monkeypatch) -> None:
    monkeypatch.setattr(talk_tools, "_agent_timeout_s", lambda: 60)
    clock = {"now": 0.0}
    monkeypatch.setattr(talk_tools.time, "monotonic", lambda: clock["now"])
    gate = threading.Event()

    def fake_turn(prompt, lane, timeout_s, run_id, resume_sid=""):
        assert gate.wait(timeout=5)
        talk_runs.queue_steer(run_id, "late steer")
        clock["now"] = 55.0  # 5s left < the 10s spawn floor
        return _agent_envelope("only reply")

    monkeypatch.setattr(talk_tools, "_run_agent_turn", fake_turn)
    run_id, _ = talk_tools.start_agent_run("task", "t", "codex")
    gate.set()

    run = _wait_agent_terminal(run_id)
    assert run["status"] == "done"
    assert "only reply" in run["output"]
    assert "ran out of budget" in run["output"]


def test_blank_reference_names_live_agent_runs(ledger: Path, monkeypatch) -> None:
    monkeypatch.setattr(talk_tools, "_create_voice_convoy", lambda title, task: (1, 1))
    monkeypatch.setattr(talk_tools, "_try_convoy_service", lambda: None)
    gate = threading.Event()

    def fake_turn(prompt, lane, timeout_s, run_id, resume_sid=""):
        assert gate.wait(timeout=5)
        return _agent_envelope("ok")

    monkeypatch.setattr(talk_tools, "_run_agent_turn", fake_turn)
    run_id, _ = talk_tools.start_agent_run("migrate the db", "migrate", "codex")

    out = _manage(action="cancel")  # blank reference, empty archon ledger
    assert "background agent" in out
    assert f"#{run_id}" in out

    gate.set()
    _wait_agent_terminal(run_id)


def test_get_names_pending_steers(agent_env, monkeypatch) -> None:
    gate = threading.Event()

    def fake_turn(prompt, lane, timeout_s, run_id, resume_sid=""):
        assert gate.wait(timeout=5)
        return _agent_envelope("ok")

    monkeypatch.setattr(talk_tools, "_run_agent_turn", fake_turn)
    run_id, _ = talk_tools.start_agent_run("task", "t", "codex")
    _manage(action="say", run_id=str(run_id), note="steer one")

    out = _manage(action="get", run_id=str(run_id))
    assert "1 steer(s) queued" in out

    gate.set()
    _wait_agent_terminal(run_id)


def test_resume_miss_marker_matches_the_cli_adapter_warning() -> None:
    """Cross-file tripwire (the WORK_STARTED pattern): the steer loop greps
    stderr for this marker, and the cli_adapter must (a) emit a warning
    containing it and (b) emit it UNCONDITIONALLY on stderr — the quiet gate
    that used to suppress it made a resume miss invisible to programmatic
    callers."""

    source = (_SCRIPTS.parent / "chat" / "adapters" / "cli_adapter.py").read_text(
        encoding="utf-8"
    )
    assert talk_tools._RESUME_MISS_MARKER in source
    warn_at = source.index(talk_tools._RESUME_MISS_MARKER)
    window = source[max(0, warn_at - 400) : warn_at + 200]
    assert "sys.stderr" in window
    assert "if not self._quiet" not in window
    # And the STRICT marker: the primary miss signal rides the quiet error
    # envelope via the ResumeTargetMissing message.
    assert talk_tools._STRICT_MISS_MARKER in source
    assert "ResumeTargetMissing" in source


def test_skill_and_look_receipts_get_kind_aware_refusal(agent_env) -> None:
    gate = threading.Event()

    def look_worker(rid: int) -> str:
        gate.wait(timeout=5)
        return "seen"

    run_id = talk_runs.start_run("look", "screen", look_worker)

    out = _manage(action="cancel", run_id=str(run_id))
    assert "is a look run" in out
    assert "check_work" in out
    gate.set()


# ─── codex R1 fixes — each test pins one found defect ─────────────────────


def test_strict_miss_envelope_triggers_reassembly(agent_env, monkeypatch) -> None:
    """Primary miss path: --resume-strict refuses BEFORE the engine runs and
    the quiet error envelope carries the marker — no context-less turn."""

    calls: list[dict] = []
    gate = threading.Event()

    def fake_turn(prompt, lane, timeout_s, run_id, resume_sid=""):
        calls.append({"prompt": prompt, "resume_sid": resume_sid})
        if len(calls) == 1:
            assert gate.wait(timeout=5)
            return _agent_envelope("first reply", session_id="cli-gone")
        if len(calls) == 2:
            return {
                "success": False,
                "response": "",
                "session_id": "",
                "error": "resume session not found: 'cli-gone'",
                "stderr": "",
            }
        return _agent_envelope("recovered", session_id="cli-r")

    monkeypatch.setattr(talk_tools, "_run_agent_turn", fake_turn)
    run_id, _ = talk_tools.start_agent_run("task", "t", "codex")
    _manage(action="say", run_id=str(run_id), note="pivot to the API")
    gate.set()

    run = _wait_agent_terminal(run_id)
    assert run["status"] == "done"
    assert run["output"] == "recovered"
    assert calls[2]["resume_sid"] == ""
    assert "pivot to the API" in calls[2]["prompt"]


def test_epoch_sized_receipts_still_route_to_the_agent_branch(
    agent_env, monkeypatch
) -> None:
    """The registry seeds epoch-sized ids when its history file is unreadable
    — a ten-digit receipt must still steer (no length gate on the divert)."""

    monkeypatch.setattr(talk_runs, "_RUN_SEQ", 1_700_000_000)
    gate = threading.Event()

    def fake_turn(prompt, lane, timeout_s, run_id, resume_sid=""):
        assert gate.wait(timeout=5)
        return _agent_envelope("ok")

    monkeypatch.setattr(talk_tools, "_run_agent_turn", fake_turn)
    try:
        run_id, _ = talk_tools.start_agent_run("task", "t", "codex")
        assert run_id > 1_000_000_000

        out = _manage(action="say", run_id=str(run_id), note="steer the big id")
        assert "next turn boundary" in out
    finally:
        gate.set()
    _wait_agent_terminal(run_id)


def test_reassembly_reserves_the_steering_text() -> None:
    """A 12k chained-skill task must not truncate the steering off the end."""

    huge_task = "x" * 20_000
    prompt = talk_tools._reassemble_prompt(huge_task, "prior output", "use Q3 numbers")

    assert len(prompt) <= talk_tools._REASSEMBLY_MAX_CHARS + 300
    assert "use Q3 numbers" in prompt
    assert prompt.rstrip().endswith("use Q3 numbers")


def test_cancel_between_status_check_and_spawn_kills_the_new_process(
    agent_env, monkeypatch
) -> None:
    """The attach race (codex R1 high): cancel lands after the worker's
    status check but before the pid attaches — the freshly spawned process
    must die immediately and never communicate."""

    killed: list[int] = []

    class _FakeProc:
        pid = 7777
        returncode = 0

        def communicate(self, timeout=None):
            return ("", "")

    monkeypatch.setattr(
        talk_tools.subprocess, "Popen", lambda *a, **k: _FakeProc()
    )
    monkeypatch.setattr(talk_tools, "_kill_process_tree", lambda proc: killed.append(proc.pid))

    gate = threading.Event()
    run_id = talk_runs.start_run("agent", "raced", lambda rid: gate.wait(2) and "x" or "x")
    try:
        # Cancel first: the run is terminal before the spawn attaches.
        talk_runs.finish_run(run_id, "failed", "cancelled by operator")

        envelope = talk_tools._run_agent_turn("prompt", "codex", 30, run_id)

        assert envelope["error"] == "cancelled"
        # The kill precedes any full-budget communicate — the freshly
        # spawned process must never run the turn.
        assert killed == [7777]
    finally:
        gate.set()


def test_follow_up_argv_carries_resume_strict(agent_env, monkeypatch) -> None:
    """The follow-up turn must pass --resume <sid> --resume-strict so a miss
    aborts pre-engine instead of executing context-less."""

    argvs: list[list[str]] = []

    class _FakeProc:
        pid = 4321
        returncode = 0

        def communicate(self, timeout=None):
            envelope = json.dumps(
                {"success": True, "response": "ok", "session_id": "cli-n"}
            )
            return (envelope, "")

    def fake_popen(argv, **kwargs):
        argvs.append(list(argv))
        return _FakeProc()

    monkeypatch.setattr(talk_tools.subprocess, "Popen", fake_popen)
    gate = threading.Event()
    run_id = talk_runs.start_run("agent", "argv", lambda rid: gate.wait(2) and "x" or "x")
    try:
        talk_tools._run_agent_turn("steer text", "codex", 30, run_id, resume_sid="cli-s1")

        assert argvs, "the turn must spawn"
        argv = argvs[0]
        assert "--resume" in argv
        assert argv[argv.index("--resume") + 1] == "cli-s1"
        assert "--resume-strict" in argv
    finally:
        gate.set()


# ─── codex R2 — the final-slot miss must never drop a steer silently ──────


def test_strict_miss_on_the_final_slot_still_delivers(agent_env, monkeypatch) -> None:
    """A strict miss is FREE (the engine never ran): even on the last
    follow-up slot the reassembly retry runs and the steer is delivered —
    the r2 repro used to end 'done' with the steer silently gone."""

    calls: list[dict] = []
    gate = threading.Event()

    def fake_turn(prompt, lane, timeout_s, run_id, resume_sid=""):
        calls.append({"prompt": prompt, "resume_sid": resume_sid})
        if len(calls) == 1:
            assert gate.wait(timeout=5)
            talk_runs.queue_steer(run_id, "steer one")
            return _agent_envelope("reply 1", session_id="cli-1")
        if len(calls) == 2:
            talk_runs.queue_steer(run_id, "steer two")
            return _agent_envelope("reply 2", session_id="cli-2")
        if len(calls) == 3:
            talk_runs.queue_steer(run_id, "the final steer")
            return _agent_envelope("reply 3", session_id="cli-3")
        if len(calls) == 4:
            # The FINAL follow-up slot strict-misses pre-engine.
            return {
                "success": False,
                "response": "",
                "session_id": "",
                "error": "resume session not found: 'cli-3'",
                "stderr": "",
            }
        return _agent_envelope("final steer applied", session_id="cli-5")

    monkeypatch.setattr(talk_tools, "_run_agent_turn", fake_turn)
    run_id, _ = talk_tools.start_agent_run("task", "t", "codex")
    gate.set()

    run = _wait_agent_terminal(run_id)
    assert run["status"] == "done"
    assert run["output"] == "final steer applied"
    assert "Original task" in calls[4]["prompt"]
    assert "the final steer" in calls[4]["prompt"]


def test_nonstrict_miss_at_the_cap_names_the_loss(agent_env, monkeypatch) -> None:
    """Older-CLI path: the context-less turn executed and charged the cap —
    at the boundary the drained steer's loss is NAMED, never silent."""

    calls: list[dict] = []
    gate = threading.Event()
    miss_line = "Warning: session 'cli-3' not found, starting new session"

    def fake_turn(prompt, lane, timeout_s, run_id, resume_sid=""):
        calls.append({"prompt": prompt, "resume_sid": resume_sid})
        if len(calls) == 1:
            assert gate.wait(timeout=5)
            talk_runs.queue_steer(run_id, "steer one")
            return _agent_envelope("reply 1", session_id="cli-1")
        if len(calls) == 2:
            talk_runs.queue_steer(run_id, "steer two")
            return _agent_envelope("reply 2", session_id="cli-2")
        if len(calls) == 3:
            talk_runs.queue_steer(run_id, "doomed steer")
            return _agent_envelope("reply 3", session_id="cli-3")
        # Final slot: the resume missed NON-strictly (turn executed).
        return _agent_envelope("context-less noise", session_id="cli-x", stderr=miss_line)

    monkeypatch.setattr(talk_tools, "_run_agent_turn", fake_turn)
    run_id, _ = talk_tools.start_agent_run("task", "t", "codex")
    gate.set()

    run = _wait_agent_terminal(run_id)
    assert run["status"] == "done"
    assert "reply 3" in run["output"]
    assert "could not be delivered" in run["output"]
    assert "context-less noise" not in run["output"]
    assert len(calls) == 4  # the cap held
