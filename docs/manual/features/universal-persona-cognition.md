# Universal Persona Cognition

Status: Shipped (epic #418, 2026-08-13 — six tickets, PRs #430/#437/#442/#441/#449/#444)
Owner: Framework (personas + memory pipelines + cofounder + curriculum)

For the experience-to-evaluation-to-adoption cycle and its operator controls, see
[Autonomous Persona Harness Learning](persona-harness-learning.md). That extension
reuses this cognitive foundation and records whether changed methods were tested,
delivered in future context, and supported by subsequent observations.

This is the operator's chapter for the whole cognitive machine. Each subsystem
has its own detailed page (linked below); this page is the map: what runs,
when, how to drive it, and how to verify it is actually learning.

## The doctrine

> "There shouldn't be no off button. You're using this, it's gonna be running
> on. … I want the full dream cycle on everybody. Each persona is just as
> powerful as the main homie — it's not some sub-homie. Just as powerful, has
> all the things — but designed for that vertical."

There is no main-homie/sub-homie hierarchy in cognitive infrastructure. A
persona = the full Living Self stack scoped to a vertical. Any cognitive
capability shipped to the main homie is a gap on every persona until ported.
Learning grants MEMORY, never capabilities — every external-mutation gate is
untouched by this system.

## The loop (what happens, end to end)

```
create persona ──► BORN LEARNING (config written at every creation door)
      │
      ▼
persona EXECUTES work (cofounder worktick assignment, crypto round, …)
      │
      ├─► deterministic experience note appended to ITS OWN tree
      │     ~/.homie/profiles/<id>/memory/experience/YYYY-MM-DD.md
      │     (zero LLM, fail-open, reindexed same-day)
      │
      └─► NEXT execution READS BACK: capped MEMORY.md + top-K recall over
            the persona's own memory.db ride the work prompt (fenced)
      │
      ▼  nightly
persona_learning_tick  (gate: chat rows OR fresh notes since last run)
      ├─► chat-corpus belief pass (operator-interaction beliefs, unchanged)
      └─► NOTES DISTILLATION: no-tools structured LLM (model_only contract,
            capable-lane fallback) → HOST writes lessons into the persona's
            MEMORY.md (forced reflection provenance) → reindexed
      │
      ▼  nightly (~3:30 AM, after the main dream)
persona_dream_tick  →  memory_dream.py -p <name>  per persona
      orient → gather → consolidate → prune → Phase-5 belief evolution
      (DREAM_SILENT zero-cost when no signal; truth-tabled receipts)
      │
      ▼  on demand
"learn this": /curriculum learn <url> · @<persona> learn <url>  (YouTube →
      the curriculum admission→study pipeline, pre-admitted, model_only)
      thehomie persona ingest <name> <file|text>  (articles/text → experience notes)
```

## What runs when (scheduled inventory)

| Job | Cadence | Silent path | State |
|---|---|---|---|
| Worktick experience writer | per executed assignment | receipt `error` never fails the assignment | note receipt in the worktick result |
| `persona_learning_tick.py` | scheduled (12h recency guard) | `PERSONA_REFLECT_SILENT` (no chat rows AND no fresh notes) | `persona-learning-<name>-state.json` (main STATE_DIR); `last_attempt`/`last_run` split |
| `persona_dream_tick.py` | nightly after the main dream | `DREAM_SILENT` per persona (zero LLM) | fan-out stamps in main STATE_DIR; each persona's `dream-state.json` in ITS profile tree |
| Curriculum tick | per-persona cadence | disabled curricula skipped free | curriculum ledger per profile |

## Apartments — main reads across persona vaults (issue #466)

Each persona owns an isolated vault-tree + recall index at birth
(`~/.homie/profiles/<id>/memory` + `<id>/data/memory.db`). The apartments
completion adds the one missing direction: the MAIN homie can now read every
apartment, read-only, on demand.

| Ask | Today |
|---|---|
| Each persona its own vault/compartment | YES — separate tree + own index, isolated |
| Personas can't see each other | YES — enforced + tested |
| Personas can't see the main vault | YES |
| Main homie can access all | YES — main reads across apartments, read-only, on demand |

**Vault naming rule: bare persona ids.** Every live persona vault sits on the
same shelf as `thehomie` and `coding-vault` under its PLAIN profile id —
"check the sales vault" is `--vault sales` / `/vault search <q> --vault sales`.
No `persona:` prefix. The static pair wins on a name collision: a persona
named `coding-vault` is shadowed, not merged. The registry is physical (dirs
on disk, resolved per call) — a persona created moments ago is addressable
immediately; a deleted one drops out on the next call.

**`--vault all` (alias `apartments`) fan-out.** Sweeps main + every registered
vault with one KEYWORD pass per vault and merges with per-vault attribution
(`[vault:sales]` on each hit). Merge policy is a per-vault cap — each vault
contributes up to its own top-k; results are never globally score-sorted into
one pool, so a single loud vault cannot crowd out the rest. Graph traversal is
skipped on fan-out (bounded read over each vault's own FTS5 index).

**The fence is one-way.** Main→persona only. Persona reads open the DB
`mode=ro&immutable=1` (no create, no WAL sidecars, no `init_schema`); an
unbuilt persona index returns empty results and is never created; under a
configured
`DATABASE_URL` the persona read stays on its own per-persona SQLite (the
shared Postgres has no persona column). Persona processes never see the
estate: outside the default profile the persona registry is empty, and a
persona-bot process asking for `all` gets its own vault only. Indexing stays
persona-owned (`memory_index.py -p <name>`) — main never writes a byte into an
apartment. Default chat recall is UNCHANGED (`engine.py` still targets the
main vault only).

**Why `immutable=1`, and the one thing it costs.** `mode=ro` alone is not
enough: the persona's OWN writer leaves the DB header in WAL mode, and a
read-only open of a WAL database still CREATES `-wal`/`-shm` sidecars in that
persona's tree (a read-only connection cannot remove them on close). Reading
the main file only is what makes the read footprint actually zero. The cost:
a read taken WHILE that persona is mid-write sees the DB as of its last
checkpoint, so a just-indexed note can be briefly invisible from the main
side. SQLite checkpoints when the persona's last connection closes, so the
window is the duration of that persona's own write, not a lasting lag.
Recall is fail-open, so the worst case is an empty sweep of that one vault —
never a corrupted one.

## Persona write gates (epic #465) — the action-proposal rail

Read toolsets (`browser_read`, `seo_geo_read`, …) grant reach. WRITE tools
never ride a bare grant: each lives in a dedicated-gate toolset whose handlers
only PROPOSE. The rail is one line: **propose → `/act approve <persona>
<code>` → execute the stored payload → experience receipt**. The card the
persona returns names the exact target (handles, domain, measurement id) —
the card is the authorization. Approving mints a one-use execution token
bound to the stored payload; the driver consumes it before anything moves, so
a replayed or hand-typed call fails closed. Every outcome lands in the
persona's own experience notes with per-target detail, and in the
`persona_action_proposals.jsonl` ledger in the persona's profile.

| Toolset | Tools | What an approval executes |
|---|---|---|
| `x_social_write` | `x_follow_accounts`, `x_enable_notifications` | Follows / notification bells on X via the visible browser (attach-only, port 18222, per-handle receipts) |
| `ga4_fleet_write` | `ga4_provision_site`, `ga4_deploy_tag` | GA4 Admin API property/stream reconcile (converge, never duplicate) and the Vercel `NEXT_PUBLIC_GA_MEASUREMENT_ID` sync + live-tag verification for a fleet brand |

Both refuse `request_tool` one-time elevation by construction
(`dedicated_gate=True`). The kill switch
`HOMIE_KILLSWITCH_PERSONA_ACTION_PROPOSALS=disabled` turns the whole rail off
and is re-checked per target mid-batch.

Grant-side, the two grains of reach (#465 1c): a TOOLSET grant
(`/persona grant <persona> <toolset>`, counter-offer `<<GRANT_REQUEST:
name>>`) adds a bundle; a TOOL grant (`/persona grant <persona>
tool:<name>`, marker `<<GRANT_REQUEST: tool:name>>`) adds one registered
capability to `tools:`. Both are reach-only — granting a dedicated-gate write
tool above lets the persona PROPOSE through `/act`, never execute.

## Which surfaces carry tools (#465 1b/1d — the per-surface calls)

| Surface | Tools? | The call |
|---|---|---|
| Discord persona channel | YES | full scoped loop + counter-offer + buttons |
| Chat engine persona turn (Telegram etc.) | YES | full scoped loop + counter-offer |
| Dashboard web chat | YES (1b) | same loop; cards point at main-chat `/grant`/`/act` (no in-dashboard buttons yet) |
| Cabinet text turns | YES (text only) | scoped loop; voice excluded by design |
| Cofounder worktick | NO — deliberate | Scheduled draft work stays text-only (`worktick.py` `allowed_tools=[]`): an unattended tick should draft, not act. Capability acquisition happens on interactive surfaces; the next interactive turn has the loop. |
| Talk voice | separate | `talk_tools` (memory search, homie_command, delegate, run_skill); persona write rail does not apply |

Lane-side (#465 1d): only lanes whose adapter carries caller tools run the
loop — `claude_sdk`, `openai_compatible`, `codex_app_server_gate`. The router
probes `supports_caller_tool_defs()` and EXCLUDES non-carriers
(`lane_router.py:158`), and a non-carrier handed a tool turn refuses honestly
instead of silently dropping the tools (e.g. `gemini_cli.py:82`).
`gemini_cli` is text-only by construction: `--allowed-tools` gates the CLI's
OWN built-ins and is not a tool-definition surface. Consequence: a persona
turn with granted tools never silently loses them to the gemini lane — the
router picks a carrier lane or the turn refuses loudly.

## Operator commands

| Command | What it does |
|---|---|
| `thehomie profile create <name>` (any door: CLI, dashboard, blueprint) | newborn is BORN learning (`learning: {enabled: true}` + audit row + `memory/experience/` dir). Sentinel names (`default`, `custom`) are rejected at every door. |
| `thehomie profile learning disable <name>` | surgical per-persona off (debugging only — not part of any product flow) |
| `/curriculum learn <url> [persona=<id>]` · `@<persona> learn <url>` | drop one YouTube link into the persona's curriculum study pipeline (operator-role-gated, pre-admitted, hostile-transcript wrapping intact) |
| `thehomie persona ingest <name> <file\|text>` | drop an article/text into the persona's experience notes (reindexed; distilled that night) |
| `uv run python persona_learning_tick.py --test` / `--once` | dry-run / single-persona tick |
| `uv run python persona_dream_tick.py --test` | side-effect-free dream fan-out check (writes `last_test_*` only, no LLM calls, no vault writes) |
| `thehomie recall "<q>" --vault <persona-id>` · `/vault search <q> --vault <persona-id>` | read ONE apartment by its plain profile id, read-only (#466) |
| `thehomie recall "<q>" --vault all` (alias `apartments`) | sweep main + every persona vault, keyword pass per vault, `[vault:<id>]` attribution on each hit (#466) |
| `uv run python memory_index.py --vault <persona-id>` | **refused** — indexing is persona-owned; run it as that persona (`memory_index.py -p <id>`) |

## Kill switches and knobs (the fire extinguishers)

| Switch | Scope |
|---|---|
| `PERSONA_LEARNING_ENABLED=false` | the whole learning tick family |
| `HOMIE_KILLSWITCH_BELIEF_AUTONOMY=disabled` | dream Phase-5 belief adoption — PROPAGATES to persona dream children (the whole `HOMIE_KILLSWITCH_*` class is threaded into spawned children) |
| `HOMIE_KILLSWITCH_PERSONA_CURRICULUM=disabled` | all curriculum discovery/study incl. learn drops |
| per-persona `learning.enabled: false` | one persona, surgical |

Corpus caps, note caps, and window knobs are call-time resolved — see
[Persona Learning Loop](persona-learning-loop.md) and
[Persona Experience Notes](persona-experience-notes.md) knob tables.

## Security invariants (load-bearing — do not weaken)

- **Provenance**: every persona-sourced belief/lesson is `source="reflection"`
  by host construction; nothing a persona reads can mint a sacrosanct
  `explicit` belief.
- **Confinement**: the notes distiller is a NO-TOOLS structured call
  (`model_only` + `disallowed_tools=["*"]`, profile-root cwd, capable-lane
  fallback); the HOST applies amendments, policy-constrained to the persona's
  own MEMORY.md. Nothing model-authored escapes the profile root.
- **Fencing at composition**: note-derived amendments are fenced as untrusted
  at the briefing-composition layer — every persona surface (Discord, web,
  Cabinet, worktick prompts) inherits the fence; identity files injected into
  work prompts pass the same containment.
- **Target-vs-ambient keying**: everything keyed to the TARGET persona
  (ledger, lock, config, notes, state) — never the ambient process profile.
- **Role ingress**: adapters stamp `user_role` from their own authenticated
  identity; default is `viewer` (fail-closed); voice commands resolve
  per-utterance via interval-bound speaker authorization (ported from
  hermes-talk) — mutating tools deny on ambiguity.

## How to verify it is actually learning (receipts, not vibes)

1. **Experience trail**: `ls ~/.homie/profiles/<id>/memory/experience/` —
   one dated file per working day; sections carry agenda refs + outcomes.
2. **Index reach**: `cd .claude/scripts && uv run python memory_search.py
   "<recent task term>" --mode keyword` with the persona's memory dir — the
   note should surface.
3. **Distilled lessons**: grep the persona's `MEMORY.md` for the distillation
   section; rows in the tick state (`candidates`/`written` counts).
4. **Dream receipts**: each persona's `dream-state.json` shows `consolidated`
   or an honest `DREAM_SILENT` with a spawn-fresh receipt (truth-tabled — a
   stale or missing receipt never reports success).
5. **Compounding proof (the point of it all)**: a work deliverable that
   references a prior note/lesson — the worktick prompt carries the recall
   block; check a draft's content against the persona's earlier notes.

## Watch items (post-ship)

- The crypto persona's FIRST live distilled market lesson lands on the next
  nightly tick (its lane resolution was fixed the day of ship — receipt:
  `round5-live-crypto-lane-receipt.json` in the #425 run artifacts).
- Voice policy question awaiting operator ratification: read-only tools
  (incl. memory search) answer for ANYONE in a voice channel (the ported
  hermes-talk principle). See PR #449's note.

## The sibling epic

Self-provisioning ("give yourself that tool", epic #419) shares the role
seam and the audit doctrine: [Persona Self-Provisioning]
(persona-self-provisioning.md) — executor shipped (PR #431); chat surfaces
land with #427/#428/#429.

## Detailed pages

- [Persona Experience Notes](persona-experience-notes.md) — the writer, caps, receipts
- [Persona Learning Loop](persona-learning-loop.md) — the tick, the composed gate, the distiller
- [Persona Memory Isolation](persona-memory-isolation.md) — trees, indexes, recall binding
- [Persona Curriculum Engine](persona-curriculum-engine.md) — feeds + the learn drop
- [Episodes](episodes.md) · [Scheduled Jobs, Settings, and Audit](scheduled-settings-audit.md)
- The Living Self (main-homie stack this generalizes): `docs/the-living-self-manual.md`
