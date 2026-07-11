# Node: typography-poster

Agnostic image-asset node. Turns a brief into the BEST production spec for a
type-forward layout (poster, flyer, quote card, event cover, announcement). This
node is brand and persona AGNOSTIC: a brand design file and a subject/persona
reference are optional inputs supplied at call time, never baked here.

Category: Posters & Typography (the typography lane). Inspired by and attributed to
the awesome-gpt-image-2 taxonomy; wording is original (no vendored corpora).

## Hard rule for this node (the reason it exists)

This is the node where the rule is strongest, because typography is the whole
point. The image model CANNOT set type. It garbles headlines, misspells words, and
mangles kerning. So the model makes ONLY the background art, the composition, and
generous EMPTY type zones. Every headline, subhead, eyebrow, date, and CTA is
HTML-overlaid later by the card composer, never baked. Therefore every prompt this
node emits MUST:
- describe a text-free background/art field with a deliberate composition, and
- explicitly forbid baked text: "no text, no words, no letters, no numbers, no
  typography, no logos, no watermarks, no lettering of any kind."
- reserve LARGE open type zones (default: a clear vertical band or upper/lower
  third left intentionally empty) where the HTML headline stack will land, and keep
  any focal art clear of those zones.

## Spec skeleton the node fills (production spec)

1. Art / background: the visual field (abstract gradient, textured paper, photo
   scene, illustrated motif, geometric shapes) and its mood.
2. Focal element: optional single subject or motif, and its exact placement OFF the
   type zone (e.g. "motif anchored bottom-right, upper two-thirds left empty").
3. Composition: aspect (default 2:3 for poster, 4:5 for social flyer), and WHERE
   the empty type zones sit for the headline/subhead/CTA overlay stack.
4. Texture / finish: paper grain, print noise, risograph feel, clean vector flat,
   or photographic depth. This carries the poster's craft.
5. Lighting / depth: for photographic or dimensional backgrounds, source and
   direction; for flat art, the layering and contrast that keep type legible.
6. Color world: pull from the optional brand design palette (bg/accent) when a
   design file is supplied; otherwise a confident, high-contrast poster palette
   that leaves room for legible overlaid type.
7. Negative constraints: no baked text/typography/logos (above), plus no busy
   clutter inside the reserved type zones, no low-contrast mush where headlines land.

## Guidance

- Lead the prompt with the TYPE ZONES: state up front which regions must stay empty
  and low-contrast, because the layout exists to hold overlaid type.
- Design the background to make type legible: keep the headline zone calm, push
  detail and contrast to the edges or the focal element.
- Give the background real craft (grain, texture, deliberate color blocking) so the
  finished poster does not look like empty filler behind text.
- When a persona/subject reference is supplied, describe identity-stable traits
  plainly and let the reference lock carry likeness; keep it clear of the type zone.
- Match the brand mood via the palette only; do not invent a wordmark or headline.

## Pitfalls

- Do NOT bake ANY lettering into the art. This is the highest-risk node for garbled
  text, so the forbid-text clause is non-negotiable.
- Do NOT fill the whole frame with busy detail; overlaid type needs calm negative
  space or it becomes unreadable.
- Do NOT place a high-contrast focal element where the headline stack will sit.
- Do NOT rely on the model for "just a little" text; even one baked word breaks the
  card. All type is HTML.

## Output contract (what a run of this node returns)

A JSON object:
- `scene_prompt`: the text-free background/art prompt (art field + focal placement
  + reserved empty type zones + texture + palette + the no-baked-text rule), ready
  for the image model.
- `copy`: `{ eyebrow, headline, subhead, cta }` for the HTML overlay type stack.
  Copy is short, correctly spelled, and carries the entire message (rendered as
  HTML, never baked).
- `aspect`: default `2:3` for posters, `4:5` for social flyers, unless the brief
  says otherwise.
- `panel_side`: where the overlaid type stack sits (default `center` for posters,
  or a named third), matched to the reserved empty zone in the art.
