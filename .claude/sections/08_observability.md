### Langfuse Full-Depth Observability

Every Telegram/CLI/relay message produces a single nested trace in Langfuse showing the complete journey from router entry to response delivery.

**PRD:** `PRPs/PRP-langfuse-full-depth-observability.md`
**Server:** Self-hosted Langfuse v3.162.0 at `http://localhost:3000` (6 Docker containers in `~/langfuse/`)
**Project:** `thehomie`

### Trace Shape (E2E Verified)

```
trace: chat_message (ROOT — 1 trace per message, all spans nested)
  session_id: "telegram:12345:67890"
  user_id: "owner"
  tags: ["telegram", "thehomie"]
  │
  ├── span: session_lookup
  │     output: {found, mode, message_count}
  │
  ├── span: process_detection (if cognition available)
  │     output: {process}
  │
  ├── span: recall (@observe on recall_service.recall)
  │     metadata: {recall_tier, results_count, top_scores, latency_ms, search_mode, caller}
  │     ├── span: classify_tier (@observe)
  │     └── span: recall_pipeline (@observe)
  │
  ├── span: region_assembly
  │     output: {total_chars}
  │
  ├── span: run_with_fallback (@observe, existing)
  │     metadata: {provider, model, cost_usd, tool_call_count, tool_names}
  │     └── generation: invoke_agent (auto-instrumented by ClaudeAgentSdkInstrumentor)
  │
  └── span: post_response (capture + continuity + session persist)
        output: {session_action}
```

### Architecture

Three layers create the full trace:

1. **Root span** — `start_as_current_observation(name="chat_message")` in `handle_message()` creates the single parent trace. All children auto-nest via OTEL context propagation.
2. **Framework layer** — `@observe` decorators on `recall()`, `classify_tier()`, `run_recall_pipeline()` + `start_as_current_observation` context managers for session_lookup, process_detection, region_assembly, post_response.
3. **Runtime layer** — `ClaudeAgentSdkInstrumentor().instrument()` (community OTEL instrumentor) auto-wraps Claude Agent SDK `query()` calls. Produces `invoke_agent` generations with model, tokens, and cost.

`propagate_attributes` wraps the root span and sets `session_id`, `user_id`, and `tags` on ALL child spans.

### Nesting Pattern (Critical)

```python
# handle_message() in engine.py:
with propagate_attributes(session_id=..., user_id=..., tags=...):
    with _lf.start_as_current_observation(name="chat_message") as root:  # ROOT TRACE
        # All child spans auto-nest under root via OTEL context:
        with _lf.start_as_current_observation(name="session_lookup"):  # child
            ...
        await recall(...)  # @observe → child
        with _lf.start_as_current_observation(name="region_assembly"):  # child
            ...
```

**Key insight**: `propagate_attributes` only sets attributes — it does NOT create a trace. The `start_as_current_observation` root span is what creates the trace. Without the root span, each child becomes a separate trace (confirmed via Langfuse GitHub PRs #1183, #1233, #1385).

### Key Files

| File | What It Does |
|------|-------------|
| `.claude/scripts/runtime/langfuse_setup.py` | `init_langfuse()`, `is_langfuse_enabled()`, `flush_langfuse()`, SDK auto-instrumentation |
| `.claude/scripts/runtime/registry.py` | `run_with_fallback` — existing `@observe` + span metadata (provider, cost, tools) |
| `.claude/chat/engine.py` | `handle_message` → `_handle_message_inner` split; root `chat_message` span + `propagate_attributes` at outer scope; child spans for session, process, regions, post-response |
| `.claude/chat/recall_service.py` | `@observe` on `recall()` + `_update_span()` bridging RecallLog to Langfuse metadata |
| `.claude/chat/cognition/recall.py` | `@observe` on `classify_tier()` and `run_recall_pipeline()` |
| `.claude/scripts/orchestration/observability.py` | `orchestration_span` context manager, `init_orchestration_observability`, `update_observation` — dual-lane (Langfuse + Sentry) tracing for team/convoy operations |
| `.claude/scripts/tests/test_team_observability.py` | Real-path integration + Sentry dual-lane tests for orchestration observability |
| `.claude/scripts/tests/test_team_observability_matrix.py` | Helper contract validation — all branches of `orchestration_span` (enabled/disabled × happy/error) |
| `.claude/scripts/tests/test_langfuse.py` | 12 tests — setup, flush, observe decoration, classify_tier behavior |

### Env Vars (in `.claude/scripts/.env`)

```
LANGFUSE_PUBLIC_KEY=pk-lf-your-public-key
LANGFUSE_SECRET_KEY=sk-lf-your-secret-key
LANGFUSE_BASE_URL=http://localhost:3000
LANGFUSE_ENABLED=true   # set to "false" to disable all tracing
```

### Dependencies

- `langfuse>=4.0.1` — Python SDK (decorators, context managers, client)
- `langsmith[claude-agent-sdk,otel]>=0.7.22` — OTEL bridge utilities
- `otel-instrumentation-claude-agent-sdk` — Community instrumentor (GitHub-only, not PyPI)

### Design Rules

- **Never let tracing break runtime** — every Langfuse call wrapped in try/except
- **Lazy imports only** — `is_langfuse_enabled()` guard before any langfuse import in chat/ files
- **Root span required for nesting** — `propagate_attributes` alone creates flat traces; the root `start_as_current_observation` is what creates the parent trace
- **`@observe` for regular functions only** — `handle_message` is an async generator, use context managers instead
- **`metadata` not `output` for @observe spans** — `@observe` auto-captures return values as `output`; use `metadata` for custom fields to avoid overwrite
- **Bridge, don't replace** — RecallLog JSON ring buffers still exist alongside Langfuse spans
- **`flush_langfuse()` on shutdown** — called in signal handler, keyboard interrupt, and crash handler in `main.py`
- **`config.py` uses `load_dotenv(override=True)`** — shell env vars get overridden by `.env` at import time; tests must mock `os.getenv` at module level
