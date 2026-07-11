# Node: brand-identity

Agnostic image-asset node. Turns a brief into the BEST production spec for a brand
mark concept (logo idea, brand-mark, emblem, monogram, icon direction). This node
is brand and persona AGNOSTIC: a brand design file and naming inputs are optional
inputs supplied at call time, never baked here.

Category: Brand & Identity (the identity lane). Inspired by and attributed to the
awesome-gpt-image-2 taxonomy; wording is original (no vendored corpora).

## Hard rule for this node (the reason it exists)

The image model CANNOT render final logotype text. It garbles letterforms, breaks
kerning, and cannot hold a wordmark. So this node pushes toward a SIMPLE, scalable
MARK or EMBLEM concept only, and it treats the output as a CONCEPT to be cleaned up
as vector later, never as a finished logo file. Any wordmark is set in HTML or
vector afterward, never generated. Therefore every prompt this node emits MUST:
- describe a single, simple, scalable symbol/mark/emblem concept that would read at
  small sizes and in one color, and
- explicitly forbid baked text: "no text, no words, no letters, no numbers, no
  typography, no wordmark, no watermarks, no lettering."
- state that the deliverable is a concept for vector cleanup, so the run is judged
  on idea and silhouette, not on pixel-final polish.

## Spec skeleton the node fills (production spec)

1. Mark concept: the core symbol idea (abstract shape, monogram silhouette,
   emblem, geometric motif) and what it evokes, in plain terms.
2. Form language: geometric vs organic, sharp vs rounded, negative-space play,
   single continuous stroke vs solid fill. Keep it reducible.
3. Composition: aspect (default 1:1), the mark centered with clear breathing room,
   sized to read as an app icon or a favicon.
4. Scalability: state that it must survive at small sizes and in a single flat
   color; avoid gradients, fine detail, or effects that break when reduced.
5. Rendering style: flat vector-like concept, clean edges, minimal shading. This is
   a concept sketch, not a photographic or heavily textured render.
6. Color world: pull from the optional brand design palette (accent) when a design
   file is supplied; otherwise a single confident color on a neutral ground, plus a
   one-color silhouette read to prove it reduces.
7. Negative constraints: no baked text/wordmark/typography (above), plus no busy
   detail, no photographic realism, no drop shadows or 3D bevels that fight vector
   cleanup, no mockup props.

## Guidance

- Lead the prompt with SIMPLE and SCALABLE: the strongest marks reduce to one shape
  and one color, so constrain complexity up front.
- Describe the concept idea plainly (what it represents, what feeling it carries)
  and let the form language keep it reducible.
- Ask for a clean centered mark on a plain ground so the silhouette is easy to trace
  into vector later.
- When a brand palette is supplied, use the accent color only; keep the mark
  legible in a single flat fill.
- Do not chase a finished logo; this node produces direction and silhouette, and
  the wordmark is added in vector/HTML after.

## Pitfalls

- Do NOT ask the model to render the brand name or any letters; garbled logotype is
  the guaranteed failure here.
- Do NOT design a mark so detailed it collapses at favicon size; scalability is the
  whole test.
- Do NOT add gradients, bevels, or drop shadows that make vector cleanup harder.
- Do NOT stage the mark on a mockup (business card, sign, shirt); this node outputs
  the concept, not a presentation.

## Output contract (what a run of this node returns)

A JSON object:
- `scene_prompt`: the text-free mark-concept prompt (single simple scalable symbol +
  form language + centered clean ground + one-color read + the no-baked-text rule),
  ready for the image model. Framed as a concept for vector cleanup.
- `copy`: optional `{ wordmark }` string carrying the intended brand name for the
  later HTML/vector lockup. It is NEVER baked into the image; it is metadata for the
  cleanup step.
- `aspect`: default `1:1` unless the brief says otherwise.
