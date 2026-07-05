# Operator Automation UX

Status: shipped (Phase 2), propose-don't-auto-create
Owner: `.claude/chat/` command registry + `.claude/scripts/orchestration/` automation modules
Last updated: 2026-07-04

## What It Does

Gives an operator a self-serve way to discover, parameterize, and create
scheduled automations without handing any agent a new auto-create surface. An
operator browses parameterized automation blueprints, receives a set of
curated starter suggestions out of the box, and accepts the ones they want into
real scheduled tasks. Every accept flows through the existing guarded
`/api/scheduled` path, so the bot-lifecycle guard runs server-side. A free,
zero-LLM `/recap` re-orients an operator juggling multiple threads.

## Operator Entry Points

| Command | What it does | Safety boundary |
|---|---|---|
| `/recap` | Zero-LLM recap of the current session: turn counts, tool histogram, recently touched files, last ask/last reply. | Read-only, local, instant. No tokens. |
| `/blueprints` | `list` / `<key>` / `<key> slot=val ...`. Lists the catalog, shows one blueprint's slots and a pre-filled command, or fills a blueprint. A fill proposes a pending suggestion. | Proposes only. A fill never schedules a task. |
| `/suggestions` | `list` / `accept <n>` / `dismiss <n>`. Lists pending proposals (seeds the starter catalog on first view), accepts one into a scheduled task, or dismisses one forever. | Accept is the only create path. Dismiss latches by dedup key. |

## Propose, Don't Auto-Create

Nothing here schedules a job without an explicit operator accept.

1. `/blueprints <key> slot=value ...` validates the values and registers a
   PENDING suggestion (`source="blueprint"`). Nothing is scheduled.
2. `/suggestions` lists pending proposals. On the first view of an empty store
   it seeds four curated starters (daily briefing, important-mail monitor,
   weekly review, vault sweep).
3. `/suggestions accept <n>` renders the suggestion's `job_spec` and POSTs it to
   `/api/scheduled`. Only a successful create makes the task real.
4. `/suggestions dismiss <n>` latches the suggestion's `dedup_key` forever, so
   the same proposal is never re-offered.

Catalog-sourced and blueprint-sourced suggestions both store the same
`/api/scheduled` create body (`persona_id`, `prompt`, `schedule`, `next_run`),
so accept passes the stored spec straight through with no re-conversion.

## Guard Refusal On Accept

The accept POST runs the Phase-1 bot-lifecycle guard server-side inside
`create_scheduled`:

- A prompt containing a bot-lifecycle command (launch, kill, or restart of
  `run_chat.sh` / `chat/main.py` / `thehomie`) returns HTTP 400. The handler
  surfaces the guard's verbatim reason as a friendly refusal string, never a
  500 or a stack trace. The suggestion stays PENDING (not latched accepted), so
  a corrected spec can be retried.
- An invalid cron returns HTTP 422 and a friendly "Invalid schedule" message.
- If the orchestration API is not running, the operator sees a friendly "start
  it with ..." message instead of a connection error.

## Source Of Truth Files

| Layer | Files |
|---|---|
| Blueprint catalog | `.claude/scripts/orchestration/blueprint_catalog.py` |
| Suggestions store | `.claude/scripts/orchestration/suggestions.py` |
| Starter catalog | `.claude/scripts/orchestration/suggestion_catalog.py` |
| Recap (zero-LLM) | `.claude/chat/recap.py` |
| Cross-process create client | `.claude/scripts/integrations/scheduled_api.py` |
| Router handlers | `.claude/chat/core_handlers.py` (`handle_recap`, `handle_blueprints`, `handle_suggestions`) |
| Command registry | `.claude/chat/commands.py` (COMMANDS, CATEGORIES, TELEGRAM_NATIVE_COMMANDS) |
| Server seam | `.claude/scripts/dashboard_api.py` (`create_scheduled`, `_validate_cron`, `_scan_scheduled_prompt`) |
| Bot-lifecycle guard | `.claude/scripts/orchestration/lifecycle_guard.py` |
| Tests | `.claude/scripts/tests/test_blueprint_catalog.py`, `test_suggestions.py`, `test_suggestion_catalog.py`, `test_recap.py`, `test_scheduled_api.py`, `test_automation_ux_commands.py`, `test_command_menu.py` |

## Safety Boundaries

- A blueprint fill only proposes. It never creates a scheduled task.
- Accept is the single create path, and it always POSTs through
  `/api/scheduled` so the server-side guard runs. The chat process never
  imports a local create path.
- A dismissed proposal never nags again (the `dedup_key` latches forever).
- The pending list is capped at five (`MAX_PENDING`); a full list drops new
  proposals until the operator clears the backlog.
- Every blueprint schedule and every catalog schedule resolves to a five-field
  cron expression, because `/api/scheduled` rejects anything else.
- `/recap` is read-only and local. It reads the current session's recent
  messages and computes a summary with no LLM call.

## Config And Env

| Var | Default | Purpose |
|---|---|---|
| `ORCHESTRATION_API_BASE_URL` | `http://127.0.0.1:4322` | Base URL for the create POST. Trailing slashes are stripped. |
| `ORCHESTRATION_API_TOKEN` | unset | Optional bearer token. Unset means loopback no-token mode. Must match the server when the server has a token set. |

The suggestions store is written to `config.STATE_DIR / "suggestions.json"`,
resolved at call time (persona-aware, install-dir-safe).

## How To Run It

```powershell
cd .claude/scripts
# The orchestration API must be up for accept to reach /api/scheduled:
uv run python -m orchestration.run_api
```

Chat examples:

```text
/recap
/blueprints
/blueprints morning-brief
/blueprints morning-brief time=07:30 deliver=origin
/suggestions
/suggestions accept 1
/suggestions dismiss 2
```

## How To Test It

```powershell
cd .claude/scripts
uv run pytest tests/test_blueprint_catalog.py tests/test_suggestions.py tests/test_suggestion_catalog.py tests/test_recap.py tests/test_scheduled_api.py tests/test_automation_ux_commands.py tests/test_command_menu.py -q
```

## Public Export Status

This feature page is public-framework safe. Public export must still go through
`scripts/sanitize.py`.

## Next Slices

- A dashboard form surface for blueprints (the catalog already emits
  `blueprint_form_schema`).
- Usage-sourced and integration-sourced suggestions (the store already accepts
  those sources).
