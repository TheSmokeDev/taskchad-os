"""Framework-wide opt-in contract for live agent/factory actions."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

LIVE_AGENT_ENV_VAR = "HOMIE_ALLOW_LIVE_AGENT_RUN"
LIVE_AGENT_FLAG = "--allow-live-agent-run"

_TRUTHY = {"1", "true", "yes", "on", "allow", "allowed", "live"}


class LiveExecutionRefused(ValueError):
    """Raised when a live agent/factory action lacks explicit opt-in."""


@dataclass(frozen=True)
class LiveExecutionStatus:
    """Operator-facing live execution state.

    Dry-run/read-only is the default. This contract sits above lower-level
    gates; BrowserOps, direct integrations, and Cabinet still enforce their
    own action policies after this guard passes.
    """

    mode: str
    live_agent_run_allowed: bool
    env_var: str = LIVE_AGENT_ENV_VAR
    cli_flag: str = LIVE_AGENT_FLAG
    opt_in_sources: list[str] = field(default_factory=list)
    default_contract: str = "dry-run/read-only"
    refusal_message: str = ""
    lower_level_gates: list[str] = field(
        default_factory=lambda: [
            "browserops_workflow_policy",
            "direct_integration_capability_policy",
            "cabinet_tool_policy",
        ]
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "live_agent_run_allowed": self.live_agent_run_allowed,
            "env_var": self.env_var,
            "cli_flag": self.cli_flag,
            "opt_in_sources": list(self.opt_in_sources),
            "default_contract": self.default_contract,
            "refusal_message": self.refusal_message,
            "lower_level_gates": list(self.lower_level_gates),
        }


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def live_execution_status(
    *,
    explicit_opt_in: bool = False,
    env: Mapping[str, str] | None = None,
) -> LiveExecutionStatus:
    """Return the current live-execution contract state."""
    environ = env if env is not None else os.environ
    sources: list[str] = []
    if explicit_opt_in:
        sources.append("explicit_flag")
    if _truthy(environ.get(LIVE_AGENT_ENV_VAR)):
        sources.append(LIVE_AGENT_ENV_VAR)

    allowed = bool(sources)
    refusal = "" if allowed else _refusal_message("live agent/factory action")
    return LiveExecutionStatus(
        mode="live" if allowed else "dry_run",
        live_agent_run_allowed=allowed,
        opt_in_sources=sources,
        refusal_message=refusal,
    )


def require_live_agent_run(
    action: str,
    *,
    explicit_opt_in: bool = False,
    env: Mapping[str, str] | None = None,
) -> LiveExecutionStatus:
    """Require explicit opt-in before a live agent/factory action."""
    status = live_execution_status(explicit_opt_in=explicit_opt_in, env=env)
    if status.live_agent_run_allowed:
        return status
    raise LiveExecutionRefused(_refusal_message(action))


def _refusal_message(action: str) -> str:
    target = (action or "live agent/factory action").strip()
    return (
        f"Live agent/factory action refused: {target} requires explicit opt-in. "
        f"Set {LIVE_AGENT_ENV_VAR}=1 or pass {LIVE_AGENT_FLAG}. "
        "Default mode is dry-run/read-only; lower-level BrowserOps, direct "
        "integration, and Cabinet gates still apply after opt-in."
    )
