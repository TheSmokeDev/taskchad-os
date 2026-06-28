# Native Vault Commands

Status: shipped, regression-proven, live adapter sync proven
Owner: `.claude/chat/` command registry, router, and adapters
Last updated: 2026-06-27

## What It Does

Homie exposes one shared vault command surface across chat channels:

```text
/vault status [vault]
/vault db [vault]
/vault search <query> [--vault name] [--mode auto|hybrid|keyword] [--limit N]
/vault context <topic> [--vault name]
/vault contacts [query] [--vault name]
/vault ingest <url> [--vault name]
/vault ops <routine> [args] [--vault name]
```

The default vault is `thehomie`. Valid vault names are `thehomie`,
`coding-vault`, and `unified-vault`, resolved through the configured vault
registry.

The native command baseline is shared. Telegram exposes `/vault` in the normal
curated native menu. Discord exposes a typed `/vault` command group with
subcommands and choices, but every Discord callback converts back into the same
router text path. That keeps behavior unified while still using platform-native
controls where they exist.

## Operator Entry Points

- Telegram: `/vault ...` from the native command menu.
- Discord: typed `/vault` group with `status`, `db`, `search`, `context`,
  `contacts`, `ingest`, and `ops`.
- Text aliases: `/vaults ...` and `/vault-ops ...`, only when the message
  starts with that exact slash command token.
- Existing strict ingest caption remains: `/vault-ingest`.

Examples:

```text
/vault db thehomie
/vault search YourProduct --vault thehomie
/vault context YourProduct prospect mockups --vault thehomie
/vault contacts Haris --vault thehomie
/vault ops context YourProduct --vault thehomie
```

## Source Of Truth Files

| Layer | Files |
|---|---|
| Command registry | `.claude/chat/commands.py` |
| Router implementation | `.claude/chat/router.py` |
| Registry guard/help handler | `.claude/chat/core_handlers.py` |
| Discord typed group | `.claude/chat/adapters/discord.py` |
| Vault resolution | `.claude/scripts/config.py` |
| Recall service | `.claude/scripts/recall_service.py` |
| URL/document ingest | `.claude/scripts/url_fetch.py`, `.claude/chat/attachment_context.py`, `.claude/scripts/entity_extractor.py` |
| Tests | `.claude/scripts/tests/test_command_menu.py`, `test_adapter_discord.py`, `test_router_transcript_persistence.py`, `test_url_fetch.py`, `test_vault_ingest_document.py`, `test_recall_cli.py` |

## Safety Boundaries

- Slash routing is explicit-only. Homie dispatches a command only when the
  trimmed message starts with an exact `/command` token followed by whitespace
  or end-of-message.
- Pasted paths, URLs, and logs are normal chat text. `docs/foo/vault.md`,
  `https://site.com/vault`, `/opt/app/docs/DESIGN.md`, and
  `phone/email/locations` must not run commands.
- Read-only vault commands (`status`, `db`, `search`, `context`, `contacts`)
  run directly through deterministic Python and recall.
- Mutating vault work only runs from explicit `/vault ingest ...`,
  `/vault-ingest`, or `/vault ops ...`.
- Discord attachment ingest downloads the interaction attachment into the same
  internal attachment model used by normal Discord uploads before the router
  sees it.
- Native command names must stay valid on every shared surface. Hyphenated text
  aliases can remain registered, but they should not be added to the shared
  native menu.

## How To Run It

```powershell
cd .claude/scripts
uv run thehomie chat -q "/vault db thehomie" -Q
uv run thehomie chat -q "/vault search YourProduct --vault thehomie" -Q
```

## How To Test It

```powershell
cd .claude/scripts
uv run pytest tests/test_command_menu.py tests/test_adapter_discord.py tests/test_router_transcript_persistence.py tests/test_url_fetch.py tests/test_vault_ingest_document.py tests/test_recall_cli.py tests/test_extension_manager.py -q
```

The broader continuity and timeout regression lane is:

```powershell
cd .claude/scripts
uv run pytest tests/test_chat_runtime_engine.py tests/test_cognition_continuity.py tests/test_router_transcript_persistence.py tests/test_chat_router_timeout.py tests/test_config_reload.py -q
```

## Current Regression Proof

- Date: 2026-06-27
- Local proof: `py_compile` passed for router, command registry, core handlers,
  Discord adapter, and the new/updated test modules.
- Combined requested suite passed: `261 passed, 28 warnings`.
- Live adapter proof: after restart, the bot log showed Telegram and Discord
  registering native slash commands from the shared command surface.
- Scope: local code, test proof, and live adapter sync proof. Platform clients
  may still cache native command menus until their UI refreshes.

## Public Export Status

This feature page is public-framework safe. Public export must still go through
`scripts/sanitize.py`.
