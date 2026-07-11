# Node: product-commerce-visual

Agnostic image-asset node. Turns a product brief into the BEST production spec for
a commerce image (product hero shot, packaging render, e-commerce detail page,
lifestyle product placement). This node is brand and persona AGNOSTIC: a brand
design file and a product/persona reference are optional inputs supplied at call
time, never baked here.

Category: Products & E-commerce (the commerce lane). Inspired by and attributed to
the awesome-gpt-image-2 taxonomy; wording is original (no vendored corpora).

## Hard rule for this node (the reason it exists)

The image model CANNOT spell and it garbles packaging copy. So the generated image
is the PRODUCT SCENE ONLY. All packaging text / label copy / price / eyebrow /
headline / CTA / logo is overlaid later as crisp HTML by the card composer.
Therefore every prompt this node emits MUST:
- keep the hero product prominent, tack-sharp, and correctly proportioned, with
  real materials (glass, metal, matte plastic, fabric, paper) rendered honestly,
  and
- explicitly forbid baked text: "no text, no words, no letters, no numbers, no
  logos, no watermarks, no label copy, no price tags."
- keep the product silhouette clean and recognizable; when a copy panel is needed,
  reserve generous EMPTY negative space to one side so the overlay never crosses
  the product.

## Spec skeleton the node fills (production spec)

1. Product: what it is, form factor, material, finish, color, exact placement
   (e.g. "centered on the lower-left third, full silhouette visible, not cropped").
2. Surface / setting: studio sweep, stone slab, wet counter, wood table, or a
   lifestyle context that matches the use case. No random props that pull focus or
   weaken product recognition.
3. Composition: aspect (default 1:1 for e-commerce, 4:5 for lifestyle), product
   side, and WHERE the empty negative space sits for the overlay panel.
4. Camera / realism: lens + distance + depth of field, honest reflections and
   shadows, crisp product edges, commercial-catalog realism.
5. Lighting: source, direction, softness. Studio softbox for hero shots, motivated
   natural light for lifestyle. Show material truthfully (specular on glass/metal).
6. Color world: pull from the optional brand design palette (bg/accent) when a
   design file is supplied; otherwise a clean neutral studio or daylight world.
7. Negative constraints: no baked text/logos/label copy (above), plus no warped
   product geometry, no melted edges, no duplicated product, no floating shadows.

## Guidance

- Lead the prompt with the PRODUCT + MATERIAL + PLACEMENT + negative-space
  instruction; product recognition is load-bearing, so state it early and firmly.
- Keep the material description concrete (brushed aluminum, frosted glass, kraft
  paper) so the render reads as a real object, not a generic blob.
- Props exist only to support scale, use case, or mood. If a prop competes with the
  product for attention, cut it.
- When a product reference is supplied, describe form-stable traits plainly and let
  the reference lock carry the exact shape; keep materials honest.
- Match the brand mood via the palette only; do not invent packaging copy or a logo.

## Pitfalls

- Do NOT bake any packaging copy, price, or logo into the scene (the #1 failure:
  garbled label text).
- Do NOT bury the product under styling props; commerce lives or dies on clear
  product recognition.
- Do NOT let the model warp product geometry; a bent bottle or melted edge kills
  trust in a hero shot.
- Do NOT over-polish materials into fake plastic; catalog realism keeps honest
  reflections and texture.

## Output contract (what a run of this node returns)

A JSON object:
- `scene_prompt`: the text-free product scene prompt (product + material +
  placement + negative space + camera + lighting + palette + the no-baked-text
  rule), ready for the image model.
- `copy`: optional `{ eyebrow, headline, subhead, cta }` for the HTML overlay
  panel. Copy is short, benefit-led, and spelled correctly (rendered as HTML, not
  baked). Many pure hero shots ship image-only with no copy.
- `aspect`: default `1:1` for e-commerce grids, `4:5` for lifestyle, unless the
  brief says otherwise.
- `panel_side`: where the overlay panel sits (default `right`), the inverse of the
  product side, so copy and product never overlap.
