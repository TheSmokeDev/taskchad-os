# Orchestration Resilience

Status: shipped (Phase 1)
Owner: `.claude/scripts/orchestration/`
Last updated: 2026-07-04

## What It Does

Three self-healing guards keep the bot's fan-out delivery and process
lifecycle stable under agent-driven load. Delivery to a confirmed-dead chat is
skipped and self-heals the moment a send succeeds again; a runaway respawn loop
is broken after a few rapid restarts while the bot keeps serving new messages;
and a scheduled/fan-out job that would launch, kill, or restart the bot is
refused at creation time. All three engage automatically (on boot, on delivery,
on job creation), never send or mutate anything themselves, and fail open — a
broken guard never wedges a healthy bot.

## Operator Entry Points

These guards are internal and automatic — there is no command to start them.
What an operator observes are the symptoms:

| Symptom | Which guard | What you see |
|---|---|---|
| A deleted group / blocked-bot chat stops receiving fan-out turns | Dead-Target Registry | Log line `dead_targets: marked <platform>:<chat_id> as unreachable`; deliveries to that target are skipped until one succeeds. Read-only skip. |
| Recovery is silent and automatic | Dead-Target Registry | Log line `dead_targets: cleared <platform>:<chat_id>` on the next successful send; no manual cleanup. Self-heals. |
| Boot stops replaying cognitive state after repeated crashes | Restart-Loop Breaker | `[WARNING] Restart-loop breaker TRIPPED — skipping boot-time state restore`. The bot still starts and serves inbound messages. Refuses to replay, does not stop the bot. |
| A scheduled/convoy job that would restart the bot is rejected | Cron Lifecycle Guard | HTTP 400 with a friendly "Blocked: job contains a bot lifecycle command" reason instead of a fire-time crash. Refuses at create, no state written. |

## The Three Guards

### Dead-Target Registry

`DeadTargetRegistry` (in `dead_targets.py`) is a thread-safe, persistent set of
delivery targets confirmed permanently unreachable, keyed `platform:chat_id`.

- **Trigger to mark:** a fan-out send raises an exception that
  `classify_send_error(exc)` resolves to a permanent whole-chat death. That
  classifier walks the `__cause__` / `__context__` chain (provider exception
  types first, then a string fallback) and returns only `"forbidden"` or
  `"not_found"` — the two kinds in `_DEAD_ERROR_KINDS`. `not_found` is narrowed
  to whole-chat phrases (`chat not found`, `channel not found`,
  `unknown channel`); a deleted topic, thread, or missing message is NOT
  recorded. On a match the delivery layer calls `mark_dead(...)`.
- **Trigger to self-heal:** ANY successful send calls `clear(...)`, removing the
  flag. A user who re-adds the bot or restores the chat recovers automatically.
- **Skip:** before delivering, callers check `is_dead(...)` and short-circuit a
  proven-dead target, sparing a wasted attempt against the platform's
  flood-control envelope.
- **Fail-open contract:** state is a small JSON file under `config.STATE_DIR`
  (`dead_targets.json`, atomic tmp + replace). A corrupt or unwritable file
  degrades to an in-memory-only registry (`OSError` / `ValueError` swallowed) —
  it never raises on the delivery path.

### Restart-Loop Circuit Breaker

`restart_loop_guard.py` is the last-resort breaker for a tight respawn cycle,
where boot-time state restore replays the same fatal path and a supervisor keeps
reviving the process every few seconds.

- **Trigger:** each boot that killed a live/stale predecessor calls
  `check_and_record()`, which appends this boot's timestamp to a rolling window
  persisted across processes (`restart_loop.json`) and reports whether the loop
  is now tripped.
- **Behavior when tripped:** after `DEFAULT_MAX_RESTARTS` (3) restart-interrupted
  boots inside `DEFAULT_WINDOW_SECONDS` (60), the caller SKIPS boot-time state
  restore for that boot — the bot still starts and serves real inbound messages,
  it just stops replaying the state that keeps killing it, breaking the cycle and
  putting a human back in the loop. `clear()` wipes the log on clean shutdown.
- **Fail-open contract:** any read/write failure fails OPEN (no false trip). If
  the boot could not be durably persisted, `check_and_record()` returns `False`
  and logs a warning — a breaker that cannot trust its own count never trips.
  Thresholds are code constants (3 restarts / 60s), not env-tunable.

### Cron Lifecycle Guard

`lifecycle_guard.py` rejects a job whose free text would launch, kill, or restart
the bot, at job-creation time.

- **Trigger:** `check_bot_lifecycle(prompt, script=None)` scans the job's prompt
  (and an optional script file) with `_BOT_LIFECYCLE_PATTERN`. A match raises
  `BotLifecycleBlocked` (a `ValueError` subclass).
- **Pattern discipline:** every branch is anchored on a concrete, bot-SPECIFIC
  two-token command shape — the launcher `run_chat.sh`, a launch verb plus the
  repo-qualified `chat/main.py` path, or a kill/service verb plus a bot token
  (`run_chat` / `thehomie` / `chat/main.py`). It NEVER matches bare `python` /
  `main.py` / `bot`, and the POSIX-kill branch requires a LEFT word boundary so
  ordinary words that merely END in "kill" (upskill, reskill, roadkill) never
  read as the `kill` command.
- **Fail-open contract:** the guard import lives inside the calling wrapper, so
  an import fault also fails open — a legit job always creates. Only a genuine
  `BotLifecycleBlocked` propagates: the convoy API's global
  `ValueError`→HTTP 400 mapper surfaces it as a create failure, and the
  dashboard `/api/scheduled` seam (which has no such mapper) translates it to
  `HTTPException(400)` itself. It never mutates state.

## Source Of Truth Files

| Layer | Files |
|---|---|
| Guard modules | `.claude/scripts/orchestration/dead_targets.py`, `.claude/scripts/orchestration/restart_loop_guard.py`, `.claude/scripts/orchestration/lifecycle_guard.py` |
| Boot breaker wiring | `.claude/chat/main.py` (`check_and_record()` at boot, `restart_loop_guard.clear()` on shutdown) |
| Dead-target delivery wiring | `.claude/chat/cabinet_relay.py`, `.claude/chat/adapters/webhook.py` (mark/clear/skip); `.claude/chat/adapters/telegram.py` (thread-level reply-target self-heal by retrying without `reply_to`) |
| Lifecycle-guard seams | `.claude/scripts/orchestration/convoy_service.py` (`_scan_bot_lifecycle` at convoy create), `.claude/scripts/dashboard_api.py` (`check_bot_lifecycle` at scheduled-job create) |
| Tests | `.claude/scripts/tests/test_dead_targets.py`, `.claude/scripts/tests/test_restart_loop_guard.py`, `.claude/scripts/tests/test_lifecycle_guard.py` |

## Safety Boundaries

- Every guard fails open. A corrupt, unwritable, or unreadable state file
  degrades gracefully (in-memory for dead targets; no-trip for the breaker) and
  never raises on the hot path.
- The guards only refuse or skip — they never send, post, or mutate an external
  account, and the lifecycle guard never mutates state (it is a creation-time
  refusal only).
- Restart-loop thresholds are code constants (3 restarts / 60s), not env knobs.
- Dead-target skip is whole-chat only. Thread/topic/message-level failures are
  NOT recorded — the adapters already self-heal those by retrying without the
  reply target.
- State files live under `config.STATE_DIR` (`dead_targets.json`,
  `restart_loop.json`), resolved at call time (persona-aware, install-dir-safe).

## How To Run It

```powershell
# There is no command to "start" these guards — they engage automatically:
#   - the restart-loop breaker records each boot in .claude/chat/main.py
#   - the dead-target registry marks/clears on every fan-out delivery
#   - the lifecycle guard runs server-side inside convoy + /api/scheduled create
#
# The orchestration API only needs to be up for the lifecycle guard's
# scheduled-job seam to run:
cd .claude/scripts
uv run python -m orchestration.run_api
```

## How To Test It

```powershell
cd .claude/scripts
uv run pytest tests/test_dead_targets.py tests/test_restart_loop_guard.py tests/test_lifecycle_guard.py -q
```

## Latest Live Proof

- Date: 2026-07-04
- Surface: orchestration resilience guards (dead targets + restart-loop breaker + cron lifecycle guard)
- Result: 103 tests green across the three guard suites (Phase 1 also added wiring-site coverage in `test_cabinet_relay.py` and `test_dashboard_api.py`)
- Proof: commit `659c4a49`

## Public Export Status

This feature page is public-framework safe. Public export must still go through
`scripts/sanitize.py`.

## Next Slices

- A `/diagnostics` surface for the current dead-target set (`all_dead()` already
  returns a snapshot).
- Wire the dead-target registry into the remaining fan-out adapters beyond
  cabinet relay and webhook.
- Extend the lifecycle-guard scan to the dormant scheduled-job `script` seam
  (`_resolve_script_path` / `_read_script_for_scanning` already exist for it).

Cross-link: `automation-ux.md` surfaces the cron lifecycle guard from the
operator side — as the scheduled-job accept-path refusal in its "Guard Refusal
On Accept" section. THIS page is the canonical guard contract.
