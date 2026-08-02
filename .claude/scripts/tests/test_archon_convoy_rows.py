"""Convoy row enrichment — real worker identity + current node (epic #252 / #258).

The Convoy page used to render `running` / `Unassigned` for voice-deployed work.
This route joins the convoy LEDGER row to Archon's own run ledger through the
#256 correlation key on `paperclip_issue_id`, so the row can name the workflow
that is actually running and the node it is on.

Path map — one non-vacuous test per distinct code path:

  join        correlated subtask -> workflow + run id + current node
              legacy `talk:<run_id>` ref (no Archon ids) -> NOT enriched
              no ref at all -> NOT enriched (zero regression for non-Archon work)
              ref present but no run row yet -> NOT enriched, still 200
  isolation   cross-workspace convoy -> 404 AND zero Archon reads (Rule 4)
              unknown convoy -> 404 · missing orchestration db -> 404
  degrade     missing archon.db -> 200 + honest status, never 500
  killswitch  503 + a counted refusal
  bound       > cap correlated rows -> truncated:true, never a silent cut
  mounts      Hono proxy file · ROUTE_MANIFEST · route policy (tenant_workspace)

Both ledgers are fixtures: a scratch orchestration sqlite file and an archon.db
built from the live DDL. No running Archon and no live convoy required.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import config  # noqa: E402
from orchestration.convoy_service import ConvoyService  # noqa: E402
from orchestration.db import OrchestrationDB  # noqa: E402
from orchestration.models import CreateConvoyInput, CreateSubtaskInput  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
DASHBOARD_SERVER = REPO_ROOT / "dashboard" / "server" / "src"

ROUTE = "/api/archon/convoy"

# Verbatim DDL from the live archon.db (same shapes as test_archon_events.py).
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


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures — two real ledgers on disk
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fixture archon.db wired into the call-time settings resolver."""
    path = tmp_path / "archon.db"
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(_EVENTS_DDL)
        connection.execute(_RUNS_DDL)
        connection.commit()
    finally:
        connection.close()
    monkeypatch.setenv("ARCHON_EVENTS_DB", str(path))
    from integrations import archon_events

    archon_events._reset_channel()
    archon_events._reset_poller()
    yield path
    archon_events._reset_channel()
    archon_events._reset_poller()


@pytest.fixture
def orch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A real orchestration DB on disk — the route opens it by config path."""
    path = tmp_path / "orch.db"
    monkeypatch.setattr(config, "ORCHESTRATION_DB_PATH", str(path))
    db = OrchestrationDB(str(path))
    yield ConvoyService(db)
    db.close()


@pytest.fixture
def client(ledger: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Dashboard router on a scratch dashboard.db, pointed at both fixtures."""
    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", str(tmp_path / "dashboard.db"))
    from dashboard_db import get_connection

    get_connection().close()

    import dashboard_api

    app = FastAPI()
    app.include_router(dashboard_api.router)
    return TestClient(app)


def _add_run(
    path: Path,
    run_id: str,
    *,
    parent_conversation_id: str,
    conversation_id: str = "worker-conv",
    workflow_name: str = "epic-piv-ticket",
    status: str = "running",
    started_at: str = "2026-07-28 06:37:00",
) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "INSERT INTO remote_agent_workflow_runs "
            "(id, conversation_id, parent_conversation_id, workflow_name, user_message, "
            "status, started_at, last_activity_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                conversation_id,
                parent_conversation_id,
                workflow_name,
                "slice the epic",
                status,
                started_at,
                started_at,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _add_node_event(
    path: Path,
    event_id: str,
    *,
    run_id: str,
    event_type: str = "node_started",
    step_name: str = "implement",
    created_at: str = "2026-07-28 06:38:34",
) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "INSERT INTO remote_agent_workflow_events "
            "(id, workflow_run_id, event_type, step_name, data, created_at) "
            "VALUES (?, ?, ?, ?, '{}', ?)",
            (event_id, run_id, event_type, step_name, created_at),
        )
        connection.commit()
    finally:
        connection.close()


def _convoy_with_subtasks(svc: ConvoyService, titles: list[str], *, workspace_id: int = 1):
    return svc.create_convoy(
        CreateConvoyInput(
            title="Spine work",
            created_by="talk",
            subtasks=[CreateSubtaskInput(title=t) for t in titles],
        ),
        workspace_id=workspace_id,
    )


def _correlate(svc: ConvoyService, subtask_id: int, ref: str, *, workspace_id: int = 1):
    """Write the correlation ref the way #256 writes it — through dispatch."""
    svc.dispatch_subtask(subtask_id, workspace_id=workspace_id, paperclip_issue_id=ref)


# ─────────────────────────────────────────────────────────────────────────────
# The join
# ─────────────────────────────────────────────────────────────────────────────
def test_correlated_subtask_gains_worker_identity_and_current_node(
    client: TestClient, orch: ConvoyService, ledger: Path
) -> None:
    """The epic's acceptance criterion: the row names the worker and its node."""
    import talk_archon

    convoy = _convoy_with_subtasks(orch, ["Implement #258"])
    subtask = convoy.subtasks[0]
    ref = talk_archon.build_correlation_ref(7, "dispatch-conv", "web-123")
    _correlate(orch, subtask.id, ref)

    _add_run(ledger, "run-abc", parent_conversation_id="dispatch-conv")
    _add_node_event(ledger, "e1", run_id="run-abc", step_name="implement")

    response = client.get(f"{ROUTE}/{convoy.convoy.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["status"] == "ok"
    assert body["truncated"] is False
    assert body["tasks"] == [
        {
            "subtaskId": subtask.id,
            "homieRunId": 7,
            "archonRunId": "run-abc",
            "conversationId": "web-123",
            "workflowName": "epic-piv-ticket",
            "runStatus": "running",
            "workingPath": None,
            "startedAt": "2026-07-28 06:37:00",
            "lastActivityAt": "2026-07-28 06:37:00",
            "currentNode": "implement",
            "nodeStatus": "running",
            "nodeAt": "2026-07-28 06:38:34",
        }
    ]


def test_uncorrelated_subtask_is_absent_so_the_row_renders_as_today(
    client: TestClient, orch: ConvoyService, ledger: Path
) -> None:
    """Zero regression for non-Archon work — no entry, not an empty entry."""
    import talk_archon

    convoy = _convoy_with_subtasks(orch, ["Archon work", "Hand work"])
    archon_task, hand_task = convoy.subtasks[0], convoy.subtasks[1]
    _correlate(
        orch,
        archon_task.id,
        talk_archon.build_correlation_ref(1, "dispatch-conv", "web-1"),
    )
    _add_run(ledger, "run-1", parent_conversation_id="dispatch-conv")

    body = client.get(f"{ROUTE}/{convoy.convoy.id}").json()
    ids = [t["subtaskId"] for t in body["tasks"]]
    assert ids == [archon_task.id]
    assert hand_task.id not in ids


def test_legacy_talk_ref_without_archon_ids_is_not_enriched(
    client: TestClient, orch: ConvoyService, ledger: Path
) -> None:
    """`talk:<run_id>` rows predate #256 and are real; they carry no join key.

    Emitting an all-null entry for them would put empty worker scaffolding on a
    row we know nothing about — worse than the pre-#258 render.
    """
    convoy = _convoy_with_subtasks(orch, ["Legacy dispatch"])
    _correlate(orch, convoy.subtasks[0].id, "talk:42")
    _add_run(ledger, "run-1", parent_conversation_id="dispatch-conv")

    body = client.get(f"{ROUTE}/{convoy.convoy.id}").json()
    assert body["status"] == "ok"
    assert body["tasks"] == []


def test_correlated_but_unregistered_run_is_absent_not_an_error(
    client: TestClient, orch: ConvoyService, ledger: Path
) -> None:
    """A dispatch registers its run a beat later; that gap is 'not yet', not a fault."""
    import talk_archon

    convoy = _convoy_with_subtasks(orch, ["Just dispatched"])
    _correlate(
        orch,
        convoy.subtasks[0].id,
        talk_archon.build_correlation_ref(3, "dispatch-conv", "web-3"),
    )
    # No run row inserted at all.
    response = client.get(f"{ROUTE}/{convoy.convoy.id}")
    assert response.status_code == 200
    assert response.json()["tasks"] == []


def test_run_without_node_events_reports_the_worker_with_a_null_node(
    client: TestClient, orch: ConvoyService, ledger: Path
) -> None:
    """Between workflow_started and the first node_started there IS no node."""
    import talk_archon

    convoy = _convoy_with_subtasks(orch, ["Starting"])
    _correlate(
        orch,
        convoy.subtasks[0].id,
        talk_archon.build_correlation_ref(4, "dispatch-conv", "web-4"),
    )
    _add_run(ledger, "run-4", parent_conversation_id="dispatch-conv")

    task = client.get(f"{ROUTE}/{convoy.convoy.id}").json()["tasks"][0]
    assert task["workflowName"] == "epic-piv-ticket"
    assert task["currentNode"] is None
    assert task["nodeStatus"] is None


# ─────────────────────────────────────────────────────────────────────────────
# Workspace isolation (Rule 4)
# ─────────────────────────────────────────────────────────────────────────────
def test_cross_workspace_convoy_404s_without_reading_archon_at_all(
    client: TestClient,
    orch: ConvoyService,
    ledger: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 4 — the gate is the STORAGE key, and the ledger is never touched.

    A 404 alone would not prove isolation: the handler could have read the
    Archon rows first and only then refused. This asserts the Archon reader is
    never called, so the enrichment cannot leak another workspace's run ids
    through timing or a partially-built response.
    """
    from integrations import archon_events

    convoy = _convoy_with_subtasks(orch, ["Tenant A work"], workspace_id=2)
    _add_run(ledger, "run-secret", parent_conversation_id="dispatch-conv")

    calls: list[str] = []

    def _tripwire(**kwargs):
        calls.append("read")
        raise AssertionError("archon ledger read on a cross-workspace request")

    monkeypatch.setattr(archon_events, "read_run_rows", _tripwire)

    # The default request carries workspace 1; the convoy lives in workspace 2.
    response = client.get(f"{ROUTE}/{convoy.convoy.id}")
    assert response.status_code == 404
    assert calls == []


def test_unknown_convoy_and_missing_orchestration_db_both_404(
    client: TestClient, orch: ConvoyService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert client.get(f"{ROUTE}/9999").status_code == 404
    monkeypatch.setattr(config, "ORCHESTRATION_DB_PATH", str(tmp_path / "absent.db"))
    assert client.get(f"{ROUTE}/1").status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# Degradation + kill switch
# ─────────────────────────────────────────────────────────────────────────────
def test_missing_archon_db_degrades_instead_of_500(
    client: TestClient,
    orch: ConvoyService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Archon not installed must not break the Convoy page — and must not read
    as 'this work has no Archon run' either. The status field carries the truth."""
    import talk_archon

    convoy = _convoy_with_subtasks(orch, ["Correlated"])
    _correlate(
        orch,
        convoy.subtasks[0].id,
        talk_archon.build_correlation_ref(5, "dispatch-conv", "web-5"),
    )
    monkeypatch.setenv("ARCHON_EVENTS_DB", str(tmp_path / "absent.db"))

    response = client.get(f"{ROUTE}/{convoy.convoy.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "db_missing"
    assert body["tasks"] == []


def test_kill_switch_refuses_and_counts(
    client: TestClient, orch: ConvoyService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from security import kill_switches

    convoy = _convoy_with_subtasks(orch, ["Anything"])
    before = kill_switches.get_refusal_counters().get("archon_events", 0)
    monkeypatch.setenv("HOMIE_KILLSWITCH_ARCHON_EVENTS", "disabled")
    response = client.get(f"{ROUTE}/{convoy.convoy.id}")
    assert response.status_code == 503
    assert response.json()["detail"]["switch"] == "archon_events"
    assert kill_switches.get_refusal_counters().get("archon_events", 0) == before + 1


def test_wide_convoy_is_bounded_and_says_so(
    client: TestClient, orch: ConvoyService, ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bound is fine; a SILENT bound reads as 'covered everything'."""
    import dashboard_api
    import talk_archon

    monkeypatch.setattr(dashboard_api, "_ARCHON_CONVOY_MAX_ROWS", 2)
    convoy = _convoy_with_subtasks(orch, ["a", "b", "c"])
    for index, subtask in enumerate(convoy.subtasks):
        conv = f"dispatch-{index}"
        _correlate(
            orch,
            subtask.id,
            talk_archon.build_correlation_ref(index + 1, conv, f"web-{index}"),
        )
        _add_run(ledger, f"run-{index}", parent_conversation_id=conv)

    body = client.get(f"{ROUTE}/{convoy.convoy.id}").json()
    assert body["truncated"] is True
    assert len(body["tasks"]) == 2


# ─────────────────────────────────────────────────────────────────────────────
# Mounts + policy — the "mounted in Python but not in Hono" 404 class
# ─────────────────────────────────────────────────────────────────────────────
def test_route_is_classified_on_the_convoy_grain() -> None:
    """tenant_workspace, NOT admin: this route is entered through a convoy id,
    which HAS a workspace column to scope by."""
    from orchestration.route_policy import ROUTE_POLICY

    assert ROUTE_POLICY[("GET", "/api/archon/convoy/{convoy_id}")] == "tenant_workspace"


def test_hono_proxy_and_manifest_mount_the_route() -> None:
    proxy = (DASHBOARD_SERVER / "routes" / "archon.ts").read_text(encoding="utf-8")
    assert "'/api/archon/convoy/:convoyId'" in proxy
    manifest = (DASHBOARD_SERVER / "routes.ts").read_text(encoding="utf-8")
    assert "'/api/archon/convoy/:convoyId'" in manifest


def test_a_cross_workspace_subtask_never_reaches_an_authorized_convoy(tmp_path):
    """Rule 4: the authorizing grain must reach the STORAGE query.

    Gate round 2 blocker. `get_convoy` filtered the convoy by
    `(id, workspace_id)` but fetched children by `convoy_id` alone, so a row
    written under another workspace came back inside an authorized convoy.
    That is not merely a stray row: a subtask's `paperclip_issue_id` IS the
    Archon correlation ref, and the #258 join FOLLOWS it — disclosing the
    other tenant's workflow, run id and current node.

    The shipped tests only put the WHOLE convoy in workspace 2, which the
    parent-level filter already caught. This is the mixed-grain case.
    """
    db = OrchestrationDB(tmp_path / "orchestration.db")
    svc = ConvoyService(db)
    created = _convoy_with_subtasks(svc, ["mine"], workspace_id=1)
    convoy_id = created.convoy.id

    # A row belonging to ANOTHER workspace, sharing this convoy id.
    db.conn.execute(
        "INSERT INTO subtasks (workspace_id, convoy_id, seq, title, status, "
        " paperclip_issue_id) VALUES (2, ?, 99, 'not yours', 'running', ?)",
        (convoy_id, "talk:9:archon:secret-dispatch:conv:secret-web"),
    )
    db.conn.commit()

    scoped = svc.get_convoy(convoy_id, workspace_id=1)

    assert scoped is not None
    titles = [task.title for task in scoped.subtasks]
    assert titles == ["mine"], f"cross-workspace row leaked: {titles}"
    refs = [task.paperclip_issue_id for task in scoped.subtasks]
    assert "talk:9:archon:secret-dispatch:conv:secret-web" not in refs
