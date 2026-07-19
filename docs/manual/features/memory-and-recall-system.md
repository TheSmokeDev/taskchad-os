# Memory And Recall System

Status: Active baseline
Owner: Memory pipelines + recall service (`.claude/chat/recall_service.py`, `.claude/chat/cognition/`, `.claude/scripts/memory_*.py`, `.claude/scripts/entity_extractor.py`, `.claude/scripts/vault_lint.py`)
Last updated: 2026-07-11

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

The background pipelines each have their own deep pages; this chapter covers
**recall** (how memory is read, what the `thehomie recall` CLI does, the
multi-vault layout, how the slash commands tap it) and the **vault maintenance
surface** (the compilation engine's link-economy guardrails and delta-lint).

| Pipeline | Cadence | Job | Deep page |
|---|---|---|---|
| Heartbeat | every 2 h at :02 (framework default 30 min; this box downshifted) | proactive sense loop over calendar/email/tasks + ambient observations | [Heartbeat Runtime](heartbeat-runtime.md) |
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
| 3. Dual search | every active tier | FTS5 keyword search **and** 768-dim BGE vector search run in parallel; each leg is floored on its own scale first, then merged/deduped on raw scores (never renormalized — evolve replay/compare/veto consume absolute `top_scores`). When a `top_n` cap applies, each leg's best surviving hit is guaranteed a slot in the cap window so raw-scale differences alone can't bury an exact match (#136 leg-representation guarantee). |
| 4. Graph hub-boost | Tier 1 | Build the wikilink graph, pull 1-hop neighbors of matched notes, and boost highly-connected hub notes (MOCs). |
| 5. qmd re-rank | Tier 1 **and** more than 3 hits | A fast model (haiku tier) re-ranks the top hits for relevance — ported from Karpathy's `qmd` pattern. On timeout or error it returns the score-ranked order unchanged. |
| 6. Sanitize + format | every active tier | Injection-defense scrub of recalled text, cap length, format with path + score + graph-hop metadata. |

Configuration knobs (environment variables, resolved at call time):

| Env var | Default | Meaning |
|---|---|---|
| `RECALL_ENABLED` | `true` | Master switch. `false` → recall returns empty. |
| `RECALL_MIN_SCORE` | `0.3` | Hybrid-leg merged-score floor to keep a hit (hybrid/vector scale). Applied inside the dual-search pipeline (`_search_with_fallback` → `search_hybrid(min_score=…)`); the keyword leg is floored separately by `RECALL_KEYWORD_MIN_SCORE`. |
| `RECALL_KEYWORD_MIN_SCORE` | `0.02` | Floor for keyword-only recall. Raw FTS5 scores are `1/(1+\|bm25\|)` (~0.05-0.17 for real hits) — a different scale than the hybrid floor; applying 0.3 here returned zero results (fixed 2026-07-15). |
| `RECALL_MAX_RESULTS` | `3` | Default cap on injected results (chat hot path). |
| `RECALL_GRAPH_CACHE_ENABLED` | `true` | #129: cache the wiki-link graph per vault, invalidated by the search-index DB mtime (freshness inherits the index's staleness bound, ~30 min worst case via heartbeat sync). `false` = rebuild on every recall — still off-loop, just uncached. |
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
| `--vault` | `thehomie` | Which vault to search: `thehomie` or `coding-vault`. |
| `--mode` | `hybrid` | `auto` (tier-classified), `hybrid` (force Tier 1 — reaches the re-rank), or `keyword` (FTS5 only, no model load). |
| `--max-results` / `-n` | `5` | Cap on returned hits. |
| `--brief` | off | Terse OUTPUT format: header line + one hit per line + ~200-char snippet (what shelling skills consume). |
| `--with-proactive-brief` | off | Prepend the proactive "while you were out" brief (the pre-2026-07-15 `--brief` behavior, now explicitly named). |
| `--json` | off | Machine-readable output (`tier`, `reranked`, `results_returned`, `latency_ms`). `brief` key is populated only under `--with-proactive-brief`. |
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

## The Compilation Engine And Link-Economy Guardrails

The write side of memory is the entity compilation engine
(`.claude/scripts/entity_extractor.py`) — the Karpathy LLM-Wiki port. When a
source is ingested (or reflection/synthesis runs), it extracts entities and
creates or updates concept pages in `{vault}/concepts/`, cross-links them, and
flags contradictions. Pure Python, zero LLM calls.

Left unchecked, that pattern link-explodes: every one-off mention becomes a
page, every ingest touches 10-15 pages, and the graph rots. The **link-economy
guardrails** (shipped 2026-07-11, from the 30-day field research on the
LLM-Wiki pattern) put a budget on it:

| Guardrail | Knob (default) | What it does |
|---|---|---|
| Create gate | `ENTITY_PAGE_MIN_MENTIONS` (`2`) | A new entity is **staged**, not paged, on first sight. The page is created only when a second *different* source mentions it — and the first source's claims are replayed onto the new page, so no provenance is lost. |
| Edit ceiling | `ENTITY_EDIT_CEILING` (`5`) | One ingest updates at most N existing concept pages. Only real writes count — recompiling the same source burns no slots. |
| Link cap | `ENTITY_LINK_CAP` (`8`) | Per-page ceiling on `related:` frontmatter links. A capped page still receives its `## From [[source]]` content section — the cap governs graph edges, not knowledge. |
| Master switch | `ENTITY_GUARDRAILS_ENABLED` (`false`) | Everything above is default-OFF. When off, compilation is byte-identical to the pre-guardrail engine. |

How staging works: first-sight entities land in a per-vault ledger at
`{vault}/_state/entity-mentions.json` (per-source payloads, atomic writes under
a file lock, 180-day TTL on single-source entries). Staged entities are
excluded from connection articles and source `related:` links, so staging never
manufactures broken wikilinks. The compile report and `concepts/BUILD-LOG.md`
show `Staged` / `Skipped (edit ceiling)` / `Skipped (link cap)` counters, so the
guardrails are always visible, never silent. Any ledger failure fails open to
legacy create-immediately behavior.

Bypass semantics: the CLI `backfill` command bypasses the create gate (bootstrap
is supposed to page everything); the nightly reflection `sweep` does **not**
bypass (otherwise every staged entity would be promoted within 24h and the gate
would be a delay, not a filter).

New concept pages also carry an **AI-first contract** (adopted 2026-07-11 from
the obsidian-thehomie field research): a `## For future Claude` preamble so
retrieval lands on self-contained context, dated per-source claim sections, and
three retrieval-honesty rules enforced in the vault skills — the false-absence
ban (never claim a note doesn't exist without exhaustive search), enumerate
never sample, and `TBD` over invention. The vault's `SCHEMA.md` carries the
full contract. The Stage-5 re-rank also uses a **position-aware blend** (qmd
pattern): the reranker is advisory for retrieval's top hits and decisive only
for the tail, so one bad model call can never bury an exact match.

```bash
cd .claude/scripts
uv run python entity_extractor.py compile "path/to/source.md" --vault-dir "<vault>"   # guardrails per env
uv run python entity_extractor.py backfill --vault-dir "<vault>"                      # bootstrap: gate bypassed
uv run python entity_extractor.py sweep --vault-dir "<vault>"                         # gate respected
```

## Vault Lint And Delta-Lint

`vault_lint.py` runs 8 health checks (orphans, broken wikilinks, frontmatter,
tags, stale content, page size, index completeness, contradictions) over a
vault. Historically every run re-read every note. **Delta-lint** (shipped
2026-07-11) re-lints only what changed:

```bash
cd .claude/scripts
uv run python vault_lint.py --vault-dir "<vault>"           # full scan (default)
uv run python vault_lint.py --vault-dir "<vault>" --delta   # changed files + their linkers
```

- State lives at `{vault}/_state/lint-state.json`: a sha256 content-hash
  snapshot per file plus its outbound wikilinks and cached content-pure issues.
  The reverse linker map is derived in memory, never persisted.
- **The output invariant: delta results are identical to a full scan, always.**
  Unchanged files replay their cached issues; time-dependent checks
  (`stale_content`) are recomputed every run; global checks (orphans, broken
  links) are evaluated from the updated link map. This is what keeps the
  scheduled "Vault lint: NE/NW" health line truthful.
- Full scan is forced automatically when the state is missing/corrupt, when
  `SCHEMA.md` changes (tag-taxonomy invalidation), or when a `--check` subset is
  requested. The whole delta path fails open to a full scan — `run_lint` never
  raises.
- `LINT_DELTA_ENABLED` (default `false`) flips the scheduled reflection lint to
  delta mode without touching the CLI default.

Both `_state/` files are per-vault, invisible to the recall index (`.md`-only
walk), and skipped by lint, backfill, and index generation.

## Operator Entry Points

- CLI: `thehomie recall "<query>" --vault <name> --mode hybrid [--brief] [--with-proactive-brief] [--json]` (`--brief` = terse output; `--mode keyword` = fast no-model path for probes)
- Chat: `/search`, `/vault search <query>`, `/vault context <topic>`, `/file`, `/working`
- Skill: `/vault-ops orient | debrief | weekly | context | think | research`
- Re-index: `uv run python memory_index.py --vault <name>`
- Compile: `uv run python entity_extractor.py compile|backfill|sweep --vault-dir <vault>`
- Lint: `uv run python vault_lint.py --vault-dir <vault> [--delta] [--format json]`

## Source Of Truth Files

| Layer | Files |
|---|---|
| Recall entrypoint | `.claude/chat/recall_service.py` |
| Recall pipeline | `.claude/chat/cognition/recall.py` |
| Recall CLI | `.claude/chat/cli.py` (the `recall` command) |
| Search backends | `.claude/scripts/memory_search.py`, `.claude/scripts/db.py` |
| Index builder | `.claude/scripts/memory_index.py` |
| Vault registry | `.claude/scripts/config.py` (`resolve_vault`, `resolve_db_path`) |
| Compilation engine + guardrails | `.claude/scripts/entity_extractor.py`; knobs in `config.py` (`get_entity_guardrail_settings`) |
| Vault lint + delta | `.claude/scripts/vault_lint.py`; knob in `config.py` (`get_lint_delta_enabled`) |
| Skill integration | `.claude/skills/vault-ops/references/{routines,intelligence,pipelines}.md` |
| Tests | `.claude/scripts/tests/test_recall_cli.py`, `test_recall_service.py`, `test_cognition_recall.py`, `test_entity_guardrails.py`, `test_vault_lint.py` |

Both the index builder (`sync_index`) and the single-file entrypoint (`reindex_file`)
guard embedding-model migrations the same way: read the *physical* vec-table
dimension via `db.get_actual_embedding_dim()` before touching schema, and force
a full rebuild on mismatch instead of trusting the `meta` table (Rule 2 — meta
is derived state, not source of truth). See `tests/test_dim_drift_guard.py`.

## Safety Boundaries

- Recall is read-only: it retrieves and ranks notes; it never writes the vault.
  The write side (compilation, lint state) touches only `concepts/`,
  `connections/`, source `related:` frontmatter, and `{vault}/_state/` — and
  every new write behavior ships default-OFF behind an env knob.
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
