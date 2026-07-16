# Bot Lifecycle — One Switch, One Enforcer

Status: Active, Phase A (#117)
Owner: runtime-chat
Last updated: 2026-07-15

## What It Does

One flag answers "should the bot be running", and one process enforces it.

- **The switch** is `STATE_DIR/bot-desired-state.json`
  (`{"desired": "on"|"off", "changed_by": ..., "changed_at": ...}`), owned by
  `.claude/scripts/bot_lifecycle_switch.py`. It records operator INTENT —
  never a claim of actual state (Rule 2: the watchdog and `is_pid_alive`
  read physical state; this file only says what the operator wants).
- **The enforcer** is the external watchdog (`bot_watchdog.py`, scheduled
  every 5 minutes). `desired=on` (or a missing/corrupt flag) keeps the guard
  up: poll `/health`, count failures, restart through `run_chat.sh`.
  `desired=off` stands the watchdog down — no poll, no failure counting, no
  restart — with a `standing_down` receipt in its state file.

A missing or broken flag always reads as **on**. The guard can only be stood
down by an explicit, well-formed "off"; a corrupted flag can never silently
kill the watchdog (fail-open).

Before this, "turn the bot off" was a fight: kill the process and the
watchdog resurrected it 5 minutes later; disable the watchdog and nothing
guarded the bot when you turned it back on. Now every off-verb writes the
flag first and every recovery path reads it first.

## Operator Entry Points

| Surface | ON | OFF |
|---|---|---|
| CLI | `thehomie on` | `thehomie off` |
| Dashboard | Activate on the `default` agent (writes `desired=on`) | Deactivate on the `default` agent (writes `desired=off`) |
| Status | `thehomie status` shows `desired: on\|off` (JSON key `desired`) | same |

- `thehomie on` sets desired=on, then starts the bot if no live pid exists —
  through the SAME launcher path the watchdog uses (Git Bash + `run_chat.sh`,
  receipts to `watchdog-launcher.log`, `/health`-verified). One launcher path
  in the whole framework.
- `thehomie off` sets desired=off, then stops the running bot via the
  profile-aware `shared.cleanup_all_bot_processes()`. The flag lands BEFORE
  the kill sweep, so the watchdog stands down even if the sweep fails.
- Dashboard restart deliberately does NOT touch the flag — restarting is not
  a statement of intent. Persona (non-default) activate/deactivate never
  touches the flag either: personas run their own bots; the flag governs the
  active-profile bot the watchdog guards.
- Chat `/homie on|off` is a deferred fast-follow (Phase B).

Kill switch: `HOMIE_KILLSWITCH_BOT_LIFECYCLE=disabled` blocks `turn_on` /
`turn_off` (CLI exits 1). Reading the flag stays available. Every flag write
lands a best-effort audit row (`bot_desired_on` / `bot_desired_off`).

## The 4-Rung Liveness Ladder

"Is the bot up?" has four different answers, each with its own check and its
own failure class. A rung can be green while the one above it is dead.

| Rung | Question | Checked by |
|---|---|---|
| 1. Process exists | Is there a live PID? | `bot.pid` + `shared.is_pid_alive` (watchdog: `/health` unreachable = process-level failure) |
| 2. Adapter probed | Does each adapter's physical state probe pass? | In-bot `LivenessSupervisor` (`chat/liveness.py`) → `/health` `adapter_liveness.healthy` |
| 3. Events flowing | Has the adapter actually RECEIVED anything recently? | Watchdog event-staleness check: `adapter_liveness.last_update_at` vs `BOT_WATCHDOG_STALENESS_SECONDS`, judged against a fresh peer |
| 4. End-to-end reply | Does a real message get a real answer? | Operator / `telegram-bot-test` skill — no automated rung yet |

The staleness rule (rung 3) is deliberately comparative: a critical adapter
whose `last_update_at` is older than the threshold is DEGRADED only when at
least one OTHER adapter is fresh — the fresh peer proves the bot and the
world are active, so the quiet one is broken, not idle. Both-quiet is NOT
stale (a quiet bot is not a dead bot), and a missing or unparseable
timestamp never produces a verdict (fail-safe).

## The 2026-07 Wedge-Class Catalog

Four distinct ways this bot has been "up" while being down, and which layer
catches each one now:

| Class | What happened | Rung it broke | Caught now by |
|---|---|---|---|
| Stale zombie (wedged ~05-31, found 07-12) | Process alive, pid file present, `/health` said `telegram: true` — Telegram polling dead for ~6 weeks. `/health` reported registration presence, not liveness; nothing external polled it. | 2 (probe lied) | In-bot `LivenessSupervisor` physical probes + the external watchdog polling `/health` every 5 min |
| Event-loop wedge (07-13) | A synchronous 60-120s browser drive inside an async handler froze the whole loop — Telegram, Discord, `/health`, and the in-process supervisor all shared it. The smoke detector was in the burning room. | 1-2 (everything in-process) | External watchdog (`/health` unreachable → restart); the framework-wide rule that the bot never drives a browser on its event loop (detached Browser Homie runner) |
| Launcher WSL trap (07-14) | Every watchdog restart silently failed all morning: Task Scheduler PATH resolved `bash` to WSL's System32 shim, which mangles Windows script paths. DEVNULL swallowed the launcher's own error output. | recovery path itself | `_find_bash()` prefers Git Bash and refuses the System32 shim; launcher output goes to `watchdog-launcher.log` (append + rotation, never truncated); bash missing = FAIL LOUD (toast + `RESTART ABORTED` receipt), never a fallback |
| Discord event-staleness (07-15) | Discord gateway task alive and `is_ready()` true — probe green — while the event stream was dead for hours and Telegram stayed busy. | 3 (probe green, events dead) | Watchdog event-staleness branch: critical adapter stale past `BOT_WATCHDOG_STALENESS_SECONDS` while a peer is fresh → DEGRADED → restart |

Retired with this slice (archived to `.claude/_archive/lifecycle-2026-07/`):

- `service.py` + `run_service.bat` + `setup_bot_scheduler.ps1` — the
  crash-only supervisor lane. A wedged process never exits, so a supervisor
  that waits on `proc.wait()` never fires; its "bot down" alert also imported
  a symbol that never existed, so it had never once alerted.
- `run_chat.bat` — hardcoded `--telegram`; every "recovery" through it
  resurrected a Telegram-only bot with no Discord and no relay.
  `run_chat.sh` (Git Bash on Windows) is the only launcher.

## Source Of Truth Files

| Layer | Files |
|---|---|
| Switch | `.claude/scripts/bot_lifecycle_switch.py` |
| Enforcer | `.claude/scripts/bot_watchdog.py` (`run_once` gate, `classify` staleness, `restart_bot` receipts) |
| CLI verbs | `.claude/chat/cli.py` (`on`, `off`, `status` desired line) |
| Dashboard flag writes | `.claude/scripts/dashboard_api.py` (`activate_agent` / `deactivate_agent`, default persona only) |
| Config | `.claude/scripts/config.py` (`get_bot_watchdog_settings`) |
| Tests | `.claude/scripts/tests/test_bot_lifecycle_switch.py`, `test_bot_watchdog.py`, `test_cli.py` |

## Config

| Knob | Default | Meaning |
|------|---------|---------|
| `BOT_WATCHDOG_STALENESS_SECONDS` | `7200` | Age of `last_update_at` past which a critical adapter counts as event-stale (needs a fresh peer to fire) |
| `HOMIE_KILLSWITCH_BOT_LIFECYCLE` | (unset) | `disabled` blocks `turn_on`/`turn_off`; `get_desired` stays readable |
| `BOT_WATCHDOG_*` | see [bot-autostart](bot-autostart.md) + `config.get_bot_watchdog_settings` | Existing watchdog knobs, unchanged |

All resolved at call time (Rule 1).

## Failure Modes

- Flag file missing/corrupt/unknown value → reads as `on`; the guard stays up.
- `bot_lifecycle_switch` import or read crashes inside the watchdog → logged,
  guard proceeds as `on` (a broken switch must never kill the guard).
- Git Bash missing at restart time → no restart attempt, operator toast
  ("restart BLOCKED"), `RESTART ABORTED` line in `watchdog-launcher.log`,
  non-OK watchdog exit. Install Git for Windows; there is no fallback.
- `turn_off` kill sweep fails → `ok: false` with the error, but desired=off
  already landed — the watchdog will not resurrect the half-dead bot.
- Dashboard flag write fails → logged warning; the activate/deactivate
  operation itself still succeeds (flag is best-effort on that surface).

## Validation

```powershell
cd .claude/scripts
uv run python -m pytest tests/test_bot_watchdog.py tests/test_bot_lifecycle_switch.py tests/test_cli.py -q
```

Live: `thehomie off` → confirm the bot dies AND the next watchdog tick logs
`standing down` with `last_verdict: standing_down` in
`.claude/data/state/bot-watchdog-state.json`; `thehomie on` → bot back with
`/health` green and `desired: on` in `thehomie status`.

## Related Pages

- [bot-autostart](bot-autostart.md) — the at-logon START (this page owns
  intent + recovery; autostart owns the reboot case)
- [bot-self-restart](bot-self-restart.md) — the in-chat `/restart` verb
  (leaves desired state untouched, like the dashboard restart)
