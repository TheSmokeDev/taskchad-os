# Security Policy

## Supported Versions

The Homie is in public preview. Security fixes land on `master` and the latest
release tag only; older alpha tags are not patched.

| Version | Supported |
|---|---|
| Latest release (`v0.1.x-alpha`) | ✅ |
| `master` | ✅ |
| Older tags | ❌ |

## Reporting a Vulnerability

Report vulnerabilities privately through GitHub's private vulnerability
reporting: the repository's **Security** tab → **Report a vulnerability**.
Please do not open a public issue for security reports.

Include what you can: the affected component (channel adapter, orchestration
API, browser executor, install scripts, …), reproduction steps, impact, and any
suggested fix.

This is an alpha project maintained on a best-effort basis. Expect an
acknowledgement within a few business days; fixes are prioritized by impact.
There is no bug bounty program.

## Scope

**In scope:** code in this repository — the chat router and channel adapters,
the cognition and memory pipelines, the orchestration service and its local
API, the runtime/provider layer, the dashboard and Desktop shell, and the
install scripts.

**Out of scope:** your own deployment configuration, credentials you supply for
optional integrations (Google, Asana, Slack, banking providers, …), and
vulnerabilities in third-party model providers or platforms the framework talks
to — report those upstream.

## Deployment Security Posture

Defaults are conservative; the README's
[Security & Data Handling](README.md#security--data-handling) section describes
them in full. Highlights:

- Local-first Markdown vault; no hosted service or account.
- Per-channel user/guild allowlists.
- External write actions (posting, sending, connection requests, DMs) are
  default-denied behind exact approval phrases, with an audit row per attempt —
  see the [Social-Write Executor](docs/manual/features/social-write-executor.md)
  contract.
- Durable identity/memory mutation passes a default-deny evidence and policy
  gate with an append-only ledger and rollback snapshots — see the
  [Living Self Manual](docs/the-living-self-manual.md).
- Telemetry (Langfuse, Sentry/GlitchTip) is opt-in and off by default.

## Hardening Checklist

Before exposing an instance beyond your own machine:

1. Set the channel allowlists (`TELEGRAM_ALLOWED_USER_IDS`,
   `DISCORD_ALLOWED_GUILDS` / `DISCORD_ALLOWED_USERS`) before sharing any bot
   token or invite link.
2. Keep environment files out of version control — the shipped `.gitignore`
   covers `.claude/scripts/.env` — and rotate any token you suspect leaked.
3. Set `ORCHESTRATION_API_TOKEN` if anything other than localhost can reach the
   orchestration API on port 4322.
4. If you enable Langfuse, prefer a self-hosted instance so traces stay inside
   your infrastructure.
5. Review [`install.sh`](install.sh) / [`install.ps1`](install.ps1) before
   piping them to a shell — they are short, plain scripts.
