Chat with The Homie through the configured Telegram bot. Each thread is a persistent conversation backed by the runtime layer — survives restarts.

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
| `adapters/telegram.py` | Telegram polling, voice/photo/document handlers, inline buttons (hash-mapped custom_ids for 64-byte callback_data limit), message formatting |
| `adapters/cli_adapter.py` | CLI adapter — interactive REPL and single-query mode |
| `adapters/web.py` | Web/relay adapter — WebSocket to Mission Control relay |
| `adapters/slack.py` | Slack adapter |
| `adapters/discord.py` | Discord adapter |
| `adapters/whatsapp.py` | WhatsApp adapter |
| `extension_manager.py` | Registry-driven command dispatch, intent detection, extension metadata |

**Config:** `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USER_IDS` in `.claude/scripts/.env`.

### BrowserOps Slash Commands

BrowserOps is the visible-browser specialist surface for requests that need the existing local Chrome/Chromium CDP session. Load `docs/browserops-agent-browser-manual.md` before changing BrowserOps, direct `agent-browser`, LinkedIn browser, or dashboard `/browser` behavior.

| Command | What it does | Safety boundary |
|---------|-------------|-----------------|
| `/browser status` | Reports visible Chrome/CDP readiness. | Read-only. |
| `/browser tabs` | Lists tabs with URL redaction. | Read-only; no raw query strings or fragments. |
| `/browser open <absolute http(s) url>` | Navigates the visible browser through the registered workflow gate. | Navigation only; no browser state export. |
| `/browser snapshot` | Captures an interactive text snapshot from the visible browser. | Read-only; page text is untrusted. |
| `/browserops capabilities` | Shows Browser Homie readiness, rules, stream state, and registered workflows. | Read-only. |
| `/browserops guide` | Loads the current installed `agent-browser` core guide plus local BrowserOps rules. | Read-only context. |
| `/browserops context` | Builds the engine-facing BrowserOps prefetch context. | Context only; must not execute actions. |
| `/linkedin_profile status` | Reports LinkedIn browser readiness using the visible browser safety layer. | Read-only. |
| `/linkedin_profile open` | Opens the configured LinkedIn profile in the visible browser. | Navigation only. |
| `/linkedin_profile edit` | Attempts a write-capable profile edit workflow. | Expected to be blocked/default-denied until a dedicated write PRP lands. |

Natural-language browser requests can attach BrowserOps context through prefetch. That path may load readiness, registered workflows, and the current `agent-browser` guide, but it must not click, type, post, edit, DM, connect, or navigate by itself.

### Cabinet Slash Commands (PRD-8 Phase 5b)

Three router-typed slash commands let an operator drive multi-persona text meetings from Telegram (or any other adapter that consumes the unified router). Cabinet handlers HTTP-route to `localhost:4322/api/cabinet/*` (the orchestration API process — same shape as `/budget` → `finance_api` → Supabase).

| Command | What it does | API call(s) |
|---------|-------------|-------------|
| `/cabinet` | Subcommand dispatcher: `create` / `list` / `send <id> <text>` / `end <id>` | `POST /api/cabinet/new` / `GET /list` / `POST /send` / `POST /end` |
| `/standup [question]` | Create meeting + send standup question as operator turn (default question via `CABINET_STANDUP_QUESTION` env) | `POST /api/cabinet/new` then `POST /api/cabinet/send` |
| `/discuss <topic>` | Create meeting + send the topic as the seed turn | `POST /api/cabinet/new` then `POST /api/cabinet/send` |

**Cross-process invariant:** the chat process (`.claude/chat/main.py`) and the orchestration API process (`.claude/scripts/orchestration/run_api.py`) are SEPARATE Python processes. Module-local channel registries cannot bridge them. Cabinet handlers MUST go via HTTP through `.claude/scripts/integrations/cabinet_api.py` — NOT direct `from cabinet.text_orchestrator import …` (which would land in the chat-process's empty `_CHANNELS`, never reaching the SSE subscribers in the API process).

**Roster behavior:** Phase 5a's `_roster_from_personas()` (`.claude/scripts/cabinet/text_orchestrator.py:81-130`) auto-snapshots whichever cabinet-eligible personas are registered at meeting-create time. Operators manage the active roster via `/persona` BEFORE running `/cabinet create`. There is NO persona-selection arg at the chat-command level (R1 B4 fix). When no cabinet-eligible personas are registered, `_roster_from_personas()` falls back to a Main-only single-turn reply.

### Cabinet Browser Room (PRD-8 Phase 5c)

The dashboard Cabinet page is now the text-first room surface. On load it calls `POST /api/cabinet/open` with a stable browser `chatId`, reuses the latest open room for that chat when one exists, and creates one only when needed. Room details, transcript fetches, stream snapshots, pin state, and participant changes all read the meeting roster snapshot from `cabinet_text_meetings.roster_json`; live persona registry reads are fallback behavior only when no usable snapshot exists.

For the full on-demand dashboard manual, vertical-slice architecture, failure-mode table, and validation map, load `docs/cabinet-room-manual.md`.

Browser sends use an additive audience contract:

| Browser input | Send body | Behavior |
|---------------|-----------|----------|
| `@sales status?` | `audience="mentions"` | Only mentioned homies respond. |
| `@sales @marketing plan?` | `audience="mentions"` | Mentioned homies respond in room roster order. |
| `what is everyone seeing?` | `audience="all"` | Every active room participant responds. |
| `/all <message>` | slash command | Server rewrites to an all-room turn without sending the command text to the LLM. |
| `/add @finance` / `/remove @finance` | slash command | Server updates `roster_json` and `cabinet_meetings.broadcast_order` in one transaction, then emits `meeting_state_update`. |
| `/pin @sales` / `/unpin` / `/voice` / `/end` / `/help` | slash command | Server-side command path; command text never enters the LLM prompt. |

Each non-default participant turn resolves the current Homie profile for that participant ID before runtime execution. The roster snapshot owns membership/order/display; the selected profile owns identity, memory files, config, runtime/auth settings, tools, and voice configuration.

**Friendly error UX:** `cabinet_api` raises `CabinetAPIUnreachable` (httpx.ConnectError), `CabinetAuthFailure` (401), `CabinetKillSwitchDisabled` (503 on synchronous endpoints — `/new` and `/end`), `CabinetMeetingNotFound` (404), `CabinetMeetingEnded` (410), `CabinetBadRequest` (400), and `CabinetChatScopeMismatch` (403 chat_mismatch from `dashboard_api.py:1986-1997`). Each carries a `friendly_message` string the handlers return verbatim — operators see "Cabinet API is not running…" / "That meeting belongs to a different chat. Use /cabinet list…" instead of stack traces.

**Config:**
- `ORCHESTRATION_API_BASE_URL` (default `http://127.0.0.1:4322`) — base URL for the orchestration API.
- `ORCHESTRATION_API_TOKEN` (optional in loopback no-token mode) — mirrors the server-side bearer-token middleware in `.claude/scripts/orchestration/api.py:252-279`. When the server has a token set, the chat-side env must match.
- `CABINET_STANDUP_QUESTION` (optional) — overrides the default standup seed.
