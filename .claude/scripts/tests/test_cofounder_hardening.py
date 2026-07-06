"""US-017 — hardening: notify wiring, merge policy, adversarial suite.

Pipeline-level composition of the primitives shipped in US-001..US-016, each
locked with a fail-without-fix test. Assertions are repo/DB-level (Rule 4
spirit): state-json fields AND re-parsed frontmatter AND archive moves on
disk — never a status string alone.

Path map (one test per distinct path, adversarial first):
  - default notify wiring: run_pass with NO notify kwarg reaches the gated
    Telegram sender — a done flip sends exactly one (faked-HTTP) sendMessage,
    audits, and round-trips message_id -> chat_thread
  - notify-once: a caps trip notifies ONCE across repeated passes
  - blocked flip notifies once; a third pass re-notifies nothing
  - subjective park notifies once, then the operator approve (the SAME code
    path as /cofounder approve) flips done + archive move on disk
  - routine progress (running-gate note) is Activity Log only — zero HTTP
  - a broken notify module fails open to the logging stub (flip still lands)
  - phantom dispatch at pipeline level: unconfirmed dispatch = failed
    attempt, no phantom building, zero notify
  - zombie kill/recover through the REAL recover_zombie (simulated via
    fixture state + mtime): one [zombie] line, state marks, re-dispatch
  - merge policy: dispatched messages carry the PR-for-review instruction
    (idempotent; stored for zombie re-dispatch; greenfield exempt); grep
    proves no merge invocation in cofounder/ source; no auto-merge knob
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

# The approve flow crosses into the chat slice (core_handlers lives in
# .claude/chat/, flat-sys.path import convention).
_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS.parent / "chat") not in sys.path:
    sys.path.insert(0, str(_SCRIPTS.parent / "chat"))

import cofounder  # noqa: E402
import config  # noqa: E402
from cofounder import engine_archon, project_model  # noqa: E402
from cofounder import notify as notify_mod  # noqa: E402
from cofounder import orchestrate as orchestrate_mod  # noqa: E402
from cofounder import repos as repos_mod  # noqa: E402
from cofounder import state as state_mod  # noqa: E402
from cofounder.repos import GREENFIELD_SLUG, RepoResolution  # noqa: E402
from cofounder.run_pass import (  # noqa: E402
    MERGE_POLICY_INSTRUCTION,
    PROJECT_AWAITING_VERDICT,
    PROJECT_BLOCKED,
    PROJECT_CAPS_TRIPPED,
    PROJECT_CHECK_FAILED,
    PROJECT_DISPATCH_FAILED,
    PROJECT_DISPATCHED,
    PROJECT_DONE,
    PROJECT_PARKED,
    PROJECT_RUNNING,
    PROJECT_ZOMBIE_RECOVERED,
    _with_merge_policy,
    run_pass,
)
from orchestration import observability  # noqa: E402
from security import kill_switches  # noqa: E402

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
def clean_env(monkeypatch, tmp_path):
    """No COFOUNDER_*/kill-switch/Telegram env leaks from the operator .env
    (config runs load_dotenv(override=True) at import). Langfuse is pinned
    OFF and the disabled-path observation jsonl redirected (US-013 gotcha).
    The wired default notify resolves its audit path from config.DATA_DIR at
    call time — redirect it so audit rows never land in the real data dir.
    """
    for key in COFOUNDER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("HOMIE_KILLSWITCH_COFOUNDER", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    monkeypatch.setenv("LANGFUSE_ENABLED", "false")
    monkeypatch.setattr(observability, "_OBS_LOG", tmp_path / "obs" / "obs.jsonl")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    # US-020 wired orchestrate.decide as the run_pass default decider; pin it
    # back to None so no hardening test can ever reach a live LLM.
    monkeypatch.setattr(orchestrate_mod, "decide", None)
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


@pytest.fixture()
def no_http(monkeypatch):
    """Any HTTP attempt fails the test (proves the zero-notify invariants)."""

    def forbidden(*args, **kwargs):
        pytest.fail("HTTP call attempted; this path must never reach Telegram")

    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", forbidden)


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def install_fake_telegram(monkeypatch, *, message_id: int = 4242) -> list[dict]:
    """Record every outgoing sendMessage; answer a canned success payload."""
    sends: list[dict] = []

    def fake_urlopen(req, timeout=10):
        sends.append(
            {
                "url": req.full_url,
                "params": dict(urllib.parse.parse_qsl(req.data.decode())),
            }
        )
        return _FakeResponse({"ok": True, "result": {"message_id": message_id}})

    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", fake_urlopen)
    return sends


def set_creds(monkeypatch, token: str = "tok123", user_ids: str = "555"):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", user_ids)


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


def _testing_project(projects_dir, tmp_path, slug, *, completion_check="echo ok", **overrides):
    """One testing-status project whose run finished — the completion seam.

    The completion check is REAL (shell builtins, cross-platform): the
    hardening suite exercises the executable-completion invariant end to end
    instead of stubbing completion_env.
    """
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
        completion_check=completion_check,
        **overrides,
    )
    return path, db


class DecideStub:
    def __init__(self, decision=None):
        self.decision = decision
        self.calls = []

    def __call__(self, project, context):
        self.calls.append((project.slug, dict(context)))
        return self.decision


class DispatchRecorder:
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


def audit_rows(audit_path: Path) -> list[dict]:
    if not audit_path.exists():
        return []
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# =============================================================================
# AC-1 — pipeline notify wiring: the default hook IS the gated sender.
# =============================================================================


def test_default_notify_done_flip_sends_once_and_stamps_chat_thread(
    projects_dir, tmp_path, monkeypatch
):
    """No notify kwarg -> the real gated Telegram sender fires exactly once
    on the done flip, audits, and round-trips message_id -> chat_thread."""
    set_creds(monkeypatch)
    sends = install_fake_telegram(monkeypatch, message_id=777)
    path, db = _testing_project(projects_dir, tmp_path, "shipit")
    state_file = tmp_path / "cofounder-state.json"

    result = run_pass(
        settings=enabled_settings(projects_dir, archon_db=db),
        state_file=state_file,
    )

    assert result.project_outcomes["shipit"] == PROJECT_DONE
    assert len(sends) == 1
    assert "[co-founder] shipit - done" in sends[0]["params"]["text"]
    archived = projects_dir / "done" / "shipit.md"
    assert archived.exists() and not path.exists()  # archive move on disk
    parsed = parse(archived)
    assert parsed.frontmatter.status == "done"
    assert parsed.frontmatter.chat_thread == 777  # message_id round-trip
    rows = audit_rows(tmp_path / "data" / "cofounder_notify.jsonl")
    assert [r["outcome"] for r in rows] == ["sent"]
    assert "done" in project_entry(state_file, "shipit")["notified"]


def test_caps_trip_notifies_exactly_once_across_passes(
    projects_dir, tmp_path, monkeypatch
):
    """The caps flip notifies once, not per pass (the notify-once proof)."""
    set_creds(monkeypatch)
    sends = install_fake_telegram(monkeypatch)
    path = make_project(
        projects_dir, "capped", status="building", iterations=5, max_iterations=5
    )
    state_file = tmp_path / "cofounder-state.json"
    settings = enabled_settings(projects_dir)

    first = run_pass(settings=settings, state_file=state_file)
    assert first.project_outcomes["capped"] == PROJECT_CAPS_TRIPPED
    assert len(sends) == 1
    assert "[co-founder] capped - awaiting-human" in sends[0]["params"]["text"]

    second = run_pass(settings=settings, state_file=state_file)
    third = run_pass(settings=settings, state_file=state_file)

    assert second.project_outcomes["capped"] == PROJECT_PARKED
    assert third.project_outcomes["capped"] == PROJECT_PARKED
    assert len(sends) == 1  # ONE notify per flip, ever — not per pass
    assert parse(path).frontmatter.status == "awaiting-human"
    assert parse(path).activity_log.count("[caps]") == 1
    assert "awaiting-human:caps" in project_entry(state_file, "capped")["notified"]


def test_blocked_flip_notifies_once_with_state_and_frontmatter_proof(
    projects_dir, tmp_path, monkeypatch
):
    """Check-fails-twice -> blocked: the state-json fail streak AND the
    re-parsed frontmatter prove the flip (Rule 4 spirit), one notify total."""
    set_creds(monkeypatch)
    sends = install_fake_telegram(monkeypatch)
    path, db = _testing_project(
        projects_dir, tmp_path, "flaky", completion_check="exit 1"
    )
    state_file = tmp_path / "cofounder-state.json"
    settings = enabled_settings(projects_dir, archon_db=db)

    first = run_pass(settings=settings, state_file=state_file)
    assert first.project_outcomes["flaky"] == PROJECT_CHECK_FAILED
    assert project_entry(state_file, "flaky")["fail_streak"] == 1
    assert sends == []  # one strike is a retry, not a terminal flip

    second = run_pass(settings=settings, state_file=state_file)
    assert second.project_outcomes["flaky"] == PROJECT_BLOCKED
    assert project_entry(state_file, "flaky")["fail_streak"] == 2  # state json
    parsed = parse(path)
    assert parsed.frontmatter.status == "blocked"  # frontmatter on disk
    assert "completion check failed twice" in parsed.activity_log
    assert len(sends) == 1
    assert "[co-founder] flaky - blocked" in sends[0]["params"]["text"]

    third = run_pass(settings=settings, state_file=state_file)
    assert third.project_outcomes["flaky"] == PROJECT_PARKED
    assert len(sends) == 1  # blocked parks; no re-check, no re-notify


@pytest.mark.asyncio
async def test_subjective_park_then_operator_approve_end_to_end(
    projects_dir, tmp_path, monkeypatch
):
    """The full human-verdict loop: run_pass parks at awaiting-human with one
    notify; the operator approve (the SAME code path as /cofounder approve)
    flips done + archive move on disk."""
    set_creds(monkeypatch)
    sends = install_fake_telegram(monkeypatch)
    path, db = _testing_project(
        projects_dir, tmp_path, "verdict", subjective_gate=True
    )
    state_file = tmp_path / "cofounder-state.json"

    result = run_pass(
        settings=enabled_settings(projects_dir, archon_db=db),
        state_file=state_file,
    )

    assert result.project_outcomes["verdict"] == PROJECT_AWAITING_VERDICT
    assert path.exists()  # parked, never archived without the verdict
    assert parse(path).frontmatter.status == "awaiting-human"
    assert len(sends) == 1
    assert "[co-founder] verdict - awaiting-human" in sends[0]["params"]["text"]

    # The operator's verdict rides the /cofounder approve handler.
    monkeypatch.setenv("COFOUNDER_PROJECTS_DIR", str(projects_dir))
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    import core_handlers  # type: ignore[import-not-found]

    out = await core_handlers.handle_cofounder(
        object(), SimpleNamespace(chat_id=42), "approve verdict"
    )

    assert "approved" in out
    archived = projects_dir / "done" / "verdict.md"
    assert archived.exists() and not path.exists()  # archive move on disk
    parsed = parse(archived)
    assert parsed.frontmatter.status == "done"
    assert "[approve] approved by operator -> done" in parsed.activity_log


def test_routine_progress_is_activity_log_only(
    projects_dir, tmp_path, monkeypatch, no_http
):
    """A running-gate pass (job in flight) appends one note and NEVER
    notifies — creds are present, so only the flip logic protects."""
    set_creds(monkeypatch)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    db = make_archon_db(
        tmp_path / "archon.db", [run_row("run-1", "running", worktree)]
    )
    path = make_project(
        projects_dir, "inflight", status="building", current_job_id="run-1"
    )

    result = run_pass(
        settings=enabled_settings(projects_dir, archon_db=db),
        state_file=tmp_path / "cofounder-state.json",
    )

    assert result.project_outcomes["inflight"] == PROJECT_RUNNING
    assert parse(path).activity_log.count("[note]") == 1


def test_broken_notify_module_fails_open_to_stub(
    projects_dir, tmp_path, monkeypatch, no_http
):
    """A broken cofounder.notify module can never break a pass: the default
    resolution falls back to the logging stub and the flip still lands."""
    monkeypatch.setattr(cofounder, "notify", None)  # from-import yields None
    path = make_project(
        projects_dir, "capped", status="building", iterations=5, max_iterations=5
    )
    state_file = tmp_path / "cofounder-state.json"

    result = run_pass(settings=enabled_settings(projects_dir), state_file=state_file)

    assert result.project_outcomes["capped"] == PROJECT_CAPS_TRIPPED
    assert parse(path).frontmatter.status == "awaiting-human"  # flip landed
    assert "awaiting-human:caps" in project_entry(state_file, "capped")["notified"]


# =============================================================================
# AC-3 — detached-dispatch hardening at pipeline level (no phantom building).
# =============================================================================


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


def test_phantom_dispatch_leaves_no_building_and_no_notify(
    projects_dir, tmp_path, monkeypatch, no_http
):
    """Unconfirmed dispatch (no archon.db receipt within grace) is a failed
    attempt at PIPELINE level: prior status kept, no job id, no notify."""
    set_creds(monkeypatch)
    path, _ = _dispatchable_project(projects_dir, tmp_path, monkeypatch, "ghost")
    dispatch = DispatchRecorder(run_id=None)
    monkeypatch.setattr(engine_archon, "dispatch", dispatch)
    decide = DecideStub({"action": "reuse", "workflow": "wf", "message": "go"})
    state_file = tmp_path / "cofounder-state.json"

    result = run_pass(
        settings=enabled_settings(projects_dir), state_file=state_file, decide=decide
    )

    assert result.project_outcomes["ghost"] == PROJECT_DISPATCH_FAILED
    parsed = parse(path)
    assert parsed.frontmatter.status == "new"  # NOT building
    assert parsed.frontmatter.current_job_id is None  # no phantom job id
    assert parsed.frontmatter.iterations == 0
    assert "[dispatch-failed]" in parsed.activity_log
    entry = project_entry(state_file, "ghost")
    assert entry["last_dispatch_failed_at"]
    assert entry.get("notified") in (None, {})  # no terminal flip recorded


# =============================================================================
# AC-4 — zombie kill/recover, simulated via fixture state + mtime, through
# the REAL recover_zombie (only the spawn itself is recorded).
# =============================================================================


def test_zombie_recovery_real_path_one_log_line_and_state_marks(
    projects_dir, tmp_path, monkeypatch, no_http
):
    set_creds(monkeypatch)
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
    dispatch = DispatchRecorder(run_id="run-z2")
    monkeypatch.setattr(engine_archon, "dispatch", dispatch)  # recover's spawn

    result = run_pass(
        settings=enabled_settings(projects_dir, archon_db=db), state_file=state_file
    )

    assert result.project_outcomes["undead"] == PROJECT_ZOMBIE_RECOVERED
    assert len(dispatch.calls) == 1  # re-dispatched detached, exactly once
    assert dispatch.calls[0]["workflow"] == "wf"
    assert dispatch.calls[0]["branch"] == "cofounder/undead-1"
    parsed = parse(path)
    assert parsed.frontmatter.current_job_id == "run-z2"  # new run stamped
    assert parsed.frontmatter.iterations == 1
    assert parsed.activity_log.count("[zombie]") == 1  # exactly ONE line
    entry = project_entry(state_file, "undead")
    assert entry["last_zombie_run_id"] == "run-z"  # old run marked failed
    assert entry["last_dispatch_at"]


# =============================================================================
# AC-2 — merge policy: PR-for-review instruction, no merge invocation,
# no auto-merge knob.
# =============================================================================


def test_dispatch_message_carries_pr_for_review_instruction(
    projects_dir, tmp_path, monkeypatch
):
    path, repo_dir = _dispatchable_project(projects_dir, tmp_path, monkeypatch, "kick")
    dispatch = DispatchRecorder(run_id="abc123")
    monkeypatch.setattr(engine_archon, "dispatch", dispatch)
    decide = DecideStub({"action": "reuse", "workflow": "wf", "message": "build it"})
    state_file = tmp_path / "cofounder-state.json"

    result = run_pass(
        settings=enabled_settings(projects_dir), state_file=state_file, decide=decide
    )

    assert result.project_outcomes["kick"] == PROJECT_DISPATCHED
    sent = dispatch.calls[0]["message"]
    assert sent.startswith("build it")
    assert MERGE_POLICY_INSTRUCTION in sent
    # Stored args carry the amended message: a zombie re-dispatch replays
    # the merge policy too.
    entry = project_entry(state_file, "kick")
    assert MERGE_POLICY_INSTRUCTION in entry["last_dispatch_args"]["message"]


def test_merge_policy_append_is_idempotent(projects_dir, tmp_path, monkeypatch):
    """A message that already carries the instruction never stacks it."""
    _dispatchable_project(projects_dir, tmp_path, monkeypatch, "redo")
    dispatch = DispatchRecorder(run_id="abc124")
    monkeypatch.setattr(engine_archon, "dispatch", dispatch)
    decide = DecideStub(
        {
            "action": "reuse",
            "workflow": "wf",
            "message": f"retry the build\n\n{MERGE_POLICY_INSTRUCTION}",
        }
    )

    run_pass(
        settings=enabled_settings(projects_dir),
        state_file=tmp_path / "cofounder-state.json",
        decide=decide,
    )

    assert dispatch.calls[0]["message"].count(MERGE_POLICY_INSTRUCTION) == 1


def test_greenfield_resolution_is_exempt_from_pr_instruction():
    """Greenfield (system-owned) repos may commit to main: the sentinel
    resolution never gets the instruction; tracked repos always do."""
    green = RepoResolution(
        slug=GREENFIELD_SLUG, local_path=None, default_branch=None, greenfield=True
    )
    assert _with_merge_policy("bootstrap it", green) == "bootstrap it"
    tracked = RepoResolution(
        slug="demo", local_path=Path("x"), default_branch="master"
    )
    amended = _with_merge_policy("build", tracked)
    assert amended.startswith("build")
    assert MERGE_POLICY_INSTRUCTION in amended


def test_no_merge_invocation_anywhere_in_cofounder_source():
    """The orchestrator NEVER merges: grep-level proof over every module in
    .claude/scripts/cofounder/ (the AC's enforcement clause)."""
    pkg_dir = Path(cofounder.__file__).resolve().parent
    forbidden = (
        "gh pr merge",
        "git merge",
        "pulls/merge",
        "merge_pull",
        "--merge",
        "auto-merge",
        "auto_merge",
    )
    sources = sorted(pkg_dir.glob("*.py"))
    assert sources, f"no cofounder modules found under {pkg_dir}"
    for module in sources:
        text = module.read_text(encoding="utf-8").lower()
        for pattern in forbidden:
            assert pattern not in text, (
                f"{module.name} contains forbidden merge invocation {pattern!r}"
            )


def test_no_auto_merge_knob_exists_in_v1():
    """No COFOUNDER_*MERGE* env knob and no merge field on the settings —
    enabling auto-merge later is its own PRP with its own gate."""
    assert not any(
        "merge" in field.lower() for field in config.CofounderSettings._fields
    )
    config_source = Path(config.__file__).read_text(encoding="utf-8")
    assert re.search(r"COFOUNDER_[A-Z_]*MERGE", config_source) is None
