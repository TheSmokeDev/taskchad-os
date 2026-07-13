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
| **Task supervision** | `_run_all()` uses `asyncio.wait()` (not `gather`) — if relay WS or MC heartbeat crashes, it's logged and the router keeps running. If the router or the **liveness supervisor** dies, the whole bot exits with a logged error and a **non-zero exit code**. |
| **Listener retry** | `_listen()` retries with exponential backoff (up to 5 attempts, max 30s) on transient errors instead of dying silently. **This cannot catch a wedge** — `listen()` is an await on a queue, so a dead poller and an idle one look identical from there. That's what the liveness probe is for. |
| **Adapter isolation** | Each adapter connects independently — one failing doesn't block the others. |
| **Crash logging** | Top-level `except Exception` around `asyncio.run()` ensures no crash ever goes unlogged, and exits `1` (it used to fall through and exit `0`, so a crash was indistinguishable from a clean shutdown). |
| **Adapter liveness** | `.claude/chat/liveness.py` — `LivenessSupervisor` probes each adapter's PHYSICAL state on an interval (Telegram: `updater.running` + a live `get_me()`; Discord: gateway task alive + `is_ready()`; web: relay socket connected). K consecutive failures → in-process reconnect → re-probe (never trust the reconnect's own return) → fail fast. Knobs: `BOT_LIVENESS_*` via `config.get_bot_liveness_settings()`. |
| **Gateway criticality** | Adapters declare `liveness_critical`. Telegram/Discord are **gateways** (the operator talks through them) — their death is restart-worthy. The web/relay adapter is **not** — it dials OUT to Mission Control and redials itself, so a dead relay is reported but never restarted over (restarting can't fix someone else's outage). |
| **External watchdog** | `.claude/scripts/bot_watchdog.py` + scheduled task `SecondBrain-BotWatchdog` (every 5 min). Polls `/health`, restarts via `run_chat.sh` when a gateway is dead or the process is gone, verifies recovery by re-polling `/health`, and enforces a 5-restarts-per-hour budget. Catches what the in-bot supervisor can't: a hard hang, an OOM kill, or a bot that never started. |

**The wedge (2026-07-12).** The bot ran wedged for ~6 weeks: process alive, PID file present, `/health` reporting `telegram: true` — while Telegram polling was dead. Three blind spots, all now closed: (1) `listen()` can't detect a dead poller; (2) `/health` reported *registration presence*, not liveness, and ran `collect_diagnostics()` synchronously on the event loop (~3.4s warm, hung at boot — now cached off the request path, ~4ms); (3) nothing external ever polled `/health` — `service.py` is crash-only and a wedge never exits. Corollary fixed at the same time: `service.py` imported a `send_notification` symbol that does not exist, so its "bot down" alert had never once fired.

**Restart trap:** `run_chat.bat` hardcodes `--telegram` — restarting through it silently resurrects a Telegram-only bot with no Discord and no relay. Always restart with `run_chat.sh` (the watchdog does).

### Key Files

| File | Purpose |
|------|---------|
| `main.py` | Entry point — instance lock, PID lifecycle, signal handlers, task supervision |
| `liveness.py` | Adapter liveness supervisor, gateway criticality, off-request-path diagnostics cache, `resolve_health_status()` |
| `../scripts/bot_watchdog.py` | External watchdog — polls `/health`, restarts a wedged/dead bot, restart budget, operator toast |
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

### Import Convention — Flat sys.path (decided 2026-06-10, #53)

The chat slice uses **flat sys.path imports** (`import voice`, `import config`,
`from voice_markers import ...`) — the launchers put `.claude/chat/` itself on
`sys.path` instead of packaging the slice. Consequences:

- **No static import resolver can see intra-slice edges.** IDEs, graphify, rope,
  and dead-code analyzers report false orphans here — `voice.py` shows zero
  cross-file edges despite all five adapters importing it via
  `import voice as voice_mod`.
- **A "dead code" verdict inside `.claude/chat/` requires grep confirmation**,
  never resolver/graph output alone. The retirement standard is the #54 bar:
  no importers + no listener + no scheduler entry + no consumers.
- Same invisibility class: `cli_entry.py` is reached through the `thehomie`
  console-script entry point (`.claude/scripts/pyproject.toml`), which AST
  tooling cannot see either.

Decision: **document-only** (this section). Package-ifying the slice (relative
imports + `__init__` wiring) would touch every chat file plus the launch
scripts for zero behavioral gain. Revisit only if a refactor forces it.

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
| `/linkedin_post <feed_url> \| <body> \| <approval phrase>` | Creates a LinkedIn post on the visible browser. | Default-deny write; fires only when the final pipe segment is exactly `post this to linkedin now`; audit row + screenshot receipt. |
| `/linkedin_connect <profile_url> \| <note> \| <approval phrase>` | Sends a LinkedIn connection request (optional note). | Default-deny write; fires only when the final pipe segment is exactly `send this linkedin connection request now`; one approval, one invite. |
| `/reddit comment <thread_url> \| <body> \| <approval phrase>` | Posts a comment reply on a Reddit thread. | Default-deny write; fires only when the final pipe segment is exactly `post this comment to reddit now`; thread URL validated. |
| `/reddit post <subreddit> \| <title> \| <body> \| <approval phrase>` | Creates a Reddit self-post in a subreddit. | Default-deny write; fires only when the final pipe segment is exactly `post this to reddit now`; subreddit validated. |

The four social-write commands above are the execution half of the Social/LinkedIn Homie. The body can never approve itself — the approval is a distinct trailing pipe segment, exact-matched. `/reddit research` and `/reddit status` are read-only. See `docs/manual/features/social-write-executor.md`.

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

**Cabinet → chat relay:** persona turns now post back into the **originating chat** (Discord/Telegram/…), not only the dashboard. When a cabinet command is issued from a chat adapter, the handler calls `ensure_relay()` (`.claude/chat/cabinet_relay.py`), which spawns one background task per meeting (deduped by `meeting_id`). The task consumes the EXISTING `cabinet_api.stream_meeting()` SSE client (preserving the cross-process invariant — no direct `text_orchestrator` import) and relays each completed `agent_done` turn (name-prefixed, e.g. `**Sales:** …`) to the origin channel via the adapter's `send()`. It stops on `meeting_ended`, the per-meeting `CABINET_CHAT_RELAY_MAX_TURNS` cap, or stream EOF, and is fail-open at every seam (a relay failure never affects the handler reply or crashes the bot). When the relay is active the chat reply says "the homies will answer right here"; when disabled it falls back to the dashboard-URL message. `/standup` fans out to the whole roster (a firehose) — prefer `@mention` audiences for tight turns.

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
- `CABINET_CHAT_RELAY_ENABLED` (default `true`) — master switch for the cabinet→chat relay. When `false`, cabinet commands behave as before (dashboard-only; chat reply points at the browser URL).
- `CABINET_CHAT_RELAY_MAX_TURNS` (default `0` = unlimited) — per-meeting cap on relayed persona turns; guards against a `/standup` firehose.
