# Node: campaign-social-visual

Agnostic image-asset node. Turns a marketing brief into the BEST production spec
for a social/advertising hero image (feed post, story, reel cover, paid ad). This
node is brand and persona AGNOSTIC: a brand design file and a subject/persona are
optional inputs supplied at call time, never baked here.

Category: Products & E-commerce / Photography & Realism / Posters & Typography
(the ad-marketing lane). Inspired by and attributed to the awesome-gpt-image-2
taxonomy; wording is original (no vendored corpora).

## Hard rule for this node (the reason it exists)

The image model CANNOT spell. So the generated image is the SCENE ONLY. All
headline / subhead / CTA / logo / price copy is overlaid later as crisp HTML by
the card composer. Therefore every prompt this node emits MUST:
- describe a text-free photographic scene, and
- explicitly forbid baked text: "no text, no words, no letters, no numbers, no
  logos, no watermarks, no UI, no signage copy."
- reserve generous EMPTY negative space for the text panel, and place the human
  subject to ONE side (default right third) with the full face and eyes clearly
  visible and never cropped by the frame edge.

## Spec skeleton the node fills (production spec)

1. Subject: who/what, wardrobe, expression, action, exact placement (e.g. "on the
   right third, full head and face visible near top-right, not cropped").
2. Scene / setting: location, props, season, time of day, story context.
3. Composition: aspect (default 9:16), subject side, and WHERE the empty negative
   space sits (default: left/upper two-thirds open) for the overlay panel.
4. Camera / realism: lens + distance + depth of field, believable imperfection,
   commercial-photography realism (avoid over-polished plastic skin).
5. Lighting / mood: source, direction, warmth.
6. Color world: pull from the optional brand design palette (bg/accent) when a
   design file is supplied; otherwise a clean neutral daylight world.
7. Negative constraints: no baked text/logos (above), plus no extra fingers, no
   warped hands, no distorted face, no duplicated limbs.

## Guidance

- Lead the prompt with the SUBJECT + PLACEMENT + negative-space instruction; the
  layout is load-bearing for the card, so state it early and firmly.
- Keep it photographic and specific (lens, light, texture), not generic.
- When a persona/subject reference is supplied, describe identity-stable traits
  plainly and let the reference lock handle likeness; keep skin natural.
- Match the brand mood via the palette/tagline only; do not invent a logo.

## Pitfalls

- Do NOT bake any copy into the scene (the #1 failure: garbled ad text).
- Do NOT center the subject if a text panel is needed; center framing collides
  with the panel and hides the face.
- Do NOT over-retouch skin into plastic; commercial realism keeps real texture.
- Do NOT crop the head/eyes; trust and connection need the full face.

## Output contract (what a run of this node returns)

A JSON object:
- `scene_prompt`: the text-free scene prompt (subject + placement + negative space
  + camera + lighting + palette + the no-baked-text rule), ready for the image model.
- `copy`: `{ eyebrow, headline, subhead, cta }` for the HTML overlay panel. Copy is
  short, benefit-led, and spelled correctly (it is rendered as HTML, not baked).
- `aspect`: default `9:16` unless the brief says otherwise.
- `panel_side`: where the overlay panel sits (default `top-left`), the inverse of
  the subject side, so copy and face never overlap.
