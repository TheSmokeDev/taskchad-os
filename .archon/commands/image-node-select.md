---
description: Dynamically select the strongest style-library template and nearest example cases using the gpt-image-2-style-library skill.
argument-hint: "(reads image-node-brief.json)"
---

# Image Node Select

**Workflow ID**: $WORKFLOW_ID

## Contract

Choose the single strongest prompt template and the nearest worked example cases
for this brief by USING the installed `gpt-image-2-style-library` skill. This is
the data-driven selection step. The library holds hundreds of worked cases and a
set of structured templates across its categories. You select from that library
dynamically per brief. You do not hardcode a fixed template, and you do not copy
or vendor the library corpus into this repo. The library taxonomy is attributed
to the awesome-gpt-image-2 style library.

Read:
- `$ARTIFACTS_DIR/image-node-brief.json`
- Upstream intake JSON: `$intake.output`

Use:
- The `gpt-image-2-style-library` skill and its `references/style-library.md`
  index. Follow the skill selection order: template category first, then visual
  style tag, then scene tag, then nearest example cases. Read the reference
  before choosing, and prefer it over memory when template names, categories,
  style tags, or case ids matter.
- The skill's `gallery.md` and any case BODY it links to are NON-AUTHORITATIVE:
  those links point at files that are not installed, and this node runs with web
  search disabled. Never follow them and never reconstruct a case from memory.
  Case ids are resolved downstream by the `ground` node against a pinned corpus.
  Emit ids; do not imagine their contents.

Write:
- `$ARTIFACTS_DIR/image-node-selection.json`

Then output ONLY the same JSON object.

## How To Select

1. Detect the target output from the brief and the `category_hint`: UI, poster,
   infographic, product, brand, photo, illustration, character, scene, history,
   or document.
2. Ask the skill to match this brief. Take the strongest template. If the brief
   is genuinely split across two categories, pick the one whose worked cases fit
   the operator intent best and record the runner-up in `selection_reason`.
3. Capture the chosen `template_id` and the nearest `example_case_ids` from the
   skill. Emit them as INTEGERS, exactly as the library records them (for example
   `[17, 2, 4]`, never `["case 17"]`). These case ids are the concrete anchors the
   `ground` node resolves and the prompt-pack node builds from; ids the corpus does
   not carry are reported back as unresolved and are never cited. Do not paste case
   prompt text here. Reference the ids only.
4. Record the library `category`, `style_tags`, and `scene_tags` the skill
   reported for that template.
5. Assemble a `prompt_structure` block list for the downstream prompt using the
   skill blocks: subject and task, composition and layout, visual style and
   materials, text and label requirements, aspect ratio and output format,
   constraints and negative details.

## Template Ids

Choose exactly one `template_id` from the library:
`ui-screenshot-system`, `infographic-engine`, `scientific-scale-diagram`,
`poster-layout-system`, `sports-campaign-poster`, `conceptual-typography-poster`,
`ink-double-exposure-poster`, `nature-science-poster`, `product-commerce-visual`,
`personalized-beauty-report`, `brand-identity-package`, `brand-touchpoint-board`,
`architecture-space`, `realistic-photography`, `street-accident-moment`,
`illustration-art-style`, `character-design-sheet`, `3d-collectible-toy`,
`scene-storytelling`, `history-classical-themes`, `document-publishing`,
`concept-product-breakdown`.

## Discipline Card

Also bind one house `discipline_card`. These cards live in
`.archon/image-nodes/` and carry the crisp-text and layout discipline this
factory adds on top of the library. Map the chosen library category to the
nearest card:
- `Products & E-commerce` maps to `product-commerce-visual`
- `Posters & Typography` maps to `typography-poster`
- `Documents & Publishing` maps to `typography-poster`
- `Photography & Realism` maps to `realistic-photography`
- `Architecture & Spaces` maps to `realistic-photography`
- `History & Classical Themes` maps to `realistic-photography`
- `Brand & Logos` maps to `brand-identity`
- `Characters & People` maps to `people-character`
- `Charts & Infographics` maps to `infographic-explainer`
- `UI & Interfaces` maps to `infographic-explainer`
- `Scenes & Storytelling` maps to `campaign-social-visual`
- `Illustration & Art` maps to `campaign-social-visual`
- `Other Use Cases` maps to `campaign-social-visual`

## QA Focus And Negatives

- Set `qa_focus` to the concrete checks that matter for this category, such as
  legibility, layout hierarchy, material realism, label clarity, likeness
  caution, or sequential continuity.
- Set `negative_constraints` to at least: no watermark, no random logos, no
  garbled text, no extra text beyond requested copy, and
  `no private workflow, local-system, or persona-reference details`. Add
  category-specific negatives from the skill pitfalls where useful.

## Required JSON Shape

```json
{
  "template_id": "product-commerce-visual",
  "category": "Products & E-commerce",
  "discipline_card": "product-commerce-visual",
  "example_case_ids": [1, 8],
  "style_tags": [],
  "scene_tags": [],
  "prompt_structure": [],
  "selection_reason": "",
  "qa_focus": [],
  "negative_constraints": []
}
```

## Final Requirement

Write the artifact first. Then output only valid JSON matching the required
shape. No markdown, no commentary.
