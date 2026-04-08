"""workspace_id propagation tests for dispatch_to_executor.

Proves that workspace_id flows correctly through the full dispatch chain:
  TeamService.dispatch_to_executor()
    → BackendSelector.select(workspace_id=...)
    → ConvoyService.dispatch_subtask(workspace_id=...)

Covers:
- Default workspace (backward compat)
- Non-default workspace threading
- Attempt record correctness per workspace
- Fallback chain with non-default workspaces
- BackendSelector receives workspace_id
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from orchestration.contract import DEFAULT_WORKSPACE_ID  # noqa: E402
from orchestration.convoy_service import ConvoyService  # noqa: E402
from orchestration.db import OrchestrationDB  # noqa: E402
from orchestration.executor import (  # noqa: E402
    BackendSelector,
    ExecutorRegistry,
    LocalExecutor,
)
from orchestration.models import (  # noqa: E402
    CreateConvoyInput,
    CreateSubtaskInput,
    CreateTeamSessionInput,
)
from orchestration.team_service import TeamService  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def db():
    d = OrchestrationDB(":memory:")
    yield d
    d.close()


def _create_team_with_subtask(
    db,
    *,
    backend_type: str = "local",
    workspace_id: int = DEFAULT_WORKSPACE_ID,
):
    """Create a team + convoy with a ready subtask in the given workspace.

    Returns (team_id, subtask_id).
    """
    convoy_svc = ConvoyService(db)
    team_svc = TeamService(db)

    convoy = convoy_svc.create_convoy(
        CreateConvoyInput(
            title="WS Dispatch Test",
            created_by="ws-test",
            subtasks=[CreateSubtaskInput(title="Dispatchable")],
        ),
        workspace_id=workspace_id,
    )
    team = team_svc.create_team_session(
        CreateTeamSessionInput(
            team_name="ws-team",
            lead_agent_id="lead-ws",
            convoy_id=convoy.convoy.id,
            backend_type=backend_type,  # type: ignore[arg-type]
        ),
        workspace_id=workspace_id,
    )
    return team.session.id, convoy.subtasks[0].id


# ── Default workspace (backward compat) ──────────────────────────────────


def test_dispatch_default_workspace_succeeds(db):
    """dispatch_to_executor works with the default workspace_id (backward compat)."""
    team_id, subtask_id = _create_team_with_subtask(db)
    ts = TeamService(db)
    receipt, actual = ts.dispatch_to_executor(team_id, subtask_id)
    assert receipt.status == "accepted"
    assert actual == "local"


def test_dispatch_default_workspace_attempt_recorded(db):
    """Attempt record is created with workspace_id = DEFAULT_WORKSPACE_ID."""
    team_id, subtask_id = _create_team_with_subtask(db)
    ts = TeamService(db)
    ts.dispatch_to_executor(team_id, subtask_id)

    row = db.conn.execute(
        "SELECT workspace_id FROM attempts WHERE subtask_id = ?",
        (subtask_id,),
    ).fetchone()
    assert row is not None
    assert row["workspace_id"] == DEFAULT_WORKSPACE_ID


# ── Non-default workspace threading ──────────────────────────────────────


def test_dispatch_nondefault_workspace_succeeds(db):
    """dispatch_to_executor with workspace_id=42 routes correctly."""
    team_id, subtask_id = _create_team_with_subtask(db, workspace_id=42)
    ts = TeamService(db)
    receipt, actual = ts.dispatch_to_executor(team_id, subtask_id, workspace_id=42)
    assert receipt.status == "accepted"
    assert actual == "local"


def test_dispatch_nondefault_workspace_attempt_recorded(db):
    """Attempt record for workspace_id=42 is scoped correctly."""
    team_id, subtask_id = _create_team_with_subtask(db, workspace_id=42)
    ts = TeamService(db)
    ts.dispatch_to_executor(team_id, subtask_id, workspace_id=42)

    row = db.conn.execute(
        "SELECT workspace_id FROM attempts WHERE subtask_id = ?",
        (subtask_id,),
    ).fetchone()
    assert row is not None
    assert row["workspace_id"] == 42


def test_dispatch_wrong_workspace_fails(db):
    """Dispatching with mismatched workspace_id raises ValueError.

    Subtask created in workspace 42 but dispatched with default workspace 1
    — the scoped SELECT returns None → 'not found'.
    """
    team_id, subtask_id = _create_team_with_subtask(db, workspace_id=42)
    ts = TeamService(db)
    with pytest.raises(ValueError, match="not found"):
        ts.dispatch_to_executor(team_id, subtask_id, workspace_id=DEFAULT_WORKSPACE_ID)


# ── BackendSelector receives workspace_id ─────────────────────────────────


def test_backend_selector_receives_workspace_id(db, monkeypatch):
    """BackendSelector.select() is called with the correct workspace_id kwarg."""
    monkeypatch.delenv("PAPERCLIP_API_URL", raising=False)
    monkeypatch.delenv("PAPERCLIP_API_KEY", raising=False)
    monkeypatch.delenv("WORKFLOW_ENGINE_URL", raising=False)
    import orchestration.executor as ex_mod
    ex_mod._DEFAULT_REGISTRY = None

    team_id, subtask_id = _create_team_with_subtask(db, workspace_id=99)
    ts = TeamService(db)

    called_with = {}
    original_select = BackendSelector.select

    def spy_select(self, backend_type, *, workspace_id=1):
        called_with["backend_type"] = backend_type
        called_with["workspace_id"] = workspace_id
        return original_select(self, backend_type, workspace_id=workspace_id)

    with patch.object(BackendSelector, "select", spy_select):
        ts.dispatch_to_executor(team_id, subtask_id, workspace_id=99)

    assert called_with["workspace_id"] == 99


# ── Fallback chain with non-default workspace ────────────────────────────


def test_fallback_with_nondefault_workspace(db, monkeypatch):
    """Paperclip fallback still works when workspace_id != 1."""
    monkeypatch.delenv("PAPERCLIP_API_URL", raising=False)
    monkeypatch.delenv("PAPERCLIP_API_KEY", raising=False)
    import orchestration.executor as ex_mod
    ex_mod._DEFAULT_REGISTRY = None

    team_id, subtask_id = _create_team_with_subtask(
        db, backend_type="paperclip", workspace_id=77,
    )
    ts = TeamService(db)
    receipt, actual = ts.dispatch_to_executor(team_id, subtask_id, workspace_id=77)
    # Falls back to local because paperclip is unconfigured
    assert actual == "local"
    assert receipt.executor_name == "local"
    assert receipt.status == "accepted"


def test_subtask_status_dispatched_with_nondefault_workspace(db):
    """After dispatch, the subtask status is 'dispatched' in the correct workspace."""
    team_id, subtask_id = _create_team_with_subtask(db, workspace_id=55)
    ts = TeamService(db)
    ts.dispatch_to_executor(team_id, subtask_id, workspace_id=55)

    row = db.conn.execute(
        "SELECT status FROM subtasks WHERE id = ? AND workspace_id = ?",
        (subtask_id, 55),
    ).fetchone()
    assert row is not None
    assert row["status"] == "dispatched"
