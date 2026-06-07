# Homie Dashboard Framework

Status: canonical operator shell
Owner: `thehomie/dashboard` thin UI over Python-owned framework APIs
Last updated: 2026-06-06

## What It Does

The Homie Dashboard is the canonical local operator shell for the Homie runtime
inside `thehomie`. It is not a donor clone and not a separate product
surface from the framework. The dashboard renders and controls framework
features through Hono proxy routes over Python-owned APIs.

## Operator Entry Points

- Dashboard root: `dashboard/`
- Web dev surface: `http://127.0.0.1:5173`
- Hono server: `http://127.0.0.1:3141`
- Python orchestration/dashboard API: `http://127.0.0.1:4322`
- Key routes: `/mission`, `/work`, `/convoy`, `/agents`, `/chat`, `/browser`,
  `/mobile`, `/teams`, `/cabinet`, `/memories`, `/hive`
- `/chat` is the dashboard-native WEB conversation surface. It sends text,
  slash commands, and follow-up buttons through Python chat routing; Hono only
  proxies the send/history/SSE contracts.
- Internal cognitive status routes stay hidden from the public dashboard nav
  until that surface is re-proven.

## Source Of Truth Files

| Layer | Files |
|---|---|
| Python/runtime | `.claude/scripts/dashboard_api.py`, `.claude/scripts/orchestration/api.py`, `.claude/scripts/orchestration/*` |
| Hono/dashboard server | `dashboard/server/src/app.ts`, `dashboard/server/src/routes.ts`, `dashboard/server/src/routes/*` |
| Dashboard web | `dashboard/web/src/App.tsx`, `dashboard/web/src/lib/routes.ts`, `dashboard/web/src/pages/*` |
| Tests | `dashboard/server/src/__tests__/routes-manifest.test.ts`, `dashboard/web/src/__tests__/donor-route-manifest.test.ts`, feature-specific tests |
| Docs/proof | `docs/mission-control-donor-cross-reference.md`, `docs/homie-dashboard-feature-gap-map-2026-05-24.md`, `docs/mc-profile-contract.md` |

## Safety Boundaries

- Python owns business logic, runtime execution, orchestration state, and memory
  access.
- Hono stays thin: auth/dev-mode policy, persona translation, route manifest,
  and proxying.
- Dashboard web renders operator controls and state; it must not fork Python
  behavior into UI-local logic.
- Dashboard chat uses the Python chat router as source of truth. The web UI
  must not run a parallel assistant, bypass command routing, or invent local
  memory/session writes.
- Donor dashboards are references only. `thehomie/dashboard` is canonical.
- Public framework export uses `scripts/sanitize.py`; never manually copy
  dashboard files into `thehomie-framework`.
- Dashboard routes that observe browser state must preserve the BrowserOps
  read-only/default-deny boundaries.

## How To Run It

```powershell
cd C:\Users\YourUser\thehomie\.claude\scripts
uv run python -m orchestration.run_api
```

```powershell
cd C:\Users\YourUser\thehomie\dashboard\server
$env:DASHBOARD_DEV_MODE_NO_AUTH='true'
npm start
```

```powershell
cd C:\Users\YourUser\thehomie\dashboard\web
npm run dev
```

Open:

```text
http://127.0.0.1:5173
```

## How To Test It

Run feature-specific tests first. The baseline manifest checks are:

```powershell
cd C:\Users\YourUser\thehomie\dashboard\server
npm run test -- src/__tests__/routes-manifest.test.ts
npm run typecheck
```

```powershell
cd C:\Users\YourUser\thehomie\dashboard\web
npm run test -- src/__tests__/donor-route-manifest.test.ts
npm run typecheck
```

For Python-owned feature APIs, run the matching `.claude/scripts/tests/*`
focused suite.

## Latest Live Proof

- Date: 2026-06-06
- Dashboard chat write surface passed on isolated ports `45139/33157`:
  `/chat` sent `hello`, `/provider`, and `/status` from the actual dashboard
  composer in the in-app browser. The page streamed a normal Homie assistant
  reply, `Runtime Provider Status`, and `Session Status` through Python-owned
  routing. No raw fetch errors, console warnings, or `Message failed` toast
  appeared after the isolated stack was ready.
- The isolated services were stopped and ports `45139/33157` were confirmed
  closed.
- Date: 2026-05-31
- Dashboard stack proved across multiple slices:
  - Team Room controls live smoke at `/teams`
  - Browser Viewer proof at `/browser`
  - Mobile Access proof at `/mobile`
- Current stack for the Mobile Access proof:
  - Python API `4322`
  - Hono `3141`
  - Vite web `5173`
  - visible Chrome CDP `9222`

## Related Handoffs

- `docs/mission-control-donor-cross-reference.md`
- `docs/homie-dashboard-feature-gap-map-2026-05-24.md`
- `docs/HANDOFF-team-room-dashboard-controls-closeout-2026-05-31.md`
- `docs/HANDOFF-team-room-v3-live-proof-closeout-2026-05-31.md`

## Public Export Status

Dashboard framework files are public-exported only when a slice explicitly runs
`scripts/sanitize.py` and pushes `C:\Users\YourUser\thehomie-framework`.

## Next Slices

- Team Room V3 artifact panels on `/teams`.
- Mission Control / Hub consumer for the BrowserOps viewer API.
- Manual pages for Unified Brain, memory graph, Work Queue, and runtime lane
  surfaces.
