# Platform Adapters

The Homie supports multiple chat platforms via a unified adapter interface.

## Available Adapters

| Adapter | File | Transport |
|---------|------|-----------|
| Telegram | `.claude/chat/adapters/telegram.py` | Long-polling |
| CLI | `.claude/chat/adapters/cli_adapter.py` | Interactive REPL |
| Web/Relay | `.claude/chat/adapters/web.py` | WebSocket |
| Slack | `.claude/chat/adapters/slack.py` | Socket Mode |
| Discord | `.claude/chat/adapters/discord.py` | Gateway WebSocket |
| WhatsApp | `.claude/chat/adapters/whatsapp.py` | Cloud API webhooks |

## Writing a Custom Adapter

Each adapter implements the platform-specific transport and converts messages into
a platform-agnostic `IncomingMessage` dataclass. See `adapters/telegram.py` as a
reference implementation.
