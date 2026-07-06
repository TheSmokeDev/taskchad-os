"""US-005 — run_pass skeleton + US-011 — the deterministic per-project pipeline.

US-005 paths (shell):
  - kill switch disabled: refused + counted, exit 0, discovery never runs,
    and it outranks the COFOUNDER_ENABLED gate (gate-order proof)
  - COFOUNDER_ENABLED=false: quiet disabled no-op, exit 0, discovery never runs
  - pass lock held by another pass: quiet locked exit, no state writes
  - --test dry run: full discovery, but zero writes (state file absent,
    project file bytes + mtime untouched)
  - real (non-dry) pass: stamps pass-level last_pass_at in state under the
    already-held lock (the caller-holds-lock seam; proves no self-deadlock)
  - --project restricts to one slug; unknown slug warns and matches nothing
  - a raising pass body is contained as a PassResult error (exit code 1),
    never an exception to the caller
  - `python -m cofounder.run_pass --test` is a real CLI entry (subprocess)

US-011 gate paths (pipeline), one test each, adversarial first:
  - caps trip ONCE to awaiting-human (single recorded notify event)
  - caps wall-clock math survives mixed naive/aware timestamps (tz fold)
  - running gate: one small note, decide never called, dispatch impossible
  - running + new steering: decide runs but the dispatch wish is REFUSED
  - completion green -> done + archive to done/ (disk-level proof)
  - completion green + subjective_gate -> awaiting-human park (no archive)
  - completion failing twice -> blocked (state-json streak AND frontmatter)
  - concurrency cap holds: excess new project queued, no LLM call burned
  - non-enum status ("in_progress") flows as active and is recoverable
  - confirmed dispatch re-stamps all machine state in code
  - unconfirmed dispatch = failed attempt, NO phantom `building`
  - decider-less pass logs decision-pending and writes nothing
  - unknown decision action is a no-op
  - steering consumed exactly once (reply cursor advances)
  - zombie classified against the prior snapshot -> recovery wired
  - dry run calls decide but executes nothing (decide-without-dispatch)
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

import config
from cofounder import engine_archon, project_model
from cofounder import notify as cofounder_notify
from cofounder import orchestrate as cofounder_orchestrate
from cofounder import repos as repos_mod
from cofounder import state as state_mod
from cofounder.repos import RepoResolution
from cofounder.run_pass import (
    MERGE_POLICY_INSTRUCTION,
    OUTCOME_COMPLETED,
    OUTCOME_DISABLED,
    OUTCOME_ERROR,
    OUTCOME_LOCKED,
    OUTCOME_REFUSED,
    PROJECT_AWAITING_VERDICT,
    PROJECT_BLOCKED,
    PROJECT_CAPS_TRIPPED,
    PROJECT_CHECK_FAILED,
    PROJECT_DECIDED,
    PROJECT_DECIDED_DRY,
    PROJECT_DECISION_NOOP,
    PROJECT_DECISION_PENDING,
    PROJECT_DISPATCH_FAILED,
    PROJECT_DISPATCH_REFUSED,
    PROJECT_DISPATCHED,
    PROJECT_DONE,
    PROJECT_PARKED,
    PROJECT_QUEUED,
    PROJECT_RUNNING,
    PROJECT_ZOMBIE_RECOVERED,
    PassResult,
    main,
    run_pass,
)
from orchestration import observability
from security import kill_switches
from shared import file_lock

SCRIPTS_DIR = Path(__file__).resolve().parents[1]

COFOUNDER_ENV_KEYS = (
    "COFOUNDER_ENABLED",
    "COFOUNDER_PROJECTS_DIR",
    "COFOUNDER_MAX_ITERATIONS",
    "COFOUNDER_MAX_WALL_CLOCK_HOURS",
    "COFOUNDER_MAX_CONCURRENT",
    "COFOUNDER_NOTIFY_LEVELS",
    "COFOUNDER_ZOMBIE_STALE_MINUTES",
    "COFOUNDER_ARCHON_DB",
    "COFOUNDER_WORKFLOW_PROVIDER",
    "COFOUNDER_WORKFLOW_MODEL",
)

# Subset of the verified live archon.db DDL (same as test_cofounder_engine).
RUNS_DDL = """
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
    started_at TEXT DEFAULT (datetime('now')),
    completed_at TEXT,
    last_activity_at TEXT DEFAULT (datetime('now')),
    working_path TEXT,
    user_id TEXT
)
"""

_RUNS_INSERT = (
    "INSERT INTO remote_agent_workflow_runs (id, conversation_id, workflow_name,"
    " user_message, status, started_at, completed_at, last_activity_at, working_path)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


@pytest.fixture(autouse=True)
def clear_cofounder_env(monkeypatch, tmp_path):
    """Each test starts with no COFOUNDER_*/kill-switch env (.env may set them).

    Langfuse is pinned OFF (US-013 wraps every project pass in the
    cofounder_pass span; a live .env pointing at a dead server costs ~4s of
    OTEL retries per span) and the disabled-path observation jsonl is
    redirected to tmp so tests never append to the real repo's .omx log.
    """
    for key in COFOUNDER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("HOMIE_KILLSWITCH_COFOUNDER", raising=False)
    monkeypatch.setenv("LANGFUSE_ENABLED", "false")
    monkeypatch.setattr(observability, "_OBS_LOG", tmp_path / "obs" / "obs.jsonl")
    # US-017 wired the gated Telegram sender as the run_pass default notify;
    # stub the module attribute (the call-time resolution seam) so a test
    # without notify= can never reach real HTTP — the operator .env carries
    # live TELEGRAM_* creds via load_dotenv(override=True).
    monkeypatch.setattr(
        cofounder_notify, "notify", lambda project, text, level: False
    )
    # US-020 wired orchestrate.decide as the run_pass default decider; pin it
    # back to None (decision-pending semantics) so a test without decide= can
    # never reach a live LLM. The ship-gate suite covers the real wiring.
    monkeypatch.setattr(cofounder_orchestrate, "decide", None)
    yield


@pytest.fixture(autouse=True)
def reset_counters():
    kill_switches._REFUSAL_COUNTERS.clear()
    kill_switches._AUDIT_WRITE_FAILURES.clear()
    yield
    kill_switches._REFUSAL_COUNTERS.clear()
    kill_switches._AUDIT_WRITE_FAILURES.clear()


@pytest.fixture()
def projects_dir(tmp_path):
    pdir = tmp_path / "cofounder"
    pdir.mkdir()
    return pdir


def make_project(
    projects_dir: Path,
    slug: str,
    *,
    status: str = "new",
    activity: tuple[str, ...] = ("- 2026-07-03T08:00:00 created",),
    **fm_overrides,
) -> Path:
    fm = {"tags": ["system", "cofounder"], "status": status}
    fm.update(fm_overrides)
    body = (
        f"# {slug}\n\n"
        "## Spec (STATIC - orchestrator MUST NOT rewrite; only the operator edits)\n"
        f"Build {slug}.\n\n"
        "## Plan / Working Memory (MUTABLE - orchestrator may rewrite)\n"
        f"- [ ] plan {slug}\n\n"
        "## Activity Log (APPEND-ONLY - newest at the bottom)\n"
        + "\n".join(activity)
        + "\n"
    )
    text = "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n" + body
    path = projects_dir / f"{slug}.md"
    path.write_text(text, encoding="utf-8")
    return path


def enabled_settings(projects_dir: Path, **overrides):
    return config.get_cofounder_settings(
        enabled=True, projects_dir=projects_dir, **overrides
    )


def make_archon_db(path: Path, rows) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.execute(RUNS_DDL)
        connection.executemany(_RUNS_INSERT, rows)
        connection.commit()
    finally:
        connection.close()
    return path


def run_row(
    run_id: str,
    status: str,
    working_path,
    *,
    last_activity: str | None = None,
    started: str = "2026-07-04 00:00:00",
    completed: str | None = None,
):
    """One archon.db fixture row in the live naive-UTC clock domain."""
    if last_activity is None:
        last_activity = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    return (
        run_id,
        "conv",
        "wf",
        "msg",
        status,
        started,
        completed,
        last_activity,
        str(working_path) if working_path is not None else None,
    )


class DiscoveryRecorder:
    """Stand-in for discover_projects that records whether it was called."""

    def __init__(self):
        self.calls = 0

    def __call__(self, projects_dir):
        self.calls += 1
        return []


# === gate: kill switch (checked FIRST) ===


def test_kill_switch_refuses_counts_and_skips_discovery(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMIE_KILLSWITCH_COFOUNDER", "disabled")
    recorder = DiscoveryRecorder()
    monkeypatch.setattr(project_model, "discover_projects", recorder)

    result = run_pass(settings=enabled_settings(tmp_path), state_file=tmp_path / "s.json")

    assert result.outcome == OUTCOME_REFUSED
    assert result.exit_code == 0
    assert kill_switches.get_refusal_counters()["cofounder"] == 1
    assert recorder.calls == 0
    assert not (tmp_path / "s.json").exists()


def test_gate_order_kill_switch_outranks_enabled_gate(monkeypatch):
    """Kill switch disabled AND COFOUNDER_ENABLED=false -> refused, not disabled."""
    monkeypatch.setenv("HOMIE_KILLSWITCH_COFOUNDER", "disabled")
    result = run_pass()
    assert result.outcome == OUTCOME_REFUSED
    assert kill_switches.get_refusal_counters()["cofounder"] == 1


def test_refused_cli_exit_code_is_zero(monkeypatch):
    monkeypatch.setenv("HOMIE_KILLSWITCH_COFOUNDER", "disabled")
    assert main([]) == 0


# === gate: COFOUNDER_ENABLED ===


def test_disabled_is_quiet_noop_exit_zero(monkeypatch):
    recorder = DiscoveryRecorder()
    monkeypatch.setattr(project_model, "discover_projects", recorder)

    result = run_pass()  # env cleared -> enabled defaults false

    assert result.outcome == OUTCOME_DISABLED
    assert result.exit_code == 0
    assert recorder.calls == 0
    assert kill_switches.get_refusal_counters() == {}
    assert main([]) == 0


# === re-entrancy: pass lock on cofounder-state.json ===


def test_lock_held_quiet_exit_no_state_writes(projects_dir, tmp_path):
    make_project(projects_dir, "alpha")
    state_file = tmp_path / "cofounder-state.json"

    with file_lock(state_file, timeout=1.0):  # another pass holds the lock
        result = run_pass(settings=enabled_settings(projects_dir), state_file=state_file)

    assert result.outcome == OUTCOME_LOCKED
    assert result.exit_code == 0
    assert result.projects_seen == ()
    assert not state_file.exists()


# === --test dry run: zero writes ===


def test_dry_run_discovers_but_writes_nothing(projects_dir, tmp_path, caplog):
    project_path = make_project(projects_dir, "alpha")
    state_file = tmp_path / "cofounder-state.json"
    before_bytes = project_path.read_bytes()
    before_mtime = project_path.stat().st_mtime_ns

    with caplog.at_level(logging.INFO, logger="cofounder.run_pass"):
        result = run_pass(
            dry_run=True, settings=enabled_settings(projects_dir), state_file=state_file
        )

    assert result.outcome == OUTCOME_COMPLETED
    assert result.dry_run is True
    assert result.projects_seen == ("alpha",)
    assert any("[dry-run]" in r.message and "alpha" in r.message for r in caplog.records)
    # zero writes: no state file, project file byte- and mtime-identical
    assert not state_file.exists()
    assert project_path.read_bytes() == before_bytes
    assert project_path.stat().st_mtime_ns == before_mtime


def test_cli_test_flag_maps_to_dry_run(monkeypatch, projects_dir, tmp_path):
    make_project(projects_dir, "alpha")
    monkeypatch.setenv("COFOUNDER_ENABLED", "true")
    monkeypatch.setenv("COFOUNDER_PROJECTS_DIR", str(projects_dir))
    state_dir = tmp_path / "state"
    monkeypatch.setattr(config, "STATE_DIR", state_dir)

    assert main(["--test"]) == 0
    assert not (state_dir / "cofounder-state.json").exists()


# === real pass: stamps last_pass_at under the held lock (no self-deadlock) ===


def test_real_pass_stamps_last_pass_at_and_preserves_projects(projects_dir, tmp_path):
    make_project(projects_dir, "alpha")
    state_file = tmp_path / "cofounder-state.json"
    state_mod.update_project_state("alpha", state_file, fail_streak=2)

    result = run_pass(settings=enabled_settings(projects_dir), state_file=state_file)

    assert result.outcome == OUTCOME_COMPLETED
    assert result.projects_seen == ("alpha",)
    state = state_mod.load_state(state_file)
    assert state["last_pass_at"]  # ISO stamp written by the pass
    assert state["projects"]["alpha"]["fail_streak"] == 2  # existing data intact
    assert not state_file.with_suffix(".json.tmp").exists()


# === --project restriction ===


def test_project_flag_restricts_to_one_slug(projects_dir, tmp_path):
    make_project(projects_dir, "alpha")
    make_project(projects_dir, "beta")

    result = run_pass(
        only_project="beta",
        settings=enabled_settings(projects_dir),
        state_file=tmp_path / "s.json",
    )

    assert result.outcome == OUTCOME_COMPLETED
    assert result.projects_seen == ("beta",)


def test_project_flag_unknown_slug_warns_and_matches_nothing(projects_dir, tmp_path, caplog):
    make_project(projects_dir, "alpha")

    with caplog.at_level(logging.WARNING, logger="cofounder.run_pass"):
        result = run_pass(
            only_project="no-such",
            settings=enabled_settings(projects_dir),
            state_file=tmp_path / "s.json",
        )

    assert result.outcome == OUTCOME_COMPLETED
    assert result.projects_seen == ()
    assert any("no-such" in r.message for r in caplog.records)


# === no exception escapes ===


def test_raising_pass_body_is_contained_as_error_result(monkeypatch, projects_dir, tmp_path):
    def boom(_projects_dir):
        raise RuntimeError("boom")

    monkeypatch.setattr(project_model, "discover_projects", boom)
    state_file = tmp_path / "s.json"

    result = run_pass(settings=enabled_settings(projects_dir), state_file=state_file)

    assert isinstance(result, PassResult)
    assert result.outcome == OUTCOME_ERROR
    assert result.exit_code == 1
    assert "RuntimeError: boom" in result.error
    assert not state_file.exists()  # died before the stamp; nothing written


# === CLI module entry ===


def test_module_cli_entry_runs_and_exits_zero():
    """`python -m cofounder.run_pass --test` is a real entry point.

    .env (load_dotenv override=True) may legitimately flip COFOUNDER_* for the
    operator, so this asserts the contract that holds in every quiet outcome:
    exit 0, no traceback. --test guarantees no writes even if enabled.
    """
    env = os.environ.copy()
    env["COFOUNDER_ENABLED"] = "false"
    env.pop("HOMIE_KILLSWITCH_COFOUNDER", None)
    proc = subprocess.run(
        [sys.executable, "-m", "cofounder.run_pass", "--test"],
        cwd=SCRIPTS_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Traceback" not in proc.stderr


# =============================================================================
# US-011 — the deterministic per-project pipeline.
# =============================================================================


class DecideStub:
    """Injected decide() stand-in: records calls, returns a canned decision."""

    def __init__(self, decision=None):
        self.decision = decision
        self.calls = []

    def __call__(self, project, context):
        self.calls.append((project.slug, dict(context)))
        return self.decision


class NotifyRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, project, text, level):
        self.calls.append((project.slug, text, level))
        return True


class DispatchRecorder:
    """Stand-in for engine_archon.dispatch returning a canned receipt."""

    def __init__(self, run_id="new-run"):
        self.run_id = run_id
        self.calls = []

    def __call__(self, workflow, branch, message, repo_path, **kwargs):
        self.calls.append(
            {
                "workflow": workflow,
                "branch": branch,
                "message": message,
                "repo_path": repo_path,
                **kwargs,
            }
        )
        return engine_archon.DispatchResult(
            run_id=self.run_id,
            pid=4242,
            argv=("archon",),
            log_path=Path("dispatch.log"),
            dispatched_at="2026-07-04 00:00:00",
        )


def parse(path: Path):
    return project_model.parse_project_file(path)


def project_entry(state_file: Path, slug: str) -> dict:
    return state_mod.get_project_state(state_mod.load_state(state_file), slug)


# === caps: trip ONCE to awaiting-human, single recorded notify event ===


def test_caps_iterations_trip_once_to_awaiting_human(projects_dir, tmp_path):
    path = make_project(
        projects_dir, "capped", status="building", iterations=5, max_iterations=5
    )
    state_file = tmp_path / "cofounder-state.json"
    notify = NotifyRecorder()

    first = run_pass(
        settings=enabled_settings(projects_dir), state_file=state_file, notify=notify
    )

    assert first.project_outcomes["capped"] == PROJECT_CAPS_TRIPPED
    parsed = parse(path)
    assert parsed.frontmatter.status == "awaiting-human"  # disk-level proof
    assert parsed.activity_log.count("[caps]") == 1
    assert notify.calls == [
        ("capped", "capped: iterations 5 >= max_iterations 5; parked awaiting human",
         "awaiting-human"),
    ]
    assert "awaiting-human:caps" in project_entry(state_file, "capped")["notified"]

    second = run_pass(
        settings=enabled_settings(projects_dir), state_file=state_file, notify=notify
    )

    assert second.project_outcomes["capped"] == PROJECT_PARKED
    assert len(notify.calls) == 1  # notified once, not per pass
    assert parse(path).activity_log.count("[caps]") == 1


@pytest.mark.parametrize(
    ("label", "hours_ago", "naive", "expect_trip"),
    [
        ("naive-exceeded", 100, True, True),
        ("aware-exceeded", 100, False, True),
        ("naive-fresh", 2, True, False),
    ],
)
def test_caps_wall_clock_mixed_tz_never_crashes(
    projects_dir, tmp_path, label, hours_ago, naive, expect_trip
):
    """The reference build crashed comparing naive vs aware timestamps; the
    pipeline folds both sides into aware-UTC so either stored form works."""
    path = make_project(projects_dir, "clocked", status="building")
    state_file = tmp_path / "cofounder-state.json"
    start = datetime.now(UTC) - timedelta(hours=hours_ago)
    if naive:
        start = start.replace(tzinfo=None)
    state_mod.update_project_state(
        "clocked", state_file, wall_clock_start=start.isoformat()
    )

    result = run_pass(settings=enabled_settings(projects_dir), state_file=state_file)

    assert result.outcome == OUTCOME_COMPLETED  # never a crash
    if expect_trip:
        assert result.project_outcomes["clocked"] == PROJECT_CAPS_TRIPPED
        assert parse(path).frontmatter.status == "awaiting-human"
    else:
        assert result.project_outcomes["clocked"] == PROJECT_DECISION_PENDING
        assert parse(path).frontmatter.status == "building"


# === the hard gate: NEVER dispatch while a job runs ===


def test_running_gate_appends_one_note_and_never_decides(
    projects_dir, tmp_path, monkeypatch
):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    db = make_archon_db(
        tmp_path / "archon.db", [run_row("run-1", "running", worktree)]
    )
    path = make_project(
        projects_dir, "inflight", status="building", current_job_id="run-1"
    )
    state_file = tmp_path / "cofounder-state.json"
    decide = DecideStub({"action": "reuse", "workflow": "wf", "message": "go"})
    dispatch = DispatchRecorder()
    monkeypatch.setattr(engine_archon, "dispatch", dispatch)

    result = run_pass(
        settings=enabled_settings(projects_dir, archon_db=db),
        state_file=state_file,
        decide=decide,
    )

    assert result.project_outcomes["inflight"] == PROJECT_RUNNING
    assert decide.calls == []  # no steering -> no decision, no LLM
    assert dispatch.calls == []
    parsed = parse(path)
    assert parsed.activity_log.count("[note]") == 1
    assert parsed.frontmatter.last_run  # machine state re-stamped in code
    snapshot = project_entry(state_file, "inflight")[engine_archon.MTIME_SNAPSHOT_KEY]
    assert snapshot["path"] == str(worktree)  # classify-then-refresh upkeep


def test_running_with_steering_decides_but_dispatch_is_refused(
    projects_dir, tmp_path, monkeypatch
):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    db = make_archon_db(
        tmp_path / "archon.db", [run_row("run-1", "running", worktree)]
    )
    path = make_project(
        projects_dir,
        "steered",
        status="building",
        current_job_id="run-1",
        activity=(
            "- 2026-07-03T08:00:00 created",
            "- 2026-07-04T08:00:00 [steer] change direction",
        ),
    )
    state_file = tmp_path / "cofounder-state.json"
    decide = DecideStub({"action": "reuse", "workflow": "wf", "message": "go"})
    dispatch = DispatchRecorder()
    monkeypatch.setattr(engine_archon, "dispatch", dispatch)

    result = run_pass(
        settings=enabled_settings(projects_dir, archon_db=db),
        state_file=state_file,
        decide=decide,
    )

    assert result.project_outcomes["steered"] == PROJECT_DISPATCH_REFUSED
    assert len(decide.calls) == 1  # human replied -> a decision remains
    assert decide.calls[0][1]["reason"] == "human-replied"
    assert dispatch.calls == []  # ...but a dispatch can never happen
    assert parse(path).frontmatter.status == "building"
    assert project_entry(state_file, "steered")["reply_cursor"] == 2  # consumed


# === completion path: the executable check is the only done signal ===


def _testing_project(projects_dir, tmp_path, slug, **overrides):
    worktree = tmp_path / f"{slug}-worktree"
    worktree.mkdir()
    db = make_archon_db(
        tmp_path / f"{slug}-archon.db",
        [run_row("run-c", "completed", worktree, completed="2026-07-04 01:00:00")],
    )
    path = make_project(
        projects_dir,
        slug,
        status="testing",
        current_job_id="run-c",
        completion_check="echo ok",
        **overrides,
    )
    return path, db


def test_completion_green_flips_done_and_archives(projects_dir, tmp_path, monkeypatch):
    path, db = _testing_project(projects_dir, tmp_path, "shippable")
    monkeypatch.setattr(
        engine_archon, "completion_env", lambda wp, check, **kw: (True, "ok")
    )
    state_file = tmp_path / "cofounder-state.json"
    notify = NotifyRecorder()

    result = run_pass(
        settings=enabled_settings(projects_dir, archon_db=db),
        state_file=state_file,
        notify=notify,
    )

    assert result.project_outcomes["shippable"] == PROJECT_DONE
    archived = projects_dir / "done" / "shippable.md"
    assert archived.exists() and not path.exists()  # archive move on disk
    assert parse(archived).frontmatter.status == "done"
    assert "[check] completion check green; done" in parse(archived).activity_log
    assert notify.calls == [
        ("shippable", "shippable: completion check green; project done", "done"),
    ]
    assert project_entry(state_file, "shippable")["fail_streak"] == 0


def test_completion_green_subjective_gate_parks_awaiting_human(
    projects_dir, tmp_path, monkeypatch
):
    path, db = _testing_project(
        projects_dir, tmp_path, "subjective", subjective_gate=True
    )
    monkeypatch.setattr(
        engine_archon, "completion_env", lambda wp, check, **kw: (True, "ok")
    )
    state_file = tmp_path / "cofounder-state.json"
    notify = NotifyRecorder()

    result = run_pass(
        settings=enabled_settings(projects_dir, archon_db=db),
        state_file=state_file,
        notify=notify,
    )

    assert result.project_outcomes["subjective"] == PROJECT_AWAITING_VERDICT
    assert path.exists()  # parked, never archived without the human verdict
    parsed = parse(path)
    assert parsed.frontmatter.status == "awaiting-human"
    assert parsed.frontmatter.current_job_id == "run-c"  # kept for the approve
    assert [call[2] for call in notify.calls] == ["awaiting-human"]


def test_completion_check_failing_twice_flips_blocked(
    projects_dir, tmp_path, monkeypatch
):
    path, db = _testing_project(projects_dir, tmp_path, "flaky")
    monkeypatch.setattr(
        engine_archon, "completion_env", lambda wp, check, **kw: (False, "boom")
    )
    state_file = tmp_path / "cofounder-state.json"
    notify = NotifyRecorder()
    settings = enabled_settings(projects_dir, archon_db=db)

    first = run_pass(settings=settings, state_file=state_file, notify=notify)

    assert first.project_outcomes["flaky"] == PROJECT_CHECK_FAILED
    assert parse(path).frontmatter.status == "testing"  # one strike is a retry
    assert project_entry(state_file, "flaky")["fail_streak"] == 1
    assert notify.calls == []

    second = run_pass(settings=settings, state_file=state_file, notify=notify)

    assert second.project_outcomes["flaky"] == PROJECT_BLOCKED
    parsed = parse(path)
    assert parsed.frontmatter.status == "blocked"  # frontmatter AND state json
    assert "completion check failed twice" in parsed.activity_log
    assert project_entry(state_file, "flaky")["fail_streak"] == 2
    assert [call[2] for call in notify.calls] == ["blocked"]


# === concurrency cap: excess waits in queued order, no LLM call burned ===


def test_concurrency_cap_holds_new_project_queued(
    projects_dir, tmp_path, monkeypatch
):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    db = make_archon_db(
        tmp_path / "archon.db", [run_row("run-a", "running", worktree)]
    )
    make_project(projects_dir, "a-running", status="building", current_job_id="run-a")
    make_project(projects_dir, "b-new", status="new")
    decide = DecideStub({"action": "reuse", "workflow": "wf", "message": "go"})
    dispatch = DispatchRecorder()
    monkeypatch.setattr(engine_archon, "dispatch", dispatch)

    result = run_pass(
        settings=enabled_settings(projects_dir, archon_db=db, max_concurrent=1),
        state_file=tmp_path / "cofounder-state.json",
        decide=decide,
    )

    assert result.project_outcomes["a-running"] == PROJECT_RUNNING
    assert result.project_outcomes["b-new"] == PROJECT_QUEUED
    assert decide.calls == []  # the queued project never burns an LLM call
    assert dispatch.calls == []


# === non-enum status tolerance flows through the pipeline ===


def test_non_enum_status_flows_as_active_and_recovers(projects_dir, tmp_path):
    path = make_project(projects_dir, "rogue", status="in_progress")
    decide = DecideStub({"action": "park"})

    result = run_pass(
        settings=enabled_settings(projects_dir),
        state_file=tmp_path / "cofounder-state.json",
        decide=decide,
    )

    assert result.project_outcomes["rogue"] == PROJECT_DECIDED
    assert len(decide.calls) == 1  # polling never stalled on the rogue string
    assert decide.calls[0][1]["reason"] == "new-project"
    assert parse(path).frontmatter.status == "awaiting-human"  # folded back in


# === dispatch execution: confirmed re-stamps, unconfirmed leaves no phantom ===


def _dispatchable_project(projects_dir, tmp_path, monkeypatch, slug):
    path = make_project(projects_dir, slug, status="new", repo="demo")
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(
        repos_mod,
        "resolve_repo",
        lambda slug_, **kw: RepoResolution(
            slug="demo", local_path=repo_dir, default_branch="master"
        ),
    )
    return path, repo_dir


def test_confirmed_dispatch_restamps_machine_state(
    projects_dir, tmp_path, monkeypatch
):
    path, repo_dir = _dispatchable_project(projects_dir, tmp_path, monkeypatch, "kick")
    dispatch = DispatchRecorder(run_id="abc123")
    monkeypatch.setattr(engine_archon, "dispatch", dispatch)
    decide = DecideStub(
        {
            "action": "reuse",
            "workflow": "wf-x",
            "message": "build it",
            "log_line": "kick off iteration",
            "plan": "- [ ] step 1",
        }
    )
    state_file = tmp_path / "cofounder-state.json"

    result = run_pass(
        settings=enabled_settings(projects_dir),
        state_file=state_file,
        decide=decide,
    )

    assert result.project_outcomes["kick"] == PROJECT_DISPATCHED
    assert len(dispatch.calls) == 1
    call = dispatch.calls[0]
    assert call["workflow"] == "wf-x"
    assert call["message"].startswith("build it")
    assert MERGE_POLICY_INSTRUCTION in call["message"]  # US-017 merge policy
    assert call["branch"] == "cofounder/kick-1"
    assert call["repo_path"] == repo_dir
    parsed = parse(path)
    assert parsed.frontmatter.status == "building"
    assert parsed.frontmatter.current_job_id == "abc123"
    assert parsed.frontmatter.iterations == 1
    assert parsed.frontmatter.branch == "cofounder/kick-1"
    assert parsed.plan == "- [ ] step 1"  # decision plan applied
    assert "kick off iteration" in parsed.activity_log
    assert "[dispatch] iteration 1: workflow wf-x run abc123" in parsed.activity_log
    entry = project_entry(state_file, "kick")
    assert entry["wall_clock_start"] and entry["last_dispatch_at"]
    # The stored args carry the AMENDED message so a zombie re-dispatch
    # replays the merge policy too (US-017).
    assert entry["last_dispatch_args"] == {
        "workflow": "wf-x",
        "message": call["message"],
        "repo_path": str(repo_dir),
    }
    assert entry["fail_streak"] == 0 and entry["notified"] == {}


def test_unconfirmed_dispatch_leaves_no_phantom_building(
    projects_dir, tmp_path, monkeypatch
):
    path, _ = _dispatchable_project(projects_dir, tmp_path, monkeypatch, "ghost")
    dispatch = DispatchRecorder(run_id=None)  # no archon.db receipt within grace
    monkeypatch.setattr(engine_archon, "dispatch", dispatch)
    decide = DecideStub({"action": "reuse", "workflow": "wf", "message": "go"})
    state_file = tmp_path / "cofounder-state.json"

    result = run_pass(
        settings=enabled_settings(projects_dir),
        state_file=state_file,
        decide=decide,
    )

    assert result.project_outcomes["ghost"] == PROJECT_DISPATCH_FAILED
    parsed = parse(path)
    assert parsed.frontmatter.status == "new"  # NOT building
    assert parsed.frontmatter.current_job_id is None  # no phantom job id
    assert parsed.frontmatter.iterations == 0
    assert "[dispatch-failed]" in parsed.activity_log
    assert project_entry(state_file, "ghost")["last_dispatch_failed_at"]


# === decision seam: pending without a decider, no-op on garbage ===


def test_decision_pending_without_decider_writes_nothing(projects_dir, tmp_path):
    path = make_project(projects_dir, "waiting", status="new")
    before = path.read_bytes()

    result = run_pass(
        settings=enabled_settings(projects_dir),
        state_file=tmp_path / "cofounder-state.json",
    )

    assert result.project_outcomes["waiting"] == PROJECT_DECISION_PENDING
    assert path.read_bytes() == before  # nothing to execute, nothing touched


def test_unknown_decision_action_is_noop(projects_dir, tmp_path):
    path = make_project(projects_dir, "confused", status="new")
    decide = DecideStub({"action": "deploy-to-prod"})

    result = run_pass(
        settings=enabled_settings(projects_dir),
        state_file=tmp_path / "cofounder-state.json",
        decide=decide,
    )

    assert result.project_outcomes["confused"] == PROJECT_DECISION_NOOP
    assert parse(path).frontmatter.status == "new"


def test_parked_steering_consumed_exactly_once(projects_dir, tmp_path):
    make_project(
        projects_dir,
        "napping",
        status="awaiting-human",
        activity=(
            "- 2026-07-03T08:00:00 created",
            "- 2026-07-04T08:00:00 [steer] please re-plan",
        ),
    )
    state_file = tmp_path / "cofounder-state.json"
    decide = DecideStub({"action": "park"})
    settings = enabled_settings(projects_dir)

    first = run_pass(settings=settings, state_file=state_file, decide=decide)
    second = run_pass(settings=settings, state_file=state_file, decide=decide)

    assert first.project_outcomes["napping"] == PROJECT_DECIDED
    assert second.project_outcomes["napping"] == PROJECT_PARKED
    assert len(decide.calls) == 1  # the reply cursor consumed the steer line
    assert decide.calls[0][1]["reason"] == "human-replied"
    assert project_entry(state_file, "napping")["reply_cursor"] == 2


# === zombie upkeep: classified against the PRIOR snapshot, recovery wired ===


def test_zombie_classified_and_recovery_wired(projects_dir, tmp_path, monkeypatch):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    db = make_archon_db(
        tmp_path / "archon.db",
        [run_row("run-z", "running", worktree, last_activity="2020-01-01 00:00:00")],
    )
    path = make_project(
        projects_dir, "undead", status="building", current_job_id="run-z"
    )
    state_file = tmp_path / "cofounder-state.json"
    state_mod.update_project_state(
        "undead",
        state_file,
        **{
            engine_archon.MTIME_SNAPSHOT_KEY: {
                "run_id": "run-z",
                "path": str(worktree),
                "mtime": 9_999_999_999.0,  # no growth since the prior pass
                "taken_at": "2026-07-04 00:00:00",
            },
            "last_dispatch_args": {
                "workflow": "wf",
                "message": "go",
                "repo_path": str(tmp_path / "repo"),
            },
        },
    )
    recoveries = []

    def fake_recover(project_path, run_id, workflow, branch, message, repo_path, **kw):
        recoveries.append({"run_id": run_id, "workflow": workflow, "branch": branch})
        return engine_archon.DispatchResult(
            run_id="run-z2",
            pid=1,
            argv=("archon",),
            log_path=Path("z.log"),
            dispatched_at="2026-07-04 00:00:00",
        )

    monkeypatch.setattr(engine_archon, "recover_zombie", fake_recover)

    result = run_pass(
        settings=enabled_settings(projects_dir, archon_db=db),
        state_file=state_file,
    )

    assert result.project_outcomes["undead"] == PROJECT_ZOMBIE_RECOVERED
    assert recoveries == [
        {"run_id": "run-z", "workflow": "wf", "branch": "cofounder/undead-1"},
    ]
    parsed = parse(path)
    assert parsed.frontmatter.current_job_id == "run-z2"  # new run stamped
    assert parsed.frontmatter.iterations == 1


# === dry run: decide-without-dispatch (the US-020 smoke seam) ===


def test_dry_run_calls_decide_but_executes_nothing(projects_dir, tmp_path):
    path = make_project(projects_dir, "rehearsal", status="new")
    before = path.read_bytes()
    state_file = tmp_path / "cofounder-state.json"
    decide = DecideStub({"action": "park"})

    result = run_pass(
        dry_run=True,
        settings=enabled_settings(projects_dir),
        state_file=state_file,
        decide=decide,
    )

    assert result.project_outcomes["rehearsal"] == PROJECT_DECIDED_DRY
    assert len(decide.calls) == 1  # the decision was made...
    assert path.read_bytes() == before  # ...and nothing was executed
    assert not state_file.exists()
