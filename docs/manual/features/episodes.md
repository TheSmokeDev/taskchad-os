# Episodes (Living Mind Act 3)

Status: Shipped
Owner: Framework (memory pipelines)
Last updated: 2026-06-12

## What It Does

Every session that produces a meaningful memory flush leaves a structured
narrative episode in `episodes/` under the memory vault root. An episode is
the flush LLM's distillation of a session — a short narrative the agent can
later recall ("I remember when…"), never a raw transcript. The writer rides
the EXISTING session-end flush; there is no new pipeline and no new LLM call.

Episodes ship with two consumers in the same act:

- **Dream cycle** — open episodes join the gather scan (they raise the
  weighted signal score), feed a capped digest into the consolidate prompt,
  and get flipped to `status: consolidated` after a successful review.
- **Recall** — `episodes/` is part of the vault, so the unfiltered index
  rglob picks episodes up automatically. No recall code changed.

## Where Episodes Come From (flush convergence map)

All three flush entry points converge on one background script
(`memory_flush.py`), so one writer covers every surface:

| Entry point | Hook | Context filename | Surface value |
|---|---|---|---|
| Session end (CLI exit, chat `/clear` lifecycle) | `session-end-flush.py` | `session-flush-{safe_id}-{YYYYMMDD}-{HHMMSS}.md` | platform token or `code` |
| Pre-compaction checkpoint | `pre-compact-flush.py` | `flush-context-{safe_id}-{YYYYMMDD}-{HHMMSS}.md` | `compact` |
| Chat `/clear` | invokes the SessionEnd hook | same as session end | platform token |

The hooks sanitize the session-id component at filename composition
(`[A-Za-z0-9._-]+`, same policy as the chat lifecycle's own transcript
naming) — this is also the root-cause fix for the Windows defect where
colon-bearing chat session keys made the flush hook exit 1. Everything else
(dedup compares, the lifecycle payload) keeps the raw id.

The engine's turn-threshold reset is NOT a flush boundary and produces no
episode — by design.

## Episode Key and Naming

Path: `{memory_dir}/episodes/{YYYY-MM-DD}-{surface}-{sid8}-{HHMMSS}.md`

- The key is **lifecycle-unique**, not channel-stable. Chat session keys are
  stable composites (`platform:channel_id:thread_id`) that get reused after
  `/clear`, so they cannot key episodes. The lifecycle identifier is the
  hook-run timestamp already embedded in the context filename.
- The filename date is the lifecycle START date (from the context-file stem,
  never write-time). One lifecycle crossing midnight stays in ONE file.
- `sid8` = first 8 hex chars of `sha1(session_id)` — filename-safe, groups a
  channel's episodes.
- Two clears of the same channel on the same day produce TWO files. A retry
  of the same flush (identical context filename) converges on ONE file and
  appends an `## Update (HH:MM)` block with demoted headings.

Surface table (exact):

| Filename prefix | Extracted id shape | surface |
|---|---|---|
| `flush-context-` | any | `compact` |
| `session-flush-` | `telegram- / discord- / slack- / whatsapp- / web- / cli-` prefix | that platform token |
| `session-flush-` | anything else (agent-runner uuid) | `code` |

## Frontmatter Schema

```yaml
---
tags: [system, memory, living-mind]
status: open
date: 2026-06-12
session_id: "telegram-1111111111-2222222222"
summary: "First ~100 sanitized chars of the Summary section"
surface: telegram
lifecycle: "20260612-201855"
---
```

- `status: open | consolidated` — note this deviates from the prose status
  vocabulary used elsewhere in the vault schema (`current|reference|archived|
  draft`). Accepted: the vault linter parses `status` but audits no status
  values, and episodes own their lifecycle semantics.
- `consolidated_at: YYYY-MM-DD` is added when the dream cycle flips the
  episode; a later `## Update` append re-opens the episode (status back to
  `open`, `consolidated_at` removed) so new content gets reviewed.
- Tags are all existing taxonomy tags — zero schema changes, zero new tag
  audit warnings.

Body: `# Episode: {date} — {surface}` followed by up to four sections the
flush prompt emits — `## Summary`, `## Key Decisions`, `## Open Threads`,
`## Texture`. Parsing is heading-tolerant: a response with missing or
unrecognized headings falls back to everything-under-Summary, so any
provider's output produces a valid episode.

## Lifecycle

```
flush fires -> episode WRITTEN (status: open)
        dream gather scans open episodes (date window, newest-first)
        dream consolidate reviews the capped digest
        -> mark_episodes_consolidated flips status + adds consolidated_at
same-lifecycle re-flush -> ## Update appended, episode RE-OPENED
```

"Consolidated" means "a successful dream Phase 3 reviewed it" — the flip
happens after `consolidate()` returns regardless of whether the LLM proposed
changes (reviewed-and-empty is still reviewed). A consolidate failure leaves
episodes open for the retry run; a flip failure is warning-logged and the
dream still reports success. Episodes are insert-only history — no archive
or aging pass touches `episodes/`.

## Knobs (env vars, call-time resolved)

| Env var | Default | Meaning |
|---|---|---|
| `EPISODE_MIN_CHARS` | 80 | Minimum parsed-body chars for a NEW episode file |
| `EPISODE_MAX_PER_DAY` | 20 | Cap on NEW episode files per lifecycle-date (same-key updates exempt) |
| `EPISODE_DREAM_MAX_FILES` | 10 | Newest-first cap on episodes fed to the dream consolidate prompt |
| `EPISODE_DREAM_MAX_CHARS_PER` | 600 | Per-episode digest excerpt cap |
| `EPISODE_DREAM_MAX_TOTAL_CHARS` | 4000 | Total digest cap |

## Privacy Contract

- Episodes contain the LLM summary, NEVER the transcript. The writer's
  signature receives only the flush response text plus the context FILENAME
  (metadata) — transcript content physically cannot reach it.
- `FLUSH_OK` responses (nothing worth saving) never produce an episode; the
  min-chars floor drops trivial residue below that.
- Raw flush context files remain machine-state outside the vault and are
  deleted on successful flush.

## How To Test It

```powershell
cd .claude/scripts
uv run pytest tests/test_episodes.py tests/test_session_flush_hooks.py tests/test_memory_flush_gate.py tests/test_memory_dream.py -q
```

## Recall Reach

Episodes are searchable through the normal memory search surface, including
prefix scoping:

```powershell
cd .claude/scripts
uv run python memory_search.py "what happened with the deploy" --mode hybrid --path-prefix episodes/ --limit 5
```

A freshly written episode is best-effort reindexed by the flush process
itself (single-file index update, no LLM), so it is searchable same-day.

## Public Export Status

Public-exported (this page ships via the manual allowlist).
