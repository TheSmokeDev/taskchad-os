# Telegram Command Menu

Status: shipped, live Telegram menu verified
Owner: `.claude/chat/` command registry and Telegram adapter
Last updated: 2026-06-02

## What It Does

Telegram shows a native slash-command dropdown. Homie keeps the full command
registry dispatchable, but exposes only a curated top-level menu in Telegram so
the visible command list stays useful.

## Operator Entry Points

- Telegram native menu: curated commands from `.claude/chat/commands.py`
- Chat command audit: `/commands native`, `/commands all`
- Full help: `/help`
- LinkedIn/Social Homie: `/linkedin [draft|ideas|revise] <topic-or-text>`

## Source Of Truth Files

| Layer | Files |
|---|---|
| Command registry | `.claude/chat/commands.py` |
| Router handlers | `.claude/chat/core_handlers.py`, `.claude/chat/router.py` |
| Telegram adapter | `.claude/chat/adapters/telegram.py` |
| LinkedIn prompt | `.claude/commands/linkedin.md` |
| Tests | `.claude/scripts/tests/test_command_menu.py`, `.claude/scripts/tests/test_skill_intent_gates.py` |

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
/linkedin ideas AI operator systems
/linkedin draft What I learned building multi-persona agents
/linkedin revise <paste draft>
```

## How To Test It

```powershell
cd .claude/scripts
uv run pytest tests/test_command_menu.py tests/test_skill_intent_gates.py -q
```

## Latest Live Proof

- Date: 2026-06-02
- Surface: Telegram `getMyCommands`
- Result before this slice: live menu was stale with 70 commands and still
  showed old `publish` and `blogstatus` entries.
- Result after Telegram restart: live menu reports 30 curated native commands,
  includes `/commands` and `/linkedin`, and no longer includes `publish` or
  `blogstatus`.

## Public Export Status

This feature page is public-framework safe. Public export must still go through
`scripts/sanitize.py`.
