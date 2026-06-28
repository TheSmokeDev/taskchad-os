# Multi-Channel Adapters

Status: active baseline, continuity locally proven, adapter startup live-proven
Owner: `.claude/chat/adapters/`
Last updated: 2026-06-28

## What It Does

The adapter layer normalizes external chat platforms into the shared Homie
message model. Platform-specific events become `IncomingMessage` objects with
stable user, channel, thread, message id, raw event, and attachment fields.

Telegram text, voice, photo, and document updates are adapter-owned ingress
paths. Discord text, voice/audio, image, and supported document uploads follow
the same normalized message contract. Document uploads are downloaded to local
temp storage, attached to the normalized message, and parsed by the chat engine
into attachment context for supported PDF, DOCX, CSV/TSV, Markdown, and text
files — prose formats (PDF, DOCX, Markdown, text) are read in full up to
env-tunable caps, delivered on the turn prompt, with partial reads disclosed;
CSV/TSV stay a deliberate tabular preview (first 60 rows, 12 cells per row,
120 chars per cell), not a full read. Local temp paths stay internal; model-visible attachment context
names the uploaded file and includes extracted text, not filesystem locations.
Telegram document albums with a shared media group id are buffered briefly and
queued as one normalized turn with multiple attachments.

The chat router buffers quick conversational bursts per user/channel/thread
before invoking the engine. Back-to-back messages become one turn. If a
follow-up arrives while a turn is already in flight, the router parks the
follow-up and shows operator controls:

- `Queue Next`: run the follow-up as the next turn after the current response.
- `Steer Current`: apply the follow-up as a revision/steer after the current
  response finishes, instead of treating it as an unrelated task.

Slash commands are explicit-only. The router only dispatches a command when the
trimmed message starts with an exact `/command` token followed by whitespace or
end-of-message. Pasted docs paths, URLs, and copied logs such as
`docs/foo/DESIGN.md`, `/opt/app/docs/index.html`, or `phone/email/locations`
must fall through to normal chat, not trigger `/design` or `/email`.

Native command surfaces share one baseline registry from
`.claude/chat/commands.py`. Telegram exposes that curated list directly.
Discord exposes the same flat list except where a platform-native typed wrapper
is better; `/vault` is a Discord command group with typed subcommands, but it
queues the same `/vault ...` router text used by Telegram and CLI. Hidden text
aliases like `/vaults` and `/vault-ops` only dispatch when they begin the
message as explicit slash commands.

Adapter startup is isolated per platform. Router startup connects configured
adapters concurrently with a bounded timeout so a stuck Telegram, Discord,
Slack, or relay connect cannot keep the other channels offline. Discord native
slash-command sync also runs asynchronously with its own timeout so gateway
connect and command registration do not block the rest of startup.

Long-running chat work uses a timeout handoff instead of pretending the turn
completed. When the engine hits `CHAT_ENGINE_TIMEOUT_SECONDS` (or the
attachment-specific timeout), the router posts an honest timeout response,
keeps the in-process engine task running, and writes a background-task record
with session, channel, thread, message id, request preview, status, and
timestamps. Completion, failure, and delivery failure update the same record.
Short follow-ups such as "still cooking?" or "how we looking?" answer from that
task status instead of generic recall. This is not yet a separate external job
worker, but it prevents the bot from claiming "nothing finished" while a
timeout handoff is still registered.

Chat continuity is local-first for generic runtimes. Do not assume Codex,
Gemini, or other generic lanes preserve server-side conversation state.
`SESSION_TURN_THRESHOLD=0` is the default and means "never reset by turn
count"; positive values are emergency caps only. The engine injects the latest
persisted transcript tail into working memory and the turn prompt so newest
Discord/Telegram turns survive long sessions and Windows system-append caps.

Sensitive email/inbox data pulls remain explicit slash-command actions;
natural-language chat should not auto-fetch Gmail/Outlook context.

The shared router also owns the natural-language external-action confirmation
gate across CLI, Telegram, Discord, Slack, WhatsApp, web, and downstream
persona channels. Pasted research, prospect lists, website snippets, Google
Maps results, contact-page URLs, scheduling links, phone-number CTAs, and
screenshotted/link-dump context are reference material and should reach the
engine or active persona as normal chat. The gate should only interrupt when
the operator is plausibly asking Homie to mutate a live surface or contact a
real person, such as sending a message, contacting a lead, booking an
appointment, posting/publishing, or deploying.

## Operator Entry Points

- Telegram bot channel
- Slack, Discord, WhatsApp, web relay, and CLI adapters when configured
- Health/status: the configured profile health port
  (`http://127.0.0.1:<health-port>/health`) and `thehomie status --json`

## Discord Configuration

The Discord adapter connects over the gateway and starts automatically whenever
`DISCORD_BOT_TOKEN` is set. The bot's `MESSAGE CONTENT INTENT` must be enabled in
the Discord Developer Portal — the adapter sets `intents.message_content = True`
in code, and without the matching portal toggle every message arrives blank.

| Env var | Effect |
|---|---|
| `DISCORD_BOT_TOKEN` | Bot token. Empty = adapter disabled. |
| `DISCORD_ALLOWED_USERS` | Comma-separated user IDs allowed to drive the bot. Empty = anyone. |
| `DISCORD_ALLOWED_GUILDS` | Comma-separated guild (server) IDs the bot operates in. Empty = all. |
| `DISCORD_WATCHED_CHANNELS` | Comma-separated channel IDs the bot auto-listens in without an `@mention`. |
| `DISCORD_WATCH_ALL_GUILD_CHANNELS` | `true` to auto-listen to every channel in the allowed guild(s) with no `@mention`. Scoped by `DISCORD_ALLOWED_GUILDS`; pair with `DISCORD_ALLOWED_USERS` to lock who can drive it. |

## Continuity Configuration

| Env var | Default | Effect |
|---|---:|---|
| `SESSION_TURN_THRESHOLD` | `0` | `0` disables turn-count resets; positive values force a new runtime session after that many persisted turns. |
| `RECENT_CONVERSATION_COUNT` | `80` | Latest persisted messages injected for local continuity. |
| `RECENT_CONVERSATION_MESSAGE_MAX_CHARS` | `2000` | Per-message clip for recent conversation context. |
| `REGION_BUDGET_RECENT_CONVERSATION` | `24000` | Recent-conversation prompt-region budget in tokens. |

In a guild, a message is handled when the bot is `@mention`ed, the channel is in
`DISCORD_WATCHED_CHANNELS`, or `DISCORD_WATCH_ALL_GUILD_CHANNELS` is on for that
guild. DMs are always handled. The user allowlist applies before any of these.

## Source Of Truth Files

| Layer | Files |
|---|---|
| Adapter protocol | `.claude/chat/adapters/base.py` |
| Shared message models | `.claude/chat/models.py` |
| Telegram adapter | `.claude/chat/adapters/telegram.py` |
| Discord adapter | `.claude/chat/adapters/discord.py` |
| Attachment parser | `.claude/chat/attachment_context.py` |
| Command registry | `.claude/chat/commands.py` |
| Command and safety gate registry | `.claude/chat/extension_manager.py` |
| Router, engine, task status | `.claude/chat/router.py`, `.claude/chat/engine.py`, `.claude/chat/background_tasks.py` |
| Transcript persistence | `.claude/chat/session.py` |
| Continuity state | `.claude/chat/cognition/continuity.py` |
| Windows launcher | `.claude/chat/run_chat.bat` |
| Tests | `.claude/scripts/tests/test_adapter_telegram.py`, `.claude/scripts/tests/test_adapter_discord.py`, `.claude/scripts/tests/test_attachment_context.py`, `.claude/scripts/tests/test_chat_runtime_engine.py`, `.claude/scripts/tests/test_chat_router_timeout.py`, `.claude/scripts/tests/test_cognition_continuity.py` |
| Public reference | `docs/adapters.md` |

## Safety Boundaries

- Attachments are external input. Treat filenames, captions, and file contents
  as untrusted data.
- Telegram and Discord document uploads are downloaded to local temp directories
  and passed to the engine as `IncomingMessage.attachments`; adapters do not
  execute file contents.
- Prose document text (PDF, DOCX, Markdown, plain text) is read in full up to
  env-tunable caps (`CHAT_ATTACHMENT_MAX_BYTES`, `CHAT_ATTACHMENT_MAX_CHARS`,
  `CHAT_ATTACHMENT_TOTAL_MAX_CHARS`) and rides the turn prompt, not the
  system-prompt append. CSV/TSV are parsed as a bounded tabular preview
  (60 rows / 12 cells / 120-char cells); the full-read char caps do not apply
  to them. Reads clipped by a cap are disclosed to the model as
  PARTIAL CONTENT with an instruction to tell the user only part was read.
  Unsupported files remain attachments, but are not parsed into model-visible
  document text.
- Do not expose local attachment temp paths in user-visible responses.
- Telegram document updates consumed before this handler existed do not replay
  automatically. Re-upload documents that were dropped by an older live bot.
- Gmail/Outlook and inbox triage are sensitive data surfaces. Use explicit
  `/email`, `/pemail`, `/inbox`, `/cleanup`, or `/brief` commands; conversational
  mentions of email/inbox do not auto-fetch mail.
- The external-action confirmation gate is shared by every adapter and runs
  before persona routing. It must not treat copied website/listing language
  such as "contact", "call", "schedule", or embedded contact-form links as
  operator intent by itself. Direct requests to send, contact, book, post,
  publish, deploy, or otherwise mutate live state still require the existing
  explicit authorization path.
- Photos, voice, and documents stay adapter-owned. Runtime/provider behavior
  remains behind the engine and runtime layers.
- Do not print bot tokens, raw Telegram update payload secrets, cookies, or
  browser state while proving adapter behavior.

## How To Run It

```powershell
cd .claude\chat
.\run_chat.bat
```

Check live health through the configured profile port:

```powershell
cd .claude/scripts
$port = uv run thehomie status --json | ConvertFrom-Json | ForEach-Object { $_.profile_lifecycle.health_check_port }
Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$port/health"
```

For restart verification, do not stop at HTTP 200. Confirm the bot PID file
points at a live process, the configured health port is owned by that process,
the health payload reports `status=ok` with expected adapters true, and the bot
log contains platform connection plus native command registration lines.

## How To Test It

```powershell
cd .claude/scripts
uv run python -m py_compile ..\chat\adapters\telegram.py
uv run python -m py_compile ..\chat\attachment_context.py ..\chat\adapters\discord.py ..\chat\engine.py
uv run pytest tests/test_adapter_telegram.py tests/test_adapter_discord.py tests/test_command_menu.py tests/test_attachment_context.py -q
uv run pytest tests/test_extension_manager.py tests/test_skill_intent_gates.py -q
uv run pytest tests/test_chat_runtime_engine.py tests/test_cognition_continuity.py tests/test_router_transcript_persistence.py tests/test_chat_router_timeout.py tests/test_config_reload.py -q
```

## Current Regression Proof

- Date: 2026-06-28
- Local proof: `py_compile` passed for the shared router, extension manager,
  main chat entrypoint, Discord adapter, and Telegram adapter.
- Focused cross-channel proof: `124 passed, 1 warning` across extension
  manager gates, router intent gates, Discord persona channels, persona
  capabilities, Discord adapter, and Telegram adapter.
- Coverage included pasted website/listing research reaching the engine across
  CLI, Discord, Telegram, Slack, web, and WhatsApp while direct external-action
  requests with reference links still require confirmation.
- Scope: local code and fixture proof for shared router behavior. Live adapter
  availability still depends on which configured runtimes are running.

## Previous Regression Proof

- Date: 2026-06-27
- Local proof: `py_compile` passed for the shared attachment parser, Discord
  adapter, chat engine, router, command registry, core handlers, and prompt-region
  modules.
- Combined requested regression lane passed: `261 passed, 28 warnings`.
- Coverage included chat runtime continuity, cognition continuity, transcript
  persistence, timeout handoff status, config reload, extension manager,
  command menu, Discord adapter, URL ingest, document ingest, and recall CLI.
- Live restart proof: a restarted Homie process reached `status=ok`; Telegram,
  Discord, and web adapters were true in `/health`; the bot log showed Telegram
  connected, Telegram native command registration, all adapters connected,
  Discord connected, and Discord native command registration.
- Scope: local code, fixture proof, and live adapter startup proof. Platform
  menu display can still depend on each client refreshing its native command UI.

## Previous Live Proof

- Date: 2026-06-04
- Local proof: `py_compile` passed for Telegram adapter, chat router, and
  extension manager.
- Focused tests: `tests/test_adapter_telegram.py`,
  `tests/test_extension_manager.py`, and `tests/test_skill_intent_gates.py`
  passed.
- Adjacent router/CLI tests: `tests/test_chat_router_timeout.py`,
  `tests/test_router_transcript_persistence.py`, and `tests/test_cli.py`
  passed.
- Live Telegram proof:
  - two rapid messages produced one combined turn and one response
  - a follow-up sent while the bot was already working produced `Queue Next`
    and `Steer Current` inline buttons
  - Telegram rendered those buttons side by side
  - tapping `Steer Current` produced a steer acknowledgement and a revision
    response
  - a three-document Telegram Web upload sent `batch-a.md`, `batch-b.md`, and
    `batch-c.md` as one attachment group
  - the live bot log showed three `Document saved` lines followed by one
    combined normalized turn:
    `[User uploaded 3 documents in one Telegram attachment group...]`
  - Telegram Web displayed one bot reply confirming all three documents were
    received and inspected as one combined attachment batch
  - no Gmail/inbox data pull occurred during the Queue/Steer or attachment
    batch proof windows

## Earlier Live Proof

- Date: 2026-06-03 23:41-23:42 America/Los_Angeles
- Surface: Telegram Web to the configured Telegram bot
- Input: a Markdown smoke document named `homie-telegram-doc-smoke.md`
- Result: the live adapter logged `Document saved`, queued a normalized
  document message, the runtime read the Markdown content, and Telegram Web
  displayed the final answer confirming the attachment was read.
- Health: `http://127.0.0.1:8787/health` reported `adapters.telegram=true`
  after restart.
- Local note: the Windows launcher shows a parent Python process plus the real
  child interpreter; the active child is the PID recorded in `.claude/chat/bot.pid`.

## Public Export Status

This feature page is public-framework safe. Public export must still go through
`scripts/sanitize.py`.

## Next Slices

- Decide whether selected text documents should be summarized by a deterministic
  preprocessor before runtime invocation.
