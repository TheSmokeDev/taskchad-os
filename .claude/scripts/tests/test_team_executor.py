"""Team executor action tests."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from orchestration.convoy_service import ConvoyService
from orchestration.db import OrchestrationDB
from orchestration.mailbox_service import MailboxService
from orchestration.models import (
    AddTeamMemberInput,
    CreateConvoyInput,
    CreateSubtaskInput,
    CreateTeamSessionInput,
)
from orchestration.team_executor import ExecutorCommandSpec, TeamExecutorService
from orchestration.team_loop import TeamTickService
from orchestration.team_service import TeamService


@pytest.fixture
def services(tmp_path):
    db = OrchestrationDB(":memory:")
    yield (
        db,
        ConvoyService(db),
        MailboxService(db),
        TeamService(db),
        tmp_path,
    )
    db.close()


def _test_commands() -> dict[str, ExecutorCommandSpec]:
    return {
        "test_ok": ExecutorCommandSpec(
            key="test_ok",
            argv=(sys.executable, "-c", "print('executor-ok')"),
            description="test command",
            timeout_seconds=10,
        )
    }


def _seed_running_team(services, repo_path: Path):
    db, convoy_svc, _mailbox_svc, team_svc, _tmp_path = services
    convoy = convoy_svc.create_convoy(
        CreateConvoyInput(
            title="Executor proof",
            created_by="dashboard",
            repo_path=str(repo_path),
            subtasks=[
                CreateSubtaskInput(
                    title="Run approved build probe",
                    assigned_agent_id="frontend-worker",
                    assigned_agent_name="Frontend Worker",
                ),
            ],
        )
    )
    subtask_id = convoy.subtasks[0].id
    team = team_svc.create_team_session(
        CreateTeamSessionInput(
            team_name="Builder Team",
            lead_agent_id="lead",
            lead_agent_name="Lead",
            convoy_id=convoy.convoy.id,
        )
    )
    team_svc.add_member(
        team.session.id,
        AddTeamMemberInput(
            agent_id="frontend-worker",
            agent_name="Frontend Worker",
            role="worker",
            subtask_id=subtask_id,
        ),
    )
    convoy_svc.dispatch_subtask(subtask_id)
    convoy_svc.transition_subtask(subtask_id, "running")
    return db, convoy.convoy.id, team.session.id, subtask_id


def test_team_executor_runs_approved_command_and_reports_mailbox(services, tmp_path):
    db, convoy_id, team_id, subtask_id = _seed_running_team(services, tmp_path)
    _db, _convoy_svc, mailbox_svc, _team_svc, _tmp_path = services
    executor_svc = TeamExecutorService(
        db,
        allowed_roots=[tmp_path],
        commands=_test_commands(),
    )

    result = executor_svc.run_executor_step(
        team_id,
        agent_id="frontend-worker",
        subtask_id=subtask_id,
        command_key="test_ok",
    )

    assert result.success is True
    assert result.exit_code == 0
    assert "executor-ok" in result.stdout
    assert result.cwd == str(tmp_path.resolve())

    messages = mailbox_svc.get_convoy_messages(convoy_id)
    assert any(entry.message.subject == "Executor step: test_ok" for entry in messages)
    assert any("executor-ok" in entry.message.body for entry in messages)


def test_team_executor_rejects_cwd_outside_approved_roots(services, tmp_path):
    db, _convoy_id, team_id, subtask_id = _seed_running_team(services, tmp_path)
    executor_svc = TeamExecutorService(
        db,
        allowed_roots=[tmp_path],
        commands=_test_commands(),
    )

    with pytest.raises(ValueError, match="outside approved roots"):
        executor_svc.run_executor_step(
            team_id,
            agent_id="frontend-worker",
            subtask_id=subtask_id,
            command_key="test_ok",
            cwd=str(tmp_path.parent),
        )


def test_team_tick_selects_executor_step_for_running_subtask(services, tmp_path):
    db, _convoy_id, team_id, subtask_id = _seed_running_team(services, tmp_path)
    executor_svc = TeamExecutorService(
        db,
        allowed_roots=[tmp_path],
        commands=_test_commands(),
    )

    result = TeamTickService(db, executor_svc=executor_svc).run_team_tick(
        team_id,
        execute_running=True,
        executor_command="test_ok",
    )

    assert result.selected_action == "executor_step"
    assert result.agent_id == "frontend-worker"
    assert result.subtask_id == subtask_id
    assert result.executor is not None
    assert result.executor.success is True
    assert "executor-ok" in result.executor.stdout


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "test_team_executor_api.db"
    with patch("config.ORCHESTRATION_DB_PATH", db_path):
        import importlib
        import orchestration.api as api_mod

        importlib.reload(api_mod)
        db, cs, ms, reg, team_svc = api_mod._get_services()
        api_mod._db = db
        api_mod._convoy_svc = cs
        api_mod._mailbox_svc = ms
        api_mod._executor_registry = reg
        api_mod._team_svc = team_svc
        yield TestClient(api_mod.app)
        db.close()


def test_team_executor_step_api(client, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    convoy = client.post(
        "/api/convoy",
        json={
            "title": "API executor step",
            "created_by": "dashboard",
            "repo_path": str(repo),
            "subtasks": [{"title": "Git status probe"}],
        },
    ).json()
    convoy_id = convoy["convoy"]["id"]
    subtask_id = convoy["subtasks"][0]["id"]
    team = client.post(
        "/api/team",
        json={
            "team_name": "Builder Team",
            "lead_agent_id": "lead",
            "convoy_id": convoy_id,
        },
    ).json()
    team_id = team["session"]["id"]
    client.post(
        f"/api/team/{team_id}/members",
        json={
            "agent_id": "frontend-worker",
            "agent_name": "Frontend Worker",
            "role": "worker",
            "subtask_id": subtask_id,
        },
    )
    client.post(
        f"/api/convoy/{convoy_id}/subtask/{subtask_id}/dispatch",
        json={"allow_live_agent_run": True},
    )
    client.post(
        f"/api/convoy/{convoy_id}/subtask/{subtask_id}/transition",
        json={"status": "running"},
    )

    response = client.post(
        f"/api/team/{team_id}/executor-step",
        json={
            "agent_id": "frontend-worker",
            "command_key": "git_status",
            "allow_live_agent_run": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["command_key"] == "git_status"
    assert body["success"] is True
    assert body["exit_code"] == 0
    assert body["message"]["msg_type"] == "work_handoff"
