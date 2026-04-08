# The Homie Framework — Development Guide

Personal AI agent framework: multi-platform chat bot, memory pipelines,
scheduled jobs, platform integrations, and Mission Control dashboard.

## Quick Start

1. Copy `.env.example` to `.env` and fill in your API keys
2. Copy `templates/memory/` to `vault/memory/` and customize
3. Run `cd .claude/scripts && uv sync` to install dependencies
4. Run `cd .claude/chat && bash run_chat.sh` to start the bot

## Architecture

See `.claude/sections/` for detailed documentation on each layer:

| Section | Topic |
|---------|-------|
| 01 | Vertical slice architecture |
| 02 | Chat interface (Telegram, Slack, Discord, etc.) |
| 03 | Memory pipelines (heartbeat, search, recall, reflect, weekly) |
| 04 | Smart data queries |
| 05 | Platform integrations |
| 08 | Observability (Langfuse) |

## Configuration

All configuration is via environment variables in `.claude/scripts/.env`.
See `.env.example` for the full list with descriptions.

### Required

- `TELEGRAM_BOT_TOKEN` — Your Telegram bot token
- `TELEGRAM_ALLOWED_USER_IDS` — Comma-separated Telegram user IDs

### Optional

- `OPENAI_API_KEY` — For OpenAI/Codex provider fallback
- `LANGFUSE_*` — For observability tracing
- Google OAuth — Run `cd .claude/scripts && uv run python setup_auth.py`
- Slack, Discord, WhatsApp — Set respective tokens in `.env`

## Memory Files

Customize these in your vault directory:

| File | Purpose |
|------|---------|
| `SOUL.md` | Agent personality and behavioral rules |
| `USER.md` | Owner profile, preferences, integrations |
| `MEMORY.md` | Durable facts, decisions, lessons |
| `GOALS.md` | Quarterly objectives and metrics |

See `templates/memory/` for starter templates.

## Testing

```bash
cd .claude/scripts && uv run pytest tests/ -v
```

## License

MIT
