# Client-Site Factory (Archon Workflow)

Status: P1-P3 shipped and live-proven 2026-07-10 -- Rebecca/Experior canary
(bidirectional structural diff identical, validator 13/13, born-clean grep
zero) and a fictional-client E2E (Crestline Pool & Spa Care: 8/8 copy pages
first-try, 9/9 images rendered+gated, one image-to-video hero composed, 13/13
site validation, zero manual edits). P4 (the deploy gate + this chapter) ships
per the frozen contract in `PRPs/active/PRP-client-site-factory-phase-4.md`.
Owner: `.archon/workflows/{client-site-factory,client-site-deploy}.yaml`,
`.archon/commands/site-{factory,deploy}-*.md`, `.archon/scripts/*.py`,
`site-templates/v1/`, `clients/` (private, gitignored)
Last updated: 2026-07-10
**Public Export Status: Private only**

## What It Does

Turns one client brief into a validated, brand-locked, multi-page static site
with imagery and optional cinematic video heroes -- and nothing else. The
factory ends at `clients/<slug>/build/` + `VALIDATION-REPORT.md`. It never
runs `vercel`, never touches a live URL, never flips `deploy.held`. Deploying
is a separate, operator-invoked, approval-gated workflow (P4) with its own
audit trail.

The shape is the same one that runs every other AI production line in this
repo: **AI decides, script resolves, AI consumes.** LLMs write per-page copy
and pick imagery concepts. Deterministic scripts compile the profile, resolve
which assets actually need rendering, assemble the static HTML, and gate
every artifact against physical state. An instruction is a suggestion; the
gates are the enforcement. No node in either workflow trusts another node's
claim -- every lane re-validates from disk.

Composition is CLI-level, not workflow-level: Archon has no sub-workflow node,
so the factory calls token-max-site-factory's copy-gate validators and
image-node-factory's grounding/gate discipline as libraries, not as nested
workflows.

## Operator Entry Points

- **Build a site**: `archon workflow run client-site-factory "<brief>"` --
  brief references `clients/<slug>/client.yaml` (bare slug resolves).
- **Deploy a site**: `archon workflow run client-site-deploy "<slug>
  [dry_run=... target=...]"` -- always from a REGULAR shell, always
  operator-invoked. The factory workflow never references this one.
- No chat command, no dashboard route, no HTTP API for either. Invoke-only.

## Source Of Truth Files

| Layer | Files |
|---|---|
| Factory workflow DAG | `.archon/workflows/client-site-factory.yaml` |
| Deploy workflow DAG (P4) | `.archon/workflows/client-site-deploy.yaml` |
| Factory node prompts | `.archon/commands/site-factory-{intake,image-pack,report}.md` |
| Deploy node prompt (P4) | `.archon/commands/site-deploy-intake.md` |
| Profile compiler | `.archon/scripts/profile-compile.py` |
| Copy lane | `.archon/scripts/copy-brief.py`, `.archon/scripts/copy-validate.py` |
| Image lane | `.archon/scripts/image-brief.py`, `.archon/scripts/image-gate.py`, `.archon/scripts/image-render.py` |
| Video lane | `.archon/scripts/video-brief.py`, `.archon/scripts/video-gate.py` |
| Assembler + validator | `.archon/scripts/site-assembler.py`, `.archon/scripts/site-validate.py`, `.archon/scripts/site-structural-diff.py` |
| Deploy gate (P4) | `.archon/scripts/deploy-verify.py`, `.archon/scripts/deploy-audit.py` |
| Template pack | `site-templates/v1/pack.yaml`, `templates/*.html`, `partials/*.html`, `assets/{site.css,site.js}` |
| Client profiles | `clients/<slug>/client.yaml` -- private, gitignored |
| Tests | `.claude/scripts/tests/test_{profile_compile,site_validate,copy_validate,image_lane,deploy_gate}.py` |
| PRPs | `PRPs/active/PRP-client-site-factory.md` (P1-P3), `PRPs/active/PRP-client-site-factory-phase-4.md` (P4) |

## Client Profile Schema -- `clients/<slug>/client.yaml`

One YAML file is the entire input. `profile-compile.py` validates it against
`REQUIRED_SECTIONS` and derives every per-lane view; no lane reads
`client.yaml` directly except the compiler.

| Section | Contents | Consumed by |
|---|---|---|
| `identity` | slug, display name, org name/short, vertical, service area | all lanes |
| `brand` | palette hexes (12 required keys), typography (display/body/mono), voice_tone, allowed/banned phrases, `opening_move` widget spec | skeleton injection, copy lane, validators |
| `facts` | the packet: advisor/org names, contact, services, `number_whitelist`, external links, parent org. THE ONLY fact source the writer may cite | copy lane, validator check 9 |
| `page_plan` | nav, footer_links, `extra_pages_allow` globs, and the ordered page list -- each page's id, path, template, meta (title/description/og), and `hero` (poster + video_webm/video_mp4 filenames) | copy lane, assembler, validator checks 1-2 |
| `images` | `persona_pack` (ref or `none`), `assets_dir` (read-only source the assembler copies from), optional `page_map`: `page_id -> role (hero\|feature\|og) -> {concept, aspect}` | image lane, validator check 11 |
| `video` | optional. `pages: page_id -> {still, look (kenburns\|cinemagraph), grade, dur, ...}`. Output filenames are NEVER named here -- they derive from that page's `page_plan` hero entry | video lane |
| `compliance` | vertical profile ref (e.g. `regulated-financial`), fine-print text (hashed, not fuzzy-matched), `must_not` list | copy lane, validator checks 6+9 |
| `copy_gates` | `min_words` per template kind, `max_overlap` (Jaccard shingle ceiling across pages) | copy lane, validator check 13 |
| `deploy` | `held` (default true), `project`, `base_path`, `canonical_base`, `meta_robots_noindex`, `structured_data_pages`, `structured_data` | deploy workflow + `deploy-verify.py --pre` (P4) -- `project` is a CHECKED grain: `--pre` refuses when the target's `.vercel/project.json` is missing or names a different project (Rule 4) |

The `images.page_map` and `video.pages` sections share one convention: a
video-hero page's still is jobbed by the IMAGE lane into
`clients/<slug>/stills/<name>.png` (an input directory the assembler never
touches) when that page also carries an `images.page_map.<page>.hero.concept`.
A client that already owns a real photo skips the `page_map` entry for that
page and points `video.pages.<page>.still` straight at the existing file --
Rebecca's `service-trusts` page does exactly this, reusing a shipped
`.webp` poster with zero image-lane involvement.

### Compile Views

`profile-compile.py` derives three JSON views under `clients/<slug>/compiled/`:

| View | Purpose |
|---|---|
| `packet.json` | the copy writer's ONLY fact source (token-max packet pattern) |
| `design.json` | the image/CSS lane view: palette, fonts, opening-move mood |
| `validate.json` | the site-validate.py gate config: banned phrases, number whitelist, `fine_print_sha256`, page plan, thresholds |

Rule 1 (this repo's global rule): every tunable is resolved at call time from
the profile, never bound as a module default. Rule 2: every lane re-reads
`compiled/validate.json` or the profile itself rather than trusting a prior
lane's report -- `deploy-verify.py --pre` re-runs `site-validate.py` against
the physical build for exactly this reason (see The Deploy Gate below).

## The Factory DAG (P1-P3)

Workflow-level settings: `provider: claude`, `worktree.enabled: false` (the
run mutates `clients/<slug>/` state -- ledger, artifacts -- in place).

| Node | Kind | Depends on | Gate / notes |
|---|---|---|---|
| `intake` | command `site-factory-intake` | (none) | Resolves a bare slug to `clients/<slug>/client.yaml`; emits FLAT `"true"`/`"false"` strings for `images`, `video`. |
| `prepare` | `bash` | `intake` | Runs `profile-compile.py` + `copy-brief.py`; bakes `until-copy-settled.sh`, the loop's real finish line, at prepare time. |
| `write-copy` | `loop` | `prepare` | `fresh_context: true`, ONE page per iteration, `until_bash` gate, `max_iterations: 32`. See Copy Lane below. |
| `enforce-copy` | `bash` | `write-copy` | `copy-validate.py --all` -- the belt over the whole lane; the loop's self-reports count for nothing. |
| `image-brief` | `bash` | `prepare` | `when: images == 'true'`. Derives jobs from `images.page_map`. |
| `image-pack` | command `site-factory-image-pack` | `image-brief` | LLM authors one scene prompt per job, echoing ids/aspect verbatim. |
| `image-render` | `bash` | `image-pack` | `image-gate.py --pack` then `image-render.py` (drives the local codex render, persona refs never leave the box). |
| `image-gate` | `bash` | `image-render` | `image-gate.py --rendered` -- PNG magic bytes, size floor, aspect-sane dimensions. |
| `video-brief` | `bash` | `prepare, image-gate` | `trigger_rule: none_failed_min_one_success`, `when: video == 'true'`. Fires even when the image lane is skipped. |
| `render-video` | `bash` | `video-brief` | Per-job skip-if-VALID via `video-gate.py --job`, else runs the `cinematic-video-hero` skill script. |
| `video-gate` | `bash` | `render-video` | `video-gate.py` over every job -- webm/mp4 codec + duration + poster floor. |
| `assemble` | `bash` | `enforce-copy, image-render, image-gate, render-video, video-gate` | `trigger_rule: none_failed_min_one_success` -- LOAD-BEARING (see Trap 1). Runs `site-assembler.py --copy-dir copy-generated`. |
| `validate-site` | `bash` | `assemble` | `site-validate.py <build> --config compiled/validate.json` -- the 13-check gate. |
| `report` | command `site-factory-report` | `prepare, validate-site` | `trigger_rule: one_success` -- a failed run still gets `VALIDATION-REPORT.md`. Never deploys, never commits, never flips `deploy.held`. |

## The Lanes

### Copy Lane

Token-max's proven loop shape, applied per client instead of per city.
`write-copy` is a **ralph-pattern loop**: fresh context every iteration, state
lives entirely on disk (`copy-ledger.json`), and the agent does exactly ONE
page per iteration:

1. Read the ledger. A page is eligible when `status != "passed"` and
   `attempts < max_attempts`. Pick the first eligible page in ledger order.
2. Read `copy-briefs/<page_id>.json` -- the ONLY fact source. Every name,
   number, phone, and org must trace to it; internal links only from its
   `link_targets`. Every prior `failures[]` entry is a binding constraint.
3. If `copy-generated/<page_id>.json` already exists, keep every slot NOT
   named in the failure verdicts byte-for-byte verbatim; rewrite only the
   flagged slots.
4. Write the artifact: one raw JSON object matching the slot schema in
   `copy-brief.py::SCHEMAS`, `fixed_slots` echoed verbatim.
5. Run `copy-validate.py <client.yaml> --page <page_id> --update-ledger`.
   Exit 0 = passed, end the iteration. Exit 1 = verdicts recorded in the
   ledger, end the iteration (no in-iteration retry). Exit 2 = infrastructure
   error -- attempts are NOT charged.

`copy-validate.py`'s exit contract is 3-way and load-bearing everywhere in
this lane: 0 pass, 1 verdict (slot-scoped: `{scope, slot, code, detail,
hint}`), 2 infra (ledger never touched). Checks include token-max's prohibited
patterns (em-dash, guarantee claims, template admissions), per-client banned
phrases, word-count floors from `copy_gates.min_words`, and cross-page overlap
(9-token shingle Jaccard, `copy_gates.max_overlap`, non-fixed slots only --
fixed nav/footer slots are excluded or they'd set an unbeatable overlap floor).
`enforce-copy` re-runs `--all` afterward: the loop's self-reports count for
nothing.

### Image Lane

1. `image-brief.py` resolves `images.page_map` against the page plan and
   computes which assets actually need rendering -- an asset that already
   exists in `assets_dir` or the `assets-generated` overlay is never re-jobbed
   (skip-if-VALID, see Trap 4), and an og/feature slot naming the SAME file as
   that page's hero poster is treated as already covered without its own
   `page_map` entry (see Trap 11).
2. `image-pack` (LLM, fresh context) reads only `image-pack-brief.json` (jobs
   + design mood + rules -- never the client brief prose, never other lanes)
   and writes one scene prompt per job.
3. `image-gate.py --pack` validates the pack before rendering: every job
   covered exactly once, ids/aspect echoed verbatim, prompt discipline
   (15-800 chars, no em-dash, no local paths, no packet person names --
   persona injection is a render-time concern, never a pack concern).
4. `image-render.py` drives `video_imagegen.generate_image` locally (the one
   place this pipeline touches the codex CLI); persona reference photos never
   leave the machine. Renders are serial by design.
5. `image-gate.py --rendered` is the final gate AND the render driver's
   per-job skip check: PNG magic bytes, a size floor, and Pillow-checked
   aspect-sane dimensions.

### Video Lane

Deterministic local `ffmpeg` over a still -- no AI video model, no API cost.
`video-brief.py` reads the optional `video.pages` section and, per page,
resolves the still (`repo_root / still`, must exist at prepare time or the
node fails loudly rather than after a render), the look (`kenburns` ping-pong
or `cinemagraph` loop), and the EXPECTED output shape (duration, fps,
filenames) -- all derived from `page_plan.pages[id].hero.video_webm`, never
named in the `video:` section itself. `render-video` calls
`video-gate.py --job <id>` before each render to decide skip-vs-regenerate
(skip-if-VALID, never skip-if-exists) and otherwise runs the matching
`cinematic-video-hero` skill script. `video-gate.py` is the final gate: both
containers exist with the right codec (VP9/H264), duration within +/-25% of
expected, and the poster clears a size floor.

### The Assembler Overlay Asset Flow

`site-assembler.py` is pure and deterministic: skeleton pack + compiled
profile + copy artifacts + image/video assets -> `clients/<slug>/build/`.
It injects CSS vars into `:root`, fills `{{COPY:...}}` and `{{ASSET:...}}`
slots, stamps the compliance fine-print into every page footer, and writes
`vercel.json` (cleanUrls + `X-Robots-Tag: noindex`) and JSON-LD blocks from
packet facts only. No LLM runs here. An unresolved slot is a build FAILURE,
not a silent blank.

Asset resolution order: renders and video outputs land in
`clients/<slug>/assets-generated/` (the overlay); the profile's `assets_dir`
(reference imagery, e.g. the Rebecca proof-package assets) is a READ-ONLY
source the assembler copies FROM and never writes TO. The overlay is checked
first when an image job decides whether an asset already exists.

## The Gates

### The Site Validator -- `site-validate.py` (13 checks)

Deterministic, offline, exit 1 on any hard failure. One numbered check has
exactly one test in `test_site_validate.py` -- neutering a check turns its
test red. Calibrated against the SHIPPED Rebecca reference: a gate that fails
the known-good site is miscalibrated, not strict.

| # | Check |
|---|---|
| 1 | Page-set completeness -- build contains exactly `page_plan`'s pages; reference-side extras must match `extra_pages_allow` globs. |
| 2 | Every internal href/src resolves to a file in the build dir. |
| 3 | No absolute local paths (`[A-Za-z]:[\\/]`, `file://`, `/home/`, `/Users/`). |
| 4 | Brand tokens present in `:root`; zero unresolved `{{...}}` slots anywhere. |
| 5 | Banned phrases + prohibited regexes absent (em-dash, per-client list, kill-list seed). |
| 6 | Compliance fine-print present on every page -- SHA-256 hash match against the profile, not a fuzzy grep. |
| 7 | Forms are mailto-only: no `action=` endpoints, no third-party scripts. |
| 8 | Noindex enforced: `vercel.json` header (may live up to two parent dirs above the site root) AND meta robots where the profile requires it. |
| 9 | Packet-only fact compliance: extracted numbers and license-shaped strings must appear in the packet's `number_whitelist`. Hard-fail on regulated classes; warn-tier everything fuzzier. |
| 10 | JSON-LD parses and carries packet facts only. |
| 11 | Every `images.page_map` asset exists with sane dimensions. |
| 12 | Born-clean scan: no client literals outside `clients/`. |
| 13 | Token-max word-count floors (`copy_gates.min_words`) + cross-page uniqueness (`copy_gates.max_overlap`) on long-form pages. |

`deploy-verify.py --pre` (P4) re-runs check-carrying `site-validate.py`
against the PHYSICAL build before any deploy -- it never trusts
`VALIDATION-REPORT.md` or a green marker file (Rule 2).

### The Structural Differ -- `site-structural-diff.py`

The P1 canary gate: bidirectional (missing-from-build AND unexpected-extras
both fail) comparison of `(tag, classes)` sequence inside `<body>`, nav
hrefs, and internal ref sets, between a factory build and a reference site.

`--max-depth` bounds the skeleton comparison only (nav hrefs and internal
refs are always collected at every depth):

| `--max-depth` | Meaning | When |
|---|---|---|
| unset (full depth) | Every DOM level compared | Byte-faithful canaries -- copy is reused verbatim from the reference (P1) |
| `3` | Only template-owned section skeleton compared; fragment interiors (info-card bodies, prose clusters) are left to the copy lane's own gates | Regenerated copy that is semantically but not byte-identical (P2+) |

## The Deploy Gate (P4)

The factory never deploys. `client-site-deploy.yaml` is a SEPARATE,
operator-invoked workflow implementing this repo's Default-Deny Mutation
Policy (`.claude/sections/01_architecture.md`): default-deny, explicit named
gate, audit row, at every layer.

```
archon workflow run client-site-deploy "<slug>"                 # dry run (default)
archon workflow run client-site-deploy "<slug> dry_run=false"   # real deploy
```

Flow: `intake` -> `preflight` (writes the `gate_pending` audit row BEFORE the
gate -- a cancelled run still leaves it) -> `deploy-gate` (approval node,
states the blast radius, NO `on_reject`) -> `record-approval` (writes
`deploy-approval.json` binding mode + slug + run id + build fingerprint) ->
`pre-verify` (the deterministic re-check) -> `[real deploy only]` `copy-build`
-> `vercel-deploy` -> `post-verify` -> `audit-close`.

| Node | Kind | Depends on | Gate / notes |
|---|---|---|---|
| `intake` | command `site-deploy-intake` | (none) | Flat strings; `dry_run` defaults `"true"`. |
| `preflight` | `bash` | `intake` | `vercel --version` (fatal only when real); re-checks the profile schema; resolves the deploy target through `--print-target` BEFORE the gate (a refusal stops the run here -- `gate_pending` means the gate was reached) and puts `target=<resolved>` in the DEPLOY PREVIEW the gate shows; writes `gate_pending`. |
| `deploy-gate` | `approval` | `preflight` | `capture_response: true`, NO `on_reject`. Message states slug, `deploy.held`, canonical URL, dry_run, the RESOLVED target, and the blast-radius line. |
| `record-approval` | `bash` | `deploy-gate` | Computes `deploy-verify.py --fingerprint`, writes `deploy-approval.json` via `deploy-audit.py --write-approval`. |
| `pre-verify` | `bash` | `record-approval` | `deploy-verify.py --pre --mode {live,dry_run} --run-id ...` -- exit 1 stops the run. |
| `copy-build` | `bash`, `when: dry_run == 'false'` | `pre-verify` | The ONLY `rm -rf`, bounded (see Trap 6). |
| `vercel-deploy` | `bash`, `when: dry_run == 'false'` | `copy-build` | Porcelain-scope guard (rename-aware; a non-git target is a hard REFUSE, never a silent skip), then `vercel deploy --prod --yes`. |
| `post-verify` | `bash`, `when: dry_run == 'false'` | `vercel-deploy` | `deploy-verify.py --post <url>` -- HTTP 200 + `X-Robots-Tag: noindex`. |
| `audit-close` | `bash`, `trigger_rule: one_success` | `record-approval, pre-verify, post-verify` | Always writes a terminal `close` row; the verdict is inferred from THIS run's audit rows only (`run_id`-scoped; corrupt lines skipped; zero matching rows = fail); consumes the approval artifact on live runs. |

### The Contract (script flags, exit codes, artifacts)

```
deploy-verify.py --pre --client <client.yaml> --mode {live,dry_run}
  [--run-id S] [--target DIR] [--approval-max-age-hours N]
    ("dry" is accepted as an alias for "dry_run")
    exit 0 pass / 1 verdict ("FAIL  ..." lines) / 2 infra
    live mode REQUIRES a live-mode, fingerprint-matching, non-stale approval
    AND a git-checkout target (a non-git target refuses on live runs)

deploy-verify.py --post <url> --client <client.yaml>
    exit 0 iff HTTP 200 AND X-Robots-Tag contains "noindex" (case-insensitive)

deploy-verify.py --print-target --client <client.yaml> [--target DIR]
    THE single target resolver: flag -> env -> refuse.
    REFUSES any target inside this repo or inside any git worktree of it
    (physical git identity: repo toplevel containment + shared git common
    dir) -- the in-repo decoy proof-package/YourProduct-client can never be a
    deploy target even though it carries the marker.
    Prints the canonical path (exit 0) or refuses printing NOTHING (exit 1).

deploy-verify.py --fingerprint --client <client.yaml>
    prints "sha256:<hex>" over the physical build/ tree (never a manifest)

deploy-audit.py --client-dir DIR --event {gate_pending,approval,pre_verify,
  copy,deploy,post_verify,close} --verdict V [--detail S] [--run-id S]
  [--mode {live,dry_run}] [--build-fingerprint S] [--write-approval]
  [--consume-approval]
    appends deploy-audit.jsonl; --write-approval only on --event approval
    (requires --mode + --build-fingerprint); --consume-approval only on
    --event close (renames the artifact to deploy-approval.consumed-<ts>.json)
```

env `CLIENT_SITE_DEPLOY_TARGET` -- the `YourProduct-client` Vercel project
checkout, OUTSIDE this repo. Inline control `target=` overrides it per run.
Never hardcoded; resolved once, inside `resolve_target()`, at call time.

### The Approval Grain

`deploy-approval.json` binds every field it stores to a check `--pre`
actually performs (Rule 4 -- no stored-but-unchecked authorization field):

```json
{
  "slug": "rebecca-dominguez-experior-financial",
  "approved_at_utc": "2026-07-10T21:14:03+00:00",
  "run_id": "<archon run/artifacts dir basename, or 'manual'>",
  "acknowledged_hold": true,
  "mode": "live" | "dry_run",
  "build_fingerprint": "sha256:<hex>",
  "response": "<captured approval text, best-effort, NEVER load-bearing>"
}
```

`--pre` checks, in order: the artifact exists and parses; slug matches;
`approved_at_utc` is fresh (default ceiling 24h, timezone-aware UTC);
`acknowledged_hold`; **mode** (a dry-run approval can never open a live
window); **build_fingerprint** (the approval binds to THIS build -- a
regenerated build invalidates it, by design); **run_id** when the caller
states one; the build exists and passes a fresh `site-validate.py` run; the
compiled `validate.json` byte-equals a fresh `build_validate(profile)` (the
WHOLE gate view, not just the fine-print hash -- banned phrases, thresholds,
and the page plan all count); the target resolves, is OUTSIDE this repo (and
not a git worktree of it), and carries the noindex marker; the target's
`.vercel/project.json` names exactly the profile's **deploy.project** (an
unlinked or mislinked checkout refuses BEFORE any mutation -- `--yes` would
otherwise auto-link by directory name); and the blast-radius bound (Trap 9):
a git-checkout target must carry no changes outside `<slug>/`, and on a LIVE
run a target without `.git` is itself a refusal (`cannot verify blast-radius
scope`) -- the guard never skips silently.

### Dry-Run Default + Negative-Path Guarantees

- `dry_run` defaults `"true"` at intake. A real deploy requires an explicit
  `dry_run=false` on invocation AND a `mode: "live"` approval.
- No approval artifact -> `--pre` refuses (exit 1), the falsifiable core of
  this phase.
- A malformed artifact (unparseable JSON, missing `approved_at_utc`) is a
  refusal line, never a traceback.
- A dry-run approval can never satisfy `--pre --mode live`.
- An approval never replays against a different build (fingerprint mismatch)
  or a different run (`--run-id` mismatch).
- A target inside this repo -- including `proof-package/YourProduct-client`, the
  marker's own donor and the only in-repo dir that satisfies the marker check
  -- refuses at the resolver, on dry AND live runs alike. So does any git
  worktree of this repo.
- A live run against a non-git target refuses (`cannot verify blast-radius
  scope`); a target not linked to `deploy.project` refuses before any
  mutation.
- `deploy-audit.jsonl` is append-only; `"rejected"` is operator-manual-only
  (see Trap 8) -- the workflow itself can never write it.
- `git ls-files clients/` prints nothing and `git check-ignore` is positive
  for any path under `clients/` -- the compensating control for the deferred
  sanitizer entry (see Trap 5).

## Operator Runbook: Onboarding A New Client

Recipe extracted from the Crestline Pool & Spa Care fictional-client run (the
P3 acceptance canary -- zero overlap with any real client, proves the factory
carries a run on config alone):

1. **Create the client directory and asset staging area.**
   ```
   mkdir -p clients/<slug>/assets-src/fonts
   ```
   `assets-src/` is YOUR client's own asset source (fonts, any pre-supplied
   photography) -- distinct from a reused reference like Rebecca's
   proof-package assets. Drop the three woff2 weights (display/body/mono) in
   `assets-src/fonts/` if the client doesn't reuse a shipped pairing.

2. **Write `clients/<slug>/client.yaml`** covering every `REQUIRED_SECTIONS`
   entry (see Client Profile Schema above). Point `images.assets_dir` at
   `clients/<slug>/assets-src`. Keep `deploy.held: true`.

3. **Fill `page_plan.pages[*].hero`** with poster/video_webm/video_mp4
   filenames for every page -- these are the single naming authority the
   video lane derives its outputs from.

4. **Fill `images.page_map`** with one `hero`/`feature`/`og` role per page
   that needs a rendered asset, each carrying a `concept` (one evocative
   scene description, no readable text, no named people) and optionally an
   `aspect`. Skip a page's `page_map` entry entirely when a real photo
   already exists at the resolved asset path.

5. **Optional: fill `video.pages`** for any page that wants a cinematic hero.
   Point `still` at `clients/<slug>/stills/<name>.png` and ALSO give that
   same page a `page_map.<page>.hero.concept` so the image lane renders the
   still before the video lane composes it. To reuse an existing photo
   instead, point `still` directly at that file's repo-relative path and
   omit the `page_map` hero entry for that page.

6. **Run the factory** (full build):
   ```
   archon workflow run client-site-factory \
     "clients/<slug>/client.yaml images=true video=true max_attempts=3"
   ```

7. **Read `clients/<slug>/VALIDATION-REPORT.md`.** It always ends with
   "HELD -- the factory never deploys." Fix any held-back page or failed
   gate and re-run; the loop resumes from the ledger, it does not restart.

8. **Deploy only when the operator is ready** -- a separate invocation (see
   The Deploy Gate above), never inline with the factory run.

## How To Run It

```bash
# factory: prompt-pack-equivalent -- copy only, no images, no video
archon workflow run client-site-factory "clients/<slug>/client.yaml"

# factory: full build
archon workflow run client-site-factory \
  "clients/<slug>/client.yaml images=true video=true max_attempts=3"

# from a plain shell, backgrounded, then poll (never from inside Claude Code -- Trap 10)
nohup archon workflow run client-site-factory "clients/<slug>/client.yaml images=true" > run.log 2>&1 &

# deploy: dry run (default -- proves the gate, mutates nothing)
archon workflow run client-site-deploy "<slug>"

# deploy: real deploy (requires the approval AND dry_run=false)
# The target must be the EXTERNAL YourProduct-client git checkout, linked
# (`vercel link`) to the deploy.project named in the profile -- a path inside
# this repo (or any worktree of it) is refused by the resolver.
CLIENT_SITE_DEPLOY_TARGET=/path/to/YourProduct-client-checkout \
  archon workflow run client-site-deploy "<slug> dry_run=false"
```

Expect `dag_workflow_finished  anyFailed:false` in the log on success.

## Known Traps

Read these before touching either workflow. Each one cost a broken build, a
silent no-op, or a would-be leaked deploy.

### 1. Skip-propagation: `none_failed_min_one_success` is load-bearing

`assemble` and `video-brief` use `trigger_rule: none_failed_min_one_success`,
not the Archon default `all_success`. With `images=false` and/or
`video=false` their upstream lane nodes are SKIPPED, not failed -- under the
default rule a skipped dependency silently no-ops every downstream node,
including `assemble`, and the whole factory produces nothing with no error.
`render-video` is kept as an explicit dependency of `assemble` too, so a
mid-lane render FAILURE (not a skip) still blocks assembly -- a skipped gate
downstream of a failure would otherwise read as "not failed."

### 2. `write-copy`'s loop is `fresh_context: true` by design

Archon's loop-node default is `fresh_context: false` -- iterations thread the
same session and remember what they tried before. `write-copy` deliberately
overrides that: every iteration starts with ZERO memory of prior iterations.
State lives entirely on disk (`copy-ledger.json`, the brief files, the
generated artifacts). The `until_bash` script -- not the agent's own claim of
being done -- is the real finish line: the loop's `until: COPY_LANE_COMPLETE`
is REQUIRED by the schema, but the prompt forbids the agent from ever
printing it, because a fresh-context agent claiming completion proves nothing
about whether work remains.

### 3. `until_bash` is baked at prepare time

The loop's finish-line script (`until-copy-settled.sh`) is written to
`$ARTIFACTS_DIR` by the `prepare` node BEFORE the loop starts, with the
client path already substituted in. There is no dynamic YAML interpolation
inside the loop reading fresh state each iteration -- if the finish-line
logic needs to change, it changes in `prepare`'s heredoc, not in the loop
node.

### 4. Overlay asset flow: renders never touch the source dir

Every image and video render lands in `clients/<slug>/assets-generated/` (an
overlay the assembler reads FROM). The profile's `images.assets_dir` (a
reused reference build's assets, or a client's own `assets-src/`) is
READ-ONLY -- no script in either lane ever writes to it. Idempotency is
skip-if-VALID (re-gate an existing output before trusting it), never
skip-if-exists: a cached defective render that merely exists on disk must
never ship via a bare existence check.

### 5. The sanitizer boundary is a gitignore, not a deny-list, for now

`clients/` is denied only via `.gitignore:143-146` -- it is NOT yet an entry
in `scripts/sanitize.py`'s `DENY_DIRS` (that file is owned by a parallel
session; the entry is WS3-deferred in the P4 PRP). The compensating control:
the sanitizer sources its file list from `git ls-files`
(`.gitignore:86-88`), and an untracked directory can never appear there, so a
client's data cannot ship publicly even though the deny-list entry is
pending. `.claude/scripts/tests/test_deploy_gate.py::
test_clients_dir_untracked_and_ignored` locks this and is DESIGNED to go red
the moment WS3 lands (its docstring names the successor test) -- upgrade the
control then, do not delete the red test.

### 6. `copy-build`'s `rm -rf` is bounded, not trusted

The deploy workflow's ONLY deletion (`copy-build`, real deploys only) is
scoped to exactly `"$TARGET/$SLUG"`, and every precondition runs before it:
the slug is shape-checked (`[a-z0-9-]+`), the target comes only from
`deploy-verify.py --print-target` (never re-derived in bash) -- which
refuses any path inside this repo or a git worktree of it, so the `rm -rf`
can never land on tracked repo content -- the noindex marker is re-checked
on the physical target directory at mutation time, and an existing
destination must carry `index.html` (proof it is a prior site build) before
it is removed. An empty or unresolved target refuses before reaching the
`rm -rf` line at all.

### 7. `.png`-only renderable images

`image-brief.py` refuses any job whose target filename does not end
`.png` -- the renderer only emits PNG. Pre-made, hand-supplied assets (like
Rebecca's reused `.webp` posters) may be any format because they are never
jobbed in the first place: a `page_map` entry only exists for assets that
need rendering.

### 8. Video poster naming authority lives in `page_plan`, never in `video:`

`video.pages.<page>.still` names an INPUT only. Every OUTPUT filename
(`<basename>.webm`, `<basename>.mp4`, `<basename>-poster.webp`) is derived
from that page's `page_plan.pages[*].hero.video_webm` entry. This is
deliberate: a rendered file and the HTML that references it can never drift,
because there is exactly one place that names them.

### 9. A deploy-gate reject cancels the run; approving one slug ships the whole surface

Two facts, engine-verified against the live Archon v0.5.0 binary:

- **Reject cancels.** `deploy-gate` carries no `on_reject`. Omitting it is
  schema-valid, and a reject at that gate CANCELS the workflow run outright
  -- no `record-approval`, no deploy, no further audit rows. The in-jsonl
  signature of a reject is a `gate_pending` row with no `approval` row after
  it; the definitive record is Archon's own run/event store. Never make the
  approval node's captured response TEXT load-bearing downstream -- gate
  completion plus the artifact are the entire contract.
- **Blast radius.** `vercel deploy --prod` has no subtree deploy for a static
  project rooted at `vercel.json` -- approving `<slug>` publishes the ENTIRE
  `YourProduct-client` checkout, every client slug currently in it, not just the
  one being deployed. The approval message states this in plain words.
  `pre-verify` FAILs when the target checkout carries uncommitted changes
  outside `<slug>/`, and `vercel-deploy` re-asserts the same scope
  post-copy, recording the porcelain listing (the pre-deploy diff) in its
  audit row. Three hardenings on this bound: (1) a live-mode target WITHOUT
  `.git` is an explicit refusal in both `pre-verify` and `vercel-deploy` --
  the guard never evaporates silently on a misaimed path (the failure mode
  that made the in-repo decoy dangerous); (2) the porcelain filter is
  rename-aware: a staged `git mv outside.html <slug>/x` line contains
  ` <slug>/` yet moves an OUTSIDE path, so a rename counts as in-scope only
  when BOTH halves live under `<slug>/` (quoted in-slug paths may
  over-refuse -- fail closed); (3) the target must be linked
  (`.vercel/project.json`) to exactly `deploy.project`, or the run refuses
  before the copy -- an unlinked dir named `YourProduct-client` would otherwise
  auto-link to the real production project under `--yes`.

### 10. Do not launch either workflow from inside a Claude Code session

With `CLAUDECODE` set, the Archon daemon hangs while `archon workflow status`
still reports "running" -- the same failure mode documented for every other
Archon workflow in this repo (`video-production.yaml:10-14`,
[image-node-factory](image-node-factory.md) trap 4). Launch
`client-site-factory` and `client-site-deploy` from a plain shell, background
the run, and poll the log or `archon workflow status` -- never from inside a
Claude Code CLI session. Verify by inspecting the run directory, not by
trusting the status line.

### 11. An og/feature slot inherits renderability when it reuses the hero's filename

`profile-compile.py`'s `validate_media_refs()` does not require its own
`images.page_map.<page>.og` or `.feature` entry when that page's
`meta.og_image` (or `feature_img`) is set to the EXACT SAME filename as
`page_plan.pages[*].hero.poster` -- `materializable()` recurses into the
hero's own check and inherits the answer. Point an og image at the hero
poster's filename and it is covered for free; give it a DIFFERENT filename
and it needs its own `page_map` entry (or a pre-supplied file already sitting
in `assets_dir`), or `profile-compile.py` fails loudly at prepare time --
before any render is attempted, not after.

## Verification

```bash
# workflow yaml is valid and discovered
archon validate workflows
archon workflow list | grep client-site-factory
archon workflow list | grep client-site-deploy

# factory regression suites
uv run --project .claude/scripts pytest \
  .claude/scripts/tests/test_profile_compile.py \
  .claude/scripts/tests/test_site_validate.py \
  .claude/scripts/tests/test_copy_validate.py \
  .claude/scripts/tests/test_image_lane.py -q

# deploy gate suite
uv run --project .claude/scripts pytest .claude/scripts/tests/test_deploy_gate.py -q

# THE negative proof, live mode: no approval artifact exists yet -> exit 1
uv run --project .claude/scripts python .archon/scripts/deploy-verify.py --pre \
  --mode live --client clients/<slug>/client.yaml
echo "exit=$? (expect 1, refusal line mentions approval)"

# compensating control (Trap 5)
git ls-files clients/                    # MUST print nothing
git check-ignore clients/anything && echo ignored-ok

# structural canary against a shipped reference (full depth = byte-faithful;
# --max-depth 3 = section skeleton, for regenerated copy)
uv run --project .claude/scripts python .archon/scripts/site-structural-diff.py \
  clients/<slug>/build proof-package/YourProduct-client/<reference> \
  --config clients/<slug>/compiled/validate.json --max-depth 3
```

Byte-safety proof for every P4 commit -- these five files must never move:

```bash
git diff --stat .archon/workflows/client-site-factory.yaml scripts/sanitize.py \
  scripts/sanitize_test.py docs/manual/README.md .gitignore
# MUST be empty for all five
```

## Related

- `PRPs/active/PRP-client-site-factory.md` -- the parent PRP: architecture,
  P1-P3 phase receipts, the full Reuse Map, and the Non-Goals list.
- `PRPs/active/PRP-client-site-factory-phase-4.md` -- the P4 PRP: the frozen
  deploy contract, the R1 adversarial disposition table, and the workstream
  split this chapter documents against.
- [image-node-factory](image-node-factory.md) -- the standalone brand-imagery
  engine this factory's image lane borrows grounding/gate discipline from
  (the two are deliberately NOT the same workflow -- see the parent PRP's
  Reuse Map deviation note).
- [archon-workflows](archon-workflows.md) -- the workflow catalog, CLI, and
  the Archon vs Convoy/Mailbox boundary.
