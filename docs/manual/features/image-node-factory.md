# Image Node Factory (Archon Workflow)

Status: Active; DAG live-proven 2026-07-09 (grounded retrieval + a 56-image render batch, 56 distinct sha256); publish-hardened 2026-07-10 (validate-pack node + subject_mode=placeholder)
Owner: `.archon/workflows/image-node-factory.yaml`, `.archon/commands/image-node-*.md`, `.archon/scripts/style-corpus.py`, `.archon/scripts/pack-validate.py`
Last updated: 2026-07-10

## What It Does

Turns a natural-language visual brief into a production-ready **prompt pack**, and
optionally into rendered bitmaps. It is the image counterpart to the coding
workflows in [archon-workflows](archon-workflows.md): an operator invokes it, it
runs a declared DAG, and it leaves auditable artifacts behind.

The style intelligence is not hardcoded. A pinned, checksum-verified local copy of
the MIT-licensed `awesome-gpt-image-2` style corpus supplies 511 worked cases and
22 structured templates across 13 categories. The `select` node picks the
strongest template per brief and cites case ids; the `ground` node resolves those
ids offline. Nothing is invented and then attributed.

Two render disciplines ship in every pack and the operator picks:

| Discipline | Meaning |
|---|---|
| `baked` | Copy is rendered inside the image. The library-native approach. |
| `overlay` | A text-free scene plus a separate `copy` block for crisp HTML overlay. |

Default is `render=false`: you get the prompt pack, packet, and manifest without
generating a single image.

## Operator Entry Points

- **CLI**: `archon workflow run image-node-factory "<brief> [controls]"`
- Installed globally at `~/.archon/workflows/`, so it runs from any repo.
- No chat command, no dashboard route, no HTTP API. It is invoke-only by design.

## Source Of Truth Files

| Layer | Files |
|---|---|
| Workflow DAG | `.archon/workflows/image-node-factory.yaml` |
| Node prompts | `.archon/commands/image-node-{intake,select,prompt-pack,render,qa,report}.md` |
| Retrieval script | `.archon/scripts/style-corpus.py` |
| Pack validator | `.archon/scripts/pack-validate.py` |
| Discipline cards | `.archon/image-nodes/*.md` (7 cards) |
| Corpus cache | `~/.archon/cache/skill-ports/gpt-image-2-style-library/<pin>/` (outside every repo tree) |
| Tests | `.claude/scripts/tests/test_style_corpus.py`, `.claude/scripts/tests/test_pack_validate.py` |
| Port contract | [skill-to-workflow-port](skill-to-workflow-port.md) |

## The DAG

Workflow-level settings: `provider: codex`, `modelReasoningEffort: medium`,
`webSearchMode: disabled`, `worktree.enabled: false`.

| Node | Kind | Depends on | Gate / notes |
|---|---|---|---|
| `preflight` | `bash` | (none) | Creates `$ARTIFACTS_DIR/images`, writes `image-node-preflight.json`. `timeout: 30000`. |
| `intake` | command `image-node-intake` | `preflight` | Parses the brief into brief JSON. Never picks a template. |
| `select` | command `image-node-select` | `intake` | Schema-validated against 22 `template_id`s, 13 categories, 7 `discipline_card`s. Emits integer `example_case_ids`. |
| `ground` | `script: style-corpus`, `runtime: uv` | `select` | Offline resolution. `timeout: 120000`. Exits non-zero ONLY on a missing or corrupt corpus. |
| `prompt-pack` | command `image-node-prompt-pack` | `preflight, intake, select, ground` | Writes fresh prompts. Stamps provenance if and only if `grounded` is true. |
| `validate-pack` | `script: pack-validate`, `runtime: uv` | `intake, ground, prompt-pack` | Deterministic re-check of the pack against the physical grounding artifact. `timeout: 60000`. Exit 1 fails the run. |
| `render` | command `image-node-render` | `intake, prompt-pack, validate-pack` | `when: $intake.output.render_requested == 'true'`. `idle_timeout: 900000`. |
| `qa` | command `image-node-qa` | `prompt-pack, validate-pack, render` | `trigger_rule: none_failed_min_one_success` (so it still runs when `render` is skipped). |
| `report` | command `image-node-report` | `qa, render` | Same trigger rule. |

`webSearchMode: disabled` is load-bearing, not incidental. It is precisely why a
skill reference that is only a URL resolves to nothing inside a node, and
therefore why `ground` exists at all. See
[skill-to-workflow-port](skill-to-workflow-port.md) for the general invariant.

## The Retrieval Contract

The shape is **AI decides, script retrieves, AI consumes. The AI never fetches.**

```
select       (AI)      emits a retrieval REQUEST: template, filters, candidate case ids
   |
ground       (script)  PURE + OFFLINE. re-hashes the pinned cache, resolves ids -> cases
   |
prompt-pack  (AI)      reads the resolved exemplars, writes FRESH wording,
                       stamps provenance IFF grounded
```

`ground` prints a small JSON object to stdout, so `$ground.output.grounded` is
usable downstream:

| Key | Meaning |
|---|---|
| `grounded` | `true` when at least one case resolved |
| `matched` | number of cases that matched the filters |
| `resolved_case_ids` | ids that resolved to a non-empty case body |
| `unresolved_case_ids` | cited-but-absent ids (kept distinct from "matched zero") |
| `prompt_engine` | present only when grounded |
| `corpus_pin`, `corpus_sha256`, `corpus_source`, `license` | provenance |

Three outcomes, and only three:

| Situation | `grounded` | exit | What `prompt-pack` does |
|---|---|---|---|
| Cache present, matches found | `true` | 0 | Stamps `prompt_engine`, `corpus_pin`, `corpus_sha256`, `license`, resolved ids |
| Cache present, zero matches | `false` | 0 | OMITS `prompt_engine` and `example_case_ids`; sets `self_authored: true` |
| Cache missing or corrupt | (n/a) | **1** | Node fails, every dependant is skipped, the run fails loudly |

The invariant is **never stamp a citation that does not resolve** -- not "die if
the data is empty". A cold cache is a provisioning bug and must be loud. Zero
matches is a legitimate answer and must be honest.

The stamping rule is written into `image-node-prompt-pack.md`, which an LLM
follows -- and an instruction is a suggestion. The `validate-pack` node is the
enforcement: pure script, it re-reads the pack and the grounding artifact and
exits 1 (skipping render/qa/report) when a `grounded:false` pack carries any
provenance key, a cited case id is outside the resolved set, provenance fields
differ from the grounding, the concept count breaks the cap of 8, a prompt
variant is empty, a placeholder pack lacks its sentinel, or the pack text
contains an absolute local path.

The guard reads physical state: `verify` re-hashes the cached bytes on every
call and never trusts a sidecar `.ok` marker, so a truncated download fails.

### Corpus facts (pinned)

| Fact | Value |
|---|---|
| Upstream | `freestylefly/awesome-gpt-image-2`, MIT |
| Pin | `a04beebfa3195ef8dfbf1c57da7df9e989c2173b` |
| `cases.json` sha256 | `3c88ef3d3c15ca319992fc82f860de6674412fe913a585a50664fc2a687261b3` |
| Cases | **511** (ids run 1..514 with 3 gaps; 514 is the highest id, not the count) |
| Templates | 22 |
| Categories | 13 |
| English-only prompts | 224 |

Provision once per machine. This is the only network step, and it never runs
inside the DAG:

```bash
uv run .archon/scripts/style-corpus.py prime
```

## Inline Controls

Append any of these to the brief string. `intake` parses them.

| Control | Values | Notes |
|---|---|---|
| `category=` | a corpus category, or `auto` | |
| `render_mode=` | `baked` \| `overlay` | Both variants are emitted regardless; this picks what renders. |
| `aspect=` | ratio or size | Stated in prose. See the aspect trap below. |
| `count=` | number | **Never above 8.** `intake` clamps it. |
| `render=` | `true` \| `false` | Default `false`. |
| `design_file=` | relative path, or `none` | Brand palette source. Role only, never inlined. |
| `persona_pack=` | relative path, or `none` | Subject likeness lock. Role only, never inlined. |
| `subject_mode=` | `generic` \| `placeholder` | Default `generic` (the pack invents a neutral subject -- see trap 1). `placeholder`: every prompt's `Subject:` carries the literal `[SUBJECT SUPPLIED AT RENDER TIME]` slot for a downstream renderer to fill; the pack never invents. |
| `exact_text=` | `"verbatim copy"` | For the baked variant. |

## Artifacts

Written to `$ARTIFACTS_DIR` (outside the repo tree):

```
image-node-preflight.json     image-node-prompt-pack.json
image-node-brief.json         image-node-imagegen-packet.json
image-node-selection.json     images/manifest.json
image-node-prompt-pack.md     qa-report.md
                              image-node-final-report.md
```

## The Prompt Schema

Every concept `prompt-pack` writes uses this 13-field structure. Knowing the
field names matters, because correcting a generated prompt means **replacing a
field**, not appending to the end of it.

```text
Use case:            <library category and asset destination>
Template:            <selected template_id>
Primary request:     <operator main request>
Input references:    <design_file and persona_pack roles, or none>
Scene/backdrop:      <environment>
Subject:             <main subject and placement>
Style/medium:        <photo, illustration, 3D, and so on>
Composition/framing: <placement; reserved empty zones for overlay>
Lighting/mood:       <lighting and mood>
Color palette:       <palette notes, from design_file when supplied>
Text handling:       <baked verbatim text, OR text-free with a forbid-text clause>
Constraints:         <must keep and must avoid>
Avoid:               <negative constraints from selection>
```

Every concept in `image-node-prompt-pack.json` carries **both** a `baked_prompt`
and an `overlay_prompt`, plus a `copy` block (`eyebrow`, `headline`, `subhead`,
`cta`). Switching discipline after the fact needs no workflow re-run: the other
variant is already in the pack.

## Safety Boundaries

- Default is `render=false`. Rendering is opt-in.
- The `render` node uses **Codex built-in image generation only**. It must never
  use an API key, an SDK script, an external marketplace service, or any fallback
  CLI/API renderer. When built-in generation is unavailable it writes a *blocked*
  manifest rather than silently falling back.
- No node may fetch: `webSearchMode: disabled`. `ground` is pure and offline. The
  only network step is the out-of-band `prime`.
- Artifacts are marketplace-public by construction: no absolute local paths, no
  private system names, no brand names, no persona names, no invented slogans,
  claims, or people.
- The corpus never enters git or a shipped artifact. Only the fetcher ships. MIT
  is satisfied by a `LICENSE` copy beside the cache plus a provenance stamp on
  every grounded artifact.

## Known Traps

Read these before the first run. Each one cost real renders.

### 1. `persona_pack=none` makes the pack invent a subject

This is deliberate, and it is the single most expensive trap. To stay
publishable, `prompt-pack` is told: *"When both are `none`, use a clean neutral
world and identity-stable generic traits."* So it does not leave `Subject:`
blank. It **writes a generic person or mascot into the prompt body**, describing
hair, face, age, or silhouette in prose.

If you later attach real reference images at local render time, an identity
paragraph **appended to the end of the prompt loses to that `Subject:` line.**
The model resolves conflicts by structure and specificity, not by recency. A
concrete `Subject: short dark brown hair` beats an abstract "preserve the
referenced identity" every time, and reference images do not save you.

**Replace the `Subject:` field in place. Never append a contradiction.**

The escape hatch is `subject_mode=placeholder`: the pack then emits a literal
`[SUBJECT SUPPLIED AT RENDER TIME]` slot instead of inventing, so attaching your
own subject becomes fill-in-the-blank instead of find-and-replace. The
`validate-pack` node enforces the sentinel's presence, `qa` treats it as
required rather than defective, and `render` blocks (rather than rendering the
token literally) when no `persona_pack` is supplied.

The same applies to `Scene/backdrop:` when it specifies an empty void. A
translucent subject in front of nothing is pixel-identical to a solid one, so a
"make it see-through" instruction cannot be satisfied or verified. Give the
scene structure, and require background elements to remain continuous through
the subject.

### 2. There is no fan-out

Archon node kinds are `command | prompt | bash | script | loop | approval |
cancel`. There is no `foreach` and no `matrix`. `loop` re-runs one prompt until a
signal and keeps only the last output. `intake` caps `count` at 8. To produce
more, run the workflow more than once and merge the packs yourself.

### 3. Runs lock per working directory

`worktree.enabled: false`, so a run executes in the working directory rather than
an isolated worktree. Archon locks per working directory, and concurrent starts
can also contend on the Archon database. Parallel runs therefore need separate
directories (each a git repository) and staggered starts.

### 4. Do not launch it from inside a Claude Code session

With `CLAUDECODE` set, the daemon hangs while `archon workflow status` still
reports "running". Launch from a plain shell, background it, and poll the log.
Verify by inspecting the run directory, not by trusting the status line.

### 5. A pack cannot tell you which brand it belongs to

Because private names are stripped by design, you cannot recover the brand from
`image-node-prompt-pack.json`. Read `image-node-brief.json` from the same run.

### 6. The aspect ratio is prose, not a constraint

The pack names an aspect in words. Renderers have been observed shipping 9:16
when the pack said 4:5. If the exact pixel geometry matters, state it explicitly
at render time.

## How To Run It

```bash
# once per machine: provision the pinned corpus (the only network step)
uv run .archon/scripts/style-corpus.py prime

# prompt pack only (default), from any repo
archon workflow run image-node-factory \
  "cinematic product hero, photography, count=4 aspect=4:5 render_mode=baked"

# with rendering
archon workflow run image-node-factory \
  "brand poster set, count=6 render=true render_mode=baked design_file=<relative path>"

# from a plain shell, backgrounded, then poll (see trap 4)
nohup archon workflow run image-node-factory "<brief>" > run.log 2>&1 &
```

Expect `dag_workflow_finished  anyFailed:false` in the log on success.

## Verification

Exercise the retrieval contract without rendering anything. All three paths are
cheap and offline:

```bash
# corpus counts (proves the pin, not a claim about it)
uv run .archon/scripts/style-corpus.py stats
#   -> {"pin": "a04beebf...", "cases": 511, "templates": 22, ...}

# re-hash the cached bytes; flip one byte and this exits 1
uv run .archon/scripts/style-corpus.py verify
#   -> {"ok": true, "pin": "a04beebf...", "cases": 511}   exit 0

# grounded retrieval
uv run .archon/scripts/style-corpus.py select --template-id realistic-photography --k 5
#   -> grounded: true, resolved_case_ids non-empty, prompt_engine present

# honest zero-match: grounded false, NO prompt_engine key, exit 0
uv run .archon/scripts/style-corpus.py select --category "Documents & Publishing" --lang en --k 5

# cold cache is a hard failure, not a silent self-author
ARCHON_PORT_CACHE_DIR=/tmp/empty uv run .archon/scripts/style-corpus.py select --template-id realistic-photography
#   -> "corpus not provisioned ... run: prime"   exit 1

uv run pytest .claude/scripts/tests/test_style_corpus.py -q
uv run pytest .claude/scripts/tests/test_pack_validate.py -q
archon validate workflows

# deterministic pack gate against any run's artifacts (exit 1 on violation)
uv run .archon/scripts/pack-validate.py validate --artifacts-dir <run artifacts dir>
```

The property that matters is `test_citation_resolves_or_is_absent`: a stamped
`example_case_ids` entry must resolve to a real case body, or the stamp must be
absent entirely.

## Related

- [skill-to-workflow-port](skill-to-workflow-port.md) -- why `ground` exists, and
  how to port any pointer-shaped skill reference into a workflow.
- [archon-workflows](archon-workflows.md) -- the workflow catalog, CLI, and the
  Archon vs Convoy/Mailbox boundary.
- [archon-repo-dispatch](archon-repo-dispatch.md) -- choosing the repo before a run.
