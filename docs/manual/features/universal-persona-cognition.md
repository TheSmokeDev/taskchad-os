# Universal Persona Cognition

Status: Foundation shipped in August 2026; continuous cognition shipped in v1.9.0
Owner: Framework (personas + memory pipelines + cofounder + curriculum)
Last updated: 2026-09-10

[Persona Harness Learning](persona-harness-learning.md) documents the continuous
cognitive lifecycle: reorient, interpret, investigate, revisit, reflect and
retain, and carry forward. The main Homie and specialists retain understanding
and unfinished questions independently of whether a new procedure qualifies for
adoption. The [developer guide](persona-harness-learning-developer.md) covers
framework function hooks and domain evidence collectors.

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

## Unified Lifecycle

The unified implementation coordinates experience, interpretation, reflection,
deeper dream consolidation, appropriate evaluation, and later use through the
same persona-local journal and queue. Existing scheduled reflection and dream
commands admit or wake this work. The foreground runtime has priority; pause,
explicit disable, provider deferral, and retry policy apply to every stage.

Tentative understanding can be retained immediately. Supported descriptive
knowledge requires source support; standing behavioral changes require independent
qualification through the shared authority. Explicit operator instructions and
manual edits retain their authority. Repeated appearances of one source are not
independent evidence. Legacy beliefs and journal versions link back to the same
source identities, so contradictions and supersession reach later readers.

Reflection and dream inputs include changed journal understanding, open
investigations, original observations, counterevidence, and episode excerpts.
Exact source revision/range manifests distinguish consumed, partial, and omitted
inputs. Retained investigative conclusions can seed a procedure proposal as
derived context without becoming a fresh observation.

The Learning tab and `profile learning lifecycle` expose stage receipts, output
IDs, pending consumers, skips, and actual model calls. Retrieval tuning has its
own labeled cases, source-family split, comparisons, versioned policy, and
rollback. See the [operator contract](persona-harness-learning.md#unified-reflection-dreaming-and-recall-tuning).
These are implementation contracts; installation-specific live acceptance and
release receipts must be checked separately.

## Foundation Before Lifecycle Unification

The following diagram records the earlier architecture. Its direct automatic
reflection/dream writers and separate eligibility are superseded by the shared
admission and authority described above.

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

## Continuous Persona Cognition (v1.9.0)

```text
reorient with identity, understanding, evidence, and open questions
  -> interpret meaningful work with actual model reasoning
  -> retain a bounded conclusion or open an investigation
  -> revisit when its validated time/evidence condition is due
  -> reassess with the original situation and fresh evidence
  -> carry relevant retained versions into later ordinary work
```

Host-attributed function hooks persist work for the Python-owned reasoning
worker. They serve the same persona lifecycle on Codex, Kimi, Claude, and other
configured runtimes. Claude Mods are an optional adapter. Understanding may
remain tentative or become source-supported; a no-change conclusion is also a
valid result. A later executed-context receipt identifies which versions reached
the request. Observation-only work can complete this cycle without a trade or
adopted method.

### Procedure Qualification Within The Cognitive Lifecycle

The [harness operator guide](persona-harness-learning.md) covers the additional
loop that tests proposed improvements and follows their later use. Both the
default Homie and named personas use the same Python-owned lifecycle:

```text
relevant learned methods enter the next turn
  -> expectation before a meaningful action
  -> actual execution and later observations
  -> conditional knowledge, self-model, or procedure candidate
  -> frozen paired evaluation on held-out cases
  -> provisional adoption through versioned skills/amendments
  -> executed-context receipt on later work
  -> subsequent outcomes, revision, or rollback
```

Each link has a separate record. A missing expectation stays missing; a completed
runtime call does not establish a business outcome. Practice qualification can
support provisional use, while absent real outcomes remain unknown. Later
counterevidence can retire a method and prompt a revision. The
[harness developer guide](persona-harness-learning-developer.md) explains the
shared hooks and domain evidence collectors.

New profiles write `learning.enabled: true`; valid profiles without the section
or key use the shared lifecycle default. Explicit `false` disables admission and
work. Pause suppresses capture, dynamic learned context, and background lifecycle
activity, including synthesis and tuning, while preserving history and already
applied skills/amendments. Rollback separately retires an adopted method or
restores a predecessor retrieval policy.

## What runs when (scheduled inventory)

| Job | Cadence | Silent path | State |
|---|---|---|---|
| Worktick experience writer | per executed assignment | receipt `error` never fails the assignment | note receipt in the worktick result |
| `persona_learning_tick.py` | scheduled reflection compatibility entry point | unchanged source/interval/disabled receipt invokes no model | shared persona synthesis journal and queue |
| `persona_dream_tick.py` | nightly dream compatibility entry point | no useful new source or disabled receipt invokes no model | shared dream admission and exact consumption receipts |
| Curriculum tick | per-persona cadence | disabled curricula skipped free | curriculum ledger per profile |
| Cognitive dispatcher and harness worker | supervised chat-service dispatcher checks every 60 seconds; existing heartbeat/reflection/dream seams provide recovery wakes | no useful or due work invokes no model; foreground work, pause, or unavailable capabilities defer work | per-profile learning journal and queue, plus installation-wide dispatcher/learner/activity leases |

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
| `thehomie profile learning disable <name>` | Set the shared config switch false for legacy reflection and harness learning; preserve existing content. |
| `thehomie profile learning summary [name] --json` | Inspect harness counts, active methods, context coverage, and queue state; omitted name selects default. |
| `thehomie profile learning pause [name]` / `resume [name]` | Pause/resume the harness without removing applied methods or overriding an explicit config disable. |
| `thehomie profile learning rollback <name> <activation-id>` | Retire the selected adopted method through its recorded activation. |
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
| `PERSONA_LEARNING_ENABLED=false` | legacy persona learning ticks and harness learning |
| `HOMIE_KILLSWITCH_HARNESS_LEARNING=disabled` | harness capture, dynamic context, and background learning |
| `HOMIE_KILLSWITCH_BELIEF_AUTONOMY=disabled` | dream Phase-5 belief adoption — PROPAGATES to persona dream children (the whole `HOMIE_KILLSWITCH_*` class is threaded into spawned children) |
| `HOMIE_KILLSWITCH_PERSONA_CURRICULUM=disabled` | all curriculum discovery/study incl. learn drops |
| per-persona `learning.enabled: false` | one persona, surgical |

Corpus caps, note caps, and window knobs are call-time resolved — see
[Persona Learning Loop](persona-learning-loop.md) and
[Persona Experience Notes](persona-experience-notes.md) knob tables.

## Security invariants (load-bearing — do not weaken)

- **Provenance**: the reflection pipeline forces its persona-sourced
  beliefs/lessons to `source="reflection"`. Harness source observations and
  qualifications retain their own typed evidence records; neither mechanism
  lets a persona's reading mint a protected `explicit` belief.
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
5. **Understanding and investigation**: follow actual evidence to a cycle's
   reasoning receipt, retained understanding or question, later evidence, and
   reassessment. Then inspect the exact retained versions in an executed context
   receipt from later ordinary work. A cycle can retain useful understanding
   while its source-support check remains pending. Use the
   [complete-cycle walkthrough](persona-harness-learning.md#verify-one-complete-learning-cycle).
6. **Qualified change**: inspect a harness candidate's supporting and
   counterevidence, paired evaluation, and activation. A saved lesson or a
   model's confidence alone does not establish improvement.
7. **Actual method use**: follow the activation/version to a later `executed` context
   receipt with its real model/provider. `prepared` and `submitted` receipts
   show assembly and attempts, not completed use; confirm the resulting work
   as well.
8. **Outcome and reassessment**: follow that experience to a subsequent
   observation and any revision or rollback. Keep absent outcomes unknown.
   The full chain can show whether a tested method helped in those cases;
   it does not by itself establish durable improvement across the domain.

## Watch items (post-ship)

- Release availability does not prove a particular installation is learning.
  Use current cycle, investigation, execution, and context receipts. Dated rollout
  results belong in the release receipt; increasing note counts alone does not
  prove improved judgment.
- Voice policy question awaiting operator ratification: read-only tools
  (incl. memory search) answer for ANYONE in a voice channel (the ported
  hermes-talk principle). See PR #449's note.

## The sibling epic

Self-provisioning ("give yourself that tool", epic #419) shares the role
seam and the audit doctrine: [Persona Self-Provisioning]
(persona-self-provisioning.md) — executor shipped (PR #431); chat surfaces
land with #427/#428/#429.

## Detailed pages

- [Persona Harness Learning](persona-harness-learning.md) — understanding, investigations, qualified methods, reports, and verification
- [Harness Developer Guide](persona-harness-learning-developer.md) — extend shared hooks and domain producers
- [Persona Experience Notes](persona-experience-notes.md) — the writer, caps, receipts
- [Persona Learning Loop](persona-learning-loop.md) — the tick, the composed gate, the distiller
- [Persona Memory Isolation](persona-memory-isolation.md) — trees, indexes, recall binding
- [Persona Curriculum Engine](persona-curriculum-engine.md) — feeds + the learn drop
- [Episodes](episodes.md) · [Scheduled Jobs, Settings, and Audit](scheduled-settings-audit.md)
- The Living Self (main-homie stack this generalizes): `docs/the-living-self-manual.md`
