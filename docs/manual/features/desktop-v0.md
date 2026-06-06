# Desktop v0

Status: Windows-first dashboard app, unpacked package, and portable artifact smoke-proven
Owner: Desktop app + dashboard server
Last updated: 2026-06-05

## What It Does

Desktop v0 wraps the existing Homie local stack in Electron. It is not a new
orchestration engine. It starts and stops the Python orchestration API and the
Hono dashboard server, waits for both to be healthy, then loads the Homie
dashboard inside the same Electron window.

The dashboard is the product surface. The old local-control shell remains only
as a fallback/maintenance screen if the dashboard cannot boot.

## Operator Entry Points

- CLI shell mode: `thehomie desktop --shell`
- Shell package: `dashboard/desktop`
- Static dashboard target: `http://127.0.0.1:3141/`
- Browser/Vite dev fallback: `thehomie desktop`

## Source Of Truth Files

| Layer | Files |
|---|---|
| Electron main/preload | `dashboard/desktop/main.cjs`, `dashboard/desktop/preload.cjs` |
| Desktop process manager | `dashboard/desktop/lib/process-manager.cjs` |
| Desktop config | `dashboard/desktop/lib/config-store.cjs` |
| Desktop packaging | `dashboard/desktop/electron-builder.cjs`, `dashboard/desktop/scripts/packaged-smoke.mjs`, `dashboard/desktop/scripts/portable-smoke.mjs` |
| Desktop controls | `dashboard/web/src/components/DesktopControls.tsx`, `dashboard/desktop/renderer/` fallback shell |
| Hono static dashboard serving | `dashboard/server/src/static-web.ts` |
| CLI entrypoint | `.claude/chat/desktop_launcher.py`, `.claude/chat/cli.py` |
| Tests | `dashboard/desktop/tests/process-manager.test.mjs`, `dashboard/server/src/__tests__/static-web.test.ts`, `.claude/scripts/tests/test_cli.py` |

## Safety Boundaries

- Python orchestration remains the source of truth for Operating Room behavior.
- Electron only owns local process lifecycle, first-run config, logs, status,
  and loading the dashboard URL in-window.
- The shell stores only local desktop config: ports, bind host, start path, and
  auto-start preference.
- It does not write raw `.env` values, expose secrets, or bypass the
  default-deny tool/runtime policy.
- Dashboard no-auth mode is only set for loopback local development.

## How To Run It

Build the dashboard web assets first:

```powershell
npm --prefix dashboard/web run build
```

Desktop v0 packaging uses the supported Electron toolchain and requires
Node.js 22.12+.

Install the desktop package dependencies:

```powershell
npm --prefix dashboard/desktop install
```

Build the no-admin unpacked Windows package:

```powershell
npm --prefix dashboard/desktop run package:win
```

Build the no-admin portable Windows artifact:

```powershell
npm --prefix dashboard/desktop run package:win:portable
```

Launch through the Homie CLI:

```powershell
cd .claude\scripts
uv run thehomie desktop --shell
```

Useful dry run:

```powershell
uv run thehomie desktop --shell --dry-run --json
```

## What Desktop Shows

- The normal Homie dashboard as the first product surface.
- A compact `Desktop Stack` strip, visible only inside Electron, with
  start/stop/refresh controls, target URL, service status, ports, and recent
  logs.
- Fallback first-run config for API port, dashboard port, bind host, start
  path, and auto-start if the dashboard cannot boot.

## How To Test It

```powershell
npm --prefix dashboard/desktop test
npm --prefix dashboard/desktop run smoke
npm --prefix dashboard/desktop run smoke:electron
npm --prefix dashboard/desktop run package:win
npm --prefix dashboard/desktop run smoke:package
npm --prefix dashboard/desktop run package:win:portable
npm --prefix dashboard/desktop run smoke:portable
npm --prefix dashboard/desktop audit --audit-level=high
npm --prefix dashboard/server test -- static-web.test.ts
cd .claude\scripts
uv run pytest tests/test_cli.py::TestCLIHelp::test_desktop_shell_dry_run_shows_electron_entrypoint -q
```

## Latest Proof

- Date: 2026-06-05
- Dashboard-first portable Windows artifact smoke: passed on alternate ports
  `45136/33154`
  - portable artifact built
    `dashboard/desktop/dist/The-Homie-Desktop-0.1.0-x64.exe`
  - artifact size: `96145321` bytes
  - Electron renderer loaded `The Homie Dashboard`, not the standalone shell
  - renderer reported `isPackaged=true`, `artifactKind=portable`, dashboard
    root, Desktop IPC bridge, embedded `Desktop Stack` controls, and Mission
    Control content
  - portable app used bundled static assets from extracted
    `resources/dashboard-web`
  - in-window route checks passed for `/mission`, `/chat`, `/mobile`,
    `/browser`, `/work`, `/convoy`, and `/teams`
  - `/work` and `/convoy` route probes reported `hasRawFetchError=false`
  - direct Python `/api/health` returned 200 from `45136`
  - Hono `/api/health` returned 200 from `33154`
  - shell reported `python-api` PID `59164` and `hono-dashboard` PID `28636`
  - shell stopped both services and ports `45136/33154` were closed after
    smoke
  - original services remained running on `4322`, `3141`, `5173`, and `7860`
- Private portable smoke report:
  `.codex/artifacts/desktop-v0-portable-smoke/report.json`
- Date: 2026-06-05
- Dashboard-first unpacked Windows package smoke: passed on alternate ports
  `45135/33153`
  - package built `dashboard/desktop/dist/win-unpacked/The Homie Desktop.exe`
  - Electron renderer loaded `The Homie Dashboard`, not the standalone shell
  - renderer reported dashboard root, Desktop IPC bridge, embedded
    `Desktop Stack` controls, and Mission Control content
  - in-window route checks passed for `/mission`, `/chat`, `/mobile`,
    `/browser`, `/work`, `/convoy`, and `/teams`
  - direct Python `/api/health` returned 200 from `45135`
  - Hono `/api/health` returned 200 from `33153`
  - Hono `/api/agents` returned 200 through the Python API after readiness gate
  - shell stopped both services and ports `45135/33153` were closed after smoke
- Date: 2026-06-04
- Unpacked Windows package smoke: passed on alternate ports `45124/33142`
  - package built `dashboard/desktop/dist/win-unpacked/The Homie Desktop.exe`
  - renderer reported `isPackaged=true`
  - packaged shell used bundled static assets from `resources/dashboard-web`
  - renderer showed Start, Stop, dashboard-open action, status, and logs
  - shell reported `python-api` PID `21860` and `hono-dashboard` PID `25272`
  - `/teams` returned 200 from Hono/static
  - direct Python `/api/health` returned 200 from `45124`
  - Hono `/api/health` returned 200 from `33142`
  - shell stopped both services and ports `45124/33142` were closed after
    smoke
- Private package smoke report:
  `.codex/artifacts/desktop-v0-package-smoke/report.json`
- Real Electron smoke: passed on alternate ports `45123/33141`
  - renderer showed Start, Stop, dashboard-open action, status, and logs
  - shell reported `python-api` PID `44056` and `hono-dashboard` PID `54532`
  - `/teams` returned 200 from Hono/static
  - direct Python `/api/health` returned 200 from `45123`
  - Hono `/api/health` returned 200 from `33141`
  - shell stopped both services and alternate ports were closed after smoke
- Private smoke report:
  `.codex/artifacts/desktop-v0-electron-smoke/report.json`
- Desktop unit tests: 11 passed
- Desktop smoke: reports `python-api`, `hono-dashboard`, and
  `http://127.0.0.1:3141/teams`
- Desktop package audit: 0 high-severity vulnerabilities
- Hono focused tests: 7 passed
- CLI shell dry-run tests: 2 passed
- Hono typecheck, Python compile, dashboard web build, sanitizer tests, and
  public export passed

## Public Export Status

Public export passed after the portable smoke. Desktop source and packaging
config ship; `dashboard/desktop/node_modules/`, `dashboard/desktop/dist/`,
`dashboard/desktop/out/`, and private `.codex` proof artifacts are denied.

## Next Slices

- Per-user installer distribution. The current proof is unpacked and portable
  no-admin Windows artifacts, not a signed installer.
- Desktop icon and artifact naming polish.
