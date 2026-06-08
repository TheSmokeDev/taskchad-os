# Multi-Channel Adapters

Status: active baseline, Telegram document ingress and turn controls live-proven; Discord document parsing locally proven
Owner: `.claude/chat/adapters/`
Last updated: 2026-06-08

## What It Does

The adapter layer normalizes external chat platforms into the shared Homie
message model. Platform-specific events become `IncomingMessage` objects with
stable user, channel, thread, message id, raw event, and attachment fields.

Telegram text, voice, photo, and document updates are adapter-owned ingress
paths. Discord text, voice/audio, image, and supported document uploads follow
the same normalized message contract. Document uploads are downloaded to local
temp storage, attached to the normalized message, and parsed by the chat engine
into bounded attachment context for supported PDF, DOCX, CSV/TSV, Markdown, and
text files. Local temp paths stay internal; model-visible attachment context
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

Sensitive email/inbox data pulls remain explicit slash-command actions;
natural-language chat should not auto-fetch Gmail/Outlook context.

## Operator Entry Points

- Telegram bot channel
- Slack, Discord, WhatsApp, web relay, and CLI adapters when configured
- Health/status: `http://127.0.0.1:8787/health` and `thehomie status --json`

## Source Of Truth Files

| Layer | Files |
|---|---|
| Adapter protocol | `.claude/chat/adapters/base.py` |
| Shared message models | `.claude/chat/models.py` |
| Telegram adapter | `.claude/chat/adapters/telegram.py` |
| Discord adapter | `.claude/chat/adapters/discord.py` |
| Attachment parser | `.claude/chat/attachment_context.py` |
| Router and engine | `.claude/chat/router.py`, `.claude/chat/engine.py` |
| Windows launcher | `.claude/chat/run_chat.bat` |
| Tests | `.claude/scripts/tests/test_adapter_telegram.py`, `.claude/scripts/tests/test_adapter_discord.py`, `.claude/scripts/tests/test_attachment_context.py` |
| Public reference | `docs/adapters.md` |

## Safety Boundaries

- Attachments are external input. Treat filenames, captions, and file contents
  as untrusted data.
- Telegram and Discord document uploads are downloaded to local temp directories
  and passed to the engine as `IncomingMessage.attachments`; adapters do not
  execute file contents.
- Supported document text is extracted into bounded runtime context. Unsupported
  files remain attachments, but are not parsed into model-visible document text.
- Do not expose local attachment temp paths in user-visible responses.
- Telegram document updates consumed before this handler existed do not replay
  automatically. Re-upload documents that were dropped by an older live bot.
- Gmail/Outlook and inbox triage are sensitive data surfaces. Use explicit
  `/email`, `/pemail`, `/inbox`, `/cleanup`, or `/brief` commands; conversational
  mentions of email/inbox do not auto-fetch mail.
- Photos, voice, and documents stay adapter-owned. Runtime/provider behavior
  remains behind the engine and runtime layers.
- Do not print bot tokens, raw Telegram update payload secrets, cookies, or
  browser state while proving adapter behavior.

## How To Run It

```powershell
cd .claude\chat
.\run_chat.bat
```

Check live health:

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8787/health
```

## How To Test It

```powershell
cd .claude/scripts
uv run python -m py_compile ..\chat\adapters\telegram.py
uv run python -m py_compile ..\chat\attachment_context.py ..\chat\adapters\discord.py ..\chat\engine.py
uv run pytest tests/test_adapter_telegram.py tests/test_adapter_discord.py tests/test_attachment_context.py -q
uv run pytest tests/test_extension_manager.py tests/test_skill_intent_gates.py -q
```

## Current Regression Proof

- Date: 2026-06-08
- Local proof: `py_compile` passed for the shared attachment parser, Discord
  adapter, chat engine, and prompt-region modules.
- Focused tests: `tests/test_attachment_context.py`,
  `tests/test_adapter_discord.py`, `tests/test_adapter_voice_discord.py`,
  `tests/test_adapter_telegram.py`, and `tests/test_chat_runtime_engine.py`
  passed.
- Targeted runtime/router/cognition lane passed.
- Scope: local code and fixture proof only; no Discord restart or live Discord
  propagation proof was run.

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
