"""Talk deploy + control surface — run_archon, computer, browse, check_work.

Nothing here reaches Archon, moves a mouse, or drives a browser: the HTTP
client, the GUI stack, and the router dispatch are all patched at their source
modules. The archon.db reads are patched too, and the work-queue ledger is a
real temp SQLite file, so the operator's live ledgers are never touched.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))

import talk_archon
import talk_computer
import talk_runs
import talk_tools
from integrations import archon_client


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    talk_runs.reset_for_tests()
    yield
    talk_runs.reset_for_tests()


def _wait_for_run(run_id: int, timeout: float = 15.0) -> dict:
    """Block until the worker thread reaches a terminal status.

    The budget is generous on purpose: these runs finish in milliseconds, but
    the whole file spawns worker threads and a tight bound turns Windows
    scheduling latency under full-suite load into a flaky red.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = talk_runs.get_run(run_id)
        if run and run["status"] in talk_runs.TERMINAL_STATUSES:
            return run
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} never terminated")


def _run_id_from(receipt: str) -> int:
    return int(receipt.split("#")[1].split(" ")[0])


def _deploy_archon(args: dict) -> str:
    """Fire a deploy. No confirmation step: a worktree run is the free tier."""

    return talk_tools.execute_talk_tool("run_archon", dict(args))


# ─── run_archon ──────────────────────────────────────────────────────────

BRIEF = (
    "Build the YourProduct employee page at /employee with the three-tier pricing "
    "table and a Stripe checkout link; done when it renders on production."
)


class _Dispatch:
    """Stand-in for ArchonDispatch carrying both ids the correlation key needs."""

    conversation_id = "web-1785-abc"
    conversation_db_id = "conv-db-1"
    dispatched = True
    accepted = True
    status = "started"


@pytest.fixture
def archon_lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Gate cleared, client spied, ledger real, audit trail in a temp file."""

    import config
    from orchestration.convoy_service import ConvoyService
    from orchestration.db import OrchestrationDB

    ledger_path = tmp_path / "orchestration.db"
    monkeypatch.setattr(config, "ORCHESTRATION_DB_PATH", ledger_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    # Bound separately from DATA_DIR at import — without this the kill-switch
    # test writes a fabricated refusal into the operational dashboard.db.
    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", tmp_path / "dashboard.db")
    monkeypatch.setenv("ARCHON_CODEBASE_ID", "cb-test")
    # A temp registry that REGISTERS cb-test: resolution refuses a target it
    # cannot verify, so the bound id has to exist somewhere — and it must not
    # be the machine's real ~/.archon/archon.db.
    registry = tmp_path / "archon-registry.db"
    registry_conn = sqlite3.connect(registry)
    with registry_conn:
        registry_conn.execute(
            "CREATE TABLE remote_agent_codebases "
            "(id TEXT PRIMARY KEY, name TEXT, default_cwd TEXT, kind TEXT)"
        )
        registry_conn.execute(
            "INSERT INTO remote_agent_codebases (id, name, default_cwd, kind) "
            "VALUES ('cb-test', 'owner/repo', ?, 'repo')",
            (str(talk_tools._repo_root()),),
        )
    registry_conn.close()
    monkeypatch.setenv("TALK_ARCHON_DB", str(registry))
    monkeypatch.delenv("HOMIE_KILLSWITCH_ARCHON_DISPATCH", raising=False)
    monkeypatch.setattr(talk_tools.time, "sleep", lambda _s: None)

    calls: list[dict] = []

    async def fake_dispatch(codebase_id, workflow, text, *, client=None):
        calls.append({"codebase_id": codebase_id, "workflow": workflow, "text": text})
        return _Dispatch()

    monkeypatch.setattr(archon_client, "dispatch_workflow", fake_dispatch)
    monkeypatch.setattr(
        talk_archon, "run_id_for_conversation", lambda _cid, **_k: "run-9"
    )

    return {
        "calls": calls,
        "ledger": lambda: ConvoyService(OrchestrationDB(ledger_path)),
        "audit": tmp_path / "archon_dispatch.jsonl",
    }


def _audit_outcomes(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [json.loads(line)["outcome"] for line in path.read_text(encoding="utf-8").splitlines()]


def test_resolve_workflow_matches_exactly_and_fuzzily() -> None:
    assert talk_tools.resolve_workflow("archon-clutch") == "archon-clutch"
    assert talk_tools.resolve_workflow("clutch") == "archon-clutch"


def test_resolve_workflow_miss_suggests_close_names() -> None:
    with pytest.raises(talk_tools.TalkToolError) as excinfo:
        talk_tools.resolve_workflow("archon-clutsh")

    assert "no Archon workflow named" in str(excinfo.value)
    assert "Did you mean" in str(excinfo.value)


def test_run_archon_needs_a_workflow_and_a_brief() -> None:
    assert "needs a workflow" in talk_tools.execute_talk_tool("run_archon", {"brief": "x"})
    assert "needs a brief" in talk_tools.execute_talk_tool(
        "run_archon", {"workflow": "archon-clutch"}
    )


def test_run_archon_dispatches_through_the_client_and_keys_the_ledger(
    archon_lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3 + the correlation key, proven on the ROW rather than the reply."""

    monkeypatch.setattr(
        talk_tools,
        "_archon_run_row",
        lambda _db, _rid: {"status": "completed", "working_path": "C:/wt/clutch"},
    )

    receipt = _deploy_archon({"workflow": "clutch", "brief": BRIEF})
    run = _wait_for_run(_run_id_from(receipt))
    run_id = _run_id_from(receipt)

    assert "kind=archon (archon-clutch)" in receipt
    assert run["status"] == "done"
    assert "finished with status completed" in run["output"]
    assert "C:/wt/clutch" in run["output"]
    # F3: the workflow went through the client with the verbatim brief.
    assert archon_lane["calls"] == [
        {"codebase_id": "cb-test", "workflow": "archon-clutch", "text": BRIEF}
    ]
    # Correlation key on the registry entry...
    expected_ref = f"talk:{run_id}:archon:conv-db-1:conv:web-1785-abc"
    assert run["meta"]["correlation_ref"] == expected_ref
    assert run["meta"]["archon_conversation_id"] == "web-1785-abc"
    # ...and on the ledger row, which is what #257/#258 actually join on.
    subtask = archon_lane["ledger"]().get_convoy(1).subtasks[0]
    assert subtask.paperclip_issue_id == expected_ref
    assert subtask.status == "completed"
    assert talk_archon.parse_correlation_ref(subtask.paperclip_issue_id) == {
        "run_id": run_id,
        "conversation_db_id": "conv-db-1",
        "conversation_id": "web-1785-abc",
    }
    assert _audit_outcomes(archon_lane["audit"]) == ["granted", "dispatched"]


def test_run_archon_refuses_a_vague_brief_without_dispatching(archon_lane) -> None:
    """The F2 lock at the tool boundary: nothing reaches the client."""

    output = talk_tools.execute_talk_tool(
        "run_archon", {"workflow": "clutch", "brief": "yeah do that"}
    )

    assert "never sees this conversation" in output
    assert "WORK_STARTED" not in output
    assert archon_lane["calls"] == []
    assert _audit_outcomes(archon_lane["audit"]) == ["refused_vague_brief"]


def test_run_archon_refuses_when_the_kill_switch_is_off(
    archon_lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The switch stops the deploy, and the refusal keeps its identity.

    `KillSwitchDisabled` propagates out of the gate (house contract), so the
    tool surface reports it rather than dispatching.
    """

    from security import kill_switches

    monkeypatch.setenv("HOMIE_KILLSWITCH_ARCHON_DISPATCH", "disabled")

    with pytest.raises(kill_switches.KillSwitchDisabled) as excinfo:
        talk_tools.start_archon_run("clutch", BRIEF)

    assert excinfo.value.switch_name == talk_archon.KILL_SWITCH
    assert archon_lane["calls"] == []


def test_run_archon_speaks_an_unreachable_archon_and_fails_the_row(
    archon_lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unreachable(*args, **kwargs):
        raise archon_client.ArchonUnreachableError()

    monkeypatch.setattr(archon_client, "dispatch_workflow", unreachable)

    receipt = _deploy_archon({"workflow": "clutch", "brief": BRIEF})
    run = _wait_for_run(_run_id_from(receipt))

    assert run["status"] == "failed"
    assert "could not deploy archon-clutch" in run["output"]
    assert "not reachable" in run["output"]
    assert archon_lane["ledger"]().get_convoy(1).subtasks[0].status == "failed"
    assert _audit_outcomes(archon_lane["audit"]) == ["granted", "failed"]


def test_run_archon_marks_the_row_failed_when_the_run_fails(
    archon_lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed Archon run is not a completed task — the ledger must say so."""

    monkeypatch.setattr(
        talk_tools, "_archon_run_row", lambda _db, _rid: {"status": "failed"}
    )

    receipt = _deploy_archon({"workflow": "clutch", "brief": BRIEF})
    run = _wait_for_run(_run_id_from(receipt))

    assert "finished with status failed" in run["output"]
    assert archon_lane["ledger"]().get_convoy(1).subtasks[0].status == "failed"


def test_run_archon_gives_up_watching_after_the_budget(
    archon_lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(talk_tools, "_archon_run_row", lambda _db, _rid: {"status": "running"})
    monkeypatch.setenv("TALK_ARCHON_BUDGET_S", "0")

    receipt = _deploy_archon({"workflow": "clutch", "brief": BRIEF})
    run = _wait_for_run(_run_id_from(receipt))

    assert run["status"] == "done"
    assert "stopped watching" in run["output"]
    assert "keeps running" in run["output"]


def test_run_archon_survives_a_run_that_never_registers(
    archon_lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dispatch landed; Archon just has not written the run row yet."""

    monkeypatch.setattr(talk_archon, "run_id_for_conversation", lambda _cid, **_k: None)
    monkeypatch.setenv("TALK_ARCHON_BUDGET_S", "0")

    receipt = _deploy_archon({"workflow": "clutch", "brief": BRIEF})
    run = _wait_for_run(_run_id_from(receipt))

    assert run["status"] == "done"
    assert "conversation web-1785-abc" in run["output"]
    # The dispatch itself still succeeded, so the row is claimed and running.
    assert archon_lane["ledger"]().get_convoy(1).subtasks[0].status == "running"


def test_an_unencodable_archon_id_fails_the_run_instead_of_losing_the_join(
    archon_lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex R3 major: the join is load-bearing, so losing it is not a success.

    Earlier this degraded to a legacy `talk:<run_id>` ref and reported the run
    as done — a run #257/#258/#259 could never steer, presented as fine. Now
    both raw ids land in the durable audit trail and the run ends failed.
    """

    class _Colon(_Dispatch):
        conversation_db_id = "conv:with:colons"

    async def odd_dispatch(*args, **kwargs):
        return _Colon()

    monkeypatch.setattr(archon_client, "dispatch_workflow", odd_dispatch)
    monkeypatch.setattr(
        talk_tools, "_archon_run_row", lambda _db, _rid: {"status": "completed"}
    )

    receipt = _deploy_archon({"workflow": "clutch", "brief": BRIEF})
    run = _wait_for_run(_run_id_from(receipt))

    assert run["status"] == "failed"
    assert "cannot join this run" in run["output"]
    rows = [
        json.loads(line)
        for line in archon_lane["audit"].read_text(encoding="utf-8").splitlines()
    ]
    lost = [r for r in rows if r["outcome"] == "correlation_unpersisted"]
    assert len(lost) == 1
    assert lost[0]["conversation_db_id"] == "conv:with:colons"
    assert lost[0]["conversation_id"] == "web-1785-abc"


def test_a_broken_work_queue_refuses_the_deploy(
    archon_lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex R2 major: the convoy row is the correlation key's specified home.

    Earlier this test blessed the opposite — deploy anyway, keep a receipt.
    But the row is what #257/#258/#259 join on, and `talk_runs` is in-memory,
    so a dispatch with no row leaves a running Archon worker nothing can
    join to after an API restart. The row is now a PRECONDITION: no ledger,
    no dispatch, and nothing reaches the client.
    """

    def broken():
        raise RuntimeError("db locked")

    monkeypatch.setattr(talk_tools, "_convoy_service", broken)

    output = _deploy_archon({"workflow": "clutch", "brief": BRIEF})

    assert "could not create the work-queue row" in output
    assert "WORK_STARTED" not in output
    assert archon_lane["calls"] == []
    outcomes = _audit_outcomes(archon_lane["audit"])
    assert outcomes[-1] == "refused_no_ledger_row"


def test_a_ledger_that_drops_the_correlation_key_fails_the_run_loudly(
    archon_lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dispatch already happened, so the run reports the lost join instead of success."""

    monkeypatch.setattr(
        talk_tools, "_archon_run_row", lambda _db, _rid: {"status": "completed"}
    )

    real_service = talk_tools._convoy_service

    class _DropsTheKey:
        """Real service for row creation; refuses the correlation write."""

        def __init__(self):
            self._inner = real_service()

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def dispatch_subtask(self, *_a, **_k):
            raise RuntimeError("db locked")

    monkeypatch.setattr(talk_tools, "_convoy_service", _DropsTheKey)

    receipt = _deploy_archon({"workflow": "clutch", "brief": BRIEF})
    run = _wait_for_run(_run_id_from(receipt))

    assert run["status"] == "failed"
    assert "never took the correlation key" in run["output"]
    # The join survives in the durable audit trail, so a re-key is mechanical.
    rows = [
        json.loads(line)
        for line in archon_lane["audit"].read_text(encoding="utf-8").splitlines()
    ]
    unpersisted = [r for r in rows if r["outcome"] == "correlation_unpersisted"]
    assert len(unpersisted) == 1
    assert unpersisted[0]["run_id"] == _run_id_from(receipt)
    assert unpersisted[0]["conversation_id"] == "web-1785-abc"
    assert unpersisted[0]["conversation_db_id"] == "conv-db-1"


# ─── computer ────────────────────────────────────────────────────────────


def test_computer_and_browse_refuse_when_the_kill_switch_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOMIE_KILLSWITCH_COMPUTER_USE", "disabled")

    assert "switched off by the operator" in talk_tools.execute_talk_tool(
        "computer", {"action": "notify", "text": "hi"}
    )
    assert "switched off by the operator" in talk_tools.execute_talk_tool(
        "browse", {"action": "status"}
    )


def test_computer_queues_desktop_actions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue = tmp_path / "commands.jsonl"
    monkeypatch.setenv("TALK_DESKTOP_QUEUE", str(queue))
    monkeypatch.setattr(talk_computer, "ensure_desktop_agent", lambda _p: True)

    assert "Running 'npm test'" in talk_tools.execute_talk_tool(
        "computer", {"action": "run_command", "text": "npm test"}
    )
    assert "Opened https://YourProduct.com" in talk_tools.execute_talk_tool(
        "computer", {"action": "open_url", "url": "https://YourProduct.com"}
    )

    lines = [json.loads(line) for line in queue.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["action"] == "run" and lines[0]["command"] == "npm test"
    assert lines[1]["action"] == "open-url"


def test_open_terminal_defaults_to_the_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue = tmp_path / "commands.jsonl"
    monkeypatch.setenv("TALK_DESKTOP_QUEUE", str(queue))
    monkeypatch.setattr(talk_computer, "ensure_desktop_agent", lambda _p: True)

    talk_tools.execute_talk_tool("computer", {"action": "open_terminal"})

    queued = json.loads(queue.read_text(encoding="utf-8").splitlines()[0])
    assert queued["action"] == "run"
    # Assert the resolved repo root, not a hardcoded directory name — the same
    # code runs from an Archon worktree, where the checkout is not "thehomie".
    assert queued["command"] == f'cd /d "{talk_tools._repo_root()}"'


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"action": "run_command"}, "needs the command"),
        ({"action": "open_url"}, "needs a URL"),
        ({"action": "open_file"}, "needs a file path"),
        ({"action": "notify"}, "needs a message"),
        ({"action": "type_into_window", "text": "hi"}, "needs part of the window's title"),
        ({"action": "type_into_window", "window_title": "claude"}, "needs the text"),
        ({"action": "press_keys"}, "needs a key or combo"),
        ({"action": "teleport"}, "isn't a computer action"),
    ],
)
def test_computer_missing_arguments_are_speakable(
    arguments: dict, expected: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TALK_DESKTOP_QUEUE", str(tmp_path / "commands.jsonl"))
    monkeypatch.setattr(talk_computer, "ensure_desktop_agent", lambda _p: True)

    assert expected in talk_tools.execute_talk_tool("computer", arguments)


def test_computer_types_into_a_window(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        talk_computer,
        "type_into_window",
        lambda title, text, press_enter=True: (
            captured.update(title=title, text=text, enter=press_enter),
            "Typed 5 characters into 'x'.",
        )[1],
    )

    output = talk_tools.execute_talk_tool(
        "computer",
        {
            "action": "type_into_window",
            "window_title": "claude",
            "text": "hello",
            "press_enter": False,
        },
    )

    assert captured == {"title": "claude", "text": "hello", "enter": False}
    assert "Typed 5 characters" in output


def test_computer_defaults_to_pressing_enter(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        talk_computer,
        "type_into_window",
        lambda title, text, press_enter=True: (captured.update(enter=press_enter), "ok")[1],
    )

    talk_tools.execute_talk_tool(
        "computer", {"action": "type_into_window", "window_title": "claude", "text": "go"}
    )

    assert captured["enter"] is True


def test_computer_surfaces_a_gui_error_as_speech(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):
        raise talk_computer.ComputerError("no window matching 'ghost'. Open windows include: A.")

    monkeypatch.setattr(talk_computer, "type_into_window", boom)

    output = talk_tools.execute_talk_tool(
        "computer", {"action": "type_into_window", "window_title": "ghost", "text": "hi"}
    )

    assert "no window matching 'ghost'" in output


def test_computer_press_keys_and_click(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(talk_computer, "press_keys", lambda keys, title: f"Pressed {keys}.")
    monkeypatch.setattr(
        talk_computer, "click", lambda x, y, title: f"Clicked at {x}, {y} / {title}."
    )

    assert "Pressed ctrl+c" in talk_tools.execute_talk_tool(
        "computer", {"action": "press_keys", "keys": "ctrl+c"}
    )
    assert "Clicked at 10, 20" in talk_tools.execute_talk_tool(
        "computer", {"action": "click", "x": 10, "y": 20}
    )


def test_look_at_screen_starts_an_async_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(talk_computer, "capture_screen", lambda: tmp_path / "shot.png")
    monkeypatch.setattr(talk_computer, "describe_screen", lambda _png: "A dashboard with a chart.")

    receipt = talk_tools.execute_talk_tool("computer", {"action": "look_at_screen"})
    run = _wait_for_run(_run_id_from(receipt))

    assert "kind=look (screen)" in receipt
    assert "thirty seconds" in receipt
    assert run["status"] == "done"
    assert run["output"] == "A dashboard with a chart."


def test_look_at_screen_capture_failure_is_speakable(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom():
        raise talk_computer.ComputerError("could not capture the screen: session locked")

    monkeypatch.setattr(talk_computer, "capture_screen", boom)

    assert "session locked" in talk_tools.execute_talk_tool(
        "computer", {"action": "look_at_screen"}
    )


# ─── browse ──────────────────────────────────────────────────────────────


def test_browse_maps_actions_to_the_gated_router_command(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list = []
    monkeypatch.setattr(
        talk_tools,
        "_handle_homie_command",
        lambda args: (captured.append(args), "browser says ok")[1],
    )

    talk_tools.execute_talk_tool("browse", {"action": "status"})
    talk_tools.execute_talk_tool("browse", {"action": "snapshot"})
    talk_tools.execute_talk_tool("browse", {"action": "open", "url": "https://YourProduct.com"})

    assert [call["args"] for call in captured] == [
        "status",
        "snapshot",
        "open https://YourProduct.com",
    ]
    assert all(call["command"] == "browser" for call in captured)


def test_browse_open_needs_a_url() -> None:
    assert "needs an absolute URL" in talk_tools.execute_talk_tool("browse", {"action": "open"})


def test_browse_rejects_unknown_actions() -> None:
    assert "isn't a browser action" in talk_tools.execute_talk_tool("browse", {"action": "hack"})


# ─── check_work ──────────────────────────────────────────────────────────


@pytest.fixture
def no_archon_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the ledger readers at a path that does not exist.

    Without this the ro reads fall through to the OPERATOR'S live
    ``~/.archon/archon.db`` — which is how a paused real run leaked into
    "nothing is running". Stubbing each reader hid that; a missing ledger is
    the honest empty state and covers every reader at once.
    """

    monkeypatch.setenv("TALK_ARCHON_DB", str(tmp_path / "absent-archon.db"))


def test_check_work_reports_nothing_running(
    no_archon_ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(talk_tools, "_voice_subtask_lines", lambda limit=5: [])

    assert "Nothing is running" in talk_tools.execute_talk_tool("check_work", {})


def test_check_work_lists_runs_archon_rows_and_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = talk_runs.start_run("agent", "audit", lambda _r: "done")
    _wait_for_run(run_id)
    monkeypatch.setattr(
        talk_tools,
        "recent_archon_runs",
        lambda limit=5: [
            {
                "workflow_name": "archon-clutch",
                "status": "running",
                "started_at": "2026-07-27 06:00:00",
            }
        ],
    )
    monkeypatch.setattr(
        talk_tools, "_voice_subtask_lines", lambda limit=5: ["- task #7 [running]: x"]
    )

    output = talk_tools.execute_talk_tool("check_work", {})

    assert f"#{run_id} agent 'audit'" in output
    assert "archon-clutch (running)" in output
    assert "task #7" in output


def test_check_work_detail_for_one_run() -> None:
    run_id = talk_runs.start_run("skill", "vault-ops", lambda _r: "3 notes updated")
    _wait_for_run(run_id)

    output = talk_tools.execute_talk_tool("check_work", {"run_id": run_id})

    assert f"Run #{run_id}" in output
    assert "3 notes updated" in output


def test_check_work_reads_archon_state_live(
    no_archon_ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 2: an archon run's status comes from the DB row, not the registry.

    The wording is #259's narration ("Run <id> (<workflow>): <status>, …"); the
    invariant under test is unchanged — the registry says nothing about status,
    and every field here comes from the patched ledger row.
    """

    run_id = talk_runs.start_run("archon", "archon-clutch", lambda _r: "watched")
    _wait_for_run(run_id)
    talk_runs.annotate_run(run_id, archon_run_id="run-9")
    monkeypatch.setattr(
        talk_tools,
        "_archon_run_row",
        lambda _db, _rid: {
            "status": "failed",
            "workflow_name": "archon-clutch",
            "working_path": "C:/wt/x",
        },
    )

    output = talk_tools.execute_talk_tool("check_work", {"run_id": run_id})

    assert f"Run #{run_id}" in output
    assert "archon-clutch): failed" in output
    assert "C:/wt/x" in output


def test_check_work_unknown_run_id() -> None:
    assert "don't have a run #99999" in talk_tools.execute_talk_tool(
        "check_work", {"run_id": 99999}
    )


def test_check_work_rejects_a_non_numeric_run_id() -> None:
    assert "numeric run id" in talk_tools.execute_talk_tool("check_work", {"run_id": "latest"})


def test_check_work_survives_a_broken_ledger_read(
    no_archon_ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom():
        raise RuntimeError("db locked")

    monkeypatch.setattr(talk_tools, "_convoy_service", boom)

    assert "Nothing is running" in talk_tools.execute_talk_tool("check_work", {})
