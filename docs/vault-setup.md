# Vault Setup

The Homie uses an Obsidian-compatible vault as its persistent memory substrate.

## Quick Start

1. Copy `templates/memory/` to your vault directory (default: `vault/memory/`)
2. Customize `SOUL.md` with your agent's personality
3. Customize `USER.md` with your profile and preferences
4. Set the vault path in `.env` if using a non-default location

## Memory Files

| File | Purpose |
|------|---------|
| `SOUL.md` | Agent personality and behavioral rules |
| `USER.md` | Owner profile, account IDs, preferences |
| `MEMORY.md` | Durable facts, decisions, lessons learned |
| `GOALS.md` | Quarterly objectives and key metrics |
| `HEARTBEAT.md` | Integration check intervals and thresholds |

## Entity Compilation

When documents are ingested, the compilation engine extracts entities and creates
concept pages in `concepts/`. This turns your vault into an interlinked knowledge graph.

See `.claude/scripts/entity_extractor.py` for CLI reference.
