---
name: video-director
description: |
  Direct a complete, cinematic MP4 video for ANY topic, niche, or brand:
  promo and launch videos, product update clips, event and fan videos,
  explainer shorts. Orchestrates the HyperFrames skill family end to end:
  brief intake, a per-brand design system, an optional recurring character,
  composition with catalog blocks, voiceover with a voice bake-off,
  voiceover-driven timing, deterministic render, and quality gates.
  Triggers: "make a video about X", "create a launch/promo video for my
  product or site", "make a video for my niche/event/team" (sports, music,
  food, fashion, anything), "brand video with my colors", "video with a
  recurring character or mascot", "add a voiceover and render an MP4".
  Nothing is hard-coded to any brand: every color, voice, character, and
  claim comes from the brief.
---

# Video Director

Turn a one-line ask into a finished, deterministic MP4: HTML/CSS + data
attributes rendered frame-by-frame in headless Chrome and encoded with
FFmpeg. This skill DIRECTS the pipeline; the mechanics live in the
HyperFrames helper skills (referenced by name below) and are not duplicated
here.

## Prerequisites

- Node 18+ (`npx hyperframes`, CLI 0.6.x or newer) and ffmpeg + ffprobe on PATH.
- The HyperFrames helper skills installed: run `npx hyperframes skills`
  (or `npx skills add heygen-com/hyperframes`), then restart the agent
  session. You get: `hyperframes` (compositions), `hyperframes-cli`
  (init/lint/preview/render), `hyperframes-media` (local TTS via Kokoro,
  transcribe, background removal), `hyperframes-registry` (catalog blocks),
  `website-to-hyperframes` (URL capture), plus animation skills (`gsap`,
  `css-animations`, `three`, `lottie`, `animejs`, `waapi`).
- Optional for voiceover: `edge-tts` (free neural voices, needs network) or
  Kokoro through `hyperframes-media` (fully local).
- Optional for a recurring character: any image tool that supports image
  EDITS with a reference image (for example, the Codex CLI image tool).

## Step 1: Brief intake

Collect (ask only for what is missing; sensible defaults otherwise):

```
topic / niche:        e.g. "World Cup group-stage hype video for my fan page"
audience + platform:  e.g. "football fans on X" -> 16:9 (or 9:16 vertical, 1:1)
duration target:      e.g. 30-45s
tone (3 adjectives):  e.g. electric, communal, cinematic
brand inputs:         a URL | palette + fonts | "pick for me"
recurring character:  yes/no (if yes: describe or provide a base image)
claims source:        where facts come from (site, changelog, press page, stats source)
CTA / payoff:         what the last frame asks (follow, visit, star, subscribe)
```

Worked example (deliberately non-software): a World Cup fan video. Brand
inputs = the national team's two colors; tone = electric, proud, cinematic;
beats = anthem-style hook, three group-stage fixtures with animated
date/venue cards, a stat count-up (titles won), payoff = "follow for every
match" card with the fan page's name and avatar. Every number comes from the
official fixture list (the claims source). No software anywhere: the same
pipeline carries any niche.

## Step 2: Design system (their brand, never a default)

Produce a `design.md` in the project root (HyperFrames tooling reads it).
Three routes:

1. **From a URL:** use the `website-to-hyperframes` skill's capture flow to
   extract palette, typography, and imagery from their site.
2. **From given palette/fonts:** fill this skeleton. Every slot comes from
   the brief; never reuse a previous project's values:

```markdown
# <Brand> Video Design System (frame.md)
## Palette (4-6 tokens, hex)
bg / fg / accent / accent-dim / warn: <from brief>
## Typography
display: <font, weight, sizes for headline/subhead>
mono or secondary: <font> (numbers, labels, tickers)
## Frame rules (<aspect>)
safe margins, headline max-width, caption band position
## Caption + lower-third layout
hero caption: position, scrim treatment; lower-third: name/handle badge spec
## Motion vocabulary (pick 4-6, name them)
entrances (rise, kinetic-words, type-on), one ambient per scene (drift,
breathe, parallax), ONE primary transition + 1-2 accents, count-up for stats
## Rhythm
a visual change every 2-3 seconds; the hook lands inside the first 2s
## Claim safety
every number/claim traces to: <claims source>; no invented stats, no
superlatives; no em-dashes in on-screen copy
## Authoring checklist
lint/validate/inspect clean; assets relative; PRE-HIDE every later reveal;
first-frame still per scene; ffprobe full-duration check
```

3. **Neither given:** browse and remix a template from hyperframes.dev/design
   via the `hyperframes` skill, then re-token it to the brief.

## Step 3: Optional recurring character (identity lock)

A recognizable character across videos is a brand asset. The technique that
keeps it consistent, with any capable image tool:

1. Generate ONE base character image and save it as the canonical reference.
2. Every later scene is an EDIT of that base, never a fresh generation.
   Prompt shape: "Identity-preserve the attached character exactly. EDIT, do
   not generate fresh. Change only scene/clothing/environment/props to:
   <scene>." With the Codex CLI, for example:
   `codex exec --enable image_generation --image <base.png> -` with the
   prompt piped on stdin (non-interactive shells must redirect stdin or the
   CLI waits forever); collect the newest image from the tool's output
   directory.
3. VERIFY each image by viewing it. File size proves nothing.
4. Multiple roles of one character: vary the dominant COLOR, the
   environment, and the props per role, or they read as the same shot.

## Step 4: Compose

- Init the project with `hyperframes-cli`. Scene order: hook -> body beats
  -> proof beat -> payoff/CTA card.
- Install catalog blocks via `hyperframes-registry` instead of hand-rolling:
  a shader transition (for example chromatic-radial-split) for the ONE
  biggest cut, a social payoff card, grain-overlay for texture, code-snippet
  or data-chart when the content calls for it.
- The payoff card uses the USER's display name, handle, and avatar from the
  brief. Render a verification badge ONLY if the user confirms the account
  is actually verified.
- Served-assets rule: copy every image/audio file INTO the project's
  `assets/` directory and reference it RELATIVELY (`assets/foo.png`). The
  headless renderer serves only the project directory; absolute paths and
  `file://` URIs render blank with no error.

## Step 5: Voiceover (voice bake-off)

1. Write per-beat VO lines (one line per scene; the first line is the hook).
2. Generate the SAME sample line in 3-4 candidate voices matched to the
   brief's tone (edge-tts: pick from `edge-tts --list-voices` by language,
   gender, energy; or Kokoro via `hyperframes-media` for fully local).
3. Deliver the candidates to the user; THEY pick. Then generate every beat
   in the winning voice.
4. Pronunciation rule: phonetically respell brand and proper names in the
   SPOKEN text only ("Nike" -> "Ny-kee" if needed); on-screen text stays
   correctly spelled. If a respell still sounds wrong, drop the name from
   the VO and let the on-screen card carry it.

## Step 6: Voiceover-driven timing

Never hand-guess durations. Measure each beat clip with ffprobe; scene
duration = VO duration + a small pad, with a minimum-frames floor, scaled to
the target total. Concatenate beats into ONE audio track with silence gaps
(ffmpeg adelay) so each line lands exactly on its scene.

## Step 7: Quality gates (all of them, every render)

1. **PRE-HIDE:** any element that reveals after t=0 gets
   `tl.set(el, {autoAlpha: 0}, 0)` BEFORE its reveal tween. A `tl.from()`
   alone leaks: the element sits visible until the playhead reaches it.
   Plain CSS `opacity: 0` is NOT enough when a shader transition rasterizes
   the scene (the rasterizer checks each element's own computed style;
   `visibility: hidden`, which autoAlpha sets, inherits to children).
   Avoid SVG stroke-only icons inside shader-transition scenes; the
   rasterizer cannot draw them.
2. `npx hyperframes lint`, `validate`, and `inspect` all clean.
3. Render, then extract a still at EACH scene's FIRST frame and view every
   one. This catches pre-hide leaks and missing assets that mid-scene stills
   miss.
4. ffprobe the MP4: H.264 video + AAC audio, both spanning the full duration.
5. Claim safety: every fact on screen or spoken traces to the brief's claims
   source. Unique output directory per run; never overwrite a prior render.

## Step 8: Deliver

Hand over the MP4 path with platform notes (size, aspect, duration). Offer
recuts to other aspects from the same composition. Revision etiquette: apply
user feedback as numbered fix rounds, change only what was flagged, one
re-render per round.

## Failure modes

| Symptom | Cause -> fix |
|---|---|
| An image is missing in the render, no error | Absolute or `file://` path. Copy into `assets/`, reference relatively. |
| Element flashes before its reveal | Pre-hide leak: `tl.set autoAlpha 0` at t=0; check first-frame stills. |
| Two scenes overlap mid-transition | The shader rasterizes both; fully hide scene A before B enters, or use dip-to-black for that cut. |
| VO mispronounces a name | Respell spoken-only; on-screen stays correct. |
| Audio drifts from visuals | Re-measure with ffprobe and rebuild the adelay concat; timing is always derived, never guessed. |
| Render passes but looks wrong | The gates prove integrity, not taste. View the stills; iterate the design system, not just the tweens. |

## Native /video command

The framework also exposes this capability as a native chat command: `/video
<brief> [--style name] [--aspect 16:9|9:16|1:1] [--design file]` works from
Telegram, Discord, Slack, the web dashboard, and the CLI, with `/video
styles` listing the built-in style library. The command is model-agnostic
(its LLM moments run through the runtime lanes) and delivers the finished
MP4 back into the same conversation. Use the command for one-shot renders;
use this skill when directing a richer, multi-revision production. Details:
`docs/manual/features/video-generation.md`.

## Boundaries

- This skill never posts to any platform; it produces files.
- It never invents claims; no claims source in the brief means no numbers in
  the video.
- For composition/HTML mechanics, defer to the `hyperframes` skill rather
  than restating it; this skill is the director, not the renderer.
