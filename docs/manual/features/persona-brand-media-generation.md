# Persona Brand Media Generation

Status: Shipped - capability live in `content_factory` + `video_imagegen`; documented 2026-07-08
Owner: social slice (`.claude/scripts/social/content_factory.py`) + scripts slice (`video_imagegen.py`, `video_pipeline.py`) + image-persona packs (`.claude/image-personas/`)
Last updated: 2026-07-08

## What It Is

The Homie can generate on-brand, identity-locked marketing media: images, ads,
and full-body spokesperson banners (and short vertical video), with a real
person's face optionally locked in. Two inputs shape every render:

- a **brand `design_file`** - the palette, fonts, and mood tokens for a brand
  (a design JSON / tokens file), and
- a **`persona_pack`** - a folder of that person's real reference photographs
  used to lock their identity onto the render.

Give it a brand and a face and it produces media that looks like that brand,
starring that person, instead of generic stock art. The capability is
provider-optional: it runs through the codex CLI's `image_generation` feature
when that CLI is installed, and degrades to caption/CSS-only when it is not.

## The Pipeline

```
content_factory.produce(channel)
  -> _render_image / _render_video
       design_file  -> _resolve_design_file  -> video_styles.resolve_design   (brand palette/fonts)
       persona_pack -> _resolve_persona_refs -> curated real-photo refs        (face lock)
  -> video_imagegen.generate_image(prompt, design, aspect, refs=...)
       -> codex exec --enable image_generation   (GPT Image 2), one -i <path> per ref
       -> newest generated PNG copied into the served assets dir
```

- **`content_factory`** is the reusable engine the Archon
  `social-content-factory` workflow and the daily cadence shell. For a channel
  it produces N drafts: copy (via `draft_generator`) + media (image or vertical
  video) + a queued draft carrying the media path.
- **Channels wire the brand and face** in `social/channels.yaml` per channel: a
  `design_file:` (relative to `social/`) and a `persona_pack:` (a folder name
  under `.claude/image-personas/`).
- **`video_imagegen`** is the ONLY place the pipeline touches the codex CLI. It
  attaches each reference photo as a repeatable `-i <path>` arg and appends two
  framework directives (below). It never raises: CLI absence, quota walls,
  timeouts, and parse failures all return `None`, and the caller falls back to
  CSS/caption-only.
- **Direct and workflow entry points:** `video_imagegen.generate_image(...)`
  directly; the Archon `image-node-factory` / `social-content-factory`
  workflows; the `gpt-image-2-style-library` skill for reusable prompt
  scaffolds.

## The Two Framework Directives (identity + retouch)

Every persona render appends two lines from `video_imagegen.py`, so the rules
live in ONE place and every caller inherits them:

- **`_IDENTITY_LOCK_LINE`** - keep the same subject identity shown in the
  references; same person, new scene.
- **`_RETOUCH_LINE`** - keep skin and face natural and photo-realistic (no
  airbrush or beautify); correct only warts/moles and under-eye bags;
  **preserve the subject's real skin TONE and complexion exactly** (do not
  lighten, whiten, or wash out); render hair clean, dry, full, and controlled
  (not damp, greasy, or frizzy) even when a reference photo catches it that way.

Both are the encoded form of the operating rules below. Change the directive,
not an inline copy at a call site.

## Operating Rules (hard-won invariants)

- **Curate the reference set - real photos only.** 3-5 well-chosen references
  beat 16 mixed ones. **Never feed AI-generated renders back into a persona
  pack as identity references** - only real photographs. Feeding renders back
  compounds a plastic, over-smoothed look. A pack's `persona.md` documents which
  refs are the primary identity anchors.
- **NAME every attribute you want controlled.** GPT Image 2 inherits any
  un-named attribute - skin tone, hair state, under-eye bags - from the
  reference photos and drifts it. If a controlled attribute matters (true skin
  tone, luscious-dry vs damp hair), it must be stated in the directive, which is
  exactly what `_RETOUCH_LINE` does.
- **Never auto-generate-and-overwrite.** A regenerate keeps the original as a
  backup; new renders are versioned (`_v2`, `_v3`), never written over the prior
  keeper. The codex CLI persists raw renders under `~/.codex/generated_images/`,
  so an overwritten original is recoverable by matching.
- **Grader vs. taste.** An automated vision grader is for OBJECTIVE defects only
  - duplicates, plastic skin, under-eye bags, wrong person. Aesthetic calls
  (hair, vibe, which of two good renders is better) belong to the operator, not
  the grader.
- **Posting stays default-deny.** `content_factory` only QUEUES drafts; the
  operator approves and the Homie dispatches. Unattended auto-posting requires
  `HOMIE_SOCIAL_UNATTENDED=true` (ships OFF).

## Where Brand And Persona Data Live (isolation boundary)

- **Persona packs** (a person's real reference photos plus a `persona.md` card
  documenting the refs and identity invariants) live under
  `.claude/image-personas/<pack>/`. This tree is **private** - the public export
  sanitizer denies `.claude/image-personas/` - and it is never shared into
  another persona's memory vault.
- **Brand design files** live under `.claude/scripts/social/brand_designs/*.json`.
  Treat brand palettes as private unless a brand is explicitly public.
- **The capability is a system fact; the brand and face data are not.**
  Framework awareness of this capability belongs in a persona's isolated
  `SELF.md` (`## Capabilities`); the brand pack and design files stay in their
  own private homes. See [Persona Memory Isolation](persona-memory-isolation.md)
  for why a fact in one persona's vault never bleeds into another's.

## How To Run It

```bash
cd .claude/scripts
# One image draft for a channel (design_file + persona_pack come from channels.yaml):
uv run python -m social.content_factory <channel> --media image --count 1

# Direct render with an explicit face-lock ref set (no channel):
uv run python -c "import video_imagegen as V; print(V.generate_image(prompt='...', design={}, aspect='1:1', assets_dir='out', refs=['/abs/ref-1.jpg','/abs/ref-2.jpg']))"
```

`content_factory` is fail-open: a media failure degrades that slot to
caption-only and never crashes the run.

## Safety Contract

- Media generation never raises out of `produce()`; a failure degrades to
  caption/CSS-only.
- Default-deny posting: no path posts to a real account unattended without
  `HOMIE_SOCIAL_UNATTENDED=true`.
- Persona packs are private (sanitizer-denied) and per-pack isolated; renders
  never post themselves.
- Identity references must be real photographs of the subject; generated renders
  are never added back as refs.

## Vertical Slice Architecture

| Layer | File | Role |
|---|---|---|
| Content factory | `.claude/scripts/social/content_factory.py` | `produce()`, `_render_image` / `_render_video`, `_resolve_design_file`, `_resolve_persona_refs` |
| Channel wiring | `.claude/scripts/social/channels.yaml` | per-channel `design_file:` + `persona_pack:` |
| Image adapter | `.claude/scripts/video_imagegen.py` | codex `image_generation` invocation, `_IDENTITY_LOCK_LINE` + `_RETOUCH_LINE`, ref attach, output discovery |
| Video pipeline | `.claude/scripts/video_pipeline.py` | vertical MP4 with identity refs on hero/payoff beats |
| Brand designs | `.claude/scripts/social/brand_designs/*.json` | brand palette/fonts token files |
| Persona packs | `.claude/image-personas/<pack>/` (private) | real reference photos + `persona.md` identity card |

## Related

- [Video Generation (`/video`)](video-generation.md) - the same identity-ref
  art path inside the video engine.
- [Social Post Pipeline](social-post-pipeline.md) - the draft/approve/post
  surface `content_factory` queues into.
- [Persona Memory Isolation](persona-memory-isolation.md) - why the capability
  fact lives in each persona's own vault.
