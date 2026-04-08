# Installation Guide

## Prerequisites

- **Python 3.12+** — [python.org](https://www.python.org/downloads/)
- **uv** — Fast Python package manager: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **Obsidian** (optional) — For vault management and cross-machine sync

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/thehomie-framework/thehomie.git
cd thehomie

# 2. Run the setup wizard
cd .claude/scripts && uv run python setup_wizard.py

# 3. Configure your .env
# The wizard creates .env from the template — edit it with your tokens

# 4. Start the bot
cd .claude/chat && bash run_chat.sh
```

## Platform Setup

### Telegram

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the prompts
3. Copy the bot token to `TELEGRAM_BOT_TOKEN` in `.env`
4. Get your user ID from [@userinfobot](https://t.me/userinfobot) and add to `TELEGRAM_ALLOWED_USER_IDS`

### Slack

1. Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps)
2. Enable Socket Mode and generate an App-Level Token (`xapp-...`)
3. Add bot scopes: `app_mentions:read`, `chat:write`, `im:history`, `im:read`, `im:write`
4. Install to workspace and copy Bot User OAuth Token
5. Set `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` in `.env`

### Discord

1. Create an application at [discord.com/developers](https://discord.com/developers/applications)
2. Go to **Bot** tab, create a bot, copy the token
3. Enable **Privileged Gateway Intents**: MESSAGE CONTENT, SERVER MEMBERS
4. Invite bot to your server with `bot` + `applications.commands` scopes
5. Set `DISCORD_BOT_TOKEN` in `.env`
6. Optionally set `DISCORD_ALLOWED_GUILDS` and `DISCORD_ALLOWED_USERS`

### WhatsApp

1. Create a Meta Business account at [business.facebook.com](https://business.facebook.com)
2. Set up WhatsApp Business API in the [Meta Developer Dashboard](https://developers.facebook.com)
3. Get your permanent access token and Phone Number ID
4. Set `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, and `WHATSAPP_VERIFY_TOKEN` in `.env`
5. Configure your webhook URL to point to `https://your-domain:8443/webhook`

## Docker Deployment

```bash
# Build and run
docker compose up -d

# Check health
curl http://localhost:8787/health

# View logs
docker compose logs -f bot
```

## systemd Deployment (Linux)

```bash
# 1. Copy files to server
./deploy/deploy.sh root@your-server /opt/thehomie

# 2. Install the service
sudo cp deploy/thehomie.service /etc/systemd/system/thehomie.service
sudo systemctl daemon-reload
sudo systemctl enable thehomie
sudo systemctl start thehomie

# 3. Set up log rotation
sudo cp deploy/logrotate.conf /etc/logrotate.d/thehomie
```

## Vault Setup

Use the example vault as a starting point:

```bash
cp -r example-vault/ vault/memory/
```

Or create your own vault with the required files. See [docs/vault-setup.md](docs/vault-setup.md) for details.

## Troubleshooting

### Bot won't start — "Another instance holds the lock"
Kill the stale process: check `bot.pid` for the PID.

### Telegram "Conflict" errors
Another polling session is active. Wait 30 seconds or restart.

### Voice notes don't work
Set `OPENAI_API_KEY` in `.env` for Whisper transcription.

### Health check returns nothing
Ensure `HEALTH_CHECK_PORT` (default 8787) is not in use by another service.
