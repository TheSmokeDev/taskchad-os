---
description: Produce the final local report for the Image Node Factory run.
argument-hint: "(reads all image workflow artifacts)"
---

# Image Node Final Report

**Workflow ID**: $WORKFLOW_ID

## Contract

Create the final local report for the workflow run. Do not render images in this
node. Do not deploy, publish, submit to a marketplace, call OpenAI APIs, or use
private paths.

Read:
- `$ARTIFACTS_DIR/image-node-preflight.json`
- `$ARTIFACTS_DIR/image-node-brief.json`
- `$ARTIFACTS_DIR/image-node-selection.json`
- `$ARTIFACTS_DIR/image-node-prompt-pack.md`
- `$ARTIFACTS_DIR/image-node-prompt-pack.json`
- `$ARTIFACTS_DIR/image-node-imagegen-packet.json`
- `$ARTIFACTS_DIR/images/manifest.json`
- `$ARTIFACTS_DIR/qa-report.md`
- Upstream QA JSON: `$qa.output`
- Upstream render JSON if present: `$render.output`

Write:
- `$ARTIFACTS_DIR/image-node-final-report.md`

## Report Shape

Use markdown with these sections:

```markdown
# Image Node Factory Report

## Verdict

## What Was Produced

## Selected Template And Discipline Card

## Style Library Attribution

State whether the run was GROUNDED. If `$ground.output.grounded` is `true`, name
the corpus pin, the source repository, the license, and the resolved case ids that
were actually read. If it is `false`, say plainly that no library case matched and
that the prompts are self-authored. Never attribute a library the run did not read.
Never quote case text, and never reference any `*.local.json` path.

## Render Mode And Status

## Artifact Paths

## QA Notes

## Local Test Commands

## Marketplace Readiness Notes
```

Rules:
- Make clear that this was a local run only.
- Name the selected `template_id`, the library `category`, the bound
  `discipline_card`, and the `example_case_ids` the selection used. Credit the
  awesome-gpt-image-2 style library as the prompt engine.
- Confirm the pack carries both a baked and an overlay variant, and state which
  `render_mode` was selected.
- If render was skipped, say `render=false` dry-run and point to the prompt pack,
  packet, and manifest.
- If render was blocked, say the pack was produced and the host did not expose
  Codex imagegen.
- If render succeeded, list the saved image paths and the rendered variant.
- Include a rerun command for prompt-pack-only mode and one for render mode.
- Do not say the workflow is submitted, published, deployed, or live.

## Final Output

After writing the report, output a concise markdown summary with the report path
and main artifact paths.
