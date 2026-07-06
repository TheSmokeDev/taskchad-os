"""US-008/US-009 — Archon engine adapter: read-only poll + detached dispatch.

US-008 asserts:
  - poll(run_id) reads status/working_path from a fixture db built with the
    live remote_agent_workflow_runs DDL subset (running / completed / failed)
  - ANY read failure degrades to ("unknown", None) and NEVER raises: missing
    row, missing file, garbage non-SQLite file, missing table, blank status,
    empty run_id
  - the connection URI is mode=ro — a write through it raises inside SQLite
    (adversarial proof, not just a string check)
  - poll works while a WAL-mode writer connection is still open (Archon
    mid-run simulation)
  - db_path=None resolves settings.archon_db at CALL time via
    COFOUNDER_ARCHON_DB (Rule 1 behavioral + structural proofs)
  - fetch_run_row exposes all five RUN_COLUMNS (US-010 needs
    last_activity_at)

US-009 asserts:
  - build_dispatch_argv builds the exact archon workflow run argv (message
    last, list form — no shell quoting)
  - build_child_env scrubs every CLAUDECODE* key and pins the archon bin dir
    + git dir onto PATH without duplicates
  - capture_run_id recovers the run_id from a matching row (newest wins,
    rows started BEFORE the dispatch timestamp never match) and returns None
    on grace-window expiry — the no-phantom-building contract
  - dispatch degrades to run_id=None (never raises) on missing repo path,
    spawn failure, and unconfirmed dispatch; the fake spawn proves argv, env
    scrub, log path, and cwd wiring
  - the dispatch timestamp lives in archon.db's clock domain (naive-UTC
    "YYYY-MM-DD HH:MM:SS")
  - detachment proof against a REAL archon spawn is opt-in via COFOUNDER_IT=1

US-010 asserts:
  - completion_env runs the operator-authored check with cwd=working_path,
    merged captured output, and a bounded timeout — pass / fail / timeout /
    missing worktree / empty check all return (bool, str), never a hang or
    an exception; oversized output keeps the tail
  - worktree_mtime is the newest st_mtime anywhere under the tree; unusable
    paths degrade to None; worktree_snapshot never writes an untrustable one
  - classify_zombie is the two-signal rule: True ONLY for a stale 'running'
    row whose worktree showed no mtime growth over the prior pass's
    snapshot; every unknown degrades to False (Rule 2)
  - recover_zombie marks the run failed in local state, re-dispatches
    detached, and appends exactly ONE Activity Log line — fail-open on a
    state or log failure
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import shared
from cofounder import engine_archon

# Subset of the verified live archon.db DDL (types and defaults preserved).
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

_INSERT = """
INSERT INTO remote_agent_workflow_runs
    (id, conversation_id, workflow_name, user_message, status,
     started_at, completed_at, last_activity_at, working_path)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# Timestamps mirror the live rows: naive-UTC strings from datetime('now').
ROWS = (
    (
        "run-running",
        "conv-1",
        "archon-ralph-dag",
        "build the thing",
        "running",
        "2026-07-04 05:51:50",
        None,
        "2026-07-04 06:03:03",
        "C:\\Users\\YourUser\\.archon\\worktrees\\cofounder-demo-1",
    ),
    (
        "run-completed",
        "conv-2",
        "archon-ralph-dag",
        "finish the thing",
        "completed",
        "2026-07-03 21:24:58",
        "2026-07-04 03:01:07",
        "2026-07-03 21:24:58",
        "~/example-client-proof",
    ),
    (
        "run-failed",
        "conv-3",
        "archon-piv-loop",
        "break the thing",
        "failed",
        "2026-07-03 20:56:13",
        "2026-07-03 21:15:17",
        "2026-07-03 20:56:13",
        "~/example-client-proof",
    ),
    (
        "run-null-path",
        "conv-4",
        "archon-ralph-dag",
        "no worktree yet",
        "pending",
        "2026-07-04 06:10:00",
        None,
        "2026-07-04 06:10:00",
        None,
    ),
    (
        "run-blank-status",
        "conv-5",
        "archon-ralph-dag",
        "rogue row",
        "   ",
        "2026-07-04 06:11:00",
        None,
        "2026-07-04 06:11:00",
        "~/somewhere",
    ),
)


def _make_db(path: Path, rows=ROWS) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.execute(RUNS_DDL)
        connection.executemany(_INSERT, rows)
        connection.commit()
    finally:
        connection.close()
    return path


@pytest.fixture(autouse=True)
def clear_archon_db_env(monkeypatch):
    """A live .env COFOUNDER_ARCHON_DB must never leak into these tests."""
    monkeypatch.delenv("COFOUNDER_ARCHON_DB", raising=False)
    yield


@pytest.fixture()
def fixture_db(tmp_path):
    return _make_db(tmp_path / "archon.db")


# === happy-path rows ===


def test_poll_running_row(fixture_db):
    status, working_path = engine_archon.poll("run-running", db_path=fixture_db)
    assert status == "running"
    assert working_path == "C:\\Users\\YourUser\\.archon\\worktrees\\cofounder-demo-1"


def test_poll_completed_row(fixture_db):
    status, working_path = engine_archon.poll("run-completed", db_path=fixture_db)
    assert status == "completed"
    assert working_path == "~/example-client-proof"


def test_poll_failed_row(fixture_db):
    status, working_path = engine_archon.poll("run-failed", db_path=fixture_db)
    assert status == "failed"
    assert working_path == "~/example-client-proof"


def test_poll_null_working_path_is_none(fixture_db):
    status, working_path = engine_archon.poll("run-null-path", db_path=fixture_db)
    assert status == "pending"
    assert working_path is None


# === degrade paths (never raise) ===


def test_missing_row_degrades_unknown(fixture_db, caplog):
    with caplog.at_level("WARNING", logger="cofounder.engine_archon"):
        result = engine_archon.poll("no-such-run", db_path=fixture_db)
    assert result == (engine_archon.UNKNOWN_STATUS, None)
    assert "not found" in caplog.text


def test_missing_file_degrades_unknown(tmp_path, caplog):
    with caplog.at_level("WARNING", logger="cofounder.engine_archon"):
        result = engine_archon.poll("run-running", db_path=tmp_path / "absent.db")
    assert result == (engine_archon.UNKNOWN_STATUS, None)
    assert "poll failed" in caplog.text


def test_garbage_file_degrades_unknown(tmp_path):
    garbage = tmp_path / "archon.db"
    garbage.write_bytes(b"this is not a sqlite database at all\x00\x01\x02")
    assert engine_archon.poll("run-running", db_path=garbage) == (
        engine_archon.UNKNOWN_STATUS,
        None,
    )


def test_missing_table_degrades_unknown(tmp_path):
    empty = tmp_path / "archon.db"
    sqlite3.connect(empty).close()  # valid sqlite file, no tables
    assert engine_archon.poll("run-running", db_path=empty) == (
        engine_archon.UNKNOWN_STATUS,
        None,
    )


def test_blank_status_degrades_unknown(fixture_db):
    assert engine_archon.poll("run-blank-status", db_path=fixture_db) == (
        engine_archon.UNKNOWN_STATUS,
        None,
    )


def test_empty_and_none_run_id_degrade(fixture_db):
    assert engine_archon.poll("", db_path=fixture_db) == (
        engine_archon.UNKNOWN_STATUS,
        None,
    )
    assert engine_archon.poll(None, db_path=fixture_db) == (
        engine_archon.UNKNOWN_STATUS,
        None,
    )
    assert engine_archon.fetch_run_row("   ", db_path=fixture_db) is None


# === read-only + WAL safety ===


def test_read_only_uri_refuses_writes(fixture_db):
    """Adversarial proof: a write through the adapter's URI dies in SQLite."""
    uri = engine_archon._read_only_uri(fixture_db)
    assert uri.endswith("?mode=ro")
    connection = sqlite3.connect(uri, uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute(
                "INSERT INTO remote_agent_workflow_runs "
                "(id, conversation_id, workflow_name, user_message) "
                "VALUES ('x', 'c', 'w', 'm')"
            )
    finally:
        connection.close()


def test_poll_with_wal_writer_connection_open(tmp_path):
    """Archon mid-run simulation: WAL db, writer still connected."""
    db = tmp_path / "archon.db"
    writer = sqlite3.connect(db)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(RUNS_DDL)
        writer.executemany(_INSERT, ROWS)
        writer.commit()
        status, working_path = engine_archon.poll("run-running", db_path=db)
        assert status == "running"
        assert working_path is not None
    finally:
        writer.close()


# === Rule 1: db path resolves via settings at call time ===


def test_default_db_path_resolves_env_at_call_time(fixture_db, monkeypatch):
    """COFOUNDER_ARCHON_DB set AFTER import steers a db_path=None poll."""
    monkeypatch.setenv("COFOUNDER_ARCHON_DB", str(fixture_db))
    status, _ = engine_archon.poll("run-running")
    assert status == "running"


def test_default_db_path_is_real_archon_db():
    resolved = engine_archon._resolve_db_path(None)
    assert resolved == Path.home() / ".archon" / "archon.db"


def test_rule1_all_def_time_defaults_are_none():
    for func in (
        engine_archon.poll,
        engine_archon.fetch_run_row,
        engine_archon.build_dispatch_argv,
        engine_archon.build_child_env,
        engine_archon.dispatch_log_path,
        engine_archon.capture_run_id,
    ):
        defaults = func.__defaults__
        assert defaults is not None
        assert all(d is None for d in defaults), (
            f"{func.__name__} def-time default capture: {defaults}"
        )
    kwdefaults = engine_archon.dispatch.__kwdefaults__ or {}
    assert kwdefaults, "dispatch lost its optional keyword-only args"
    assert all(v is None for v in kwdefaults.values()), (
        f"dispatch def-time default capture: {kwdefaults}"
    )


# === row shape for US-010 ===


def test_fetch_run_row_exposes_all_five_columns(fixture_db):
    row = engine_archon.fetch_run_row("run-completed", db_path=fixture_db)
    assert row is not None
    assert set(row) == set(engine_archon.RUN_COLUMNS)
    assert row["status"] == "completed"
    assert row["started_at"] == "2026-07-03 21:24:58"
    assert row["completed_at"] == "2026-07-04 03:01:07"
    assert row["last_activity_at"] == "2026-07-03 21:24:58"
    assert row["working_path"] == "~/example-client-proof"


# === US-009: argv + branch + env construction ===


def _insert_run(
    db: Path,
    *,
    run_id: str,
    workflow: str,
    message: str,
    started_at: str,
    status: str = "running",
    working_path: str | None = None,
) -> None:
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            _INSERT,
            (
                run_id,
                "conv-x",
                workflow,
                message,
                status,
                started_at,
                None,
                started_at,
                working_path,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def test_worktree_branch_naming():
    assert engine_archon.worktree_branch("demo-app", 3) == "cofounder/demo-app-3"


def test_build_dispatch_argv_order(tmp_path):
    argv = engine_archon.build_dispatch_argv(
        "archon-ralph-dag",
        engine_archon.worktree_branch("demo-app", 3),
        "build the thing",
        tmp_path,
        archon_bin="C:/tools/archon.exe",
    )
    assert argv == [
        "C:/tools/archon.exe",
        "workflow",
        "run",
        "archon-ralph-dag",
        "--branch",
        "cofounder/demo-app-3",
        "--cwd",
        str(tmp_path),
        "build the thing",
    ]


def test_resolve_archon_bin_standard_install_then_path_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert engine_archon._resolve_archon_bin(None) == "archon"  # no install
    exe = "archon.exe" if sys.platform == "win32" else "archon"
    bin_dir = tmp_path / ".archon" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / exe).write_bytes(b"")
    assert engine_archon._resolve_archon_bin(None) == str(bin_dir / exe)
    assert engine_archon._resolve_archon_bin("C:/x/archon.exe") == "C:/x/archon.exe"


def test_child_env_scrubs_claudecode_keys(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDECODE_SESSION_ID", "abc")
    monkeypatch.setenv("COFOUNDER_KEEP_ME", "yes")
    env = engine_archon.build_child_env(archon_bin="C:/tools/archon.exe")
    assert not [key for key in env if key.upper().startswith("CLAUDECODE")]
    assert env["COFOUNDER_KEEP_ME"] == "yes"


def test_child_env_pins_archon_bin_and_git_on_path(tmp_path):
    exe = tmp_path / "bin" / "archon.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    env = engine_archon.build_child_env(archon_bin=exe)
    parts = env["PATH"].split(os.pathsep)
    assert parts[0] == str(exe.parent)
    git = shutil.which("git")
    if git:
        assert str(Path(git).parent) in parts


def test_child_env_does_not_duplicate_pinned_dirs(tmp_path):
    exe = tmp_path / "archon.exe"
    exe.write_bytes(b"")
    base = {"PATH": str(tmp_path)}
    env = engine_archon.build_child_env(archon_bin=exe, base_env=base)
    assert env["PATH"].split(os.pathsep).count(str(tmp_path)) == 1


def test_dispatch_log_path_override_and_default(tmp_path, monkeypatch):
    import config

    override = engine_archon.dispatch_log_path("demo", 2, logs_dir=tmp_path)
    assert override == tmp_path / "demo-2.log"
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    resolved = engine_archon.dispatch_log_path("demo", 2)
    assert resolved == tmp_path / "data" / "logs" / "cofounder" / "demo-2.log"


def test_dispatch_timestamp_matches_archon_db_clock_domain():
    stamp = engine_archon._utc_db_now()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", stamp)
    parsed = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    assert abs((datetime.now(UTC) - parsed).total_seconds()) < 60


# === US-009: run_id capture ===


def test_capture_run_id_happy_path(fixture_db):
    _insert_run(
        fixture_db,
        run_id="run-fresh",
        workflow="archon-ralph-dag",
        message="fresh dispatch",
        started_at="2099-01-01 00:00:10",
    )
    run_id = engine_archon.capture_run_id(
        "archon-ralph-dag",
        "fresh dispatch",
        "2099-01-01 00:00:00",
        db_path=fixture_db,
        grace_seconds=0,
        poll_interval=0.01,
    )
    assert run_id == "run-fresh"


def test_capture_run_id_picks_newest_matching(fixture_db):
    for run_id, started_at in (
        ("run-older", "2099-01-01 00:00:05"),
        ("run-newer", "2099-01-01 00:00:30"),
    ):
        _insert_run(
            fixture_db,
            run_id=run_id,
            workflow="archon-ralph-dag",
            message="fresh dispatch",
            started_at=started_at,
        )
    run_id = engine_archon.capture_run_id(
        "archon-ralph-dag",
        "fresh dispatch",
        "2099-01-01 00:00:00",
        db_path=fixture_db,
        grace_seconds=0,
        poll_interval=0.01,
    )
    assert run_id == "run-newer"


def test_capture_run_id_ignores_rows_started_before_dispatch(fixture_db):
    """An OLD run with the same workflow+message is never this dispatch's
    receipt — the started_at >= dispatched_at bound is the phantom guard."""
    run_id = engine_archon.capture_run_id(
        "archon-ralph-dag",
        "build the thing",  # exists in ROWS with started_at 2026-07-04
        "2099-01-01 00:00:00",
        db_path=fixture_db,
        grace_seconds=0,
        poll_interval=0.01,
    )
    assert run_id is None


def test_capture_grace_window_expiry_returns_none(fixture_db, caplog):
    start = time.monotonic()
    with caplog.at_level("WARNING", logger="cofounder.engine_archon"):
        run_id = engine_archon.capture_run_id(
            "archon-ralph-dag",
            "never inserted",
            "2099-01-01 00:00:00",
            db_path=fixture_db,
            grace_seconds=0.15,
            poll_interval=0.05,
        )
    assert run_id is None
    assert time.monotonic() - start >= 0.15  # it really polled the window out
    assert "presumed dead" in caplog.text


def test_capture_run_id_unreadable_db_degrades_to_none(tmp_path):
    garbage = tmp_path / "archon.db"
    garbage.write_bytes(b"this is not a sqlite database")
    run_id = engine_archon.capture_run_id(
        "wf",
        "msg",
        "2099-01-01 00:00:00",
        db_path=garbage,
        grace_seconds=0,
        poll_interval=0.01,
    )
    assert run_id is None


# === US-009: dispatch (fake spawn) ===


def test_dispatch_happy_path_with_fake_spawn(fixture_db, tmp_path, monkeypatch):
    calls: dict[str, object] = {}

    def fake_spawn(cmd, *, env=None, log_path=None, cwd=None):
        calls.update(cmd=cmd, env=env, log_path=log_path, cwd=cwd)
        # Simulate archon inserting its run row after the spawn.
        _insert_run(
            fixture_db,
            run_id="run-dispatched",
            workflow="archon-ralph-dag",
            message="ship US-009",
            started_at="2099-01-01 00:00:00",
        )
        return 4242

    monkeypatch.setattr(shared, "spawn_detached", fake_spawn)
    repo = tmp_path / "repo"
    repo.mkdir()
    result = engine_archon.dispatch(
        "archon-ralph-dag",
        engine_archon.worktree_branch("demo", 1),
        "ship US-009",
        repo,
        slug="demo",
        iteration=1,
        db_path=fixture_db,
        grace_seconds=0,
        poll_interval=0.01,
        logs_dir=tmp_path / "logs",
        archon_bin="C:/tools/archon.exe",
    )
    assert result.run_id == "run-dispatched"
    assert result.pid == 4242
    cmd = calls["cmd"]
    assert cmd[0] == "C:/tools/archon.exe"
    assert cmd[1:3] == ["workflow", "run"]
    assert cmd[-1] == "ship US-009"
    assert "cofounder/demo-1" in cmd
    assert calls["log_path"] == tmp_path / "logs" / "demo-1.log"
    assert calls["cwd"] == repo
    env = calls["env"]
    assert not [key for key in env if key.upper().startswith("CLAUDECODE")]
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", result.dispatched_at
    )


def test_dispatch_unconfirmed_run_id_is_none(fixture_db, tmp_path, monkeypatch):
    """No archon.db row within grace = failed attempt, never a phantom."""
    monkeypatch.setattr(shared, "spawn_detached", lambda cmd, **kwargs: 4242)
    repo = tmp_path / "repo"
    repo.mkdir()
    result = engine_archon.dispatch(
        "archon-ralph-dag",
        "cofounder/demo-2",
        "never lands",
        repo,
        slug="demo",
        iteration=2,
        db_path=fixture_db,
        grace_seconds=0.05,
        poll_interval=0.01,
        logs_dir=tmp_path,
    )
    assert result.run_id is None
    assert result.pid == 4242


def test_dispatch_spawn_failure_degrades(fixture_db, tmp_path, monkeypatch, caplog):
    def boom(cmd, **kwargs):
        raise OSError("no exe")

    monkeypatch.setattr(shared, "spawn_detached", boom)
    repo = tmp_path / "repo"
    repo.mkdir()
    with caplog.at_level("WARNING", logger="cofounder.engine_archon"):
        result = engine_archon.dispatch(
            "wf",
            "cofounder/demo-3",
            "msg",
            repo,
            slug="demo",
            iteration=3,
            db_path=fixture_db,
            grace_seconds=0,
            poll_interval=0.01,
            logs_dir=tmp_path,
        )
    assert result.run_id is None
    assert result.pid is None
    assert "spawn failed" in caplog.text


def test_dispatch_missing_repo_path_refuses_spawn(fixture_db, tmp_path, monkeypatch):
    spawned: list[object] = []
    monkeypatch.setattr(
        shared, "spawn_detached", lambda cmd, **kwargs: spawned.append(cmd) or 1
    )
    result = engine_archon.dispatch(
        "wf",
        "cofounder/demo-4",
        "msg",
        tmp_path / "absent",
        slug="demo",
        iteration=4,
        db_path=fixture_db,
        grace_seconds=0,
        poll_interval=0.01,
        logs_dir=tmp_path,
    )
    assert result.run_id is None
    assert result.pid is None
    assert spawned == []


# === US-009: opt-in real detachment proof ===


@pytest.mark.skipif(
    os.getenv("COFOUNDER_IT") != "1",
    reason="opt-in integration: set COFOUNDER_IT=1",
)
def test_detachment_proof_real_dispatch(tmp_path):
    """Phase 9 detachment proof — REAL archon spawn, operator-run.

    Requires COFOUNDER_IT=1 plus COFOUNDER_IT_WORKFLOW (a tiny workflow name)
    and COFOUNDER_IT_REPO (a registered repo path). The dispatch happens in a
    short-lived parent python process; after that parent exits, the archon
    child must still be alive (Global Invariant 7).
    """
    workflow = os.getenv("COFOUNDER_IT_WORKFLOW", "")
    repo = os.getenv("COFOUNDER_IT_REPO", "")
    if not workflow or not repo:
        pytest.skip("COFOUNDER_IT_WORKFLOW / COFOUNDER_IT_REPO not set")
    scripts_dir = Path(__file__).resolve().parents[1]
    child_script = (
        "import sys\n"
        f"sys.path.insert(0, {str(scripts_dir)!r})\n"
        "from cofounder import engine_archon\n"
        "result = engine_archon.dispatch(\n"
        f"    {workflow!r},\n"
        "    engine_archon.worktree_branch('it-detach', 0),\n"
        "    'cofounder US-009 detachment proof',\n"
        f"    {repo!r},\n"
        "    slug='it-detach',\n"
        "    iteration=0,\n"
        "    grace_seconds=0,\n"
        "    poll_interval=0.01,\n"
        f"    logs_dir={str(tmp_path)!r},\n"
        ")\n"
        "print(result.pid or 0)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", child_script],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    pid = int(completed.stdout.strip().splitlines()[-1])
    assert pid > 0, "dispatch did not spawn a child"
    time.sleep(2.0)  # the dispatching parent has already exited
    import psutil

    assert psutil.pid_exists(pid), "archon child died with the dispatching parent"


# === US-010: completion_env ===

# The scripts venv path has no spaces on this box, so the interpreter rides
# unquoted through cmd.exe's shell=True parsing (quoting it would trip the
# cmd /C outer-quote-stripping rule).
_PY = sys.executable


def test_completion_env_check_passes(tmp_path):
    passed, output = engine_archon.completion_env(
        tmp_path, "echo completion-ok", timeout_seconds=60
    )
    assert passed is True
    assert "completion-ok" in output


def test_completion_env_check_fails_with_merged_stderr(tmp_path):
    passed, output = engine_archon.completion_env(
        tmp_path, "echo boom-detail 1>&2 && exit 7", timeout_seconds=60
    )
    assert passed is False
    assert "boom-detail" in output  # stderr merged into the captured output


def test_completion_env_runs_in_worktree_cwd(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    check = f'{_PY} -c "import os; print(os.getcwd())"'
    passed, output = engine_archon.completion_env(worktree, check, timeout_seconds=60)
    assert passed is True
    reported = Path(output.strip().splitlines()[-1]).resolve()
    assert reported == worktree.resolve()


def test_completion_env_timeout_kills_and_fails(tmp_path):
    check = f'{_PY} -c "import time; time.sleep(30)"'
    start = time.monotonic()
    passed, output = engine_archon.completion_env(tmp_path, check, timeout_seconds=1.0)
    elapsed = time.monotonic() - start
    assert passed is False
    assert "timed out" in output
    assert elapsed < 25, "timeout did not kill the check tree"


def test_completion_env_missing_worktree_fails(tmp_path):
    passed, output = engine_archon.completion_env(tmp_path / "gone", "echo hi")
    assert passed is False
    assert "does not exist" in output
    passed, _ = engine_archon.completion_env(None, "echo hi")
    assert passed is False


def test_completion_env_empty_check_fails(tmp_path):
    for check in ("", "   ", None):
        passed, output = engine_archon.completion_env(tmp_path, check)
        assert passed is False
        assert "no completion_check" in output


def test_completion_env_output_keeps_tail_when_capped(tmp_path):
    check = f"{_PY} -c \"print('start-marker'); print('x' * 60000)\""
    passed, output = engine_archon.completion_env(tmp_path, check, timeout_seconds=60)
    assert passed is True
    assert output.startswith("[output truncated]")
    assert len(output) <= engine_archon.COMPLETION_OUTPUT_CAP + 32
    assert output.endswith("x" * 100)  # the tail survives, the head is dropped
    assert "start-marker" not in output


# === US-010: worktree mtime + snapshot ===


def _worktree(tmp_path: Path) -> Path:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "main.py").write_text("print('hi')", encoding="utf-8")
    return worktree


def test_worktree_mtime_newest_nested_file_wins(tmp_path):
    worktree = _worktree(tmp_path)
    nested = worktree / "deep" / "sub"
    nested.mkdir(parents=True)
    target = nested / "hot.txt"
    target.write_text("x", encoding="utf-8")
    future = time.time() + 3600
    os.utime(target, (future, future))
    assert engine_archon.worktree_mtime(worktree) == pytest.approx(future)


def test_worktree_mtime_unusable_paths_degrade_to_none(tmp_path):
    assert engine_archon.worktree_mtime(None) is None
    assert engine_archon.worktree_mtime("") is None
    assert engine_archon.worktree_mtime(tmp_path / "gone") is None
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("x", encoding="utf-8")
    assert engine_archon.worktree_mtime(not_a_dir) is None


def test_worktree_snapshot_shape_and_degrade(tmp_path):
    worktree = _worktree(tmp_path)
    snapshot = engine_archon.worktree_snapshot("run-1", worktree)
    assert snapshot is not None
    assert snapshot["run_id"] == "run-1"
    assert snapshot["path"] == str(worktree)
    assert snapshot["mtime"] == pytest.approx(engine_archon.worktree_mtime(worktree))
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", snapshot["taken_at"])
    assert engine_archon.worktree_snapshot("run-1", tmp_path / "gone") is None


# === US-010: classify_zombie (two-signal rule) ===

_NOW = datetime(2026, 7, 4, 12, 0, 0)  # naive-UTC test clock


def _db_ts(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _zombie_row(
    working_path: str | None,
    *,
    status: str = "running",
    minutes_stale: int = 120,
    last_activity: str | None = "",
) -> dict:
    if last_activity == "":
        last_activity = _db_ts(_NOW - timedelta(minutes=minutes_stale))
    return {
        "status": status,
        "working_path": working_path,
        "started_at": _db_ts(_NOW - timedelta(hours=5)),
        "completed_at": None,
        "last_activity_at": last_activity,
    }


def _snapshot_for(worktree: Path, offset: float = 0.0) -> dict:
    return {
        "run_id": "run-running",
        "path": str(worktree),
        "mtime": engine_archon.worktree_mtime(worktree) + offset,
        "taken_at": "2026-07-04 11:30:00",
    }


def test_zombie_true_positive(tmp_path):
    """Stale running row + no mtime growth over the prior snapshot = zombie."""
    worktree = _worktree(tmp_path)
    row = _zombie_row(str(worktree), minutes_stale=120)
    entry = {"mtime_snapshot": _snapshot_for(worktree)}
    assert (
        engine_archon.classify_zombie(row, entry, stale_minutes=60, now=_NOW) is True
    )


def test_zombie_fresh_activity_not_classified(tmp_path):
    worktree = _worktree(tmp_path)
    row = _zombie_row(str(worktree), minutes_stale=5)
    entry = {"mtime_snapshot": _snapshot_for(worktree)}
    assert (
        engine_archon.classify_zombie(row, entry, stale_minutes=60, now=_NOW) is False
    )


def test_zombie_mtime_growth_not_classified(tmp_path):
    """DB stale but the worktree grew since the snapshot — still alive."""
    worktree = _worktree(tmp_path)
    row = _zombie_row(str(worktree), minutes_stale=120)
    entry = {"mtime_snapshot": _snapshot_for(worktree, offset=-100.0)}
    assert (
        engine_archon.classify_zombie(row, entry, stale_minutes=60, now=_NOW) is False
    )


def test_zombie_non_running_status_not_classified(tmp_path):
    worktree = _worktree(tmp_path)
    entry = {"mtime_snapshot": _snapshot_for(worktree)}
    for status in ("completed", "failed", "pending", "unknown"):
        row = _zombie_row(str(worktree), status=status, minutes_stale=999)
        assert (
            engine_archon.classify_zombie(row, entry, stale_minutes=60, now=_NOW)
            is False
        )
    assert engine_archon.classify_zombie(None, entry, stale_minutes=60, now=_NOW) is False


def test_zombie_missing_working_path_degrades_to_not_zombie(tmp_path):
    """'unknown' path never triggers recovery — even with a stale DB row."""
    entry = {"mtime_snapshot": {"path": "anything", "mtime": 1.0}}
    for path in (None, "", "   "):
        row = _zombie_row(path, minutes_stale=999)
        assert (
            engine_archon.classify_zombie(row, entry, stale_minutes=60, now=_NOW)
            is False
        )
    gone = tmp_path / "gone-worktree"
    row = _zombie_row(str(gone), minutes_stale=999)
    entry = {"mtime_snapshot": {"path": str(gone), "mtime": 1.0}}
    assert (
        engine_archon.classify_zombie(row, entry, stale_minutes=60, now=_NOW) is False
    )


def test_zombie_unparseable_last_activity_not_classified(tmp_path):
    worktree = _worktree(tmp_path)
    entry = {"mtime_snapshot": _snapshot_for(worktree)}
    for garbage in (None, "not-a-timestamp"):
        row = _zombie_row(str(worktree), last_activity=garbage)
        assert (
            engine_archon.classify_zombie(row, entry, stale_minutes=60, now=_NOW)
            is False
        )


def test_zombie_requires_prior_cycle_snapshot(tmp_path):
    """First pass seeing a run can never classify it — growth is unprovable."""
    worktree = _worktree(tmp_path)
    row = _zombie_row(str(worktree), minutes_stale=999)
    for entry in ({}, None, {"mtime_snapshot": None}, {"mtime_snapshot": "junk"}):
        assert (
            engine_archon.classify_zombie(row, entry, stale_minutes=60, now=_NOW)
            is False
        )


def test_zombie_snapshot_from_other_worktree_not_classified(tmp_path):
    worktree = _worktree(tmp_path)
    row = _zombie_row(str(worktree), minutes_stale=999)
    entry = {
        "mtime_snapshot": {
            "path": str(tmp_path / "other-worktree"),
            "mtime": engine_archon.worktree_mtime(worktree),
        }
    }
    assert (
        engine_archon.classify_zombie(row, entry, stale_minutes=60, now=_NOW) is False
    )


def test_zombie_stale_minutes_resolves_env_at_call_time(tmp_path, monkeypatch):
    """Rule 1 behavioral proof: stale_minutes=None reads the knob per call."""
    worktree = _worktree(tmp_path)
    row = _zombie_row(str(worktree), minutes_stale=5)
    entry = {"mtime_snapshot": _snapshot_for(worktree)}
    monkeypatch.setenv("COFOUNDER_ZOMBIE_STALE_MINUTES", "1")
    assert engine_archon.classify_zombie(row, entry, now=_NOW) is True
    monkeypatch.setenv("COFOUNDER_ZOMBIE_STALE_MINUTES", "60")
    assert engine_archon.classify_zombie(row, entry, now=_NOW) is False


def test_zombie_aware_now_folds_to_naive_utc(tmp_path):
    """A tz-aware clock input must not crash the naive-UTC comparison."""
    worktree = _worktree(tmp_path)
    row = _zombie_row(str(worktree), minutes_stale=120)
    entry = {"mtime_snapshot": _snapshot_for(worktree)}
    aware_now = _NOW.replace(tzinfo=UTC)
    assert (
        engine_archon.classify_zombie(row, entry, stale_minutes=60, now=aware_now)
        is True
    )


def test_rule1_us010_def_time_defaults_are_none():
    assert engine_archon.completion_env.__defaults__ == (None,)
    classify_kwdefaults = engine_archon.classify_zombie.__kwdefaults__
    assert classify_kwdefaults and all(
        value is None for value in classify_kwdefaults.values()
    )
    recover_kwdefaults = engine_archon.recover_zombie.__kwdefaults__ or {}
    assert recover_kwdefaults and all(
        value is None for value in recover_kwdefaults.values()
    )


# === US-010: recover_zombie ===

PROJECT_MD = """---
tags: [system, cofounder]
status: building
created: 2026-07-01T00:00:00
last_run: null
repo: greenfield
branch: cofounder/demo-1
current_job_id: run-running
iterations: 1
max_iterations: 50
max_wall_clock_hours: 72
completion_check: "echo ok"
subjective_gate: false
archon_workflow: null
chat_thread: null
---
# Demo

## Spec (STATIC - orchestrator MUST NOT rewrite; only the operator edits)

Build the demo.

## Plan / Working Memory (MUTABLE - orchestrator may rewrite)

- [ ] step

## Activity Log (APPEND-ONLY - newest at the bottom)

- 2026-07-03T00:00:00 created
"""


def _activity_lines(path: Path) -> list[str]:
    section = path.read_text(encoding="utf-8").split("## Activity Log", 1)[1]
    return [line for line in section.splitlines() if line.startswith("- ")]


def test_recover_zombie_marks_state_redispatches_logs_once(
    fixture_db, tmp_path, monkeypatch
):
    project = tmp_path / "demo.md"
    project.write_text(PROJECT_MD, encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    state_file = tmp_path / "cofounder-state.json"

    def fake_spawn(cmd, *, env=None, log_path=None, cwd=None):
        _insert_run(
            fixture_db,
            run_id="run-redispatch",
            workflow="archon-ralph-dag",
            message="revive demo",
            started_at="2099-01-01 00:00:00",
        )
        return 777

    monkeypatch.setattr(shared, "spawn_detached", fake_spawn)
    state = {
        "projects": {
            "demo": {"fail_streak": 2, "mtime_snapshot": {"path": "old", "mtime": 1.0}}
        }
    }
    lines_before = len(_activity_lines(project))
    result = engine_archon.recover_zombie(
        project,
        "run-running",
        "archon-ralph-dag",
        "cofounder/demo-2",
        "revive demo",
        repo,
        slug="demo",
        iteration=2,
        state=state,
        state_file=state_file,
        db_path=fixture_db,
        grace_seconds=0,
        poll_interval=0.01,
        logs_dir=tmp_path / "logs",
    )
    assert result.run_id == "run-redispatch"
    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    entry = persisted["projects"]["demo"]
    assert entry["last_zombie_run_id"] == "run-running"
    assert entry["mtime_snapshot"] is None  # the new run starts a fresh cycle
    assert entry["fail_streak"] == 2  # unrelated bookkeeping preserved
    lines = _activity_lines(project)
    assert len(lines) == lines_before + 1, "recovery must append exactly ONE line"
    assert "[zombie]" in lines[-1]
    assert "run-running" in lines[-1]
    assert "run-redispatch" in lines[-1]


def test_recover_zombie_missing_project_file_fail_open(
    fixture_db, tmp_path, monkeypatch, caplog
):
    """A log-line failure warns but never breaks the recovery (Invariant 6)."""
    monkeypatch.setattr(shared, "spawn_detached", lambda cmd, **kwargs: 1)
    repo = tmp_path / "repo"
    repo.mkdir()
    state_file = tmp_path / "state.json"
    with caplog.at_level("WARNING", logger="cofounder.engine_archon"):
        result = engine_archon.recover_zombie(
            tmp_path / "missing.md",
            "run-running",
            "wf",
            "cofounder/demo-3",
            "msg",
            repo,
            slug="demo",
            iteration=3,
            state={},
            state_file=state_file,
            db_path=fixture_db,
            grace_seconds=0,
            poll_interval=0.01,
            logs_dir=tmp_path,
        )
    assert result.run_id is None  # unconfirmed dispatch, no phantom receipt
    assert "zombie log line failed" in caplog.text
    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert persisted["projects"]["demo"]["last_zombie_run_id"] == "run-running"
