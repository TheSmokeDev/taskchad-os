# The Homie Installation Guide

## Prerequisites

- **Python 3.12+** — [python.org](https://www.python.org/downloads/)
- **Node.js 22.12+** — required for dashboard and Desktop v0 assets
- **uv** — Fast Python package manager: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **Obsidian** (optional) — For vault management and cross-machine sync

## Quick Start

```bash
# 1. Clone the public framework repo
git clone https://github.com/TheSmokeDev/taskchad-os.git
cd taskchad-os

# 2. Run the setup wizard
cd .claude/scripts && uv run python setup_wizard.py

# 3. Configure your .env
# The wizard creates .env from the template — edit it with your tokens

# 4. Start the bot (background — writes bot.log, bot.pid)
cd .claude/chat && bash run_chat.sh
# or run in the foreground:
# cd .claude/scripts && uv run python ../chat/main.py
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
6. **Set `DISCORD_ALLOWED_GUILDS` to your server ID** so native slash commands
   register **per-guild and appear instantly**. Leave it empty and the bot syncs
   commands **globally**, which Discord can take **up to ~1 hour** to propagate
   to a fresh install — the `/` picker looks empty until then (normal Discord
   behavior, not a bug). Optionally also set `DISCORD_ALLOWED_USERS`.

### WhatsApp

1. Create a Meta Business account at [business.facebook.com](https://business.facebook.com)
2. Set up WhatsApp Business API in the [Meta Developer Dashboard](https://developers.facebook.com)
3. Get your permanent access token and Phone Number ID
4. Set `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, and `WHATSAPP_VERIFY_TOKEN` in `.env`
5. Configure your webhook URL to point to `https://your-domain:8443/webhook`

### Buzz collaboration

Buzz is an external signed room client and relay; the Homie Dashboard remains
the operator/control plane. Install the stock Buzz Desktop and official `buzz`
CLI from the upstream Buzz `0.5.x` release, then run the upstream local stack.
Do not copy keys into Dashboard settings.

Configure one identity per Homie profile:

```bash
BUZZ_RELAY_URL=
BUZZ_PRIVATE_KEY=
<REDACTED-buzz-private-key>
BUZZ_PUBKEY_ROLES=



BUZZ_CLI_PATH=buzz
BUZZ_TRANSPORT=auto
BUZZ_REQUIRE_MENTION=true
```

Every allowlisted sender defaults to Homie's `viewer` role. Use comma-separated
`pubkey=viewer|operator|admin` entries in `BUZZ_PUBKEY_ROLES` only for explicit
per-profile elevation; for example, map the operator's own public key to
`admin`. A Buzz room membership or reaction never grants a Homie role.

Run only the Buzz adapter with `uv run thehomie chat --buzz`. Verify the active
transport and CLI compatibility with `uv run thehomie status --json` and
`uv run thehomie doctor`. See
[`docs/manual/features/buzz-native-collaboration.md`](docs/manual/features/buzz-native-collaboration.md)
for security, receipts, desktop, and local-pilot details.

## Talk Mode (voice)

Talk Mode is live speech-to-speech over the OpenAI Realtime API. It has two
surfaces with different setup costs:

- **Dashboard Talk view** is zero-install. The browser negotiates WebRTC with
  OpenAI directly, so there is nothing extra to run.
- **Discord `/talk join`** needs a one-time sidecar install, because py-cord
  cannot share an environment with discord.py:

```bash
cd .claude/scripts/discord_voice
uv sync
```

Authentication resolves in order: `TALK_OPENAI_API_KEY`, then `OPENAI_API_KEY`,
then Codex OAuth via `codex login` (which reuses an existing ChatGPT sign-in, so
no API key is required). The Discord surface also needs `DISCORD_BOT_TOKEN` and
the orchestration API running on port 4322.

Verify with `/talk status` in Discord. See
[`docs/talk-mode-showcase.md`](docs/talk-mode-showcase.md) for the full setup
walkthrough, the receive-pipeline tuning knobs, and troubleshooting.

To have an agent do it instead, the repo ships a `talk-mode-setup` skill that
detects what is already configured and asks before changing anything. It also
steers voice to a Codex subscription when one is available, since an API key
would otherwise outrank it and bill per minute.

## Docker Deployment

```bash
# Create the Compose env file first
cp .claude/scripts/.env.example .claude/scripts/.env

# Validate, build, and run
docker compose config
docker compose up -d

# Check health
curl http://localhost:8787/health

# View logs
docker compose logs -f bot
```

## systemd Deployment (Linux)

```bash
# 1. Copy files to server
./deploy/deploy.sh root@your-server /opt/taskchad-os

# 2. Install the service
sudo cp deploy/secondbrain.service /etc/systemd/system/thehomie.service
sudo systemctl daemon-reload
sudo systemctl enable thehomie
sudo systemctl start thehomie

# 3. Set up log rotation
sudo cp deploy/logrotate.conf /etc/logrotate.d/thehomie
```

The source unit file is still named `deploy/secondbrain.service` for backward
compatibility; install it under the service name you want to operate.

## Vault Setup

Use the example vault as a starting point:

```bash
cp -r example-vault/ vault/memory/
```

Or create your own vault with the required files. See [docs/vault-setup.md](docs/vault-setup.md) for details.

## Integrations (Google OAuth, Asana, Slack)

```bash
cd .claude/scripts
uv run python setup_auth.py           # Walk through each integration
uv run python setup_auth.py --check   # Verify everything is connected
```

## Memory Search Index

```bash
cd .claude/scripts
uv run python memory_index.py --rebuild   # ~80MB ONNX model, one-time download
```

## Background Jobs (Windows — Task Scheduler)

```powershell
# Creates: heartbeat (30 min), daily reflection (8 AM),
#          weekly synthesis (Sun 8 PM), dream consolidation (post-weekly + on-demand)
powershell -ExecutionPolicy Bypass -File .claude/scripts/setup_scheduler.ps1   # Run as Admin
```

On Linux, the Docker Compose scheduler service covers the same jobs
(`docker compose up` runs bot + scheduler), or use systemd timers.

## Troubleshooting

### Bot won't start — "Another instance holds the lock"
Kill the stale process: check `bot.pid` for the PID.

### Telegram "Conflict" errors
Another polling session is active. Wait 30 seconds or restart.

### Voice notes don't work
Set `OPENAI_API_KEY` in `.env` for Whisper transcription.

### Health check returns nothing
Ensure `HEALTH_CHECK_PORT` (default 8787) is not in use by another service.
