# Dashboard Mobile Access

Status: shipped and live-proven
Owner: Homie Dashboard over Python-owned dashboard API
Last updated: 2026-05-31

## What It Does

Dashboard Mobile Access makes the local Homie Dashboard usable from a phone on
the same Tailscale tailnet. It exposes the working raw tailnet IP dashboard URLs
for Browser Viewer, Teams, and the Mobile Access page, plus sanitized local
Tailscale/Dashboard/Serve status.

## Operator Entry Points

- Dashboard: `/mobile`
- API: `GET /api/dashboard/mobile-access`
- Hono proxy: `GET /api/dashboard/mobile-access`

## Source Of Truth Files

| Layer | Files |
|---|---|
| Python/runtime | `.claude/scripts/dashboard_api.py` |
| Hono/dashboard server | `dashboard/server/src/routes/settings.ts`, `dashboard/server/src/routes.ts` |
| Dashboard web | `dashboard/web/src/pages/MobileAccess.tsx`, `dashboard/web/src/lib/routes.ts`, `dashboard/web/src/App.tsx` |
| Tests | `.claude/scripts/tests/test_dashboard_api.py`, `dashboard/server/src/__tests__/settings.test.ts`, `dashboard/web/src/__tests__/mobile-access.test.tsx` |
| Tracker/proof | `PRPs/active/TRACKER.md` |

## Safety Boundaries

- Read-only status surface only.
- Does not mutate Tailscale state.
- Does not mutate browser state.
- Does not expose peers, users, node keys, tokens, cookies, browser state, or
  full Tailscale status payloads.
- Hono stays thin and forwards to Python.
- Phone links prefer raw tailnet IP URLs because `.ts.net` HTTPS can be
  browser/serve sensitive on local dev stacks.

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
npm run dev -- --host <tailscale-ip>
```

Open:

```text
http://<tailscale-ip>:5173/mobile
```

## How To Test It

```powershell
cd C:\Users\YourUser\thehomie\.claude\scripts
uv run pytest tests/test_dashboard_api.py -q -k "mobile_access or dashboard_settings"
uv run python -m py_compile dashboard_api.py
```

```powershell
cd C:\Users\YourUser\thehomie\dashboard\server
npm run test -- src/__tests__/settings.test.ts src/__tests__/routes-manifest.test.ts
npm run typecheck
```

```powershell
cd C:\Users\YourUser\thehomie\dashboard\web
npm run test -- src/__tests__/mobile-access.test.tsx src/__tests__/donor-route-manifest.test.ts
npm run typecheck
```

## Latest Live Proof

- Date: 2026-05-31
- Surface: Tailscale raw-IP Vite URL, `http://<tailscale-ip>:5173/mobile`
- Result: page rendered, API returned sanitized status, Browser Viewer link
  pointed to the working raw-IP phone URL, Copy showed success, and console
  logs had no relevant warnings/errors.
- Commit: private `3fb2138 feat: add dashboard mobile access`

## Related Handoffs

- No separate handoff yet; tracker contains the live proof summary.

## Public Export Status

Private repo only at initial ship. Public framework export was not run.

## Next Slices

- Add a small dashboard health card showing whether API/Hono/Vite listeners are
  live for the current machine.
- Decide whether the public framework should include this local tailnet helper.
