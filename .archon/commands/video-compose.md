---
description: Author a full HyperFrames composition from the approved vision - agent-crafted choreography, registry blocks, voiceover-driven timing, and the pre-hide pass.
argument-hint: (no arguments; reads vision.json, intake.json, dossier.json from $ARTIFACTS_DIR)
---

# Video Compose

You are the composition step of the video-production workflow. The operator
has APPROVED the vision in `$ARTIFACTS_DIR/vision.json`; your job is to turn
that outline into a complete, deterministic HyperFrames project at
`$ARTIFACTS_DIR/composition/`. This is the agent-crafted tier: richer
choreography than the native pipeline's archetype renderer. Follow the
`video-director` skill for the full workflow; this file pins the contract.

## Inputs

- `$ARTIFACTS_DIR/vision.json`: the approved angle, beats
  (`[{kind, summary}]`), imagery treatment, duration_s, aspect, style.
- `$ARTIFACTS_DIR/intake.json`: the original brief and flags.
- `$ARTIFACTS_DIR/dossier.json` + `$ARTIFACTS_DIR/research-assets/`
  (optional): facts (the ONLY permitted source of numbers beyond the brief),
  a derived design dict, downloaded reference images.

## Contract (hard rules)

1. **Project shape**: `composition/index.html` + `composition/assets/`.
   Initialize with the HyperFrames CLI (`npx hyperframes init`) or copy its
   minimal shape. Every media file is copied INTO `assets/` and referenced
   RELATIVELY (`assets/foo.png`); absolute and `file://` paths render blank.
2. **Design system**: write `composition/design.md` first. Source order:
   the vision's named style tokens > the dossier's `derived_design` (palette,
   fonts) > a neutral dark default. Every color in the composition comes
   from these tokens; no ad-hoc hex.
3. **Scenes from the outline**: one scene per vision beat, in order, kind
   driving the layout (hero opener, stat as a wallpaper-scale number, list /
   cards / ledger as structured panels, quote as one strong line, mockup as
   a product frame, payoff as the closing CTA, caption as plain statement).
   Scene containers carry `class="scene clip"`, `data-start`,
   `data-duration`, `data-track-index`.
4. **Voiceover drives timing**: write one spoken line per beat, synthesize
   per-beat audio (edge-tts, or the `hyperframes-media` TTS), measure each
   clip with ffprobe, set scene durations = VO duration + a small pad with a
   minimum-scene floor, scale to the vision's duration_s. Mix into one track
   with ffmpeg adelay so each line lands exactly on its scene; add it as an
   `<audio>` node.
5. **Karaoke captions**: split each beat's spoken line into words, distribute
   them across that beat's measured duration by character weight (no STT),
   page at most 5 words or 30 chars, per-word highlight tweens, hard
   `tl.set` hide on each page exit.
6. **Choreography**: one paused GSAP timeline registered on
   `window.__timelines["main"]`. Entrances staggered by importance (total
   under ~0.5s), one ambient motion per scene, transitions between scenes
   (crossfade / slide / whip / dip; at most ONE shader transition and only
   between fully raster-safe scenes). Deterministic only: no Math.random,
   no Date.now, no requestAnimationFrame, no setTimeout, no infinite repeats.
7. **PRE-HIDE pass (last, mandatory)**: every element that reveals after
   t=0 gets `tl.set(el, {autoAlpha: 0}, 0)` BEFORE its reveal tween. A
   `tl.from()` alone leaks the element before its entrance. Use autoAlpha,
   never plain opacity, so children are hidden from any shader raster too.
8. **Claim safety**: every number on screen or spoken traces to the brief or
   `dossier.json` facts. No invented metrics, no superlatives, no em-dash
   characters in on-screen copy.
9. **Imagery treatment**: honor `vision.imagery.treatment`. `css` means
   typographic scenes only; `photos` means use `research-assets/` images
   directly; `stylized` permits generated art when an image tool is
   available, else degrade to css gracefully.
10. **Scene index for QA**: write `$ARTIFACTS_DIR/scenes.json` as
    `[{"id": "...", "start": <seconds>}]` for every scene, sorted by start.

## Finish line

Run `npx hyperframes lint` and `npx hyperframes validate` inside
`composition/` and fix every error before finishing. Your final output is a
short build report: scene count, total duration, design source used,
transition plan, and the composition path.
