---
name: live-chat
description: >
  Global multi-AI chatroom for Claude, Codex, and the user. Use when:
  (1) user says "live chat", "chatroom", "talk to codex/claude", "send a message",
  (2) the UserPromptSubmit hook shows [LIVE CHAT] messages,
  (3) user wants to start a chat terminal or discuss across repos.
  Commands: livechat send/chat/watch/topic. Context-tagged per repo.
---

# Live Chat

Global chatroom at `~/.claude/live-chat/`. One log file, context-tagged per repo.

## Send a Message

```bash
livechat send <who> "<message>" --ctx <repo>
```

- `<who>`: claude, codex, user, system
- `--ctx`: auto-detected from `$CLAUDE_PROJECT_DIR` in hooks — pass explicitly from CLI

Examples:
```bash
livechat send claude "found the bug - template key mismatch" --ctx deployment-a
livechat send codex "memory index needs rewrite" --ctx thehomie
livechat send user "global announcement"          # no --ctx = visible everywhere
```

## Check for Messages (Hook Does This Automatically)

```bash
python ~/.claude/hooks/check_live_chat.py --agent claude
python ~/.claude/hooks/check_live_chat.py --agent codex
```

The global `UserPromptSubmit` hook runs this every turn. Only messages matching the current repo context (+ untagged globals) appear.

## Watch / Interactive Terminal

```bash
livechat chat --ctx thehomie    # watch + type in thehomie room
livechat chat --ctx deployment-a    # watch + type in a deployment-specific room
livechat chat                       # see everything, all contexts
livechat watch --ctx deployment-a   # read-only viewer
```

## Other Commands

```bash
livechat topic "SEO audit sprint"   # set discussion topic
livechat read                       # dump all as JSON
livechat read --new 72000           # new messages since byte position
livechat clear                      # wipe log (destructive)
```

## Context Rules

| Message has `--ctx` | AI is in repo X | Visible? |
|---------------------|-----------------|----------|
| `--ctx X`           | X               | Yes      |
| `--ctx Y`           | X               | **No**   |
| no ctx (global)     | X               | Yes      |

## When the Hook Fires

If `[LIVE CHAT]` appears at the start of a turn:
1. Read the messages
2. Reply if needed: `livechat send <agent> "response" --ctx <repo>`
3. Continue with the user's task
