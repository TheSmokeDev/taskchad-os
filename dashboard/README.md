# The Homie — Dashboard

Operator-facing web dashboard for The Homie framework. Replaces the
retired Next.js `mission-control` app. Ships as a vertical slice owned
by `dashboard-owner` (`.claude/agents/dashboard-owner.md`).

The dashboard slice has three components:

| Component | Tech | Port | Purpose |
|---|---|---|---|
| `.claude/scripts/dashboard_api.py` | Python / FastAPI | 4322 (shared) | 30 framework HTTP endpoints, mounted onto the orchestration app. |
| `dashboard/server/` | TypeScript / Hono | 3141 | Thin proxy. Translates `main↔default` (Q4 lock), forwards to port 4322. |
| `dashboard/web/` | TypeScript / Vite + Preact | 5173 (dev) | Browser bundle. Hono serves the production build same-origin from port 3141. |

The Python API is the single source of truth for all business logic.
The Hono server is a translation/auth boundary; it never opens SQLite,
never reads `<profile>/config.yaml`, never re-implements business
logic.

---

## Quick Start (Development)

You need three processes running:

```bash
# Terminal 1 — framework orchestration + dashboard HTTP API (port 4322)
cd .claude/scripts
uv run python -m uvicorn orchestration.api:app --host 127.0.0.1 --port 4322 --reload

# Terminal 2 — Hono thin proxy (port 3141)
cd dashboard/server
npm install
npm run dev

# Terminal 3 — Vite dev server (port 5173)
cd dashboard/web
npm install
npm run dev
```

Open `http://127.0.0.1:3141/` (or the Vite dev URL printed in terminal
3 if you want HMR). The first load auto-reads a `?token=<value>` query
parameter, persists it to `sessionStorage`, and uses it as the
`Authorization: Bearer <token>` for every subsequent request. SSE
streams pass the token via query string (browser EventSource API
limitation; see `docs/mc-profile-contract.md` § Auth Model).

---

## Production Build

```bash
# Build the web bundle
cd dashboard/web
npm install
npm run build
# Output: dashboard/web/dist/

# Build the Hono server
cd dashboard/server
npm install
npm run build
# Output: dashboard/server/dist/

# Start the Hono server in production mode
cd dashboard/server
npm start
```

The production Hono server serves `dashboard/web/dist/` as static
assets same-origin from port 3141. The framework HTTP API still runs
on port 4322 alongside (no separate proxy hop for SSE).

Both `dashboard/server/dist/` and `dashboard/web/dist/` are sanitizer-
denied — they never ship in the public framework export. Sources do
ship.

---

## Tokens and Dashboard Chat

### Token aliases

`DASHBOARD_TOKEN` is an operator-friendly alias for
`ORCHESTRATION_API_TOKEN`. Either both may be set (must be equal), or
exactly one may be set (it aliases for the other), or neither may be
set (loopback dev-mode opt-in only). The four-branch policy is fully
documented in `docs/mc-profile-contract.md` § 1.2 and enforced at
startup by `dashboard/server/src/auth-policy.ts`.

| Configuration | Behavior |
|---|---|
| Both set, equal | Start normally (mode `token-equal`). |
| Both set, different | Refuse to start. |
| Only one set | Start normally — the set value aliases for the unset variable (mode `token-alias`). |
| Neither set, non-loopback bind | Refuse to start. |
| Neither set, loopback bind, `DASHBOARD_DEV_MODE_NO_AUTH=true` | Start with WARN-on-every-request. Loopback only (mode `dev-mode-loopback`). |
| Neither set, loopback bind, no opt-in | Refuse to start. |

### Browser session token persistence

The first browser request to the dashboard URL may include a
`?token=<value>` query parameter. The web bundle reads it, stores it
under `sessionStorage["dashboard_token"]`, and removes the query
parameter from `window.location` so it does not leak into the address
bar history. Subsequent requests use the stored value as a Bearer
header. Closing the tab clears the session (browser
`sessionStorage` semantics).

### SSE token via query string

Because the browser `EventSource` API cannot set custom headers, the
SSE endpoint
(`GET /api/conversation/{persona_id}/stream`) accepts the token via
`?token=<value>` query string. The Hono `log-scrub.ts` middleware
strips this parameter from access logs, sets `Referrer-Policy: no-
referrer` on the response, and never forwards the query string to the
Python framework. Token-in-query-string is permitted ONLY for browser→
Hono SSE; anywhere else it is forbidden.

### Dashboard chat conversation

The default dashboard chat uses persona `main` and conversation
`dashboard-main`. It loads history through
`GET /api/conversation/main/history`, opens the SSE stream through
`GET /api/conversation/main/stream?conversation_id=dashboard-main`,
and sends text, slash commands, or follow-up button actions through
`POST /api/conversation/main/send`.

The send path is intentionally thin. Hono proxies to Python, and Python
routes the message through the same chat router and WEB adapter contract
used by the rest of the framework. Linked channel streams opened with a
legacy `chatId` remain read-only in the dashboard.

---

## Browser Routing Map (15 routes)

The Vite + Preact bundle uses `wouter` for routing. The 15 routes are:

| Route | Page | Purpose |
|---|---|---|
| `/mission` | `MissionControl` | Default landing — convoy/team/mailbox overview. |
| `/scheduled` | `Scheduled` | Cron-style scheduled task list and editor. |
| `/agents` | `Agents` | Persona dashboard — list, create wizard, toggle activate. |
| `/agents/:id` | `AgentDetail` | Single persona — config files, conversation, tokens, tasks. |
| `/agents/:id/files` | `AgentFiles` | Identity-file editor (config.yaml, SOUL.md, USER.md, etc.). |
| `/chat` | `Chat` | Dashboard WEB chat — history, send, follow-up buttons, and SSE stream. |
| `/memories` | `Memories` | Memory search proxying `recall_service`. |
| `/hive` | `HiveMind` | 3D Hive Mind brain visualization (Three.js). |
| `/usage` | `Usage` | Lane-aware token/cost rollups. |
| `/jarvis` | `Jarvis` | Runtime, autonomy, memory, channel, and observability proof surface. |
| `/audit` | `Audit` | Append-only audit log viewer (placeholder for Phase 7). |
| `/cabinet` | `Cabinet` | Multi-persona War Room shell (placeholder for Phase 5). |
| `/voices` | `Voices` | Voice cascade configuration (placeholder for Phase 4). |
| `/standup` | `StandupConfig` | Daily standup configuration (placeholder for Phase 5). |
| `/settings` | `Settings` | Dashboard UI settings — theme, suggestions cursor. |

Plus three legacy redirects:

- `/hive-mind` → `/hive`
- `/hivemind` → `/hive`
- `/memory` → `/memories`
- `/warroom` → `/cabinet`

The root `/` redirects to the configured `DEFAULT_ROUTE` (default
`/mission`).

---

## Components and Pages Catalog

### 17 components in `dashboard/web/src/components/`

`AgentActions`, `AgentCard`, `AgentCreateWizard`, `AgentRow`,
`AvatarUploader`, `BrainGraph`, `BrainGraph3D`, `CommandPalette`,
`Empty`, `FileEditor`, `KillSwitchBanner`, `LaneStatusPill`,
`MemoryRow`, `Modal`, `ScheduledRow`, `Sidebar`, `Spinner`, `Tabs`,
`Toaster`, `TopBar`.

These are all leaf-level reactive components — `@preact/signals` is
the single state library; do not introduce parallel libs (Redux,
Zustand, Jotai).

### 10 functional pages

`MissionControl`, `Scheduled`, `Agents`, `AgentDetail`, `AgentFiles`,
`Chat`, `Memories`, `HiveMind`, `Usage`, `Settings`.

### 4 placeholder pages

`Audit`, `Cabinet`, `Voices`, `StandupConfig` — visible in the sidebar
and routable but render placeholder content. They become functional
in Phase 4 (Voices), Phase 5 (Cabinet, StandupConfig), and Phase 7
(Audit).

---

## Future Work

- **Dashboard chat attachments.** The current dashboard chat write path
  supports text, slash commands, and follow-up buttons only. File upload,
  voice, and richer attachment parity should be added only after the
  channel attachment contract is explicitly designed and tested.
- **Telegram avatar fetch + bot avatar PUT (OQ4 deferred).** Today
  avatar upload is local: PNG/JPEG/WEBP files via
  `PUT /api/agents/{id}/avatar`. Reading the bot's existing avatar
  from Telegram (`getUserProfilePhotos`) and pushing avatar updates
  out to Telegram is a Phase 4 nice-to-have.
- **Scheduled-task runner (deferred).** The dashboard reads/writes the
  scheduled-task table. The runner that actually fires the cron
  schedule against the bot is a separate slice; Phase 3 ships the
  CRUD surface without the scheduler daemon.
- **`personas.list_personas()` public API addition (OQ9).** The
  dashboard reads the persona list via internal helpers today.
  Phase 4 will promote a `list_personas()` helper into
  `personas.__all__` for consistent third-party consumption.
- **AES-256-GCM encryption at rest (Phase 7).** Sensitive configuration
  (bot tokens, audit-log rows) currently rides Bearer-token transport
  encryption only. Phase 7 hardens with at-rest encryption for the
  fields that warrant it.

---

## Dashboard Lineage

The dashboard and operator experience were inspired by
[ClaudeClaw](https://github.com/earlyaidopters/claudeclaw), especially the
idea that an agent runtime should have a visible operator surface instead of
living only behind chat commands. The Homie dashboard adapts that direction for
its own Python-owned runtime:

- `dashboard/web/` provides the Vite + Preact operator surface.
- `dashboard/server/` strips bot internals out of the TypeScript layer; Hono
  proxies to Python-owned runtime APIs.
- `translate.ts` keeps the Q4 single-translation-site lock.
- The TypeScript dashboard never imports YAML directly; Python owns the Q5
  single-yaml surface.
- Cost/usage endpoints stay lane-aware.
- Hard-delete behavior stays aligned with disk-state Rule 2.

This is attribution and lineage credit only. The Homie is independent and is
not affiliated with, sponsored by, or endorsed by ClaudeClaw or its
maintainers.

---

Sign-off: **YourAgent**
