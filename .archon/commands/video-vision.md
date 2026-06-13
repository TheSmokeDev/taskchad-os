---
description: Draft the operator-facing VISION (angle, beat outline, imagery treatment) with pipeline parity, ready for the approval gate.
argument-hint: (no arguments; reads intake.json and dossier.json from $ARTIFACTS_DIR)
---

# Video Vision

You are the vision step of the video-production workflow. Produce the
production plan a human approves BEFORE anything is composed or rendered.
Use the SAME engine the native pipeline uses (`generate_vision` in
`video_pipeline.py`) so the gate artifact matches pipeline behavior exactly:
schema clamps, the claim gate over angle + summaries, one retry, then a
deterministic fallback. Never invent numbers; `photos` imagery is only
proposed when the dossier carries visuals.

## Run

From the repo root:

```bash
cd .claude/scripts
uv run python -c '
import json, os, pathlib
art = pathlib.Path(os.environ["ARTIFACTS_DIR"])
intake = json.loads((art / "intake.json").read_text(encoding="utf-8"))
dossier = None
p = art / "dossier.json"
if p.is_file():
    dossier = json.loads(p.read_text(encoding="utf-8"))
import video_pipeline
brief = (intake.get("brief") or "").strip()
if not brief and dossier:
    title = str(dossier.get("title") or "").strip()
    brief = f"a video about {title}" if title else ""
if not brief:
    brief = (intake.get("url") or "a short brand video").strip()
vision = video_pipeline.generate_vision(
    brief,
    kind=(intake.get("kind") or None),
    dossier=dossier,
    style=(intake.get("style") or None),
    duration_s=(int(intake["duration"]) if str(intake.get("duration") or "").strip() else None),
    aspect=(intake.get("aspect") or None),
)
(art / "vision.json").write_text(json.dumps(vision, indent=2), encoding="utf-8")
print(json.dumps(vision, indent=2))
'
```

The call contract (do not deviate): `generate_vision(brief, *, kind, dossier,
style, voice_label, duration_s, aspect, feedback, prior_vision)` returns
`{ok, angle, beats: [{kind, summary}], imagery: {treatment, note},
duration_s, aspect, style, voice, provider, notes}` and never raises.

Redo round (when the gate rejected a prior vision): pass the operator's
rejection feedback as `feedback="..."` and the previous vision dict as
`prior_vision=json.loads((art / "vision.json").read_text(...))` so the
engine produces a DIFFERENT take instead of repeating itself. Overwrite
`vision.json` with the new result.

## Output

Your final output must be ONLY the vision JSON object (the same content as
`$ARTIFACTS_DIR/vision.json`), nothing else. The approval gate renders it
for the operator.
