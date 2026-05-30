"""Bounded team executor actions.

This module gives team sessions a small, auditable execution surface. It runs
named command presets only, validates the working directory against approved
roots, captures bounded output, and reports the result through the convoy
mailbox. It does not give team runtime turns tool access.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from orchestration.contract import DEFAULT_WORKSPACE_ID
from orchestration.convoy_service import ConvoyService
from orchestration.db import OrchestrationDB
from orchestration.mailbox_service import MailboxService
from orchestration.models import AgentMessage, ProgressReport, SendMessageInput, Subtask
from orchestration.observability import orchestration_span, update_observation
from orchestration.team_service import TeamService


MAX_OUTPUT_CHARS = 6000


@dataclass(frozen=True)
class ExecutorCommandSpec:
    key: str
    argv: tuple[str, ...]
    description: str
    timeout_seconds: int = 120


DEFAULT_COMMANDS: dict[str, ExecutorCommandSpec] = {
    "git_status": ExecutorCommandSpec(
        key="git_status",
        argv=("git", "status", "--short"),
        description="Show concise git worktree status.",
        timeout_seconds=30,
    ),
    "git_diff_stat": ExecutorCommandSpec(
        key="git_diff_stat",
        argv=("git", "diff", "--stat"),
        description="Show a concise git diff summary.",
        timeout_seconds=30,
    ),
    "npm_build": ExecutorCommandSpec(
        key="npm_build",
        argv=("npm", "run", "build"),
        description="Run npm build.",
        timeout_seconds=180,
    ),
    "npm_lint": ExecutorCommandSpec(
        key="npm_lint",
        argv=("npm", "run", "lint"),
        description="Run npm lint.",
        timeout_seconds=180,
    ),
    "npm_test": ExecutorCommandSpec(
        key="npm_test",
        argv=("npm", "test"),
        description="Run npm test.",
        timeout_seconds=180,
    ),
    "pnpm_build": ExecutorCommandSpec(
        key="pnpm_build",
        argv=("pnpm", "run", "build"),
        description="Run pnpm build.",
        timeout_seconds=180,
    ),
    "pnpm_lint": ExecutorCommandSpec(
        key="pnpm_lint",
        argv=("pnpm", "run", "lint"),
        description="Run pnpm lint.",
        timeout_seconds=180,
    ),
    "pnpm_test": ExecutorCommandSpec(
        key="pnpm_test",
        argv=("pnpm", "test"),
        description="Run pnpm test.",
        timeout_seconds=180,
    ),
    "uv_pytest": ExecutorCommandSpec(
        key="uv_pytest",
        argv=("uv", "run", "pytest", "-q"),
        description="Run pytest through uv.",
        timeout_seconds=240,
    ),
}


@dataclass
class TeamExecutorStepResult:
    team_id: int
    agent_id: str
    convoy_id: int
    subtask_id: int
    command_key: str
    argv: list[str]
    cwd: str
    success: bool
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    stdout: str = ""
    stderr: str = ""
    message: AgentMessage | None = None
    completed: bool = False
    convoy_completed: bool = False
    newly_ready: list[Subtask] = field(default_factory=list)


class TeamExecutorService:
    """Run one approved command for a bound team member subtask."""

    def __init__(
        self,
        db: OrchestrationDB,
        *,
        allowed_roots: Sequence[Path | str] | None = None,
        commands: dict[str, ExecutorCommandSpec] | None = None,
    ):
        self.db = db
        self.team_svc = TeamService(db)
        self.mailbox_svc = MailboxService(db)
        self.convoy_svc = ConvoyService(db)
        self._static_allowed_roots = [Path(p) for p in allowed_roots] if allowed_roots else None
        self._commands = commands or DEFAULT_COMMANDS

    def run_executor_step(
        self,
        team_id: int,
        *,
        agent_id: str,
        subtask_id: int | None = None,
        command_key: str = "git_status",
        cwd: str | None = None,
        timeout_seconds: int | None = None,
        complete_on_success: bool = False,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
    ) -> TeamExecutorStepResult:
        """Run one bounded executor command and report the result to mailbox."""
        with orchestration_span(
            "team_executor.run_executor_step",
            metadata={
                "team_id": team_id,
                "agent_id": agent_id,
                "subtask_id": subtask_id,
                "command_key": command_key,
                "complete_on_success": complete_on_success,
            },
            trace_metadata={"feature_phase": 10, "team_id": team_id},
            expected_exceptions=(ValueError,),
        ):
            team = self.team_svc.get_team_session(team_id, workspace_id=workspace_id)
            if team is None:
                raise ValueError(f"Team {team_id} not found")
            if team.session.convoy_id is None:
                raise ValueError(f"Team {team_id} has no convoy_id set")

            member = next((m for m in team.members if m.agent_id == agent_id), None)
            if member is None:
                raise ValueError(f"Agent '{agent_id}' is not a member of team {team_id}")
            if member.status != "active":
                raise ValueError(f"Agent '{agent_id}' is not active in team {team_id}")

            effective_subtask_id = subtask_id or member.subtask_id
            if effective_subtask_id is None:
                raise ValueError(f"Agent '{agent_id}' has no bound subtask")

            subtask = self.convoy_svc.get_subtask(effective_subtask_id, workspace_id=workspace_id)
            if subtask is None:
                raise ValueError(f"Subtask {effective_subtask_id} not found")
            if subtask.convoy_id != team.session.convoy_id:
                raise ValueError(
                    f"Subtask {effective_subtask_id} belongs to convoy {subtask.convoy_id}, "
                    f"not team {team_id}'s convoy {team.session.convoy_id}"
                )
            if subtask.status not in ("dispatched", "running", "stalled"):
                raise ValueError(
                    f"Subtask {effective_subtask_id} must be dispatched/running before executor step "
                    f"(status: {subtask.status})"
                )
            if subtask.status == "dispatched":
                subtask = self.convoy_svc.transition_subtask(
                    effective_subtask_id,
                    "running",
                    workspace_id=workspace_id,
                )

            convoy = self.convoy_svc.get_convoy(team.session.convoy_id, workspace_id=workspace_id)
            if convoy is None:
                raise ValueError(f"Convoy {team.session.convoy_id} not found")

            spec = self._command_spec(command_key)
            run_cwd = self._resolve_cwd(
                requested_cwd=cwd,
                convoy_repo_path=convoy.convoy.repo_path,
                subtask_worktree_path=subtask.worktree_path,
            )
            timeout = self._resolve_timeout(timeout_seconds, spec.timeout_seconds)
            raw = self._run_command(spec.argv, run_cwd, timeout)

            self.convoy_svc.report_progress(
                effective_subtask_id,
                ProgressReport(
                    subtask_id=effective_subtask_id,
                    convoy_id=team.session.convoy_id,
                    executor_name="team_executor",
                    progress_pct=1.0 if raw["success"] else 0.0,
                    message=f"{command_key} exit {raw['exit_code']}",
                    timestamp=int(time.time()),
                ),
                workspace_id=workspace_id,
            )

            completed = False
            convoy_completed = False
            newly_ready: list[Subtask] = []
            if complete_on_success and raw["success"]:
                newly_ready, convoy_completed = self.convoy_svc.handle_subtask_completion(
                    effective_subtask_id,
                    workspace_id=workspace_id,
                )
                completed = True

            result = TeamExecutorStepResult(
                team_id=team_id,
                agent_id=agent_id,
                convoy_id=team.session.convoy_id,
                subtask_id=effective_subtask_id,
                command_key=command_key,
                argv=list(spec.argv),
                cwd=str(run_cwd),
                success=raw["success"],
                exit_code=raw["exit_code"],
                timed_out=raw["timed_out"],
                duration_ms=raw["duration_ms"],
                stdout=raw["stdout"],
                stderr=raw["stderr"],
                completed=completed,
                convoy_completed=convoy_completed,
                newly_ready=newly_ready,
            )
            result.message = self._send_result_message(
                result,
                lead_agent_id=team.session.lead_agent_id,
                workspace_id=workspace_id,
            )
            self.team_svc.ping_activity(team_id, agent_id=agent_id, workspace_id=workspace_id)
            update_observation(
                metadata={
                    "team_id": team_id,
                    "agent_id": agent_id,
                    "convoy_id": team.session.convoy_id,
                    "subtask_id": effective_subtask_id,
                    "command_key": command_key,
                    "exit_code": result.exit_code,
                    "success": result.success,
                },
                output={"duration_ms": result.duration_ms},
            )
            return result

    def _command_spec(self, command_key: str) -> ExecutorCommandSpec:
        spec = self._commands.get(command_key)
        if spec is None:
            allowed = ", ".join(sorted(self._commands))
            raise ValueError(f"Unknown executor command '{command_key}' (allowed: {allowed})")
        return spec

    def _resolve_cwd(
        self,
        *,
        requested_cwd: str | None,
        convoy_repo_path: str | None,
        subtask_worktree_path: str | None,
    ) -> Path:
        candidate = requested_cwd or subtask_worktree_path or convoy_repo_path or str(_repo_root())
        path = Path(candidate).expanduser()
        if not path.exists() or not path.is_dir():
            raise ValueError(f"Executor cwd does not exist or is not a directory: {candidate}")
        resolved = path.resolve()
        allowed_roots = self._allowed_roots(convoy_repo_path, subtask_worktree_path)
        if not any(_is_relative_to(resolved, root) for root in allowed_roots):
            allowed_display = ", ".join(str(root) for root in allowed_roots)
            raise ValueError(f"Executor cwd is outside approved roots: {resolved} (allowed: {allowed_display})")
        return resolved

    def _allowed_roots(
        self,
        convoy_repo_path: str | None,
        subtask_worktree_path: str | None,
    ) -> list[Path]:
        roots: list[Path] = [_repo_root()]
        if self._static_allowed_roots is not None:
            roots.extend(self._static_allowed_roots)
        else:
            env_roots = os.getenv("TEAM_EXECUTOR_ALLOWED_ROOTS", "").strip()
            if env_roots:
                roots.extend(Path(p) for p in env_roots.split(os.pathsep) if p.strip())
        if convoy_repo_path:
            roots.append(Path(convoy_repo_path))
        if subtask_worktree_path:
            roots.append(Path(subtask_worktree_path))
        return _dedupe_resolved_roots(roots)

    @staticmethod
    def _resolve_timeout(requested: int | None, default: int) -> int:
        timeout = requested if requested is not None else default
        if timeout < 1:
            raise ValueError("timeout_seconds must be >= 1")
        return min(timeout, 600)

    @staticmethod
    def _run_command(argv: tuple[str, ...], cwd: Path, timeout: int) -> dict:
        started = time.monotonic()
        try:
            completed = subprocess.run(
                list(argv),
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            return {
                "success": completed.returncode == 0,
                "exit_code": completed.returncode,
                "timed_out": False,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "stdout": _truncate(completed.stdout),
                "stderr": _truncate(completed.stderr),
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "success": False,
                "exit_code": None,
                "timed_out": True,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "stdout": _truncate(_to_text(exc.stdout)),
                "stderr": _truncate(_to_text(exc.stderr) or f"Timed out after {timeout}s"),
            }

    def _send_result_message(
        self,
        result: TeamExecutorStepResult,
        *,
        lead_agent_id: str,
        workspace_id: int,
    ) -> AgentMessage:
        status = "passed" if result.success else "failed"
        body = (
            f"Executor step {status} for subtask #{result.subtask_id}.\n\n"
            f"Command: {result.command_key}\n"
            f"Cwd: {result.cwd}\n"
            f"Exit: {result.exit_code if result.exit_code is not None else 'timeout'}\n"
            f"Duration: {result.duration_ms}ms\n\n"
            f"STDOUT:\n{result.stdout or '(empty)'}\n\n"
            f"STDERR:\n{result.stderr or '(empty)'}"
        )
        return self.mailbox_svc.send_message(
            SendMessageInput(
                from_agent=result.agent_id,
                recipients=[lead_agent_id] if lead_agent_id != result.agent_id else [result.agent_id],
                convoy_id=result.convoy_id,
                subject=f"Executor step: {result.command_key}",
                body=body,
                msg_type="work_handoff",
                message_type="handoff",
                artifact_refs={
                    "command_key": result.command_key,
                    "argv": result.argv,
                    "cwd": result.cwd,
                    "exit_code": result.exit_code,
                    "timed_out": result.timed_out,
                    "duration_ms": result.duration_ms,
                    "success": result.success,
                },
            ),
            workspace_id=workspace_id,
        )


def executor_result_to_dict(result: TeamExecutorStepResult) -> dict:
    return {
        "team_id": result.team_id,
        "agent_id": result.agent_id,
        "convoy_id": result.convoy_id,
        "subtask_id": result.subtask_id,
        "command_key": result.command_key,
        "argv": result.argv,
        "cwd": result.cwd,
        "success": result.success,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "duration_ms": result.duration_ms,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "message": dataclasses.asdict(result.message) if result.message else None,
        "completed": result.completed,
        "convoy_completed": result.convoy_completed,
        "newly_ready": [dataclasses.asdict(subtask) for subtask in result.newly_ready],
    }


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3].resolve()


def _dedupe_resolved_roots(roots: list[Path]) -> list[Path]:
    resolved: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            candidate = root.expanduser().resolve()
        except OSError:
            continue
        key = os.path.normcase(str(candidate))
        if key not in seen and candidate.exists() and candidate.is_dir():
            resolved.append(candidate)
            seen.add(key)
    return resolved


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _truncate(value: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...[truncated]"


def _to_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value
