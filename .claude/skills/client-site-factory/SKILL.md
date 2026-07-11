---
name: client-site-factory
description: >-
  Point-and-shoot client website factory: one clients/<slug>/client.yaml ->
  validated brand-locked static site + AI imagery + cinematic video hero ->
  VALIDATION-REPORT.md -> operator-approved, grain-locked deploy. Works for ANY
  vertical (proven on an invented one). Use when the user says "site factory",
  "client site factory", "build a client site", "onboard a client", "make a
  website for <business/vertical>" (dental, HVAC, pool, roofing, whatever),
  "run the factory on <client>", "regenerate <client>'s copy/images/video", or
  "deploy the client site". NOT for YourBusiness fleet brand sites
  (de-ai-slop-frontend) or programmatic city pages (token-max-factory).
---

# Client-Site Factory

One config file per client in -> a finished website out, every step gated by a
deterministic validator. The shape: **AI decides, script resolves, AI
consumes.** LLMs write copy and scene prompts; scripts compile, assemble,
render, and gate. The factory NEVER deploys — deploying is a separate,
approval-locked workflow.

**Canonical manual (read before changing ANY factory code):**
`docs/manual/features/client-site-factory.md` — schema, lanes, gates, the
deploy contract, and 11 known traps. This skill is the router; the manual is
the depth.

## The two workflows

| Workflow | What it does | Invocation |
|---|---|---|
| `.archon/workflows/client-site-factory.yaml` | brief -> copy + images + video -> build + report. Never deploys. | `archon workflow run client-site-factory "clients/<slug>/client.yaml images=true video=true"` |
| `.archon/workflows/client-site-deploy.yaml` | operator-gated deploy of an existing build | `archon workflow run client-site-deploy "clients/<slug>/client.yaml dry_run=true"` |

**TRAP (manual Trap 10): NEVER launch either workflow from inside a Claude
Code session — archon hangs.** Run them from a desktop terminal, or drive the
lanes in-session with the script sequence below (that is exactly how P2/P3
acceptance ran).

## Onboard a new client (the proven recipe)

1. `mkdir clients/<slug>/assets-src/fonts clients/<slug>/stills` and copy the
   three open-license woff2 fonts into `assets-src/fonts/` (or the client's
   own pairing).
2. Write `clients/<slug>/client.yaml` — copy the shape from an existing
   profile. Sections: `identity`, `brand` (palette + typography + voice +
   banned_phrases + opening_move widget), `facts` (the packet: services,
   contact, whitelist — the writer's ONLY truth), `page_plan` (pages, nav,
   metas, hero assets), `images` (`assets_dir` + `page_map` role-keyed render
   concepts), optional `video` (per-page still + look), `compliance`
   (fine_print), `copy_gates`, `deploy` (held: true, base_path,
   canonical_base).
3. Compile early, fail early: the compiler refuses any page whose media can
   never materialize.

## In-session lane sequence (all from `.claude/scripts`, `uv run python ...`)

```bash
# prepare
.archon/scripts/profile-compile.py clients/<slug>/client.yaml
.archon/scripts/copy-brief.py     clients/<slug>/client.yaml
# copy lane: ONE fresh-context writer per page (brief file = its ONLY input),
# then per page:
.archon/scripts/copy-validate.py  clients/<slug>/client.yaml --page <id> --update-ledger
.archon/scripts/copy-validate.py  clients/<slug>/client.yaml --all      # enforce belt
# image lane
.archon/scripts/image-brief.py    clients/<slug>/client.yaml
# (LLM authors clients/<slug>/image-pack.json from image-pack-brief.json ONLY)
.archon/scripts/image-gate.py     clients/<slug>/image-jobs.json --pack clients/<slug>/image-pack.json --person-names "<from validate.json>"
.archon/scripts/image-render.py   clients/<slug>/image-jobs.json --pack clients/<slug>/image-pack.json --design clients/<slug>/compiled/design.json
.archon/scripts/image-gate.py     clients/<slug>/image-jobs.json --rendered
# video lane (local ffmpeg, free)
.archon/scripts/video-brief.py    clients/<slug>/client.yaml
bash .claude/skills/cinematic-video-hero/scripts/kenburns-hero.sh <still> clients/<slug>/assets-generated <basename> --grade
.archon/scripts/video-gate.py     clients/<slug>/video-jobs.json
# assemble + gate
.archon/scripts/site-assembler.py clients/<slug>/client.yaml --copy-dir copy-generated
.archon/scripts/site-validate.py  clients/<slug>/build --config clients/<slug>/compiled/validate.json
```

Writer/pack-author subagents get ONE brief file and write ONE artifact —
never the web, never other pages, never the client brief prose.

## Deploy (default-deny, grain-locked)

- `deploy-verify.py --pre --mode live` is the deterministic seam: it refuses
  without a fresh approval artifact that grain-binds **mode + slug + build
  fingerprint + run id**. A dry-run approval can NEVER authorize live; a
  changed build voids the approval; `--consume-approval` closes replay.
- Target comes from `CLIENT_SITE_DEPLOY_TARGET` (or `target=`) and must
  resolve physically OUTSIDE this repo and its worktrees (fail-closed; the
  in-repo reference copy is refused by name of this check).
- `dry_run` defaults true; the `vercel` CLI is reachable only under
  `dry_run=='false'`. Audit trail: `clients/<slug>/deploy-audit.jsonl`.
- A real approved live deploy has never been run — treat the first as a
  canary and get explicit operator sign-off.

## Standing guardrails

- `clients/` is GITIGNORED and must stay untracked until `scripts/sanitize.py`
  gains its DENY_DIRS entry (locked by `test_clients_dir_untracked_and_ignored`).
  Never track client data before that lands.
- `proof-package/` reference sites are READ-ONLY calibration inputs.
- Zero client literals outside `clients/` (born-clean; site-validate check 12).
- Template pack is versioned (`site-templates/v1/`) — breaking layout changes
  are a `v2/`, never edits that mutate shipped clients.
- Tests: `cd .claude/scripts && uv run pytest tests/test_profile_compile.py
  tests/test_site_validate.py tests/test_copy_validate.py
  tests/test_image_lane.py tests/test_deploy_gate.py -q` (95 green baseline).
