---
name: agent-browser
description: Use Vercel agent-browser for browser automation through a persistent visible Chrome or Chromium CDP session. Trigger when the user asks to use agent-browser, Vercel agent browser, real Chrome, non-headless browser automation, CDP, Hotbox, LinkedIn profile browser work, or authenticated browser workflows.
---

# Agent Browser

Use this skill when browser state matters and the workflow needs the user's logged-in, visible browser.

## Contract

- Use one persistent visible browser per deployment.
- Attach through CDP. Do not launch a separate headless or test browser.
- Do not copy cookies, profiles, raw tokens, tabs, `.env` files, or service credentials between machines.
- Do not scrape or print secrets from browser storage.
- Do not perform external writes such as posts, DMs, connection requests, purchases, or profile edits unless the user explicitly asks for that exact action.

## First Check

From the Homie chat surface, prefer deterministic router checks before model-driven browser work:

```text
/browser status
/browser tabs
```

Use `/linkedin_profile status` only for the LinkedIn-specific wrapper. It uses the same browser helper contract.

## Local Windows Backend

Expected local backend:

- real Chrome
- visible window
- CDP on port `9222`
- `agent-browser --cdp 9222 ...`

Useful direct commands when operating from a terminal:

```powershell
agent-browser --cdp 9222 snapshot -i -c
agent-browser --cdp 9222 open https://www.linkedin.com/
```

If CDP is unreachable, restart real Chrome with remote debugging from the start. Do not fall back to Playwright/headless just to make a test pass.

## Linux / VPS Backend

The production reference uses:

- `qm-chromium.service` for persistent Chromium
- `xvfb-99.service` for the visible virtual display
- `/tmp/ab.sh` as the safe wrapper
- `hotbox-cdp-stream.service` for viewer streaming

Use the wrapper on VPS:

```bash
/tmp/ab.sh snapshot -i -c
/tmp/ab.sh open https://www.linkedin.com/
```

Treat VPS browser state as deployment-local. Do not copy it into the repo or local machine.

## Viewer

Hotbox is the proven VPS reference for watching the remote browser. The framework viewer target is Mission Control or Hub, but local Hotbox work is not part of the first slice.

## Output Discipline

When reporting browser state:

- say whether CDP is reachable
- say whether the visible/non-headless guard passed, failed, or was unknown
- redact URL query strings and fragments in tab lists
- keep command output concise
- surface blockers plainly instead of claiming browser readiness
