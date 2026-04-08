Chat with The Homie through Telegram (`@YourBot`). Each thread is a persistent conversation backed by the runtime layer — survives restarts.

**Location:** `.claude/chat/`

**Start it:**
```bash
cd .claude/chat && bash run_chat.sh          # Background (writes bot.log, bot.pid)
cd .claude/chat && bash run_chat.sh --fg     # Foreground (for debugging)
```

**Test without connecting:**
```bash
cd .claude/scripts && uv run python ../chat/main.py --test
```

**How it works:** Telegram message → platform-agnostic `IncomingMessage` → router (slash commands handled instantly) or engine (runtime-backed conversation with tools) → response posted back. Sessions stored in `.claude/data/chat.db`.

### Process Lifecycle & Resilience

| Component | How it works |
|-----------|-------------|
| **Instance lock** | Windows named mutex (`Global\SecondBrainTelegramBot`). Prevents double-spawn from venv launcher. Auto-recovers orphaned mutexes by checking if the PID that holds it is alive. |
| **PID tracking** | `bot.pid` written on startup, cleaned on exit via `atexit`. Signal handlers (SIGTERM/SIGINT) ensure clean shutdown. |
| **Task supervision** | `_run_all()` uses `asyncio.wait()` (not `gather`) — if relay WS or MC heartbeat crashes, it's logged and the router keeps running. If the router itself dies, the whole bot exits with a logged error. |
| **Listener retry** | `_listen()` retries with exponential backoff (up to 5 attempts, max 30s) on transient errors instead of dying silently. |
| **Adapter isolation** | Each adapter connects independently — one failing doesn't block the others. |
| **Crash logging** | Top-level `except Exception` around `asyncio.run()` ensures no crash ever goes unlogged. |

### Key Files

| File | Purpose |
|------|---------|
| `main.py` | Entry point — instance lock, PID lifecycle, signal handlers, task supervision |
| `run_chat.sh` | Shell wrapper — resolves real cpython (skips venv shim), kills old process, starts background |
| `router.py` | Routes messages: slash commands handled instantly, natural language → engine |
| `engine.py` | Runtime-backed conversations via Claude Agent SDK |
| `adapters/telegram.py` | Telegram polling, voice/photo/document handlers, message formatting |
| `adapters/cli_adapter.py` | CLI adapter — interactive REPL and single-query mode |
| `adapters/web.py` | Web/relay adapter — WebSocket to Mission Control relay |
| `adapters/slack.py` | Slack adapter |
| `adapters/discord.py` | Discord adapter |
| `adapters/whatsapp.py` | WhatsApp adapter |
| `extension_manager.py` | Registry-driven command dispatch, intent detection, extension metadata |

**Config:** `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USER_IDS` in `.claude/scripts/.env`.
