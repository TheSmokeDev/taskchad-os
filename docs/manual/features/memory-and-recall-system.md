# Memory And Recall System

Status: Active baseline
Owner: Memory pipelines + recall service (`.claude/chat/recall_service.py`, `.claude/chat/cognition/`, `.claude/scripts/memory_*.py`)
Last updated: 2026-06-27

## What It Does

The Homie's long-term memory is an Obsidian-style vault of Markdown notes. That
vault is the canonical source of truth; everything derived from it is a cache.
A local SQLite index (`memory.db`) holds an FTS5 keyword index plus 768-dim BGE
vector embeddings (`BAAI/bge-base-en-v1.5`) of every note, fully regenerable
from the vault at any time.

Two things sit on top of that index:

1. **Five background pipelines** keep memory fresh (sense, promote, synthesize,
   consolidate, compile).
2. **One unified recall service** is the single entrypoint every consumer uses
   to read from memory — the chat engine, the heartbeat, daily reflection,
   weekly synthesis, the `/vault-ops` skill, and the `thehomie recall` CLI all
   call the same function. There is no second search path (Invariant I-3).

The background pipelines each have their own deep pages; this chapter is about
**recall** — how memory is read, what the `thehomie recall` CLI does, the
multi-vault layout, and how the slash commands tap it.

| Pipeline | Cadence | Job | Deep page |
|---|---|---|---|
| Heartbeat | every 30 min | proactive sense loop over calendar/email/tasks + ambient observations | [Heartbeat Runtime](heartbeat-runtime.md) |
| Daily reflection | 8 AM | promote yesterday's log into long-term memory; compile entities | [The Living Self Manual](../the-living-self-manual.md) |
| Weekly synthesis | Sun 8 PM | write the weekly note; update goals; compile entities | [The Living Self Manual](../the-living-self-manual.md) |
| Dream consolidation | post-weekly + manual | merge cross-session signal, resolve contradictions, prune stale entries | [Episodes](episodes.md) |
| Entity compilation | on ingest / reflect / synthesis | build concept pages from sources (Karpathy LLM Wiki pattern) | [Document Uploads And Ingest](document-uploads-and-ingest.md) |

Related reading: [Episodes](episodes.md) (the self's autobiography),
[Session Opening Brief](session-opening-brief.md) (the "while you were out"
block), and [Memory, Knowledge Graph, And Dashboard Chat](memory-hive-chat-observer.md)
(the dashboard memory views).

## The Recall Pipeline

A recall call executes up to six stages in order. The cheap, rules-only stages
run first; the one model call (the re-rank) only fires for substantive queries.

| Stage | Runs when | What it does |
|---|---|---|
| 1. Tier classify | AUTO mode only | Rules-only (no model): skip for greetings/very short messages (Tier 0), otherwise Tier 1. |
| 2. Query expansion | Tier 1 | Split the query into 2-3 sub-queries (heuristic by default, sub-millisecond). |
| 3. Dual search | every active tier | FTS5 keyword search **and** 768-dim BGE vector search run in parallel, then merge; results below the score floor are dropped. |
| 4. Graph hub-boost | Tier 1 | Build the wikilink graph, pull 1-hop neighbors of matched notes, and boost highly-connected hub notes (MOCs). |
| 5. qmd re-rank | Tier 1 **and** more than 3 hits | A fast model (haiku tier) re-ranks the top hits for relevance — ported from Karpathy's `qmd` pattern. On timeout or error it returns the score-ranked order unchanged. |
| 6. Sanitize + format | every active tier | Injection-defense scrub of recalled text, cap length, format with path + score + graph-hop metadata. |

Configuration knobs (environment variables, resolved at call time):

| Env var | Default | Meaning |
|---|---|---|
| `RECALL_ENABLED` | `true` | Master switch. `false` → recall returns empty. |
| `RECALL_MIN_SCORE` | `0.3` | Minimum merged score to keep a hit. |
| `RECALL_MAX_RESULTS` | `3` | Default cap on injected results (chat hot path). |
| `RECALL_RERANK_ENABLED` | `true` | Toggle the Stage-5 qmd re-rank. |
| `RECALL_RERANK_TOP_N` | `10` | How many hits feed the re-ranker. |
| `RECALL_RERANK_TIMEOUT_S` | `3.0` | Hard timeout; on expiry fall back to score order. |

## The `thehomie recall` CLI

`thehomie recall` is one-shot access to the exact pipeline above — the same
`recall_service.recall()` the chat engine uses. It exists so that skills and
operators can run the real recall stack from a shell without importing any
framework code.

```bash
thehomie recall "<query>" --vault thehomie --mode hybrid --max-results 6 --brief
```

| Flag | Default | Purpose |
|---|---|---|
| `--vault` | `thehomie` | Which vault to search: `thehomie`, `coding-vault`, or `unified-vault`. |
| `--mode` | `hybrid` | `auto` (tier-classified), `hybrid` (force Tier 1 — reaches the re-rank), or `keyword` (FTS5 only, no model load). |
| `--max-results` / `-n` | `5` | Cap on returned hits. |
| `--brief` | off | Prepend the proactive "while you were out" brief. |
| `--json` | off | Machine-readable output (`tier`, `reranked`, `results_returned`, `latency_ms`). |
| `--caller` | `vault-ops` | Observability tag (shows up in Langfuse spans). |

Two behaviors are load-bearing:

- **`--mode hybrid` (the default) forces Tier 1**, which is what makes the
  Stage-5 qmd re-rank reachable from a one-shot call. `keyword` is the fast
  escape (no embedding model load); `auto` mirrors the chat engine's
  tier-classified behavior.
- **Fail-open.** If recall is disabled, the index is stale, or anything errors,
  the command prints nothing and exits 0 — never a stack trace. Shelling skills
  pair this with a visible degradation marker
  (`|| echo "[recall] UNAVAILABLE - degraded to floor reads"`) so a missing
  shim or dead index is SEEN, never silently swallowed (the old bare `|| true`
  hid a dead semantic layer for weeks — 2026-07-10 lesson).

The console script only materializes inside `.claude/scripts/.venv/` on
`uv sync` — it is NOT on PATH by itself. Reach it from any cwd via the
`~/bin/thehomie` shim (deployment glue) or explicitly:
`uv run --project .claude/scripts thehomie recall ...`.

## Multi-Vault Setup

Recall addresses three independent vaults, each with its own index file:

| Vault | Configured by | Index file | Availability |
|---|---|---|---|
| `thehomie` | always present | `memory.db` | Always (the Homie's own vault). |
| `coding-vault` | `HOMIE_CODING_VAULT_DIR` | `memory.coding-vault.db` | Only when the env var is set. |
| `unified-vault` | `HOMIE_UNIFIED_VAULT_DIR` | `memory.unified-vault.db` | Only when the env var is set. |

The default (no extra env vars set) is byte-identical single-vault behavior. A
`--vault` request whose env path is unset returns a friendly error rather than
crashing. Re-index a vault with:

```bash
cd .claude/scripts && uv run python memory_index.py --vault coding-vault
```

## How The Slash Commands Use Memory

The `/vault-ops` skill (orient / debrief / weekly / context / think / research)
used to read the vault by skimming Markdown in a fixed order. Those commands now
**augment** that read with the real recall stack by shelling `thehomie recall`.

| Command | Step | What recall adds |
|---|---|---|
| `orient` | 3.5 | semantic context for the start-of-session SITREP (priorities, blockers, deadlines, resume point) |
| `debrief` | 2.5 | recall-ranked hits to seed the autolink + decisions sections |
| `weekly` | 1.5 | cross-vault recurring themes (runs across all three vaults) to seed the synthesis |
| `context <topic>` | 7 | the semantic layer behind a topic briefing — finds matches by meaning, not keyword |
| `think <topic>` | 1 | grounds the strategic dialogue in the most relevant notes |
| `research <topic>` | 1 | prior-art / "what do we already know" before going to the web |

Two invariants make this safe:

- **Invariant I-3 — single recall entrypoint.** The skill only **shells the
  CLI**; it never imports `recall_service`, `memory_search`, or `cognition`.
  That keeps one search path, one observability surface, and one kill-switch.
- **Additive + fail-open (visible).** Recall augments the Markdown floor, it
  never replaces it. Every call is wrapped with
  `|| echo "[recall] UNAVAILABLE - degraded to floor reads"` — the command
  still proceeds on its plain reads, but the degradation prints a marker
  instead of vanishing behind a silent `|| true`.

The native `/vault search` and `/vault context` chat commands are recall-backed
the same way — see [Native Vault Commands](native-vault-commands.md).

## Operator Entry Points

- CLI: `thehomie recall "<query>" --vault <name> --mode hybrid [--brief] [--json]`
- Chat: `/search`, `/vault search <query>`, `/vault context <topic>`, `/file`, `/working`
- Skill: `/vault-ops orient | debrief | weekly | context | think | research`
- Re-index: `uv run python memory_index.py --vault <name>`

## Source Of Truth Files

| Layer | Files |
|---|---|
| Recall entrypoint | `.claude/chat/recall_service.py` |
| Recall pipeline | `.claude/chat/cognition/recall.py` |
| Recall CLI | `.claude/chat/cli.py` (the `recall` command) |
| Search backends | `.claude/scripts/memory_search.py`, `.claude/scripts/db.py` |
| Index builder | `.claude/scripts/memory_index.py` |
| Vault registry | `.claude/scripts/config.py` (`resolve_vault`, `resolve_db_path`) |
| Skill integration | `.claude/skills/vault-ops/references/{routines,intelligence,pipelines}.md` |
| Tests | `.claude/scripts/tests/test_recall_cli.py`, `test_recall_service.py`, `test_cognition_recall.py` |

## Safety Boundaries

- Read-only. Recall retrieves and ranks notes; it never writes the vault.
- Recalled text is injection-sanitized before it enters any prompt.
- The skills never import the recall machinery — they shell the CLI, so the
  kill-switch and observability cover every caller (Invariant I-3).
- No vault paths are hardcoded in framework code; the extra vaults come from
  environment variables and are absent on a default install.

## How To Run It

```bash
cd .claude/scripts
uv run python cli_entry.py recall "current priorities and blockers" --mode hybrid --json
```

## How To Test It

```bash
cd .claude/scripts
uv run pytest tests/test_recall_cli.py tests/test_recall_service.py tests/test_cognition_recall.py -q
```

A live check that the full pipeline (including the qmd re-rank) fires:

```bash
RECALL_RERANK_ENABLED=true uv run python cli_entry.py recall \
  "what should I work on today" --mode hybrid --json
# expect: "tier": "tier_1", "reranked": true
```

## Public Export Status

Public-framework safe. Public export still goes through `scripts/sanitize.py`;
never copy manually.
