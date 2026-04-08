"""Executor boundary tests — Phase 4 verification.

Proves:
- All executor adapters return normalized ExecutorReceipts
- ExecutorRegistry resolves by name with LocalExecutor fallback
- Framework can dispatch without Paperclip (GUI-off proof)
- Framework can dispatch without workflow engine (GUI-off proof)
- Adapters are downstream of framework state (never write to DB)
- Progress reporting works through the service layer
- Dispatch endpoint returns receipt via API
"""

import dataclasses
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


from orchestration.db import OrchestrationDB
from orchestration.convoy_service import ConvoyService
from orchestration.executor import (
    ExecutorAdapter,
    ExecutorRegistry,
    LocalExecutor,
    PaperclipExecutor,
    WorkflowRunnerExecutor,
    create_default_registry,
)
from orchestration.models import (
    CreateConvoyInput,
    CreateSubtaskInput,
    ExecutorReceipt,
    ProgressReport,
    Subtask,
)


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def db():
    d = OrchestrationDB(":memory:")
    yield d
    d.close()


@pytest.fixture
def convoy_svc(db):
    return ConvoyService(db)


@pytest.fixture
def convoy_with_ready_subtask(convoy_svc):
    """Create a convoy with a single ready subtask for dispatch testing."""
    result = convoy_svc.create_convoy(CreateConvoyInput(
        title="Executor Test",
        created_by="sb",
        subtasks=[CreateSubtaskInput(title="Dispatchable Task")],
    ))
    return result


# ── ExecutorReceipt model tests ───────────────────────────────────────────


def test_receipt_defaults():
    r = ExecutorReceipt()
    assert r.status == "accepted"
    assert r.executor_name == "local"
    assert r.external_ref is None
    assert r.error is None
    assert r.progress_pct is None
    assert r.metadata == {}
    assert r.timestamp == 0


def test_receipt_serializes_to_dict():
    r = ExecutorReceipt(status="completed", executor_name="paperclip", external_ref="PCP-123")
    d = dataclasses.asdict(r)
    assert d["status"] == "completed"
    assert d["executor_name"] == "paperclip"
    assert d["external_ref"] == "PCP-123"


# ── LocalExecutor tests ──────────────────────────────────────────────────


def test_local_executor_name():
    ex = LocalExecutor()
    assert ex.name == "local"


def test_local_executor_dispatch_returns_accepted_receipt():
    ex = LocalExecutor()
    subtask = Subtask(id=1, title="Test Task")
    receipt = ex.dispatch(subtask)
    assert isinstance(receipt, ExecutorReceipt)
    assert receipt.status == "accepted"
    assert receipt.executor_name == "local"
    assert receipt.timestamp > 0
    assert receipt.error is None


def test_local_executor_cancel_returns_cancelled_receipt():
    ex = LocalExecutor()
    receipt = ex.cancel(Subtask(id=1, title="Test"))
    assert receipt.status == "cancelled"
    assert receipt.executor_name == "local"


def test_local_executor_check_status_returns_receipt():
    ex = LocalExecutor()
    receipt = ex.check_status(Subtask(id=1, title="Test"))
    assert isinstance(receipt, ExecutorReceipt)
    assert receipt.executor_name == "local"


def test_local_executor_capabilities():
    caps = LocalExecutor().get_capabilities()
    assert caps["name"] == "local"
    assert caps["async_dispatch"] is False
    assert caps["progress_polling"] is False


# ── PaperclipExecutor tests ──────────────────────────────────────────────


def test_paperclip_executor_name():
    ex = PaperclipExecutor()
    assert ex.name == "paperclip"


def test_paperclip_unconfigured_rejects_dispatch():
    ex = PaperclipExecutor()
    assert not ex.is_configured
    receipt = ex.dispatch(Subtask(id=1, title="Test"))
    assert receipt.status == "rejected"
    assert receipt.executor_name == "paperclip"
    assert "not configured" in receipt.error


def test_paperclip_unconfigured_rejects_cancel():
    ex = PaperclipExecutor()
    receipt = ex.cancel(Subtask(id=1, title="Test"))
    assert receipt.status == "rejected"
    assert "not configured" in receipt.error


def test_paperclip_unconfigured_rejects_status():
    ex = PaperclipExecutor()
    receipt = ex.check_status(Subtask(id=1, title="Test"))
    assert receipt.status == "rejected"


def test_paperclip_configured_but_stub_rejects():
    ex = PaperclipExecutor(api_url="https://test.example.com", api_key="test-key")
    assert ex.is_configured
    receipt = ex.dispatch(Subtask(id=1, title="Test"))
    assert receipt.status == "rejected"
    assert "not yet implemented" in receipt.error


def test_paperclip_capabilities():
    ex = PaperclipExecutor(api_url="https://test.example.com", api_key="k")
    caps = ex.get_capabilities()
    assert caps["name"] == "paperclip"
    assert caps["async_dispatch"] is True
    assert caps["configured"] is True


def test_paperclip_capabilities_unconfigured():
    caps = PaperclipExecutor().get_capabilities()
    assert caps["configured"] is False


# ── WorkflowRunnerExecutor tests ─────────────────────────────────────────


def test_workflow_executor_name():
    ex = WorkflowRunnerExecutor()
    assert ex.name == "workflow"


def test_workflow_unconfigured_rejects_dispatch():
    ex = WorkflowRunnerExecutor()
    assert not ex.is_configured
    receipt = ex.dispatch(Subtask(id=1, title="Test"))
    assert receipt.status == "rejected"
    assert receipt.executor_name == "workflow"
    assert "not configured" in receipt.error


def test_workflow_unconfigured_rejects_cancel():
    ex = WorkflowRunnerExecutor()
    receipt = ex.cancel(Subtask(id=1, title="Test"))
    assert receipt.status == "rejected"


def test_workflow_configured_but_stub_rejects():
    ex = WorkflowRunnerExecutor(engine_url="https://workflow.example.com")
    assert ex.is_configured
    receipt = ex.dispatch(Subtask(id=1, title="Test"))
    assert receipt.status == "rejected"
    assert "not yet implemented" in receipt.error


def test_workflow_capabilities():
    ex = WorkflowRunnerExecutor(engine_url="https://test.com")
    caps = ex.get_capabilities()
    assert caps["name"] == "workflow"
    assert caps["async_dispatch"] is True
    assert caps["configured"] is True


# ── ExecutorRegistry tests ────────────────────────────────────────────────


def test_registry_has_local_by_default():
    reg = ExecutorRegistry()
    assert "local" in reg.available
    assert isinstance(reg.get("local"), LocalExecutor)


def test_registry_resolve_returns_local_for_unknown():
    reg = ExecutorRegistry()
    ex = reg.resolve("nonexistent")
    assert ex.name == "local"


def test_registry_resolve_returns_local_for_none():
    reg = ExecutorRegistry()
    ex = reg.resolve(None)
    assert ex.name == "local"


def test_registry_resolve_returns_named_executor():
    reg = ExecutorRegistry()
    pce = PaperclipExecutor()
    reg.register(pce)
    assert reg.resolve("paperclip").name == "paperclip"


def test_registry_register_and_list():
    reg = ExecutorRegistry()
    reg.register(PaperclipExecutor())
    reg.register(WorkflowRunnerExecutor())
    assert set(reg.available) == {"local", "paperclip", "workflow"}


def test_registry_list_capabilities():
    reg = ExecutorRegistry()
    reg.register(PaperclipExecutor())
    caps = reg.list_capabilities()
    assert len(caps) == 2
    names = {c["name"] for c in caps}
    assert names == {"local", "paperclip"}


def test_create_default_registry_has_all_three():
    reg = create_default_registry()
    assert set(reg.available) == {"local", "paperclip", "workflow"}


# ── Convoy dispatch with executor boundary ────────────────────────────────


def test_dispatch_with_default_local_executor(convoy_svc, convoy_with_ready_subtask):
    """Proof: framework dispatches without Paperclip or workflow engine."""
    subtask = convoy_with_ready_subtask.subtasks[0]
    receipt = convoy_svc.dispatch_subtask(subtask.id)  # no executor = LocalExecutor
    assert receipt.status == "accepted"
    assert receipt.executor_name == "local"

    # Framework state updated
    updated = convoy_svc.get_convoy(convoy_with_ready_subtask.convoy.id)
    assert updated.subtasks[0].status == "dispatched"
    assert updated.convoy.status == "active"


def test_dispatch_with_explicit_local_executor(convoy_svc, convoy_with_ready_subtask):
    subtask = convoy_with_ready_subtask.subtasks[0]
    receipt = convoy_svc.dispatch_subtask(subtask.id, executor=LocalExecutor())
    assert receipt.status == "accepted"
    assert receipt.executor_name == "local"


def test_dispatch_with_unconfigured_paperclip_keeps_subtask_ready(convoy_svc, convoy_with_ready_subtask):
    """Rejected executor requests should not fake a downstream dispatch."""
    subtask = convoy_with_ready_subtask.subtasks[0]
    pce = PaperclipExecutor()  # unconfigured
    receipt = convoy_svc.dispatch_subtask(subtask.id, executor=pce)
    assert receipt.status == "rejected"
    assert "not configured" in receipt.error

    # Framework records the failed attempt but keeps the subtask ready.
    updated = convoy_svc.get_convoy(convoy_with_ready_subtask.convoy.id)
    assert updated.subtasks[0].status == "ready"
    assert updated.convoy.status == "draft"


def test_dispatch_with_unconfigured_workflow_keeps_subtask_ready(convoy_svc, convoy_with_ready_subtask):
    """Workflow executor absence should not move the framework state machine."""
    subtask = convoy_with_ready_subtask.subtasks[0]
    wfe = WorkflowRunnerExecutor()  # unconfigured
    receipt = convoy_svc.dispatch_subtask(subtask.id, executor=wfe)
    assert receipt.status == "rejected"

    # Framework remains ready to try a valid executor later.
    updated = convoy_svc.get_convoy(convoy_with_ready_subtask.convoy.id)
    assert updated.subtasks[0].status == "ready"
    assert updated.convoy.status == "draft"


def test_dispatch_records_executor_error_in_attempt(db, convoy_svc, convoy_with_ready_subtask):
    """Proof: executor errors are recorded in the attempt table for auditing."""
    subtask = convoy_with_ready_subtask.subtasks[0]
    pce = PaperclipExecutor()
    convoy_svc.dispatch_subtask(subtask.id, executor=pce)

    # Check attempt record
    row = db.conn.execute(
        "SELECT * FROM attempts WHERE subtask_id = ?", (subtask.id,)
    ).fetchone()
    assert row["status"] == "failed"  # executor rejected
    assert "not configured" in row["error_message"]


def test_dispatch_records_accepted_attempt(db, convoy_svc, convoy_with_ready_subtask):
    subtask = convoy_with_ready_subtask.subtasks[0]
    convoy_svc.dispatch_subtask(subtask.id, executor=LocalExecutor())

    row = db.conn.execute(
        "SELECT * FROM attempts WHERE subtask_id = ?", (subtask.id,)
    ).fetchone()
    assert row["status"] == "sent"  # accepted
    assert row["error_message"] is None


def test_dispatch_with_explicit_paperclip_issue_id(convoy_svc, convoy_with_ready_subtask):
    """Explicit paperclip_issue_id takes precedence over executor external_ref."""
    subtask = convoy_with_ready_subtask.subtasks[0]
    receipt = convoy_svc.dispatch_subtask(
        subtask.id,
        paperclip_issue_id="MANUAL-123",
        executor=LocalExecutor(),
    )
    assert receipt.status == "accepted"

    updated = convoy_svc.get_convoy(convoy_with_ready_subtask.convoy.id)
    assert updated.subtasks[0].paperclip_issue_id == "MANUAL-123"


# ── Adapters are downstream (never write to DB) ──────────────────────────


def test_executor_does_not_write_to_db(db, convoy_svc, convoy_with_ready_subtask):
    """Proof: executor adapters don't receive DB access and cannot write state."""
    subtask = convoy_with_ready_subtask.subtasks[0]

    # Count rows before dispatch
    before_convoys = db.conn.execute("SELECT COUNT(*) as c FROM convoys").fetchone()["c"]
    before_subtasks = db.conn.execute("SELECT COUNT(*) as c FROM subtasks").fetchone()["c"]

    # Dispatch through all executors — none should create extra DB rows
    for ex in [LocalExecutor(), PaperclipExecutor(), WorkflowRunnerExecutor()]:
        # Only first dispatch will succeed (subtask becomes non-ready after)
        # But all executor.dispatch() calls are safe — they don't touch DB
        ex.dispatch(subtask)

    # DB unchanged by executor calls (only service layer writes)
    after_convoys = db.conn.execute("SELECT COUNT(*) as c FROM convoys").fetchone()["c"]
    after_subtasks = db.conn.execute("SELECT COUNT(*) as c FROM subtasks").fetchone()["c"]
    assert after_convoys == before_convoys
    assert after_subtasks == before_subtasks


# ── Progress reporting ────────────────────────────────────────────────────


def test_report_progress(convoy_svc, convoy_with_ready_subtask):
    subtask = convoy_with_ready_subtask.subtasks[0]
    convoy_svc.dispatch_subtask(subtask.id)

    progress = ProgressReport(
        subtask_id=subtask.id,
        convoy_id=convoy_with_ready_subtask.convoy.id,
        executor_name="local",
        progress_pct=0.75,
        message="Three quarters done",
        timestamp=int(time.time()),
    )
    convoy_svc.report_progress(subtask.id, progress)

    updated = convoy_svc.get_convoy(convoy_with_ready_subtask.convoy.id)
    meta = json.loads(updated.subtasks[0].metadata)
    assert meta["last_progress"]["pct"] == 0.75
    assert meta["last_progress"]["message"] == "Three quarters done"
    assert meta["last_progress"]["executor"] == "local"


def test_report_progress_preserves_existing_metadata(convoy_svc):
    """Progress updates merge into existing metadata, don't overwrite."""
    result = convoy_svc.create_convoy(CreateConvoyInput(
        title="Meta Test",
        created_by="sb",
        subtasks=[CreateSubtaskInput(title="Task", metadata='{"custom": "data"}')],
    ))
    subtask = result.subtasks[0]
    convoy_svc.dispatch_subtask(subtask.id)

    progress = ProgressReport(subtask_id=subtask.id, convoy_id=result.convoy.id,
                              progress_pct=0.5, message="Half")
    convoy_svc.report_progress(subtask.id, progress)

    updated = convoy_svc.get_convoy(result.convoy.id)
    meta = json.loads(updated.subtasks[0].metadata)
    assert meta["custom"] == "data"  # preserved
    assert meta["last_progress"]["pct"] == 0.5  # added


def test_report_progress_not_found(convoy_svc):
    with pytest.raises(ValueError, match="not found"):
        convoy_svc.report_progress(9999, ProgressReport())


# ── Custom executor via subclass ──────────────────────────────────────────


class MockExecutor(ExecutorAdapter):
    """Test executor that tracks calls and returns configurable receipts."""

    def __init__(self, dispatch_receipt: ExecutorReceipt | None = None):
        self._dispatch_receipt = dispatch_receipt or ExecutorReceipt(
            status="accepted", executor_name="mock", external_ref="MOCK-001",
            timestamp=int(time.time()),
        )
        self.dispatched: list[Subtask] = []
        self.cancelled: list[Subtask] = []

    @property
    def name(self) -> str:
        return "mock"

    def dispatch(self, subtask: Subtask) -> ExecutorReceipt:
        self.dispatched.append(subtask)
        return self._dispatch_receipt

    def cancel(self, subtask: Subtask) -> ExecutorReceipt:
        self.cancelled.append(subtask)
        return ExecutorReceipt(status="cancelled", executor_name="mock",
                               timestamp=int(time.time()))

    def check_status(self, subtask: Subtask) -> ExecutorReceipt:
        return ExecutorReceipt(status="accepted", executor_name="mock",
                               timestamp=int(time.time()))


def test_custom_executor_receives_subtask(convoy_svc, convoy_with_ready_subtask):
    """Proof: custom executors can be plugged in without changing framework."""
    subtask = convoy_with_ready_subtask.subtasks[0]
    mock = MockExecutor()
    receipt = convoy_svc.dispatch_subtask(subtask.id, executor=mock)

    assert receipt.status == "accepted"
    assert receipt.executor_name == "mock"
    assert receipt.external_ref == "MOCK-001"
    assert len(mock.dispatched) == 1
    assert mock.dispatched[0].id == subtask.id


def test_custom_executor_external_ref_stored(convoy_svc, convoy_with_ready_subtask):
    """External ref from executor is persisted in subtask.paperclip_issue_id."""
    subtask = convoy_with_ready_subtask.subtasks[0]
    mock = MockExecutor()
    convoy_svc.dispatch_subtask(subtask.id, executor=mock)

    updated = convoy_svc.get_convoy(convoy_with_ready_subtask.convoy.id)
    assert updated.subtasks[0].paperclip_issue_id == "MOCK-001"


def test_custom_executor_in_registry(convoy_svc, convoy_with_ready_subtask):
    """Custom executors work through the registry resolution path."""
    reg = ExecutorRegistry()
    reg.register(MockExecutor())
    assert reg.resolve("mock").name == "mock"

    subtask = convoy_with_ready_subtask.subtasks[0]
    receipt = convoy_svc.dispatch_subtask(subtask.id, executor=reg.resolve("mock"))
    assert receipt.executor_name == "mock"


# ── API integration tests for executor boundary ──────────────────────────


@pytest.fixture
def api_client(tmp_path):
    """API test client with isolated DB."""
    db_path = tmp_path / "test_executor_api.db"
    with patch("config.ORCHESTRATION_DB_PATH", db_path):
        import importlib
        import orchestration.api as api_mod

        importlib.reload(api_mod)
        db, cs, ms, reg, ts = api_mod._get_services()
        api_mod._db = db
        api_mod._convoy_svc = cs
        api_mod._mailbox_svc = ms
        api_mod._executor_registry = reg
        api_mod._team_svc = ts

        from fastapi.testclient import TestClient
        yield TestClient(api_mod.app)
        db.close()


def test_api_dispatch_returns_receipt(api_client):
    create = api_client.post("/api/convoy", json={"title": "API Exec", "created_by": "sb"})
    cid = create.json()["convoy"]["id"]
    subs = api_client.post(f"/api/convoy/{cid}/subtasks", json={
        "subtasks": [{"title": "API Task"}],
    }).json()
    sid = subs[0]["id"]

    r = api_client.post(f"/api/convoy/{cid}/subtask/{sid}/dispatch", json={})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "accepted"
    assert data["executor_name"] == "local"
    assert "timestamp" in data


def test_api_dispatch_with_executor_name(api_client):
    create = api_client.post("/api/convoy", json={"title": "Named Exec", "created_by": "sb"})
    cid = create.json()["convoy"]["id"]
    subs = api_client.post(f"/api/convoy/{cid}/subtasks", json={
        "subtasks": [{"title": "Named Task"}],
    }).json()
    sid = subs[0]["id"]

    # Dispatch with paperclip executor (unconfigured, will return rejected)
    r = api_client.post(f"/api/convoy/{cid}/subtask/{sid}/dispatch", json={
        "executor_name": "paperclip",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["executor_name"] == "paperclip"
    assert data["status"] == "rejected"


def test_api_dispatch_unknown_executor_returns_400(api_client):
    create = api_client.post("/api/convoy", json={"title": "Fallback", "created_by": "sb"})
    cid = create.json()["convoy"]["id"]
    subs = api_client.post(f"/api/convoy/{cid}/subtasks", json={
        "subtasks": [{"title": "Fallback Task"}],
    }).json()
    sid = subs[0]["id"]

    r = api_client.post(f"/api/convoy/{cid}/subtask/{sid}/dispatch", json={
        "executor_name": "nonexistent",
    })
    assert r.status_code == 400
    assert "unknown executor" in r.json()["detail"].lower()


def test_api_progress_endpoint(api_client):
    create = api_client.post("/api/convoy", json={"title": "Progress", "created_by": "sb"})
    cid = create.json()["convoy"]["id"]
    subs = api_client.post(f"/api/convoy/{cid}/subtasks", json={
        "subtasks": [{"title": "Progress Task"}],
    }).json()
    sid = subs[0]["id"]

    # Dispatch first
    api_client.post(f"/api/convoy/{cid}/subtask/{sid}/dispatch", json={})

    # Report progress
    r = api_client.post(f"/api/convoy/{cid}/subtask/{sid}/progress", json={
        "progress_pct": 0.6,
        "message": "Almost there",
        "executor_name": "local",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_api_list_executors(api_client):
    r = api_client.get("/api/executors")
    assert r.status_code == 200
    names = {e["name"] for e in r.json()}
    assert "local" in names
    assert "paperclip" in names
    assert "workflow" in names
