---
description: Build the dual-variant prompt pack, imagegen packet, and initial manifest using the selected library template.
argument-hint: "(reads intake and selection artifacts)"
---

# Image Node Prompt Pack

**Workflow ID**: $WORKFLOW_ID

## Contract

Build original, copy-safe prompts for Codex built-in image generation from the
selected library template. Do not render images in this node. Do not call OpenAI
APIs or CLI fallback scripts. Do not vendor or quote external prompt corpora. Use
the `gpt-image-2-style-library` skill as the structure and quality reference for
the chosen `template_id` and its example cases, then write your own prompts.

Read:
- `$ARTIFACTS_DIR/image-node-preflight.json`
- `$ARTIFACTS_DIR/image-node-brief.json`
- `$ARTIFACTS_DIR/image-node-selection.json`
- `$ARTIFACTS_DIR/image-node-grounding.local.json` (written by the `ground` node)
- Upstream preflight JSON: `$preflight.output`
- Upstream intake JSON: `$intake.output`
- Upstream selection JSON: `$select.output`
- Upstream grounding JSON: `$ground.output`
- The house discipline card at `.archon/image-nodes/$select.output.discipline_card.md`,
  falling back to `~/.archon/image-nodes/$select.output.discipline_card.md` when this
  workflow runs from a repository that does not carry its own cards.

Use:
- `image-node-grounding.local.json` is the ONLY authoritative source of example
  cases. It carries real, checksum-verified case prompts for the resolved
  `example_case_ids`, retrieved offline from a pinned corpus. Read the exemplars
  for structure and quality, then write fresh prompt wording. Do not paste case
  text into the pack.
- The installed skill's own `gallery.md` and case references are
  NON-AUTHORITATIVE. They are links to files that are not installed, and this node
  runs with web search disabled. Never resolve, follow, or quote them. If a case id
  is not in the grounding artifact, it does not exist for your purposes.
- The skill's template blocks and taxonomy remain a valid structure reference.

Grounding and provenance:
- When `$ground.output.grounded` is `true`, stamp `prompt_engine`, `corpus_pin`,
  `corpus_source`, `corpus_sha256`, `license`, and the RESOLVED
  `example_case_ids` (never the unresolved ones) into the prompt-pack JSON.
- When it is `false`, the retrieval matched nothing. Set `self_authored: true` and
  OMIT `prompt_engine` and `example_case_ids` entirely. Never cite a source you did
  not read.

Write:
- `$ARTIFACTS_DIR/image-node-prompt-pack.md`
- `$ARTIFACTS_DIR/image-node-prompt-pack.json`
- `$ARTIFACTS_DIR/image-node-imagegen-packet.json`
- `$ARTIFACTS_DIR/images/manifest.json`

Then output ONLY the summary JSON described below.

## Two Variants, Always Both

For each of the `count` concepts, emit TWO prompt variants so the operator can
compare disciplines:

1. `baked_prompt` - the library native approach. Text is rendered INSIDE the
   image. If `exact_text` is set, quote it verbatim in the prompt with strict
   placement and legibility guidance. This is the variant to use when the
   operator trusts the model to set the visible copy.

2. `overlay_prompt` plus `copy` - the house discipline from the bound
   `discipline_card`. The scene is TEXT-FREE. The prompt must forbid baked text
   with a clause like: no text, no words, no letters, no numbers, no logos, no
   watermarks, no lettering of any kind. It must reserve generous empty negative
   space where an HTML overlay will land. The correctly spelled message lives in
   a separate `copy` object: `{ eyebrow, headline, subhead, cta }`, rendered as
   HTML later, never baked.

`render_mode` from intake selects which variant the render node will generate. It
does NOT drop the other variant from the pack. Both variants are always written.

## Prompt Scaffold

Build each prompt from the blocks the selection node listed in
`prompt_structure`. A useful shared scaffold:

```text
Use case: <library category and asset destination>
Template: <selected template_id>
Primary request: <operator main request>
Input references: <design_file and persona_pack roles, or none>
Scene/backdrop: <environment>
Subject: <main subject and placement>
Style/medium: <photo, illustration, 3D, and so on>
Composition/framing: <wide, close, top-down; placement; reserved empty zones for overlay>
Lighting/mood: <lighting and mood>
Color palette: <palette notes, from design_file when supplied>
Text handling: <baked verbatim text, OR text-free with a forbid-text clause>
Constraints: <must keep and must avoid>
Avoid: <negative constraints from selection>
```

Rules:
- If `count` is greater than 1, make each concept a useful variant of the same
  brief, not unrelated ideas. Reuse the one template and vary subject,
  composition, palette, and scene.
- `design_file` and `persona_pack` are optional runtime references only. Describe
  their ROLE (brand palette source, subject likeness lock). Never inline a brand
  name, a persona name, or an absolute local path. When both are `none` AND
  `subject_mode` is `generic`, use a clean neutral world and identity-stable
  generic traits.
- When `$intake.output.subject_mode` is `placeholder`, do NOT invent a subject
  anywhere. The `Subject:` field of EVERY concept prompt (both `baked_prompt`
  and `overlay_prompt`) must begin with the literal token
  `[SUBJECT SUPPLIED AT RENDER TIME]` followed only by placement and posture
  notes plus this clause: `preserve the attached reference subject's identity,
  features, and proportions exactly; do not invent, describe, or restyle the
  subject`. Never write physical traits (hair, face, age, build, silhouette,
  colors of the subject's body) in any field. A downstream renderer replaces the
  token with its own reference-locked subject.
- Do not invent brand names, slogans, people, data, or claims.
- Do not use API-only execution fields such as quality, model, input_fidelity,
  masks, or output paths inside a prompt.
- For transparent-background requests, keep the built-in-first chroma-key
  contract: flat key background plus local removal later. Do not switch to CLI
  native transparency.

## Artifact Hygiene

This workflow is intended to be public and marketplace-portable.
- Refer to artifacts by basename or relative artifact name, such as
  `image-node-brief.json` or `images/manifest.json`.
- Do not write absolute local run paths or private system names into the public
  prompt pack, packet JSON, or manifest.
- `image-node-grounding.local.json` is a private, local-only reference. Any
  `*.local.json` file is excluded from the publishable pack: never quote its case
  text, never copy it into an artifact, never list it in the manifest, and never
  name it in a public-facing report. The exemplars are third-party MIT-licensed
  text you may learn structure from, not text you may redistribute.
- If `$ARTIFACTS_DIR/image-node-preflight.json` is missing, reconstruct it from
  `$preflight.output` before writing the pack.

## Prompt Pack JSON

`image-node-prompt-pack.json` should carry the concepts array. Each concept:

```json
{
  "concept_id": "concept-01",
  "template_id": "product-commerce-visual",
  "baked_prompt": "",
  "overlay_prompt": "",
  "copy": { "eyebrow": "", "headline": "", "subhead": "", "cta": "" },
  "aspect": "1:1",
  "panel_side": "top-left"
}
```

Set `copy` and `panel_side` only for the overlay discipline. Keep the baked
variant self-contained.

## Packet Compatibility

`image-node-imagegen-packet.json` must stay compatible with the imagegen packet
family where useful. Include:

The `prompt_engine` and `prompt_engine_attribution` keys below are conditional.
Copy them from `$ground.output` when `grounded` is `true`. When it is `false`,
DROP both keys and set `"self_authored": true` instead. A packet must never name
an engine whose cases it did not read.

```json
{
  "artifact_type": "imagegen-workflow-packet",
  "schema_version": 1,
  "name": "image-node-factory-v1",
  "prompt_engine": "gpt-image-2-style-library",
  "prompt_engine_attribution": "awesome-gpt-image-2 style library",
  "corpus_pin": "<from $ground.output.corpus_pin>",
  "license": "MIT",
  "render_disciplines": ["baked", "overlay"],
  "compatibility": {
    "packet_family": "homie-imagegen-prompt-v1",
    "node_order": [
      "classify-request",
      "route-vertical",
      "route-lighting-choice",
      "collect-input-images",
      "build-production-spec",
      "qa-generated-image",
      "execute-imagegen"
    ]
  },
  "goal": "Convert a visual brief into a dual-variant image prompt pack and optional generated assets using the style library.",
  "selected_template": "",
  "example_case_ids": [],
  "public_node_functions": [],
  "nodes": [],
  "manifest": {}
}
```

Fill `selected_template` and `example_case_ids` from the selection JSON. The
`nodes` array summarizes the packet decisions for those canonical node names. The
`manifest` points to the prompt pack and image manifest artifacts.

## Initial Manifest

Write `$ARTIFACTS_DIR/images/manifest.json` with:
- `status`: `render_pending` if render_requested is `true`, otherwise
  `prompt_pack_only`
- `render_mode`: the intake `render_mode` value
- `render_engine`: `codex_builtin_imagegen`
- `api_key_dependency`: `none`
- `image_count`: 0
- `expected_count`: count
- `images`: []
- `prompt_pack_json_path`: `image-node-prompt-pack.json`
- `packet_path`: `image-node-imagegen-packet.json`

## Summary Output Shape

```json
{
  "status": "ready",
  "render_requested": "false",
  "render_mode": "overlay",
  "prompt_count": 1,
  "baked_variant_present": "true",
  "overlay_variant_present": "true",
  "prompt_pack_md_path": "image-node-prompt-pack.md",
  "prompt_pack_json_path": "image-node-prompt-pack.json",
  "packet_path": "image-node-imagegen-packet.json",
  "manifest_path": "images/manifest.json"
}
```

## Final Requirement

Write all four artifacts first. Then output only valid JSON matching the summary
shape. No markdown, no commentary.
