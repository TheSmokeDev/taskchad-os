# Talk Mode — start here

Give a second brain that's already running a real voice. You talk to it
like it's on a call. It hears you live, not voice notes, and it remembers
the whole conversation after you hang up. Works like ChatGPT Voice on the
Codex app, except it's your own second brain and it's open source.

Two doors:
- **Dashboard** — open the `/talk` page and start talking. Zero install.
- **Discord** — drop `/talk join` in a voice channel and steer a running
  agent mid flight, out loud.

It rides your own Codex subscription instead of metering an API key.

## Install it

This is a Claude Code skill. If you already run a second brain with a
Claude Code setup:

1. Make sure this folder lives at `.claude/skills/talk-mode-setup/` in your
   project (it does if you cloned the repo).
2. Tell your assistant: **"run talk-mode-setup"** — or just **"give my
   second brain a voice."**
3. It runs an interactive preflight, detects what's already configured, and
   asks only about what's missing. Every change is shown before it's made.

Your assistant reads `SKILL.md`, you answer a few questions, voice is on.

## What's in here

| File | What it is |
|------|-----------|
| `SKILL.md` | The skill your assistant reads and runs. |
| `scripts/preflight.py` | The detector. Checks your `.env`, OS, and venv so the skill only asks about gaps. |
| `references/troubleshooting.md` | Fixes for "the bot can't hear me" and "`/talk join` fails". |

## The full story

Design, the receive-pipeline fixes, and every knob:
**[docs/talk-mode-showcase.md](../../../docs/talk-mode-showcase.md)**
