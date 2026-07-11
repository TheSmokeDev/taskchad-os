# Node: realistic-photography

Agnostic image-asset node. Turns a brief into the BEST production spec for a
realistic photograph (portrait, lifestyle, street, commercial realism). This node
is brand and persona AGNOSTIC: a brand design file and a subject/persona reference
are optional inputs supplied at call time, never baked here.

Category: Photography & Realism (the realism lane). Inspired by and attributed to
the awesome-gpt-image-2 taxonomy; wording is original (no vendored corpora).

## Hard rule for this node (the reason it exists)

Two rules define this node. First, realism is earned through specifics: the prompt
MUST name the lens, the camera distance, the light source, and the surface texture,
and it MUST keep skin NATURAL with real texture (pores, fine lines, subtle color
variation) instead of plastic over-retouched perfection. Second, the image model
CANNOT spell, so any headline / label / caption stays out of the frame. Therefore
every prompt this node emits MUST:
- specify lens + camera distance + light source + texture, and preserve believable
  human imperfection (natural skin, stray hair, real fabric wrinkles), and
- explicitly forbid baked text: "no text, no words, no letters, no numbers, no
  logos, no watermarks, no captions."
- when a copy panel is needed, place the subject to ONE side (default right third)
  with the full face and eyes visible and never cropped, and reserve empty negative
  space for the overlay. Many portraits ship image-only with no panel.

## Spec skeleton the node fills (production spec)

1. Subject: who, wardrobe, expression, action, exact placement (e.g. "on the right
   third, full head and face near top-right, not cropped").
2. Scene / setting: location, era, season, time of day, story context, background
   depth.
3. Composition: aspect (default 4:5 for portrait, 3:2 for lifestyle/street),
   subject side, and WHERE any empty negative space sits.
4. Camera / realism: lens (e.g. 35mm, 50mm, 85mm), camera distance (close-up, waist
   up, full body), depth of field, and the believable imperfection that sells it.
5. Lighting: source (window, golden hour, softbox, overcast, practical), direction,
   warmth, and how it falls on skin and texture.
6. Color world: pull from the optional brand design palette when a design file is
   supplied; otherwise a natural, honest color response for the light described.
7. Negative constraints: no baked text/logos (above), plus no plastic skin, no
   extra fingers, no warped hands, no distorted face, no duplicated limbs, no
   waxy over-smoothing.

## Guidance

- Lead with the SUBJECT + lens/distance + light source; realism is built from those
  concrete camera facts, not from adjectives like "beautiful" or "cinematic."
- Name the texture the light reveals (skin pores, wool weave, wet asphalt, worn
  leather). Texture is what separates a photograph from a render.
- When a persona/subject reference is supplied, describe identity-stable traits
  plainly and let the reference lock carry likeness; keep skin natural.
- Allow one believable imperfection on purpose (a stray hair, a soft catchlight, a
  slight asymmetry). Perfection reads as fake.
- Match the brand mood via the palette only; do not invent a logo or caption.

## Pitfalls

- Do NOT bake any copy into the frame (garbled caption text ruins the shot).
- Do NOT let the model over-retouch skin into plastic; that is the top realism tell.
- Do NOT omit the lens and light source; a vague prompt yields a generic render.
- Do NOT center a subject when a copy panel is needed; center framing collides with
  the panel and hides the face.
- Do NOT crop the head or eyes when connection matters; the full face carries trust.

## Output contract (what a run of this node returns)

A JSON object:
- `scene_prompt`: the text-free photographic prompt (subject + lens + distance +
  light source + texture + natural skin + placement + the no-baked-text rule),
  ready for the image model.
- `aspect`: default `4:5` for portraits, `3:2` for lifestyle/street, unless the
  brief says otherwise.
- `copy`: optional `{ eyebrow, headline, subhead, cta }` for an HTML overlay panel.
  Omit for pure image-only portraits. Copy is short and spelled correctly (rendered
  as HTML, not baked).
- `panel_side`: optional. Where the overlay panel sits (default `top-left`), the
  inverse of the subject side, so copy and face never overlap. Omit when image-only.
