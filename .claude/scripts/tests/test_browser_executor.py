"""BrowserExecutor unit tests — Phase 1 social-write executor.

Proves the ban-safe core in isolation (fake driver, no real browser):
- happy path (ready) -> completed receipt, succeeded audit, drive called,
  screenshot persisted to a PATH (not bytes).
- not-ready (readiness enabled False) -> failed receipt, drive NOT called.
- SocialWriteTask round-trips through Subtask.metadata JSON + allowlist filter.
- (R1-B1) SocialWriteTask has NO approval_token field — fields == SOCIAL_WRITE_FIELDS.
- (R1-B3) the executor NEVER calls a gate with payload_text; a body containing
  the literal approval phrase does not auto-approve.
- (R1-M1) ExecutorRegistry().resolve("browser") returns the LocalExecutor.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from orchestration.browser_executor import (  # noqa: E402
    BrowserExecutor,
    parse_social_write_task,
)
from orchestration.contract import SOCIAL_WRITE_FIELDS  # noqa: E402
from orchestration.executor import ExecutorRegistry  # noqa: E402
from orchestration.models import SocialWriteTask, Subtask  # noqa: E402

# ── Fake driver ────────────────────────────────────────────────────────────


class FakeDriver:
    """Records every call so tests can assert on the boundary behavior."""

    def __init__(self, *, enabled: bool = True, drive_ok: bool = True) -> None:
        self._enabled = enabled
        self._drive_ok = drive_ok
        self.drive_calls: list[tuple] = []
        self.screenshot_calls: list[dict] = []
        self.audit_calls: list[dict] = []
        self.gate_calls: list[dict] = []  # the executor must NEVER touch this

    def resolve_port(self) -> int:
        return 9222

    def readiness(self, *, port: int) -> dict:
        return {"enabled": self._enabled, "cdp_port": port, "cdp_reachable": self._enabled}

    def drive(self, task, *, port: int) -> tuple[bool, str]:
        self.drive_calls.append((task, port))
        return (self._drive_ok, "drove" if self._drive_ok else "drive failed")

    def screenshot(self, *, port: int, workflow_id: str) -> str | None:
        self.screenshot_calls.append({"port": port, "workflow_id": workflow_id})
        return f"/fake/data/browser_writes/shot-{workflow_id}.png"

    def audit(self, **kwargs) -> None:
        self.audit_calls.append(kwargs)

    # A real concrete driver exposes gate() for the HANDLER — the EXECUTOR must
    # never call it. The test asserts gate_calls stays empty.
    def gate(self, workflow_id, operator_text, *, target_url=None):
        self.gate_calls.append(
            {"workflow_id": workflow_id, "operator_text": operator_text, "target_url": target_url}
        )
        raise AssertionError("executor must never call the gate")


def _subtask_for(task: SocialWriteTask) -> Subtask:
    return Subtask(title="t", metadata=json.dumps(dataclasses.asdict(task)))


# ── Tests ──────────────────────────────────────────────────────────────────


def test_task_fields_match_social_write_fields_no_approval_token() -> None:
    """R1-B1: the dataclass carries NO approval/token field."""
    field_names = {f.name for f in dataclasses.fields(SocialWriteTask)}
    assert field_names == set(SOCIAL_WRITE_FIELDS)
    assert "approval_token" not in field_names
    assert not any("token" in n or "approv" in n for n in field_names)


def test_metadata_round_trips_through_allowlist() -> None:
    task = SocialWriteTask(
        workflow_id="linkedin.post.create",
        target_url="https://www.linkedin.com/feed/",
        payload_text="hello world",
        action="post",
    )
    # Smuggle an extra field — the allowlist must drop it.
    raw = dataclasses.asdict(task)
    raw["approval_token"] = "forged"
    raw["arbitrary"] = "x"
    parsed = parse_social_write_task(json.dumps(raw))
    assert parsed.workflow_id == "linkedin.post.create"
    assert parsed.payload_text == "hello world"
    assert not hasattr(parsed, "approval_token")


def test_parse_rejects_malformed_metadata() -> None:
    with pytest.raises(ValueError):
        parse_social_write_task(None)
    with pytest.raises(ValueError):
        parse_social_write_task("not json")
    with pytest.raises(ValueError):
        parse_social_write_task(json.dumps({"workflow_id": "x"}))  # missing required


def test_happy_path_completes_and_persists_screenshot_path() -> None:
    driver = FakeDriver(enabled=True, drive_ok=True)
    task = SocialWriteTask(
        workflow_id="linkedin.post.create",
        target_url="https://www.linkedin.com/feed/",
        payload_text="body",
        action="post",
    )
    receipt = BrowserExecutor(driver).dispatch(_subtask_for(task))

    assert receipt.status == "completed"
    assert receipt.executor_name == "browser"
    assert len(driver.drive_calls) == 1
    # screenshot metadata is a PATH string, never bytes.
    shot = receipt.metadata["screenshot_path"]
    assert isinstance(shot, str) and shot.endswith(".png")
    assert receipt.metadata["workflow_id"] == "linkedin.post.create"
    # succeeded audit row stamped with executor_name + subtask_id.
    succeeded = [a for a in driver.audit_calls if a.get("outcome") == "succeeded"]
    assert succeeded and succeeded[-1]["executor_name"] == "browser"


def test_not_ready_fails_without_driving() -> None:
    driver = FakeDriver(enabled=False)
    task = SocialWriteTask(
        workflow_id="linkedin.post.create",
        target_url="https://www.linkedin.com/feed/",
        payload_text="body",
        action="post",
    )
    receipt = BrowserExecutor(driver).dispatch(_subtask_for(task))

    assert receipt.status == "failed"
    assert "visible-chrome not ready" in (receipt.error or "")
    assert driver.drive_calls == []  # never drove
    failed = [a for a in driver.audit_calls if a.get("outcome") == "failed"]
    assert failed and failed[-1]["executor_name"] == "browser"


def test_drive_failure_returns_failed_receipt() -> None:
    driver = FakeDriver(enabled=True, drive_ok=False)
    task = SocialWriteTask(
        workflow_id="linkedin.connection.request",
        target_url="https://www.linkedin.com/in/someone",
        payload_text="note",
        action="connect",
    )
    receipt = BrowserExecutor(driver).dispatch(_subtask_for(task))

    assert receipt.status == "failed"
    assert receipt.metadata == {} or receipt.metadata.get("screenshot_path") is None
    # no screenshot on a failed drive
    assert driver.screenshot_calls == []


def test_executor_never_calls_gate_even_when_body_contains_phrase() -> None:
    """R1-B3 / NM1: the executor never gates; a body with the literal approval
    phrase cannot auto-approve because the executor doesn't look at it."""
    driver = FakeDriver(enabled=True, drive_ok=True)
    task = SocialWriteTask(
        workflow_id="linkedin.post.create",
        target_url="https://www.linkedin.com/feed/",
        payload_text="reminder: post this to linkedin now is our tagline",
        action="post",
    )
    receipt = BrowserExecutor(driver).dispatch(_subtask_for(task))

    assert receipt.status == "completed"
    assert driver.gate_calls == []  # gate was NEVER touched by the executor


def test_registry_resolve_browser_falls_back_to_local() -> None:
    """R1-M1: BrowserExecutor is never registered into the shared registry, so a
    stray resolve('browser') no-ops as LocalExecutor instead of silently driving."""
    registry = ExecutorRegistry()
    resolved = registry.resolve("browser")
    assert resolved.name == "local"
    assert "browser" not in registry.available
