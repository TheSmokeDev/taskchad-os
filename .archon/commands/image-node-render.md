---
description: Render the selected variant with Codex built-in image generation or block cleanly.
argument-hint: "(reads image-node-prompt-pack.json; runs only when render=true)"
---

# Image Node Render

**Workflow ID**: $WORKFLOW_ID

## Contract

Render the prompt pack using Codex built-in image generation only. This node must
never use OpenAI API keys, ad hoc SDK scripts, external marketplace services, or
any fallback CLI or API renderer.

Read:
- `$ARTIFACTS_DIR/image-node-preflight.json`
- `$ARTIFACTS_DIR/image-node-prompt-pack.json`
- `$ARTIFACTS_DIR/images/manifest.json`
- Upstream intake JSON: `$intake.output`

Write:
- Final generated images under `$ARTIFACTS_DIR/images/`
- Updated `$ARTIFACTS_DIR/images/manifest.json`
- `$ARTIFACTS_DIR/render-blocked.md` only if rendering is blocked

Then output ONLY the render JSON described below.

## Variant Selection

- Read `render_mode` from `$intake.output.render_mode`.
- When `render_mode` is `baked`, render each concept's `baked_prompt`.
- When `render_mode` is `overlay`, render each concept's `overlay_prompt`. The
  overlay copy stays as HTML data in the pack. Do not bake it here. The rendered
  image is the text-free scene with reserved empty space.
- Record which variant was rendered in the manifest and in the render JSON.

## Render Rules

- When `$intake.output.subject_mode` is `placeholder` and
  `$intake.output.persona_pack` is `none`, do not render. Write
  `render-blocked.md` and output `render_status: "blocked"` with
  `blocked_reason` stating the pack carries a `[SUBJECT SUPPLIED AT RENDER
  TIME]` slot and expects a downstream renderer to supply the subject
  references. Rendering the placeholder literally would produce garbage.
- Use the built-in Codex image generation capability for each selected prompt.
- Make one image generation call per concept.
- Inspect each generated image for prompt fit, visible text accuracy in the baked
  case, empty overlay space in the overlay case, and major visual failures.
- Move or copy selected final images into `$ARTIFACTS_DIR/images/`. Do not leave
  the only copy in the default Codex generated-images location.
- Use stable filenames: `asset-01.png`, `asset-02.png`, and so on.
- Update the manifest with saved image paths, source concept ids, the rendered
  variant, and any review notes.

## Hard Capability Gate

If the built-in image generation tool is unavailable in this Archon or Codex run:

1. Do not call any fallback API, CLI, SDK, or external service.
2. Leave the prompt pack and packet intact.
3. Write `$ARTIFACTS_DIR/render-blocked.md` explaining that Codex imagegen was not
   exposed to this run.
4. Update `$ARTIFACTS_DIR/images/manifest.json` with:
   - `status`: `blocked`
   - `render_mode`: the intake render_mode
   - `render_engine`: `codex_builtin_imagegen`
   - `blocked_reason`: a short explanation
   - `api_key_dependency`: `none`
   - `images`: []
5. Output render_status `blocked`.

## Render JSON Shape

```json
{
  "render_status": "blocked",
  "render_mode": "overlay",
  "image_count": 0,
  "manifest_path": "images/manifest.json",
  "blocked_reason": "Codex built-in image generation was not available in this Archon run."
}
```

If rendering succeeds, use:

```json
{
  "render_status": "rendered",
  "render_mode": "overlay",
  "image_count": 1,
  "manifest_path": "images/manifest.json",
  "blocked_reason": ""
}
```

## Final Requirement

Write or update the artifacts first. Then output only valid JSON matching the
render shape. No markdown, no commentary.
