# BrowserOps And Browser Viewer

Status: shipped and live-proven
Owner: `.claude/chat/` browser policy plus Python dashboard API
Last updated: 2026-05-31

## What It Does

BrowserOps is the Homie browser specialist surface. It loads browser-capable
context, checks visible Chrome/CDP readiness, gates browser workflows, writes
sanitized audit rows, and exposes a read-only dashboard Browser Viewer at
`/browser`.

## Operator Entry Points

- Chat/Telegram: `/browser status`, `/browser tabs`, `/browser snapshot`,
  `/browserops capabilities`, `/browserops guide`, `/browserops context`,
  `/linkedin_profile status`, `/linkedin_profile open`
- Dashboard: `/browser`
- API: `/api/browser-viewer/status`, `/api/browser-viewer/screenshot`,
  `/api/browser-viewer/stream/enable`, `/api/browser-viewer/stream/disable`
- Direct tool: `agent-browser --cdp 9222` for authorized visible-browser work

## Source Of Truth Files

| Layer | Files |
|---|---|
| Browser policy | `.claude/chat/browser_control.py`, `.claude/chat/browser_workflows.py`, `.claude/chat/browser_audit.py`, `.claude/chat/browser_ops.py` |
| Chat/router | `.claude/chat/core_handlers.py`, `.claude/chat/commands.py`, `.claude/chat/router.py`, `.claude/chat/extension_manager.py` |
| Python dashboard API | `.claude/scripts/dashboard_api.py` |
| Hono/dashboard server | `dashboard/server/src/routes/browser-viewer.ts`, `dashboard/server/src/routes.ts` |
| Dashboard web | `dashboard/web/src/pages/BrowserViewer.tsx`, `dashboard/web/src/lib/routes.ts` |
| Tests | `.claude/scripts/tests/test_agent_browser_framework.py`, `.claude/scripts/tests/test_browser_workflows.py`, `.claude/scripts/tests/test_browser_audit.py`, `.claude/scripts/tests/test_browser_ops.py`, `dashboard/server/src/__tests__/browser-viewer.test.ts`, `dashboard/web/src/__tests__/browser-viewer.test.tsx` |
| Docs/proof | `docs/browserops-agent-browser-manual.md`, `docs/HANDOFF-team-room-v3-live-proof-closeout-2026-05-31.md` |

## Safety Boundaries

- Dashboard `/browser` is read-only.
- Dashboard `/browser` must not type, click, navigate, log in, edit profiles,
  send messages, post to social, inspect raw URL lists, or export cookies/tokens.
- Direct browser input belongs to an explicitly authorized visible CDP browser
  workflow, not the dashboard viewer.
- Browser writes such as LinkedIn edits/posts/DMs remain default-denied until a
  dedicated PRP defines approval UX, audit, tests, and proof.

## How To Run It

```powershell
cd C:\Users\YourUser\thehomie\.claude\scripts
uv run thehomie chat -q "/browser status" -Q
uv run thehomie chat -q "/browserops capabilities" -Q
uv run thehomie chat -q "/browserops guide" -Q
```

Dashboard:

```text
http://127.0.0.1:5173/browser
```

## How To Test It

```powershell
cd C:\Users\YourUser\thehomie\.claude\scripts
uv run pytest tests/test_agent_browser_framework.py tests/test_browser_workflows.py tests/test_browser_audit.py tests/test_browser_ops.py -q
```

```powershell
cd C:\Users\YourUser\thehomie\dashboard\server
npm run test -- src/__tests__/browser-viewer.test.ts src/__tests__/routes-manifest.test.ts
npm run typecheck
```

```powershell
cd C:\Users\YourUser\thehomie\dashboard\web
npm run test -- src/__tests__/browser-viewer.test.tsx src/__tests__/donor-route-manifest.test.ts
npm run typecheck
```

## Latest Live Proof

- Date: 2026-05-31
- Surface: dashboard `/browser` observing the same visible Chrome CDP `9222`
  Telegram Web session used for Team Room V3 proof.
- Result: readiness `ready`, mode `read_only`, controls
  `browser_input=false`, `navigation=false`.

## Related Handoffs

- `docs/browserops-agent-browser-manual.md`
- `docs/HANDOFF-team-room-v3-live-proof-closeout-2026-05-31.md`

## Public Export Status

BrowserOps manual was allowed for public framework export. Browser Viewer code
was shipped through the framework path in earlier BrowserOps phases.

## Next Slices

- Mission Control / Hub consumer for the same read-only viewer API.
- Separate BrowserOps write-workflow PRP only when explicitly requested.
