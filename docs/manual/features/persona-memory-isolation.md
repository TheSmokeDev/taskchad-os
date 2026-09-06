# Persona Memory Isolation And Inventory Repair

Status: shipped 2026-07-07
Owner: personas slice (`personas/lifecycle.py`) with CLI, doctor, and boot-guard surfaces
Last updated: 2026-08-13

## What It Does

Every named persona profile owns an isolated memory vault at
`<profiles-root>/<name>/memory/` — 15 identity files (SOUL.md, MEMORY.md,
GOALS.md, ...) plus 20 memory subdirectories (concepts/, daily/, episodes/,
experience/, ...). The persona learning loop, reflection, episode, and
experience-note writers all write into that tree; the cabinet/persona turn
context is built FROM it.

> **Seeded is not loaded.** Only **seven** of those 15 identity files reach a
> chat prompt; the rest are inert, so rules written into them have no effect.
> Which files load, their region budgets, the ordering authority, and the rule
> for what belongs in an identity file versus a per-turn prompt are documented
> in [Persona Identity And Prompt Architecture](persona-identity-and-prompt.md).
> Read it before authoring persona files.

`experience/` joined the required inventory in #422 (2026-08-13) so a persona
is BORN with the substrate its learning loop needs. Profiles created before
that report `inventory_ok: false` until touched — inventory health is
warn-level, and the repair paths below backfill the directory.

The failure class this feature closes: a profile created before the inventory
contract existed (or hand-provisioned with just `config.yaml` + `.env`) can be
missing part or all of that tree. Context loading fails OPEN — every read of a
missing file returns an empty string, so the persona answers every turn with
zero knowledge context and no error anywhere. The persona also cannot learn:
reflection and episode writes have nowhere to land.

Three layers make the inventory guaranteed:

1. **Repair primitive** — `ensure_profile_inventory(name)` runs the same
   idempotent bootstrap `profile create` runs (mkdir `exist_ok` + seed a stub
   ONLY when the file is missing) against an existing profile. It NEVER
   overwrites an authored identity file, and it reports what it created vs
   found. A read-only twin, `inspect_profile_inventory(name)`, powers every
   diagnostic surface without writing.
2. **Operator visibility** — `thehomie doctor` flags a missing `memory/` dir
   as an error (with the repair command as the fix hint), partial inventory
   and orphaned root identity files as warnings. `profile list` marks broken
   profiles with `inv=BROKEN(N missing)`; `profile show` prints the missing
   entries and the fix hint.
3. **Boot guards** — the cabinet persona-turn path and the persona bot
   activation path each stat `memory/` once on the happy path and run the
   repair primitive only when the dir is missing. Guards are fail-open
   (a guard failure never kills a turn or blocks a spawn) but loud: the
   failure is logged and the violation stays on disk where doctor reports it.

**Orphaned root identity files:** an identity file sitting at the profile
ROOT (`<profile>/SOUL.md`) instead of `<profile>/memory/SOUL.md` is dead
weight — the loader never reads it. Repair detects and reports these but
NEVER moves them; merging root content into `memory/` is an operator
decision.

## Inference-Time Recall (Discord Persona Turns)

Shipped 2026-07-07 (issue #110). The write side (learning loop, reflection,
episode writes) accumulates knowledge into each persona vault; this is the
matching READ side at answer time.

A Discord persona-channel turn (`#crypto`, `#sales`, ...) now runs semantic
recall over **that persona's own** memory index, mirroring the main engine
(`engine.py:1211-1244`) but bound to the persona vault:

- `discord_persona_runtime.py` calls
  `recall_service.recall(query=<user msg>, memory_dir=paths["memory"], ...)` in
  AUTO mode. `config.resolve_db_path(paths["memory"])` routes it to
  `<profiles-root>/<name>/data/memory.db` — the persona's OWN co-located index,
  per-persona-unique and NEVER the main vault (Rule 2, physical on-disk state).
- The top-N reranked snippets are injected into the persona system prompt as a
  `# Persona Recalled Memory` block, alongside the frozen briefing.
- Fail-open: any recall failure OR an empty/unbuilt persona `memory.db` →
  briefing-only turn (the prior behavior). Recall is never turn-killing.

**DB-path isolation (the trap this closed):** every persona `memory/` dir shares
the basename `memory`. Before the fix, `resolve_db_path`'s slug fallback mapped
them ALL to a single `DATA_DIR/memory.memory.db` in the MAIN vault (name
collision + wrong root). The fix teaches the fallback the profile layout — a
`<root>/memory` dir with a sibling `<root>/data` resolves to its own
`<root>/data/memory.db`. Regression-locked by
`tests/test_persona_recall_isolation.py` (a fact indexed only in persona A is
recalled for A, NOT for B or main, asserted at the DB/result level).

**Index freshness (recall is only as good as the index):** a persona's
`data/memory.db` is populated by whatever indexes its vault. The scheduled
learning tick reindexes episodes/beliefs into the persona vault (subprocess with
`HOMIE_HOME` flipped → its `resolve_db_path` hits the match branch → the same
`<profile>/data/memory.db`). For **bulk-fed** content (e.g. pointing a persona
at a domain repo), run the one-time build so recall has something to find:

```bash
cd .claude/scripts && uv run python memory_index.py -p <name>
```

Until the index exists, recall correctly returns empty and the turn falls back
to the briefing — a no-op, not an error.

## Work-Time Read-Back (Co-Founder Worktick Draft Turns)

Shipped 2026-08-12 (issue #421, epic #418). Same read side as above, pointed at
the OTHER place a persona thinks: the co-founder work loop. Before this, a
persona wrote experience into its vault but never re-read it while working —
`build_draft_prompt` assembled SOUL + repo notes + the task and nothing else.

> **The fence is not worktick-only.** Fencing one consumer was the shape the
> #425 round-4 gate rejected. The trust split lives at the COMPOSITION layer:
> `runtime/bootstrap.py:read_durable_memory` splits MEMORY.md into the
> operator-authored head and the machine-authored `## Autonomous Amendments`
> tail — the split itself owned by
> `cognition.amendments.split_autonomous_amendments`, which owns the format —
> and emits the tail fenced under `### Machine-Written Memory`. Every official
> persona surface (Discord `chat/discord_persona_runtime.py`, web/dashboard
> `chat/web_persona_runtime.py`, Cabinet `scripts/cabinet/text_orchestrator.py`)
> builds its system context through `build_session_start_context`, so all three
> inherit it — in the briefing path AND in the raw full-dump fallback that
> persona profiles actually take. That fallback is deliberately NOT narrowed: a
> persona's MEMORY.md has no capsule structure, so for personas the dump IS the
> normal path and degrading to "whatever extracted" would strip their identity.
> It is safe because the dump applies the same split, not because it is rare.
> `worktick.py` keeps its own fence because it reads the profile files directly
> rather than through bootstrap.

`cofounder/worktick.py` now adds two capped, additive, fail-open blocks to every
draft-mode work prompt:

1. **Durable memory** — a capped read of the persona's own `memory/MEMORY.md`
   (`_persona_memory`, the `_persona_soul` shape, `MEMORY_PROMPT_CAP`), fenced
   by `_fenced_identity_block` before it enters the prompt.

   That fence arrived with #425 and closes the constraint #421 pinned on it.
   The original premise was "same trust class as SOUL.md: first-party identity
   memory, injected as-is". #425 invalidated it on BOTH files: the notes
   distiller now writes model-authored lessons into persona `MEMORY.md`,
   distilled from work notes that carry external research titles and quoted
   third-party prose, and a steered amendment could reach `SOUL.md` by the same
   ledger. So SOUL.md and MEMORY.md now route through the identical
   `sanitize_recalled_content` + `wrap_recalled_memory` containment
   `_persona_recall` already uses in this file. Screening is per `## ` section
   (`_identity_chunks`), because `sanitize_recalled_content` is rejection-only
   over the whole string it is handed and its patterns include `act as a` — a
   legitimate hand-written SOUL.md would otherwise vanish from every draft
   prompt on one false positive. The host-authored framing lines ("speak in
   this voice") stay OUTSIDE the fence; only file content goes inside.
2. **Own-index recall** — top-K over the persona's own `data/memory.db`, keyed
   on the assignment text (`_persona_recall`, `RECALL_MAX_RESULTS`,
   `RECALL_PROMPT_CAP`).

Properties that differ from the Discord path, each deliberate:

- **KEYWORD mode, not AUTO.** `recall_service` stays the sole entrypoint, but
  its AUTO/HYBRID path runs `run_recall_pipeline`, whose step 4.5 fires the
  haiku re-ranker whenever `len(merged) > 3` — and `_merge_and_rank(top_n=…)`
  reorders without truncating, so `max_results` does not bound that list. AUTO
  would put a live LLM call on the work-turn path. KEYWORD is pure FTS5 over
  the persona's own DB: zero LLM, no embedding-model load in the heartbeat
  process. Semantic work-time recall is a follow-up and needs a per-call
  re-rank opt-out first.
- **Every recalled FIELD is re-fenced at the prompt-assembly boundary, not
  just the body.** `recall_service._keyword_only_recall`'s own
  `formatted_text` sanitizes only `r.text` before interpolating `r.path` and
  `r.section_title` raw — a poisoned note heading containing a literal
  `</recalled-memory>` would close the untrusted-data fence early and let
  the rest of the heading read as bare prompt instructions. `_persona_recall`
  therefore ignores `formatted_text` and calls `_sanitized_recall_block`,
  which rebuilds the block from `response.results` (the raw pre-formatting
  fields `recall_service` still returns) and routes path, section title, AND
  body through the identical `sanitize_recalled_content` /
  `wrap_recalled_memory` pair. This is fixed in `worktick.py` at the call
  site — not inside `recall_service`, which is the shared entrypoint for
  chat/heartbeat/reflection/weekly and out of this seam's blast radius — so
  a future `formatted_text` change upstream cannot silently reopen the hole
  for this prompt.
- **The task text is never the query, and one AND query is not enough.**
  `db._quote_fts_query` quotes every whitespace term and ANDs them, so a whole
  assignment sentence demands that all ~15 words co-occur in one chunk —
  structurally zero hits. `_recall_terms` keeps the few most distinctive terms
  (longest first, ties by first appearance, original order preserved) and
  tokenizes Unicode-aware (`[^\W_]+`, so accented/non-English words survive
  whole instead of shredding at the diacritic), so no FTS5 metacharacter from
  operator- or LLM-authored task text can reach the MATCH expression. Even
  with good terms, a real note rarely restates every chosen word, so the
  combined AND query alone still misses relevant-but-not-verbatim notes;
  `_persona_recall` also runs each term alone. All of those queries run —
  taking the first one that happened to hit made retrieval depend on TERM
  ORDER, so an early term's irrelevant note buried a later term's relevant
  one and the later terms were never queried at all. The results are pooled,
  deduplicated by CHUNK IDENTITY (path + line range, hashing the body for a
  degenerate range) keeping each chunk's best score, ranked globally, and
  capped to `RECALL_MAX_RESULTS`. The dedupe key is deliberately NOT the
  section title: `memory_index.chunk_markdown` splits a long section at
  `max_chars` and carries the same `section_title` onto every piece, so
  section-keying would let an early distractor chunk evict the later relevant
  chunk sharing its heading. The combined query runs first and the sort is
  stable, so its higher-precision hits win ties.
- **The index path is checked before the read (Rule 2) — including the
  backend, not just the file.** `resolve_db_path` only returns
  `<profile>/data/memory.db` when the sibling `data/` dir exists; otherwise it
  falls back to a slug DB in the MAIN vault that every persona would share.
  The read is gated on the resolved path being the persona's own file, so a
  half-provisioned profile degrades to a briefing-only prompt instead of
  reading another mind's memory. That file-path check is meaningless on its
  own, though: `db.get_memory_db` ignores `db_path` entirely and returns the
  single shared `PostgresMemoryDB` whenever a Postgres URL is configured, and
  Postgres has no persona/tenant column. Reading `config.DATABASE_URL` would
  not settle that either — `db.py` binds its own `DATABASE_URL` at import
  time, so after any supported config reload/override the two copies disagree
  and the guard can read "SQLite, safe" while the factory hands the search leg
  Postgres. `_persona_recall` therefore asks the REAL factory, with the REAL
  argument the search leg passes, and proceeds only when the object that would
  actually be queried is a `SQLiteMemoryDB` — until persona-grained Postgres
  storage exists. Both backend constructors are lazy (they store a path/URL;
  `_get_conn` does the connecting), so the probe opens no file and no socket,
  and building a prompt still never creates an empty SQLite DB as a side
  effect.

Everything fails open: no memory tree, no built index, no hits, or any error at
all → the exact prompt the loop produced before. The operator's `recall` kill
switch already covers this seam inside `recall_service` (refusals counted
there); the read-back adds no second switch.

## Operator Entry Points

- CLI: `thehomie profile repair [NAME|--all] [--check] [--json]`
- CLI: `thehomie doctor` (inventory checks), `thehomie profile list|show`
- Automatic: cabinet persona turns and `POST /api/agents/{id}/activate`
  self-repair a missing memory dir at boot

## Source Of Truth Files

| Layer | Files |
|---|---|
| Inventory contract + primitives | `.claude/scripts/personas/lifecycle.py` (`_REQUIRED_IDENTITY_FILES`, `_REQUIRED_MEMORY_DIRS`, `_REQUIRED_PROFILE_DIRS`, `InventoryReport`, `inspect_profile_inventory`, `ensure_profile_inventory`) |
| CLI | `.claude/chat/cli.py` (`profile repair`, list/show markers) |
| Doctor | `.claude/chat/diagnostics.py` (`check_environment` inventory block) |
| Boot guards | `.claude/scripts/cabinet/text_orchestrator.py` (`_profile_execution_context`), `.claude/scripts/dashboard_bot_lifecycle.py` (`activate`) |
| Work-time read-back | `.claude/scripts/cofounder/worktick.py` (`build_draft_prompt`, `_persona_memory`, `_fenced_identity_block`, `_identity_chunks`, `_persona_recall`, `_recall_query`, `_cap_recall`) |
| Tests | `.claude/scripts/tests/test_persona_inventory_repair.py`, plus cases in `test_persona_cli_handler.py`, `test_diagnostics.py`, `test_dashboard_bot_lifecycle.py`, `test_cofounder_worktick.py` (read-back) |

## Safety Boundaries

- Seed-if-missing is the load-bearing invariant: repair creates missing dirs
  and stubs missing files, and never touches an existing file (byte-compare
  locked by tests). There is no overwrite mode.
- Repair mutates disk, so it gates on the `persona_mutation` kill-switch
  (same switch as profile create/delete/use). The boot guards pre-check the
  switch with `is_disabled()` and skip silently-but-logged when disabled —
  a kill-switched guard degrades to the old fail-open behavior.
- `inspect_profile_inventory` is pure read-only (no kill-switch needed);
  doctor and `--check` never write.
- Repair repairs existing profiles only — a missing profile root raises
  instead of creating a profile from nothing. The `default` profile is out
  of scope (its memory contract is the install-dir vault, not the PRD tree).
- Every decision reads physical disk state (Rule 2) — there is no cached or
  sidecar "inventory status" that can go stale. A failed boot-guard repair
  needs no event log: the violation is still on disk, so doctor reports it.
- `repair --all` is batch-resilient: one un-repairable directory (e.g. a
  hand-created reserved-name folder under profiles/) is reported and skipped;
  the rest of the fleet still gets repaired, and the exit code stays non-zero
  so nothing looks falsely clean.
- Consumer-managed lock files (`LOG.md.lock`, `WORKING.md.lock`) are NOT part
  of the required inventory — their absence is healthy.

## How To Run It

```powershell
cd <repo>\.claude\scripts

# Read-only fleet audit (exit 1 if any profile violates the inventory)
uv run thehomie profile repair --all --check

# Repair one profile / the whole fleet (idempotent; healthy profiles are no-ops)
uv run thehomie profile repair <name>
uv run thehomie profile repair --all

# Visibility
uv run thehomie doctor
uv run thehomie profile list
uv run thehomie profile show <name>
```

Machine-readable: add `--json` to `repair` (per-profile `InventoryReport`
objects; batch failures appear as `{"name", "error"}` entries) or to
`profile list|show` (`inventory_ok`, `inventory_missing` fields).

## How To Test It

```powershell
cd <repo>\.claude\scripts
uv run pytest tests/test_persona_inventory_repair.py -q
uv run pytest tests/test_persona_cli_handler.py tests/test_diagnostics.py tests/test_dashboard_bot_lifecycle.py -q
```

Tests build synthetic broken profiles in tmp (create, then delete pieces) —
they never touch live profiles.

## Failure Modes

| Symptom | Meaning | Fix |
|---|---|---|
| Persona answers with no knowledge of its own identity/memory | Missing `memory/` dir (pre-contract profile) — context fails open to empty | `thehomie profile repair <name>`; the boot guard also self-heals on the next turn/activation |
| `doctor` error: profile has NO memory/ dir | The silent-lobotomy case above, surfaced | Same as above |
| `doctor` warn: inventory incomplete (N missing) | Profile predates a contract addition (e.g. `episodes/` joined the tree later) | `thehomie profile repair <name>` |
| `doctor` warn: orphaned root identity file(s) | Identity file at profile root — loader never reads it | Diff root copy vs `memory/` copy, merge manually; repair never auto-moves |
| Boot-guard log line: "inventory repair skipped ... kill-switch disabled" | `HOMIE_KILLSWITCH_PERSONA_MUTATION=disabled` blocks the auto-repair | Re-enable the switch or repair manually once |
| `repair --all` exits 1 but most rows say ok/repaired | One un-repairable dir under profiles/ (reserved name, invalid id) | Read the `Error: <name>:` line; rename or remove the stray dir |
