# Bot Self-Restart

Status: Active baseline (live-proven)
Owner: runtime-chat
Last updated: 2026-06-14

## What It Does

The `/restart` command makes the chat bot relaunch itself: it acknowledges in
chat, hands off to a detached relauncher process, exits, and a fresh bot comes
back online on the same profile within a few seconds. A process cannot reliably
kill and revive itself, so the actual relaunch is done by a separate launcher
that outlives the dying bot.

## Operator Entry Points

- Chat/Telegram: `/restart`
- CLI: `python .claude/chat/relaunch.py` (runs the relauncher directly — useful
  for testing or recovering a wedged bot)
- Dashboard: n/a
- API: n/a

## How It Works

1. `/restart` replies "Restarting myself... back in a few seconds," then spawns
   the relauncher **detached** (Windows `CREATE_NEW_PROCESS_GROUP |
   DETACHED_PROCESS`, POSIX `start_new_session`) so it survives the bot's exit,
   and the old bot exits.
2. The relauncher waits for the old bot to disappear (polling the active
   profile's bot PIDs, with a timeout), force-cleans any straggler, then spawns
   a fresh bot.
3. The fresh bot is launched with the same Python interpreter and the SAME
   profile (`HOMIE_HOME` flows through unchanged), but with the host's
   nested-session markers scrubbed so the underlying Agent SDK does not refuse
   to start ("cannot be launched inside another agent session").

This is pure Python — no shell dependency — and the handoff is deterministic:
the old bot is gone before the new one acquires the single-instance lock and
opens its channel poller, so there is no double-poll conflict.

## Source Of Truth Files

| Layer | Files |
|---|---|
| Relauncher | `.claude/chat/relaunch.py` |
| Chat handler | `.claude/chat/core_handlers.py` (`handle_restart`) |
| Detached spawn helper | `.claude/scripts/shared.py` (`spawn_detached`) |
| Nested-marker scrub | `.claude/scripts/runtime/subprocess_env.py` (`scrub_nested_claude_state`) |
| Profile-aware paths | `.claude/scripts/personas/services.py` (`get_log_dir`) |
| Tests | `.claude/scripts/tests/test_bot_restart.py` |

## Safety Boundaries

- Restarts ONLY the bot belonging to the active profile; the cleanup step is
  profile-scoped and never touches another profile's bot.
- Preserves the running profile and credentials; it sheds only the host's
  nested-session markers (so the relaunched SDK starts cleanly).
- Does not change configuration, model, or lane selection — a restart simply
  picks up whatever the on-disk config currently says.
- `/restart` cannot be chained with other commands (it runs alone).

## How To Run It

```text
/restart
```

```powershell
# Direct relaunch (test, or recover a wedged bot):
python .claude/chat/relaunch.py
```

## How To Test It

```powershell
cd .claude/scripts
uv run pytest tests/test_bot_restart.py -q
```

End-to-end: with the bot running, invoke `/restart` (or run the relauncher
directly) and confirm the log shows the old bot stopping and a fresh bot
reaching "all adapters connected" within a few seconds, with a new process id
and no nested-session error.

## Latest Live Proof

- Date: 2026-06-14
- Surface: chat `/restart` + direct relauncher
- Result: old bot stopped, fresh bot reconnected its channel ~5s after spawn,
  no nested-session error.

## Public Export Status

public-exported.

## Next Slices

- Optional graceful channel-poller close before exit for an even faster handoff.
- Optional `/restart` confirmation message once the new bot is back online.
