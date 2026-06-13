---
description: Apply exactly one numbered fix round from the QA issues - change only what was flagged, then re-verify.
argument-hint: <the QA issues JSON, or empty to read the latest QA verdict from the workflow context>
---

# Video Fix (One Round)

You are one fix round of the video-production workflow. Revision etiquette
is strict: apply the flagged fixes, change NOTHING else, re-render once.

## Input

QA issues: $ARGUMENTS

When empty, use the most recent QA verdict available in context.

## Rules

1. Work through the issues IN ORDER by their numbers. For each one, make the
   smallest change in `$ARTIFACTS_DIR/composition/` that resolves it
   (`fix_hint` is the starting point, not a constraint).
2. Touch only what an issue names. No drive-by refactors, no new scenes, no
   palette adjustments that were not flagged, no copy rewrites beyond the
   flagged text.
3. Pre-hide discipline applies to every fix: a new or moved element that
   reveals after t=0 needs its `tl.set(el, {autoAlpha: 0}, 0)` at the top of
   the timeline.
4. Keep determinism: no Math.random, no Date.now, no requestAnimationFrame,
   no setTimeout, no network fetches.
5. After the edits: `npx hyperframes lint` and `npx hyperframes validate`
   inside the composition dir must pass clean, then re-render the MP4 and
   re-extract the per-scene first-frame stills into `$ARTIFACTS_DIR/stills/`
   (overwrite) using `$ARTIFACTS_DIR/scenes.json` for the scene start times.

## Output

A numbered list mirroring the input issues: what was changed for each, plus
the lint/validate result and the re-rendered MP4 path. If an issue could not
be fixed without violating rule 2, say so explicitly instead of improvising.
