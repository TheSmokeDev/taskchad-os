"""Fail-closed, lane-first model-only execution for curriculum cognition."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from runtime.base import RuntimeRequest, RuntimeResult
from runtime.lane_router import run_with_runtime_lanes


def secure_curriculum_request(request: RuntimeRequest) -> RuntimeRequest:
    """Return the exact zero-tool request the scheduled runtime will execute."""

    secured = replace(
        request,
        allowed_tools=[],
        disallowed_tools=["*"],
        mcp_servers=[],
        tool_defs=None,
        tool_dispatch=None,
        read_only_tools=False,
        workspace_write_tools=False,
        model_only=True,
    )
    return replace(secured, hooks=None, setting_sources=[])


def get_scheduled_runtime_contracts() -> dict[
    str,
    Callable[[RuntimeRequest], RuntimeRequest],
]:
    """Return the registered scheduled authorities and their request guards.

    The mapping is rebuilt at call time so tests and runtime extensions that
    replace a module attribute are observed immediately.
    """

    return {"curriculum_study": secure_curriculum_request}


async def run_curriculum_model(request: RuntimeRequest) -> RuntimeResult:
    """Use canonical lane selection with all tool and setting authority removed."""
    secured = secure_curriculum_request(request)
    return await run_with_runtime_lanes(secured)
