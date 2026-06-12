# Video Generation (`/video`)

Status: Shipped, native command across all adapters
Owner: chat slice (`core_handlers.handle_video`) + scripts slice (`video_pipeline.py`, `video_styles.py`)
Last updated: 2026-06-11

## What It Is

`/video` turns a one-line brief into a finished MP4: HTML/CSS scenes rendered
deterministically through HyperFrames (headless Chrome + FFmpeg), with a
generated voiceover driving the scene timing. It is a native router command,
so it works identically from Telegram, Discord, Slack, WhatsApp, the web
dashboard chat, and the CLI. The finished video is delivered back into the
same conversation as a file attachment.

The pipeline is model-agnostic by construction. Its only LLM moments (beat
copy from the brief, an optional quality-judge pass) run through the runtime
lanes, so the command behaves the same whether the deployment runs the
Claude, Codex, or Gemini lane. Everything else is deterministic Python.

## Operator Quickstart

```
/video styles
/video our app now syncs offline --style blockframe
/video product launch teaser --aspect 9:16 --duration 20
/video quarterly recap --design path/to/your-design.md
/video status
```

- `styles` lists the built-in style library.
- A bare brief renders with the neutral default style.
- `--style <name>` picks a library style; `--design <file>` derives the look
  from your own design file (markdown tokens or JSON).
- `--aspect 16:9|9:16|1:1`, `--duration <seconds>` shape the output.
- One render runs at a time; `/video status` reports progress. On chat
  adapters the command acknowledges immediately and sends the MP4 when the
  render finishes (typically a few minutes). On the CLI it renders inline.

## The Style Library

The capability deliberately ships with a plethora of looks rather than one
hardcoded brand. The built-in registry ports designs from the public
HyperFrames design gallery (hyperframes.dev/design), including BlockFrame,
Coral, Capsule, Cobalt Grid, Editorial Forest, Bold Poster, Broadside, and
Blue Professional, plus a neutral default. Every visual decision in the
renderer comes from the selected design dict: palette, typography, motion.

Per-deployment branding: set `VIDEO_STYLE` (a library name) or
`VIDEO_DESIGN_FILE` (a path to your own design file) in the scripts `.env`
and bare `/video <brief>` renders in your house style by default. Operators
can also keep multiple design files and switch per render with `--design`.

## Vertical Slice Architecture

| Layer | File | Role |
|---|---|---|
| Command registry | `.claude/chat/commands.py` | `/video` router-typed entry |
| Handler | `.claude/chat/core_handlers.py` (`handle_video`) | parse flags, dependency preflight, concurrency guard, background render task, same-adapter delivery |
| Pipeline | `.claude/scripts/video_pipeline.py` | brief to beats (runtime lanes), VO synthesis, VO-driven timing, HTML composition, HyperFrames render, ffprobe verify, scorecard |
| Style registry | `.claude/scripts/video_styles.py` | design dicts + `resolve_design()` precedence (file > name > env > neutral) |
| Tests | `.claude/scripts/tests/test_video_pipeline.py`, `test_video_styles.py` | timing math, claim gate, style precedence, composition invariants |

Renders land under `.claude/data/video-renders/<run-id>/` (unique per run,
never committed).

## Model-Agnostic Contract

- Beat copy and the optional judge pass use `run_with_runtime_lanes` with
  `TEXT_REASONING`, `allowed_tools=[]`, `max_turns=1`. No provider SDK is
  imported by the pipeline or the handler.
- If no runtime lane is available, the pipeline falls back to a deterministic
  two-beat composition built from the raw brief, and the scorecard falls back
  to the automated pass. The render never blocks on a provider.
- Image generation is NOT part of this public pipeline; heroes are styled
  HTML/CSS scenes. An optional `art_dir` lets an operator drop in their own
  imagery (newest file used, copied into the served assets).

## Safety Contract

- The command produces files; it never posts to any platform.
- Claim safety: facts in the video come from the brief (and optional claims
  source); the pipeline rejects invented metrics and superlatives.
- Dependency preflight refuses cleanly with install hints when node, npx,
  ffmpeg, ffprobe, or edge-tts are missing. No partial renders, no crashes.
- One render at a time per process; state is reported by `/video status`.

## Prerequisites

Node 18+ (`npx hyperframes`), ffmpeg + ffprobe on PATH, the `edge-tts`
Python package for voiceover. The HyperFrames helper skills
(`npx hyperframes skills`) are recommended for authoring custom compositions
but are not required by `/video`.

## Validation Checklist

```
uv run pytest tests/test_video_styles.py tests/test_video_pipeline.py -q
uv run python -m py_compile ../chat/core_handlers.py ../chat/commands.py
# CLI smoke (renders inline):
#   /video styles
#   /video a two beat smoke test --duration 10
# ffprobe the produced MP4: H.264 video + AAC audio, full duration.
```

## Common Failure Modes

| Symptom | Cause and fix |
|---|---|
| "Video rendering needs these tools installed" | Missing system deps; install node/ffmpeg/edge-tts and retry. |
| "A render is already running" | Single-render guard; wait or check `/video status`. |
| Render ok but a scene shows no image | Asset referenced by absolute path; use the served assets dir (the pipeline copies `art_dir` files in automatically). |
| Voiceover mispronounces a name | Respell it phonetically in the brief's spoken text; on-screen text stays correct. |
| MP4 did not arrive in chat | Check the completion message for the file path; very large renders may exceed a platform's upload limit and are then delivered as a path. |
