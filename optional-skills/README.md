# Optional Skills

A curated catalog of **opt-in** skills for TaskChad OS (The Homie Framework).

These skills are **not loaded by default**. The always-on skill set lives in
`.claude/skills/` and is part of every session. The skills in this directory are
extras you install deliberately — when a particular workflow becomes relevant —
so the context window stays lean for everyone who doesn't need them.

This mirrors the pattern from
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent/tree/main/optional-skills):
a browsable, category-organized hub of capabilities you graft onto the agent on
demand.

## Philosophy

> The context window is a public good.

Every always-loaded skill spends tokens in *every* session, whether or not it is
used. Optional skills pay that cost only when installed. Keep `.claude/skills/`
to the capabilities you use constantly; reach into `optional-skills/` for the
rest.

## Catalog

| Category | Skill | What it does |
|----------|-------|--------------|
| `research/` | `duckduckgo-search` | Free web search (text, news, images) — no API key |
| `research/` | `arxiv-digest` | Search arXiv and summarize papers into the vault |
| `productivity/` | `daily-brief` | Compose a morning briefing from calendar, tasks, and memory |
| `productivity/` | `flashcards` | Turn vault notes into spaced-repetition flashcards |
| `communication/` | `whatsapp-bridge` | Add WhatsApp as a chat channel |
| `communication/` | `voice-transcribe` | Transcribe inbound voice notes before the agent replies |
| `finance/` | `crypto-watch` | Track crypto prices and fire threshold alerts |
| `devops/` | `heartbeat-monitor` | External uptime check for the bot + memory pipelines |
| `devops/` | `log-triage` | Summarize bot/Langfuse logs into an incident digest |
| `security/` | `secret-scan` | Scan the vault and repo for leaked secrets before commit/send |
| `creative/` | `meme-generator` | Generate captioned memes for social replies |
| `mcp/` | `mcp-bridge` | Connect arbitrary MCP servers as on-demand tool sources |

## Anatomy of an optional skill

Each skill is a self-contained folder:

```
optional-skills/<category>/<skill-name>/
├── SKILL.md          (required) — frontmatter + instructions
├── scripts/          (optional) — executable helpers
├── references/       (optional) — docs loaded on demand
└── assets/           (optional) — files used in output
```

The `SKILL.md` frontmatter carries catalog metadata under `metadata.taskchad`
(tags, related skills, category) so the hub stays browsable.

## Installing a skill

Optional skills become active only once they live under `.claude/skills/`.

```bash
# From the repo root — enable a single skill
cp -r optional-skills/research/duckduckgo-search .claude/skills/

# Restart the chat process so the new skill is registered
cd .claude/chat && bash run_chat.sh
```

To remove it again, delete the copied folder from `.claude/skills/`.

> **Tip:** symlink instead of copy (`ln -s ../../optional-skills/... .claude/skills/`)
> if you want catalog updates to flow through automatically.

## Security invariant

Every optional skill that mutates the outside world — posts, sends, connects,
spends — must honor the framework's **default-deny mutation policy**: ship the
mutating path gated behind an explicit capability flag with an audit trail. See
"Default-Deny Mutation Policy" in `.claude/sections/01_architecture.md`. Skills
in this catalog that touch external surfaces flag the required gate in their
`SKILL.md`.

## Contributing a new optional skill

1. Pick (or add) the right category folder.
2. Scaffold with the always-on `skill-creator` skill, then move the result here.
3. Add `metadata.taskchad` frontmatter (tags, category, related_skills).
4. Add a row to the catalog table above.

License: MIT
