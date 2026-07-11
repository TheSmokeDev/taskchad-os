# Node: people-character

Agnostic image-asset node. Turns a brief into the BEST production spec for people
and character work (character design, pose sheet, mascot, persona card, avatar).
This node is brand and persona AGNOSTIC: a brand design file and a subject/persona
reference are optional inputs supplied at call time, never baked here.

Category: People & Character (the character lane). Inspired by and attributed to
the awesome-gpt-image-2 taxonomy; wording is original (no vendored corpora).

## Hard rule for this node (the reason it exists)

Two rules define this node. First, consistency: the character must stay the SAME
character across a set. When a persona reference is supplied, let the reference lock
carry the likeness and keep skin natural; when none is supplied, describe
identity-stable traits plainly (face shape, hair, build, signature wardrobe) so the
character is reproducible. Second, the image model CANNOT spell, so any name tag /
label / caption stays out of the frame. Therefore every prompt this node emits MUST:
- describe identity-stable traits plainly and let any supplied reference lock the
  likeness, keeping natural skin texture (not plastic), and
- explicitly forbid baked text: "no text, no words, no letters, no numbers, no
  logos, no watermarks, no name tags, no captions."
- when a copy panel is needed (persona cards), place the character to ONE side with
  the full face and eyes visible and never cropped, and reserve empty negative space
  for the overlay. Pure character/pose sheets ship image-only.

## Spec skeleton the node fills (production spec)

1. Character: identity-stable traits (face shape, hair, skin, build, age read),
   wardrobe, signature details, expression, and exact placement.
2. Pose / action: the specific pose or, for a pose sheet, the set of poses/angles;
   consistent proportions across the set.
3. Scene / setting: background or neutral studio ground; keep it supportive so the
   character stays the focus.
4. Composition: aspect (default 4:5 for persona card, 1:1 for avatar, 3:2 for pose
   sheet), character side, and WHERE any empty negative space sits.
5. Style / render: photographic realism, stylized illustration, or mascot-cartoon;
   state it clearly and hold it consistent across the set.
6. Lighting: source, direction, warmth, and how it falls on the face and material
   so the same character reads the same way each time.
7. Color world: pull from the optional brand design palette when a design file is
   supplied; otherwise a clean, consistent palette that keeps the character legible.
8. Negative constraints: no baked text/logos/name tags (above), plus no plastic
   skin, no extra fingers, no warped hands, no distorted or drifting face, no
   inconsistent proportions across the set.

## Guidance

- Lead with the CHARACTER's identity-stable traits; those are what make the set
  reproducible, so state them the same way every run.
- When a persona reference is supplied, trust the reference lock for likeness and
  keep skin natural; do not over-describe the face into a caricature.
- For a set (pose sheet, multi-angle), hold wardrobe, proportion, and style words
  identical across every prompt so the character does not drift.
- For a mascot, keep the silhouette simple and memorable so it reads at small sizes.
- Match the brand mood via the palette only; do not invent a name tag or caption.

## Pitfalls

- Do NOT bake any name, tag, or caption into the frame (garbled text breaks a
  persona card).
- Do NOT let the face drift between images in a set; identity-stable trait wording
  and the reference lock keep it consistent.
- Do NOT over-retouch skin into plastic; natural texture keeps a realistic
  character believable.
- Do NOT center the character when a copy panel is needed; center framing collides
  with the panel and hides the face.
- Do NOT crop the head or eyes when connection matters; the full face carries the
  persona.

## Output contract (what a run of this node returns)

A JSON object:
- `scene_prompt`: the text-free character prompt (identity-stable traits + pose +
  style + placement + natural skin + the no-baked-text rule), ready for the image
  model.
- `aspect`: default `4:5` for persona cards, `1:1` for avatars, `3:2` for pose
  sheets, unless the brief says otherwise.
- `copy`: optional `{ eyebrow, headline, subhead, cta }` for an HTML overlay panel
  on persona cards. Omit for pure character/pose sheets. Copy is short and correctly
  spelled (rendered as HTML, not baked).
- `panel_side`: optional. Where the overlay panel sits (default `top-left`), the
  inverse of the character side, so copy and face never overlap. Omit when
  image-only.
