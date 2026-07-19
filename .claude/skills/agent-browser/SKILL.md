---
name: agent-browser
description: Use Vercel agent-browser for browser automation through a persistent visible Chrome or Chromium CDP session. Trigger when the user asks to use agent-browser, Vercel agent browser, real Chrome, non-headless browser automation, CDP, Hotbox, LinkedIn profile browser work, or authenticated browser workflows.
---

# Agent Browser

Use this skill when browser state matters and the workflow needs the user's logged-in, visible browser.

## Prime And Upstream Sources

Read the local deployment manual and load the installed CLI's version-matched
core skill before acting:

```powershell
Get-Content docs\browserops-agent-browser-manual.md
agent-browser skills get core --full
```

Use the official sources when commands, CDP behavior, sessions, bundled skills,
or known bugs may have changed:

- <https://agent-browser.dev/>
- <https://agent-browser.dev/commands>
- <https://agent-browser.dev/skills>
- <https://agent-browser.dev/cdp-mode>
- <https://github.com/vercel-labs/agent-browser>
- <https://github.com/vercel-labs/agent-browser/releases>

Check version drift without changing the machine:

```powershell
agent-browser --version
npm view agent-browser version
agent-browser skills list --json
```

Review intervening releases and get operator authority before running
`npm install -g agent-browser@latest`. Do not run `agent-browser install` for
this attach-only deployment; it downloads an Agent Browser-managed Chrome.
After an authorized update, reload `core`, run BrowserOps tests, and prove one
read-only `--cdp 18222` attachment.

## Contract

- Use one persistent visible browser per deployment.
- Attach through CDP. Do not launch a separate headless or test browser.
- Put `--cdp 18222` on every direct local command. Never use bare `open`,
  auto-connect, or upstream example port `9222` on this machine.
- Use current snapshot refs or allowlisted selectors and real Agent Browser
  `click`/`fill` commands. Never substitute `eval`, `HTMLElement.click()`,
  injected JavaScript, blind coordinates, or address-bar typing.
- Re-snapshot after navigation or DOM mutation and verify the resulting URL,
  selected state, or visible confirmation.
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
- CDP on port `18222`
- `agent-browser --cdp 18222 ...`
- sole launcher/keeper `SecondBrain-LinkedInChrome`
- deployment-local profile `%USERPROFILE%\.codex\browser-profiles\chrome-cdp-9222`
  (`9222` is a legacy directory suffix, not the active port)

Useful direct commands when operating from a terminal:

```powershell
agent-browser --cdp 18222 snapshot -i -c
agent-browser --cdp 18222 --session operator-desk tab --json
```

If CDP is unreachable, fail closed and diagnose the existing keeper. Do not
launch or restart Chrome from Agent Browser, add another keeper, warm the Agent
Browser daemon from the keeper, or fall back to Playwright/headless just to make
a test pass. Upwork uses the named attach-only session `operator-desk`
inside this same Chrome process/profile.

If raw `http://127.0.0.1:18222/json/version` health succeeds but Agent Browser
attach hangs, stop the worker. Foreground and reload the exact existing tab,
retry one read-only attachment, then keep the workflow paused if it still
fails. Never kill, relaunch, or duplicate Chrome as recovery.

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
