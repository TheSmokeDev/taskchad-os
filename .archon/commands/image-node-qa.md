---
description: Validate the Image Node Factory prompt-pack and render artifacts.
argument-hint: "(reads prompt pack, manifest, and optional render result)"
---

# Image Node QA

**Workflow ID**: $WORKFLOW_ID

## Contract

Validate the image workflow artifacts. This is an artifact QA pass, not a render
node. Do not call image generation, OpenAI APIs, CLI fallback scripts, or
external services.

Read:
- `$ARTIFACTS_DIR/image-node-preflight.json`
- `$ARTIFACTS_DIR/image-node-brief.json`
- `$ARTIFACTS_DIR/image-node-selection.json`
- `$ARTIFACTS_DIR/image-node-prompt-pack.md`
- `$ARTIFACTS_DIR/image-node-prompt-pack.json`
- `$ARTIFACTS_DIR/image-node-imagegen-packet.json`
- `$ARTIFACTS_DIR/images/manifest.json`
- Render output if present: `$render.output`

Write:
- `$ARTIFACTS_DIR/qa-report.md`

Then output ONLY the QA JSON described below.

## Checks

Selection checks:
- `template_id` is one of the library template ids.
- `category` and `discipline_card` are set and consistent with the mapping.
- `example_case_ids` is present and is an array of INTEGERS. The selection
  references library cases rather than pasting their prompt text.

Grounding checks (a citation must resolve, or must not be made):
- If the packet or prompt pack names `prompt_engine`, then `$ground.output.grounded`
  is `true` AND every cited `example_case_ids` entry appears in
  `$ground.output.resolved_case_ids`. A cited id that is unresolved is a FAIL.
- If `$ground.output.grounded` is `false`, the pack carries `self_authored: true`
  and carries NO `prompt_engine` and NO `example_case_ids`. Anything else is a FAIL.
- No `*.local.json` file is quoted, copied, or listed in the manifest or the
  publishable pack.

Prompt pack checks:
- Required artifacts exist and contain parseable JSON where expected.
- Concept count matches expected count.
- Every concept carries BOTH a `baked_prompt` and an `overlay_prompt`.
- Overlay concepts include a `copy` object and a text-free scene with a
  forbid-text clause and reserved empty space.
- Baked concepts quote `exact_text` verbatim when it was requested.
- Negative constraints include no watermark, no random logos, no garbled text,
  and no extra text beyond requested copy.
- No absolute local run paths, private system names, OpenAI API-key
  dependencies, or external render services appear in the public prompt pack.
- Public prompt-pack and packet JSON use relative artifact names. The QA report
  may include absolute local paths because it is a local run report.

Packet checks:
- `artifact_type` is `imagegen-workflow-packet`.
- `schema_version` is `1`.
- `render_disciplines` lists both `baked` and `overlay`.
- Packet includes the canonical node order: `classify-request`, `route-vertical`,
  `route-lighting-choice`, `collect-input-images`, `build-production-spec`,
  `qa-generated-image`, `execute-imagegen`.

Render checks:
- If manifest status is `prompt_pack_only`, this is a passing dry-run when the
  prompt pack and packet are valid.
- If manifest status is `blocked`, this is a passing local capability-gate test
  when `render-blocked.md` exists and no API fallback was attempted.
- If manifest status is `rendered`, image files must exist under
  `$ARTIFACTS_DIR/images/`, match the manifest count, and record which variant
  was rendered.
- Any exact-text, brand or logo, likeness, or public-figure risk requires manual
  review even if the dry-run passes.
- When `$intake.output.subject_mode` is `placeholder`: the literal token
  `[SUBJECT SUPPLIED AT RENDER TIME]` in every prompt's `Subject:` field is
  REQUIRED, not a defect. Flag as an issue only its ABSENCE, or any invented
  physical subject traits alongside it. (The deterministic validate-pack node
  has already hard-failed structural violations; this pass is semantic.)

## QA Report

The markdown report must include:
- verdict
- render_status and render_mode
- artifacts checked
- issues
- manual review notes
- next local test command

## QA JSON Shape

```json
{
  "pass": "true",
  "render_status": "prompt_pack_only",
  "render_mode": "overlay",
  "manual_review_required": "false",
  "issue_count": 0,
  "qa_report_path": "qa-report.md"
}
```

## Final Requirement

Write the QA report first. Then output only valid JSON matching the QA shape. No
markdown, no commentary.
