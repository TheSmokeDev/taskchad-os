The Repositories System is the dispatch arm for coding work across tracked repos. It combines private memory (vault-owned repo context) with profile config (runtime dispatch mode).

### Dual-Layer Structure

| Layer | Location | What It Holds |
|-------|----------|---------------|
| **Private memory** | `vault/memory/REPOSITORIES.md` + `repositories/*.md` | Dispatch defaults, workflow prefs, dispatch history, per-repo activity. Sanitizer-denied. |
| **Profile config** | `config.yaml` → `repositories:` section | Runtime dispatch mode per repo, Archon enabled flag. Validated by `repository_config.py`. |

### Key Files

| File | Purpose |
|------|---------|
| `.claude/scripts/repository_memory.py` | Read/validate repo index and per-repo pages, build briefing sections |
| `.claude/scripts/repository_config.py` | Profile-owned repo config, validation, config briefing builder |
| `vault/memory/REPOSITORIES.md` | Private repo index — slug table, dispatch defaults, page rules |
| `vault/memory/repositories/*.md` | Per-repo pages (6 active) — 6 required H2 sections each |

### Coding Dispatch Rule

For substantive coding work in tracked repos, dispatch through Archon with isolated worktrees. Resolve the repo slug via `REPOSITORIES.md` and read the matching per-repo page before dispatch. Skip Archon for trivial edits, read-only explanations, planning conversations, or urgent hotfixes. Full repo context, dispatch history, and workflow preferences are documented in `vault/memory/REPOSITORIES.md`.

### YourProduct Repo Family (voice ≠ website ≠ homie)

YourProduct is a **family of repos**, not one repo. Resolve the right one before touching code —
and never commit YourProduct product changes into `thehomie` (the Homie cockpit).

| Slug | GitHub | Stack / Deploy | Role |
|------|--------|----------------|------|
| `YourProduct-voice-platform` | your-github-user/YourProduct-voice-platform | Python/Docker → AWS EC2 (`tcvp-*` on box `203.0.113.10`) · **Dograh fork** | Voice agents — wf18/Ryan, telephony, engine |
| `YourProduct` | your-github-user/YourProduct | Next.js 15 → Vercel (`b2b-umbrella`) | Website YourProduct.com |
| `YourProduct-sites` | your-github-user/YourProduct-sites | Next.js → Vercel | Client demo sites (`client.YourProduct.com/<slug>`) |
| `thehomie` | thehomie-framework/thehomie | Python framework | The Homie **cockpit** — never commit product changes here |

**Drift warning (voice):** the `tcvp` box is NOT a git checkout. `telephony.py` and the Pipecat
engine run as bind-mounted patches under `/home/ec2-user/tcvp-patches/`, and workflow/tool/def
config is DB state — so `YourProduct-voice-platform` lags production. Deploy proof for voice is live
container health + run transcripts + DB values, not preview URLs. Full context:
`repositories/YourProduct-voice-platform.md`.

### Dispatch Defaults

1. Resolve the repo slug from REPOSITORIES.md before coding work
2. Read the matching per-repo page before dispatch
3. Prefer Archon with isolated worktrees for substantive work in tracked repos
4. Work in-session for trivial edits, read-only explanations, planning, urgent hotfixes
5. CLAUDE.md § Repositories System carries the dispatch rule for turn-1 visibility

### Integration Points

| Surface | File | What It Does |
|---------|------|--------------|
| **SessionStart injection** | `bootstrap.py:350-358` | `build_repository_briefing_section()` injects compact repo index + dispatch defaults |
| **Profile config briefing** | `bootstrap.py:355-358` | `build_repository_config_briefing()` injects per-repo runtime config when present |
| **Reflection routing** | `memory_reflect.py:361-370` | Daily reflection routes repo/codebase activity to per-repo pages (dispatch history, recent activity, workflow prefs) |
| **Flush capture** | `memory_flush.py:85-93` | Session flush captures repo slug, workflow, branch, outcome as daily-log bullets |

### Connection to Self-Map

The `homie-self-map` skill (`.claude/skills/homie-self-map/SKILL.md`) documents the framework's vertical slice architecture. The Repositories System tells the agent WHERE to dispatch work; the self-map tells it HOW the framework is structured.
