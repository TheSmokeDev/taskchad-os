"""Cabinet text-mode slice (PRD-8 Phase 5a).

Port of ClaudeClaw `src/warroom-text-*` modules + Hermes title generator.

Same-process invariant: producer (`text_orchestrator.handle_text_turn`)
AND subscriber (`/api/cabinet/stream` SSE handler in `dashboard_api.py`)
BOTH live in the orchestration API process (port 4322). The module-local
`_CHANNELS` registry in `meeting_channel.py` bridges them.

Phase 5b's Telegram chat process accesses cabinet via HTTP only — never
imports cabinet/* directly. The chat-side import shim
`.claude/chat/cabinet_text.py` re-exports the canonical names.

B1 lock — cabinet code MUST NOT invoke any concrete provider client. Every
per-persona turn dispatches via `runtime.lane_router.run_with_runtime_lanes`
with a `RuntimeRequest` carrying `disallowed_tools`/`mcp_servers`/`metadata`/
`auth_profile` (per WS1.0).
"""

from . import meeting_channel, text_orchestrator, text_router, title, tool_policy

__all__ = [
    "meeting_channel",
    "text_orchestrator",
    "text_router",
    "title",
    "tool_policy",
]
