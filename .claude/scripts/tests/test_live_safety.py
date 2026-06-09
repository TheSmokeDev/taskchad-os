"""Live agent/factory opt-in safety contract tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from orchestration.live_safety import (  # noqa: E402
    LIVE_AGENT_ENV_VAR,
    LiveExecutionRefused,
    live_execution_status,
    require_live_agent_run,
)


def test_live_execution_defaults_to_dry_run_read_only() -> None:
    status = live_execution_status(env={})

    assert status.mode == "dry_run"
    assert status.live_agent_run_allowed is False
    assert status.default_contract == "dry-run/read-only"
    assert status.opt_in_sources == []
    assert "BrowserOps" in status.refusal_message
    assert "browserops_workflow_policy" in status.lower_level_gates
    assert "direct_integration_capability_policy" in status.lower_level_gates
    assert "cabinet_tool_policy" in status.lower_level_gates


def test_require_live_agent_run_refuses_without_opt_in() -> None:
    with pytest.raises(LiveExecutionRefused) as exc:
        require_live_agent_run("factory dispatch", env={})

    message = str(exc.value)
    assert "Live agent/factory action refused" in message
    assert "factory dispatch" in message
    assert f"{LIVE_AGENT_ENV_VAR}=1" in message


def test_require_live_agent_run_allows_explicit_flag() -> None:
    status = require_live_agent_run(
        "factory dispatch",
        explicit_opt_in=True,
        env={},
    )

    assert status.mode == "live"
    assert status.live_agent_run_allowed is True
    assert status.opt_in_sources == ["explicit_flag"]


def test_require_live_agent_run_allows_env_opt_in() -> None:
    status = require_live_agent_run(
        "factory dispatch",
        env={LIVE_AGENT_ENV_VAR: "1"},
    )

    assert status.mode == "live"
    assert status.live_agent_run_allowed is True
    assert status.opt_in_sources == [LIVE_AGENT_ENV_VAR]
