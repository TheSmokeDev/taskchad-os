---
name: telegram-bot-test
description: E2E test the The Homie Telegram bot (@YourBot) via agent-browser on Telegram Web. Opens the bot chat, sends real messages by typing in the browser, verifies responses visually and via bot.log. Use when verifying bot features after code changes, testing cognitive architecture (Move 1/2/3), or running regression checks. Triggers on "test the bot", "run bot tests", "verify telegram", "test cognitive", "bot e2e".
---

# Telegram Bot E2E Test

Test the The Homie bot by chatting with it on Telegram Web via agent-browser.

## Prerequisites

1. Bot running: `cd .claude/chat && bash run_chat.sh`
2. agent-browser daemon running (start if not): `node "$(npm root -g)/agent-browser/dist/daemon.js" &`
3. Telegram Web logged in (user should already be logged in)

## Workflow

### 1. Open Telegram Web and navigate to YourAgent bot

```bash
npx agent-browser open https://web.telegram.org/a/
npx agent-browser snapshot -i -c    # find YourAgent chat link
npx agent-browser click @eN          # click YourAgent chat
```

### 2. Send a message (type in the browser input)

```bash
npx agent-browser snapshot -i -c -s "[contenteditable]"   # find input ref
npx agent-browser click @e1                                 # focus input
npx agent-browser keyboard type "your test message here"    # type message
npx agent-browser press Enter                               # send
```

IMPORTANT: Use `keyboard type` (not `type @ref`). Telegram Web uses contenteditable divs — `type @ref` can timeout. `keyboard type` sends real keystrokes.

### 3. Wait for response and verify

```bash
sleep 10                                                    # wait for bot to respond
npx agent-browser screenshot /path/to/screenshot.png        # visual check
```

Then check bot.log for cognitive events:
```bash
tail -15 .claude/chat/bot.log | grep -E "\[Process\]|\[Recall\]|Runtime"
```

### 4. Evaluate pass/fail

- **PASS**: Bot responded with relevant content (visible in screenshot) AND expected log events appeared
- **FAIL**: Bot returned error message, no response, or wrong cognitive mode detected

## Test Cases

Run each test by sending `/new` first (reset session), then the test message.

### Process Detection (Move 3)

| Mode | Message to Send | Expected in bot.log |
|------|----------------|-------------------|
| Planning | "how should we approach the brand fleet SEO?" | `[Process].*new=planning` |
| Monitoring | "check on the server health" | `[Process].*new=monitoring` |
| Learning | "remember that the teller cert expires next week" | `[Process].*new=learning` |
| Execution | "build the weekly synthesis report" | `[Process].*new=execution` |
| Default | "what's up with the recovery campaigns?" | No `[Process]` line (stays default) |

### Recall Pipeline

| Test | Message | Expected |
|------|---------|----------|
| Tier 1 recall | "what do we know about Wells Fargo?" | `[Recall].*tier=tier_1.*results=\d+` |
| Tier 0 skip | "hi" | No `[Recall]` line |

### Slash Commands

| Test | Message | Expected |
|------|---------|----------|
| Session reset | "/new" | "Session cleared" in chat |
| Budget check | "/budget" | Budget snapshot response |

## Gotchas

- On Windows, `/new` in Git Bash expands to `C:/Program Files/Git/new`. Type it in the browser instead.
- `keyboard type` is for real keystrokes. `type @ref "text"` is for filling form fields. Telegram needs `keyboard type`.
- Bot processes messages from the USER only. The Telegram Bot API `sendMessage` sends AS the bot — useless for testing.
- After restarting the bot, old log entries are gone. Check `bot.log` only for events after the restart.
