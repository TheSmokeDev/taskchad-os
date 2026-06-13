# Video Generation (`/video`)

Status: Shipped, V3 creative-director engine (wizard + vision gate + research)
Owner: chat slice (`core_handlers.handle_video`) + scripts slice (`video_pipeline.py`, `video_archetypes.py`, `video_styles.py`, `video_research.py`, `video_imagegen.py`)
Last updated: 2026-06-12

## What It Is

`/video` turns an idea into a finished MP4: HTML/CSS scenes rendered
deterministically through HyperFrames (headless Chrome + FFmpeg), with a
generated voiceover driving the scene timing and karaoke captions burned in.
It is a native router command, so it works identically from Telegram,
Discord, Slack, WhatsApp, the web dashboard chat, and the CLI. The finished
video is delivered back into the same conversation as a file attachment.

V3 makes the engine a creative director. A bare `/video` opens a guided
wizard (kind, raw material, style, voice), optionally researches a URL you
hand it, and then presents a VISION, the angle, the beat outline, and the
imagery treatment, that you approve BEFORE anything renders. An explicit
brief (`/video our app now syncs offline`) still bypasses everything as a
one-shot power path.

The pipeline is model-agnostic by construction. Its LLM moments (the vision,
beat copy from the brief, an optional quality-judge pass) run through the
runtime lanes, so the command behaves the same whether the deployment runs
the Claude, Codex, or Gemini lane. Everything else is deterministic Python.

## Operator Quickstart

```
/video                                       guided wizard with vision approval
/video styles                                list the style library
/video our app now syncs offline --style blockframe     one-shot power path
/video product launch teaser --aspect 9:16 --duration 20
/video --kind promo --url https://yoursite.example      flagged wizard (text vision)
/video approve | /video redo [notes] | /video cancel    drive a pending vision
/video status                                render state + wizard stage
```

- A brief renders immediately (no wizard, no gate). Bare `/video` walks you
  through the guided flow and gates the render on your approval.
- One render runs at a time; `/video status` reports progress and the wizard
  stage. On chat adapters the command acknowledges immediately and sends the
  MP4 when the render finishes (typically a few minutes). On the CLI it
  renders inline.

## The Guided Wizard

Five steps, per-channel state with a 10 minute TTL refreshed on every
transition. Telegram and Discord get buttons; every step ALSO carries a
numbered typed fallback, so the same flow works on buttonless adapters
(Slack, web chat, WhatsApp, CLI). Picker steps are match-only: a reply that
is not an option number or name falls through to normal chat with the wizard
kept pending ("2" advances the wizard; "2pm works for me" is just a chat
message). "cancel" or "stop" exits at any step.

```
STEP 1  Let's make a video. What kind?
          1 event recap  2 brand promo  3 product launch
          4 explainer  5 hype reel  6 surprise me
        (reply with a number, or skip everything: /video <brief> --style <name>)

STEP 2  <kind> it is. Now the raw material - drop a URL to build from,
        a theme, or a full brief. Flags ride along: --aspect 9:16 --duration 20.

        (URL detected -> "Reading the site... a few seconds." Research failure
         never stalls the wizard: "Couldn't read <host> (<reason>). Going
         theme-only - your words carry it.")

STEP 3  Here's how I'd style it:
          1. your brand (from the site)        <- only when research derived a design
          2. * <name> (recommended)            <- ranked against brief + kind + dossier
          3..n remaining styles, "surprise me" last
        (reply with a number or a name)

STEP 4  Style locked: <style>. Pick the narration voice:
          1..6 curated voices
        (reply with a number or a name)

STEP 5  THE VISION
        <angle - one sentence>

        beats:
          1. [hero] <summary>
          2. [stat] <summary>
          ...
        imagery: <treatment> - <one-line why>
        look: <style>   voice: <key>   ~<N>s   <aspect>

        Reply with notes to redo it your way.
        [Approve & render] [Change style] [Redo vision] [Cancel]
```

Re-entrancy: bare `/video` mid-wizard restarts at step 1 ("Restarting the
video setup."); `/video <brief>` mid-wizard pops the wizard and runs the
power path. An expired setup answers "That video setup expired (10 min).
/video to start fresh."

## The Vision Gate

The vision is the approval artifact: an angle (one sentence), 2 to 8 outline
beats (`{kind, summary}` each), an imagery treatment with a one-line why,
and the resolved duration and aspect. It is drafted through the runtime
lanes with schema clamps and the claim gate (numbers must come from the
brief or the research), one retry, then a deterministic fallback. The card
is production notes for you, not narration.

What approval binds into the render:

| Vision field | Render binding |
|---|---|
| beat outline | seeds the copy lane: beat count, kinds (positional), and summaries; duration fill extends the outline when the target needs more beats |
| imagery treatment | `css` renders pure typographic scenes (art off); `photos` maps researched reference images onto art-eligible beats (no generation); `stylized` keeps the generated-art path |
| duration / aspect | passed as the render target |
| style | the picked style; "your brand (from the site)" resolves the dossier's derived design |
| voice | the picked narration voice spec |
| research dossier | rides along: claims allowlist, untrusted background context for the copy lane, reference images |

The whole approved vision is recorded verbatim in the run's `beats.json` for
audit, alongside the resolved archetype per scene.

Gate actions: **Approve & render** kicks off the existing render machinery
(the wizard is popped only after the dependency and concurrency guards pass,
so a refused approve can be retried). **Change style** loops back to the
ranked style keyboard and then redrafts the vision in the new look without
re-asking the voice. **Redo vision** (or any typed reply at step 5) feeds
your notes back as operator feedback and produces a different take, the
prior outline is sent along so it does not repeat itself. **Cancel** scraps
the setup.

Approve while a render is already running keeps the pending vision: "Your
vision is saved - /video approve to retry once it finishes." Same for
missing dependencies.

Typed path for buttonless surfaces: `/video --kind promo --url <url>` (no
brief) runs the flagged wizard, inline research, top-ranked style, default
voice, then prints the vision as plain text with the footer
`-> /video approve | /video redo [notes] | /video cancel`. This is the CLI
shape of the gate.

## Site And Theme Research

Hand the wizard (or `--url` / `--research`) a URL and the research stage
builds a dossier before anything is written:

- **Facts**: number-bearing sentences first, capped, deduped. They feed the
  copy prompt as untrusted background data (explicitly framed as data, never
  instructions) and join the claim gate's allowlist, so the video may cite
  the site's real numbers and nothing invented.
- **Derived design**: the page's own colors (CSS custom properties, style
  blocks, inline styles) and fonts (Google Fonts links, font-family rules)
  become a complete validated design dict, offered first in the style step
  as "your brand (from the site)".
- **Reference images**: og:image / twitter:image / apple-touch-icon / the
  largest content image, raster-only (magic-byte checked), size-capped.
  They become identity references for generated art, or the literal scene
  art under the `photos` treatment.

Theme mode: a non-URL query searches the web (optional, `EXA_API_KEY`; no
key means no search, the wizard simply continues theme-only), fetches the
top two results, and merges their facts. Theme dossiers never derive a
design and keep at most one reference image.

Research is read-only and audited: every network touch (fetch, search,
image) lands in the dossier's `audit` list with timing and byte counts, and
the dossier is persisted (minus the cached page html) as `research.json` in
the run dir. Research failure never fails a render and never stalls the
wizard.

## Scene Archetypes

Every beat resolves to one of nine deterministic scene builders
(`video_archetypes.KINDS`); the vision outline picks kinds explicitly and
the parser backfills from content when it must (a stat present means a stat
scene, two or more items mean cards or ledger, first scene hero, last scene
payoff):

| Kind | Scene |
|---|---|
| `hero` | the opening title scene (first beat is always hero) |
| `stat` | one wallpaper-scale number with a label; only when a real number exists |
| `list` | a checklist moment of 2 to 4 parallel facts |
| `quote` | one strong line delivered like a quotation |
| `cards` | 2 to 4 labeled tiles side by side |
| `ledger` | compact log rows, receipts-style |
| `mockup` | a product or website walkthrough moment |
| `payoff` | the closing scene (last beat is always payoff) |
| `caption` | a plain statement scene, the default |

Per-beat `energy` (low/medium/high) tunes the motion. Scene boundaries pick
from `cut`, `crossfade`, `slide`, `whip`, and `dip` transitions; the
`chroma_split` WebGL shader transition exists behind a style flag
(default off everywhere) and only fires between raster-safe archetypes,
degrading to a crossfade otherwise.

## Imagery Treatments

The vision proposes one of three treatments; you approve or override it
(`--imagery` flag or the vision card):

- `stylized`: generated identity-locked art panels (the optional image
  adapter; researched reference images ride along as identity refs).
- `photos`: real photographs collected from the researched site, mapped
  directly onto art-eligible beats, hero first, no generation. Only
  offered when the dossier actually carries visuals; otherwise it is
  coerced to `stylized` with a note.
- `css`: pure typographic scenes, no imagery (exactly `art="off"`).

## The Style Library

The capability deliberately ships with a plethora of looks rather than one
hardcoded brand. The built-in registry ports designs from the public
HyperFrames design gallery (hyperframes.dev/design), including BlockFrame,
Coral, Capsule, Cobalt Grid, Editorial Forest, Bold Poster, Broadside, and
Blue Professional, plus a neutral default. Every visual decision in the
renderer comes from the selected design dict: palette, typography, motion,
and texture flags (`grain`, `vignette`, `hud_scanline`, `typed_eyebrow`,
`shader_transitions`) that add film grain, a vignette, HUD scanlines, a
typed-text eyebrow, or the shader transition on styles that carry them.

Style resolution precedence at render time: `--design` file > explicit
`--style` ("auto" suggests against the brief and dossier) > the dossier's
derived design > env (`VIDEO_DESIGN_FILE`, then `VIDEO_STYLE`) > neutral.
The wizard's style step ranks the registry per brief keywords, picked kind,
and dossier signals (`suggest_styles_ranked`), with an optional env-gated
LLM refinement (`VIDEO_SUGGEST_LLM`).

Per-deployment branding: set `VIDEO_STYLE` (a library name) or
`VIDEO_DESIGN_FILE` (a path to your own design file) in the scripts `.env`
and bare briefs render in your house style by default.

## Karaoke Captions

When a voice track exists, the renderer burns in karaoke captions: words are
timed by character weight over each beat's ffprobe-measured voiceover
duration (no STT, the script is known), grouped into pages of at most 5
words or 30 characters, shown at the first word's start and hidden after
the last word ends, with a per-word highlight tween. Style-aware: mono
font, a translucent pill over the canvas, accent highlight with a luminance
fallback. The composition reserves a caption band at the bottom so scenes
never collide with the strip. Switch: `--captions on|off` (pipeline CLI) or
`VIDEO_CAPTIONS` env; default on.

## Flags And Subcommands

```
/video <brief> [flags...]        one-shot power path (no wizard, no gate)
/video [flags...]                no brief + --kind/--url = flagged wizard (text vision)
```

| Flag | Values | What it does |
|---|---|---|
| `--style` | registry name or `auto` | pick a library style; `auto` suggests from the brief (and dossier) |
| `--design` | file path | derive the look from your own design.md/JSON token file |
| `--aspect` | `16:9` `9:16` `1:1` | canvas (default: honor the brief's stated orientation, else 16:9) |
| `--duration` | seconds (8..120) | target length (default: honor the brief's stated length, else 30) |
| `--kind` | `event` `promo` `launch` `explainer` `hype` `surprise` | video kind (tunes style ranking + duration default) |
| `--url` | http(s) URL | research source; with a brief it researches without ceremony |
| `--research` | `on` `off` | research switch (default on when a URL is present) |
| `--voice` | curated key or `Name\|+N%` spec | narration voice (curated keys resolve to full specs) |
| `--imagery` | `stylized` `photos` `css` | force the imagery treatment (`photos` needs dossier visuals) |

| Subcommand | What it does |
|---|---|
| `/video styles` | list the style library |
| `/video status` | render state + pending wizard stage |
| `/video approve` | approve the pending vision and render |
| `/video redo [notes]` | redraft the pending vision (notes become operator feedback) |
| `/video cancel` | scrap the pending wizard |

The scripts-side CLI (`video_pipeline.py`) adds `--claims-source`,
`--output-root`, `--art-dir`, `--art off`, `--captions on|off`,
`--research <url-or-theme>`, `--art-max N`, `--list-styles`, and
`--check-deps` for direct pipeline runs.

## Environment Variables

All read at call time in the scripts `.env` (never cached at import):

| Var | Default | Purpose |
|---|---|---|
| `VIDEO_STYLE` | unset | default library style for bare renders |
| `VIDEO_DESIGN_FILE` | unset | default design file (wins over `VIDEO_STYLE`) |
| `VIDEO_VOICE` | pipeline default voice | voice spec `ShortName\|+N%` |
| `VIDEO_VOICE_RATE` | `+14%` | rate fallback when the spec has none |
| `VIDEO_CAPTIONS` | `on` | karaoke captions switch |
| `VIDEO_ART` | on | `off` disables generated art (an approved vision outranks this) |
| `VIDEO_ART_MAX` | `1` | cap on generated art images per render |
| `VIDEO_RENDER_TIMEOUT_S` | `900` | HyperFrames render subprocess timeout |
| `VIDEO_RENDER_QUALITY` | `standard` | HyperFrames render quality preset |
| `VIDEO_SUGGEST_LLM` | off | let one runtime-lane call refine the style ranking |
| `EXA_API_KEY` | unset | enables web search for theme research (optional) |

## Vertical Slice Architecture

| Layer | File | Role |
|---|---|---|
| Command registry | `.claude/chat/commands.py` | `/video` router-typed entry |
| Handler + wizard | `.claude/chat/core_handlers.py` (`handle_video`, `handle_video_button`, `try_consume_video_message`) | flags, wizard state machine, vision gate, dependency preflight, concurrency guard, background render task, same-adapter delivery |
| Pipeline | `.claude/scripts/video_pipeline.py` | vision + beats (runtime lanes), duration fill, VO synthesis, VO-driven timing, karaoke captions, HTML composition, HyperFrames render, ffprobe verify, scorecard |
| Archetypes | `.claude/scripts/video_archetypes.py` | nine deterministic scene builders, transitions, texture layer, fragment validation |
| Style registry | `.claude/scripts/video_styles.py` | design dicts, `resolve_design()` precedence, `design_from_tokens()`, ranked suggestion |
| Research | `.claude/scripts/video_research.py` | URL/theme dossiers: facts, derived design, reference images, claims allowlist, audit |
| Image adapter | `.claude/scripts/video_imagegen.py` | optional generated art with identity references |
| Tests | `.claude/scripts/tests/test_video_*.py`, `tests/test_core_handlers_video.py` | timing math, claim gate, style precedence, composition invariants, wizard/gate matrix |

Renders land under `.claude/data/video-renders/<run-id>/` (unique per run,
never committed) with `beats.json` and `research.json` audit records.

## Model-Agnostic Contract

- The vision, beat copy, and the optional judge pass use
  `run_with_runtime_lanes` with `TEXT_REASONING`, `allowed_tools=[]`,
  `max_turns=1`. No provider SDK is imported by the pipeline or the handler.
- If no runtime lane is available, the vision and the beats both fall back
  to deterministic compositions built from the raw brief, and the scorecard
  falls back to the automated pass. The render never blocks on a provider.
- Generated art is an optional adapter; without it, scenes are styled
  HTML/CSS. An optional `art_dir` lets an operator drop in their own
  imagery (newest file used, copied into the served assets).

## Safety Contract

- The command produces files; it never posts to any platform.
- Claim safety: facts in the video come from the brief, the optional claims
  source, and the research dossier's allowlist; the pipeline rejects
  invented metrics and superlatives, in the vision and in the copy.
- Research is read-only with an audit row for every network touch. Page and
  image fetches are size-capped; images are magic-byte checked.
- Untrusted-data framing: researched text enters prompts inside explicit
  data tags with an instruction to ignore anything instruction-shaped.
- Dependency preflight refuses cleanly with install hints when node, npx,
  ffmpeg, ffprobe, or edge-tts are missing. No partial renders, no crashes.
- One render at a time per process; state is reported by `/video status`.

## Prerequisites

Node 18+ (`npx hyperframes`), ffmpeg + ffprobe on PATH, the `edge-tts`
Python package for voiceover. The HyperFrames helper skills
(`npx hyperframes skills`) are recommended for authoring custom compositions
but are not required by `/video`.

## Validation Checklist

A native command needs four registrations: the `COMMANDS` row and the
handler (dispatch), plus `TELEGRAM_NATIVE_COMMANDS` and a `CATEGORIES`
group in `commands.py` (the Telegram `/` autocomplete menu). After a bot
restart the log line "Registered N slash commands with Telegram" must
grow by one; Telegram clients cache the menu, so reopen the chat to see
the new entry.

```
# scripts-side suites (303 tests):
#   test_video_pipeline.py (93)  test_video_styles.py (43)
#   test_video_archetypes.py (127)  test_video_research.py (22)
#   test_video_imagegen.py (18)
uv run pytest tests/test_video_pipeline.py tests/test_video_styles.py \
  tests/test_video_archetypes.py tests/test_video_research.py \
  tests/test_video_imagegen.py -q
# chat-side wizard/gate matrix (44 tests):
uv run pytest tests/test_core_handlers_video.py -q
uv run python -m py_compile ../chat/core_handlers.py ../chat/commands.py
# CLI smoke (renders inline):
#   /video styles
#   /video a two beat smoke test --duration 10
#   /video --kind promo --url https://example.com   (text vision + /video approve)
# ffprobe the produced MP4: H.264 video + AAC audio, full duration.
```

## Common Failure Modes

| Symptom | Cause and fix |
|---|---|
| "Video rendering needs these tools installed" | Missing system deps; install node/ffmpeg/edge-tts and retry. A saved vision survives: `/video approve` once they're in. |
| "A render is already running" | Single-render guard; wait or check `/video status`. |
| "That video setup expired (10 min)" | Wizard TTL elapsed (or the bot restarted mid-wizard). Start again with `/video`; the power path needs no state. |
| "Couldn't read <host> ... Going theme-only" | The site refused, timed out, or had no readable content. The wizard continues without research; facts then come only from your words. |
| Approve answered "render already running, vision saved" | The gate refuses to double-render. The pending vision is kept; approve again when the current render finishes. |
| Vision proposed `photos` but the render used stylized art | The dossier carried no usable reference images; `photos` is coerced to `stylized` and noted in the vision/score notes. |
| Render ok but a scene shows no image | Asset referenced by absolute path; use the served assets dir (the pipeline copies `art_dir` files in automatically). |
| Voiceover mispronounces a name | Respell it phonetically in the brief's spoken text; on-screen text stays correct. |
| MP4 did not arrive in chat | Check the completion message for the file path; very large renders may exceed a platform's upload limit and are then delivered as a path. |
