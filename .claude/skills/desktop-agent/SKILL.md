---
name: desktop-agent
description: >
  Native Windows desktop bridge — breaks out of Claude Code's Git Bash sandbox to
  open terminal windows, launch URLs, open files, run commands, and send toast
  notifications on the user's actual desktop. Use when:
  (1) user says "open", "launch", "start", "show me", "pop up", "new window",
  (2) you need to open a terminal, browser, file, or app for the user,
  (3) you need to send a desktop notification,
  (4) any action that requires native Windows GUI access from the sandbox.
  Auto-starts on Windows login. Auto-starts on first command if not running.
---

# Desktop Agent

Native daemon at `~/.claude/live-chat/desktop_agent.py` that watches a command queue
and executes actions on the real Windows desktop. Runs as `pythonw` (no console window).

All commands go through `desktop_cmd.py` which auto-starts the agent if not running.

## Commands

```bash
DCMD="python ~/.claude/live-chat/desktop_cmd.py"

# Open a live chat terminal
$DCMD open-chat --ctx thehomie
$DCMD open-chat --ctx deployment-a
$DCMD open-chat                          # all contexts

# Open URL in default browser
$DCMD open-url --url https://example.com

# Open file in default app
$DCMD open-file --path "~/some-file.pdf"

# Run any command in a new visible terminal window
$DCMD run --command "npm start" --title "Dev Server"
$DCMD run --command "ssh root@ai.your-domain.example.com" --title "SSH"

# Windows toast notification
$DCMD notify --title "Build Done" --message "deployment finished"

# Agent management
$DCMD status                             # check if running + PID
$DCMD ensure                             # start if not running
```

## How It Works

1. `desktop_cmd.py` appends a JSON command to `~/.claude/live-chat/commands.jsonl`
2. `desktop_agent.py` (running natively via `pythonw`) polls the file every 500ms
3. Agent reads the command and executes it with full Windows desktop access
4. Single-instance: PID lock with ctypes `OpenProcess` (not broken `os.kill`)
5. Auto-start: VBScript in Windows Startup folder, also `desktop_cmd.py` starts it on demand

## Anti-Zombie Guarantees

- Single instance enforced via PID file + live process check (ctypes, not os.kill)
- `atexit` handler cleans PID file on exit
- Stale PID file from dead process gets overwritten (checks `STILL_ACTIVE` exit code)
- `subprocess.Popen` with `shell=True` for `start` commands — child terminals are independent
- Agent log at `~/.claude/live-chat/state/desktop-agent.log`

## Auto-Start

VBScript at `shell:startup` runs `pythonw desktop_agent.py` on Windows login.
No console window. If the agent dies, next `desktop_cmd.py` call restarts it.
