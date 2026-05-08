"""Runtime selection and fallback execution.

PRD-8 Phase 7a WS4 — `requireEnabled("llm")` is invoked at the head of
`run_with_fallback` so even legacy callers that bypass the lane router
hit the kill-switch. Module-attribute lookup (Rule 3).
"""

from __future__ import annotations

from security import kill_switches

from . import langfuse_setup
from .base import RuntimeRequest, RuntimeResult
from .lane_router import run_with_runtime_lanes


async def run_with_fallback(request: RuntimeRequest) -> RuntimeResult:
    """Deprecated compatibility shim for the old provider-first runtime facade."""

    # PRD-8 Phase 7a WS4 — operator kill-switch. Mirrors lane_router so legacy
    # callers that import this shim directly are also gated.
    kill_switches.requireEnabled("llm", caller="registry_shim")

    # Module-attribute lookup so monkey-patches on
    # `runtime.langfuse_setup.is_langfuse_enabled` (e.g. in `isolate_langfuse()`)
    # actually reach this call site. A top-level
    # `from .langfuse_setup import is_langfuse_enabled` would cache the original.
    if not langfuse_setup.is_langfuse_enabled():
        return await run_with_runtime_lanes(request)

    from langfuse import get_client, observe

    # Keep the legacy span name while downstream traces still depend on it.
    traced = observe(name="run_with_fallback", as_type="span")(run_with_runtime_lanes)
    result = await traced(request)
    try:
        get_client().update_current_span(
            metadata={
                "runtime_lane": result.runtime_lane,
                "provider": result.provider,
                "model": result.model or "",
                "profile_key": result.profile_key,
                "cost_usd": result.cost_usd,
                "tool_call_count": result.tool_call_count,
                "tool_names": result.tool_names_used,
                "task_name": request.task_name,
                "capability": request.capability,
            },
            usage={
                "total_cost": result.cost_usd or 0.0,
            },
            model=result.model or result.provider,
        )
    except Exception:
        pass
    return result
