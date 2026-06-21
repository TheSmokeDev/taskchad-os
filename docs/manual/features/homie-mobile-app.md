# Homie Mobile App

Status: shipped (v1 — Android local build; iOS via cloud build)
Owner: Homie Mobile (Capacitor) over the Python-owned dashboard API
Last updated: 2026-06-21

## What It Does

Homie Mobile is an installed phone app (iOS + Android) that puts the framework
dashboard — chat, Cabinet multi-persona rooms, the read-only browser viewer,
memory search — on a phone. It is a **Capacitor shell**: a thin native wrapper
whose WebView loads the existing dashboard over a mesh VPN (Tailscale) instead of
re-implementing the UI. On first launch a connect screen takes the dashboard host
plus an access token, stores them on-device, and loads the dashboard; later
launches reconnect automatically.

This is distinct from `dashboard-mobile-access` (which only surfaces the tailnet
URLs for a phone browser). Homie Mobile is the installable app around that same
dashboard. Because the WebView loads the live dashboard, dashboard UI changes
appear in the app on refresh — no app rebuild/reinstall for web-only changes.

Operators can rebrand the app (icon + display name) for their own deployment
without touching framework code.

## Operator Entry Points

- App: install on the phone, open, complete the one-time connect screen (host + token).
- Android: sideload the signed APK, or ship via Play internal testing.
- iOS: TestFlight, or sideload the unsigned `.ipa` with a free Apple ID.
- Backstop: the same dashboard is reachable from a phone browser via `/mobile`
  (see `dashboard-mobile-access`).

## Source Of Truth Files

The native app lives in a standalone Capacitor repo (out of tree). The
framework-side touchpoints that make it work:

| Layer | Files |
|---|---|
| Hono/dashboard server | `dashboard/server/src/middleware/csrf.ts` (mutation origin allowlist; honors `DASHBOARD_URL`) |
| Dashboard web | `dashboard/web/index.html` (`viewport-fit=cover`); `dashboard/web/src/styles/main.css` (`--safe-*` vars + `.topbar-safe`/`.composer-safe`/`.h-app` utilities); `dashboard/web/src/components/Sidebar.tsx`, `dashboard/web/src/pages/Cabinet.tsx` (responsive collapse) |
| Mobile app (out of tree) | Capacitor config (`server.allowNavigation`, `cleartext`); `www/` launcher connect screen; `android/` project; `.github/workflows/` cloud-Mac iOS build |
| Tests | `dashboard/web` + `dashboard/server` suites covering the safe-area and CSRF paths |

## Safety Boundaries

- Remote access requires the dashboard's own origin to be allow-listed via
  `DASHBOARD_URL`; otherwise mutating requests are rejected `403 cross-origin`
  by the CSRF middleware. Reads (GET) still succeed, so a misconfig looks
  "half-broken" (loads, but cannot send).
- Bearer-token auth; the token is entered once and kept in on-device storage —
  never committed and never shipped inside the build.
- The in-app browser viewer is read-only (same boundary as the dashboard
  `/browser` surface).
- The app holds no secrets of its own; it renders dashboard state and sends
  commands. Default transport is a mesh VPN, so the backend opens no inbound
  ports.

## How To Run It

Bring the dashboard up reachable on the mesh VPN, then point the app at it.

```powershell
cd <repo>\.claude\scripts
uv run python -m orchestration.run_api
```

```powershell
cd <repo>\dashboard\server
$env:DASHBOARD_BIND='<mesh-vpn-ip>'
$env:DASHBOARD_TOKEN='<your-token>'
$env:DASHBOARD_URL='http://<mesh-vpn-ip>:3141'
npm start
```

Build the app from the standalone Capacitor repo:

```powershell
# Android (local SDK; appId placeholder com.example.homie)
npm run build
npx cap sync android
cd android
.\gradlew assembleDebug   # use assembleRelease + a keystore for store/sideload

# iOS — no Mac required: a GitHub Actions macOS runner builds an unsigned .ipa
```

## How To Test It

```powershell
cd <repo>\dashboard\web
npm run build
npm run test
```

Device smoke: install the app, connect over the mesh VPN, send a chat message and
open a Cabinet room (confirm streaming), then background the app mid-stream and
confirm it resumes.

## Latest Live Proof

- Date: 2026-06-21
- Surface: Android debug APK, sideloaded, connected to the dashboard over a mesh VPN
- Result: dashboard rendered in the native WebView; chat and Cabinet streamed over
  SSE; safe-area insets cleared the status and navigation bars; an iOS `.ipa` was
  built on a cloud-Mac CI runner with no local Mac.

## Public Export Status

Public-safe operator manual exported through `scripts/sanitize.py`. The native app
repo, tokens, hostnames, and any deployment-specific branding stay out of tree.

## Next Slices

- Discord-style channel switcher (persona DMs + Cabinet rooms in one rail).
- Persist `DASHBOARD_URL` into the dashboard server env so remote chat survives a
  server restart.
- Native rebuild (SwiftUI / Jetpack Compose) — gated on the daily-drive feel of
  the Capacitor wrap.
