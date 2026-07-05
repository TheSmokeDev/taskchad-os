# Telegram Command Menu

Status: shipped, shared native command registry with Telegram menu
Owner: `.claude/chat/` command registry and Telegram adapter
Last updated: 2026-06-27

## What It Does

Telegram shows a native slash-command dropdown. Homie keeps the full command
registry dispatchable, but exposes only a curated top-level menu so the visible
command list stays useful.

The curated list is also the shared baseline for native chat commands. Discord
uses the same baseline for flat commands and adds richer typed wrappers where
the platform supports them, such as the `/vault` group.

## Operator Entry Points

- Telegram native menu: curated commands from `.claude/chat/commands.py`
- Chat command audit: `/commands native`, `/commands all`
- Full help: `/help`
- LinkedIn/Social Homie: `/linkedin [draft|ideas|revise] <topic-or-text>`
- Shared vault surface: `/vault ...`

## Source Of Truth Files

| Layer | Files |
|---|---|
| Command registry | `.claude/chat/commands.py` |
| Router handlers | `.claude/chat/core_handlers.py`, `.claude/chat/router.py` |
| Telegram adapter | `.claude/chat/adapters/telegram.py` |
| Discord native wrappers | `.claude/chat/adapters/discord.py` |
| LinkedIn prompt | `.claude/commands/linkedin.md` |
| Tests | `.claude/scripts/tests/test_command_menu.py`, `.claude/scripts/tests/test_chat_router_timeout.py`, `.claude/scripts/tests/test_adapter_telegram.py`, `.claude/scripts/tests/test_adapter_discord.py` |

## Safety Boundaries

- Hidden commands still work when typed manually; the native menu is only the
  visible dropdown.
- `/linkedin` is draft-only. It can create ideas, drafts, and revisions, but it
  must not publish, DM, edit profiles, connect, scrape, or open/control a
  browser.
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
uv run thehomie chat -q "/linkedin draft a post about multi-persona AI operators" -Q
```

Telegram examples:

```text
/commands native
/vault db thehomie
/vault search YourProduct --vault thehomie
/linkedin ideas AI operator systems
/linkedin draft What I learned building multi-persona agents
/linkedin revise <paste draft>
```

## How To Test It

```powershell
cd .claude/scripts
uv run pytest tests/test_command_menu.py tests/test_adapter_discord.py tests/test_skill_intent_gates.py -q
uv run pytest tests/test_chat_router_timeout.py tests/test_adapter_telegram.py -q
```

## Current Local Proof

- Date: 2026-06-27
- Result: shared native command menu tests passed with `/vault` included for
  Telegram, Discord flat commands reusing the same curated list, and Discord
  registering one typed `/vault` group instead of a duplicate flat `/vault`.
- Live adapter proof: after restart, the bot log showed Telegram registering
  41 slash commands and Discord registering 41 slash commands from the shared
  command surface.
- Scope: local test proof plus live adapter sync proof. Platform clients may
  still cache native command menus until their UI refreshes.

## Latest Live Proof

- Date: 2026-06-02
- Surface: Telegram `getMyCommands`
- Result before this slice: live menu was stale with 70 commands and still
  showed old `publish` and `blogstatus` entries.
- Result after Telegram restart: live menu reports 30 curated native commands,
  includes `/commands` and `/linkedin`, and no longer includes `publish` or
  `blogstatus`.
- Delivery gate proof: a live Telegram answer rendered in Telegram Web and the
  bot log recorded final answer delivery before any follow-up delivery.

## Public Export Status

This feature page is public-framework safe. Public export must still go through
`scripts/sanitize.py`.
