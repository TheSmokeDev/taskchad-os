# Node: infographic-explainer

Agnostic image-asset node. Turns a brief into the BEST production spec for an
explainer visual (infographic, knowledge map, process diagram, data explainer).
This node is brand and persona AGNOSTIC: a brand design file and a subject/persona
reference are optional inputs supplied at call time, never baked here.

Category: Infographics & Explainers (the explainer lane). Inspired by and
attributed to the awesome-gpt-image-2 taxonomy; wording is original (no vendored
corpora).

## Hard rule for this node (the reason it exists)

The image model CANNOT render real data. It garbles numbers, mislabels charts,
misspells legends, and invents fake axes. So the image supplies ONLY the decorative
hero art, section motifs, iconography, and clean EMPTY regions where data will
land. The actual title, labels, numbers, chart values, and legends are HTML or SVG
overlaid later by the card composer, never baked. Therefore every prompt this node
emits MUST:
- describe decorative section art plus clear empty data regions, with NO real
  charts, numbers, or labels drawn by the model, and
- explicitly forbid baked text and fake data: "no text, no words, no letters, no
  numbers, no charts with values, no axis labels, no legends, no logos, no
  watermarks."
- reserve well-defined EMPTY regions (panels, bands, columns) sized to hold the
  overlaid title and data points, and keep decorative art clear of those regions.

## Spec skeleton the node fills (production spec)

1. Hero / theme art: the decorative visual that sets the subject (illustrated
   motif, abstract flow, isometric scene) without depicting real data.
2. Section structure: how the empty data regions are arranged (stacked bands, grid
   of cards, left rail plus content, radial map) and where each empty region sits.
3. Iconography: simple, consistent decorative icons or motifs per section, drawn as
   art only, never as labeled chart elements.
4. Composition: aspect (default 4:5 for social infographic, 2:3 for tall
   explainer), and WHERE each empty data/title region sits for the overlay.
5. Style / render: flat vector, soft illustration, isometric, or clean line art;
   held consistent across sections.
6. Color world: pull from the optional brand design palette (bg/accent) when a
   design file is supplied; otherwise a clean, high-legibility palette that keeps
   overlaid data readable in the empty regions.
7. Negative constraints: no baked text/numbers/charts/labels (above), plus no fake
   graphs, no invented axes, no clutter inside the reserved data regions, no
   low-contrast mush where numbers land.

## Guidance

- Lead the prompt with the EMPTY DATA REGIONS: state up front which panels or bands
  must stay clean and low-contrast, because the whole layout exists to hold overlaid
  data.
- Let the model do what it is good at: decorative hero art, section motifs, and
  consistent iconography. Keep it away from anything that looks like a real chart.
- Design the empty regions with clear edges and calm backgrounds so overlaid numbers
  and labels stay legible.
- When a persona/subject reference is supplied, describe identity-stable traits
  plainly and let the reference lock carry likeness; keep it clear of data regions.
- Match the brand mood via the palette only; do not invent titles, numbers, or a
  logo.

## Pitfalls

- Do NOT let the model draw any chart, number, axis, or label. Model-rendered data
  is always garbled and is the #1 failure of this node.
- Do NOT fill the data regions with decorative detail; overlaid numbers need calm,
  clean space.
- Do NOT ask for "a chart showing X"; ask for an EMPTY region where the HTML/SVG
  chart will be placed.
- Do NOT let icons drift in style between sections; consistency carries the
  explainer.

## Output contract (what a run of this node returns)

A JSON object:
- `scene_prompt`: the text-free explainer-art prompt (decorative hero + section
  motifs + iconography + clearly reserved EMPTY data regions + palette + the
  no-baked-text-or-data rule), ready for the image model.
- `copy`: `{ title, points[] }` for the HTML/SVG overlay. `title` is the explainer
  headline; `points[]` are the labels, numbers, and data that land in the empty
  regions. All correctly spelled and rendered as HTML/SVG, never baked.
- `aspect`: default `4:5` for social infographics, `2:3` for tall explainers,
  unless the brief says otherwise.
- `panel_side`: where the primary title/data overlay sits (default `top`), matched
  to the reserved empty region in the art.
