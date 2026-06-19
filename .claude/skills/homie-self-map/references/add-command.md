# Add a Slash Command to The Homie

4-registration checklist. Miss any step and the command silently breaks.

## Steps

### 1. Add COMMANDS row in `commands.py`

File: `.claude/chat/commands.py`, variable `COMMANDS` (~line 39).

```python
("mycommand", "Short description shown in /help", "router", "user"),
```

Fields: `(name, description, type, min_role)`. Type is `"router"` (handled in router, fast) or `"engine"` (forwarded to reasoning engine).

### 2. Add handler in `core_handlers.py`

File: `.claude/chat/core_handlers.py`, variable `CORE_HANDLERS` (~line 5019).

```python
"mycommand": handle_mycommand,
```

Write the handler function above the dict:

```python
async def handle_mycommand(adapter, incoming, args, *, collect_only=False):
    # Return a string response
    return "Done."
```

Signature must match: `(adapter, incoming, args, *, collect_only)`.

### 3. Add to TELEGRAM_NATIVE_COMMANDS

File: `.claude/chat/commands.py`, variable `TELEGRAM_NATIVE_COMMANDS` (~line 155).

Add the command name string to the tuple. This registers it in Telegram's bot menu autocomplete.

### 4. Add to CATEGORIES

File: `.claude/chat/commands.py`, variable `CATEGORIES` (~line 127).

Add the command name to the appropriate category group. This controls where it appears in `/help` output.

## Verification

After all 4 registrations:

1. Restart the bot: `cd .claude/chat && bash run_chat.sh`
2. Check the log line: `Registered N slash commands with Telegram` — N should increase by 1
3. Type `/mycommand` in Telegram to test

**Gotcha**: Telegram clients cache the command menu. After adding a new native command, users may need to restart their Telegram client to see it in autocomplete.

## Registration flow

`main.py:436` calls `manager.register_core_commands(COMMANDS, CATEGORIES, CORE_HANDLERS)` at startup — that's where all 4 pieces connect.
