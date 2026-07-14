# Bot Autostart at Logon

Status: Active, Windows V1
Owner: runtime-chat
Last updated: 2026-07-14

## What It Does

An **opt-in** toggle that registers a Windows Task Scheduler task
(`SecondBrain-BotStart` by default) to start the chat bot at user logon. The
external watchdog only *recovers* a bot that was running and died; nothing
*starts* one after a reboot unless this toggle is on — without it, a reboot
leaves the bot down until an operator notices.

The toggle ships OFF (no task registered) — whether the bot auto-starts is the
operator's choice, per machine.

## Operator Entry Points

- Chat/Telegram/Discord: `/autostart` (status), `/autostart on`, `/autostart off`
- CLI: `thehomie autostart status [--json]`, `thehomie autostart on`,
  `thehomie autostart off`
- Dashboard: Settings page → "Startup" section → "Start bot at logon" checkbox
- API: `GET /api/autostart`, `POST /api/autostart {"enabled": true|false}`
  (admin route policy)

All four surfaces call the same three functions in
`.claude/scripts/autostart.py` — none carry their own Task Scheduler logic.

## How It Works

1. **Status is physical state.** `status()` runs
   `schtasks /query /tn <task>` and reads only the exit code (0 = registered).
   No config flag, no DB row — a task deleted by hand in the Task Scheduler
   GUI immediately reads as disabled, with no drift. (Existence, not the
   enabled/disabled sub-state: a task manually Disabled in the GUI still reads
   as on; `enable` always overwrites, so correctness never depends on it.)
2. **Enable always overwrites.** `enable()` runs a static PowerShell script:
   guarded `Unregister-ScheduledTask` then `Register-ScheduledTask` with an
   at-logon trigger (1-minute delay), `RunLevel Limited` (no elevation
   needed), and a **5-minute** execution time limit — the registered launcher
   (`run_bot_start.bat` → Git Bash `run_chat.sh`) exits in seconds after
   detaching the bot, so a wedged launcher gets reaped without touching the
   running bot. A stale task pointing at an old path is a non-case: on always
   replaces.
3. **Values travel as environment variables** (`HOMIE_AUTOSTART_TASK/_BAT/
   _WORKDIR`) — never interpolated into the PowerShell script text.
4. **Disable is idempotent.** Unregistering an absent task reports success
   ("task was not registered").
5. **Every mutation writes an audit row** (`autostart_enable` /
   `autostart_disable`, outcome succeeded/failed) to the dashboard `audit_log`,
   best-effort.
6. **Non-Windows** platforms report `supported: false` cleanly on every
   surface and never spawn a subprocess (V1).

## Watchdog vs. Autostart

| | Watchdog (`bot_watchdog.py`) | Autostart (this feature) |
|--|--|--|
| Job | RECOVER a bot that died while running | START a bot after logon/reboot |
| Trigger | `/health` polling every 5 min | Windows logon event |
| Default | On (scheduled task) | Off (opt-in toggle) |

Both are needed: a reboot with autostart off leaves the watchdog restarting a
bot that was never started — its restart budget is not a substitute for a
boot-time start.

## Config

| Knob | Default | Meaning |
|------|---------|---------|
| `BOT_AUTOSTART_TASK_NAME` | `SecondBrain-BotStart` | Task Scheduler task name |
| `BOT_AUTOSTART_TIMEOUT_SECONDS` | `60` | Subprocess timeout for schtasks/PowerShell |
| `HOMIE_KILLSWITCH_AUTOSTART` | (unset) | `disabled` blocks enable/disable (kill switch; status stays readable) |

Resolved at call time (Rule 1) via `config.get_bot_autostart_settings()`.

## Failure Modes

- PowerShell nonzero exit → `ok: false` with the stderr tail in `detail`;
  chat/CLI surface it verbatim; API returns 500.
- Kill switch set → chat replies "disabled by operator", CLI exits 1, API 503.
- Non-Windows → chat/CLI report unsupported (CLI `status` still exits 0), API
  returns 501 on POST.
- The launcher `.bat` missing → `enable` fails fast before touching Task
  Scheduler.

## Validation

- Unit: `.claude/scripts/tests/test_autostart.py` (15 tests — one per code
  path incl. error paths, subprocess boundary mocked).
- Live: `thehomie autostart status --json`, toggle off/on, verify with
  `schtasks /query /tn SecondBrain-BotStart`; audit rows in `dashboard.db`
  `audit_log`. Final proof is a reboot: bot up within ~2 minutes of logon.
