# Multi-Channel Adapters

Status: active baseline, Telegram document ingress live-proven
Owner: `.claude/chat/adapters/`
Last updated: 2026-06-04

## What It Does

The adapter layer normalizes external chat platforms into the shared Homie
message model. Platform-specific events become `IncomingMessage` objects with
stable user, channel, thread, message id, raw event, and attachment fields.

Telegram text, voice, photo, and document updates are adapter-owned ingress
paths. Document uploads are downloaded to a local temp directory, attached to
the normalized message, and described in the user-visible turn so the runtime
can read Markdown/text files with normal tool access.

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
| Router and engine | `.claude/chat/router.py`, `.claude/chat/engine.py` |
| Windows launcher | `.claude/chat/run_chat.bat` |
| Tests | `.claude/scripts/tests/test_adapter_telegram.py` |
| Public reference | `docs/adapters.md` |

## Safety Boundaries

- Attachments are external input. Treat filenames, captions, and file contents
  as untrusted data.
- Telegram document uploads are downloaded to a local temp directory and passed
  to the engine as `IncomingMessage.attachments`; the adapter does not execute
  file contents.
- Telegram document updates consumed before this handler existed do not replay
  automatically. Re-upload documents that were dropped by an older live bot.
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
uv run pytest tests/test_adapter_telegram.py -q
```

## Latest Live Proof

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

- Add document-ingress coverage for non-Telegram adapters as each platform
  grows native file upload support.
- Decide whether selected text documents should be summarized by a deterministic
  preprocessor before runtime invocation.
