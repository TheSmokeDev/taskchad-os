# Telegram Command Menu

Status: shipped, shared native command registry with Telegram menu
Owner: `.claude/chat/` command registry and Telegram adapter
Last updated: 2026-07-12

## What It Does

Telegram shows a native slash-command dropdown. Homie keeps the full command
registry dispatchable, but exposes only a curated top-level menu so the visible
command list stays useful.

The curated list is also the shared baseline for native chat commands. Discord
inherits the same curated menu for its flat commands (minus flat `vault`) and
adds richer typed wrappers where the platform supports them, such as the
`/vault` group. Whatever lands in `TELEGRAM_NATIVE_COMMANDS` shows up on both
surfaces for free.

The curated menu currently exposes 57 commands (`TELEGRAM_NATIVE_COMMANDS`);
Discord renders 56 flat commands plus the typed `/vault` group.

## Drift-Proofing — Every Command Makes An Explicit Menu Decision

`COMMANDS` (the full registry) and `TELEGRAM_NATIVE_COMMANDS` (the visible menu)
are independent lists. Historically a new command could dispatch fine but never
autocomplete because the menu edit was forgotten (the `/video` failure). A name
with no registry description is silently dropped by `get_telegram_command_menu`.

`NATIVE_MENU_EXCLUDED` (in `commands.py`, right after the menu tuple) closes
this. Every registry command must be in **exactly one** of the two sets — shown
(`TELEGRAM_NATIVE_COMMANDS`) or deliberately hidden (`NATIVE_MENU_EXCLUDED`, each
name carrying a per-family reason comment). The CI test
`test_command_menu.py::test_every_command_makes_an_explicit_menu_decision`
asserts:

```text
set(COMMANDS names) == set(TELEGRAM_NATIVE_COMMANDS) | NATIVE_MENU_EXCLUDED
and the two sets are disjoint, and neither holds a name that isn't in COMMANDS
```

so a command that skips the menu decision fails the build instead of silently
never autocompleting.

### Native command checklist (updated)

A new native command still needs its four registrations:

1. a `COMMANDS` row;
2. membership in the right `CATEGORIES` group;
3. **add to `TELEGRAM_NATIVE_COMMANDS` (shown) OR to `NATIVE_MENU_EXCLUDED` with a
   reason (typed-only) — CI fails if it's in neither, and both if it's in both**;
4. a slashless handler in `CORE_HANDLERS` (for router-type commands).

`NATIVE_MENU_EXCLUDED` families today: mode toggles, raw engine integrations
(NL-first), hyphenated names (Telegram-illegal), content long-tail, the PIV
coding-session workflow, and engine-only dev tools.

## Operator Entry Points

- Telegram native menu: curated commands from `.claude/chat/commands.py`
- Chat command audit: `/commands native`, `/commands all`
- Full help: `/help`
- LinkedIn workshop: `/linkedin [cook <rough-idea>|run|cancel]`
- Video learning: `/watch <video-url> [question] [--detail smart|transcript|deep]`
- Shared vault surface: `/vault ...`

## Source Of Truth Files

| Layer | Files |
|---|---|
| Command registry | `.claude/chat/commands.py` |
| Router handlers | `.claude/chat/core_handlers.py`, `.claude/chat/router.py` |
| Telegram adapter | `.claude/chat/adapters/telegram.py` |
| Discord native wrappers | `.claude/chat/adapters/discord.py` |
| LinkedIn workshop | `.claude/chat/core_handlers.py`, `.claude/scripts/social/linkedin_workshop.py` |
| Tests | `.claude/scripts/tests/test_command_menu.py`, `.claude/scripts/tests/test_chat_router_timeout.py`, `.claude/scripts/tests/test_adapter_telegram.py`, `.claude/scripts/tests/test_adapter_discord.py` |

## Safety Boundaries

- Hidden commands still work when typed manually; the native menu is only the
  visible dropdown.
- `/linkedin` creates and revises queue drafts locally. Only its authenticated
  **Approve & Post** button may publish the exact displayed row through the
  existing gated executor.
- `/watch` keeps source media/transcripts in operational data and saves only a
  sourced, paraphrased dossier. Its Apply button creates a proposal; only a
  second exact-proposal approval may edit the local workspace.
- Browser execution remains under `/browserops`, `/browser`, and
  `/linkedin_profile` policy gates.
- Telegram's menu refreshes when the Telegram adapter reconnects and registers
  commands again.
- Native command names must be valid across shared surfaces. Hyphenated text
  aliases can stay registered, but should not be placed in the shared native
  menu.
- Slash commands are explicit-only. Pasted paths, URLs, and copied chat logs
  must not trigger commands unless the message starts with the exact slash
  command token.
- Follow-up nudges, including `/file` save prompts, are gated behind successful
  final-answer delivery. A nudge must not become the only visible reply for a
  turn.

## How To Run It

```powershell
cd .claude/scripts
uv run thehomie chat -q "/commands native" -Q
uv run thehomie chat -q "/commands all" -Q
uv run thehomie chat -q "/linkedin" -Q
```

Telegram examples:

```text
/commands native
/vault db thehomie
/vault search YourProduct --vault thehomie
/linkedin
/linkedin cook What I learned building multi-persona agents
/linkedin run
/watch https://youtu.be/example What should we apply? --detail smart
```

## How To Test It

```powershell
cd .claude/scripts
uv run pytest tests/test_command_menu.py tests/test_adapter_discord.py tests/test_adapter_telegram.py tests/test_skill_command_registration.py -q
uv run pytest tests/test_chat_router_timeout.py -q
# native-commands doctor check (registry count + live getMyCommands vs expected)
uv run thehomie doctor
```

## Current Local Proof

- Date: 2026-07-12
- Result: the curated menu contains 57 commands, including the native `/watch`
  video-learning lane. Menu math is clean — 90 registry commands = 57 shown +
  33 in `NATIVE_MENU_EXCLUDED`, no
  overlap, no unclassified names, no zombies. New guards green:
  `test_every_command_makes_an_explicit_menu_decision` (completeness) and the
  Telegram adapter `set_my_commands` wiring assertion.
- Expected after restart: the Telegram adapter will register 57 slash commands
  and Discord 56 flat + `/vault` group. (`thehomie doctor` reports the live
  Telegram count vs this expected — a mismatch means the bot hasn't restarted
  since the menu change.)
- Scope: local test proof plus the doctor registration check. Platform clients
  may still cache native command menus until their UI refreshes.

## Latest Live Proof

- Surface: Telegram `getMyCommands` (via `thehomie doctor`)
- Proof date: 2026-07-12
- Result: `thehomie doctor` reported `Telegram live: 57 (in sync)` after the
  bot refresh. Discord's 56-flat-plus-`/vault` registry math is locally proven;
  this page does not claim a separate Discord API enumeration.
- Delivery gate proof: a live Telegram answer rendered in Telegram Web and the
  bot log recorded final answer delivery before any follow-up delivery.

## Public Export Status

This feature page is public-framework safe. Public export must still go through
`scripts/sanitize.py`.
