---
name: video-director
description: |
  Direct a complete, cinematic MP4 video for ANY topic, niche, or brand:
  promo and launch videos, product update clips, event and fan videos,
  explainer shorts. Orchestrates the HyperFrames skill family end to end:
  brief intake, a per-brand design system, an optional recurring character,
  scene choreography with a named motion vocabulary, composition with
  catalog blocks, voiceover with a voice bake-off, voiceover-driven timing
  and karaoke captions, deterministic render, and quality gates.
  Triggers: "make a video about X", "create a launch/promo video for my
  product or site", "video from this URL in my brand colors", "make a video
  for my niche/event/team" (sports, music, food, fashion, anything),
  "walk me through making a video" (the guided wizard), "draft the vision
  first" (the approval gate), "video with a recurring character or mascot",
  "add a voiceover and render an MP4". Nothing is hard-coded to any brand:
  every color, voice, character, and claim comes from the brief.
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
  transcribe, background removal), `hyperframes-registry` (the catalog:
  70+ transition blocks and 15 caption styles to install instead of
  hand-rolling), `website-to-hyperframes` (URL capture), plus animation
  skills (`gsap`, `css-animations`, `three`, `lottie`, `animejs`, `waapi`).
- Optional for voiceover: `edge-tts` (free neural voices, needs network) or
  Kokoro through `hyperframes-media` (fully local).
- Optional for a recurring character: any image tool that supports image
  EDITS with a reference image (for example, the Codex CLI image tool).

## Step 1: Brief intake

Collect (ask only for what is missing; sensible defaults otherwise):

```
topic / niche:        e.g. "World Cup group-stage hype video for my fan page"
kind:                 event recap | brand promo | product launch | explainer
                      | hype reel | surprise me  (sets pacing + duration default)
audience + platform:  e.g. "football fans" -> 16:9 (or 9:16 vertical, 1:1)
duration target:      e.g. 30-45s (hype ~20s, promo/launch ~30s, explainer ~45s)
tone (3 adjectives):  e.g. electric, communal, cinematic
brand inputs:         a URL | palette + fonts | "pick for me"
imagery treatment:    stylized art | real photos from their site | pure CSS
                      scenes (ask; photos only when a source site exists)
recurring character:  yes/no (if yes: describe or provide a base image)
claims source:        where facts come from (site, changelog, press page)
CTA / payoff:         what the last frame asks (follow, visit, star, subscribe)
```

Worked example (deliberately non-software): a World Cup fan video. Kind =
hype reel; brand inputs = the national team's two colors; tone = electric,
proud, cinematic; beats = anthem-style hook, three group-stage fixtures with
animated date/venue cards, a stat count-up (titles won), payoff = "follow
for every match" card with the fan page's name and avatar. Every number
comes from the official fixture list (the claims source). No software
anywhere: the same pipeline carries any niche.

## Step 2: Design system (their brand, never a default)

Produce a `design.md` in the project root (HyperFrames tooling reads it).
Three routes:

1. **From a URL:** capture palette, typography, and imagery from their site.
   Use the `website-to-hyperframes` skill's capture flow, or extract tokens
   directly: CSS custom properties and frequent hex values map to
   bg/fg/accent roles, Google Fonts links and font-family rules map to
   display/body/mono. (The framework's research module automates exactly
   this derivation for the native command.)
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

## Step 4: Scene choreography patterns

The grammar that separates a caption deck from a directed video. Plan these
before writing a single tween:

- **Beat kinds.** Type each beat: hero (the opener, always first), stat (one
  wallpaper-scale number), list (2-4 parallel facts), quote (one strong
  line), cards (2-4 labeled tiles), ledger (log rows), mockup (a product
  frame), payoff (the closer, always last), caption (plain statement, the
  default). The kind decides the layout AND the energy of its entrance.
- **Z-index handoff.** Scenes are absolutely positioned stacks. The incoming
  scene briefly takes a HIGHER z-index during a directional transition (so
  it slides OVER the outgoing one), then returns to the shared scene level
  once the outgoing scene is hidden. Reset the outgoing scene's transforms
  (`x/y: 0`, `filter: blur(0px)`) AFTER hiding it so a later loopback or
  still-extraction never catches a half-transformed scene.
- **Transition windows.** A transition is a ~0.34-0.45s window straddling
  the scene boundary: the outgoing scene starts exiting ~0.34s BEFORE the
  boundary, the incoming scene is fully owned by ~0.04s after it. Voiceover
  for the next beat starts ON the boundary, so the window must never delay
  the first reveal of the incoming scene's headline.
- **Stagger discipline.** Stagger entrances in order of importance: eyebrow,
  headline (word by word), rule, subhead, supporting panels. Total entrance
  stagger under ~0.5s; per-element steps of 0.08-0.12s. Decay the slide
  distance as elements get smaller (90px for the first word, 10px for the
  last) so the motion reads as one gesture, not six.
- **Sub-beat motion.** Any scene that holds longer than ~3s needs a second
  motion layer so the attention clock resets: a number ticking up, rows
  logging in one by one, a cursor moving and clicking, a ring cycling
  through options, a progress bar sweeping. Sub-beats are absolute-time
  tweens inside the scene's window, never infinite loops.
- **Energy.** Let the beat's energy (low/medium/high) scale entrance
  distance and duration. A hype reel's hero slams (short durations, long
  travel); an explainer's caption drifts (longer durations, short travel).

## Step 5: Timeline architecture (the skeleton)

One paused GSAP timeline, registered for the renderer, all times absolute.
Copy-paste starting block:

```html
<script>
  window.__timelines = window.__timelines || {};
  var tl = gsap.timeline({ paused: true });

  // --- scene handles -----------------------------------------------------
  var SC = {
    s1: document.getElementById("sc1"),
    s2: document.getElementById("sc2"),
    // ...
  };
  function showScene(el, t) { tl.set(el, { autoAlpha: 1 }, t); }
  function hideScene(el, t) { tl.set(el, { autoAlpha: 0 }, t); }

  // --- PRE-HIDE PASS (always the FIRST block) ----------------------------
  // Every element that reveals after t=0 is forced hidden at t=0, BEFORE
  // any reveal tween. A tl.from() alone leaks: until the playhead reaches
  // it, the element sits visible in its natural CSS state.
  tl.set("#s2-chip1", { autoAlpha: 0 }, 0);
  tl.set("#s2-chip2", { autoAlpha: 0 }, 0);
  // ... one line per later-revealing element, including caption pages.

  // --- scene 1 entrances (absolute times) --------------------------------
  tl.fromTo("#s1-headline", { y: 60, opacity: 0 },
    { y: 0, opacity: 1, duration: 0.7, ease: "expo.out" }, 0.4);
  // ...

  // --- transitions at the boundaries, ambient drifts, captions -----------

  window.__timelines["main"] = tl;
</script>
```

Rules the renderer enforces or assumes:

- Timed elements carry `class="clip"` plus `data-start`, `data-duration`,
  `data-track-index`; the root carries the composition id and total duration.
- Deterministic only: no `Math.random`, no `Date.now`, no
  `requestAnimationFrame`, no `setTimeout`, no network fetches, no infinite
  repeats. Finite ambient drift = `repeat: 0` with `duration` equal to the
  scene (or video) length.
- `autoAlpha` over plain `opacity` for anything that must be INVISIBLE:
  autoAlpha also sets `visibility: hidden`, which inherits to children
  (opacity does not), and shader rasterizers check each element's own
  computed style.
- Typed text is a `tl.call()` per character writing `textContent`
  (`0.035-0.045s` steps) plus a caret blinking via `yoyo` repeats with
  `ease: "steps(1)"`, killed with a final `tl.set(caret, {opacity: 0})`.

## Step 6: Named motion vocabulary

Author scenes by composing named verbs, not by reinventing tweens. The core
set (parameters tuned per design system):

| Verb | What it does | Shape |
|---|---|---|
| `rise` | default headline entrance, up and in | `fromTo(el, {y:60,opacity:0}, {y:0,opacity:1,duration:0.7,ease:"expo.out"})` |
| `kinetic-words` | headline reveals word by word, slide distance decays | per-word `fromTo` at `t0 + i*0.085`, slides `[90,64,44,28,16,10]` |
| `type-on` | eyebrow/command types in char by char with a blink caret | `tl.call()` per char + `steps(1)` caret yoyo |
| `rule-draw` | accent rule wipes left to right | `fromTo(el,{scaleX:0},{scaleX:1,ease:"power3.out",transformOrigin:"left center"})` |
| `card-slide` | a card rises from below with a spring settle | `fromTo(card,{y:70,opacity:0,scale:0.92},{y:0,opacity:1,scale:1,ease:"back.out(1.4)"})` |
| `count-up` | a number counts to its value | tween a proxy object, `onUpdate` writes `tabular-nums` text |
| `operator-float` | the hero subject drifts and breathes over the scene | `y` +/- 8px, `scale` 1.0-1.04, `sine.inOut`, scene-long |
| `glow-pulse` | a radial accent glow breathes opacity and scale | `sine.inOut`, finite repeats only |
| `whip` | fast directional push between scenes, with motion blur | incoming `fromTo {xPercent:100}` + `filter: blur(14px) -> 0`, 0.36s, `power4.inOut`; outgoing pushed to -19 percent and blurred |
| `dip` | dip-to-black through a blackout plate | plate `opacity 0->1` (0.34s, `power2.in`), swap scenes under it, `1->0` (0.42s, `power2.out`) |

Pick 4-6 per video and declare them in the design system. Vary the entrance
direction and the ambient verb per scene; at least 3 distinct eases per
scene. A vertical (9:16) canvas flips `whip` to the y axis.

## Step 7: Shader-rasterizer gotchas

Shader transitions (e.g. a chromatic split) capture BOTH boundary scenes'
DOM into canvas textures. Everything the rasterizer cannot draw, or that is
not truly hidden, corrupts the texture:

1. **autoAlpha, not opacity.** The raster helper checks each element's OWN
   computed style; `opacity: 0` does not inherit to children, so a child
   can still be sampled. `visibility: hidden` (what autoAlpha sets) inherits.
2. **No SVG stroke-only icons** inside shader-transition scenes; the
   canvas2d rasterizer cannot draw them and renders empty boxes.
3. **Explicit background-color on every scene.** A transparent scene
   rasterizes black. No `transparent` keyword inside gradients (use the
   target color at zero alpha); no gradients on elements thinner than 4px.
4. **The both-scenes trap.** The shader samples both scenes as-is. If scene
   A has not fully exited, it double-exposes into the transition. Fully hide
   A before B enters, or swap that cut to `dip`. Keep ONE shader cut per
   video, on the biggest moment, and restrict it to text-and-solid scenes.
5. **Always ship a fallback.** Guard the WebGL context; when `gl` is
   unavailable, degrade that boundary to a CSS crossfade.
6. Do not mix CSS and shader transitions on the SAME boundary.

## Step 8: Texture and finishing

Registry-first: install `grain-overlay` and friends from
`hyperframes-registry` when available. Hand-rolled fallbacks:

- **Grain:** an SVG `feTurbulence` fractal-noise tile as a data-URI
  background on a 200 percent-sized layer, `opacity` ~0.16,
  `mix-blend-mode: overlay`, drifting a few px over the full runtime
  (finite, `repeat: 0`), `z-index` above scenes, `pointer-events: none`.
- **Vignette:** an inset `box-shadow` frame on a full-bleed layer; strong on
  dark canvases, subtle on light ones.
- **Scrim:** a bottom-weighted `linear-gradient` (transparent at ~40
  percent, near-opaque at 100 percent) behind the caption band so copy
  stays legible over busy art.
- **Glow:** a radial-gradient accent blob, `mix-blend-mode: screen`, used as
  the breathing ambient layer behind hero subjects.
- **Blackout plate:** one full-bleed plate at the top z-index, `opacity: 0`,
  shared by every `dip` transition and the final close (fade it in over the
  last ~0.45s so the video ends on black, never on a freeze).

## Step 9: Compose, voiceover, timing

- Init the project with `hyperframes-cli`. Scene order: hook, body beats,
  proof beat, payoff/CTA card. Install catalog blocks via
  `hyperframes-registry` instead of hand-rolling: a shader transition for
  the ONE biggest cut, a social payoff card, grain-overlay, code-snippet or
  data-chart when the content calls for it.
- The payoff card uses the USER's display name, handle, and avatar from the
  brief. Render a verification badge ONLY if the user confirms the account
  is actually verified.
- Served-assets rule: copy every image/audio file INTO the project's
  `assets/` directory and reference it RELATIVELY (`assets/foo.png`). The
  headless renderer serves only the project directory; absolute paths and
  `file://` URIs render blank with no error.
- **Voiceover (voice bake-off):** write per-beat VO lines (one per scene,
  the first line is the hook). Generate the SAME sample line in 3-4
  candidate voices matched to the brief's tone (edge-tts: pick from
  `edge-tts --list-voices`; or Kokoro via `hyperframes-media`). The user
  picks; then generate every beat in the winner. Phonetically respell brand
  names in the SPOKEN text only; on-screen text stays correctly spelled.
- **Timing is derived, never guessed:** measure each beat clip with ffprobe;
  scene duration = VO duration + a small pad, with a minimum-frames floor,
  scaled to the target total. Concatenate beats into ONE audio track with
  silence gaps (ffmpeg adelay) so each line lands exactly on its scene.
- **Karaoke captions (sound-off default):** split each beat's spoken line
  into words and distribute them across the beat's MEASURED duration by
  character weight (no STT needed; you wrote the script). Page at most 5
  words or ~30 joined chars; show a page at its first word's start, hide at
  its last word's end + 0.15s, clamped to the next page's show time. One
  highlight tween per word (`duration: min(0.18, its time share)`). Reserve
  a bottom caption band (~8.5 percent of frame height) so scenes never
  collide with the strip; hard `tl.set` hide on every page exit so words
  never stick. The registry's 15 caption styles are valid starting points.

## Step 10: Quality gates (all of them, every render)

1. **PRE-HIDE sweep:** re-read the timeline; every later-revealing element
   has its `tl.set(el, {autoAlpha: 0}, 0)` line. Then prove it with stills.
2. `npx hyperframes lint`, `validate`, and `inspect` all clean.
3. **First-frame stills ritual:** render, then extract a still at EACH
   scene's FIRST frame (`ffmpeg -ss <scene-start> -frames:v 1`) and VIEW
   every one. First frames catch pre-hide leaks and missing assets that
   mid-scene stills miss. Numbered findings, smallest-fix-first.
4. ffprobe the MP4: H.264 video + AAC audio, both spanning the full duration.
5. Claim safety: every fact on screen or spoken traces to the brief's claims
   source. Unique output directory per run; never overwrite a prior render.

## Step 11: Deliver

Hand over the MP4 path with platform notes (size, aspect, duration). Offer
recuts to other aspects from the same composition. Revision etiquette: apply
user feedback as numbered fix rounds, change only what was flagged, one
re-render per round.

## Failure modes

| Symptom | Cause -> fix |
|---|---|
| An image is missing in the render, no error | Absolute or `file://` path. Copy into `assets/`, reference relatively. |
| Element flashes before its reveal | Pre-hide leak: `tl.set autoAlpha 0` at t=0; check first-frame stills. |
| Two scenes double-exposed mid-transition | The shader rasterizes both; fully hide scene A before B enters, or use dip-to-black for that cut. |
| Ghost icon boxes in a shader cut | SVG stroke-only icons in a rasterized scene; replace with text/divs or pre-hide the whole panel. |
| Captions stuck on screen / overlapping pages | Missing hard `tl.set` hide on page exit, or page hide not clamped to the next page's show time. |
| Captions drift off the voice | Timing guessed instead of measured; rebuild word times from ffprobe durations (char-weighted). |
| VO mispronounces a name | Respell spoken-only; on-screen stays correct. |
| Audio drifts from visuals | Re-measure with ffprobe and rebuild the adelay concat; timing is always derived, never guessed. |
| Render passes but looks wrong | The gates prove integrity, not taste. View the stills; iterate the design system, not just the tweens. |

## Native /video command

The framework also exposes this capability as a native chat command, V3
creative-director shape:

- Bare `/video` opens a guided wizard (kind, raw material, ranked styles,
  voice) and gates the render on an approved VISION card (angle, beat
  outline, imagery treatment). Buttons on adapters that support them, a
  numbered typed fallback everywhere else.
- `/video <brief> [--style name] [--aspect 16:9|9:16|1:1] [--design file]
  [--duration s] [--voice key] [--url u] [--research on|off]
  [--imagery stylized|photos|css]` is the one-shot power path (no wizard,
  no gate).
- `/video --kind promo --url <site>` (no brief) is the flagged wizard: text
  vision + `/video approve | /video redo [notes] | /video cancel`.
- Hand it a URL and it researches the site read-only: facts feed the claim
  gate, the page's own colors/fonts become an offered "your brand" style,
  and its images become reference art.

The command is model-agnostic (its LLM moments run through the runtime
lanes) and delivers the finished MP4 back into the same conversation. Use
the command for guided or one-shot renders; use this skill when directing a
richer, multi-revision production by hand. Details:
`docs/manual/features/video-generation.md`.

## Archon workflow (optional rail)

For dev machines with the Archon workflow engine installed, the same
direction loop exists as a reviewable DAG:
`.archon/workflows/video-production.yaml` with atoms
`.archon/commands/video-{intake,research,vision,compose,qa-stills,fix,report}.md`.
It runs preflight, intake, research, a pipeline-parity vision, a NATIVE
human approval gate (reject with notes regenerates the vision, up to 3
rounds), an agent-crafted composition per THIS skill, lint, render,
first-frame stills, fresh-context QA, a bounded fix loop (max 3 rounds),
and a final verified report.

Describing or reading this workflow is not authorization to run it; run
only on a direct imperative, from a regular shell, never from inside a
Claude Code session. Renders land under the run's artifacts directory and
are never committed.

```
archon validate workflows
archon workflow run video-production "<brief, URL, or flags>"
```

## Boundaries

- This skill never posts to any platform; it produces files.
- It never invents claims; no claims source in the brief means no numbers in
  the video.
- For composition/HTML mechanics, defer to the `hyperframes` skill rather
  than restating it; this skill is the director, not the renderer.
