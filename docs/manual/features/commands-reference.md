# Slash Commands Reference

Status: Active baseline
Owner: `.claude/chat/` command registry (`commands.py`, `core_handlers.py`, `router.py`)
Last updated: 2026-07-18

## What It Does

This is the catalog of every operator slash command, grouped by category, with a
pointer to the deep feature page for each area. It is a map, not the source of
truth — for the always-current list run **`/commands all`** (full registry) or
**`/commands native`** (the curated Telegram menu), and **`/help`** for grouped
help in chat.

Commands come in three layers:

1. **Router-backed** (`router` in the registry) — handled instantly by a Python
   handler in `core_handlers.py`. Zero model tokens; ~1-3s. Example: `/budget`,
   `/browser`, `/cabinet`.
2. **Engine-passed** (`engine` in the registry) — the command text is handed to
   the runtime and interpreted as an instruction. Example: `/search`, `/file`,
   `/calendar`, the content + dev-workflow commands.
3. **Skills** (`/skill-name`) — Claude Code skills invoked through the Skill
   tool, outside the normal router/engine flow. Example: `/vault-ops`,
   `/graphify`, `/direct-integrations`.

Most commands require the `admin` role; a few are `operator` or `viewer`.
Mutating "write to the outside world" commands are **default-denied** and need an
explicit, exact approval phrase (see [Approval-gated writes](#approval-gated-writes)).

### Native menu vs text-only

Every command in this catalog works when typed. A curated subset also shows up
in the **native slash menu** (Telegram's `/` dropdown and Discord's slash
picker) — 55 commands today. The rest are **text-only**: they dispatch fine but
are kept off the menu on purpose (`NATIVE_MENU_EXCLUDED` in `commands.py`).

- **Menu-registered families:** system/session basics (`/help`, `/status`,
  `/diagnostics`, `/clear`, `/provider`, `/model`, `/voice`, `/restart`), integrations
  (`/email`, `/pemail`, `/cleanup`, `/accounts`, `/inbox`, `/calendar`,
  `/tasks`), browser/social (`/browser`, `/browserops`, `/ghost`, `/x`,
  `/reddit`, the LinkedIn commands, `/video`), analytics (`/gsc`, `/analytics`,
  `/signal`), finance (`/budget`), cabinet/team (`/cabinet`, `/standup`,
  `/discuss`, `/teamroom`, `/team`, `/teamtick`, `/cofounder`), memory
  (`/search`, `/vault`, `/file`, `/skills`, `/working`), content (`/blog`,
  `/image`, `/tweet`, `/quote`, `/instagram`, `/design`), and automation
  (`/recap`, `/blueprints`, `/suggestions`).
- **Text-only families:** mode toggles (`/plan`, `/go`, `/mode`, `/reload`,
  `/cost`, `/extensions`), raw engine integrations (`/post`, `/slack`,
  `/sheets`, `/docs`, `/drive`, `/circle`), hyphenated aliases
  (`/generate-image`, `/owner-image` — Telegram command names can't contain
  hyphens), content long-tail (`/yt_script`, `/shorts`), the PIV coding-session
  workflow (`/prime`, `/planning`, `/implement`, `/validate`, `/review`,
  `/reviewfix`, `/commit`, `/prd`, `/e2e`, `/sysreview`, `/execreport`,
  `/clutch`), and engine-only dev tools (`/diagram`, `/pdf`, `/slides`, `/sop`).

Discord inherits the same menu minus the flat `vault` (it registers a typed
`/vault` command group instead). See
[Telegram Command Menu](telegram-command-menu.md) for the drift-proofing
contract.

---

## System & Session

Deep dive: [Telegram Command Menu](telegram-command-menu.md) · [Runtime Status And Model Control](runtime-status-model-control.md) · [Bot Self-Restart](bot-self-restart.md) · [Scheduled Jobs, Settings, And Audit](scheduled-settings-audit.md)

| Command | What it does | Notes |
|---|---|---|
| `/help` | Show all available commands | grouped by category |
| `/commands` | Browse the native menu or the full registry | `native` / `all` |
| `/status` | Session info — messages, cost, uptime | |
| `/diagnostics` | Full health report — cognition, recall, runtime, sessions, adapters | admin |
| `/cost` | Current session cost in USD | |
| `/clear` (`/new`) | Clear the conversation and start fresh | lifecycle flush boundary |
| `/plan` | Plan mode — research only, no changes | |
| `/go` (`/execute`) | Execute mode — implement changes | |
| `/mode` | Show the current mode | |
| `/voice` | Persist voice reply behavior across Telegram and Discord | `always` = voice + text, `auto` = reply in voice to voice messages, `off` = text only; `/voice on` aliases `always` |
| `/provider` | Runtime lane status — selection, routes, health | admin |
| `/model` | Select lane/provider/model (claude, sonnet, opus, codex, gemini, openrouter, openai, kimi, auto) | admin; Discord-native slash command; `kimi:k3` pins the Kimi lane model |
| `/reload` | Reload bot config without restarting | admin |
| `/restart` | Restart the bot — kill this process and start fresh | admin |
| `/update` | Safe stable framework update — `status`, `now`, `auto on|off|status`, `history` | admin |
| `/extensions` | Extension diagnostics — list, doctor, enable/disable | admin |

## Memory & Vault

Deep dive: **[Memory And Recall System](memory-and-recall-system.md)** · [Native Vault Commands](native-vault-commands.md) · [Video Learning](video-learning.md) · [Episodes](episodes.md) · [Session Opening Brief](session-opening-brief.md) · [Document Uploads And Ingest](document-uploads-and-ingest.md) · [Skill-From-Experience Loop](skill-from-experience-loop.md)

| Command | What it does | Sub-commands |
|---|---|---|
| `/vault` | Vault operations over the recall stack | `status` / `db` / `search <q>` / `context <topic>` / `contacts` / `ingest <url>` / `ops <routine>` |
| `/search` | Search memory — keyword or semantic over notes | |
| `/file` | File the last answer as a vault note (entity compilation) | |
| `/working` | Cross-session scratchpad — open threads, hypotheses, questions | `add "<text>"` / `resolve <N>` |
| `/skills` | Review / promote / reject self-authored skill drafts | `review` / `promote <name>` / `reject <name>` |
| `/learn` | Author a staged reusable skill from a URL, path, conversation, or notes | source plus optional focus |
| `/watch` | Learn from one video, compare it with current context, and save a sourced note | `status` / `retry` / `cancel` / `apply` / `approve` |

The `/vault-ops` skill chains the atomic vault operations and drives the recall
stack (see [Memory And Recall System](memory-and-recall-system.md) → "How The
Slash Commands Use Memory"):

| `/vault-ops` sub-command | Job |
|---|---|
| `orient` | Start-of-work SITREP (recall-augmented) |
| `debrief` | End-of-work capture + autolink |
| `weekly` | Full audit + synthesis across all vaults |
| `context <topic>` | Topic briefing from existing vault knowledge |
| `think <topic>` | Strategic Socratic reasoning grounded in the vault |
| `research <topic>` | Net-new research (vault prior-art first, then web) |
| `capture` / `ingest` / `compile` / `maintain` / `status` | Intake, integration, entity compilation, health, snapshot |

## Browser & Social

Deep dive: [BrowserOps + Browser Viewer](browserops-browser-viewer.md) · [Social-Write Executor](social-write-executor.md) · [Social Post Pipeline](social-post-pipeline.md) · [Social Cadence Draft Delivery](social-cadence-draft-delivery.md)

| Command | What it does | Write-gated? |
|---|---|---|
| `/browser` | Browser checks over the visible Chrome CDP session — status, tabs, open, snapshot | read-only |
| `/browserops` | Browser specialist context — agent-browser guide, readiness, policy | read-only |
| `/linkedin_profile` | LinkedIn profile browser ops — status / open | read-only |
| `/linkedin_post` | Post to LinkedIn via the visible browser | **yes** — trailing approval phrase |
| `/linkedin_connect` | Send a LinkedIn connection request | **yes** — trailing approval phrase |
| `/reddit` | Reddit ops — research (read-only), comment / post | **yes** for comment/post |
| `/x` | X scout — scout / timeline / search the X timeline | read-only |
| `/social` | Social post queue — status, queue, draft, approve, reject, post, cadence | post is gated |
| `/linkedin` | Guided LinkedIn workshop — Cook Together or Run It for Me, revise copy/image, approve exact queue row | **yes** — only Approve & Post publishes |

## Cabinet & Team

Deep dive: [Cabinet Rooms](cabinet-rooms.md) · [Cabinet Voice](cabinet-voice.md) · [Team Room](team-room.md) · [Autonomous Team Scheduler](autonomous-team-scheduler.md) · [Convoy, Work Queue, And Mailbox](convoy-work-mailbox.md) · [Team Operations And Executor](team-operations-executor.md)

| Command | What it does | Sub-commands |
|---|---|---|
| `/cabinet` | Multi-persona text meeting orchestrator | `create` / `list` / `send <id> <text>` / `end <id>` |
| `/standup` | Send a standup question to the cabinet | `[question]` |
| `/discuss` | Start a discussion with a seed topic | `<topic>` |
| `/teamtick` | Run one autonomous team scheduler tick | `<team_id>` |
| `/teamroom` | Run the Growth Boardroom team workflow | `[--v2] [--runtime] <goal>` |
| `/team` | Alias for `/team room <goal>` | |

## Finance

| Command | What it does | Sub-commands |
|---|---|---|
| `/budget` | Personal finance snapshot | `status` / `bills` / `loans` / `accounts` / `transactions` / `spending` / `add bill` / `add loan` / `sync` / `import <csv>` / `connect` (Teller / Plaid) |
| `/forecast` | Forecast cash flow + bill timing | |

> No dedicated feature page yet — a Finance manual page is a planned follow-up.

## Integrations & Communication

Deep dive: [Direct Integration Capability Contract](direct-integration-capability-contract.md)

| Command | What it does | Layer |
|---|---|---|
| `/email` | Check Gmail — list, unread, urgent | router |
| `/pemail` | Check a personal Gmail account (read-only) | router |
| `/inbox` | Inbox briefing — prioritized TL;DR | router |
| `/cleanup` | Inbox cleanup — dry run, `/cleanup go` to execute | router |
| `/accounts` | Social media account connection status | router |
| `/send` | Send a draft email via Outlook | router |
| `/brief` | Quick briefing — `/brief all` for the full dashboard | router |
| `/calendar` | Check Google Calendar — today or upcoming | engine |
| `/tasks` | Check Asana tasks — mine, overdue, due soon | engine |
| `/slack` | Check or send Slack messages | engine |
| `/sheets` | Read or write Google Sheets | engine |
| `/docs` | Read a Google Doc | engine |
| `/drive` | Search or list Google Drive files | engine |
| `/circle` | Check the Circle community — posts, DMs, feed | engine |
| `/post` | Post to social media (`/post x ...`, `/post facebook ...`) | engine |

## Analytics & Signal

Deep dive: [Business Signal Engine](business-signal-engine.md)

| Command | What it does | Notes |
|---|---|---|
| `/gsc` | Google Search Console — queries, pages, CTR | operator |
| `/analytics` | Google Analytics — sessions, traffic, pages | operator |
| `/signal` | Business signal digest | `refresh` to run now |

## Content & Design

Deep dive: [Video Generation](video-generation.md) · [Video Learning](video-learning.md) · [Native Design](design-capability.md)

| Command | What it does | Layer |
|---|---|---|
| `/video` | Generate a branded video — guided wizard or `--style` | router |
| `/design` | Generate brand-grade single-file HTML pages and dashboards | router |
| `/image` (`/generate-image`) | Generate or edit an image | engine |
| `/owner-image` | Generate a saved persona rep image | engine |
| `/quote` | Generate an insurance quote via the configured quoting skill | engine |
| `/blog` | Generate a research-backed blog article | engine |
| `/tweet` | Draft an X post or thread | engine |
| `/instagram` | Create Instagram content — carousel or caption | engine |
| `/yt_script` | Write a YouTube video script | engine |
| `/shorts` | Write a YouTube Shorts script | engine |

## Developer Workflow & Tools

Deep dive: [Intent-PRD and Clutch Review](intent-prd-and-clutch.md) · [Context-Economy DX](context-economy-dx.md) · [Archon Workflows](archon-workflows.md)

The PIV (Prime → Implement → Validate) loop and utility commands are
engine-passed developer tooling:

| Command | What it does |
|---|---|
| `/prime` | Load codebase context and conventions |
| `/planning` | Create a research-backed implementation plan |
| `/implement` | Execute an implementation plan step by step |
| `/validate` | Run the 5-level validation pyramid (lint/types/tests) |
| `/review` / `/reviewfix` | Pre-commit code review / fix review findings |
| `/commit` | Create a git commit with conventional format |
| `/prd` | Create a Product Requirements Document |
| `/e2e` | End-to-end: prime + plan + implement + commit |
| `/sysreview` / `/execreport` | System review / post-implementation report |
| `/clutch` | CLUTCH orchestrator — multi-phase team execution with review gates |
| `/diagram` | Create an Excalidraw architecture diagram |
| `/pdf` | Work with PDFs — extract, merge, split, create |
| `/slides` | Generate a PPTX presentation |
| `/sop` | Create a runbook or technical documentation |

Skills layer (invoked as `/skill-name`): `/vault-ops` (the single consolidated
vault skill — orient/debrief/weekly, ingest, query, maintain, discover, build),
`/graphify` (queryable code+docs knowledge graph), `/direct-integrations`
(direct Gmail/Calendar/Asana/Slack/Sheets/Docs/Drive access), and the content /
documentation skills. Run the skill name directly in a coding session.

## Approval-gated writes

Anything that posts, sends, connects, or DMs to a real account is
**default-denied** and fires only on an exact trailing approval phrase, with an
audit row + screenshot receipt per attempt. The body can never approve itself —
the approval is a distinct final pipe segment.

| Command | Exact approval phrase (final pipe segment) |
|---|---|
| `/linkedin_post <feed_url> \| <body> \| …` | `post this to linkedin now` |
| `/linkedin_connect <profile_url> \| <note> \| …` | `send this linkedin connection request now` |
| `/reddit comment <thread_url> \| <body> \| …` | `post this comment to reddit now` |
| `/reddit post <subreddit> \| <title> \| <body> \| …` | `post this to reddit now` |

See [Social-Write Executor](social-write-executor.md) for the full write
contract.

## The `thehomie` Shell CLI

Slash commands run inside a chat session; the `thehomie` CLI is the shell-side
surface for session startup, health, and orchestration:

```bash
# Chat
thehomie chat                    # Interactive REPL
thehomie chat -q "hello"         # Single query, stdout response
thehomie chat -q "hello" -Q      # JSON output (machine/API contract)
thehomie chat --resume <id>      # Resume session by ID
thehomie chat -c                 # Resume most recent session
thehomie chat -m claude          # Force a specific provider/lane

# System
thehomie setup                   # Interactive onboarding wizard
thehomie setup --check           # Verify all integrations without changing anything
thehomie status                  # System health overview
thehomie status --json           # JSON health report
thehomie doctor                  # Deep diagnostics with actionable fix hints
thehomie desktop --shell         # Launch the Desktop dashboard app

# Multi-agent convoy
thehomie convoy create ...       # Create convoy with subtasks + deps
thehomie convoy list             # List convoys (optional: --status active)
thehomie convoy show <id>        # Convoy detail + subtask status
thehomie convoy dispatch <sid>   # Dispatch a subtask via executor
thehomie convoy complete <sid>   # Mark subtask complete
thehomie convoy fail <sid>       # Mark subtask failed
thehomie convoy cancel <id>      # Cancel convoy
thehomie convoy add-task <id>    # Add subtask to existing convoy

# Mailbox
thehomie mailbox send ...        # Send typed inter-agent message
thehomie mailbox inbox <agent>   # Check agent inbox
thehomie mailbox claim <agent>   # Claim deliveries
thehomie mailbox ack <did>       # Acknowledge delivery

# Team sessions
thehomie team list               # List active team sessions
thehomie team status <id>        # Team detail + members + mailbox backlog
thehomie team members <id>       # Member list with roles
thehomie team shutdown <id>      # Request graceful shutdown
thehomie team ping <id>          # Bump activity timestamp
thehomie team close <id>         # Force-close team session
```

## Source Of Truth Files

| Layer | Files |
|---|---|
| Command registry + categories | `.claude/chat/commands.py` (`COMMANDS`, `CATEGORIES`, `TELEGRAM_NATIVE_COMMANDS`, `CORE_INTENTS`) |
| Router-backed handlers | `.claude/chat/core_handlers.py` |
| Routing / dispatch | `.claude/chat/router.py`, `.claude/chat/extension_manager.py` |
| Skills | `.claude/skills/*/SKILL.md` |

## Public Export Status

Public-framework safe. Public export still goes through `scripts/sanitize.py`;
never copy manually.
