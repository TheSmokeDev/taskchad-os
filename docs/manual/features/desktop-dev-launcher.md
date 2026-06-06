# Desktop Dev Launcher

Status: Windows-first dev/operator launcher implemented
Owner: CLI/runtime
Last updated: 2026-06-04

## What It Does

`thehomie desktop` starts the local operator stack for the dashboard-first
desktop experience: Python orchestration API, Hono dashboard server, and the
Vite web dashboard. The default target opens Operating Room at `/teams`.

For the Electron dashboard app/process-manager path, use
[Desktop v0](desktop-v0.md). This page documents the browser/Vite dev launcher.

## Operator Entry Points

- CLI: `thehomie desktop`
- Electron dashboard mode: `thehomie desktop --shell`
- CLI dry run: `thehomie desktop --dry-run --json`
- Dashboard target: `/teams`

## Source Of Truth Files

| Layer | Files |
|---|---|
| Python/runtime | `.claude/chat/desktop_launcher.py` |
| Chat/router | `.claude/chat/cli.py` |
| Hono/dashboard server | `dashboard/server/src/index.ts` |
| Dashboard web | `dashboard/web/vite.config.ts` |
| Electron dashboard app | `dashboard/desktop/` |
| Tests | `.claude/scripts/tests/test_cli.py` |
| Docs/proof | this page |

## Safety Boundaries

- The launcher is local-loopback by default.
- It sets dashboard dev no-auth only for loopback local development.
- It does not edit secrets or write raw `.env` values.
- It starts existing source-of-truth processes; it does not move
  orchestration logic into Electron, Hono, or the dashboard.

## How To Run It

```powershell
cd .claude\scripts
uv run thehomie desktop
```

Dry run:

```powershell
uv run thehomie desktop --dry-run --json
```

Useful flags:

- `--api-port`
- `--dashboard-port`
- `--web-port`
- `--no-open`
- `--no-vite`
- `--shell`

## How To Test It

```powershell
cd .claude\scripts
uv run pytest tests/test_cli.py::TestCLIHelp::test_desktop_dry_run_shows_local_stack -q
```

## Latest Proof

- Date: 2026-06-04
- Surface: CLI dry-run test
- Result: dry run reports `python-api`, `hono-dashboard`, `vite-web`, and the
  default Operating Room URL.

## Related Handoffs

- Private proof handoffs stay outside `docs/manual`.

## Public Export Status

Public-export eligible through `scripts/sanitize.py`. Export must be run before
any public push.

## Next Slices

- Per-user or signed installer distribution for Desktop v0.
- Desktop icon and artifact naming polish.
