# Persona Memory Isolation And Inventory Repair

Status: shipped 2026-07-07
Owner: personas slice (`personas/lifecycle.py`) with CLI, doctor, and boot-guard surfaces
Last updated: 2026-07-07

## What It Does

Every named persona profile owns an isolated memory vault at
`<profiles-root>/<name>/memory/` — 15 identity files (SOUL.md, MEMORY.md,
GOALS.md, ...) plus 19 memory subdirectories (concepts/, daily/, episodes/,
...). The persona learning loop, reflection, and episode writers all write
into that tree; the cabinet/persona turn context is built FROM it.

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
| Tests | `.claude/scripts/tests/test_persona_inventory_repair.py`, plus cases in `test_persona_cli_handler.py`, `test_diagnostics.py`, `test_dashboard_bot_lifecycle.py` |

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
