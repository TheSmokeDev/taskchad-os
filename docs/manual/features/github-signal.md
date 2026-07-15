# GitHub Signal — Star Backlog Resurfacing + Trending

Status: Active, weekly scheduled + Repo Scout persona surfaces
Owner: memory-cognition / scheduled jobs
Last updated: 2026-07-14

## What It Does

Operators star GitHub repos constantly and rarely revisit them. GitHub Signal
turns the starred backlog into a working queue: a weekly digest that lists
new stars since the last run, resurfaces a handful of BACKLOG stars matched
against the operator's active work, and garnishes with AI-relevant hits from
github.com/trending. Each backlog pick carries a one-line "why now" bridging
the repo to what the operator is doing right now — contextual resurfacing,
because random resurfacing gets ignored.

The digest is a dated vault file plus a compact Telegram card. Two loop-closing
commands (`/stars used`, `/stars snooze`) keep the same repo from nagging twice.

## Operator Entry Points

- Chat: `/stars` (status), `/stars refresh`, `/stars eval <owner/repo>`,
  `/stars used <repo>`, `/stars snooze <repo> [weeks]`, `/stars trending`
- CLI: `cd .claude/scripts && uv run python -m github_signal.engine [--test]`;
  one-off eval: `uv run python -m github_signal.eval_runner <owner/repo>`
- Scheduler: `SecondBrain-GitHubSignal` task, Monday 09:00 weekly
  (register once via `setup_github_signal_scheduler.ps1`)
- Output: digest `Memory/github-signal/YYYY-WNN.md` + eval notes
  `Memory/github-signal/evals/` + Telegram card + optional Discord channel
  card + daily-log receipt
- Persona: the Repo Scout profile (seed via
  `uv run python -m personas.repo_scout`) discusses digests/evals in its
  bound Discord channel; see Repo Scout section below

## How It Works

1. **Starred inventory** — `github_signal/fetch.py` pulls the full starred
   list (`GET /user/starred`, `Accept: application/vnd.github.star+json` for
   `starred_at`, stdlib urllib, `GITHUB_TOKEN`/`GH_TOKEN` bearer). Weekly
   full refetch; GitHub is the source of truth for the inventory (Rule 2).
2. **New-vs-backlog split** — a single `starred_at` ISO **timestamp
   watermark** in state marks "new since last run" (O(1) state, no ID sets).
   First run baselines the watermark without replaying the whole backlog as
   "new" — but backlog picks still run on day 1.
3. **Eligibility** — the lifecycle map in
   `.claude/data/state/github-signal-state.json` is SPARSE: a repo with no
   entry is fresh. `used` excludes forever, `snoozed` until its date,
   `surfaced` for a cooldown window (default 8 weeks).
4. **Contextual picks** — ONE background quality-tier LLM call
   (`get_background_models()["quality"]`, never the interactive flagship)
   sees the whole eligible backlog (one compact line per repo, oldest star
   first) plus active-work context (GOALS, the active-PRP tracker, last 7
   daily logs) and returns `[{full_name, why_now}]`. Returned names are
   validated as a subset of the eligible set — hallucinated names are dropped
   and topped up from the deterministic fallback (most recently starred).
   On any LLM failure the digest still writes, flagged
   `picks_via_llm: false`.
5. **Trending garnish** — `github_signal/trending.py` scrapes
   github.com/trending (stdlib HTMLParser; no official API exists), filters
   by `GITHUB_SIGNAL_TRENDING_KEYWORDS`, dedups via the watchers `Watermark`.
   Unversioned scraping is fragile by nature, so it fails to `[]` and is
   never fatal to the run.
6. **Silent gate** — no new stars AND no eligible picks AND no trending hits
   → `GITHUB_SIGNAL_SILENT`: zero LLM cost, zero ping (the dream-cycle
   pattern).
7. **Cross-process state** — the bot process and the cron process share one
   substrate: the state file, every mutation under `shared.file_lock`. The
   engine never holds the lock across the LLM call; it snapshots at start
   and merges at the end, and the merge NEVER downgrades an operator-set
   `used`/`snoozed` to `surfaced`. `/stars refresh` spawns the engine
   detached (`shared.spawn_detached`) — the ~8s starred fetch never rides
   the bot event loop.

## Discord Lane

`GITHUB_SIGNAL_DISCORD_CHANNEL_ID` (default "" = off) makes every digest and
eval card ALSO post to a Discord channel via
`social.notify.send_text_to_discord` — a direct REST call (`Bot` token auth,
2000-char truncation), because the cron process has no gateway connection.
Both notify lanes are independently fail-open; the vault artifact is always
the durable output. The channel ID belongs in the operator's untracked
`.env`, never in shipped code.

## Repo Eval (`/stars eval <owner/repo>`)

A detached job (never on the bot event loop) that produces an adopt/try/skip
verdict card:

1. GitHub API metadata (stars, pushed_at, archived, license, size)
2. Size guard: > `GITHUB_SIGNAL_EVAL_MAX_REPO_MB` (200) skips the clone
3. `git clone --depth 1` into the gitignored eval sandbox
4. Evidence: capped file tree, README head, manifest heads — **file reads
   only; repo code is NEVER executed, installed, or tested**
5. One background quality-tier LLM call → verdict JSON, validated
   (bad/missing recommendation = LLM failure)
6. Card to both notify lanes + durable eval note in `github-signal/evals/`
7. State updated ADDITIVELY (`evaluated_at`, `eval_recommendation`) — an
   eval never changes used/snoozed lifecycle
8. Clone deleted (Windows chmod-retry rmtree) unless
   `GITHUB_SIGNAL_EVAL_KEEP_CLONE=true`

Degradation ladder: oversize/clone-fail → API-only evidence (raw README
endpoint); LLM-fail → facts-only card with verdict "unavailable". The card
and the note always ship. Eval accepts any valid `owner/repo`, starred or
not; bare names resolve against the current picks.

## Repo Scout Persona

The Scout is the conversational surface for this pipeline: a persona profile
(seeded by `personas/repo_scout.py` — kill-switch-gated, never clobbers
operator-authored identity) bound to a Discord channel via the untracked
channel-bindings file. Channel turns are framework-tool-denied (text
reasoning + per-persona recall only), so:

- **His knowledge arrives via sync**: after every digest/eval, `scout_sync.py`
  copies the artifact into `<profile>/memory/research/github-signal/` and
  refreshes the persona's own recall index (incremental). Gate:
  `GITHUB_SIGNAL_SCOUT_PROFILE` (default `repo-scout`, "" = off); missing
  profile = silent skip — the pipelines never depend on the persona existing.
- **His actions are the /stars surface**: refresh and eval run as detached
  jobs whose cards land back in his channel.
- Capability matrix entry: `repo-scout` gets `runtime_core, vault_memory,
  github_ops` env groups and no skills (the skill index is advisory text in
  a tool-denied channel).

## Failure Modes

| Symptom | Cause | Fix |
|---|---|---|
| `failed` + 401 in log | No `GITHUB_TOKEN` in `.claude/scripts/.env` | Add a classic PAT (no extra scopes needed for public repos); watermark is untouched, next run rescans |
| Trending section always empty | GitHub changed the trending page HTML | Parser fails to `[]` by contract; update `TrendingParser` selectors |
| Same repo keeps appearing | It was never marked | `/stars used <repo>` (permanent) or `/stars snooze <repo> [weeks]` |
| `/stars used` says "try again in a moment" | Engine holds the state lock (start/end merge windows, sub-second) | Retry; if persistent, check for a stuck engine process |
| Digest exists but no Telegram card | Telegram env missing or send failed | Non-fatal by design; digest is the durable artifact — check `TELEGRAM_BOT_TOKEN`/`TELEGRAM_ALLOWED_USER_IDS` |

## Knobs

| Env var | Default | Meaning |
|---|---|---|
| `GITHUB_SIGNAL_ENABLED` | `true` | Master toggle |
| `GITHUB_SIGNAL_PICK_COUNT` | `4` | Backlog picks per digest |
| `GITHUB_SIGNAL_RESURFACE_COOLDOWN_WEEKS` | `8` | Weeks before a surfaced-but-unactioned pick can return |
| `GITHUB_SIGNAL_SNOOZE_WEEKS` | `4` | Default `/stars snooze` duration |
| `GITHUB_SIGNAL_TRENDING_KEYWORDS` | generic AI terms | Comma list filtering trending hits |
| `GITHUB_SIGNAL_MAX_BUDGET_USD` | `0.25` | Per-run LLM budget cap |
| `GITHUB_SIGNAL_DISCORD_CHANNEL_ID` | `""` (off) | Discord channel for digest + eval cards |
| `GITHUB_SIGNAL_SCOUT_PROFILE` | `repo-scout` | Persona profile receiving memory sync ("" = off) |
| `GITHUB_SIGNAL_EVAL_MAX_REPO_MB` | `200` | Skip clone above this size (API-only evidence) |
| `GITHUB_SIGNAL_EVAL_KEEP_CLONE` | `false` | Keep the sandbox clone after eval |

## Validation Map

- `tests/test_github_signal_fetch.py` — pagination, 401/500/network errors,
  trending parser golden fixture + malformed HTML, keyword filter
- `tests/test_github_signal_state.py` — lifecycle round-trips, eligibility
  matrix, never-downgrade merge, unstarred pruning
- `tests/test_github_signal_picks.py` — prompt assembly, hallucination
  drop + top-up, deterministic fallback, missing-context degradation
- `tests/test_github_signal_engine.py` — first-run baseline, silent path,
  success path, fetch-fail watermark safety, non-fatal trending/Telegram
- `tests/test_stars_command.py` — command registration + handler behavior,
  detached refresh spawn, lock-busy message
