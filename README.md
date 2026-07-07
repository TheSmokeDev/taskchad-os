# The Homie

**An open-source, self-hosted cognitive agent OS — persistent memory, proactive monitoring, multi-agent orchestration, and a provider-agnostic runtime.**

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=flat-square)
![Public Preview](https://img.shields.io/badge/public%20preview-v0.1.0--alpha.1-blue?style=flat-square)
![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![Channels: 6](https://img.shields.io/badge/Channels-Telegram%20%C2%B7%20Slack%20%C2%B7%20Discord%20%C2%B7%20WhatsApp%20%C2%B7%20Web%20%C2%B7%20CLI-4A154B?style=flat-square)

The Homie runs on your own hardware — locally, on a VPS, or in Docker — and talks to you over Telegram, Slack, Discord, WhatsApp, the web, or the CLI. All six channels enter one canonical ingress and share one session model, one recall service, and one runtime. The framework runs the same on Claude, Codex, Gemini, or any OpenAI-compatible backend.

Underneath is a cognition stack rather than a linear chat loop: token-budgeted prompt assembly over an immutable working memory, tiered recall combining keyword and vector search with graph traversal, an operator-belief and contradiction engine, and identity files whose durable changes only land through a default-deny evidence and policy gate. Every one of those is a shipped, tested module — the [Architecture](#architecture) section and [docs/architecture.md](docs/architecture.md) map each claim to its implementation.

## Demo

![The Homie v0.1.0-alpha.1 dashboard product tour](https://github.com/TheSmokeDev/taskchad-os/releases/download/v0.1.0-alpha.1/the-homie-v0.1.0-alpha.1-demo-preview.gif)

45-second product tour: dashboard, Desktop Stack controls, Mobile Access, Browser Viewer, Work Queue, Convoy, Operating Room, and clean shutdown. A full-quality MP4 is attached to the [v0.1.0-alpha.1 release](https://github.com/TheSmokeDev/taskchad-os/releases/tag/v0.1.0-alpha.1).

## Key Features

| Capability | What it does |
|---|---|
| **Proactive monitoring** | A heartbeat checks your email, calendar, tasks, and metrics every 30 minutes; daily reflection and weekly synthesis promote what matters to long-term memory — running whether or not you are in a session. |
| **Persistent memory** | A local-first, Obsidian-compatible Markdown vault with hybrid search (FTS5 keyword + 768-dim vector + optional LLM re-rank) and graph-aware recall injected into every turn. A cross-session scratchpad carries open threads between sessions. |
| **Knowledge compilation** | Ingested documents are compiled into an interlinked knowledge graph — concept pages, connection articles, and contradiction flags — automatically during ingest, daily reflection, and weekly synthesis. |
| **Learning from experience** | Per-turn capture is staged and promoted through scheduled reflection. An operator-belief and contradiction engine models you from your own words; durable identity changes land only through a default-deny evidence and policy gate with rollback snapshots. |
| **Six channels, one brain** | Telegram, Slack, Discord, WhatsApp, web, and CLI all enter a single canonical ingress. Transport identity is separated from conversation identity, so sessions survive reconnects. |
| **Provider-agnostic runtime** | Claude SDK, OpenAI Codex, Gemini CLI, OpenRouter, or any OpenAI-compatible endpoint — with health-aware fallback, manual `/provider` and `/model` control, cost tracking, and retry on transient failures. |
| **Multi-persona teams** | Register specialized personas, each with its own identity, memory, tools, and voice; coordinate them in shared rooms with roster and turn order owned by the framework. |
| **Supervised browser automation** | A visible Chrome session you can watch live in the dashboard. Write actions such as posting and DMs are default-denied until explicitly approved, with an audit row per attempt. |
| **Multi-agent orchestration** | Dependency-tracked convoy DAGs with parallel release, a typed inter-agent mailbox with a claim/ack lifecycle, and team sessions with backend fallback — exposed on a local API. |
| **Observability** | One nested trace per message (opt-in Langfuse, self-hosted or cloud) covering recall, prompt assembly, runtime execution, and cost; optional Sentry/GlitchTip error capture. |

## Quick Install

```bash
# Linux/macOS/WSL
curl -sSL https://raw.githubusercontent.com/TheSmokeDev/taskchad-os/master/install.sh | bash
```

```powershell
# Windows PowerShell
irm https://raw.githubusercontent.com/TheSmokeDev/taskchad-os/master/install.ps1 | iex
```

Prefer to review before running? The installers are plain scripts: [`install.sh`](install.sh) · [`install.ps1`](install.ps1). Manual path:

```bash
git clone https://github.com/TheSmokeDev/taskchad-os.git
cd taskchad-os/.claude/scripts
uv sync
cp .env.example .env
uv run python setup_wizard.py
uv run thehomie chat
```

## Getting Started

```bash
thehomie chat                    # Start a conversation
thehomie setup                   # Configure providers and integrations
thehomie setup --check           # Verify setup without changing anything
thehomie status --json           # Machine-readable health report
thehomie doctor                  # Diagnostics with fix hints
thehomie desktop --shell         # Launch the Desktop dashboard app
thehomie team list               # Inspect team sessions
```

The full CLI and in-chat command catalog is in the
[Commands Reference](docs/manual/features/commands-reference.md).

## Requirements

- **Python 3.12+** with [uv](https://docs.astral.sh/uv/)
- **Node.js 22.12+** for the dashboard and Desktop v0 assets
- **At least one model provider** — Claude Code CLI (`npm install -g @anthropic-ai/claude-code`), Codex, Gemini, OpenRouter, or any OpenAI-compatible endpoint

Full prerequisites, channel credentials, and platform setup: [INSTALL.md](INSTALL.md).

## Architecture

```
CHANNELS                          COGNITIVE ENGINE                    RUNTIME (lane-first)
──────────                        ────────────────                    ─────────────────────
Telegram ─┐                       ChatRouter._handle_inner()          selection.py
Slack ────┤                            │                              lane_router.py
Discord ──┤  IncomingMessage      ConversationEngine                       │
WhatsApp ─┤  ──────────────→          ├─ Tier Gate (rules, no LLM)         ├─ Claude SDK (Max sub)
Web/MC ───┤                           ├─ Recall (dual search+graph)        ├─ Codex CLI (ChatGPT sub)
CLI ──────┘                           ├─ Region Assembly (frozen)          └─ openai-compatible
                                      │   identity · self · user · durable    (Gemini · OpenRouter ·
                                      │   working · recent_conversation       OpenAI · local)
                                      │   + dynamic regions
                                      ├─ Mental Process Detection
                                      ├─ Runtime dispatch              Health-aware fallback,
                                      └─ Post-response learning        manual /provider control,
                                                                       cost tracking, retry

MEMORY SUBSTRATE (the vault)      BACKGROUND PIPELINES               ORCHESTRATION
────────────────                  ────────────────────               ─────────────
Obsidian-compatible Markdown      Heartbeat ───── every 30 min       Convoy DAGs
  SOUL · SELF · USER · MEMORY     Reflection ──── 8 AM daily         Typed mailbox
  GOALS · WORKING · HEARTBEAT     Weekly ──────── Sunday 8 PM        Team sessions
  daily/ · weekly/                Dream ───────── post-weekly +      Backend fallback
  concepts/ · connections/                          on-demand          (auto → paperclip
  qa/ · raw/ · _state/            All via recall_service.recall()      → workflow → local)
  INDEX.md · LOG.md · MOCs        (sole entrypoint, Invariant I-3)   Local API :4322
Hybrid search (FTS5 + 768-dim
  BGE vector + LLM re-rank)       COMPILATION ENGINE
Memory graph (1-hop + hub boost)  ──────────────────
                                  entity_extractor.py (pure Python heuristic)
                                  Ingest → extract → compile → connect
                                  → contradict → reindex → log → archive
```

The framework is provider-agnostic by construction: provider invocation goes only through the runtime layer, and editor adapters (Claude Code project instructions, hooks, MCP bridges) are integration surfaces layered on top of the framework, not part of it. The 9-layer cognitive stack, the five product dimensions, and the framework invariants are documented in [docs/architecture.md](docs/architecture.md).

## The Memory Vault

The agent's persistent state is plain Markdown in an Obsidian-compatible vault (default `vault/memory/`, override with `HOMIE_VAULT_DIR`): identity files (`SOUL.md`, `SELF.md`, `USER.md`), long-term memory and goals (`MEMORY.md`, `GOALS.md`, `WORKING.md`), daily and weekly logs, and an auto-compiled knowledge graph (`concepts/`, `connections/`, `qa/`, `raw/`). Obsidian is optional — the framework only needs the files, and any editor works. Setup, file reference, search, and the compilation engine are documented in [docs/vault-setup.md](docs/vault-setup.md).

## Security & Data Handling

- **Local-first by default.** Memory lives in a plain-Markdown vault on your machine. There is no hosted service and no account; the framework only calls the model providers you configure.
- **Channel access control.** Each channel supports allowlists (`TELEGRAM_ALLOWED_USER_IDS`, `DISCORD_ALLOWED_GUILDS` / `DISCORD_ALLOWED_USERS`, Slack workspace install) so only approved users can talk to your instance.
- **Default-deny external writes.** Actions that post, send, connect, or DM on a real account are denied unless accompanied by an exact approval phrase, and each attempt produces an audit row and receipt. See the [Commands Reference](docs/manual/features/commands-reference.md#approval-gated-writes).
- **Gated identity mutation.** Changes to durable identity and memory files pass a default-deny evidence and policy gate — confidence floor, vault-confined evidence reads, secret rejection — with an append-only ledger and a rollback snapshot per apply. See the [Living Self Manual](docs/the-living-self-manual.md).
- **Secret guardrails.** Credential patterns are rejected before team-memory writes, and public exports run sanitizer and leak checks so private vault data and local tokens stay out of the framework.
- **Telemetry is opt-in.** Langfuse tracing and Sentry/GlitchTip error capture are off until you configure them, and Langfuse can point at a self-hosted instance so traces never leave your infrastructure.

Vulnerability reporting and the security policy: [SECURITY.md](SECURITY.md).

## Deployment

**Docker:**

```bash
cp .claude/scripts/.env.example .claude/scripts/.env
docker compose config
docker compose up    # bot + scheduler (heartbeat · reflection · weekly synthesis)
```

**Local or VPS:** run the setup wizard, then start the agent in the foreground or as a background process; Linux hosts can install the provided systemd unit and Windows hosts can register the scheduled jobs script. Step-by-step instructions, including background-job scheduling and log rotation, are in [INSTALL.md](INSTALL.md).

**Key configuration (`.env`):**

| Variable | Description |
|----------|-------------|
| `OWNER_NAME` | Your name — used in heartbeat prompts and memory |
| `HOMIE_VAULT_DIR` | Absolute path to your vault (default `vault/memory/`) |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_ALLOWED_USER_IDS` | Comma-separated user IDs allowed to chat |
| `HEARTBEAT_TIMEZONE` | IANA timezone (e.g. `America/Chicago`) |
| `LANGFUSE_SECRET_KEY` | Langfuse API key for observability (optional) |
| `ORCHESTRATION_API_TOKEN` | Bearer token for the local orchestration API (optional) |

Full reference: [INSTALL.md](INSTALL.md).

## Testing & Quality

The suite currently stands at **4,262 test functions across 230 files** in `.claude/scripts/tests/`. Counts are from the current export and will drift as the suite grows; reproduce them locally:

```bash
git ls-files '.claude/scripts/tests/test_*.py' | wc -l          # test files
grep -r "def test_" .claude/scripts/tests --include='test_*.py' | wc -l   # test functions
```

Coverage is concentrated where the complexity is:

| Subsystem | Tests |
|-----------|-------|
| Cognition + memory (recall, beliefs, episodes, briefs) | 606 across 25 files |
| Orchestration (convoy / mailbox / team / executor) | 322 across 13 files |
| Runtime + lane routing | 70 across 6 files |
| Memory pipelines | 54 across 4 files |
| Observability (Langfuse) | 27 |

Beyond unit coverage, releases are exercised through operator-loop and smoke testing:

- Fresh public Windows install smoke from a clean clone — install, setup check, real CLI chat, Desktop launch, route checks, clean shutdown.
- Desktop package/portable-app smokes and dashboard route smokes across all dashboard surfaces.
- Langfuse trace validation of the full message lifecycle, plus sanitizer/export leak checks before each public release ([LANGFUSE-PROOF.md](LANGFUSE-PROOF.md)).

Continuous integration is not wired up yet; until it is, treat the numbers above as self-reported and reproducible rather than CI-verified.

```bash
cd .claude/scripts
uv run pytest tests/ -v          # full active suite
uv run ruff check .              # lint
uv run ruff format .             # format
```

## Documentation

| Document | What it covers |
|---|---|
| [Install Guide](INSTALL.md) | Prerequisites, setup wizard, channel credentials, Docker, systemd, background jobs |
| [Operator Manual](docs/manual/README.md) | Public feature map, source-of-truth files, operator entry points, tests, proof boundaries |
| [Architecture](docs/architecture.md) | Slice map, the 9-layer cognitive stack, product dimensions, framework invariants, observability |
| [Security Policy](SECURITY.md) | Vulnerability reporting, scope, deployment hardening checklist |
| [Vault Setup](docs/vault-setup.md) | Vault layout, memory files, search, knowledge compilation, vault health |
| [Commands Reference](docs/manual/features/commands-reference.md) | Every CLI and in-chat command, approval-gated writes |
| [The Living Self Manual](docs/the-living-self-manual.md) | Belief formation, contradiction engine, cognitive pass, evolve adoption gate |
| [Desktop v0](docs/manual/features/desktop-v0.md) | Dashboard-first Electron app, portable/package smoke proof, lifecycle |
| [Multi-Channel Adapters](docs/manual/features/multi-channel-adapters.md) | Telegram attachments, grouped documents, quick-turn batching, Queue/Steer controls |
| [Runtime Status and Model Control](docs/manual/features/runtime-status-model-control.md) | `/provider`, `/model`, lane-first runtime behavior, quiet JSON contract |
| [How It Compares](docs/comparison.md) | Positioning relative to OpenClaw and Hermes Agent |
| `FRAMEWORK.md` | Compact development guide generated during public framework export |

## Project Status

The Homie is a **public preview** (`v0.1.0-alpha.1`). It is used daily by its maintainers, but interfaces and file layouts may change without notice until 1.0 — pin a release tag if you need stability. Releases are tagged and changes tracked in [CHANGELOG.md](CHANGELOG.md); the current version lives in [VERSION](VERSION).

### Known Limitations

- Desktop v0 proves the dashboard-first Electron app plus unpacked and portable no-admin Windows artifacts. A signed installer is not claimed yet.
- Fresh public Windows install smoke has proven install, setup check, real CLI chat, Desktop launch, route checks, and clean shutdown from a clean clone.
- Cabinet Voice has lifecycle controls and a partial LiveKit spike. The browser mic → transcript → Cabinet reply path is not claimed ready.
- Optional integrations require user-owned credentials. No private account data, local tokens, or machine-specific proof artifacts belong in the public export.
- Continuous integration (and a CI-backed test badge) is a planned follow-up; test counts are currently self-reported and reproducible.

## Support

- **Bugs and feature requests:** [GitHub Issues](https://github.com/TheSmokeDev/taskchad-os/issues)
- **Setup problems:** run `thehomie doctor` first — it diagnoses most configuration issues with fix hints — and check the [Install Guide](INSTALL.md) troubleshooting section.
- **Security reports:** see [SECURITY.md](SECURITY.md) — please do not open public issues for vulnerabilities.

This is an alpha project maintained on a best-effort basis; there is no commercial support or SLA.

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the contributor guide, test invocation, and style rules.

## Lineage & Provenance

The Homie is the original public Homie framework export, maintained by The Homie contributors. It evolved from Cole and the Dynamous Community's Claude Code workshop into an identity-first agent OS with its own memory, orchestration, and multi-channel ingress. OpenClaw, Hermes Agent, OpenSouls, and ClaudeClaw are credited as ecosystem influences; The Homie is an independent project and is not affiliated with, sponsored by, or endorsed by those projects. See [NOTICE.md](NOTICE.md) and [AUTHORS.md](AUTHORS.md).

## License

MIT — see [LICENSE](LICENSE). To cite this project in academic work, use [CITATION.cff](CITATION.cff).
