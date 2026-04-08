"""Backend selection and fallback tests — Phase 6.

Proves:
- BackendSelector returns LocalExecutor for 'local' always
- Paperclip/workflow fall back to LocalExecutor when unconfigured
- 'auto' picks best available with final local fallback
- is_available() is cheap (no network calls)
- Fallback emits a WARNING log
- TeamService.dispatch_to_executor() routes via the team's backend_type
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from orchestration.convoy_service import ConvoyService  # noqa: E402
from orchestration.db import OrchestrationDB  # noqa: E402
from orchestration.executor import (  # noqa: E402
    BackendSelector,
    ExecutorRegistry,
    LocalExecutor,
    PaperclipExecutor,
    WorkflowRunnerExecutor,
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


def _make_registry(*, paperclip_url: str | None = None, workflow_url: str | None = None) -> ExecutorRegistry:
    """Build a registry with optional Paperclip/Workflow backends."""
    reg = ExecutorRegistry()
    reg.register(PaperclipExecutor(api_url=paperclip_url, api_key="k" if paperclip_url else None))
    reg.register(WorkflowRunnerExecutor(engine_url=workflow_url))
    return reg


# ── BackendSelector.select ────────────────────────────────────────────────


def test_select_local_always_returns_local_executor():
    reg = _make_registry()
    ex, actual = reg.backend_selector.select("local")
    assert isinstance(ex, LocalExecutor)
    assert actual == "local"


def test_select_paperclip_configured_returns_paperclip():
    reg = _make_registry(paperclip_url="https://paperclip.example.com")
    ex, actual = reg.backend_selector.select("paperclip")
    assert isinstance(ex, PaperclipExecutor)
    assert actual == "paperclip"


def test_select_paperclip_unconfigured_falls_back_to_local(caplog):
    reg = _make_registry()  # no paperclip URL
    with caplog.at_level(logging.WARNING, logger="orchestration.executor"):
        ex, actual = reg.backend_selector.select("paperclip")
    assert isinstance(ex, LocalExecutor)
    assert actual == "local"
    assert any("falling back" in r.message.lower() for r in caplog.records)


def test_select_workflow_unconfigured_falls_back_to_local():
    reg = _make_registry()  # no workflow URL
    ex, actual = reg.backend_selector.select("workflow")
    assert isinstance(ex, LocalExecutor)
    assert actual == "local"


def test_select_workflow_configured_returns_workflow():
    reg = _make_registry(workflow_url="https://wf.example.com")
    ex, actual = reg.backend_selector.select("workflow")
    assert isinstance(ex, WorkflowRunnerExecutor)
    assert actual == "workflow"


def test_select_auto_nothing_configured_returns_local():
    reg = _make_registry()
    ex, actual = reg.backend_selector.select("auto")
    assert isinstance(ex, LocalExecutor)
    assert actual == "local"


def test_select_auto_with_paperclip_returns_paperclip():
    reg = _make_registry(paperclip_url="https://pcp.example.com")
    ex, actual = reg.backend_selector.select("auto")
    assert isinstance(ex, PaperclipExecutor)
    assert actual == "paperclip"


def test_select_auto_paperclip_preferred_over_workflow():
    reg = _make_registry(
        paperclip_url="https://pcp.example.com",
        workflow_url="https://wf.example.com",
    )
    ex, actual = reg.backend_selector.select("auto")
    assert actual == "paperclip"  # paperclip comes first in auto chain
    assert isinstance(ex, PaperclipExecutor)


def test_select_auto_only_workflow_configured():
    reg = _make_registry(workflow_url="https://wf.example.com")
    ex, actual = reg.backend_selector.select("auto")
    assert isinstance(ex, WorkflowRunnerExecutor)
    assert actual == "workflow"


def test_select_unknown_backend_type_defaults_to_local(caplog):
    reg = _make_registry()
    with caplog.at_level(logging.WARNING, logger="orchestration.executor"):
        ex, actual = reg.backend_selector.select("nonexistent-backend")
    assert isinstance(ex, LocalExecutor)
    assert actual == "local"
    assert any("unknown backend_type" in r.message.lower() for r in caplog.records)


# ── BackendSelector.is_available ──────────────────────────────────────────


def test_is_available_local_always_true():
    reg = _make_registry()
    assert reg.backend_selector.is_available("local") is True


def test_is_available_paperclip_configured():
    reg = _make_registry(paperclip_url="https://pcp.example.com")
    assert reg.backend_selector.is_available("paperclip") is True


def test_is_available_paperclip_not_configured():
    reg = _make_registry()
    assert reg.backend_selector.is_available("paperclip") is False


def test_is_available_workflow_configured():
    reg = _make_registry(workflow_url="https://wf.example.com")
    assert reg.backend_selector.is_available("workflow") is True


def test_is_available_unknown_backend_false():
    reg = _make_registry()
    assert reg.backend_selector.is_available("nonexistent") is False


def test_registry_backend_selector_accessor():
    reg = _make_registry()
    selector = reg.backend_selector
    assert isinstance(selector, BackendSelector)
    # Same instance on subsequent access
    assert reg.backend_selector is selector


# ── TeamService.dispatch_to_executor ──────────────────────────────────────


def _create_team_with_subtask(db, *, backend_type: str = "local"):
    """Create a team + convoy with a ready subtask. Returns (team_id, subtask_id)."""
    convoy_svc = ConvoyService(db)
    team_svc = TeamService(db)

    convoy = convoy_svc.create_convoy(
        CreateConvoyInput(
            title="Backend Test",
            created_by="sb",
            subtasks=[CreateSubtaskInput(title="Dispatchable")],
        )
    )
    team = team_svc.create_team_session(
        CreateTeamSessionInput(
            team_name="backend-team",
            lead_agent_id="lead-1",
            convoy_id=convoy.convoy.id,
            backend_type=backend_type,  # type: ignore[arg-type]
        )
    )
    return team.session.id, convoy.subtasks[0].id


def test_dispatch_to_executor_local_backend(db):
    team_id, subtask_id = _create_team_with_subtask(db, backend_type="local")
    ts = TeamService(db)
    receipt, actual = ts.dispatch_to_executor(team_id, subtask_id)
    assert receipt.status == "accepted"
    assert receipt.executor_name == "local"
    assert actual == "local"


def test_dispatch_to_executor_auto_no_backends_configured_uses_local(db, monkeypatch):
    # Ensure env-driven defaults don't accidentally configure paperclip/workflow
    monkeypatch.delenv("PAPERCLIP_API_URL", raising=False)
    monkeypatch.delenv("PAPERCLIP_API_KEY", raising=False)
    monkeypatch.delenv("WORKFLOW_ENGINE_URL", raising=False)
    # Reset the process-wide default registry so env changes are picked up
    import orchestration.executor as ex_mod
    ex_mod._DEFAULT_REGISTRY = None

    team_id, subtask_id = _create_team_with_subtask(db, backend_type="auto")
    ts = TeamService(db)
    receipt, actual = ts.dispatch_to_executor(team_id, subtask_id)
    assert receipt.status == "accepted"
    assert actual == "local"


def test_dispatch_to_executor_unknown_team_raises(db):
    ts = TeamService(db)
    with pytest.raises(ValueError, match="not found"):
        ts.dispatch_to_executor(9999, 1)


def test_dispatch_to_executor_returns_actual_backend_on_fallback(db, monkeypatch, caplog):
    monkeypatch.delenv("PAPERCLIP_API_URL", raising=False)
    monkeypatch.delenv("PAPERCLIP_API_KEY", raising=False)
    import orchestration.executor as ex_mod
    ex_mod._DEFAULT_REGISTRY = None

    team_id, subtask_id = _create_team_with_subtask(db, backend_type="paperclip")
    ts = TeamService(db)
    with caplog.at_level(logging.WARNING, logger="orchestration.executor"):
        receipt, actual = ts.dispatch_to_executor(team_id, subtask_id)
    # Fallback to local because paperclip is unconfigured
    assert actual == "local"
    assert receipt.executor_name == "local"
    assert receipt.status == "accepted"
    assert any("falling back" in r.message.lower() for r in caplog.records)
