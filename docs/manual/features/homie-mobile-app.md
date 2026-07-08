# Homie Mobile App

Status: shipped (v2 native — Expo/React Native, M0–M12 + PhoneOps P3.0, live-proven on device)
Owner: `mobile/` in-tree Expo app over the Hono dashboard proxy
Last updated: 2026-07-06

> Deep manual: [The Homie Mobile Manual](../../homie-mobile-manual.md) —
> architecture, every screen, PhoneOps, safety model, failure modes, and the
> validation map. This page is the short operator summary.

## What It Does

Homie Mobile is the native phone cockpit for the framework: streaming chat
with live tool-call detail, per-message model/effort picks, stop/steer,
camera and voice as agent senses, every persona, the Cabinet War Room,
sessions with full-text search, skills/files/tasks/memory/usage windows, and
a Browser screen that watches AND drives the desktop's Chrome — plus a
Desktop | Phone toggle (PhoneOps P3.0) that drives the phone's own Chrome
through the same gated surface.

It is a **thin native client**: Expo (runs in Expo Go for all of v1),
TypeScript strict, expo-router. It talks only to the Hono proxy (`:3141`)
with a Bearer header on every call including SSE — never Python `:4322`
directly, never SQLite, zero business logic in the app.

The v1 Capacitor WebView shell this page previously described is retired;
the native app replaced it (M0–M4 shipped 2026-07-04/05, phone-verified).

## Operator Entry Points

- Phone: Expo Go + the Metro dev server for v1 (`cd mobile && npx expo start`).
- Pairing: scan the operator-minted QR (`POST /api/pair/start`), then approve
  the pending device — default-deny with audit rows.
- Backstop: the same dashboard is reachable from a phone browser via
  `/mobile` (see `dashboard-mobile-access`).

## Source Of Truth Files

| Layer | Files |
|---|---|
| App | `mobile/src/app/` (screens), `mobile/src/api/` (client, sse, browser-stream), `mobile/src/ui/` (tokens/icons/chrome/markdown/tools/palette), `mobile/src/state/connection.tsx` |
| Hono proxy | `dashboard/server/src/routes/conversation.ts`, `sessions.ts`, `browser-viewer.ts` |
| Framework API | `.claude/scripts/dashboard_api.py` (send/stream/stop/steer, sessions, library, browser-viewer + target gate, pairing) |
| Phone transport | `.claude/chat/adb_control.py`, `.claude/chat/browser_control.py` (target registry) |
| Docs | `mobile/SPEC.md` (HOW), `mobile/AGENTS.md` (conventions), `mobile/PROGRESS.md` (evidence), [deep manual](../../homie-mobile-manual.md) |

## Safety Boundaries

- Bearer credential in `expo-secure-store`; headers everywhere (no
  token-in-URL, including SSE).
- Pairing, camera, mic: default-deny, per-use consent, audit rows.
- Browser actions ride registered workflow gates with shape validation and
  audit rows; the live viewport stream is bearer-auth relayed (the raw
  agent-browser WebSocket stays loopback-only).
- PhoneOps: `HOMIE_PHONEOPS_ENABLED` default OFF (phone target → 403); the
  target is a server-resolved enum — a raw CDP port never comes from a
  client; the adb forward exposes the phone's personal Chrome profile to
  local PC processes, so keep the switch off when not in use.
- Files surface is read-only over allowlisted roots; command palette drafts
  writes instead of firing them.

## How To Run It

```powershell
cd <repo>\.claude\scripts
uv run python -m orchestration.run_api        # Python API :4322

cd <repo>\dashboard\server
$env:DASHBOARD_BIND='0.0.0.0'                 # or mesh-VPN IP
$env:DASHBOARD_TOKEN='<your-token>'
npm start                                     # Hono proxy :3141

cd <repo>\mobile
npx expo start                                # Metro for Expo Go
```

PhoneOps additionally needs the one-time adb pairing (USB → `adb tcpip 5555`
→ `adb connect <phone-ip>:5555`) and `HOMIE_PHONEOPS_ENABLED=true` — full
runbook in the deep manual §7.

## How To Test It

```powershell
cd <repo>\.claude\scripts
uv run python -m pytest tests/test_agent_browser_framework.py tests/test_dashboard_api.py tests/test_tenant_route_policy.py -q

cd <repo>\dashboard\server
npm run typecheck; npm test

cd <repo>\mobile
npx tsc --noEmit; npx expo lint
```

Device smoke: pair → chat streams with tool cards → stop/steer → Browser
desktop watch/drive → toggle Phone → status/screenshot/act against the real
phone → back to Desktop unchanged → audit rows carry the targets.

## Latest Live Proof

- Date: 2026-07-05/06
- Surface: real Android device over Expo Go + wireless adb, full
  Hono→Python→adb stack
- Result: the complete cockpit verified on-device (M0–M12); PhoneOps P3.0
  drove the phone's own Chrome live — status/act/elements/screenshot/
  navigate/stream, desktop parity in the same session, audit rows verified;
  the freezer spike set the act-path policy (acts foreground Chrome; reads
  never hijack). Route-count lock unchanged at 137.

## Public Export Status

Public-safe. Exports through `scripts/sanitize.py`; tokens, device serials,
host IPs, and deployment branding stay out of tree.

## Next Slices

- P3.1+: phone read layer, agent-device app/OS control, scrcpy mirror —
  same adb transport, explicitly out of P3.0 scope.
- iOS build target; store packaging deferred until the daily-drive feel is
  locked.
