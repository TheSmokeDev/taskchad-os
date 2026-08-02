"""Archon live-telemetry ingest + dashboard bridge (epic #252 / ticket #254).

Path map — one non-vacuous test per distinct code path:

  read layer      ro-URI shape · write-refusal · db_missing · db_unreadable · happy
  sanitizer       null · bad JSON · non-object · nested flatten · secret redaction ·
                  oversize trim (small keys survive) · hostile key truncation
  normalizer      camelCase projection · non-int step_index
  cursor          `>=` inclusivity · seen-id suppression · cursor advance ·
                  boundary-set rebuild · limit spillover (no loss) · failure
                  leaves cursor UNCHANGED · NO tool_* filter (Archon divergence)
  snapshot        per-run · per-conversation correlation key · both (intersect) ·
                  empty correlation · newest-N reversed · db missing
  runs            projection · terminal detection (terminal/running/unknown/unreadable)
  join (#258)     parent_conversation_id filter is a DIFFERENT column · current
                  node newest-by-rowid (same-second handoff) · every live node
                  event type mapped · non-node events ignored · no-events /
                  unknown-run / blank-name → None · db-missing status
  filter          unscoped None vs empty frozenset vs run id
  channel         seq monotonic · ring eviction · since() · fan-out · slow-subscriber drop
  poller          no-loop start · idle skip · live drain · failure counters ·
                  physical running state
  REST route      happy · db-missing degradation (never 500) · kill-switch 503
  SSE route       kill-switch 503 · 410 + X-Refetch-Hint · Last-Event-ID override ·
                  terminal-run close · snapshot frame omits `id:`
  mounts          Hono proxy file + app mount + ROUTE_MANIFEST + route policy rows

Every DB test runs against a fixture SQLite file built from the live archon.db
DDL — no running Archon required.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config

REPO_ROOT = Path(__file__).resolve().parents[3]
DASHBOARD_SERVER = REPO_ROOT / "dashboard" / "server" / "src"


# ─────────────────────────────────────────────────────────────────────────────
# Fixture ledger — verbatim DDL from the live archon.db.
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


def _build_ledger(path: Path) -> None:
    """Create an empty fixture ledger with the real table shapes."""
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(_EVENTS_DDL)
        connection.execute(_RUNS_DDL)
        connection.commit()
    finally:
        connection.close()


def _add_run(
    path: Path,
    run_id: str,
    *,
    conversation_id: str = "conv-1",
    parent_conversation_id: str | None = None,
    workflow_name: str = "spike-echo",
    status: str = "running",
    started_at: str = "2026-07-27 18:00:00",
    completed_at: str | None = None,
    working_path: str | None = None,
) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "INSERT INTO remote_agent_workflow_runs "
            "(id, conversation_id, parent_conversation_id, workflow_name, user_message, "
            "status, started_at, completed_at, last_activity_at, working_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                conversation_id,
                parent_conversation_id,
                workflow_name,
                "do the thing",
                status,
                started_at,
                completed_at,
                started_at,
                working_path,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _add_event(
    path: Path,
    event_id: str,
    *,
    run_id: str = "run-1",
    event_type: str = "node_started",
    step_name: str | None = "implement",
    step_index: int | None = None,
    data: str = "{}",
    created_at: str = "2026-07-27 18:00:00",
) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "INSERT INTO remote_agent_workflow_events "
            "(id, workflow_run_id, event_type, step_index, step_name, data, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_id, run_id, event_type, step_index, step_name, data, created_at),
        )
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fixture archon.db wired into the call-time settings resolver."""
    path = tmp_path / "archon.db"
    _build_ledger(path)
    monkeypatch.setenv("ARCHON_EVENTS_DB", str(path))
    from integrations import archon_events

    archon_events._reset_channel()
    archon_events._reset_poller()
    yield path
    archon_events._reset_channel()
    archon_events._reset_poller()


@pytest.fixture
def events_mod(ledger: Path):
    from integrations import archon_events

    return archon_events


@pytest.fixture
def client(ledger: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Dashboard router on a scratch dashboard.db, pointed at the fixture ledger."""
    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", str(tmp_path / "dashboard.db"))
    from dashboard_db import get_connection

    get_connection().close()

    import dashboard_api

    app = FastAPI()
    app.include_router(dashboard_api.router)
    return TestClient(app)


# ─────────────────────────────────────────────────────────────────────────────
# Read layer
# ─────────────────────────────────────────────────────────────────────────────
def test_read_only_uri_physically_refuses_writes(events_mod, ledger: Path) -> None:
    """The mode=ro URI is the boundary — SQLite itself refuses the write."""
    uri = events_mod.read_only_uri(ledger)
    assert uri.startswith("file:")
    assert uri.endswith("?mode=ro")
    connection = sqlite3.connect(uri, uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute(
                "INSERT INTO remote_agent_workflow_events "
                "(id, workflow_run_id, event_type) VALUES ('x', 'y', 'z')"
            )
    finally:
        connection.close()


def test_missing_db_degrades_to_status_not_exception(
    events_mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARCHON_EVENTS_DB", str(tmp_path / "nope.db"))
    events, status = events_mod.read_recent_events()
    assert events == []
    assert status == events_mod.STATUS_DB_MISSING


def test_garbage_db_degrades_to_unreadable(
    events_mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that exists but is not SQLite must not raise past the boundary."""
    junk = tmp_path / "junk.db"
    junk.write_text("this is definitely not a sqlite database", encoding="utf-8")
    monkeypatch.setenv("ARCHON_EVENTS_DB", str(junk))
    events, status = events_mod.read_recent_events()
    assert events == []
    assert status == events_mod.STATUS_DB_UNREADABLE


# ─────────────────────────────────────────────────────────────────────────────
# Sanitizer — `data` is hostile input
# ─────────────────────────────────────────────────────────────────────────────
def test_sanitize_null_and_empty_blob(events_mod) -> None:
    assert events_mod.sanitize_event_data(None, max_chars=2000) == {}
    assert events_mod.sanitize_event_data("", max_chars=2000) == {}


def test_sanitize_invalid_json_is_preserved_not_dropped(events_mod) -> None:
    out = events_mod.sanitize_event_data("{not json at all", max_chars=2000)
    assert "value" in out
    assert "not json" in out["value"]


def test_sanitize_non_object_json_lands_under_value(events_mod) -> None:
    out = events_mod.sanitize_event_data("[1, 2, 3]", max_chars=2000)
    assert list(out) == ["value"]
    assert "1" in out["value"]


def test_sanitize_flattens_nested_tool_input(events_mod) -> None:
    """tool_input is nested and unbounded upstream — it must land as a string."""
    raw = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls -la", "n": [1, 2]}})
    out = events_mod.sanitize_event_data(raw, max_chars=2000)
    assert out["tool_name"] == "Bash"
    assert isinstance(out["tool_input"], str)
    assert "ls -la" in out["tool_input"]


def test_sanitize_redacts_secrets_in_values(events_mod) -> None:
    raw = json.dumps({"tool_input": {"command": "export OPENAI_API_KEY=<REDACTED-openai>"}})
    out = events_mod.sanitize_event_data(raw, max_chars=4000)
    assert "<REDACTED-openai>" not in out["tool_input"]


def test_sanitize_oversize_object_keeps_small_high_signal_keys(events_mod) -> None:
    """The trim drops the biggest values first — tool_name/duration_ms survive."""
    raw = json.dumps(
        {
            "tool_name": "Bash",
            "duration_ms": 1234,
            "node_output": "x" * 5000,
        }
    )
    out = events_mod.sanitize_event_data(raw, max_chars=300)
    assert out["tool_name"] == "Bash"
    assert out["duration_ms"] == 1234
    assert out["node_output"] == "[truncated]"
    assert out["_truncated"] is True


def test_sanitize_truncates_hostile_key(events_mod) -> None:
    raw = json.dumps({"k" * 500: "v"})
    out = events_mod.sanitize_event_data(raw, max_chars=2000)
    assert all(len(key) <= 64 for key in out)


def test_normalize_row_projects_camelcase_and_guards_step_index(events_mod) -> None:
    row = {
        "id": "e1",
        "workflow_run_id": "run-1",
        "event_type": "tool_called",
        "step_index": "not-an-int",
        "step_name": "implement",
        "data": json.dumps({"tool_name": "Bash"}),
        "created_at": "2026-07-27 18:00:00",
    }
    out = events_mod.normalize_event_row(row, max_data_chars=2000)
    assert out["id"] == "e1"
    assert out["runId"] == "run-1"
    assert out["type"] == "tool_called"
    assert out["stepIndex"] is None  # non-int coerced away, not crashed on
    assert out["stepName"] == "implement"
    assert out["data"]["tool_name"] == "Bash"


# ─────────────────────────────────────────────────────────────────────────────
# Cursor state machine
# ─────────────────────────────────────────────────────────────────────────────
def test_cursor_is_inclusive_at_the_boundary_second(events_mod, ledger: Path) -> None:
    """`created_at >= cursor` — a strict `>` would skip this row entirely."""
    _add_event(ledger, "e1", created_at="2026-07-27 18:00:00")
    result = events_mod.drain_events_since("2026-07-27 18:00:00")
    assert [e["id"] for e in result.events] == ["e1"]


def test_seen_ids_suppress_reemit_at_the_boundary(events_mod, ledger: Path) -> None:
    _add_event(ledger, "e1", created_at="2026-07-27 18:00:00")
    first = events_mod.drain_events_since("2026-07-27 18:00:00")
    assert [e["id"] for e in first.events] == ["e1"]
    second = events_mod.drain_events_since(first.cursor, first.seen_ids)
    assert second.events == []
    assert second.cursor == first.cursor


def test_cursor_advances_and_boundary_set_covers_skipped_rows(
    events_mod, ledger: Path
) -> None:
    """The new boundary set includes rows skipped this pass (Archon parity)."""
    _add_event(ledger, "e1", created_at="2026-07-27 18:00:00")
    _add_event(ledger, "e2", created_at="2026-07-27 18:00:05")
    _add_event(ledger, "e3", created_at="2026-07-27 18:00:05")
    first = events_mod.drain_events_since("2026-07-27 18:00:00")
    assert first.cursor == "2026-07-27 18:00:05"
    assert first.seen_ids == frozenset({"e2", "e3"})
    second = events_mod.drain_events_since(first.cursor, first.seen_ids)
    assert second.events == []


def test_limit_spillover_inside_one_second_paginates_instead_of_stalling(
    events_mod, ledger: Path
) -> None:
    """Regression: an id-set ALONE deadlocks here.

    Five rows share one boundary second and the drain limit is 2. With Archon's
    id-set-only cursor the ``LIMIT`` window keeps returning the same two
    already-seen head rows forever and NOTHING after them is ever emitted. The
    rowid watermark is what makes the window advance. Every row must arrive
    exactly once across successive drains.
    """
    for index in range(5):
        _add_event(ledger, f"e{index}", created_at="2026-07-27 18:00:00")

    seen: list[str] = []
    cursor = "2026-07-27 18:00:00"
    rowid = 0
    seen_ids: frozenset[str] = frozenset()
    for _ in range(4):
        result = events_mod.drain_events_since(
            cursor, seen_ids, last_rowid=rowid, limit=2
        )
        seen.extend(event["id"] for event in result.events)
        cursor, rowid, seen_ids = result.cursor, result.last_rowid, result.seen_ids

    assert seen == ["e0", "e1", "e2", "e3", "e4"]  # once each, in order


def test_late_row_at_the_boundary_second_is_still_caught(
    events_mod, ledger: Path
) -> None:
    """The `>=` inclusivity survives the watermark.

    A row written into the boundary second AFTER a drain already advanced past
    it gets a higher rowid, so `created_at >= cursor AND rowid > watermark`
    still finds it — a strict `created_at >` would drop it on the floor.
    """
    _add_event(ledger, "e0", created_at="2026-07-27 18:00:00")
    first = events_mod.drain_events_since("2026-07-27 18:00:00")
    assert [e["id"] for e in first.events] == ["e0"]

    # Same second, written late.
    _add_event(ledger, "e1", created_at="2026-07-27 18:00:00")
    second = events_mod.drain_events_since(
        first.cursor, first.seen_ids, last_rowid=first.last_rowid
    )
    assert [e["id"] for e in second.events] == ["e1"]


def test_drain_failure_leaves_cursor_untouched(
    events_mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient read failure costs latency, never events."""
    monkeypatch.setenv("ARCHON_EVENTS_DB", str(tmp_path / "gone.db"))
    result = events_mod.drain_events_since(
        "2026-07-27 18:00:00", frozenset({"keep"}), last_rowid=41
    )
    assert result.events == []
    assert result.cursor == "2026-07-27 18:00:00"
    assert result.last_rowid == 41
    assert result.seen_ids == frozenset({"keep"})
    assert result.status == events_mod.STATUS_DB_MISSING


def test_drain_does_not_filter_tool_events(events_mod, ledger: Path) -> None:
    """The deliberate divergence from Archon's poller: no tool_* strip."""
    _add_event(
        ledger,
        "e1",
        event_type="tool_called",
        data=json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}, "duration_ms": 7}),
        created_at="2026-07-27 18:00:00",
    )
    _add_event(ledger, "e2", event_type="hook_activity", created_at="2026-07-27 18:00:00")
    _add_event(ledger, "e3", event_type="task_activity", created_at="2026-07-27 18:00:00")
    result = events_mod.drain_events_since("2026-07-27 18:00:00")
    types = {e["type"] for e in result.events}
    assert {"tool_called", "hook_activity", "task_activity"} <= types
    tool_event = next(e for e in result.events if e["type"] == "tool_called")
    assert tool_event["data"]["tool_name"] == "Bash"
    assert tool_event["data"]["duration_ms"] == 7


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot reads
# ─────────────────────────────────────────────────────────────────────────────
def test_snapshot_filters_by_run(events_mod, ledger: Path) -> None:
    _add_event(ledger, "a1", run_id="run-1", created_at="2026-07-27 18:00:01")
    _add_event(ledger, "b1", run_id="run-2", created_at="2026-07-27 18:00:02")
    events, status = events_mod.read_recent_events(run_id="run-1")
    assert status == events_mod.STATUS_OK
    assert [e["id"] for e in events] == ["a1"]


def test_snapshot_filters_by_conversation_correlation_key(
    events_mod, ledger: Path
) -> None:
    _add_run(ledger, "run-1", conversation_id="conv-a")
    _add_run(ledger, "run-2", conversation_id="conv-b")
    _add_event(ledger, "a1", run_id="run-1", created_at="2026-07-27 18:00:01")
    _add_event(ledger, "b1", run_id="run-2", created_at="2026-07-27 18:00:02")
    events, status = events_mod.read_recent_events(conversation_id="conv-a")
    assert status == events_mod.STATUS_OK
    assert [e["id"] for e in events] == ["a1"]


def test_snapshot_intersects_run_and_conversation(events_mod, ledger: Path) -> None:
    """Both filters narrow; they must never widen the scope."""
    _add_run(ledger, "run-1", conversation_id="conv-a")
    _add_run(ledger, "run-2", conversation_id="conv-b")
    _add_event(ledger, "a1", run_id="run-1", created_at="2026-07-27 18:00:01")
    _add_event(ledger, "b1", run_id="run-2", created_at="2026-07-27 18:00:02")
    events, _ = events_mod.read_recent_events(run_id="run-2", conversation_id="conv-a")
    assert events == []


def test_snapshot_unknown_conversation_is_empty_ok(events_mod, ledger: Path) -> None:
    events, status = events_mod.read_recent_events(conversation_id="nope")
    assert events == []
    assert status == events_mod.STATUS_OK


def test_snapshot_returns_newest_rows_oldest_first(events_mod, ledger: Path) -> None:
    """A long run shows its TAIL, ordered for a transcript render."""
    for index in range(5):
        _add_event(ledger, f"e{index}", created_at=f"2026-07-27 18:00:0{index}")
    events, _ = events_mod.read_recent_events(limit=3)
    assert [e["id"] for e in events] == ["e2", "e3", "e4"]


def test_run_rows_projection_and_redaction(events_mod, ledger: Path) -> None:
    _add_run(
        ledger,
        "run-1",
        conversation_id="conv-a",
        status="completed",
        completed_at="2026-07-27 18:05:00",
        working_path="C:/work/tree",
    )
    runs, status = events_mod.read_run_rows(run_id="run-1")
    assert status == events_mod.STATUS_OK
    assert runs[0]["runId"] == "run-1"
    assert runs[0]["conversationId"] == "conv-a"
    assert runs[0]["status"] == "completed"
    assert runs[0]["completedAt"] == "2026-07-27 18:05:00"
    assert runs[0]["workingPath"] == "C:/work/tree"


@pytest.mark.parametrize(
    ("status", "expected"),
    [("completed", True), ("failed", True), ("running", False), ("pending", False)],
)
def test_run_is_terminal_reads_the_row(
    events_mod, ledger: Path, status: str, expected: bool
) -> None:
    _add_run(ledger, "run-1", status=status)
    assert events_mod.run_is_terminal("run-1") is expected


def test_run_is_terminal_false_on_unknown_and_unreadable(
    events_mod, ledger: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uncertainty must not close a live stream."""
    assert events_mod.run_is_terminal("no-such-run") is False
    monkeypatch.setenv("ARCHON_EVENTS_DB", str(tmp_path / "gone.db"))
    assert events_mod.run_is_terminal("run-1") is False


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch join + current node (epic #252 / ticket #258)
# ─────────────────────────────────────────────────────────────────────────────
def test_parent_conversation_filter_is_a_different_column(
    events_mod, ledger: Path
) -> None:
    """The dispatch join is `parent_conversation_id`, NOT `conversation_id`.

    Archon spawns a separate WORKER conversation for a web-dispatched run, so
    filtering the natural-looking way returns nothing — and "nothing" reads
    exactly like "not started yet". This asserts the two filters address
    different rows so a future refactor cannot quietly collapse them.
    """
    _add_run(
        ledger,
        "run-1",
        conversation_id="worker-conv",
        parent_conversation_id="dispatch-conv",
        workflow_name="epic-piv-ticket",
    )
    hit, status = events_mod.read_run_rows(parent_conversation_id="dispatch-conv")
    assert status == events_mod.STATUS_OK
    assert [r["runId"] for r in hit] == ["run-1"]
    assert hit[0]["workflowName"] == "epic-piv-ticket"

    # The dispatching conversation is NOT the run's own conversation_id.
    miss, _ = events_mod.read_run_rows(conversation_id="dispatch-conv")
    assert miss == []
    # ...and the pre-existing conversation_id filter still addresses the worker.
    worker, _ = events_mod.read_run_rows(conversation_id="worker-conv")
    assert [r["runId"] for r in worker] == ["run-1"]


def test_current_node_prefers_the_newest_node_event_by_rowid(
    events_mod, ledger: Path
) -> None:
    """The rowid tiebreak is load-bearing, not cosmetic.

    archon.db timestamps have 1-second resolution and a real node handoff lands
    `node_completed <prev>` and `node_started <next>` inside the SAME second
    (measured on run 2c6810717e185807a369f009ee7c0414). Ordering on the
    timestamp alone would flap between the finished node and the running one.
    """
    _add_run(ledger, "run-1")
    _add_event(
        ledger,
        "e1",
        run_id="run-1",
        event_type="node_completed",
        step_name="prep",
        created_at="2026-07-28 06:38:34",
    )
    _add_event(
        ledger,
        "e2",
        run_id="run-1",
        event_type="node_started",
        step_name="implement",
        created_at="2026-07-28 06:38:34",
    )
    node, status = events_mod.read_current_node("run-1")
    assert status == events_mod.STATUS_OK
    assert node == {
        "currentNode": "implement",
        "nodeStatus": "running",
        "eventType": "node_started",
        "at": "2026-07-28 06:38:34",
    }


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        ("node_started", "running"),
        ("node_completed", "completed"),
        ("node_failed", "failed"),
        ("node_skipped", "skipped"),
        ("node_skipped_prior_success", "skipped"),
    ],
)
def test_current_node_maps_every_live_node_event_type(
    events_mod, ledger: Path, event_type: str, expected: str
) -> None:
    """Every node event type present in the live ledger maps to a status."""
    _add_run(ledger, "run-1")
    _add_event(ledger, "e1", run_id="run-1", event_type=event_type, step_name="gate")
    node, _ = events_mod.read_current_node("run-1")
    assert node is not None
    assert node["nodeStatus"] == expected


def test_current_node_ignores_non_node_events(events_mod, ledger: Path) -> None:
    """A newer tool_called must not become the 'current node'.

    tool_* rows outnumber node rows ~17:1 in the live ledger, so an unfiltered
    'newest event' read would almost always answer with a tool call.
    """
    _add_run(ledger, "run-1")
    _add_event(
        ledger,
        "e1",
        run_id="run-1",
        event_type="node_started",
        step_name="implement",
        created_at="2026-07-28 06:38:34",
    )
    _add_event(
        ledger,
        "e2",
        run_id="run-1",
        event_type="tool_called",
        step_name="implement",
        data=json.dumps({"tool_name": "Bash"}),
        created_at="2026-07-28 06:41:00",
    )
    node, _ = events_mod.read_current_node("run-1")
    assert node is not None
    assert node["eventType"] == "node_started"


def test_current_node_none_for_no_events_unknown_run_and_blank_name(
    events_mod, ledger: Path
) -> None:
    """Three honest 'no node' cases — none of them invent a placeholder."""
    _add_run(ledger, "run-1")
    assert events_mod.read_current_node("run-1") == (None, events_mod.STATUS_OK)
    assert events_mod.read_current_node("no-such-run") == (None, events_mod.STATUS_OK)
    assert events_mod.read_current_node("") == (None, events_mod.STATUS_OK)
    _add_event(ledger, "e1", run_id="run-1", event_type="node_started", step_name="")
    assert events_mod.read_current_node("run-1") == (None, events_mod.STATUS_OK)


def test_current_node_degrades_on_a_missing_ledger(
    events_mod, ledger: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable ledger is a STATUS, never an exception and never a lie."""
    monkeypatch.setenv("ARCHON_EVENTS_DB", str(tmp_path / "gone.db"))
    node, status = events_mod.read_current_node("run-1")
    assert node is None
    assert status == events_mod.STATUS_DB_MISSING


# ─────────────────────────────────────────────────────────────────────────────
# Subscriber filter
# ─────────────────────────────────────────────────────────────────────────────
def test_event_matches_distinguishes_unscoped_from_empty_scope(events_mod) -> None:
    event = {"runId": "run-1"}
    assert events_mod.event_matches(event, run_id=None, run_ids=None) is True
    # An empty scope is a real answer ("no runs"), never the firehose.
    assert events_mod.event_matches(event, run_id=None, run_ids=frozenset()) is False
    assert events_mod.event_matches(event, run_id="run-1", run_ids=None) is True
    assert events_mod.event_matches(event, run_id="run-2", run_ids=None) is False


# ─────────────────────────────────────────────────────────────────────────────
# Channel
# ─────────────────────────────────────────────────────────────────────────────
def test_channel_seq_is_monotonic_and_since_is_strict(events_mod) -> None:
    channel = events_mod.ArchonEventChannel(max_buffer=10)
    assert channel.emit({"type": "a"}) == 1
    assert channel.emit({"type": "b"}) == 2
    assert [entry.seq for entry in channel.since(1)] == [2]
    assert channel.latest_seq() == 2
    assert channel.oldest_seq() == 1


def test_channel_ring_eviction_moves_oldest_seq(events_mod) -> None:
    channel = events_mod.ArchonEventChannel(max_buffer=3)
    for index in range(10):
        channel.emit({"type": "x", "i": index})
    assert channel.oldest_seq() == 8
    assert channel.latest_seq() == 10


@pytest.mark.asyncio
async def test_channel_fans_out_to_subscribers_and_unsub_is_idempotent(
    events_mod,
) -> None:
    channel = events_mod.ArchonEventChannel(max_buffer=10)
    queue, unsub = channel.subscribe()
    channel.emit({"type": "a"})
    entry = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert entry.event["type"] == "a"
    assert channel.listener_count() == 1
    unsub()
    unsub()  # idempotent
    assert channel.listener_count() == 0


@pytest.mark.asyncio
async def test_channel_slow_subscriber_drop_does_not_raise(events_mod) -> None:
    """A full subscriber queue drops the live frame; the tail keeps running."""
    channel = events_mod.ArchonEventChannel(max_buffer=100)
    _queue, unsub = channel.subscribe(queue_size=1)
    try:
        channel.emit({"type": "a"})
        channel.emit({"type": "b"})  # queue is full — must not raise
        assert channel.latest_seq() == 2
    finally:
        unsub()


def test_channel_overflow_sets_drop_marker_once_per_episode(
    events_mod,
) -> None:
    """Kimi R1 MAJOR 2: an overflow must become a visible gap signal.

    `consume_dropped` reads AND clears — True exactly once per drop episode,
    False when nothing was shed, and per-subscriber (a fast subscriber never
    sees a slow sibling's marker)."""
    channel = events_mod.ArchonEventChannel(max_buffer=100)
    slow, unsub_slow = channel.subscribe(queue_size=1)
    fast, unsub_fast = channel.subscribe(queue_size=10)
    try:
        assert channel.consume_dropped(slow) is False
        channel.emit({"type": "a"})
        channel.emit({"type": "b"})  # slow overflows here
        assert channel.consume_dropped(slow) is True
        assert channel.consume_dropped(slow) is False  # cleared by the read
        assert channel.consume_dropped(fast) is False  # per-subscriber
        unsub_slow()
        # After unsubscribe the marker is gone, not resurrected.
        assert channel.consume_dropped(slow) is False
    finally:
        unsub_slow()
        unsub_fast()


def test_archon_stream_synthetic_frames_never_carry_an_id_line() -> None:
    """Kimi R1 MAJOR 1 tripwire: within the Archon telemetry section of
    dashboard_api.py, no frame may be emitted as `_sse_format(0, ...)` — pings
    and synthesized frames go through `_sse_format_no_id` so the browser's
    Last-Event-ID cursor only ever names a real ring seq."""
    source = (REPO_ROOT / ".claude" / "scripts" / "dashboard_api.py").read_text(
        encoding="utf-8"
    )
    start = source.index("Archon live telemetry (epic #252")
    section = source[start:]
    assert "_sse_format(0," not in section
    assert "_sse_format_no_id" in section


# ─────────────────────────────────────────────────────────────────────────────
# Poller
# ─────────────────────────────────────────────────────────────────────────────
def test_poller_start_without_loop_returns_false(events_mod) -> None:
    """Importable and testable outside an event loop — REST needs no poller."""
    poller = events_mod.ArchonEventPoller()
    assert poller.start() is False
    assert poller.is_running() is False


@pytest.mark.asyncio
async def test_poller_skips_the_query_when_nobody_is_listening(
    events_mod, ledger: Path
) -> None:
    """Idle = cheap: no rows emitted, cursor kept fresh (Archon parity)."""
    _add_event(ledger, "e1", created_at="2026-07-27 18:00:00")
    poller = events_mod.ArchonEventPoller()
    poller.cursor = "2026-07-27 18:00:00"
    emitted = await poller.drain_once()
    assert emitted == 0
    assert events_mod.get_channel().latest_seq() == 0
    assert poller.cursor != "2026-07-27 18:00:00"  # advanced to now


@pytest.mark.asyncio
async def test_poller_drains_into_the_channel_for_a_live_subscriber(
    events_mod, ledger: Path
) -> None:
    _add_event(ledger, "e1", event_type="tool_called", created_at="2026-07-27 18:00:00")
    channel = events_mod.get_channel()
    _queue, unsub = channel.subscribe()
    try:
        poller = events_mod.ArchonEventPoller()
        poller.cursor = "2026-07-27 18:00:00"
        emitted = await poller.drain_once()
        assert emitted == 1
        assert channel.latest_seq() == 1
        assert poller.consecutive_failures == 0
        assert poller.snapshot()["status"] == events_mod.STATUS_OK
    finally:
        unsub()


@pytest.mark.asyncio
async def test_poller_counts_consecutive_failures_and_holds_the_cursor(
    events_mod, ledger: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel = events_mod.get_channel()
    _queue, unsub = channel.subscribe()
    try:
        monkeypatch.setenv("ARCHON_EVENTS_DB", str(tmp_path / "gone.db"))
        poller = events_mod.ArchonEventPoller()
        poller.cursor = "2026-07-27 18:00:00"
        for _ in range(events_mod.FAILURE_ESCALATION_THRESHOLD):
            assert await poller.drain_once() == 0
        assert poller.consecutive_failures >= events_mod.FAILURE_ESCALATION_THRESHOLD
        assert poller.cursor == "2026-07-27 18:00:00"
        assert poller.snapshot()["lastError"] == events_mod.STATUS_DB_MISSING
    finally:
        unsub()


@pytest.mark.asyncio
async def test_poller_running_state_reads_the_task_not_a_flag(
    events_mod, ledger: Path
) -> None:
    """Rule 2 — is_running() inspects the physical asyncio task."""
    poller = events_mod.ArchonEventPoller()
    assert poller.start(interval_s=60) is True
    try:
        assert poller.is_running() is True
        assert poller.snapshot()["running"] is True
    finally:
        poller.stop()
    assert poller.is_running() is False


# ─────────────────────────────────────────────────────────────────────────────
# REST route
# ─────────────────────────────────────────────────────────────────────────────
def test_rest_snapshot_returns_events_and_runs(client: TestClient, ledger: Path) -> None:
    _add_run(ledger, "run-1", conversation_id="conv-a", status="running")
    _add_event(
        ledger,
        "e1",
        run_id="run-1",
        event_type="tool_called",
        data=json.dumps({"tool_name": "Bash", "duration_ms": 12}),
        created_at="2026-07-27 18:00:01",
    )
    response = client.get("/api/archon/events?runId=run-1")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["status"] == "ok"
    assert [e["id"] for e in body["events"]] == ["e1"]
    assert body["events"][0]["data"]["tool_name"] == "Bash"
    assert body["runs"][0]["conversationId"] == "conv-a"
    assert "latestSeq" in body
    assert body["poller"]["running"] in (True, False)


def test_rest_snapshot_degrades_instead_of_500_when_ledger_is_gone(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Archon not installed / not yet run must not break the dashboard page."""
    monkeypatch.setenv("ARCHON_EVENTS_DB", str(tmp_path / "absent.db"))
    response = client.get("/api/archon/events")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["status"] == "db_missing"
    assert body["events"] == []


def test_rest_snapshot_kill_switch_refuses_and_counts(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from security import kill_switches

    before = kill_switches.get_refusal_counters().get("archon_events", 0)
    monkeypatch.setenv("HOMIE_KILLSWITCH_ARCHON_EVENTS", "disabled")
    response = client.get("/api/archon/events")
    assert response.status_code == 503
    assert response.json()["detail"]["switch"] == "archon_events"
    after = kill_switches.get_refusal_counters().get("archon_events", 0)
    assert after == before + 1


# ─────────────────────────────────────────────────────────────────────────────
# SSE route
# ─────────────────────────────────────────────────────────────────────────────
def test_stream_kill_switch_refuses(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOMIE_KILLSWITCH_ARCHON_EVENTS", "disabled")
    response = client.get("/api/archon/stream")
    assert response.status_code == 503
    assert response.json()["switch"] == "archon_events"


def test_stream_410_with_refetch_hint_on_replay_gap(
    client: TestClient, events_mod, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARCHON_EVENTS_BUFFER_SIZE", "5")
    events_mod._reset_channel()
    channel = events_mod.get_channel()
    for index in range(20):
        channel.emit({"type": "ping", "i": index, "runId": "run-1"})
    # oldest_seq=16 → sinceSeq=2 is well outside the ring.
    response = client.get("/api/archon/stream?runId=run-1&sinceSeq=2")
    assert response.status_code == 410
    assert response.json()["error"] == "replay_gap"
    assert response.headers["X-Refetch-Hint"] == "GET /api/archon/events?runId=run-1"


def test_stream_last_event_id_overrides_since_seq(
    client: TestClient, events_mod, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The browser's reconnect header wins — proven through the 410 gate."""
    monkeypatch.setenv("ARCHON_EVENTS_BUFFER_SIZE", "5")
    events_mod._reset_channel()
    channel = events_mod.get_channel()
    for index in range(20):
        channel.emit({"type": "ping", "i": index})
    # sinceSeq=0 alone would NOT 410 (the gate needs sinceSeq > 0); the header
    # supplies the stale position, so a 410 here proves the override ran.
    response = client.get("/api/archon/stream", headers={"Last-Event-ID": "2"})
    assert response.status_code == 410
    assert response.json()["sinceSeq"] == 2


def test_stream_closes_on_a_terminal_run_after_the_snapshot(
    client: TestClient, ledger: Path
) -> None:
    """A finished run gets its backfill and a clean close, not an open socket."""
    _add_run(ledger, "run-1", status="completed", completed_at="2026-07-27 18:05:00")
    _add_event(ledger, "e1", run_id="run-1", created_at="2026-07-27 18:00:01")

    with client.stream("GET", "/api/archon/stream?runId=run-1") as response:
        assert response.status_code == 200
        chunks: list[bytes] = []
        for chunk in response.iter_raw():
            chunks.append(chunk)
            joined = b"".join(chunks)
            if b"run_ended" in joined or len(joined) > 20000:
                break

    body = b"".join(chunks).decode("utf-8", errors="replace")
    assert "archon_snapshot" in body
    assert "run_ended" in body
    assert '"id": "e1"' in body or '"id":"e1"' in body


def test_stream_snapshot_frame_omits_id_line(client: TestClient, ledger: Path) -> None:
    """A snapshot must not clobber the browser's lastEventId on reconnect."""
    _add_run(ledger, "run-1", status="completed")
    with client.stream("GET", "/api/archon/stream?runId=run-1") as response:
        chunks: list[bytes] = []
        for chunk in response.iter_raw():
            chunks.append(chunk)
            if b"run_ended" in b"".join(chunks):
                break
    body = b"".join(chunks).decode("utf-8", errors="replace")
    frames = [frame for frame in body.split("\n\n") if "data:" in frame]
    snapshot_frame = next(f for f in frames if "archon_snapshot" in f)
    assert not any(line.startswith("id:") for line in snapshot_frame.split("\n"))


# ─────────────────────────────────────────────────────────────────────────────
# Mounts + policy — the "mounted in Python but not in Hono" 404 class
# ─────────────────────────────────────────────────────────────────────────────
_ARCHON_ROUTES = ("/api/archon/events", "/api/archon/stream")


def test_routes_are_classified_in_route_policy() -> None:
    from orchestration.route_policy import ROUTE_POLICY

    for path in _ARCHON_ROUTES:
        assert ROUTE_POLICY[("GET", path)] == "admin"


def test_hono_proxy_file_mounts_both_routes() -> None:
    source = (DASHBOARD_SERVER / "routes" / "archon.ts").read_text(encoding="utf-8")
    for path in _ARCHON_ROUTES:
        assert f"'{path}'" in source, f"{path} not proxied in archon.ts"


def test_hono_app_mounts_the_archon_router() -> None:
    source = (DASHBOARD_SERVER / "app.ts").read_text(encoding="utf-8")
    assert "archonRoute" in source
    assert "app.route('/', archonRoute);" in source


def test_hono_route_manifest_lists_both_routes() -> None:
    source = (DASHBOARD_SERVER / "routes.ts").read_text(encoding="utf-8")
    for path in _ARCHON_ROUTES:
        assert f"'{path}'" in source, f"{path} missing from ROUTE_MANIFEST"


def test_hono_stream_handler_uses_streaming_fetch() -> None:
    """B3 lock — authedFetch() would buffer via .text() and kill the SSE."""
    source = (DASHBOARD_SERVER / "routes" / "archon.ts").read_text(encoding="utf-8")
    start = source.index("archonRoute.get('/api/archon/stream'")
    block = source[start:]
    assert "authedFetchStream(" in block
    assert "await authedFetch(" not in block
