"""Unified async-run registry tests — lifecycle, eviction, and the sentinel.

The registry is the seam between a voice tool call and the browser poller,
so the sentinel format is asserted against a literal copy of the regex the
Talk page uses. Workers here are plain callables — no subprocesses.
"""

from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS))

import talk_runs

# Literal copy of WORK_STARTED_RE in dashboard/web/src/pages/Talk.tsx — if the
# two drift, results stop being spoken and this test is the tripwire.
_BROWSER_SENTINEL_RE = re.compile(r"WORK_STARTED #(\d+) kind=(\w+)")


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    talk_runs.reset_for_tests()
    yield
    talk_runs.reset_for_tests()


def _wait_terminal(run_id: int, timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = talk_runs.get_run(run_id)
        if run and run["status"] in talk_runs.TERMINAL_STATUSES:
            return run
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} never reached a terminal status")


# ─── lifecycle ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", talk_runs.RUN_KINDS)
def test_each_kind_runs_to_done(kind: str) -> None:
    run_id = talk_runs.start_run(kind, "label", lambda _rid: f"{kind} finished")

    run = _wait_terminal(run_id)
    assert run["status"] == "done"
    assert run["output"] == f"{kind} finished"
    assert run["kind"] == kind


def test_worker_exception_marks_failed_with_speakable_text() -> None:
    def boom(_run_id: int) -> str:
        raise RuntimeError("the lane died")

    run = _wait_terminal(talk_runs.start_run("agent", "doomed", boom))

    assert run["status"] == "failed"
    assert "RuntimeError: the lane died" in run["output"]


def test_unknown_kind_is_rejected_before_a_thread_starts() -> None:
    with pytest.raises(ValueError, match="unknown run kind"):
        talk_runs.start_run("telepathy", "nope", lambda _rid: "never")


def test_unknown_run_id_reads_none() -> None:
    assert talk_runs.get_run(4242) is None


def test_worker_can_annotate_while_running() -> None:
    release = threading.Event()

    def worker(run_id: int) -> str:
        talk_runs.annotate_run(run_id, archon_run_id="abc123", archon_status="running")
        release.wait(timeout=2)
        return "done"

    run_id = talk_runs.start_run("archon", "archon-clutch", worker)
    for _ in range(100):
        if (talk_runs.get_run(run_id)["meta"] or {}).get("archon_run_id"):
            break
        time.sleep(0.02)

    assert talk_runs.get_run(run_id)["meta"]["archon_run_id"] == "abc123"
    assert talk_runs.get_run(run_id)["status"] == "running"
    release.set()
    _wait_terminal(run_id)


def test_annotate_unknown_run_is_a_noop() -> None:
    talk_runs.annotate_run(999, pid=1)  # must not raise


def test_finish_run_is_idempotent() -> None:
    run_id = talk_runs.start_run("look", "screen", lambda _rid: "first")
    _wait_terminal(run_id)

    talk_runs.finish_run(run_id, "failed", "second")

    assert talk_runs.get_run(run_id)["output"] == "first"


def test_finish_run_rejects_a_non_terminal_status() -> None:
    with pytest.raises(ValueError, match="not a terminal status"):
        talk_runs.finish_run(1, "running", "x")


def test_get_run_returns_a_snapshot_not_a_live_handle() -> None:
    run_id = talk_runs.start_run("skill", "vault-ops", lambda _rid: "ok")
    _wait_terminal(run_id)

    snapshot = talk_runs.get_run(run_id)
    snapshot["status"] = "mutated"
    snapshot["meta"]["injected"] = True

    assert talk_runs.get_run(run_id)["status"] == "done"
    assert "injected" not in talk_runs.get_run(run_id)["meta"]


def test_list_runs_is_newest_first_and_capped() -> None:
    for index in range(5):
        _wait_terminal(talk_runs.start_run("skill", f"s{index}", lambda _rid: "ok"))

    listed = talk_runs.list_runs(3)

    assert [row["runId"] for row in listed] == [5, 4, 3]
    assert listed[0]["label"] == "s4"


# ─── eviction ─────────────────────────────────────────────────────────────


def test_terminal_runs_older_than_the_ttl_are_evicted() -> None:
    stale_id = talk_runs.start_run("skill", "ancient", lambda _rid: "ok")
    _wait_terminal(stale_id)
    talk_runs._RUNS[stale_id]["updated"] = time.time() - (talk_runs._RUN_TTL_S + 60)

    _wait_terminal(talk_runs.start_run("skill", "fresh", lambda _rid: "ok"))

    assert talk_runs.get_run(stale_id) is None


def test_running_entries_are_never_evicted_by_age() -> None:
    release = threading.Event()
    long_id = talk_runs.start_run("archon", "slow", lambda _rid: release.wait(timeout=3) or "ok")
    for _ in range(50):
        if talk_runs.get_run(long_id):
            break
        time.sleep(0.02)
    talk_runs._RUNS[long_id]["updated"] = time.time() - (talk_runs._RUN_TTL_S + 60)

    _wait_terminal(talk_runs.start_run("skill", "fresh", lambda _rid: "ok"))

    assert talk_runs.get_run(long_id)["status"] == "running"
    release.set()


def test_registry_is_capped_at_max_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(talk_runs, "_MAX_RUNS", 5)

    for index in range(12):
        _wait_terminal(talk_runs.start_run("skill", f"s{index}", lambda _rid: "ok"))

    assert len(talk_runs._RUNS) <= 5
    # oldest terminal entries went first
    assert talk_runs.get_run(1) is None
    assert talk_runs.get_run(12) is not None


# ─── the browser contract ─────────────────────────────────────────────────


def test_sentinel_matches_the_browser_regex() -> None:
    sentinel = talk_runs.started_sentinel(12, "archon", "archon-clutch")

    match = _BROWSER_SENTINEL_RE.search(sentinel)

    assert match is not None
    assert match.group(1) == "12"
    assert match.group(2) == "archon"


def test_sentinel_survives_a_trailing_sentence() -> None:
    text = talk_runs.started_sentinel(3, "agent", "audit the site") + " It's running now."

    match = _BROWSER_SENTINEL_RE.search(text)

    assert match is not None and match.group(2) == "agent"


# ─── history tee (the Runs panel's durable tail) ──────────────────────────


import json  # noqa: E402

import config as config_mod  # noqa: E402


@pytest.fixture
def history_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Opt in to the tee (inert under pytest by default) on a tmp STATE_DIR."""

    monkeypatch.setattr(config_mod, "STATE_DIR", tmp_path)
    monkeypatch.setattr(talk_runs, "_history_enabled", lambda: True)
    return tmp_path


def _history_records(path: Path) -> list[dict]:
    file = path / talk_runs._HISTORY_FILENAME
    if not file.exists():
        return []
    return [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines()]


def test_history_tee_on_start_and_finish(history_env: Path) -> None:
    run_id = talk_runs.start_run("agent", "audit", lambda _rid: "all good")
    _wait_terminal(run_id)

    records = _history_records(history_env)
    mine = [r for r in records if r["runId"] == run_id]
    assert [r["status"] for r in mine] == ["running", "done"]
    assert mine[-1]["output"] == "all good"
    assert mine[-1]["kind"] == "agent"


def test_history_tee_is_inert_without_optin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under pytest the tee stays off unless a test opts in — suites that
    exercise the registry transitively must not write the operator's real
    STATE_DIR."""

    monkeypatch.setattr(config_mod, "STATE_DIR", tmp_path)
    run_id = talk_runs.start_run("skill", "s", lambda _rid: "x")
    _wait_terminal(run_id)

    assert _history_records(tmp_path) == []


def test_history_merge_marks_dead_process_runs_lost(history_env: Path) -> None:
    file = history_env / talk_runs._HISTORY_FILENAME
    file.write_text(
        json.dumps({"runId": 6, "kind": "skill", "label": "done one", "status": "done", "output": "ok", "ts": 1.0, "updated": 2.0})
        + "\n"
        + json.dumps({"runId": 7, "kind": "archon", "label": "died mid-flight", "status": "running", "ts": 3.0, "updated": 3.0})
        + "\n",
        encoding="utf-8",
    )

    runs = {r["runId"]: r for r in talk_runs.list_runs(50, include_history=True)}

    assert runs[7]["status"] == "lost"
    assert runs[7]["fromHistory"] is True
    assert runs[6]["status"] == "done"
    assert runs[6]["output"] == "ok"


def test_live_registry_wins_over_history(history_env: Path) -> None:
    run_id = talk_runs.start_run("agent", "live", lambda _rid: "fresh output")
    _wait_terminal(run_id)

    merged = [r for r in talk_runs.list_runs(50, include_history=True) if r["runId"] == run_id]

    assert len(merged) == 1
    assert merged[0]["status"] == "done"
    assert merged[0]["output"] == "fresh output"
    assert "fromHistory" not in merged[0]


def test_default_list_shape_is_unchanged(history_env: Path) -> None:
    """The Talk page's bare poll must not grow history rows."""

    file = history_env / talk_runs._HISTORY_FILENAME
    file.write_text(
        json.dumps({"runId": 99, "kind": "skill", "label": "old", "status": "done", "ts": 1.0, "updated": 1.0}) + "\n",
        encoding="utf-8",
    )
    run_id = talk_runs.start_run("look", "screen", lambda _rid: "seen")
    _wait_terminal(run_id)

    runs = talk_runs.list_runs()

    assert [r["runId"] for r in runs] == [run_id]


def test_seq_seeds_past_history(history_env: Path) -> None:
    file = history_env / talk_runs._HISTORY_FILENAME
    file.write_text(
        json.dumps({"runId": 41, "kind": "agent", "label": "old", "status": "done", "ts": 1.0, "updated": 1.0}) + "\n",
        encoding="utf-8",
    )
    talk_runs.reset_for_tests()

    run_id = talk_runs.start_run("agent", "new", lambda _rid: "x")
    _wait_terminal(run_id)

    assert run_id == 42


def test_history_compaction_keeps_newest(history_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(talk_runs, "_HISTORY_MAX_BYTES", 400)
    monkeypatch.setattr(talk_runs, "_HISTORY_COMPACT_KEEP", 3)

    ids = []
    for i in range(8):
        rid = talk_runs.start_run("skill", f"run {i}", lambda _rid: "out")
        _wait_terminal(rid)
        ids.append(rid)

    records = _history_records(history_env)
    kept_ids = {r["runId"] for r in records}
    assert len(kept_ids) <= 4  # keep cap plus at most the post-compact append
    assert ids[-1] in kept_ids  # newest survives
    assert ids[0] not in kept_ids  # oldest compacted away


def test_history_io_failure_is_fail_open(history_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom():
        raise OSError("disk gone")

    monkeypatch.setattr(talk_runs, "_history_path", _boom)

    run_id = talk_runs.start_run("agent", "still works", lambda _rid: "done anyway")
    run = _wait_terminal(run_id)

    assert run["status"] == "done"
    assert run["output"] == "done anyway"


def test_list_limit_is_clamped(history_env: Path) -> None:
    run_id = talk_runs.start_run("skill", "one", lambda _rid: "x")
    _wait_terminal(run_id)

    assert len(talk_runs.list_runs(0)) == 1
    assert talk_runs.list_runs(10_000)  # no explosion; server-side cap applies


# ─── history corruption (codex R1: one torn byte must cost one line) ──────


def test_invalid_utf8_costs_one_line_not_the_file(history_env: Path) -> None:
    file = history_env / talk_runs._HISTORY_FILENAME
    good_5 = json.dumps({"runId": 5, "kind": "skill", "label": "a", "status": "done", "ts": 1.0, "updated": 1.0})
    good_6 = json.dumps({"runId": 6, "kind": "agent", "label": "b", "status": "done", "ts": 2.0, "updated": 2.0})
    file.write_bytes(good_5.encode() + b"\n\xff\xfe torn line \xba\n" + good_6.encode() + b"\n")

    records = talk_runs._load_history()

    assert set(records.keys()) == {5, 6}
    runs = {r["runId"] for r in talk_runs.list_runs(50, include_history=True)}
    assert {5, 6} <= runs


def test_seed_survives_invalid_utf8(history_env: Path) -> None:
    file = history_env / talk_runs._HISTORY_FILENAME
    good = json.dumps({"runId": 41, "kind": "agent", "label": "old", "status": "done", "ts": 1.0, "updated": 1.0})
    file.write_bytes(good.encode() + b"\n\xff\xfe torn\n")
    talk_runs.reset_for_tests()

    run_id = talk_runs.start_run("agent", "new", lambda _rid: "x")
    _wait_terminal(run_id)

    assert run_id == 42


def test_seed_floor_when_history_unreadable(history_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A present-but-unreadable file must not seed colliding ids from zero."""

    class _UnreadablePath:
        def exists(self) -> bool:
            return True

        def read_text(self, *a, **k) -> str:
            raise OSError("locked by another process")

        # _append_history path operations — fail there too, fail-open.
        parent = history_env

        def open(self, *a, **k):
            raise OSError("locked by another process")

    monkeypatch.setattr(talk_runs, "_history_path", lambda: _UnreadablePath())
    talk_runs.reset_for_tests()

    run_id = talk_runs.start_run("agent", "still mints", lambda _rid: "x")
    run = _wait_terminal(run_id)

    assert run["status"] == "done"
    assert run_id > 1_000_000_000  # wall-clock floor, not a colliding small id


def test_compaction_survives_invalid_utf8(history_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A torn byte in the file must not wedge compaction into unbounded growth."""

    monkeypatch.setattr(talk_runs, "_HISTORY_MAX_BYTES", 400)
    monkeypatch.setattr(talk_runs, "_HISTORY_COMPACT_KEEP", 3)
    file = history_env / talk_runs._HISTORY_FILENAME
    file.write_bytes(b"\xff\xfe torn seed line\n")

    ids = []
    for i in range(8):
        rid = talk_runs.start_run("skill", f"run {i}", lambda _rid: "out")
        _wait_terminal(rid)
        ids.append(rid)

    records = _history_records(history_env)
    kept_ids = {r["runId"] for r in records}
    assert len(kept_ids) <= 4
    assert ids[-1] in kept_ids
    assert ids[0] not in kept_ids


# ─── steering primitives (queue / drain-or-finish / undelivered capture) ──


def test_queue_steer_refuses_unknown_wrong_kind_and_terminal() -> None:
    assert "don't have a run" in talk_runs.queue_steer(999, "go left")

    look_id = talk_runs.start_run("look", "screen", lambda rid: "seen")
    _wait_terminal(look_id)
    assert "steering reaches background agents only" in talk_runs.queue_steer(
        look_id, "go left"
    )

    gate = threading.Event()
    agent_id = talk_runs.start_run("agent", "a", lambda rid: gate.wait(5) and "done" or "done")
    gate.set()
    _wait_terminal(agent_id)
    assert "already finished" in talk_runs.queue_steer(agent_id, "too late")


def test_queue_steer_caps_the_queue() -> None:
    gate = threading.Event()
    run_id = talk_runs.start_run("agent", "a", lambda rid: gate.wait(5) and "x" or "x")
    for i in range(talk_runs.STEER_QUEUE_CAP):
        assert "Queued" in talk_runs.queue_steer(run_id, f"steer {i}")
    refusal = talk_runs.queue_steer(run_id, "one too many")
    assert "let it catch up" in refusal
    gate.set()
    _wait_terminal(run_id)


def test_drain_returns_steers_and_keeps_running() -> None:
    gate = threading.Event()
    run_id = talk_runs.start_run("agent", "a", lambda rid: gate.wait(5) and "x" or "x")
    talk_runs.queue_steer(run_id, "first")
    talk_runs.queue_steer(run_id, "second")

    drained = talk_runs.drain_steers_or_finish(run_id, "partial output")

    assert drained == ["first", "second"]
    run = talk_runs.get_run(run_id)
    assert run["status"] == "running"
    assert run["steers"] == []
    gate.set()
    _wait_terminal(run_id)


def test_drain_with_no_steers_finishes_done() -> None:
    gate = threading.Event()
    run_id = talk_runs.start_run("agent", "a", lambda rid: gate.wait(5) and "x" or "x")

    drained = talk_runs.drain_steers_or_finish(run_id, "the final answer")

    assert drained == []
    run = talk_runs.get_run(run_id)
    assert run["status"] == "done"
    assert run["output"] == "the final answer"
    gate.set()  # worker resumes, its own finish attempts are no-ops


def test_drain_on_terminal_or_unknown_returns_empty() -> None:
    assert talk_runs.drain_steers_or_finish(12345, "x") == []
    run_id = talk_runs.start_run("agent", "a", lambda rid: "quick")
    _wait_terminal(run_id)
    assert talk_runs.drain_steers_or_finish(run_id, "y") == []
    # And the terminal output was not overwritten by the late drain call.
    assert talk_runs.get_run(run_id)["output"] == "quick"


def test_finish_run_captures_undelivered_steers() -> None:
    gate = threading.Event()
    run_id = talk_runs.start_run("agent", "a", lambda rid: gate.wait(5) and "x" or "x")
    talk_runs.queue_steer(run_id, "never delivered")

    transitioned = talk_runs.finish_run(run_id, "failed", "cancelled by operator")

    assert transitioned is True
    run = talk_runs.get_run(run_id)
    assert run["meta"]["undelivered_steers"] == ["never delivered"]
    assert "arrived too late" in run["output"]
    # Verbatim steering text must NOT reach the output string — output is
    # teed to durable history; only the count is durable.
    assert "never delivered" not in run["output"]
    gate.set()


def test_finish_run_reports_whether_it_transitioned() -> None:
    run_id = talk_runs.start_run("agent", "a", lambda rid: "done fast")
    _wait_terminal(run_id)
    assert talk_runs.finish_run(run_id, "failed", "cancelled") is False
    assert talk_runs.get_run(run_id)["output"] == "done fast"


def test_snapshots_do_not_alias_the_steer_queue() -> None:
    gate = threading.Event()
    run_id = talk_runs.start_run("agent", "a", lambda rid: gate.wait(5) and "x" or "x")
    talk_runs.queue_steer(run_id, "held steer")

    snapshot = talk_runs.get_run(run_id)
    listed = talk_runs.list_runs(5)[0]
    talk_runs.drain_steers_or_finish(run_id, "out")

    # The drain replaced the live list; taken snapshots keep their copies.
    assert snapshot["steers"] == ["held steer"]
    assert listed["steers"] == ["held steer"]
    assert talk_runs.get_run(run_id)["steers"] == []
    gate.set()
    _wait_terminal(run_id)


def test_steers_never_reach_the_history_tee(history_env: Path) -> None:
    gate = threading.Event()
    run_id = talk_runs.start_run("agent", "a", lambda rid: gate.wait(5) and "x" or "x")
    talk_runs.queue_steer(run_id, "secret course correction")
    talk_runs.finish_run(run_id, "failed", "cancelled by operator")
    gate.set()

    records = _history_records(history_env)
    mine = [r for r in records if r["runId"] == run_id]
    assert mine, "terminal transition must still tee"
    for record in mine:
        assert "steers" not in record
        assert "meta" not in record
        # The redaction that matters: the steering TEXT must be absent from
        # the whole serialized record, not just from dedicated keys — the
        # output string is teed, so the text must never ride in it.
        assert "secret course correction" not in json.dumps(record)
