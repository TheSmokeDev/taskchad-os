# Scheduled Jobs, Settings, And Audit

Status: active baseline
Owner: Python dashboard API with thin dashboard controls
Last updated: 2026-05-31

## What It Does

Scheduled Jobs, Settings, and Audit cover utility dashboard surfaces: cron-like
scheduled tasks, dashboard settings, mobile access status, kill-switch banners,
and audit placeholders.

## Operator Entry Points

- Dashboard: `/scheduled`, `/settings`, `/audit`, `/mobile`
- API: `/api/scheduled/*`, `/api/dashboard/settings`,
  `/api/dashboard/mobile-access`, Python-owned audit endpoints

## Source Of Truth Files

| Layer | Files |
|---|---|
| Python/runtime | `.claude/scripts/dashboard_api.py` |
| Hono/dashboard server | `dashboard/server/src/routes/settings.ts`, scheduled/audit routes, `dashboard/server/src/routes.ts` |
| Dashboard web | `dashboard/web/src/pages/Scheduled.tsx`, `dashboard/web/src/pages/Settings.tsx`, `dashboard/web/src/pages/Audit.tsx`, `dashboard/web/src/pages/MobileAccess.tsx`, `dashboard/web/src/components/KillSwitchBanner.tsx` |
| Tests | `.claude/scripts/tests/test_dashboard_api.py`, `dashboard/server/src/__tests__/settings.test.ts`, `dashboard/web/src/__tests__/mobile-access.test.tsx`, `dashboard/web/src/__tests__/kill-switch-banner.test.tsx` |

## Safety Boundaries

- Cron/schedule input needs validation.
- Settings patches are scoped dashboard settings, not arbitrary config writes.
- Mobile Access is read-only and must not mutate Tailscale/browser state.
- Audit log access should preserve admin/auth boundaries.
- Kill-switch banners must not hide backend kill-switch failures.

## How To Run It

```text
http://127.0.0.1:5173/scheduled
http://127.0.0.1:5173/settings
http://127.0.0.1:5173/audit
http://127.0.0.1:5173/mobile
```

## How To Test It

```powershell
cd C:\Users\YourUser\thehomie\.claude\scripts
uv run pytest tests/test_dashboard_api.py -q -k "scheduled or dashboard_settings or mobile_access"
```

```powershell
cd C:\Users\YourUser\thehomie\dashboard\server
npm run test -- src/__tests__/settings.test.ts src/__tests__/routes-manifest.test.ts
npm run typecheck
```

```powershell
cd C:\Users\YourUser\thehomie\dashboard\web
npm run test -- src/__tests__/mobile-access.test.tsx src/__tests__/kill-switch-banner.test.tsx
npm run typecheck
```

## Latest Live Proof

Mobile Access was live-proven on 2026-05-31. Scheduled/settings/audit surfaces
should be re-smoked before making fresh live claims.

## Related Handoffs

- `docs/manual/features/dashboard-mobile-access.md`
- `PRPs/active/TRACKER.md`

## Public Export Status

Verify per slice. Mobile Access was private-only at initial ship.

## Next Slices

- Flesh out scheduled jobs examples.
- Decide whether `/audit` should become a first-class operator audit page.
