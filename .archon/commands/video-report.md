---
description: Final production report - MP4 path, ffprobe verification, scorecard, and the artifacts index.
argument-hint: (no arguments; reads the run artifacts from $ARTIFACTS_DIR)
---

# Video Report

You are the closing step of the video-production workflow. Produce the
operator-facing report for this run. Verify, never assume.

## Procedure

1. **Locate the MP4** under `$ARTIFACTS_DIR/composition/` (the render
   output). If it does not exist, the report is a FAILURE report: say which
   step's artifacts stop existing and what the last error was.
2. **ffprobe verification** (run it, quote the real values):
   - container duration in seconds
   - video stream: codec (expect h264), resolution, fps
   - audio stream: codec (expect aac), duration
   - flag any mismatch against the approved vision's duration_s and aspect.
3. **Scorecard** (one line each, pass/fail with evidence):
   - vision fidelity: scene count and kinds match the approved outline
   - QA verdict: the final qa pass value and remaining minor issues
   - claim safety: numbers traced to brief/dossier facts
   - captions: karaoke present and synced to the voiceover
   - determinism: lint + validate clean on the final composition
4. **Artifacts index**: list with relative paths under `$ARTIFACTS_DIR/`:
   `intake.json`, `dossier.json` (if present), `research-assets/` (count),
   `vision.json`, `composition/` (project + design.md), `scenes.json`,
   `stills/` (count), and the MP4 (with size in MB).

## Output

A compact report in exactly this order: VERDICT (one line), MP4 (absolute
path + size + duration), ffprobe summary, scorecard, artifacts index, and a
"next steps" line (how to re-run a fix round or recut another aspect).
Renders and stills stay in the artifacts directory; never commit them.
