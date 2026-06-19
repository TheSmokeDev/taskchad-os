# Business Signal Engine

Status: Shipped (#79), merged to master
Owner: `.claude/scripts/business_signal/` (pipeline) + `.claude/chat/` (the `/signal` command)
Last updated: 2026-06-19

## What It Does

The Business Signal Engine is a native, scheduled intelligence pipeline. On the
existing memory cadence it pulls business signal from RSS feeds and HARO reporter
emails, triages each item against an operator-configurable keyword focus profile,
enriches and analyzes the survivors with a cheap background model, and writes a
daily digest plus ready-to-edit content drafts into the vault.

It replaces ad-hoc, manual signal-gathering — browsing feeds, losing items
between sessions, no systematic triage — with a deterministic, recall-indexed
daily artifact. It is **write-only by design**: it drafts and files, it never
posts. Acting on a draft is a separate, operator-gated step (see
[Social Post Pipeline](social-post-pipeline.md)).

## Operator Entry Points

- Chat/Telegram: `/signal` (status of the last run) and `/signal refresh` (run
  now). The command is admin-only.
- CLI: `uv run python .claude/scripts/business_signal/signal_engine.py [--test] [--days N]`
- Cadence: runs automatically as a non-blocking post-step of daily reflection
  (fast tier) and weekly synthesis (quality tier).
- Dashboard/API: none — this is a vault-and-chat surface.

## The Six Stages

The pipeline is `fetch → triage → research → analyze → synthesize → output`. The
first three stages are pure-Python and free. The background model is only invoked
*after* triage proves there is something worth spending tokens on — if triage
returns zero items the engine sets `SIGNAL_SILENT` and exits before any LLM call
(the same zero-cost short-circuit the dream cycle uses with `DREAM_SILENT`).

| Stage | What it does | Owner file | Cost |
|---|---|---|---|
| 1. Fetch | Pull items from configured RSS feeds + HARO emails | `fetchers/rss_fetcher.py`, `fetchers/haro_fetcher.py`, `fetchers/__init__.py` | Free |
| 2. Triage | Score each item against the keyword focus profile (HIGH/MED/SKIP), drop below threshold, sort, cap | `triage.py`, `focus.py` | Free |
| 3. Research | Optionally enrich survivors with full-text fetched from their URLs | `research.py` | Free |
| 4. Analyze | One batched background-model call adds a `content_angle` to each item | `analyze.py` | LLM (1 call) |
| 5. Synthesize | One background-model call builds the digest markdown (exec summary + top items + opportunities) | `synthesize.py` | LLM (1 call) |
| 6. Output | Write the vault digest, create content drafts for high-signal items, append the daily log, persist run state | `output.py` | Free |

The fetch and focus layers are deliberately generic: the shipped default targets
the operator's own verticals, but the feed list and the keyword tiers are fully
configurable (see Config Knobs). Fetcher failures are isolated per-source — one
dead feed or an unconfigured email account never crashes a run.

## Cadence

| When | Trigger | Tier | Model |
|---|---|---|---|
| Daily | Post-step of daily reflection (`memory_reflect.py`) | `fast` | cheap background model (e.g. haiku) |
| Weekly | Post-step of weekly synthesis (`memory_weekly.py`) | `quality` | stronger background model (e.g. sonnet) |
| On demand | `/signal refresh` or the CLI | `fast` (default) | — |

Both cadence hooks are wrapped in their own try/except — a signal-engine failure
never breaks reflection or synthesis. The weekly run raises the tier via
`SIGNAL_MODEL_TIER` for a deeper pass over the week's cross-domain signal.

## Outputs

- **Daily digest** — `MEMORY_DIR/BUSINESS_SIGNAL_DIGEST.md` (the memory-vault root), a single rolling
  file overwritten each run. Frontmatter carries run metadata (`sources_checked`,
  `sources_failed`, `items_triaged`, `total_fetched`, `date`); the body is an
  LLM exec summary plus a section per triaged item (title, source, relevance
  score, tags, content angle, URL, summary).
- **Content drafts** — one file per high-signal item (relevance ≥
  `SIGNAL_DRAFT_THRESHOLD`) under `MEMORY_DIR/drafts/active/`, with a
  `## Signal`, `## Suggested Angle`, `## AI-Drafted Post` (a short punchy
  post from `draft_generator.py`), and a `## Your Perspective` placeholder for
  the operator.
- **Daily log line** — a one-line summary (sources scanned, top items by score,
  drafts created) appended to today's daily log.
- Every artifact is picked up by the normal memory indexer (the vault `rglob`),
  so digests and drafts are immediately searchable via `/search` (scope with
  `--path-prefix drafts/`).

## Config Knobs

All resolved at call time via `get_signal_settings()` (Rule 1 — no tunable bound
in a default arg). Env-var names only; set values in `.claude/scripts/.env`.

| Env var | Default | Meaning |
|---|---|---|
| `SIGNAL_ENABLED` | `true` | Master switch. If false, a run returns `disabled` immediately. |
| `SIGNAL_TRIAGE_THRESHOLD` | `0.3` | Minimum relevance score to pass triage. All-below ⇒ `SIGNAL_SILENT`, zero LLM cost. |
| `SIGNAL_MAX_ITEMS` | `30` | Cap on items processed per run (token guard). |
| `SIGNAL_DRAFT_THRESHOLD` | `0.7` | Minimum relevance score to generate a content draft. |
| `SIGNAL_RSS_FEEDS` | (shipped defaults) | Comma-separated feed URLs; overrides the default feed list. |
| `SIGNAL_MODEL_TIER` | `fast` | `fast` (daily) or `quality` (weekly) — selects the background-model tier. |

## Safety Boundaries

- **Write-only.** The engine drafts and files into the vault. It never posts,
  emails, or calls an outbound integration. Posting is a separate, operator-gated
  action.
- **Kill-switch guarded LLM.** Every model-calling stage (`analyze`, `synthesize`,
  `draft_generator`) checks the `llm` kill-switch through the module-attribute
  helper before the call. If disabled, the stage returns its input unchanged and
  logs a skip — the pipeline degrades gracefully, it never crashes.
- **Zero-cost when quiet.** No triaged items ⇒ no model calls at all.
- **Fetch isolation.** Each fetcher runs under its own try/except; failures are
  counted (`sources_failed`) but never fatal. HARO email scanning auto-disables
  when the email integration is unconfigured.
- **Rule 1 / call-time config.** All `SIGNAL_*` knobs resolve inside
  `get_signal_settings()` so test/replay overrides are honored.

## How To Run It

```powershell
cd .claude/scripts

# Status of the last run (also via /signal in chat)
uv run python business_signal/signal_engine.py --test   # dry run: prints, no writes, no LLM

# Live run now (fetch -> triage -> LLM -> digest + drafts)
uv run python business_signal/signal_engine.py

# Wider dedup window
uv run python business_signal/signal_engine.py --days 14
```

## How To Test It

```powershell
cd .claude/scripts
uv run pytest tests/test_signal_engine.py tests/test_signal_fetchers.py tests/test_signal_focus.py tests/test_signal_models.py tests/test_signal_output.py tests/test_signal_research.py tests/test_signal_triage.py -q
```

61 tests across 7 files: dataclass shape, focus scoring (HIGH/MED/SKIP), triage
threshold/sort, fetcher mocking + per-source isolation + dedup TTL, output
(digest frontmatter/body, draft creation, `SIGNAL_SILENT` no-output), research
enrichment, and an end-to-end integration (mock fetchers → real triage → mock LLM
→ verified digest/drafts/state).

## Latest Live Proof

- Date: 2026-06-19
- Surface: merged PR #79 (squash commit `3fc795f7`); re-reviewed and merged.
- Result: 61/61 signal tests green on the merged tree; `/signal` registered and
  live on the running bot.
- Proof: the merged PR review thread (all six original review findings closed +
  the kill-switch-guard regression closed).

## File Ownership Map

| File | Responsibility |
|---|---|
| `business_signal/signal_engine.py` | Orchestrator: runs the six stages, manages state + file lock, the `SIGNAL_SILENT`/`disabled` gates, CLI. |
| `business_signal/config.py` | Path constants + `get_signal_settings()` call-time resolver. |
| `business_signal/models.py` | `SignalItem` / `SignalDigest` dataclasses. |
| `business_signal/focus.py` | Keyword focus profile + `score_relevance()`. |
| `business_signal/triage.py` | Pure-Python triage (score, filter, sort). |
| `business_signal/research.py` | Optional URL full-text enrichment. |
| `business_signal/analyze.py` | Background-model content-angle pass (kill-switch guarded). |
| `business_signal/synthesize.py` | Background-model digest builder (kill-switch guarded). |
| `business_signal/draft_generator.py` | Background-model draft copy (kill-switch guarded, graceful fallback). |
| `business_signal/output.py` | Stage 6: digest write, draft files, daily-log append. |
| `business_signal/fetchers/__init__.py` | Fetcher protocol + registry (per-fetcher error isolation). |
| `business_signal/fetchers/rss_fetcher.py` | RSS/Atom parsing + URL dedup (TTL). |
| `business_signal/fetchers/haro_fetcher.py` | HARO email scan + keyword match. |
| `.claude/chat/commands.py`, `.claude/chat/core_handlers.py` | `/signal` command registration + `handle_signal`. |
| `.claude/scripts/memory_reflect.py`, `memory_weekly.py` | Daily/weekly cadence hooks. |

## Public Export Status

Public-safe by construction (mechanism only, vertical-neutral framing, no account
IDs, no secret values). Because `docs/` is in the sanitizer `DENY_DIRS`, this page
ships publicly only through an explicit per-file entry in the sanitizer
`INCLUDE_FILES` list. Export goes only through `scripts/sanitize.py`; never copy
files between repos by hand.

## Next Slices

- Web-search fetcher (currently RSS + HARO).
- A dashboard surface for the digest (currently vault + chat only).
- Tighter hand-off into the social pipeline (high-signal draft → `/social draft`).
