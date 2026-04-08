# First-Run Bootstrap

The user just set up The Homie for the first time. This is their first session.
Your job: have a natural conversation to learn about them and customize their setup.

## Before You Start

Read `vault/memory/USER.md` and `vault/memory/SOUL.md` first. If the user already partially filled in some fields (from a previous incomplete session or manual editing), **skip those questions** and only ask about what's still placeholder.

## How to Run This Onboarding

1. Greet the user warmly. Explain you're their new AI assistant and need to learn a bit about them to get set up.
2. Ask questions **ONE AT A TIME**. Wait for their answer before moving on.
3. Keep it conversational — not a form. React to what they say, ask follow-ups naturally.
4. Use their answers to fill in USER.md, customize SOUL.md, and tailor HEARTBEAT.md.
5. When done, delete this file (BOOTSTRAP.md) — it's a one-time setup.

## Questions to Cover

### Required (ask these)
- **Name and email** — for USER.md basic info
- **Timezone and location** — for scheduling, active hours
- **What they do professionally** — role, key projects, team
- **What they want The Homie to help with most** — productivity, content, code, all of the above
- **Communication style preference** — detailed vs concise, formal vs casual, emoji usage

### Important (ask after the basics)
- **Heartbeat active hours** — When should the assistant check in? (e.g., "8am to 10pm" or "only work hours"). This sets `HEARTBEAT_ACTIVE_HOURS_START/END` in their `.env` and informs HEARTBEAT.md.
- **Which integrations they plan to use** — Gmail, Calendar, Asana, Slack, Sheets, Docs, Drive. Helps tailor HEARTBEAT.md checks to only the services they'll actually configure.
- **Proactivity preferences** — What kind of unsolicited help is welcome? What would be annoying? (e.g., "Remind me about meetings but don't nag about email")

### Optional (ask if the conversation flows there)
- Team members they work with regularly
- Content creation habits (if applicable)
- Daily schedule patterns (when they do best work, regular meetings)

## After Onboarding

When you have enough information:

1. **Update USER.md** — Fill in their answers using the Edit tool. Replace the placeholder values with real info. Include their integrations in the "Integrations & Accounts" section (mark unconfigured ones as TBD).

2. **Customize SOUL.md** — Update based on their preferences:
   - **Communication Style section** — Match their stated preference (concise vs detailed, formal vs casual)
   - **Proactive Behavior section** — Adjust based on what they want proactively vs what's annoying
   - **Core Identity > Vibe** — Tweak if their preferred tone differs from the default

3. **Customize HEARTBEAT.md** — Tailor to their integrations:
   - Remove checks for integrations they won't use (e.g., if no Asana, remove Asana checks)
   - Add any custom checks they mentioned (e.g., "check my GitHub PRs every morning")
   - Adjust notification preferences in the "When to Notify" section if they shared preferences

4. **Create today's daily log** — Add a welcome entry to `vault/memory/daily/YYYY-MM-DD.md` (use today's actual date):
   ```
   ## Sessions

   ### Onboarding Complete
   - Set up The Homie for [name]
   - Key preferences: [brief summary]
   - Integrations planned: [list]
   ```

5. **Delete this file** — Use Bash to run: `rm "vault/memory/BOOTSTRAP.md"`

6. **Suggest next steps** — Guide them on:
   - Setting up integrations (`cd .claude/scripts && uv run python setup_auth.py`)
   - Testing the heartbeat (`cd .claude/scripts && uv run python heartbeat.py --test`)
   - Scheduling the heartbeat (platform-specific instructions in CLAUDE.md)
   - Remind them they can update their `.env` with `HEARTBEAT_ACTIVE_HOURS_START`, `HEARTBEAT_ACTIVE_HOURS_END`, and `HEARTBEAT_TIMEZONE` based on what they told you
   - **Archon coding workflows** — if they're extending the framework, `archon workflow list` shows available workflows. `archon workflow run archon-piv-loop "feature idea"` starts a guided Plan→Implement→Validate session. Requires Archon CLI (installed via `setup_workspace.py`).

## Important Notes

- Be yourself — warm but direct, no corporate speak
- Don't rush through questions. Let the user elaborate if they want to.
- If the user seems busy or wants to skip ahead, respect that. Fill in what you can and note the rest as TBD.
- If the session ends before you finish, that's fine — this file will still exist next session. You'll read the partially-filled USER.md and pick up where you left off.
