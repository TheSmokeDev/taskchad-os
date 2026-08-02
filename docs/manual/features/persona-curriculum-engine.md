# Persona Curriculum Engine

Status: Implemented and live-accepted on `ai-engineer`. The Windows scheduler
installer is intentionally run only after this branch lands in the canonical
checkout so its persistent action never targets a disposable worktree.

Owner: persona profiles + curriculum controller + memory/recall

## Purpose

The curriculum engine turns approved, high-signal source feeds into independent
experts that compound domain judgment over time. It does not clone creators,
fine-tune a model, or dump summaries into the main vault.

One persona owns one private, multi-source curriculum bundle:

```text
<profile>/
├── config.yaml
├── data/curricula/
│   ├── curriculum.db
│   ├── artifacts/
│   ├── raw/
│   └── vendor/
├── memory/curricula/<domain>/
│   ├── index.md
│   ├── log.md
│   ├── concepts/
│   ├── entities/
│   └── sources/
└── state/memory-candidates.jsonl
```

Only synthesized doctrine is recall-indexed. Raw transcripts and vendor seeds
remain under the profile's private data root.

## Learning Contract

A deep study:

1. Reads the complete transcript in bounded segments.
2. Extracts timestamped claims and evidence types.
3. Recalls that persona's existing doctrine.
4. Classifies reinforcement, contradiction, novelty, staleness, experiments,
   and rejected noise.
5. Updates an OKF v0.2-style source dossier and canonical concept pages.
6. Produces zero or more internal application proposals.

The model receives no terminal, browser, deployment, posting, production, or
generic filesystem tools. Admission and study set the runtime's strict
`model_only` contract in addition to `allowed_tools=[]` and
`disallowed_tools=["*"]`. Claude receives an empty advertised tool catalog.
If quota forces a Gemini fallback, a system-precedence one-run configuration
removes core tools, extensions, MCP, hooks, custom discovery, inherited
context, and unrelated Vertex project routing. Codex is ineligible because its
CLI cannot prove a zero-tool surface. The controller owns approved network
reads, private corpus writes, citation validation, recall, indexing, and
proposal persistence.

Transcript text is always wrapped as untrusted evidence. It cannot change the
study prompt, grant tools, escape profile paths, or mint an `explicit`
self-belief. External curriculum changes domain doctrine only. Operator grades
enter the existing persona reflection staging pipeline with
`source="reflection"`.

## Configuration

Each named profile has a strict `curriculum` section:

```yaml
curriculum:
  enabled: true
  domain: ai-engineering
  sources:
    - id: creator-one
      kind: youtube_channel
      url: https://www.youtube.com/@example
      policy: full
      seed_url: https://github.com/example/private-seed-reference
    - id: conference-channel
      kind: youtube_channel
      url: https://www.youtube.com/@conference
      policy: curated
  schedule_hours: 6
  backfill_limit: 120
  metadata_batch_size: 50
  daily_skims: 10
  daily_deep_studies: 3
  steady_daily_deep_studies: 1
  admission_model_tier: fast
  study_model_tier: quality
```

Config is validated on read and written through strict read-modify-write
helpers. Source IDs are unique, URLs must be HTTPS, channel hosts must be
YouTube, batch size cannot exceed 50, and budgets have bounded ranges.

## Discovery And Economy

- Scheduled discovery prefers YouTube RSS and falls back to metadata-only
  `yt-dlp --flat-playlist`.
- Channel watermarks and unique video IDs make restart and duplicate polling
  idempotent.
- Catalog inventory does not download media.
- Cognitive admission runs in batches of at most 50 metadata rows.
- Obvious welcome reels, sponsor/admin noise, and similar low-signal metadata
  remain hard rejects even when a model over-infers from a credible speaker.
- A cognitive model cannot promote a deterministic reject directly to deep
  study; it may request only a bounded transcript skim.
- Deterministic fallback and a persistent rejection reason make provider
  failure inspectable.
- Curated backfills enforce a domain-diverse cap across harnesses/evals,
  memory/context, tools/protocols, production/security, models/data, and
  product/FDE.
- Transcript/audio/frame extraction starts only after admission.

The implementation follows the immutable evidence, canonicalization,
synthesis, validation, and incremental refresh design in the
[Cole Medin knowledge base](https://github.com/coleam00/cole-medin-knowledge-base)
and its [making-of document](https://github.com/coleam00/cole-medin-knowledge-base/blob/main/docs/MAKING-OF.md).
The bundle shape is informed by
[Karpathy's LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
and targets the lifecycle/provenance expectations of
[OKF v0.2](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md).

## Operator Commands

```bash
thehomie curriculum status <persona> --json
thehomie curriculum sources <persona> --json
thehomie curriculum run <persona> [--full-inventory] [--study-limit N]
thehomie curriculum review <persona>
thehomie curriculum route <persona> <proposal-id> [--recipient operator]
thehomie curriculum grade <persona> <proposal-id> <A|B|C|D|F> --note "outcome"
thehomie curriculum enable <persona>
thehomie curriculum disable <persona>
thehomie curriculum import-seed <persona> <local-seed-root>
```

Chat exposes the same core lifecycle:

```text
/curriculum status [persona=<id>]
/curriculum sources [persona=<id>]
/curriculum run [--full-inventory] [persona=<id>]
/curriculum review [persona=<id>]
/curriculum route <proposal-id> [persona=<id>]
/curriculum grade <proposal-id> <A|B|C|D|F> [outcome note]
/curriculum enable|disable [persona=<id>]
```

`route` writes a typed `curriculum_proposal` mailbox delivery. It does not
create a convoy, dispatch an executor, or begin work.

Quiet JSON retains the runtime receipt fields: `success`, `error`,
`session_id`, `lane`, `provider`, `model`, `cost_usd`, `tool_calls`, and
`execution_time_ms`.

## Scheduling

`curriculum_tick.py` is the single scheduler entrypoint. The parent must run as
the default profile. It enumerates named profiles, skips disabled curricula
without a model call or ledger creation, applies each persona's cadence, and
spawns one capability-scoped child process per due persona.

Windows:

```powershell
.\setup_curriculum_scheduler.ps1
Get-ScheduledTask -TaskName SecondBrain-PersonaCurriculum
Disable-ScheduledTask -TaskName SecondBrain-PersonaCurriculum
```

Manual smoke:

```bash
uv run python curriculum_tick.py --test
uv run python curriculum_tick.py --once
```

Set `HOMIE_KILLSWITCH_PERSONA_CURRICULUM=disabled` to stop all discovery and
study immediately. Disabling one profile preserves its ledger and doctrine.

## Seed And Delta Import

`curriculum import-seed` accepts a local synthesized Markdown tree. It migrates
concept, entity, and source pages into the persona-private v0.2 bundle. It
does not redistribute the upstream repo, and it never copies raw/vendor
content into framework memory.

For a channel with an existing private seed:

1. Keep the upstream clone under `<profile>/data/curricula/vendor/`.
2. Import its synthesized pages.
3. Run a live metadata inventory.
4. Compare immutable source IDs in the ledger.
5. Admit only channel IDs absent from the seed/ledger.

Unlicensed or license-unknown source corpora must remain private and are denied
from the public sanitizer output.

## Verification

```bash
cd .claude/scripts
uv run pytest tests/test_curriculum.py tests/test_command_menu.py \
  tests/test_persona_subcommand_collision.py -q
```

Acceptance requires config/path isolation, hostile-transcript refusal, OKF
validation, one admitted and one rejected live source, cited persona recall,
zero provider calls for a disabled founder curriculum, proposal-only routing,
persona-only grade staging, and a sanitizer proof that no private corpus data
enters the public framework.

Keep live acceptance IDs, counts, learned doctrine, grades, and proposal
receipts in private PRP/handoff evidence. The public manual documents only the
framework mechanism and operator contract.

## Related Manuals

- [Persona Team](persona-team.md)
- [Persona Blueprints And Capability Provisioning](persona-blueprints-capability-provisioning.md)
- [Persona Learning Loop](persona-learning-loop.md)
- [Video Learning (`/watch`)](video-learning.md)
- [The Living Self Manual](../../the-living-self-manual.md)
- [Commands Reference](commands-reference.md)
- [Scheduled Jobs, Settings, And Audit](scheduled-settings-audit.md)
