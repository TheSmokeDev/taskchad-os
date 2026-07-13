---
name: secret-scan
description: Scan the vault and repo for leaked secrets — API keys, tokens, private keys — before committing or sending content externally. Use before a commit/push, before posting vault content to any external surface, or when the user asks to check for exposed credentials.
version: 1.0.0
author: YourProduct OS
license: MIT
platforms: [linux, macos, windows]
metadata:
  YourProduct:
    category: security
    tags: [secrets, security, scanning, credentials, pre-commit]
    related_skills: [log-triage]
    mutates: false
---

# Secret Scan

Catch credentials before they leak into a commit or an outbound message.

## Run

```bash
uv run python optional-skills/security/secret-scan/scripts/scan.py .
# scan a specific path before sending it out:
uv run python optional-skills/security/secret-scan/scripts/scan.py vault/notes/draft.md
```

Exit code is non-zero if anything matches, so it drops into a pre-commit hook or
a pre-send guard.

## What it flags

Common high-signal patterns: AWS keys, Google API keys, Slack/Telegram/GitHub
tokens, OpenAI/Anthropic keys, private-key PEM blocks, and generic
`SECRET`/`PASSWORD`/`TOKEN = "..."` assignments. The detector lives in
`scripts/scan.py` — extend the pattern table there as new providers appear.

## Where this belongs in the framework

This is the read-only half of the framework's **default-deny mutation policy**.
Before any skill sends vault content to an external surface, run this scan on the
exact payload. A match means **stop and ask the user**, never auto-send. The
scan never edits or redacts on its own — it reports; a human decides.

## Reducing false positives

- Respect `.gitignore` / `.graphifyignore`; skip `.env*`, `node_modules`, `.git`.
- Example/placeholder values (`xxxx`, `your-key-here`, `example`) are downranked.
- When in doubt, surface the finding with `file:line` and let the user judge.
