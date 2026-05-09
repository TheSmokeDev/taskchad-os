# MC Profile Contract — Dashboard HTTP API Reference

**Status:** Active (PRD-8 Phase 3, shipped)
**Surface:** Framework HTTP API on port `4322`, mounted onto the orchestration FastAPI app
**Slice owner:** `dashboard-owner` (`.claude/agents/dashboard-owner.md`)
**JSON contract:** `PRPs/contracts/prd-8-phase-3.json`
**Forward compatibility:** Q3 — additive only. Phase 5/6/7 add columns/tables/endpoints; existing endpoint shapes never break.

This document is the public contract between The Homie's framework HTTP API
and any dashboard/proxy/operator tool that consumes it. It supersedes the
deferred PRD-7 Phase 6 `mc-profile-contract.md` placeholder. The 30
endpoints introduced by PRD-8 Phase 3 plus the existing
convoy/team/mailbox endpoints together form the complete framework HTTP
surface that ships through `scripts/sanitize.py` to the public mirror.

---

## 1. Auth Model

The dashboard API inherits the orchestration API auth middleware. There
is **one auth path** — `Bearer <token>` in the `Authorization` header —
applied to all routes except `/api/health`.

### 1.1 Token sources

Two environment variables are recognized. Either both may be set
(equal), or exactly one may be set (it aliases for the other), or
neither may be set (loopback dev-mode opt-in only).

| Variable | Purpose |
|---|---|
| `ORCHESTRATION_API_TOKEN` | Canonical framework HTTP API token. Used by Python framework, callbacks, and CLI clients. |
| `DASHBOARD_TOKEN` | Operator alias used by the Hono dashboard server. When set, treated as equal to `ORCHESTRATION_API_TOKEN`. |

### 1.2 4-Branch Token Policy (R5 NM1)

The Hono server's `auth-policy.ts` enforces all four branches at startup
with no fallthrough to silent insecure mode:

| `DASHBOARD_TOKEN` | `ORCHESTRATION_API_TOKEN` | Bind | Outcome |
|---|---|---|---|
| Set | Set, equal | any | Start normally (mode `token-equal`). |
| Set | Set, not equal | any | Refuse to start, exit 1 with diff message. |
| Set | Unset | any | Start normally — `DASHBOARD_TOKEN` aliases for `ORCHESTRATION_API_TOKEN` (mode `token-alias`). |
| Unset | Set | any | Start normally — `ORCHESTRATION_API_TOKEN` aliases for `DASHBOARD_TOKEN` (mode `token-alias`). |
| Unset | Unset | non-loopback | Refuse to start, exit 1. |
| Unset | Unset | loopback `127.0.0.1`, `DASHBOARD_DEV_MODE_NO_AUTH` unset | Refuse to start, exit 1. |
| Unset | Unset | loopback, `DASHBOARD_DEV_MODE_NO_AUTH=true` | Start with `WARN: dashboard request served without authentication` on every request. Loopback only (mode `dev-mode-loopback`). |

Dev-mode opt-in (`DASHBOARD_DEV_MODE_NO_AUTH=true`) is the **only** path
that produces an unauthenticated dashboard. It requires explicit
operator action and emits a per-request log warning.

The token-alias branch (exactly one set) is provided as an operator
convenience: rotating one variable without touching the other does not
require the operator to also set the alias. Both names refer to the
same logical secret.

### 1.3 Health endpoint exemption

`GET /api/health` is exempt from auth checking. Both token-set and
token-unset modes return 200 — liveness probes do not need credentials.

**Response shape (200 application/json):**

```json
{
  "status": "ok",
  "version": "<server version>",
  "uptime_seconds": <int>,
  "lane_status": { "claude_native": "ready", "generic_runtime": "ready" },
  "killSwitches": {
    "counters": { "<switch_name>": <int_refusal_count> },
    "audit_write_failures": { "<switch_name>": <int_failure_count> },
    "process_started_at": <unix_timestamp_float | null>
  }
}
```

The payload contains NO PII, NO secrets, and NO internal paths. PRD-8
Phase 7a (R2 NM4) populated the `killSwitches` field — formerly an empty
stub `{}` (Phase 3 forward-only-additive contract slot). The rich
snapshot exposes:

- `counters` — per-switch refusal count since process start (resets on
  restart). Rendered by `dashboard/web/src/components/KillSwitchBanner.tsx`
  (PRD-8 Phase 7a WS7 — see §8 below).
- `audit_write_failures` — per-switch audit-write failure count. Surfaces
  silent persistence loss (e.g., dashboard.db disk-full). Operators see
  this as a separate banner segment.
- `process_started_at` — unix timestamp when the FastAPI process began
  serving (so operators understand counters are process-local).

### 1.4 Browser SSE token-via-query exception

The browser `EventSource` API cannot set custom headers. For SSE streams
only (`GET /api/conversation/{persona_id}/stream`), the browser
forwards `?token=<DASHBOARD_TOKEN>` as a query string to the Hono
dashboard server. Hono validates the token, **strips it from access
logs** via `log-scrub.ts` middleware, sets `Referrer-Policy:
no-referrer` on the response, and **never forwards the query string to
the Python framework** — Python always receives a Bearer header from
Hono.

Token-in-query-string is permitted ONLY for browser→Hono SSE. Anywhere
else (non-SSE Hono routes, Python endpoints, browser→Python direct):
forbidden.

---

## 2. URL Conventions

### 2.1 Path prefix

All framework HTTP API endpoints live under `/api/`. The orchestration
endpoints (`/api/convoy/*`, `/api/team/*`, `/api/mailbox/*`,
`/api/executor/callback`) and the dashboard endpoints share this
prefix and are mounted onto the same FastAPI app at port `4322`.

### 2.2 Trailing slashes

No trailing slashes. `GET /api/agents` is canonical; `GET /api/agents/`
is not enforced as equivalent. Clients should not rely on redirect
behavior.

### 2.3 Path parameters

Path parameters use `{name}` notation: `/api/agents/{persona_id}`,
`/api/scheduled/{task_id}`. Persona ids are lowercase, alphanumeric,
underscore or hyphen, length 1–31, matching the regex
`^[a-z][a-z0-9_-]{0,30}$`.

### 2.4 Response envelope shapes

The dashboard API uses three response envelope shapes, each with a
distinct purpose:

- **Success object** — `{"<resource>": {...}}` for single-resource GET
  responses (e.g. `{"agent": {...}}`).
- **Success collection** — `{"<resource>s": [...]}` for list responses
  (e.g. `{"agents": [...]}`).
- **Action result** — `{"deleted": true, "warnings": [...]}` for
  destructive operations and patches that produce status flags.

The `warnings: [...]` field is a stable contract slot for Phase 5/7
audit-augmentation rows; consumers must tolerate empty `[]` and unknown
warning strings.

---

## 3. Endpoint Reference

### 3.1 Phase 3 dashboard endpoints (30 total)

These are the endpoints ported from ClaudeClaw's `dashboard.ts` and
extended for The Homie's persona-bot lifecycle. They are defined in
`.claude/scripts/dashboard_api.py`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Liveness probe (auth-exempt). Returns JSON `{status, version, uptime_seconds, lane_status, killSwitches}` (see §1.3). |
| GET | `/api/info` | Build metadata, lane configuration, framework version. |
| GET | `/api/agents` | List all personas with status, model, last activity. |
| POST | `/api/agents` | Create a new persona profile (body: `persona_id`, `display_name?`, `bot_token_env?`, `model?`). |
| GET | `/api/agents/{persona_id}` | Single persona detail (config, model, schedule, recent activity). |
| DELETE | `/api/agents/{persona_id}` | Soft-delete (move to `_archive/profiles/<persona_id>`). |
| DELETE | `/api/agents/{persona_id}/full` | Hard-delete with disk-state Rule 2 compliance. Requires `?confirm=true`. Optional `?expected_persona_id` cross-check. Returns 200/207/500/503 per outcome enum (`success` / `partial_failure` / `lifecycle_error_no_change` / `internal_error_no_change`). Default profile rejected with 403. |
| PUT | `/api/agents/{persona_id}/avatar` | Upload PNG/JPEG/WEBP avatar. Pillow `.verify()` magic-byte validation, atomic os.replace, cleans up sibling extensions only after replace succeeds. |
| DELETE | `/api/agents/{persona_id}/avatar` | Remove avatar. |
| POST | `/api/agents/{persona_id}/activate` | Start the persona's bot (PID lifecycle through `dashboard_bot_lifecycle.py`). |
| POST | `/api/agents/{persona_id}/deactivate` | Stop the persona's bot. |
| POST | `/api/agents/{persona_id}/restart` | Restart the persona's bot. |
| POST | `/api/agents/validate-id` | Validate a candidate persona id (regex + reserved-name check). |
| POST | `/api/agents/validate-token` | Validate a Telegram bot token by calling `getMe` against the Bot API. |
| GET | `/api/agents/suggestions` | Persona-template suggestions (paginated rotating pool). |
| POST | `/api/agents/suggestions/refresh` | Advance the suggestions cursor (persisted to `dashboard_settings.suggestions_cursor`). |
| GET | `/api/agents/templates` | Available persona templates. |
| GET | `/api/agents/model` | Active default model selection. |
| PATCH | `/api/agents/model` | Update default model. |
| PATCH | `/api/agents/{persona_id}/model` | Update a specific persona's model. |
| GET | `/api/agents/{persona_id}/files` | List the persona's identity files (`config.yaml`, `SOUL.md`, `USER.md`, `MEMORY.md`, `GOALS.md`, `WORKING.md`, `SELF.md`). |
| PATCH | `/api/agents/{persona_id}/files/{filename}` | Edit an identity file. `config.yaml` validated through `personas.validate_config_yaml_text` (Q5 single-yaml-surface lock). |
| GET | `/api/agents/{persona_id}/files/history` | Append-only revision history for the persona's files. |
| GET | `/api/agents/{persona_id}/conversation` | Recent conversation messages from `chat.db` for this persona. |
| GET | `/api/agents/{persona_id}/tokens` | Lane-aware usage: `claude_native: {turns, plan_quota_used_pct}` AND `generic: {by_provider: {...}, total_cost_usd}`. |
| GET | `/api/agents/{persona_id}/tasks` | Convoy subtasks scoped to this persona, via `convoy_service.list_subtasks_by_agent` (no direct convoy SQL). |
| GET | `/api/scheduled` | Scheduled-task list. |
| POST | `/api/scheduled` | Create a scheduled task. |
| PATCH | `/api/scheduled/{task_id}` | Update a scheduled task. |
| DELETE | `/api/scheduled/{task_id}` | Remove a scheduled task. |
| GET | `/api/memories` | Memory-search results (proxies `recall_service`). |
| GET | `/api/tokens` | Cross-persona lane-aware usage rollup. |
| GET | `/api/hive-mind/recent` | Recent cross-persona events for the 3D Hive Mind visualization. |
| GET | `/api/dashboard/settings` | Dashboard UI settings (theme, suggestions cursor, etc.). |
| PATCH | `/api/dashboard/settings` | Update dashboard UI settings. |
| GET | `/api/conversation/{persona_id}/stream` | SSE stream for live chat overlay. See §6. |
| GET | `/api/audit-log` | PRD-8 Phase 7a admin-only audit-log query (paginated). Auth: `Authorization: Bearer <DASHBOARD_ADMIN_TOKEN>` (sole auth path — outer middleware exempts this path). 503 fail-closed when `DASHBOARD_ADMIN_TOKEN` unset. Query params: `limit` (default 50, max 200), `before_id` (cursor pagination), optional `action` filter (e.g. `action=killswitch_refusal`). Detail field redacted on read via SECRET_PREFIXES. Phase 7a scope: kill-switch refusal rows + Phase 3 hard-delete rows ONLY (this is a `security_events` view, NOT a full audit trail — broader writers ship in Phase 7b). |

### 3.2 Convoy / team / mailbox / executor endpoints (existing — Phase 3 dashboard MAY consume)

These endpoints are owned by `.claude/scripts/orchestration/api.py` and
documented in `.claude/sections/07_orchestration.md`. Phase 3 cross-
references them here so dashboard frontend integrators know what
already exists. These are NOT reimplemented in `dashboard_api.py`; the
dashboard either calls them directly or proxies through the
`/api/agents/{persona_id}/tasks` shape that wraps
`convoy_service.list_subtasks_by_agent`.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/convoy` | Create convoy with subtasks + edges. |
| GET | `/api/convoy/{id}` | Get convoy with subtasks. |
| GET | `/api/convoy` | List convoys (optional status filter). |
| POST | `/api/convoy/{id}/status` | Transition convoy status. |
| DELETE | `/api/convoy/{id}` | Delete convoy (cascade). |
| POST | `/api/convoy/{id}/subtasks` | Add subtasks to an existing convoy. |
| GET | `/api/convoy/{id}/ready` | Get ready-to-dispatch subtasks. |
| POST | `/api/convoy/{id}/subtask/{sid}/dispatch` | Dispatch subtask via executor. |
| POST | `/api/convoy/{id}/subtask/{sid}/complete` | Mark complete (triggers dependency release). |
| POST | `/api/convoy/{id}/subtask/{sid}/fail` | Mark failed. |
| POST | `/api/convoy/{id}/subtask/{sid}/transition` | Mechanical transitions (running/stalled/cancelled). |
| PATCH | `/api/convoy/{id}/subtask/{sid}` | Update fields (agent, branch, merge_commit, error). |
| POST | `/api/convoy/{id}/subtask/{sid}/progress` | Report progress percentage + message. |
| POST | `/api/mailbox/send` | Send inter-agent message. |
| GET | `/api/mailbox/inbox/{agent}` | Agent inbox. |
| POST | `/api/mailbox/claim/{agent}` | Claim deliveries. |
| POST | `/api/mailbox/ack/{delivery_id}` | Acknowledge delivery. |
| POST | `/api/executor/callback` | Executor callback ingress (idempotent via `callback_receipts.idempotency_key`). |
| GET | `/api/team` | Team-session list. |
| GET | `/api/team/{id}` | Team detail. |
| GET | `/api/team/{id}/members` | Team members. |
| POST | `/api/team/{id}/shutdown` | Coordinated team shutdown. |
| GET | `/api/team/{id}/memory` | Team-scoped memory entries. |
| POST | `/api/team/{id}/memory` | Append a team-memory entry. |

The dashboard MUST NOT add a duplicate router for any of these; it
either consumes them directly via `framework-client.ts` or wraps them
in a Phase 3 dashboard endpoint that adds persona-scoped filtering
(e.g. `/api/agents/{persona_id}/tasks`).

---

## 4. main↔default Translation Contract (Q4 Lock)

ClaudeClaw's frontend uses `main` for the default persona id; The
Homie's Python framework uses `default`. Phase 3 fixes the boundary at
**one site only** — `dashboard/server/src/translate.ts`.

### 4.1 The single translation site

`translate.ts` exports two pure functions:

```ts
export function inboundPersonaId(id: string): string;   // 'main' → 'default'
export function outboundPersonaId(id: string): string;  // 'default' → 'main'
```

Every Hono route handler that takes a persona id from the browser
calls `inboundPersonaId()` BEFORE forwarding to port 4322. Every
response that returns a persona id calls `outboundPersonaId()` BEFORE
returning to the browser.

### 4.2 Python framework rejects `main`

The Python framework helper `_reject_main_translation(persona_id)` is
called at the entry of every `dashboard_api.py` endpoint that takes a
`persona_id` path parameter. If the value is `main`, the framework
raises `HTTPException(422)` with a clear error message instructing the
caller to translate at the Hono boundary. This prevents a second
translation site from accidentally appearing inside Python.

### 4.3 Regression test

`test_python_framework_does_not_translate_main` regression-gates this
contract: every Phase 3 endpoint with a persona-id path parameter is
exercised with `persona_id="main"` and must return 422.

---

## 5. Error Envelope

Three error envelope shapes are returned, distinguished by which
middleware/handler emits them:

### 5.1 FastAPI HTTPException

```json
{ "detail": "<string explanation>" }
```

Returned by FastAPI for `HTTPException(status_code=N, detail=...)`.
Status codes used: 400 (bad request), 403 (forbidden), 404 (not found),
409 (conflict), 410 (gone — SSE replay), 415 (unsupported media type),
422 (unprocessable entity — schema/validation), 500 (internal),
503 (audit log unavailable).

### 5.2 Auth / CSRF middleware

```json
{ "error": "<string explanation>" }
```

Returned by the orchestration auth middleware (401 missing/invalid
token) and the Hono CSRF middleware (403 origin not in allowlist).

### 5.3 Lane-aware operation result with warnings

```json
{
  "deleted": true,
  "outcome": "success",
  "warnings": ["audit_after_write_failed: <error>"]
}
```

Returned by destructive endpoints (`DELETE /api/agents/{id}/full`,
`PUT /api/agents/{id}/avatar`) where the result must carry both an
operation flag and one or more non-fatal post-condition warnings. The
`warnings` array is always present (possibly empty); the `outcome`
enum follows the disk-state Rule 2 contract documented in
`dashboard-owner.md` § Hard-Delete Audit-After Failure Policy.

Lane-aware usage endpoints (`/api/agents/{persona_id}/tokens`,
`/api/tokens`) return both lane fields side-by-side:

```json
{
  "claude_native": { "turns": 142, "plan_quota_used_pct": 23.5 },
  "generic": {
    "by_provider": { "openai_codex": { "cost_usd": 0.42 }, "openai-compatible": { "cost_usd": 0.18 } },
    "total_cost_usd": 0.60
  }
}
```

Both branches are always present; missing data renders as zeros, never
as omitted keys.

---

## 6. SSE Event Reference

The single SSE endpoint is `GET /api/conversation/{persona_id}/stream`.
It emits a Server-Sent-Events stream that the dashboard's chat overlay
subscribes to. The contract below is locked under criterion
`framework_endpoint_sse_conversation_stream`.

### 6.1 Event format

Each event is two lines plus a blank-line terminator:

```
id: <monotonic int>
event: <event-type>
data: <json>

```

The `id:` line MUST precede the `data:` line. The `event:` line
disambiguates `processing` / `chunk` / `complete` / `error`.

### 6.2 Monotonic integer event_id

Event ids are **monotonic integers per (persona_id, conversation_id)
stream**, starting at 1 and incrementing by 1. Hash-based ids are not
permitted (R5-R7 lock — closes the duplicate-id-on-restart class).

### 6.3 Keepalive

The server emits `: keepalive\n\n` (a comment line, no event/data)
every 20 seconds when no real events are flowing. This prevents
intermediate proxies (nginx, cloudflare) from closing the idle
connection. `: keepalive\n\n` is the canonical Phase 3 doctrine; any
"30s keepalive" reference is stale R1-pre prose.

### 6.4 Last-Event-ID resume + 410 Gone

The browser EventSource auto-reconnect sends `Last-Event-ID: <int>` in
the request header on reconnect. The server:

1. Reads the header, parses it as int (returns no-replay on parse
   failure).
2. Looks up the in-memory replay buffer for `(persona_id,
   conversation_id)`. The buffer holds the last 100 events.
3. If `last_event_id < earliest_buffered_id`, returns
   `HTTP 410 Gone` with header `X-Refetch-Hint:
   GET /api/agents/{persona_id}/conversation` and a JSON body
   `{ "error": "stale Last-Event-ID outside replay buffer" }`. The
   client should drop the EventSource and refetch the conversation
   from scratch.
4. Otherwise replays buffered events with `id > last_event_id` (no
   duplicates, no skipped events) before resuming live emit.

### 6.5 100-event in-memory replay buffer

The buffer is keyed by `(persona_id, conversation_id)`, capped at 100
events per stream. It is process-local (in-memory). On Hono restart or
Python framework restart, all buffers are dropped — clients hit 410
Gone on next reconnect and refetch.

### 6.6 Response headers

The SSE response sets these headers for proxy compatibility and
defense-in-depth:

| Header | Value |
|---|---|
| `Content-Type` | `text/event-stream` |
| `X-Accel-Buffering` | `no` (defeats nginx response buffering) |
| `Cache-Control` | `no-cache` |
| `Referrer-Policy` | `no-referrer` (defense for token-in-query-string SSE flow) |

---

## 7. Forward-Only-Additive Policy (Q3)

The dashboard contract is **additive only**. Phase 5 / Phase 6 / Phase
7 may extend the surface but must never break existing endpoint shapes
that operators already depend on.

### 7.1 What is allowed

- Adding new endpoints under `/api/`.
- Adding new optional query parameters with backward-compatible
  defaults.
- Adding new optional fields to request bodies (must default to
  preserving existing behavior when absent).
- Adding new optional fields to response objects (consumers must not
  reject unknown keys).
- Adding new columns or tables to `dashboard.db` via idempotent
  `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE IF NOT EXISTS` patterns
  — never `DROP COLUMN` or `RENAME COLUMN`.
- Adding new SSE event types — the `event:` line is the discriminator
  and unknown types are tolerated by spec.
- Adding new entries to the response `warnings: [...]` array.

### 7.2 What is forbidden

- Removing an endpoint that has shipped publicly. Use a 410 Gone
  response with a deprecation note instead.
- Renaming a request body field without keeping the old name as an
  alias for at least one phase.
- Renaming a response field without keeping the old name as a
  duplicate output for at least one phase.
- Changing the type of an existing field (string → int, scalar →
  array). This breaks every consumer that doesn't strictly type-check
  responses.
- Tightening a regex/validation rule that previously accepted some
  shape of input. Loosening is permitted; tightening is a breaking
  change for clients that submit edge-case inputs.
- Removing a deny-rule from the sanitizer that previously protected
  data leaking into the public mirror.

### 7.3 Phase 5 / 7 expansion notes

- Phase 5 (cabinet) introduces a sibling Python service for advanced
  cabinet workflows. The cabinet endpoints will mount onto the same
  port-4322 app under `/api/cabinet/*` and will not modify any Phase
  3 endpoint shape.
- Phase 7 (security hardening) extends the `audit_log` writers and
  introduces AES-256-GCM encryption at rest for sensitive
  configuration. Phase 7 will add new fields to existing audit-log
  rows but will not rename or remove the existing
  `outcome` / `target_persona_id` / `operator_id` shape contract.

---

## 8. Kill-switches (PRD-8 Phase 7a + Phase 7b)

Operator-toggleable env-var kill-switches gate LLM, recall, voice, persona-
mutation, persona-operations (and — Phase 7b commit-2 — cabinet) surfaces.
Each refusal raises `KillSwitchDisabled`, increments a per-switch counter,
and writes an `audit_log` row with `action='killswitch_refusal'`. Callers
catch the exception explicitly and degrade gracefully.

### 8.1 Env var pattern

`HOMIE_KILLSWITCH_<NAME>=disabled` (case-insensitive). Read on every call
(Rule 2 — never cached). Switches:

- `HOMIE_KILLSWITCH_LLM` (Phase 7a) — gates `runtime/lane_router.run_with_runtime_lanes`,
  `runtime/registry.run_with_fallback`, and `heartbeat.py` HARO direct-SDK
  pitch generation
- `HOMIE_KILLSWITCH_RECALL` (Phase 7a) — gates `chat/recall_service.recall`
  (wrap is INSIDE the `@observe` scope so the `chat_message → recall`
  Langfuse span hierarchy is preserved on refusal — refusal becomes the
  span output with `tier="killswitch_disabled"`)
- `HOMIE_KILLSWITCH_VOICE` (Phase 7b commit-1, WS2) — gates ALL THREE voice
  cascade chokepoints: `voice.transcribe_audio_file` (cascade STT entry),
  `voice.transcribe` (legacy 3-arg helper), `voice.synthesize` (cascade TTS
  entry). All 6 adapters (Telegram, Discord, Slack, WhatsApp, Web/Relay,
  CLI) catch and emit a friendly degraded reply.
- `HOMIE_KILLSWITCH_PERSONA_MUTATION` (Phase 7b commit-1, WS4) — gates
  persistent-state mutations on the persona surface. Lifecycle layer:
  `personas.lifecycle.create_profile`, `delete_profile`, `use_profile`.
  HTTP layer: 9 routes (`POST /api/agents`, `DELETE /api/agents/{id}`,
  `DELETE /api/agents/{id}/full`, `PUT/DELETE /api/agents/{id}/avatar`,
  `POST /api/agents/suggestions/refresh`, `PATCH /api/agents/model`,
  `PATCH /api/agents/{id}/model`, `PATCH /api/agents/{id}/files/{filename}`).
  Returns 503 with `{"error": "persona mutations are disabled by operator",
  "switch": "persona_mutation"}` on refusal.
- `HOMIE_KILLSWITCH_PERSONA_OPERATIONS` (Phase 7b commit-1, WS4) — SECOND,
  lighter-scope switch for runtime-lifecycle ONLY (NO persistent-state
  writes): `POST /api/agents/{id}/activate`, `POST /api/agents/{id}/deactivate`,
  `POST /api/agents/{id}/restart`. Operators can lock identity changes
  (persona_mutation) while still rebooting persona bots, or vice versa.
  Returns 503 with `{"error": "persona operations are disabled by operator",
  "switch": "persona_operations"}` on refusal.
- `HOMIE_KILLSWITCH_CABINET` (Phase 7b commit-2 — pending cabinet ship) —
  will gate cabinet chat-process chokepoints at `core_handlers.handle_cabinet`,
  `handle_standup`, `handle_discuss`. The HTTP-layer cabinet wrap on
  `/api/cabinet/send` already exists (see Phase 5a M7 layer 1). Adding the
  chat chokepoints in commit-2 closes the direct-CLI/Telegram bypass.

### 8.2 KillSwitchDisabled contract

Callers MUST catch `KillSwitchDisabled` explicitly and return a documented
degraded response (NOT swallow silently, NOT surface as a generic error).
Phase 7a wires explicit catches at:

- `chat/engine.py` — yields `[killswitch:<name>] This feature is disabled
  by the operator…` outgoing message
- `scripts/memory_reflect.py` — logs `Reflection skipped: kill-switch
  '<name>' disabled`, returns None (exit 0, NOT failed)
- `scripts/memory_weekly.py` — logs `Weekly synthesis skipped: kill-switch
  '<name>' disabled`, returns None
- `scripts/memory_dream.py` — saves state with `result="skipped_killswitch"`
  (NOT `"failed"`) so the recency guard does NOT force retry

### 8.3 Refusal counter visibility

`/api/health` exposes the rich snapshot under `killSwitches` (see §1.3).
The dashboard frontend `dashboard/web/src/components/KillSwitchBanner.tsx`
(PRD-8 Phase 7a WS7) renders nonzero counters/audit_write_failures
explicitly. Phase 7a shipped 4 vitest tests; Phase 7b commit-1 (WS6)
extended that to 9 to cover voice, persona_mutation, persona_operations,
and a forward-compat cabinet case (the new switches auto-light-up via the
`_REFUSAL_COUNTERS` dict; ZERO backend changes needed for the banner to
surface them — the test extension locks the rendering contract).
`dashboard/web/src/__tests__/kill-switch-banner.test.tsx`.

### 8.4 Audit row shape on refusal

```json
{
  "action": "killswitch_refusal",
  "operator_id": "kill_switch_runtime",
  "target_persona_id": "<switch_name>",   // e.g. "llm" or "recall"
  "outcome": "disabled",
  "blocked": true,
  "detail": "{\"caller\": \"<caller_label>\", \"switch\": \"<switch_name>\"}"
}
```

`/api/audit-log` (admin-only) reads these rows back with the `detail`
field scrubbed via SECRET_PREFIXES (defense-in-depth — caller paths or
stringified objects could include real keys).

### 8.5 Single-source-of-truth secret patterns

`.claude/scripts/security/patterns.py:SECRET_PREFIXES` (≥27 vendor key
prefixes — Anthropic, OpenAI, Stripe, ElevenLabs, Groq, Gradium, Slack,
GitHub, AWS, Google, JWT, npm, Docker, GitLab, SendGrid, Mailgun, Heroku,
Postmark, Langfuse) is the sole source consumed by `scripts/sanitize.py`,
`runtime/subprocess_env.py`, and the dashboard `_redact_secret_shaped`
helper. Three-layer parity test rejects any local copies. Phase 4 keys
(`sk_` ElevenLabs, `gsk_` Groq, `gr_` Gradium) are present so Phase 4
ships safely.

### 8.6 Log-message redact (Phase 7b commit-1, WS1)

`.claude/scripts/security/redact.py` (Hermes verbatim port — 340 LOC) is
the single redaction helper for log/error message text, NOT just env
scrubbing. Default ON via `_REDACT_ENABLED` import-time snapshot reading
`HOMIE_REDACT_SECRETS` against the tuple `("0", "false", "no",
"off")`. Unset env → not in tuple → True (ON), matching Hermes line 60.
Operators wrap log call sites:

```python
from security import redact
logger.error("submission failed: %s", redact.redact(str(exc)))
```

`redact()` is unconditional (NOT gated on a kill-switch — making it
operator-toggleable would let an operator disable secret scrubbing). The
`_REDACT_ENABLED` snapshot is for test/debug ONLY; it's resolved at module
import and not re-read on each call (defeats LLM mid-process attacks that
might try to flip the env var).

The `security/__init__.py` re-export of `redact` is LAZY via PEP 562
`__getattr__` — non-redact consumers (lane_router, cabinet text_router/
text_orchestrator) do NOT trigger config import or `_REDACT_ENABLED`
snapshot when they `from security import kill_switches`. Only consumers
that explicitly access `security.redact` pay the import cost.

ClaudeClaw `getScrubbedSdkEnv` parity (Phase 7b WS5) lives in
`runtime/subprocess_env.py`; the upstream audit at
`~/.refs/claudeclaw-os/src/security.ts:200-334` is locked
by `tests/test_subprocess_env_claudeclaw_parity.py` (36 tests). Closed
gap on this audit: nested Claude-Code-session state vars
(`CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `_EXECPATH`, `_SSE_PORT`,
`_IPC_PORT`, `_MAX_OUTPUT_TOKENS`, `_EXPERIMENTAL_AGENT_TEAMS`) added
to `_NESTED_CLAUDE_CODE_STATE_KEYS` and dropped BEFORE the `CLAUDE_CODE_`
prefix-allowlist check (the prefix exists for `CLAUDE_CODE_OAUTH_TOKEN`
auth — not session state).

---

Sign-off: **YourAgent**
