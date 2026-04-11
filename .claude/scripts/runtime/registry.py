"""Runtime selection and fallback execution."""

from __future__ import annotations

from .base import RuntimeRequest, RuntimeResult
from .langfuse_setup import is_langfuse_enabled
from .lane_router import run_with_runtime_lanes


async def run_with_fallback(request: RuntimeRequest) -> RuntimeResult:
    """Deprecated compatibility shim for the old provider-first runtime facade."""

    if not is_langfuse_enabled():
        return await run_with_runtime_lanes(request)

    from langfuse import observe
    from langfuse import get_client

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
